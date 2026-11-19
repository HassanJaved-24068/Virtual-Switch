from vt import *

# ======================= MAC TABLE =======================
@test("MAC-01", "Aging: entry disappears ~T..T+2 s after last seen as source; lookup does not refresh age", "roadmap Part 6")
def _():
    sw = Switch(3, conf="mac_aging_seconds = 3\n").start(); a, b = hosts_up(sw, 2)
    a.send(BCAST); time.sleep(0.2); t0 = time.time()
    ck(any(r[1] == M(1) for r in sw.mac_table()), "learned", "present", sw.mac_table())
    for i in range(3):          # lookups (b -> a) must not refresh
        b.send(M(1)); time.sleep(0.5)
    while time.time() - t0 < 8 and any(r[1] == M(1) for r in sw.mac_table()): time.sleep(0.2)
    dt = time.time() - t0
    ck(2.5 <= dt <= 5.6, "aged out in 3..5.5 s", "2.5-5.6s", "%.1fs" % dt)
    a.send(BCAST); time.sleep(0.2); ck(any(r[1] == M(1) for r in sw.mac_table()), "relearned after aging")
    # source refresh keeps it alive
    for i in range(8): a.send(BCAST); time.sleep(0.6)
    ck(any(r[1] == M(1) for r in sw.mac_table()), "continuous source traffic keeps entry alive", "present", sw.mac_table())
    ck(sw.stop() == 0, "exit 0")

@test("MAC-02", "LRU eviction with K=4 and 6 distinct MACs: oldest evicted, table never exceeds 4, switch keeps forwarding")
def _():
    sw = Switch(8, conf="mac_table_size = 4\nmax_macs_per_port = 100\n").start(); hs = hosts_up(sw, 6)
    for i, h in enumerate(hs): h.send(BCAST, payload=b"l%d" % i); time.sleep(0.15)
    rows = sw.mac_table(); macs = {r[1] for r in rows}
    ck(len(rows) == 4, "table holds exactly K=4", 4, rows)
    ck(M(1) not in macs and M(2) not in macs, "two oldest evicted", "M1,M2 gone", macs)
    ck({M(3), M(4), M(5), M(6)} == macs, "four newest kept", "M3..M6", macs)
    cnt = sum(int(l.split()[4]) for l in sw.ctl("show", "ports")[1].splitlines()[1:])
    ck(cnt == 4, "sum of per-port MAC counts equals table size", 4, sw.ctl("show", "ports")[1])
    for h in hs: h.drain(0.2)
    hs[5].send(M(1), payload=b"to-evicted"); ck(hs[0].got_data("to-evicted") and hs[2].got_data("to-evicted"), "dst evicted -> flood")
    ck(sw.stop() == 0, "exit 0")

@test("MAC-03", "Port security M=3: 4th distinct src MAC from one host shuts the port DISABLED, logged; other port unaffected; up re-enables")
def _():
    sw = Switch(3, conf="max_macs_per_port = 3\n").start(); a, b = hosts_up(sw, 2)
    for i in range(3): a.send(BCAST, src=M(10 + i), payload=b"ok%d" % i)
    time.sleep(0.3); ck(sw.wait_ports("UP", [1]), "3 MACs allowed (<= M)", "UP", sw.ctl("show", "ports")[1])
    a.send(BCAST, src=M(20), payload=b"four")
    ck(sw.wait_ports("DISABLED", [1]), "4th MAC disables port 1", "DISABLED", sw.ctl("show", "ports")[1])
    ck(a.eof(3), "host disconnected", "EOF", "open"); ck("MAC flooding" in sw.log(), "log 'MAC flooding'", "line", sw.log()[-400:])
    ck(not any(r[2] == 1 for r in sw.mac_table()), "its MACs flushed")
    b.send(BCAST); ck(sw.wait_ports("UP", [2]), "port 2 unaffected")
    ck(sw.ctl("up", 1)[0] == 0, "up OK"); a2 = sw.host(M(10)); settle(sw, 3)
    for i in range(3): a2.send(BCAST, src=M(30 + i))
    time.sleep(0.3); ck(sw.wait_ports("UP", [1]), "counter restarted from 0 after up", "UP", sw.ctl("show", "ports")[1])
    ck(sw.stop() == 0, "exit 0")

@test("MAC-04", "Port security boundary: M=1; spoof flood of 1000 MACs from one host; MAC moves between ports do not count against wrong port")
def _():
    sw = Switch(3, conf="max_macs_per_port = 1\n").start(); a, b = hosts_up(sw, 2)
    a.send(BCAST, src=M(1)); a.send(BCAST, src=M(1)); time.sleep(0.2)
    ck(sw.wait_ports("UP", [1]), "same MAC repeatedly is not a violation")
    # move M(1) to port 2: counts must transfer
    b.send(BCAST, src=M(1)); time.sleep(0.2)
    ck(sw.wait_ports("UP", [1, 2]), "MAC move does not trigger security", "UP UP", sw.ctl("show", "ports")[1])
    rows = [l.split() for l in sw.ctl("show", "ports")[1].splitlines()[1:]]
    ck(rows[0][4] == "0" and rows[1][4] == "1", "mac_count adjusted on both ports", "0 and 1", rows)
    try:
        for i in range(1000): a.send(BCAST, src=M(500 + i), payload=b"s")
    except OSError: pass
    ck(sw.wait_ports("DISABLED", [1]), "spoof flood disables the port", "DISABLED", sw.ctl("show", "ports")[1])
    ck(sw.alive(), "alive"); ck(sw.stop() == 0, "exit 0")

@test("MAC-05", "Host moves: same MAC reconnects on another port -> 'MAC moved' log; traffic follows")
def _():
    sw = Switch(4).start(); a, b = hosts_up(sw, 2)
    a.send(BCAST, payload=b"x"); b.drain(0.3)
    a.close(); sw.wait_ports("FREE", [1])
    c = sw.host(M(3)); settle(sw, 3)      # takes port 1
    c.send(BCAST); d = sw.host(M(1)); settle(sw, 4)   # M1 on port 3 now
    d.send(BCAST, payload=b"y"); b.drain(0.3); c.drain(0.3)
    b.send(M(1), payload=b"to-moved"); ck(d.got_data("to-moved"), "unicast reaches host on new port", "delivered", "missing")
    ck(not c.got_data("to-moved", 0.4), "not to the old port's new occupant")
    ck(sw.stop() == 0, "exit 0")

@test("MAC-06", "Hash collisions: K=1024 with 5000 MACs through one port set (M huge): no crash, count <= K, eviction path consistent", 180)
def _():
    sw = Switch(2, conf="max_macs_per_port = 65536\n").start(); a, b = hosts_up(sw, 2)
    for i in range(5000): a.send(BCAST, src=M(1000 + i), payload=b"")
    b.drain(1, maxt=15)
    rows = sw.mac_table(); ck(len(rows) <= 1024, "table size <= K", "<=1024", len(rows))
    cnt = sum(int(l.split()[4]) for l in sw.ctl("show", "ports")[1].splitlines()[1:])
    ck(cnt == len(rows), "port MAC counts consistent with table", len(rows), cnt)
    ck(len({r[1] for r in rows}) == len(rows), "no duplicate entries", "unique", len(rows))
    ck(sw.alive() and sw.stop() == 0, "exit 0")

@test("MAC-07", "MAC table is per VLAN: same MAC on two VLANs coexists")
def _():
    sw = Switch(4, conf="port.1.vlan=10\nport.2.vlan=20\n").start(); a, b = hosts_up(sw, 2)
    a.send(BCAST, src=M(9)); b.send(BCAST, src=M(9)); time.sleep(0.3)
    rows = [r for r in sw.mac_table() if r[1] == M(9)]
    ck(len(rows) == 2 and {r[0] for r in rows} == {10, 20}, "(10,M9) and (20,M9) both present", "2 rows", rows)
    ck(sw.stop() == 0, "exit 0")

# ======================= QOS =======================
@test("QOS-01", "WRR 5:3:1 ordering with all queues busy (aging disabled via prio_aging_ms=60000, rate-limited consumer)", "roadmap Part 11", 90)
def _():
    sw = Switch(3, conf="port.2.rate = 10\nprio_aging_ms = 60000\nqueue_capacity = 64\n").start(); a, b = hosts_up(sw, 2)
    time.sleep(1.2)                                   # bucket full (10)
    b.send(M(1)); a.drain(0.2)
    for i in range(12): a.send(M(2), prio=1, payload=b"burn")   # use the burst
    time.sleep(0.15)
    for i in range(18):
        a.send(M(2), prio=2, payload=b"L"); a.send(M(2), prio=1, payload=b"N"); a.send(M(2), prio=0, payload=b"H")
    seq = []
    end = time.time() + 12
    while len(seq) < 66 and time.time() < end:
        f = b.recv(1)
        if isinstance(f, dict): seq.append(f["data"].decode())
    seq = [s for s in seq if s != "burn"]
    s = "".join(seq)
    ck(len(s) >= 18, "received enough frames", ">=18", s)
    # expect H,N,L ordering: first 9 after the leftover should follow HHHHHNNNL pattern (allow phase offset): count per window of 9
    win = s[:18]
    ck(win.count("H") >= 9 and win.count("L") <= 4, "high served before low (ratio 5:3:1 over 18)", "H=10 N=6 L=2", win)
    h, n, l = (s[:27].count(x) for x in "HNL")
    ck(abs(h - 15) <= 3 and abs(n - 9) <= 3 and abs(l - 3) <= 2, "ratio ~5:3:1 over first 27", "15/9/3", (h, n, l, s))
    ck(sw.stop() == 0, "exit 0")

@test("QOS-02", "Starvation avoidance: low frame delivered within ~prio_aging_ms (200 ms) + write while high traffic is continuous", "roadmap Part 11", 90)
def _():
    sw = Switch(3, conf="port.2.rate = 20\nprio_aging_ms = 200\nqueue_capacity = 256\n").start(); a, b = hosts_up(sw, 2)
    b.send(M(1)); a.drain(0.2); time.sleep(1.2)
    for i in range(60): a.send(M(2), prio=0, payload=b"H")
    t0 = time.time(); a.send(M(2), prio=2, payload=b"LOW")
    for i in range(60): a.send(M(2), prio=0, payload=b"H")
    tl = None; end = time.time() + 8
    while time.time() < end:
        f = b.recv(1)
        if isinstance(f, dict) and f["data"] == b"LOW": tl = time.time() - t0; break
    ck(tl is not None, "low-priority frame is eventually delivered", "delivered", "never")
    # with 120 high frames at 20/s = 6 s of backlog; strict priority would take >=3 s (60+) ; aging must deliver within ~0.2 + tick
    ck(tl is not None and tl < 1.5, "delivered within ~200 ms aging + one token tick (<1.5s)", "<1.5s", tl)
    ck(sw.stop() == 0, "exit 0")

@test("QOS-03", "Full low ring drops only low frames; high/normal still accepted (queue_capacity=4)")
def _():
    sw = Switch(3, conf="port.2.rate = 1\nqueue_capacity = 4\nprio_aging_ms = 60000\n").start(); a, b = hosts_up(sw, 2)
    b.send(M(1)); a.drain(0.2); time.sleep(1.2)
    for i in range(30): a.send(M(2), prio=2, payload=b"L%d" % i)
    time.sleep(0.4); d0 = sw.stats()[2]["drop"]
    ck(d0 >= 20, "excess low frames dropped and counted", ">=20", d0)
    for i in range(4): a.send(M(2), prio=0, payload=b"H%d" % i)
    time.sleep(0.4); d1 = sw.stats()[2]["drop"]
    ck(d1 == d0, "4 high frames all accepted despite full low ring", d0, d1)
    ck(sw.stop() == 0, "exit 0")

# ======================= RATE =======================
@test("RATE-01", "port.2.rate=50: ~50 frames/s to port 2 over 4 s (<= 50*4 + 50 burst + slack); other port unaffected; CPU ~0 while throttled", "roadmap Part 12", 90)
def _():
    sw = Switch(4, conf="port.2.rate = 50\nqueue_capacity = 64\n").start(); a, b, c = hosts_up(sw, 3)
    time.sleep(1.3)
    cnt = [0]; ccnt = [0]; stop = [False]
    def rd(h, box):
        while not stop[0]:
            f = h.recv(0.2)
            if isinstance(f, dict): box[0] += 1
    t1 = threading.Thread(target=rd, args=(b, cnt)); t2 = threading.Thread(target=rd, args=(c, ccnt)); t1.start(); t2.start()
    cpu0 = sw.cpu_ticks(); t0 = time.time()
    blob = pack(M(1), BCAST, 1, 1, b"r")
    while time.time() - t0 < 4: a.s.sendall(blob * 4); time.sleep(0.004)
    stop[0] = True; t1.join(); t2.join()
    ck(170 <= cnt[0] <= 270, "port 2 got ~250 (50/s*4s + 50 burst)", "170..270", cnt[0])
    ck(ccnt[0] > cnt[0] * 2, "unlimited port 3 not throttled", ">2x", (ccnt[0], cnt[0]))
    # throttled idle CPU
    d = Switch(3, conf="port.2.rate = 5\n").start(); x, y = hosts_up(d, 2); x.send(M(2)); 
    for i in range(50): x.send(BCAST)
    time.sleep(0.5); c0 = d.cpu_ticks(); time.sleep(3); c1 = d.cpu_ticks()
    ck(c1 - c0 <= 30, "<=10% CPU while throttled and idle (no spinning)", "<=30 ticks/3s", c1 - c0)
    ck(sw.stop() == 0 and d.stop() == 0, "exit 0")

@test("RATE-02", "Rate change via SIGHUP applies at once; rate_limit global; rate 0 = unlimited again", "", 90)
def _():
    sw = Switch(3, conf="rate_limit = 20\n").start(); a, b = hosts_up(sw, 2)
    def measure(secs=2.0):
        n = 0; t0 = time.time(); blob = pack(M(1), BCAST, 1, 1, b"r")
        a.s.sendall(blob * 50)   # prefill
        while time.time() - t0 < secs:
            a.s.sendall(blob * 5); 
            while True:
                f = b.recv(0.005)
                if not isinstance(f, dict): break
                n += 1
            time.sleep(0.005)
        return n
    time.sleep(1.3); n1 = measure()
    ck(n1 <= 20 * 2 + 20 + 10, "rate_limit=20 limits to ~60 in 2s incl burst", "<=70", n1)
    open(sw.conffile, "w").write("rate_limit = 0\n"); sw.sig(signal.SIGHUP); time.sleep(0.5); b.drain(1, maxt=10)
    n2 = measure(1.0); ck(n2 > 150, "after reload rate_limit=0 -> unlimited", ">150", n2)
    ck(sw.stop() == 0, "exit 0")

# ======================= SHM / STATS =======================
@test("SHM-01", "Counters: IN/OUT/DROPPED match exact host counts; swctl stats reads; shm mode 0600; stale shm replaced at start; gone after exit")
def _():
    open("/dev/shm/vswitch_stats_P", "wb").write(b"garbage" * 100)       # stale segment
    sw = Switch(3).start(); a, b = hosts_up(sw, 2)
    ck(sw.alive(), "switch starts over stale/garbage segment", True, sw.err())
    st = os.stat("/dev/shm/vswitch_stats_P"); ck(oct(st.st_mode & 0o777) == "0o600", "shm mode 0600", "0o600", oct(st.st_mode & 0o777))
    for i in range(100): a.send(BCAST)
    n = len(b.drain(0.5)); time.sleep(0.3)
    s = sw.stats(); ck(n == 100 and s[1]["inn"] == 100 and s[2]["out"] == 100, "IN(1)=100, OUT(2)=100", "100/100", s)
    ck(s[1]["state"] == "UP" and s[3]["state"] == "FREE", "states in stats", "UP/FREE", s)
    rc, out = sw.ctl("stats"); ck(rc == 0 and "MIRRORED" in out, "stats header", "MIRRORED", out)
    ck(sw.stop() == 0, "exit 0"); ck(not os.path.exists("/dev/shm/vswitch_stats_P"), "shm removed")

@test("SHM-02", "swctl stats --watch refreshes every second, clears screen, exits on SIGINT (exit 0)")
def _():
    sw = Switch(2).start(); a = hosts_up(sw, 1)[0]
    p = subprocess.Popen([os.path.join(ROOT, "swctl"), "-p", str(sw.port), "stats", "--watch"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(3.3); p.send_signal(signal.SIGINT)
    try: out, _e = p.communicate(timeout=3)
    except subprocess.TimeoutExpired: p.kill(); out = b""; ck(False, "--watch exits on SIGINT", "exit", "hang")
    n = out.count(b"\x1b[H\x1b[2J"); ck(3 <= n <= 5, "3-5 screen refreshes in 3.3 s", "3..5", n)
    ck(p.returncode in (0, -2, 130), "exit status on SIGINT", "0", p.returncode)
    ck(sw.stop() == 0, "exit 0")

@test("SHM-03", "Reader killed (-9) while holding stats mutex, and a hung swctl reader do not wedge the switch")
def _():
    sw = Switch(3).start(); a, b = hosts_up(sw, 2)
    ps = [subprocess.Popen([os.path.join(ROOT, "swctl"), "-p", str(sw.port), "stats", "--watch"], stdout=subprocess.DEVNULL) for _ in range(5)]
    for i in range(50):
        a.send(BCAST); 
        if i == 25: [p.kill() for p in ps]
    b.drain(0.5); a.send(BCAST, payload=b"post"); ck(b.got_data("post"), "forwarding continues after killing stats readers")
    ck(sw.ctl("stats")[0] == 0, "stats still readable")
    for p in ps: p.wait()
    ck(sw.stop() == 0, "exit 0")

# ======================= HOST PROGRAM =======================
def host_run(sw, mac, args=(), stdin="", timeout=6, port=None):
    p = subprocess.run([os.path.join(ROOT, "host"), "127.0.0.1", str(port or sw.port), mac] + list(args), input=stdin, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr

@test("HOST-01", "host program: bad arguments/MACs exit 1; send/sendx/quit; stdin EOF; invalid commands do not kill it")
def _():
    sw = Switch(4).start()
    for args in ([], ["127.0.0.1"], ["127.0.0.1", "1"], ["127.0.0.1", str(sw.port), "zz"], ["127.0.0.1", str(sw.port), "02:00:00:00:00"], ["127.0.0.1", str(sw.port), "02:00:00:00:00:0g"],
                 ["127.0.0.1", str(sw.port), "02-00-00-00-00-01"], ["127.0.0.1", "0", M(1)], ["127.0.0.1", "99999", M(1)], ["not-an-ip", str(sw.port), M(1)], ["127.0.0.1", "abc", M(1)],
                 ["127.0.0.1", str(sw.port), M(1), "--bogus"], ["127.0.0.1", str(sw.port), M(1), "--flood", "5"], ["127.0.0.1", str(sw.port), M(1), "--flood", "x", "--to", M(2)],
                 ["127.0.0.1", str(sw.port), M(1), "--to", M(2)], ["127.0.0.1", str(sw.port), M(1), "--flood", "-1", "--to", M(2)], ["127.0.0.1", str(sw.port), M(1), "--sniff", "--noread"]):
        try: p = subprocess.run([os.path.join(ROOT, "host")] + args, input="", capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired: ck(False, "host %r hangs" % args, "exit 1", "timeout"); continue
        ck(p.returncode == 1, "host %r -> exit 1" % (args[2:] or args), 1, "%s %s" % (p.returncode, p.stderr[:80]))
    # connection refused
    p = subprocess.run([os.path.join(ROOT, "host"), "127.0.0.1", "29998", M(1)], input="", capture_output=True, text=True, timeout=5); ck(p.returncode == 1, "connection refused -> exit 1", 1, p.returncode)
    r = sw.host(M(2)); settle(sw, 1)
    stdin = "send bcast hello world\nsendx bcast 5 0 prio0\nsend zz bad\nbogus\nsend\nsendx bcast 0 1 x\nsendx bcast 4095 1 x\nsendx bcast 5 3 x\nsendx\n\nsend bcast " + "A" * 1501 + "\nsend bcast after\nquit\nsend bcast notsent\n"
    rc, out, err = host_run(sw, M(1), stdin=stdin)
    ck(rc == 0, "host exits 0 after quit", 0, (rc, err))
    got = [f for f in r.drain(0.6) if isinstance(f, dict)]
    d = [f["data"] for f in got]
    ck(b"hello world" in d, "send with spaces in text", "hello world", d); ck(b"prio0" in d, "sendx works")
    f = [x for x in got if x["data"] == b"prio0"]; ck(f and f[0]["prio"] == 0, "sendx prio 0", 0, f and f[0]["prio"])
    ck(b"after" in d, "host continues after invalid commands", "after", d)
    ck(b"notsent" not in d, "nothing sent after quit")
    ck(b"x" not in d, "invalid vlan/prio sendx not sent", "none", d)
    ck(all(len(x["data"]) <= 1500 for x in got), "payload > 1500 never put on wire", "<=1500", [len(x["data"]) for x in got])
    ck(sw.stop() == 0, "exit 0")

@test("HOST-02", "host options: --quiet counts RX_TOTAL; --flood N --to; --die-midframe; --partial; --noread; --sniff; 'switch full' exit 2")
def _():
    sw = Switch(4, conf="port_wait_timeout = 1\n").start(); r = sw.host(M(2)); settle(sw, 1)
    rc, out, err = host_run(sw, M(1), ["--flood", "50", "--to", M(2)], stdin="")
    ck(rc == 0, "--flood exits 0", 0, (rc, err)); fr = r.drain(0.6); ck(len(fr) == 50, "50 flood frames arrive", 50, len(fr))
    ck(all(f["data"].startswith(b"flood ") for f in fr), "flood payload format 'flood <i>'", "flood i", fr[:1] and fr[0]["data"])
    rc, out, err = host_run(sw, M(3), ["--die-midframe"]); ck(sw.wait_log("port is FREE", 2), "die-midframe -> port freed")
    ck(sw.alive(), "alive")
    rc, out, err = host_run(sw, M(3), ["--partial"], stdin="send bcast slowly\n"); ck(rc == 0, "--partial exits 0"); ck(r.got_data("slowly", 3), "--partial frame delivered")
    # quiet
    q = subprocess.Popen([os.path.join(ROOT, "host"), "127.0.0.1", str(sw.port), M(4), "--quiet"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True); time.sleep(0.6)
    for i in range(25): r.send(BCAST, payload=b"q")
    time.sleep(0.6); out = q.communicate("quit\n", timeout=5)[0]
    ck("RX_TOTAL 25" in out, "--quiet prints RX_TOTAL 25", "RX_TOTAL 25", out)
    # switch full
    ps = [sw.host(M(10)), sw.host(M(11)), sw.host(M(12))]; sw.wait_ports("UP", [1, 2, 3, 4]); time.sleep(0.3)
    hp = subprocess.Popen([os.path.join(ROOT, "host"), "127.0.0.1", str(sw.port), M(5)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try: hp.wait(8)
    except subprocess.TimeoutExpired: hp.kill()
    out, err = hp.stdout.read(), hp.stderr.read(); rc = hp.returncode
    ck(rc == 2 and "SWITCH FULL" in out, "host exit code 2 + 'SWITCH FULL'", "rc 2", (rc, out, err))
    ck(sw.stop() == 0, "exit 0")
    sw = Switch(3).start(); a = sw.host(M(1)); settle(sw, 1)
    s = subprocess.Popen([os.path.join(ROOT, "host"), "127.0.0.1", str(sw.port), M(2), "--sniff"], stdout=subprocess.PIPE, text=True); settle(sw, 2)
    a.send(BCAST, payload=b"sn"); time.sleep(0.4)
    sw.stop(); out = s.communicate(timeout=5)[0]
    ck(re.search(r"RX src=%s dst=ff:ff:ff:ff:ff:ff vlan=1 prio=1 len=2 data=sn" % M(1), out) is not None, "RX line format per roadmap (src dst vlan prio len data)", "RX src=.. dst=.. vlan=.. prio=.. len=.. data=..", out)
    ck(s.returncode == 0, "--sniff ends when switch closes", 0, s.returncode)
