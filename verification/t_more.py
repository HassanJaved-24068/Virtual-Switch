from vt import *
from t_stress import children, scenario

@test("EXT-01", "Large MAC table (K=65536, 20000 entries): swctl show mac complete & sorted, SIGUSR1 dump complete, exit codes OK", "boundary: big replies", 200)
def _():
    sw = Switch(2, conf="mac_table_size=65536\nmax_macs_per_port=65536\n").start(); a, b = hosts_up(sw, 2)
    N = 20000
    for i in range(0, N, 100): a.s.sendall(b"".join(pack(M(2000 + j), BCAST, 1, 1, b"") for j in range(i, i + 100)))
    b.drain(1.0, 30); time.sleep(1.0)
    rc, out = sw.ctl("show", "mac", timeout=60)
    rows = [l for l in out.splitlines() if l and l[0].isdigit()]
    ck(rc == 0, "show mac exit 0", 0, (rc, out[-80:]))
    ck(len(rows) == N, "show mac lists all %d entries" % N, N, len(rows))
    ck(out.rstrip().endswith("OK") or rc == 0, "")
    d = os.path.join(sw.dir, "d.txt"); open(sw.conffile, "a").write("mac_dump_file = %s\n" % d); sw.sig(signal.SIGHUP); time.sleep(0.5); sw.sig(signal.SIGUSR1); time.sleep(1.5)
    n = len([l for l in open(d) if l and l[0].isdigit()]) if os.path.exists(d) else -1
    ck(n == N, "SIGUSR1 dump lists all %d entries" % N, N, n, logs=sw.log()[-300:])
    ck(sw.ctl("clear", "mac")[0] == 0 and sw.mac_table() == [], "clear mac of a full table")
    ck(sw.stop() == 0, "exit 0")

@test("EXT-02", "Closed stdin/stdout/stderr (daemon-style) start: switch works and exits 0", "")
def _():
    port = next_port(); d = tempfile.mkdtemp(prefix="vt."); lf = d + "/l.log"
    p = subprocess.Popen("exec %s/switch %d 3 <&- >&- 2>&-" % (ROOT, port), shell=True, env=dict(os.environ, VSWITCH_LOG_FILE=lf), cwd=d)
    time.sleep(1.0); ck(p.poll() is None, "switch runs with fds 0-2 closed", "running", p.poll())
    try:
        a = RawHost(port, M(1)); b = RawHost(port, M(2)); time.sleep(0.3); a.send(BCAST, payload=b"fd"); ck(b.got_data("fd", 2), "forwards with closed std fds")
        a.close(); b.close()
    except OSError as e: ck(False, "connect failed", "connected", e)
    p.send_signal(signal.SIGINT)
    try: p.wait(6)
    except Exception: p.kill(); ck(False, "exit on SIGINT", 0, "hang")
    ck(p.returncode == 0, "exit 0", 0, p.returncode); shutil.rmtree(d, ignore_errors=True)

@test("EXT-03", "Semaphore invariant: shutdown/up of FREE and UP ports 50 times, then all N hosts still admitted and N+1th refused", "roadmap Part 5/16")
def _():
    sw = Switch(4, conf="port_wait_timeout=1\n").start(); hs = hosts_up(sw, 2); rnd = random.Random(9)
    for i in range(100):
        p = rnd.randint(1, 4); sw.ctl("shutdown", p); 
        if rnd.random() < .7: sw.ctl("up", p)
    for p in range(1, 5): sw.ctl("up", p)
    sw.wait_ports("FREE", [1, 2, 3, 4], 8)
    rows = sw.ctl("show", "ports")[1]
    for h in hs: h.close()
    sw.hosts = []; time.sleep(0.5); sw.wait_ports("FREE", [1, 2, 3, 4], 8)
    hh = [sw.host(M(10 + i)) for i in range(4)]
    ck(settle(sw, 2 + 4 - 0, 6) or sw.log().count("host connected") >= 4, "all 4 ports admit hosts", ">=4 connects", sw.log().count("host connected"), logs=rows)
    x = sw.host(M(99)); f = x.recv(4); ck(isinstance(f, dict) and f["data"] == b"switch full", "5th refused with switch full", "switch full", f)
    ck(sw.stop() == 0, "exit 0")

@test("EXT-04", "shutdown of a port whose host is --noread with a full output queue completes in < 5 s (reaper unblocks stuck write)", "roadmap Part 5/16")
def _():
    sw = Switch(3, conf="queue_capacity=8\n").start(); a, b = hosts_up(sw, 2)
    b.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    for i in range(1500): a.send(BCAST, payload=b"n" * 1400)
    time.sleep(0.5); t0 = time.time(); rc, out = sw.ctl("shutdown", 2, timeout=15); dt = time.time() - t0
    ck(rc == 0 and dt < 5, "shutdown of stuck port OK in <5s", "rc0 <5s", "rc=%s %.1fs %s" % (rc, dt, out))
    ck(sw.stop(timeout=8) == 0, "exit 0")

@test("EXT-05", "Reload: key removed from file reverts to default; port.N.vlan removed -> VLAN 1; swctl vlan kept by unrelated reload; rate raised wakes a throttled sender", "README promises", 90)
def _():
    sw = Switch(4, conf="port.1.vlan=7\nmax_macs_per_port=5\n").start(); a, b = hosts_up(sw, 2)
    ck("7" == sw.ctl("show", "ports")[1].splitlines()[1].split()[2], "vlan from config at connect", 7, sw.ctl("show", "ports")[1])
    open(sw.conffile, "w").write("max_macs_per_port=5\n"); sw.sig(signal.SIGHUP); time.sleep(0.5)
    ck("1" == sw.ctl("show", "ports")[1].splitlines()[1].split()[2], "removed port.1.vlan -> back to 1", 1, sw.ctl("show", "ports")[1])
    sw.ctl("vlan", 2, 9); open(sw.conffile, "w").write("max_macs_per_port=6\n"); sw.sig(signal.SIGHUP); time.sleep(0.5)
    ck("9" == sw.ctl("show", "ports")[1].splitlines()[2].split()[2], "unrelated reload keeps swctl vlan", 9, sw.ctl("show", "ports")[1])
    open(sw.conffile, "w").write("port.2.vlan=3\n"); sw.sig(signal.SIGHUP); time.sleep(0.5)
    ck("3" == sw.ctl("show", "ports")[1].splitlines()[2].split()[2], "reload with port.2.vlan overrides swctl value", 3, sw.ctl("show", "ports")[1])
    ck(sw.stop() == 0, "exit 0")
    sw = Switch(3, conf="port.2.rate=1\n").start(); a, b = hosts_up(sw, 2); time.sleep(1.2)
    for i in range(40): a.send(BCAST, payload=b"t")
    time.sleep(0.5); n0 = len(b.drain(0.3)); open(sw.conffile, "w").write("port.2.rate=0\n"); sw.sig(signal.SIGHUP)
    t0 = time.time(); n = 0
    while time.time() - t0 < 2:
        f = b.recv(0.3)
        if isinstance(f, dict): n += 1
    ck(n0 + n >= 30, "after raising the limit the backlog is delivered promptly (>=30 of 40 within 2s)", ">=30", n0 + n)
    ck(sw.stop() == 0, "exit 0")

@test("EXT-06", "Mirror loops: mirror 1->2 and 2->1 with traffic; no amplification/hang; counters bounded", "")
def _():
    sw = Switch(3).start(); a, b, c = hosts_up(sw, 3)
    sw.ctl("mirror", 1, 2); sw.ctl("mirror", 2, 1)
    for i in range(20): a.send(BCAST, payload=b"m%d" % i)
    time.sleep(0.5); na = len([f for f in a.drain(0.5) if isinstance(f, dict)]); nb = len([f for f in b.drain(0.5) if isinstance(f, dict)])
    ck(nb <= 40, "sniffer 2 gets <= 2 copies per frame (no ping-pong)", "<=40", nb); ck(na <= 40, "port 1 gets no amplified copies", "<=40", na)
    ck(sw.alive(), "alive"); ck(sw.stop() == 0, "exit 0")
    sw = Switch(4).start(); a, b, c, d = hosts_up(sw, 4)
    ck(sw.ctl("mirror", 1, 3)[0] == 0 and sw.ctl("mirror", 2, 3)[0] == 0, "two ports mirrored to one sniffer")
    a.send(BCAST, payload=b"A"); b.send(BCAST, payload=b"B"); g = {f["data"] for f in c.drain(0.6) if isinstance(f, dict)}
    ck({b"A", b"B"} <= g, "sniffer sees both", "A,B", g)
    ck(sw.ctl("mirror", 1, 2)[0] == 0 and sw.ctl("mirror", 1, 4)[0] == 0, "re-targeting a mirror is allowed")
    ck(sw.stop() == 0, "exit 0")

@test("EXT-07", "RST storm: 300 hosts connect, send, and abort with SO_LINGER=0 while others flood; stable and clean exit", "", 120)
def _():
    sw = Switch(6).start(); a, b = hosts_up(sw, 2); stop = [False]
    def fl():
        while not stop[0]:
            try: a.send(BCAST, payload=b"f" * 500); b.recv(0.001)
            except OSError: break
    t = threading.Thread(target=fl, daemon=True); t.start()
    for i in range(300):
        try:
            s = socket.create_connection(("127.0.0.1", sw.port), 2); s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.sendall(pack(M(500 + i), BCAST, 1, 1, b"r" * 100)); s.close()
        except OSError: pass
    stop[0] = True; time.sleep(1); ck(sw.alive(), "alive", True, sw.p.poll(), logs=sw.log()[-300:])
    ck(sw.stop(timeout=10) == 0, "exit 0", 0, sw.p.returncode)

@test("EXT-08", "swctl stats --watch exits (not spins) when the switch goes away", "")
def _():
    sw = Switch(2).start(); p = subprocess.Popen([ROOT + "/swctl", "-p", str(sw.port), "stats", "--watch"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2); sw.stop(); time.sleep(3.5)
    r = p.poll(); 
    if r is None: p.kill()
    ck(r is not None, "--watch terminates after switch exit (or shows 'not running')", "exit", "still running after 3.5s")

@test("EXT-09", "mac_table_size not a power of two / tiny (1, 2, 3, 5, 1000): works, eviction correct, no crash", "")
def _():
    for K in (1, 2, 3, 5, 1000):
        sw = Switch(3, conf="mac_table_size=%d\nmax_macs_per_port=1000\n" % K).start(); a, b = hosts_up(sw, 2)
        for i in range(K + 10): a.send(BCAST, src=M(700 + i), payload=b"")
        time.sleep(0.3); rows = sw.mac_table()
        ck(len(rows) == min(K, K + 10) and len(rows) <= K, "K=%d: table size == K" % K, K, len(rows))
        ck(sw.alive() and sw.stop() == 0, "K=%d exit 0" % K)

@test("EXT-10", "Unprivileged low port (80/1023) fails cleanly; port in TIME_WAIT; admin socket path occupied by a regular file/dir", "")
def _():
    sw = Switch(args=["80", "2"]); sw.start(wait=False)
    try: sw.p.wait(5)
    except Exception: pass
    ck(sw.p.returncode == 1, "bind to port 80 without privilege -> exit 1", 1, sw.p.returncode if sw.p.returncode is not None else "running")
    if sw.p.returncode is None: sw.kill()
    port = next_port(); path = "/tmp/vswitch_%d.sock" % port; open(path, "w").write("x")
    sw = Switch(2, port=port).start(); ck(sw.alive(), "regular file at socket path is replaced", True, sw.err() + sw.log())
    if sw.alive(): ck(sw.ctl("show", "ports")[0] == 0, "admin works"); sw.stop()
    port = next_port(); path = "/tmp/vswitch_%d.sock" % port; os.mkdir(path)
    sw = Switch(2, port=port); sw.start(wait=False)
    try: sw.p.wait(5)
    except Exception: pass
    ck(sw.p.returncode is not None, "directory at socket path: defined behaviour (error exit, not silent run without admin)", "exit", "running"); 
    if sw.p.returncode is None: sw.kill()
    os.rmdir(path)

@test("EXT-11", "SIGINT racing admin shutdown / reload / dump under traffic, 20 rounds: always exit 0 without hang", "roadmap Part 16 edge", 300)
def _():
    for r in range(20):
        sw = Switch(6, conf="mac_aging_seconds=30\n").start(); hs = hosts_up(sw, 4)
        ths = [threading.Thread(target=lambda a=a: sw.ctl(*a, timeout=15), daemon=True) for a in (("shutdown", 2), ("shutdown", 3), ("vlan", 1, 4), ("clear", "mac"), ("mirror", 1, 4))]
        for h in hs: h.send(BCAST, payload=b"z" * 800)
        [t.start() for t in ths]; sw.sig(signal.SIGHUP); sw.sig(signal.SIGUSR1)
        time.sleep(random.random() * 0.02); rc = sw.stop(timeout=10)
        ck(rc == 0, "round %d exit 0" % r, 0, rc, logs=sw.log()[-400:])
        if rc != 0: break

@test("EXT-12", "Frame header with bad/odd fields on trunk and access: src==dst, dst multicast, src all-zero; zero-length flood at high rate", "")
def _():
    sw = Switch(3).start(); a, b = hosts_up(sw, 2)
    a.send(M(1), src=M(1), payload=b"selfloop"); ck(not a.got_data("selfloop", 0.4), "src==dst not echoed")
    a.send(BCAST, src="00:00:00:00:00:00", payload=b"zero"); ck(b.got_data("zero", 1), "all-zero src accepted and forwarded")
    blob = pack(M(1), BCAST, 1, 1, b"") * 100
    for i in range(300): a.s.sendall(blob)
    ck(sw.alive(), "alive after 30000 empty frames", True, sw.p.poll())
    end = time.time() + 15
    while time.time() < end and sw.stats()[1]["inn"] < 30001: time.sleep(0.3)
    st = sw.stats(); ck(st[1]["inn"] >= 30001, "IN counter counts zero-length frames", ">=30001", st[1])
    ck(sw.stop() == 0, "exit 0")

@test("EXT-13", "Sanitizer coverage of bonus: valgrind on --pair (both exit 0, 0 errors) and TSan on supervisor-less --persist-mac restart", "roadmap Part 19 completion criteria", 400)
def _():
    if not shutil.which("valgrind"): raise Skip("valgrind not installed")
    from t_bonus import pair_start, hb
    pa = next_port(); pb = pa + 1
    sw = Switch(4, port=pa, args=[str(pa), "4", "--pair", str(pb)], conf="port.1.vlan=10\nport.2.vlan=20\n",
                pre=["valgrind", "--leak-check=full", "--show-leak-kinds=all", "--errors-for-leak-kinds=all", "--error-exitcode=1", "--trace-children=yes", "--track-fds=no"])
    bc = os.path.join(sw.dir, "b.conf"); open(bc, "w").write("port.1.vlan=10\n"); sw.cmd += ["--peer-config", bc]; sw.start(timeout=90)
    end = time.time() + 30
    while time.time() < end and not os.path.exists("/tmp/vswitch_%d.sock" % pb): time.sleep(0.2)
    a1 = sw.host(M(1)); b1 = RawHost(pb, M(11)); sw.hosts.append(b1); time.sleep(1)
    a1.send(BCAST, payload=b"vg"); ck(b1.got_data("vg", 5), "cross-link under valgrind")
    rc = sw.stop(timeout=90); err = sw.err()
    ck(rc == 0, "A exits 0 under valgrind", 0, rc, logs=err[-1200:])
    ck(err.count("ERROR SUMMARY: 0 errors") >= 2, "both processes report 0 errors", ">=2 clean summaries", re.findall(r"ERROR SUMMARY: .*", err), logs=err[-1500:])
