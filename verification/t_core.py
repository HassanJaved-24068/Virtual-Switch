from vt import *

# ======================= BUILD =======================
@test("BLD-01", "Clean build from a pristine copy: gcc -Wall -Wextra -Werror, zero warnings", "roadmap global rule: builds with -std=c11 -Wall -Wextra -Werror", 300)
def _():
    d = tempfile.mkdtemp(prefix="vtb.")
    try:
        rc, out = sh("cp -r src include Makefile %s/ && cd %s && make 2>&1" % (d, d))
        ck(rc == 0, "make succeeds", "rc 0", "rc %s\n%s" % (rc, out[-600:]), logs=out)
        ck("warning" not in out.lower(), "no warnings", "none", out[-400:])
        for b in ("switch", "host", "swctl", "supervisor"):
            ck(os.path.exists(os.path.join(d, b)), "binary %s produced" % b, "exists", "missing")
        sh("cd %s && make clean >/dev/null 2>&1" % d); out = " ".join(os.listdir(d))
        left = [x for x in out.split() if x in ("switch", "host", "swctl", "supervisor")]
        ck(not left, "make clean removes the programs", "no programs left", left)
        rc, out = sh("cd %s && make tsan 2>&1" % d, timeout=300)
        ck(rc == 0 and os.path.exists(d + "/switch_tsan") and os.path.exists(d + "/host_tsan"), "make tsan produces switch_tsan/host_tsan", "both exist", out[-400:])
    finally: shutil.rmtree(d, ignore_errors=True)

@test("BLD-02", "make bench builds three lock variants + bench programs", "Part 21 make bench", 300)
def _():
    d = tempfile.mkdtemp(prefix="vtb.")
    try:
        sh("cp -r src include bench Makefile %s/" % d)
        rc, out = sh("cd %s && make bench 2>&1" % d, timeout=300)
        ck(rc == 0, "make bench succeeds", "rc 0", out[-500:])
        for b in ("switch_global", "switch_striped", "switch_rwlock", "bench/bench"):
            ck(os.path.exists(os.path.join(d, b)), "%s built" % b, "exists", "missing")
    finally: shutil.rmtree(d, ignore_errors=True)

# ======================= WIRE FORMAT / FORWARDING =======================
@test("WIRE-01", "Unicast frame layout on the wire; byte order of length; learned unicast goes only to destination")
def _():
    sw = Switch(4).start(); a, b, c = hosts_up(sw, 3)
    a.send(M(2), payload=b"hello"); # unknown -> flood
    fb = b.expect(); fc = c.expect()
    ck(fb and fb["data"] == b"hello" and fc and fc["data"] == b"hello", "unknown unicast floods to every other port", "b and c get it", (fb, fc))
    b.send(M(1), payload=b"re")            # teaches b
    ck(a.got_data("re"), "reply arrives at a")
    c.drain(0.3)
    a.send(M(2), payload=b"\x01\x02" * 600)  # len 1200 = 0x04B0
    fb = b.expect()
    ck(fb is not None and fb["len"] == 1200 and fb["raw"][:2] == b"\x04\xb0", "length is network byte order 04 B0", "04 b0", fb["raw"][:2].hex() if fb else None)
    ck(fb is not None and fb["raw"][2:8] == mac_b(M(1)) and fb["raw"][8:14] == mac_b(M(2)), "src/dst at offsets 2 and 8")
    ck(c.recv(0.5) is None, "learned unicast is NOT delivered to a third host", "nothing", "frame at c")
    ck(a.recv(0.3) is None, "no echo to sender")
    rc = sw.stop(); ck(rc == 0, "SIGINT exit 0", 0, rc)

@test("WIRE-02", "Payload length boundaries: 0, 1, 1499, 1500 delivered intact")
def _():
    sw = Switch(3).start(); a, b = hosts_up(sw, 2)
    for n in (0, 1, 1499, 1500):
        pl = bytes((i * 7 + n) & 255 for i in range(n))
        a.send(BCAST, payload=pl)
        f = b.expect(3)
        ck(f is not None and f["len"] == n and f["data"] == pl, "len %d delivered intact" % n, "identical", None if f is None else (f["len"], f["data"][:8]))
    ck(sw.stop() == 0, "clean exit")

@test("WIRE-03", "Oversize length 1501 / 65535 -> protocol violation, only that host is disconnected")
def _():
    sw = Switch(3).start(); a, b, c = hosts_up(sw, 3)
    a.raw(pack(M(1), BCAST, 1, 1, b"", length=1501))
    ck(a.eof(3), "host with len=1501 is disconnected", "EOF", "still connected", logs=sw.log())
    ck(sw.wait_log("port is FREE", 1), "port freed", "teardown logged", sw.log())
    b.send(M(3), payload=b"ok"); ck(c.got_data("ok"), "other hosts unaffected")
    c.raw(pack(M(3), BCAST, 1, 1, b"", length=0xFFFF))
    ck(c.eof(3), "len=65535 disconnects", "EOF", "connected")
    ck(sw.alive(), "switch alive")
    ck(sw.stop() == 0, "clean exit")

@test("WIRE-04", "Partial writes: 1-byte chunks, 1.5 frames in one write, header split")
def _():
    sw = Switch(3).start(); a, b = hosts_up(sw, 2)
    fr = pack(M(1), BCAST, 1, 1, b"slowdata")
    for x in fr: a.s.sendall(bytes([x])); time.sleep(0.002)
    ck(b.got_data("slowdata", 3), "1-byte-at-a-time frame reassembled")
    f1 = pack(M(1), BCAST, 1, 1, b"one"); f2 = pack(M(1), BCAST, 1, 1, b"twotwotwo")
    a.raw(f1 + f2[:10]); time.sleep(0.2); a.raw(f2[10:])
    ck(b.got_data("one") and b.got_data("twotwotwo"), "1.5 frames then rest")
    a.raw(f1 * 50)
    n = 0
    while b.recv(0.5) not in (None, "EOF"): n += 1
    ck(n == 50, "50 back-to-back frames in one write", 50, n)
    ck(sw.stop() == 0, "clean exit")

@test("WIRE-05", "Truncated frames (mid-header, mid-payload, zero-bytes) free the port, switch survives")
def _():
    sw = Switch(2, ).start()
    for i, cut in enumerate((5, 17, 20)):
        h = sw.host(M(1)); settle(sw, i + 1)
        h.raw(pack(M(1), BCAST, 1, 1, b"A" * 10)[:cut]); h.close()
        ck(sw.wait_log("port is FREE", i + 1), "truncated at %d -> port freed" % cut, "FREE", sw.log())
    ck("truncat" in sw.log().lower(), "log mentions truncated frame", "truncat*", sw.log()[-500:])
    h = sw.host(M(1)); settle(sw, 4); h.send(BCAST); ck(sw.alive(), "alive")
    ck(sw.stop() == 0, "exit 0")

@test("WIRE-06", "Access port overwrites VLAN field; priority>2 forwarded unchanged; vlan 0/4095 on access port accepted & overwritten")
def _():
    sw = Switch(3).start(); a, b = hosts_up(sw, 2)
    for v in (0, 1, 4094, 4095, 65535):
        a.send(BCAST, vlan=v, payload=b"v%d" % v)
        f = b.expect(2)
        ck(f is not None and f["vlan"] == 1, "access vlan %d rewritten to port VLAN 1" % v, 1, None if f is None else f["vlan"])
    for p in (2, 3, 7, 255):
        a.send(BCAST, prio=p, payload=b"p%d" % p)
        f = b.expect(2)
        ck(f is not None and f["prio"] == p, "prio %d forwarded unchanged" % p, p, None if f is None else f["prio"])
    ck(sw.alive(), "alive"); ck(sw.stop() == 0, "exit 0")

@test("WIRE-07", "Source broadcast dropped; multicast src forwarded but not learned; dst==own port filtered")
def _():
    sw = Switch(3).start(); a, b, c = hosts_up(sw, 3)
    a.send(M(2), src=BCAST, payload=b"bsrc")
    ck(not b.got_data("bsrc", 0.8), "frame with broadcast src is dropped", "not delivered", "delivered")
    a.send(BCAST, src="01:00:5e:00:00:01", payload=b"msrc")
    ck(b.got_data("msrc", 1), "multicast-src frame is flooded (only learning skipped)")
    rows = sw.mac_table()
    ck(not any(r[1] == "01:00:5e:00:00:01" for r in rows), "multicast src not learned", "absent", rows)
    a.send("01:00:5e:01:02:03", payload=b"mdst"); ck(b.got_data("mdst") and c.got_data("mdst"), "multicast dst floods")
    # learn b then a sends to b's mac but b mac is on ingress port => filter
    b.send(M(1), payload=b"learn"); a.drain(0.3)
    b.send(M(2), payload=b"selfdst")   # dst learned on ingress port -> dropped
    ck(not a.got_data("selfdst", 0.6) and not c.got_data("selfdst", 0.6), "dst on ingress port is filtered, not flooded", "dropped", "delivered")
    ck(sw.stop() == 0, "exit 0")

@test("WIRE-08", "Src MAC spoofing / hosts with the same MAC on two ports: MAC moves, logged")
def _():
    sw = Switch(4).start(); a, b, c = hosts_up(sw, 3)
    a.send(BCAST, src=M(50), payload=b"1"); b.drain(0.3); c.drain(0.3)
    b.send(BCAST, src=M(50), payload=b"2")
    ck(sw.wait_log("MAC moved", 1), "MAC moved logged", "'MAC moved' line", sw.log())
    rows = [r for r in sw.mac_table() if r[1] == M(50)]
    ck(rows and rows[0][2] == 2, "table says port 2", 2, rows)
    ck(sw.stop() == 0, "exit 0")

@test("WIRE-09", "Garbage fuzz: random bytes on 30 connections + random valid-looking headers; switch survives and still forwards")
def _():
    sw = Switch(8).start(); rnd = random.Random(7)
    good1 = sw.host(M(1)); good2 = sw.host(M(2)); settle(sw, 2)
    for i in range(30):
        try:
            s = socket.create_connection(("127.0.0.1", sw.port), 2)
            for _ in range(rnd.randint(1, 5)):
                n = rnd.randint(1, 3000); s.sendall(bytes(rnd.getrandbits(8) for _ in range(n)))
            s.close()
        except OSError: pass
        time.sleep(0.01)
    time.sleep(1)
    ck(sw.alive(), "switch alive after fuzz", True, sw.p.poll(), logs=sw.log())
    good1.send(BCAST, payload=b"still"); ck(good2.got_data("still", 3), "forwarding still works after fuzz")
    ck(sw.stop() == 0, "clean exit after fuzz", 0, sw.p.returncode)
    ck(san_clean(sw), "no sanitizer/valgrind complaints")

@test("PORT-01", "Lowest-free-port allocation and reuse; show ports reflects it")
def _():
    sw = Switch(4).start(); a, b, c = hosts_up(sw, 3)
    ck(sw.wait_ports("UP", [1, 2, 3]), "3 ports UP")
    b.close(); ck(sw.wait_ports("FREE", [2]), "port 2 FREE after close")
    d = sw.host(M(9)); settle(sw, 4)
    ck(sw.wait_ports("UP", [1, 2, 3]), "new host takes lowest free port (2)", "ports 1-3 UP,4 FREE", sw.ctl("show", "ports")[1])
    d.send(BCAST); a.drain(0.3)
    st = sw.stats(); ck(st.get(2, {}).get("inn") == 1, "frame counted IN on port 2", 1, st)
    ck(sw.stop() == 0, "exit 0")

@test("PORT-02", "Switch full: 3rd host waits then gets exact 'switch full' frame after port_wait_timeout")
def _():
    sw = Switch(2, conf="port_wait_timeout = 2\n").start(); a, b = hosts_up(sw, 2)
    t0 = time.time(); c = sw.host(M(3)); f = c.recv(6)
    dt = time.time() - t0
    ck(isinstance(f, dict), "full frame received", "frame", f)
    if isinstance(f, dict):
        exp = pack("00:00:00:00:00:00", BCAST, 1, 0, b"switch full")
        ck(f["raw"] == exp, "frame bytes exact", exp.hex(), f["raw"].hex())
    ck(1.6 <= dt <= 3.5, "arrives after ~2s", "1.6-3.5s", "%.2f" % dt)
    ck(c.eof(2), "then connection closed")
    ck(sw.alive(), "switch alive"); ck(sw.stop() == 0, "exit 0")

@test("PORT-03", "Waiting host gets a port when another quits within the timeout")
def _():
    sw = Switch(2, conf="port_wait_timeout = 5\n").start(); a, b = hosts_up(sw, 2)
    c = sw.host(M(3)); time.sleep(0.5); b.close()
    t0 = time.time(); settle(sw, 3, 4)
    ck(sw.log().count("host connected") == 3, "3rd host admitted", 3, sw.log().count("host connected"), logs=sw.log())
    a.send(M(3), payload=b"hi"); c.send(M(1), payload=b"yo")
    ck(a.got_data("yo", 2), "c is functional")
    ck(sw.stop() == 0, "exit 0")

@test("PORT-04", "port_wait_timeout = 0: immediate 'switch full'")
def _():
    sw = Switch(1, conf="port_wait_timeout = 0\n").start(); a = hosts_up(sw, 1)[0]
    t0 = time.time(); c = sw.host(M(3)); f = c.recv(3)
    ck(isinstance(f, dict) and f["data"] == b"switch full", "full frame", "switch full", f)
    ck(time.time() - t0 < 1.2, "immediately", "<1.2s", time.time() - t0)
    ck(sw.stop() == 0, "exit 0")

@test("PORT-05", "200 connect/disconnect cycles: threads, fds and RSS stay stable")
def _():
    sw = Switch(3).start(); a = sw.host(M(1)); settle(sw, 1)
    th0, fd0, rss0 = sw.threads(), sw.nfds(), sw.rss_kb()
    for i in range(200):
        h = sw.host(M(10 + (i % 5))); h.send(BCAST, payload=b"x"); h.close(); sw.hosts.remove(h)
        if i % 20 == 0: time.sleep(0.15)
    sw.wait_ports("FREE", [2, 3], 8); time.sleep(1.0)
    th1, fd1, rss1 = sw.threads(), sw.nfds(), sw.rss_kb()
    ck(th1 <= th0 + 1, "thread count stable", th0, th1)
    ck(fd1 <= fd0 + 1, "fd count stable", fd0, fd1)
    ck(rss1 < rss0 + 4000, "RSS growth < 4MB", "<%d" % (rss0 + 4000), rss1)
    ck(sw.alive(), "alive"); ck(sw.stop() == 0, "exit 0")

@test("PORT-06", "64 ports: 64 simultaneous hosts, 65th gets switch full; broadcast reaches 63")
def _():
    sw = Switch(64, conf="port_wait_timeout = 1\n").start()
    hs = [sw.host(M(i + 1)) for i in range(64)]
    ck(settle(sw, 64, 15), "64 connected", 64, sw.log().count("host connected"))
    x = sw.host(M(99)); f = x.recv(4); ck(isinstance(f, dict) and f["data"] == b"switch full", "65th refused", "switch full", f)
    hs[0].send(BCAST, payload=b"bc")
    got = sum(1 for h in hs[1:] if h.got_data("bc", 3))
    ck(got == 63, "63 receivers got broadcast", 63, got)
    ck(hs[0].recv(0.3) is None, "sender got no echo")
    ck(sw.threads() >= 64 * 2, "has rx+tx per port", ">=128", sw.threads())
    ck(sw.stop() == 0, "exit 0")

def _hol(frozen):
    sw = Switch(4, conf="queue_capacity = 256\n").start()
    a, b, c = hosts_up(sw, 3)
    if frozen: c.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    else:
        threading.Thread(target=lambda: [c.recv(8) for _ in range(1)], daemon=True).start()
    N = 4000; got = [0]; stop = [False]
    def rd():
        while True:
            f = b.recv(3)
            if not isinstance(f, dict): break
            got[0] += 1
    def rd2():
        while not stop[0]:
            f = c.recv(1)
            if f == "EOF": break
    if not frozen: threading.Thread(target=rd2, daemon=True).start()
    t = threading.Thread(target=rd); t.start()
    t0 = time.time(); blob = pack(M(1), BCAST, 1, 1, b"x" * 1400)
    for i in range(N):
        a.s.sendall(blob)
        if i % 8 == 7: time.sleep(0.0005)       # paced so that python readers can keep up
    dt = time.time() - t0; t.join(); stop[0] = True
    st = sw.stats()
    return sw, dt, got[0], st

@test("HOL-01", "Head-of-line: frozen reader on port C does not slow A->B; drops counted on C; B receives like in a control run")
def _():
    sw0, dt0, got0, _s = _hol(False); sw0.stop()
    sw, dt, got, st = _hol(True)
    ck(dt < dt0 * 1.5 + 1, "A's send time with frozen C ~ control", "<%.1fs" % (dt0 * 1.5 + 1), "%.1fs (control %.1fs)" % (dt, dt0))
    ck(got >= got0 * 0.95, "B receives as many frames as in the control run", ">=%d" % int(got0 * .95), "%d (control %d)" % (got, got0))
    ck(st.get(3, {}).get("drop", 0) > 0, "drops counted on frozen port 3", ">0", st)
    ck(sw.stop(timeout=8) == 0, "clean exit with a stuck host", 0, sw.p.returncode)
