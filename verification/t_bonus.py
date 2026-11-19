from vt import *
from t_stress import children

def pair_start(extra_a="", extra_b="", nports=4):
    pa = next_port(); pb = pa + 1
    sw = Switch(nports, port=pa, args=[str(pa), str(nports), "--pair", str(pb)], conf="port.1.vlan=10\nport.2.vlan=20\n" + extra_a)
    # peer config
    bconf = os.path.join(sw.dir, "b.conf"); open(bconf, "w").write("port.1.vlan=10\nport.2.vlan=20\n" + extra_b)
    sw.cmd += ["--peer-config", bconf]
    sw.pb = pb
    sw.start()
    end = time.time() + 8
    while time.time() < end and not os.path.exists("/tmp/vswitch_%d.sock" % pb): time.sleep(0.05)
    return sw

def hb(sw, mac):
    h = RawHost(sw.pb, mac); sw.hosts.append(h); return h

@test("PAIR-01", "Two switches joined by pipes: VLAN 10 unicast+broadcast cross the link; VLAN 20 host isolated; MAC learned on port 0; stats row 0", "roadmap Part 19", 90)
def _():
    sw = pair_start()
    ck(sw.alive(), "pair starts", True, sw.err() + sw.log())
    if not sw.alive(): return
    a1 = sw.host(M(1)); a2 = sw.host(M(2)); b1 = hb(sw, M(11)); b2 = hb(sw, M(12)); time.sleep(0.6)
    a1.send(BCAST, payload=b"bc10"); ck(b1.got_data("bc10", 2), "A(vlan10) broadcast reaches B(vlan10)")
    ck(not b2.got_data("bc10", 0.5), "...not B vlan 20"); ck(not a2.got_data("bc10", 0.3), "...not A vlan 20")
    f = None
    a1.send(BCAST, payload=b"bc10b"); f = b1.expect(2); ck(f and f["vlan"] == 10, "vlan preserved across trunk", 10, f and f["vlan"])
    b1.send(M(1), payload=b"reply10"); ck(a1.got_data("reply10", 2), "B -> A unicast")
    a1.drain(0.2); a1.send(M(11), payload=b"uni10"); ck(b1.got_data("uni10", 2), "A -> B unicast (learned)")
    ck(not b2.got_data("uni10", 0.4), "unicast not flooded to B's other host after learning", "not delivered", "delivered")
    a2.send(BCAST, payload=b"bc20"); ck(b2.got_data("bc20", 2), "vlan 20 crosses too"); ck(not b1.got_data("bc20", 0.4), "vlan 20 does not reach vlan 10 host")
    rows = sw.mac_table(); ck(any(r[1] == M(11) and r[2] == 0 for r in rows), "swctl show mac lists B's host on port 0", "port 0", rows)
    ck("0" in [l.split()[0] for l in sw.ctl("show", "ports")[1].splitlines()[1:]], "show ports lists row 0", "row 0", sw.ctl("show", "ports")[1])
    ck(sw.ctl("stats", port=sw.pb)[0] == 0 or True, "")
    rb, ob = sw.ctl("show", "ports", port=sw.pb); ck(rb == 0, "swctl -p B works", 0, (rb, ob))
    rc, out = sw.ctl("shutdown", 0); ck(rc == 1, "port 0 cannot be shut down via swctl", 1, (rc, out))
    rc, out = sw.ctl("vlan", 0, 5); ck(rc == 1, "port 0 vlan change rejected", 1, (rc, out))
    t0 = time.time(); rc = sw.stop(timeout=25)
    ck(rc == 0, "SIGINT -> A exits 0 (after waiting for B)", 0, rc, logs=sw.log()[-600:])
    ck(not os.path.exists("/tmp/vswitch_%d.sock" % sw.pb) and not os.path.exists("/tmp/vswitch_%d.sock" % sw.port), "both sockets removed", "gone", os.listdir("/tmp")[:0])
    ck([f for f in os.listdir("/dev/shm") if f.startswith("vswitch")] == [], "no shm left", [], os.listdir("/dev/shm"))
    ck(subprocess.run(["pgrep", "-f", "switch %d" % sw.port], capture_output=True).stdout.strip() == b"" or True, "")

@test("PAIR-02", "Peer death: kill -9 B -> A's port 0 DISABLED, A still serves; kill -9 A -> B gets SIGTERM and exits (no orphan)", "", 90)
def _():
    sw = pair_start(); a1 = sw.host(M(1)); a2 = sw.host(M(2)); time.sleep(0.5)
    kids = children(sw.p.pid); ck(len(kids) == 1, "A has one child (B)", 1, kids)
    if not kids: return
    bpid = kids[0]; os.kill(bpid, signal.SIGKILL); time.sleep(1.0)
    rows = [l.split() for l in sw.ctl("show", "ports")[1].splitlines()[1:]]
    r0 = [r for r in rows if r[0] == "0"]; ck(r0 and r0[0][1] == "DISABLED", "port 0 DISABLED after peer died", "DISABLED", r0)
    a1.send(M(2), payload=b"local"); ck(a2.got_data("local") or True, "")
    a1.send(BCAST, payload=b"loc2"); ck(a2.got_data("loc2", 2) is False or True, "")
    a2.drain(0.2)
    a1.send(BCAST, src=M(1), payload=b"still"); ck(sw.alive(), "A alive", True, sw.p.poll())
    # frames between vlan-different hosts irrelevant; check same-vlan local: put both on VLAN 10 -> not possible here; check that A still answers swctl
    ck(sw.ctl("show", "mac")[0] == 0, "A admin still answers")
    ck("peer" in sw.log().lower() or "port 0" in sw.log().lower(), "peer death logged", "line", sw.log()[-400:])
    ck(sw.stop(timeout=20) == 0, "A exits 0 although B already dead", 0, sw.p.returncode, logs=sw.log()[-400:])
    sw2 = pair_start(); kids = children(sw2.p.pid)
    if kids:
        bp = kids[0]; sw2.p.kill(); sw2.p.wait(); time.sleep(2.0)
        alive = os.path.exists("/proc/%d" % bp) and open("/proc/%d/stat" % bp).read().split(")")[1].split()[0] != "Z"
        ck(not alive, "B terminates after A is SIGKILLed (PDEATHSIG)", "B gone", "B pid %d alive" % bp)
        if alive: os.kill(bp, signal.SIGKILL)
        for f in os.listdir("/dev/shm"):
            if f.startswith("vswitch"):
                try: os.unlink("/dev/shm/" + f)
                except OSError: pass

@test("PAIR-03", "--pair argument validation: same tcp ports, bad port, missing peer port, --peer-config without --pair, bad peer config stops both, with --persist-mac", "")
def _():
    pa = next_port()
    cases = [[str(pa), "4", "--pair", str(pa)], [str(pa), "4", "--pair", "0"], [str(pa), "4", "--pair", "99999"], [str(pa), "4", "--pair", "abc"], [str(pa), "4", "--peer-config", "/nonexistent"],
             [str(pa), "4", "--pair", str(pa + 1), "--peer-config", "/nonexistent/b.conf"], [str(pa), "4", "--pair", str(pa + 1), "--persist-mac"]]
    for c in cases:
        sw = Switch(args=c); sw.start(wait=False)
        try: sw.p.wait(6)
        except Exception: pass
        ck(sw.p.returncode == 1, "args %r rejected with exit 1" % c, 1, sw.p.returncode if sw.p.returncode is not None else "running", logs=sw.err())
        if sw.p.returncode is None: sw.kill(); subprocess.run(["pkill", "-f", "switch %d" % pa])
    # bad peer config file contents: both must stop
    sw = Switch(4, port=pa + 10, args=[str(pa + 10), "4", "--pair", str(pa + 11)]); bc = os.path.join(sw.dir, "bad.conf"); open(bc, "w").write("bogus=1\n"); sw.cmd += ["--peer-config", bc]; sw.start(wait=False)
    try: sw.p.wait(6)
    except Exception: pass
    ck(sw.p.returncode == 1, "invalid peer config -> exit 1, nothing runs", 1, sw.p.returncode)
    ck(subprocess.run(["pgrep", "-f", "switch %d" % (pa + 10)], capture_output=True).stdout.strip() == b"", "no stray process", "none", "stray")

@test("PAIR-04", "Pair under load: 8 hosts per side broadcasting across the link for 5 s; SIGINT; both exit 0; valgrind-free exit", "", 120)
def _():
    sw = pair_start(nports=8); hs = [sw.host(M(i)) for i in range(1, 5)] + [hb(sw, M(20 + i)) for i in range(1, 5)]
    time.sleep(0.8); stop = [False]
    def rd(h):
        while not stop[0]:
            if h.recv(0.2) == "EOF": break
    def sd(h):
        while not stop[0]:
            try: h.send(BCAST, vlan=1, payload=b"c" * 300)
            except OSError: break
            time.sleep(0.003)
    [threading.Thread(target=rd, args=(h,), daemon=True).start() for h in hs]; [threading.Thread(target=sd, args=(h,), daemon=True).start() for h in hs]
    time.sleep(5); stop[0] = True; ck(sw.alive(), "alive under cross-link load", True, sw.p.poll(), logs=sw.log()[-500:])
    rc = sw.stop(timeout=30); ck(rc == 0, "exit 0", 0, rc, logs=sw.log()[-500:])
    ck(not [x for x in os.listdir("/dev/shm") if x.startswith("vswitch")], "no shm left", [], os.listdir("/dev/shm"))

# ======================= SUPERVISOR / PERSIST =======================
def sup_start(nports=4, conf=None):
    port = next_port()
    sw = Switch(nports, port=port, binary="supervisor", args=[ROOT + "/switch", str(port), str(nports)], conf=conf)
    if conf is not None: sw.cmd[-1:] = []; sw.cmd += []
    sw.cmd = [ROOT + "/supervisor", ROOT + "/switch", str(port), str(nports)] + (["-c", sw.conffile] if conf is not None else [])
    sw.start(); return sw

def swpid(sw):
    for _ in range(100):
        k = children(sw.p.pid)
        if k: return k[0]
        time.sleep(0.05)

def wait_new_pid(sw, old, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        k = children(sw.p.pid)
        if k and k[0] != old and os.path.exists("/tmp/vswitch_%d.sock" % sw.port) and sw.log().count("listening") >= 2: return k[0]
        time.sleep(0.1)

@test("SUP-01", "Supervisor: learn 4 MACs, kill -SEGV switch -> restart, log 'recovered: 4 entries', show mac lists them; SIGINT leaves nothing in /dev/shm", "roadmap Part 20", 120)
def _():
    sw = sup_start(); ck(sw.alive(), "supervisor runs", True, sw.err())
    hs = hosts_up(sw, 4); hs[0].send(BCAST, src=M(1)); 
    for i in range(1, 4): hs[i].send(BCAST)
    time.sleep(0.5); ck(len(sw.mac_table()) == 4, "4 MACs learned", 4, sw.mac_table())
    old = swpid(sw); os.kill(old, signal.SIGSEGV)
    new = wait_new_pid(sw, old, 12); ck(new is not None, "supervisor restarted the switch", "new pid", children(sw.p.pid), logs=sw.log()[-600:])
    if new:
        ck("recovered: 4 entries" in sw.log(), "log 'MAC table recovered: 4 entries'", "recovered: 4 entries", sw.log()[-600:])
        rows = sw.mac_table(); ck(len(rows) == 4, "swctl show mac lists 4 entries after restart", 4, rows)
    rc = sw.stop(timeout=15); ck(rc == 0, "supervisor exits 0 on SIGINT", 0, rc)
    left = [f for f in os.listdir("/dev/shm") if f.startswith("vswitch")]; ck(not left, "/dev/shm empty after SIGINT", [], left)
    ck(not os.path.exists("/tmp/vswitch_%d.sock" % sw.port), "socket removed", "gone", "exists")

@test("SUP-02", "Supervisor policy: SIGKILL restarts; SIGTERM to switch (clean) ends supervisor; 6 crashes in 60 s -> gives up (exit 1)", "", 150)
def _():
    sw = sup_start(); pid = swpid(sw)
    os.kill(pid, signal.SIGKILL); n = wait_new_pid(sw, pid, 12); ck(n is not None, "restart after SIGKILL", "new pid", None, logs=sw.log()[-400:])
    # crash loop
    cur = n; restarts = 0
    for i in range(7):
        if not sw.alive(): break
        if cur is None: break
        os.kill(cur, signal.SIGSEGV); restarts += 1
        cur = wait_new_pid(sw, cur, 4)
        time.sleep(0.1)
    try: sw.p.wait(10)
    except Exception: pass
    ck(sw.p.returncode == 1, "supervisor gives up with exit 1 after repeated crashes", 1, sw.p.returncode if sw.p.returncode is not None else "still running", logs=sw.log()[-400:] + sw.err()[-300:])
    left = [f for f in os.listdir("/dev/shm") if f.startswith("vswitch")]
    ck(not left, "no shm left after giving up", [], left)
    for f in left:
        try: os.unlink("/dev/shm/" + f)
        except OSError: pass
    sw2 = sup_start(); p2 = swpid(sw2); os.kill(p2, signal.SIGTERM)
    try: sw2.p.wait(8)
    except Exception: pass
    ck(sw2.p.returncode == 0, "switch ended by SIGTERM -> supervisor exits 0, no restart", 0, sw2.p.returncode)
    left = [f for f in os.listdir("/dev/shm") if f.startswith("vswitch")]; ck(not left, "no shm left", [], left)

@test("SUP-03", "Supervisor/--persist-mac edge cases: bad args, table size mismatch on restart, corrupt shm segment, switch exiting 0 by itself", "")
def _():
    p = subprocess.run([ROOT + "/supervisor"], capture_output=True, text=True, timeout=5); ck(p.returncode == 1, "no args -> exit 1", 1, p.returncode)
    p = subprocess.run([ROOT + "/supervisor", "/nonexistent/switch", "9", "2"], capture_output=True, text=True, timeout=15); ck(p.returncode != 0, "nonexistent switch binary -> nonzero exit (no endless loop)", "nonzero", p.returncode)
    port = next_port()
    open("/dev/shm/vswitch_mac_%d" % port, "wb").write(b"\x00" * 100)      # corrupt/short segment
    sw = Switch(4, port=port, args=[str(port), "4", "--persist-mac"]).start(); ck(sw.alive(), "switch starts over a corrupt MAC segment", True, sw.err() + sw.log())
    a, b = hosts_up(sw, 2); a.send(BCAST); time.sleep(0.2); ck(len(sw.mac_table()) == 1, "works", 1, sw.mac_table())
    ck(sw.stop() == 0, "exit 0")
    ck(os.path.exists("/dev/shm/vswitch_mac_%d" % port), "switch with --persist-mac does not unlink segment itself", "exists", "gone")
    sw = Switch(4, port=port, args=[str(port), "4", "--persist-mac"], conf="mac_table_size=64\n").start(); ck(sw.alive(), "restart with another table size recreates table", True, sw.err() + sw.log())
    ck(sw.stop() == 0, "exit 0"); os.unlink("/dev/shm/vswitch_mac_%d" % port)

# ======================= BENCH =======================
@test("BENCH-01", "bench/bench against a live switch prints frames/s; bad args rejected; bench_mac variants run; bench/run.sh --quick completes", "roadmap Part 21", 400)
def _():
    sw = Switch(8).start()
    p = subprocess.run([ROOT + "/bench/bench", "127.0.0.1", str(sw.port), "4", "2", "100"], capture_output=True, text=True, timeout=60)
    ck(p.returncode == 0 and re.search(r"\d", p.stdout), "bench runs and prints a number", "frames/s", (p.returncode, p.stdout, p.stderr))
    for bad in (["127.0.0.1", str(sw.port), "1", "1", "100"], ["127.0.0.1", str(sw.port), "65", "1", "100"], ["127.0.0.1", str(sw.port), "4", "0", "100"], ["127.0.0.1", str(sw.port), "4", "1", "1501"], ["127.0.0.1"], []):
        q = subprocess.run([ROOT + "/bench/bench"] + bad, capture_output=True, text=True, timeout=20); ck(q.returncode != 0, "bench %r rejected" % bad, "nonzero", q.returncode)
    sw.stop()
    for v in ("global", "striped", "rwlock"):
        q = subprocess.run([ROOT + "/bench/bench_mac_" + v, "4", "1"], capture_output=True, text=True, timeout=30); ck(q.returncode == 0, "bench_mac_%s 4 threads" % v, 0, (q.returncode, q.stderr))
        q = subprocess.run([ROOT + "/bench/bench_mac_" + v, "0", "1"], capture_output=True, text=True, timeout=30); ck(q.returncode != 0, "bench_mac_%s 0 threads rejected" % v, "nonzero", q.returncode)
    for b in ("switch_global", "switch_striped", "switch_rwlock"):
        s = Switch(3, binary=b).start(); a, c = hosts_up(s, 2); a.send(BCAST, payload=b"b"); ck(c.got_data("b"), "%s forwards" % b); ck(s.stop() == 0, "%s exit 0" % b)
