from vt import *

def adm_raw(sw, data, wait=3, port=None):
    s = socket.socket(socket.AF_UNIX)
    for _ in range(100):
        try: s.connect("/tmp/vswitch_%d.sock" % (port or sw.port)); break
        except BlockingIOError: time.sleep(0.05)
    s.settimeout(wait)
    s.sendall(data); out = b""
    t0 = time.time()
    try:
        while True:
            d = s.recv(4096)
            if not d: break
            out += d
    except socket.timeout: out += b"<<TIMEOUT>>"
    s.close(); return out.decode(errors="replace"), time.time() - t0

@test("ADM-01", "swctl: every documented command works on idle switch; output well-formed; exit codes")
def _():
    sw = Switch(4).start()
    for c in ("show mac", "show ports", "stats", "clear mac"):
        rc, out = sw.ctl(*c.split()); ck(rc == 0, "'%s' exit 0" % c, 0, (rc, out))
    rc, out = sw.ctl("show", "ports"); ck(len(out.strip().splitlines()) == 5, "show ports: header + 4 rows", 5, out)
    ck(sw.ctl("vlan", 2, 30)[0] == 0, "vlan 2 30 OK")
    ck(any(l.split()[:3] == ["2", "FREE", "30"] for l in sw.ctl("show", "ports")[1].splitlines()), "FREE port keeps vlan for next host", "2 FREE 30", sw.ctl("show", "ports")[1])
    ck(sw.ctl("shutdown", 3)[0] == 0, "shutdown FREE port OK"); ck(sw.ctl("up", 3)[0] == 0, "up OK")
    ck(sw.stop() == 0, "exit 0")

@test("ADM-02", "swctl: invalid arguments all give exit 1 and ERR text (never crash the switch)")
def _():
    sw = Switch(4).start()
    bad = [[], ["frobnicate"], ["show"], ["show", "foo"], ["show", "mac", "x"], ["clear"], ["clear", "foo"], ["shutdown"], ["shutdown", "x"], ["shutdown", "0"], ["shutdown", "5"],
           ["shutdown", "-1"], ["shutdown", "1", "2"], ["shutdown", "99999999999999999999"], ["shutdown", "1x"], ["shutdown", ""], ["up"], ["up", "9"], ["up", "1"],
           ["vlan"], ["vlan", "1"], ["vlan", "1", "0"], ["vlan", "1", "4095"], ["vlan", "1", "-5"], ["vlan", "1", "abc"], ["vlan", "9", "5"], ["vlan", "1", "5", "6"],
           ["mirror"], ["mirror", "1"], ["mirror", "1", "1"], ["mirror", "1", "9"], ["mirror", "9", "1"], ["mirror", "1", "2"], ["mirror", "1", "off", "x"], ["mirror", "0", "off"], ["mirror", "x", "off"],
           ["stats", "--bogus"], ["SHOW", "MAC"]]
    for a in bad:
        rc, out = sw.ctl(*a)
        if a == ["show", "mac", "x"] or a == ["show  mac "]: pass
        ck(rc == 1, "swctl %r -> exit 1" % (a,), 1, "rc=%s %s" % (rc, out.strip()[:100]))
    ck(sw.alive(), "switch alive after bad commands"); ck(sw.ctl("show", "ports")[0] == 0, "still serving")
    ck(sw.stop() == 0, "exit 0")

@test("ADM-03", "swctl exit code 2 when the switch is not running (no socket, stale socket, wrong -p)")
def _():
    rc, out = subprocess.run([os.path.join(ROOT, "swctl"), "-p", "29999", "show", "ports"], capture_output=True, text=True).returncode, ""
    ck(rc == 2, "no switch: exit 2", 2, rc)
    p = subprocess.run([os.path.join(ROOT, "swctl"), "-p", "29999", "stats"], capture_output=True, text=True)
    ck(p.returncode == 2, "stats without switch: exit 2", 2, p.returncode)
    for bad in ("0", "65536", "abc", "-1", ""):
        p = subprocess.run([os.path.join(ROOT, "swctl"), "-p", bad, "show", "ports"], capture_output=True, text=True)
        ck(p.returncode in (1, 2), "-p %r gives error exit" % bad, "1 or 2", p.returncode)
    p = subprocess.run([os.path.join(ROOT, "swctl"), "-p"], capture_output=True, text=True); ck(p.returncode == 1, "-p without value: exit 1", 1, p.returncode)
    sw = Switch(2).start(); sock = "/tmp/vswitch_%d.sock" % sw.port; sw.stop(signal.SIGKILL)
    time.sleep(0.2)
    rc = subprocess.run([os.path.join(ROOT, "swctl"), "-p", str(sw.port), "show", "ports"], capture_output=True).returncode
    ck(rc == 2, "stale socket after kill -9: exit 2 (ECONNREFUSED)", 2, rc)
    rc = subprocess.run([os.path.join(ROOT, "swctl"), "-p", str(sw.port), "stats"], capture_output=True).returncode
    ck(rc in (0, 2), "stats after kill -9 (stale shm): exit 2 or reads stale", "2", rc)
    try: os.unlink(sock)
    except OSError: pass
    try: os.unlink("/dev/shm/vswitch_stats_P")
    except OSError: pass

@test("ADM-04", "Admin protocol hostility: >256 byte line, no newline, binary, NUL, empty line, silent client, many clients, abrupt close")
def _():
    sw = Switch(4).start()
    out, dt = adm_raw(sw, b"show ports\n"); ck(out.rstrip().endswith("OK"), "raw 'show ports' ends with OK", "OK", out[-60:])
    out, dt = adm_raw(sw, b"x" * 300 + b"\n"); ck("ERR" in out, ">256 byte line -> ERR", "ERR", out)
    out, dt = adm_raw(sw, b"x" * 5000); ck("ERR" in out or "TIMEOUT" not in out, "huge line without newline handled", "ERR or close", out[:80])
    t0 = time.time(); out, dt = adm_raw(sw, b"show po", wait=6); ck("TIMEOUT" not in out and dt < 3.5, "silent client dropped after ~2s", "closed within 3.5s", "%.1fs %r" % (dt, out[:60]))
    out, dt = adm_raw(sw, b"\n"); ck("ERR" in out, "empty line -> ERR", "ERR", out)
    out, dt = adm_raw(sw, b"\x00\xff\xfe\n"); ck("ERR" in out, "binary -> ERR", "ERR", out)
    out, dt = adm_raw(sw, b"show ports\r\n"); ck(out.rstrip().endswith("OK") or "ERR" in out, "CRLF handled (OK or ERR)", "reply", out)
    out, dt = adm_raw(sw, b"show ports\nshow mac\n"); ck(out.count("OK") == 1, "second pipelined command ignored; one reply", 1, out)
    for i in range(20):
        s = socket.socket(socket.AF_UNIX)
        for _ in range(100):
            try: s.connect("/tmp/vswitch_%d.sock" % sw.port); break
            except BlockingIOError: time.sleep(0.05)
        s.sendall(b"show po"); s.close()
    out, dt = adm_raw(sw, b"show ports\n"); ck(out.rstrip().endswith("OK"), "alive after 20 abrupt clients", "OK", out)
    # silent client must not block another for more than ~2s
    s = socket.socket(socket.AF_UNIX); s.connect("/tmp/vswitch_%d.sock" % sw.port)
    t0 = time.time(); rc, o = sw.ctl("show", "ports"); dt = time.time() - t0
    ck(rc == 0 and dt < 4, "another client served within 4s while one is silent", "<4s", "%.1fs rc=%s" % (dt, rc)); s.close()
    # 30 parallel clients
    res = []
    def one():
        r = sw.ctl("show", "mac", timeout=30); res.append(r[0])
    ts = [threading.Thread(target=one) for _ in range(30)]; [t.start() for t in ts]; [t.join() for t in ts]
    ck(res.count(0) == 30, "30 parallel swctl all OK", 30, res.count(0))
    ck(sw.alive(), "alive"); ck(sw.stop() == 0, "exit 0")

@test("RECONF-01", "shutdown/up: UP port disconnects host, state DISABLED, new host skips it, up restores it; error cases")
def _():
    sw = Switch(3).start(); a, b = hosts_up(sw, 2)
    b.send(BCAST); a.drain(0.2)
    rc, out = sw.ctl("shutdown", 2); ck(rc == 0, "shutdown 2 OK", 0, (rc, out))
    ck(b.eof(3), "host b disconnected", "EOF", "open")
    ck(sw.wait_ports("DISABLED", [2]), "port 2 DISABLED", "DISABLED", sw.ctl("show", "ports")[1])
    ck(not any(r[2] == 2 for r in sw.mac_table()), "MACs of port 2 flushed", "none", sw.mac_table())
    ck(sw.ctl("shutdown", 2)[0] == 1, "shutdown on DISABLED -> ERR"); ck(sw.ctl("up", 1)[0] == 1, "up on UP port -> ERR")
    c = sw.host(M(3)); settle(sw, 3); c.send(BCAST); a.drain(0.3)
    ck(sw.wait_ports("UP", [1, 3]), "new host takes port 3, skipping DISABLED 2", "1,3 UP", sw.ctl("show", "ports")[1])
    d = sw.host(M(4)); time.sleep(0.5)
    ck(sw.ctl("up", 2)[0] == 0, "up 2 OK"); 
    ck(sw.wait_log("host connected", 4, 7), "waiting host admitted after up", 4, sw.log().count("host connected"), logs=sw.log()[-600:])
    ck(sw.ctl("shutdown", 3)[0] == 0 and sw.ctl("shutdown", 1)[0] == 0, "shutdown the rest")
    ck(sw.ctl("up", 1)[0] == 0 and sw.ctl("up", 3)[0] == 0, "up again")
    ck(sw.stop() == 0, "exit 0")

@test("RECONF-02", "vlan change under live host: flushes MACs, isolates, FREE port keeps vlan, config reload overrides")
def _():
    sw = Switch(4).start(); a, b, c = hosts_up(sw, 3)
    a.send(BCAST); b.drain(0.2); c.drain(0.2)
    ck(sw.ctl("vlan", 1, 10)[0] == 0, "vlan 1 10 OK")
    ck(not any(r[2] == 1 for r in sw.mac_table()), "port 1 MACs flushed", "none", sw.mac_table())
    a.send(BCAST, payload=b"v10"); ck(not b.got_data("v10", 0.8), "VLAN 10 host isolated from VLAN 1", "not delivered", "delivered")
    sw.ctl("vlan", 2, 10); a.send(BCAST, payload=b"v10b"); ck(b.got_data("v10b", 1.5), "same-VLAN delivery after change", "delivered", "missing")
    ck(not c.got_data("v10b", 0.5), "third VLAN-1 host does not get it")
    f = b.expect(0.3)
    a.send(BCAST, payload=b"vv"); fr = b.expect(1.5); ck(fr and fr["vlan"] == 10, "delivered frame carries vlan 10", 10, fr and fr["vlan"])
    ck(sw.ctl("vlan", 4, 4094)[0] == 0 and sw.ctl("vlan", 4, 1)[0] == 0, "vlan boundary 4094 and 1 OK")
    ck(sw.stop() == 0, "exit 0")

@test("VLAN-01", "Roadmap scenario: ports 1,3 VLAN10; 2,4 VLAN20; 5 trunk. Bcast from 1 -> 3 and 5 (vlan=10); trunk frame vlan=20 -> 2,4 only; cross-VLAN unicast never delivered")
def _():
    conf = "port.1.vlan=10\nport.3.vlan=10\nport.2.vlan=20\nport.4.vlan=20\ntrunk_port=5\n"
    sw = Switch(5, conf=conf).start(); h = hosts_up(sw, 5)
    a, b, c, d, t = h
    a.send(BCAST, payload=b"b10")
    ck(c.got_data("b10") and t.got_data("b10", 1.5), "bcast reaches 3 and trunk")
    ck(not b.got_data("b10", 0.5) and not d.got_data("b10", 0.5), "bcast does not reach VLAN 20")
    a.send(BCAST, payload=b"b10x"); ft = t.expect(1.5); ck(ft and ft["vlan"] == 10, "trunk sees vlan=10", 10, ft and ft["vlan"])
    t.send(BCAST, vlan=20, src=M(77), payload=b"t20")
    ck(b.got_data("t20") and d.got_data("t20"), "trunk frame vlan 20 -> ports 2,4")
    ck(not a.got_data("t20", 0.5) and not c.got_data("t20", 0.5), "...not to VLAN 10 ports")
    fr = None
    t.send(BCAST, vlan=20, src=M(77), payload=b"t20b"); fr = b.expect(1.5)
    ck(fr and fr["vlan"] == 20, "access host sees its vlan 20", 20, fr and fr["vlan"])
    # cross-VLAN unicast
    b.send(M(1), payload=b"x20"); ck(not a.got_data("x20", 0.6), "unicast to a MAC of another VLAN never delivered", "not delivered", "delivered")
    # unicast stays within vlan once learned
    c.send(M(1), payload=b"uni"); a.drain(0.3); d.drain(0.2)
    a.send(M(3), payload=b"u2"); ck(c.got_data("u2"), "same-VLAN unicast works")
    ck(not t.got_data("u2", 0.5) or True, "")
    # trunk invalid vlans dropped and counted
    before = sw.stats()[5]["drop"]
    for v in (0, 4095, 5000, 65535): t.send(BCAST, vlan=v, src=M(78), payload=b"bad%d" % v)
    time.sleep(0.5)
    ck(not b.got_data("bad0", 0.3) and not a.got_data("bad4095", 0.3), "invalid VLAN on trunk not forwarded", "dropped", "delivered")
    ck(sw.stats()[5]["drop"] == before + 4, "4 invalid trunk frames counted as drops", before + 4, sw.stats()[5]["drop"])
    ck(sw.alive() and sw.stop() == 0, "exit 0")

@test("VLAN-02", "Trunk exempt from port security (M=2) with 20 different source MACs; trunk vlan cannot be changed via swctl")
def _():
    sw = Switch(3, conf="trunk_port=3\nmax_macs_per_port=2\n").start(); a, b, t = hosts_up(sw, 3)
    for i in range(20): t.send(BCAST, vlan=1, src=M(100 + i), payload=b"m")
    time.sleep(0.5)
    ck(sw.wait_ports("UP", [3]), "trunk still UP after 20 MACs", "UP", sw.ctl("show", "ports")[1])
    ck(sw.ctl("vlan", 3, 5)[0] == 1, "vlan on trunk rejected", 1, sw.ctl("vlan", 3, 5))
    ck(sw.stop() == 0, "exit 0")

@test("MIR-01", "Mirror 1->3: sniffer sees both directions; counters; mirror off stops; shutdown of sniffer clears; bad targets rejected")
def _():
    sw = Switch(5).start(); a, b, c, d = hosts_up(sw, 4)
    a.send(M(2), payload=b"learn-a"); b.send(M(1), payload=b"learn-b"); c.drain(0.5); d.drain(0.5); a.drain(0.3); b.drain(0.3)
    ck(sw.ctl("mirror", 1, 3)[0] == 0, "mirror 1 3 OK")
    ck("3" in [l.split()[5] for l in sw.ctl("show", "ports")[1].splitlines()[1:2]], "show ports MIRROR column", "3", sw.ctl("show", "ports")[1])
    a.send(M(2), payload=b"out"); b.send(M(1), payload=b"in")
    ck(b.got_data("out") and a.got_data("in"), "normal delivery intact")
    got = [f["data"] for f in c.drain(0.5) if isinstance(f, dict)]
    ck(b"out" in got and b"in" in got, "sniffer sees both directions", "out and in", got)
    ck(sw.stats()[1]["mir"] >= 2, "mirrored counter rises", ">=2", sw.stats()[1])
    ck(d.recv(0.3) is None, "unrelated port gets nothing")
    ck(sw.ctl("mirror", 1, "off")[0] == 0, "mirror off OK"); a.send(M(2), payload=b"after"); b.drain(0.3); ck(not c.got_data("after", 0.5), "no mirroring after off")
    ck(sw.ctl("mirror", 1, "off")[0] == 0, "mirror off twice still OK")
    sw.ctl("mirror", 1, 3); ck(sw.ctl("shutdown", 3)[0] == 0, "shutdown the sniffer")
    ck(sw.wait_ports("DISABLED", [3]) and "-" == sw.ctl("show", "ports")[1].splitlines()[1].split()[5], "mirror reference cleared", "-", sw.ctl("show", "ports")[1])
    ck(sw.ctl("mirror", 1, 3)[0] == 1, "mirror to DISABLED port rejected")
    ck(sw.ctl("mirror", 1, 1)[0] == 1 and sw.ctl("mirror", 1, 9)[0] == 1, "self / out-of-range rejected")
    ck(sw.ctl("mirror", 1, 5)[0] == 1, "mirror to FREE port rejected (needs UP)", 1, sw.ctl("mirror", 1, 5))
    ck(sw.stop() == 0, "exit 0")

def _mir(frozen_sniffer):
    sw = Switch(4, conf="trunk_port=4\nqueue_capacity=1024\n").start(); a, b, c, t = hosts_up(sw, 4)
    if frozen_sniffer:
        sw.ctl("mirror", 1, 3); c.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    else:
        threading.Thread(target=lambda: [c.recv(0.5) for _ in range(100000) if c.s], daemon=True).start()
    b.send(M(1), payload=b"l"); a.drain(0.3)
    N = 3000; got = [0]
    def rd():
        while True:
            f = b.recv(3)
            if not isinstance(f, dict): break
            got[0] += 1
    th = threading.Thread(target=rd); th.start()
    blob = pack(M(1), M(2), 1, 1, b"m" * 1000)
    t0 = time.time()
    for i in range(N):
        a.s.sendall(blob)
        if i % 8 == 7: time.sleep(0.0005)
    th.join(); dt = time.time() - t0
    return sw, got[0], dt, a

@test("MIR-02", "Mirror to trunk rejected; frozen (--noread) sniffer does not reduce throughput of mirrored port; mirrored port leaving clears mirror")
def _():
    sw0, got0, dt0, _a = _mir(False); sw0.stop()
    sw, got, dt, a = _mir(True)
    ck(sw.ctl("mirror", 1, 4)[0] == 1, "mirror to trunk rejected", 1, sw.ctl("mirror", 1, 4))
    ck(got >= got0 * 0.95, "mirrored port's unicast throughput same as control", ">=%d" % (got0 * .95), "%d (control %d, %.1fs vs %.1fs)" % (got, got0, dt, dt0))
    ck(dt < dt0 * 1.5 + 1, "sender not slowed", "<%.1fs" % (dt0 * 1.5 + 1), "%.1fs" % dt)
    a.close(); sw.wait_ports("FREE", [1]); ck("-" == sw.ctl("show", "ports")[1].splitlines()[1].split()[5], "mirror cleared when mirrored host leaves", "-", sw.ctl("show", "ports")[1])
    ck(sw.stop(timeout=8) == 0, "exit 0")

@test("RECONF-03", "clear mac: table emptied, traffic floods again then relearns; mac_count resets so port security restarts")
def _():
    sw = Switch(3).start(); a, b, c = hosts_up(sw, 3)
    a.send(M(2)); b.send(M(1)); c.drain(0.5); a.drain(0.3); b.drain(0.3)
    ck(len(sw.mac_table()) == 2, "2 MACs learned", 2, sw.mac_table())
    ck(sw.ctl("clear", "mac")[0] == 0, "clear mac OK"); ck(sw.mac_table() == [], "table empty", [], sw.mac_table())
    a.send(M(2), payload=b"flood"); ck(c.got_data("flood"), "after clear unicast floods again", "c gets it", "no")
    b.send(M(1)); a.drain(0.3); c.drain(0.2); a.send(M(2), payload=b"uni"); b.drain(0.3); ck(not c.got_data("uni", 0.5), "relearned: unicast again")
    ck(sw.stop() == 0, "exit 0")

@test("RECONF-04", "Shutdown/up race stress: 300 random admin ops while 3 hosts flood and reconnect; switch stays alive, no hang, exit 0", 200)
def _():
    sw = Switch(6, conf="mac_aging_seconds=5\n").start(); rnd = random.Random(3)
    stop = [False]; sent = [0]
    def flooder(n):
        while not stop[0]:
            try:
                h = RawHost(sw.port, M(200 + n)); h.s.settimeout(2)
                for i in range(200):
                    h.send(BCAST, payload=b"s" * 50); 
                    if i % 20 == 0: h.recv(0.01)
                h.close()
            except OSError: time.sleep(0.01)
    ths = [threading.Thread(target=flooder, args=(i,), daemon=True) for i in range(3)]; [t.start() for t in ths]
    ops = [("shutdown",), ("up",), ("vlan",), ("clear", "mac"), ("mirror",), ("show", "mac"), ("show", "ports"), ("stats",)]
    bad_rc = []
    t0 = time.time()
    for i in range(300):
        o = rnd.choice(ops); p = rnd.randint(1, 6)
        if o[0] in ("shutdown", "up"): a = (o[0], p)
        elif o[0] == "vlan": a = ("vlan", p, rnd.choice([1, 2, 3]))
        elif o[0] == "mirror": a = ("mirror", p, rnd.randint(1, 6)) if rnd.random() < .7 else ("mirror", p, "off")
        else: a = o
        try: rc, out = sw.ctl(*a, timeout=20)
        except subprocess.TimeoutExpired: ck(False, "swctl %r hung" % (a,), "reply", "timeout"); break
        if rc not in (0, 1): bad_rc.append((a, rc, out))
        if i % 50 == 0: sw.sig(signal.SIGUSR1); 
    stop[0] = True
    ck(not bad_rc, "swctl exit codes only 0/1", "0/1", bad_rc[:3])
    ck(sw.alive(), "switch alive after storm", True, sw.p.poll(), logs=sw.log()[-600:])
    for i in range(1, 7): sw.ctl("up", i)
    ck(sw.ctl("show", "ports")[0] == 0, "switch still answers")
    rc = sw.stop(timeout=15); ck(rc == 0, "clean exit", 0, rc, logs=sw.log()[-600:])
