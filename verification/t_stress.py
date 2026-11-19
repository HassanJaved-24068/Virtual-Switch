from vt import *

def children(pid):
    r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
    return [int(x) for x in r]

def scenario(sw, n_hosts=8, secs=4.0):
    """mixed traffic + admin + signals; returns nothing, used by sanitizer runs too"""
    hs = [sw.host(M(i + 1)) for i in range(n_hosts)]
    settle(sw, n_hosts, 30)
    stop = [False]
    def flood(h, i):
        try:
            while not stop[0]:
                h.send(BCAST if i % 2 else M(((i + 1) % n_hosts) + 1), payload=b"s" * (i * 37 % 1400))
                time.sleep(0.002)
        except OSError: pass
    def reader(h):
        while not stop[0]:
            f = h.recv(0.2)
            if f == "EOF": break
    ths = []
    for i, h in enumerate(hs):
        if i != 3: ths.append(threading.Thread(target=reader, args=(h,), daemon=True))     # host 4 never reads
        ths.append(threading.Thread(target=flood, args=(h, i), daemon=True))
    [t.start() for t in ths]
    t0 = time.time(); k = 0
    while time.time() - t0 < secs:
        k += 1
        for a in (("vlan", 2, 1 + k % 3), ("clear", "mac"), ("mirror", 1, 5), ("mirror", 1, "off"), ("show", "mac"), ("stats",), ("shutdown", 6), ("up", 6)):
            sw.ctl(*a, timeout=30)
        sw.sig(signal.SIGUSR1)
        if hasattr(sw, "conffile"): sw.sig(signal.SIGHUP)
        time.sleep(0.1)
    stop[0] = True; time.sleep(0.3)

@test("STR-01", "Hidden-test simulation: 32 hosts (noread, partial, kill -9 mid-stream, die-midframe, flooders) + admin commands + SIGHUP/SIGUSR1 throughout; untouched hosts keep traffic; clean exit", "roadmap Part 18", 200)
def _():
    sw = Switch(32, conf="mac_aging_seconds=20\nport_wait_timeout=1\n").start()
    H = ROOT + "/host"; procs = []
    def spawn(i, *opts, stdin=True):
        p = subprocess.Popen([H, "127.0.0.1", str(sw.port), M(300 + i)] + list(opts), stdin=subprocess.PIPE if stdin else subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p); return p
    good = [sw.host(M(i)) for i in range(1, 7)]; settle(sw, 6)
    noread = [spawn(i, "--noread") for i in range(8)]
    partial = [spawn(20 + i, "--partial") for i in range(4)]
    for p in partial: p.stdin.write(b"send bcast slow\n" * 20); p.stdin.flush()
    floods = [spawn(40 + i, "--flood", "20000", "--to", "ff:ff:ff:ff:ff:ff") for i in range(4)]
    die = [spawn(60 + i, "--die-midframe") for i in range(4)]
    stop = [False]; recv_cnt = [0]
    def rd(h):
        while not stop[0]:
            f = h.recv(0.3)
            if isinstance(f, dict): recv_cnt[0] += 1
    ths = [threading.Thread(target=rd, args=(h,), daemon=True) for h in good]; [t.start() for t in ths]
    def sender():
        while not stop[0]:
            try: good[0].send(M(2), payload=b"g"); time.sleep(0.003)
            except OSError: break
    ts = threading.Thread(target=sender, daemon=True); ts.start()
    t0 = time.time(); rnd = random.Random(5); k = 0
    while time.time() - t0 < 12:
        k += 1
        if k % 3 == 0 and floods: 
            p = floods.pop(); p.kill()                   # kill -9 mid-stream
        for a in (("show", "ports"), ("clear", "mac"), ("vlan", rnd.randint(20, 32), rnd.randint(1, 3)), ("shutdown", rnd.randint(7, 32)), ("up", rnd.randint(7, 32)), ("stats",)):
            try: sw.ctl(*a, timeout=30)
            except subprocess.TimeoutExpired: ck(False, "admin %r hung under load" % (a,), "reply", "timeout")
        sw.sig(signal.SIGUSR1); sw.sig(signal.SIGHUP)
        if k % 2 == 0: spawn(100 + k, "--die-midframe")
        time.sleep(0.2)
    n_at_end = recv_cnt[0]
    ck(sw.alive(), "switch alive", True, sw.p.poll(), logs=sw.log()[-800:])
    ck(n_at_end > 500, "untouched hosts received traffic throughout", ">500", n_at_end)
    ck(sw.ctl("show", "ports")[0] == 0, "admin still answers")
    stop[0] = True
    t0 = time.time(); rc = sw.stop(timeout=12)
    ck(rc == 0, "clean exit 0", 0, rc, logs=sw.log()[-800:]); ck(time.time() - t0 < 6, "exit promptly", "<6s", time.time() - t0)
    for p in procs:
        try: p.kill()
        except Exception: pass
    for p in procs:
        try: p.wait(2)
        except Exception: pass

@test("STR-02", "Descriptor exhaustion (ulimit -n 40) with 80 connection attempts: switch never exits, recovers, admin works", "roadmap: EMFILE is never fatal")
def _():
    sw = Switch(8, ulimit_n=40, conf="port_wait_timeout=1\n").start()
    socks = []
    for i in range(80):
        try: s = socket.create_connection(("127.0.0.1", sw.port), 2); socks.append(s)
        except OSError: pass
    time.sleep(1.5)
    ck(sw.alive(), "switch alive during EMFILE storm", True, sw.p.poll(), logs=sw.log()[-600:])
    for s in socks: s.close()
    time.sleep(2.5)
    ck(sw.alive(), "alive after storm")
    rc, out = sw.ctl("show", "ports"); ck(rc == 0, "admin answers after storm", 0, (rc, out))
    a = sw.host(M(1)); b = sw.host(M(2)); settle(sw, sw.log().count("host connected") + 0, 3)
    time.sleep(0.5); a.send(BCAST, payload=b"rec"); ck(b.got_data("rec", 3), "forwarding works after recovery", "delivered", "missing", logs=sw.log()[-600:])
    ck(sw.stop() == 0, "exit 0", 0, sw.p.returncode)

@test("STR-03", "Connection storm: 3000 connect/close without data; thread/fd counts return to baseline; RSS stable", "", 120)
def _():
    sw = Switch(4, conf="port_wait_timeout=1\n").start(); time.sleep(0.2)
    th0, fd0, r0 = sw.threads(), sw.nfds(), sw.rss_kb()
    for i in range(3000):
        try: s = socket.create_connection(("127.0.0.1", sw.port), 2); s.close()
        except OSError: pass
    time.sleep(6)
    ck(sw.alive(), "alive", True, sw.p.poll(), logs=sw.log()[-400:])
    th1, fd1, r1 = sw.threads(), sw.nfds(), sw.rss_kb()
    ck(th1 <= th0 + 1, "threads back to baseline", th0, th1); ck(fd1 <= fd0 + 1, "fds back to baseline", fd0, fd1); ck(r1 < r0 + 5000, "RSS growth <5MB", "<%d" % (r0 + 5000), r1)
    a = sw.host(M(1)); b = sw.host(M(2)); time.sleep(0.5); a.send(BCAST, payload=b"s"); ck(b.got_data("s", 3), "forwarding OK after storm")
    ck(sw.stop() == 0, "exit 0")

@test("STR-04", "Data integrity under load: 4 unicast flows + broadcast, 1500B payload with checksum, in-order per flow, no corruption, no duplicates", "", 120)
def _():
    sw = Switch(8, conf="queue_capacity=4096\n").start(); hs = hosts_up(sw, 8)
    for h in hs: h.send(BCAST, payload=b"learn"); 
    time.sleep(0.5); [h.drain(0.3) for h in hs]
    import zlib
    N = 400; errs = []; counts = {}
    def sender(i):
        src = hs[i]; dst = M(((i + 4) % 8) + 1)
        for k in range(N):
            body = struct.pack("!II", i, k) + bytes((i * 31 + k * 7 + j) & 255 for j in range(1400))
            src.send(dst, payload=struct.pack("!I", zlib.crc32(body)) + body); time.sleep(0.0003)
    def receiver(i):
        h = hs[(i + 4) % 8]; last = -1; n = 0
        while n < N:
            f = h.recv(4)
            if not isinstance(f, dict): errs.append("recv timeout %d after %d" % (i, n)); break
            d = f["data"]
            if len(d) != 1412 or struct.unpack("!I", d[:4])[0] != zlib.crc32(d[4:]): errs.append("corrupt flow %d" % i); break
            s, k = struct.unpack("!II", d[4:12])
            if s != i or k <= last: errs.append("order/dup flow %d: %d after %d" % (i, k, last)); break
            last = k; n += 1
        counts[i] = n
    ts = [threading.Thread(target=sender, args=(i,)) for i in range(4)] + [threading.Thread(target=receiver, args=(i,)) for i in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    ck(not errs, "no loss/corruption/reordering", "none", errs[:4]); ck(all(counts.get(i) == N for i in range(4)), "all %d frames per flow received" % N, N, counts)
    ck(sw.stop() == 0, "exit 0")

@test("STR-05", "64 hosts broadcast concurrently (64x300 frames); switch alive, counters consistent (in = frames sent), exit 0", "", 200)
def _():
    sw = Switch(64, conf="queue_capacity=128\n").start(); hs = hosts_up(sw, 64)
    stop = [False]
    def rd(h):
        while not stop[0]:
            if h.recv(0.2) == "EOF": break
    [threading.Thread(target=rd, args=(h,), daemon=True).start() for h in hs]
    def snd(h):
        for k in range(300): h.send(BCAST, payload=b"z" * 200)
    ts = [threading.Thread(target=snd, args=(h,)) for h in hs]; t0 = time.time(); [t.start() for t in ts]; [t.join() for t in ts]
    time.sleep(1.5); stop[0] = True
    st = sw.stats()
    ck(sum(v["inn"] for k, v in st.items() if k) == 64 * 300, "IN counters sum = frames sent", 64 * 300, sum(v["inn"] for k, v in st.items() if k))
    tot = sum(v["out"] + v["drop"] for k, v in st.items() if k)
    ck(tot == 64 * 300 * 63, "out+dropped = in * 63 (every copy accounted for)", 64 * 300 * 63, tot)
    ck(sw.alive(), "alive"); ck(sw.stop(timeout=15) == 0, "exit 0")

@test("STR-06", "Slowloris: a host holding half a header forever blocks nobody and does not prevent shutdown; 20 such hosts", "")
def _():
    sw = Switch(24).start(); a, b = hosts_up(sw, 2)
    slow = []
    for i in range(20):
        h = sw.host(M(50 + i)); h.raw(b"\x00\x05\x02\x00"); slow.append(h)
    a.send(BCAST, payload=b"fast"); ck(b.got_data("fast"), "others unaffected")
    ck(sw.stop(timeout=6) == 0, "shutdown exit 0 with 20 half-header hosts", 0, sw.p.returncode, logs=sw.log()[-300:])

@test("STR-07", "Idle: 8 connected idle hosts, 5 s => CPU < 2% (no busy waiting)", "roadmap: no busy waiting")
def _():
    sw = Switch(8).start(); hosts_up(sw, 8); time.sleep(1)
    c0 = sw.cpu_ticks(); time.sleep(5); c1 = sw.cpu_ticks()
    ck((c1 - c0) <= 10, "<=10 ticks (2%) in 5s", "<=10", c1 - c0); ck(sw.stop() == 0, "exit 0")

@test("STR-08", "Sustained flood: 100000 frames A->B (unicast) ; throughput, no frame lost with consumer keeping up (C reader --quiet), RSS stable", "", 200)
def _():
    sw = Switch(3, conf="queue_capacity=4096\n").start(); a, b = hosts_up(sw, 2)
    b.send(M(1)); a.drain(0.2); r0 = sw.rss_kb()
    rcv = subprocess.Popen([ROOT + "/host", "127.0.0.1", str(sw.port), M(9), "--quiet"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    time.sleep(0.5); got = [0]
    def rd():
        while True:
            f = b.recv(4)
            if not isinstance(f, dict): break
            got[0] += 1
    t = threading.Thread(target=rd); t.start(); blob = pack(M(1), M(2), 1, 1, b"p" * 64)
    t0 = time.time()
    for i in range(1000): a.s.sendall(blob * 100)       # 100k frames
    t.join(); dt = time.time() - t0
    ck(got[0] > 90000, "most of 100000 frames delivered (consumer is python)", ">90000", got[0])
    st = sw.stats(); ck(st[2]["out"] + st[2]["drop"] == 100000, "out+drop on port 2 == 100000 (accounting exact)", 100000, st[2])
    ck(sw.rss_kb() < r0 + 20000, "RSS growth < 20MB", "<%d" % (r0 + 20000), sw.rss_kb())
    rcv.communicate("quit\n", timeout=5)
    ck(sw.stop() == 0, "exit 0")

@test("STR-09", "Static rule checks: no sleep()/usleep() in switch sources; every pthread_*/read/write result handled (grep)", "roadmap global rules")
def _():
    rc, out = sh("grep -n '\\bsleep(\\|usleep(' src/*.c | grep -v '^src/host.c\\|^src/swctl.c\\|^src/supervisor.c' | grep -v '^[^:]*:[0-9]*: *[/*]'")
    ck(out.strip() == "", "no sleep()/usleep() in switch code", "none", out)
    rc, out = sh("grep -n 'while *(! *[a-z_>.-]*) *;' src/*.c"); ck(out.strip() == "", "no empty-body busy loops", "none", out)
    rc, out = sh("grep -n 'pthread_[a-z_]*(' src/*.c | grep -v 'destroy_failed\\|CHECK_PT\\|rc = \\|int rc\\|(void)\\|if (\\|= pthread\\|return pthread\\|_rc\\|err = \\|e = \\|r = \\|ret' | grep -v '^[^:]*:[0-9]*: *[/*]' | head -40")
    ck(out.strip() == "", "pthread_* results are checked", "all checked", out[:600])

@test("STR-10", "Unaligned/odd: frame sent in 1500 separate 1-byte writes at once from 30 hosts concurrently (partial reassembly under load)", "", 120)
def _():
    sw = Switch(34).start(); hs = hosts_up(sw, 30); rx = sw.host(M(99)); settle(sw, 31)
    fr = pack(M(1), BCAST, 1, 1, b"w" * 300)
    def trickle(h):
        f = pack(h.mac, M(99), 1, 1, b"w" * 300)
        for x in f: h.s.sendall(bytes([x]))
    ts = [threading.Thread(target=trickle, args=(h,)) for h in hs]; [t.start() for t in ts]; [t.join() for t in ts]
    n = len([f for f in rx.drain(1.0, 15) if isinstance(f, dict) and f["data"] == b"w" * 300])
    ck(n == 30, "30 trickled frames all arrive intact", 30, n)
    ck(sw.stop() == 0, "exit 0")

# ======================= SANITIZERS =======================
def _san(binary, pre, label, secs):
    sw = Switch(16, conf="mac_aging_seconds=30\nport.2.vlan=2\n", binary=binary, pre=pre)
    sw.start(timeout=60); ck(sw.alive(), "%s switch starts" % label, True, sw.err())
    if not sw.alive(): return
    scenario(sw, 8, secs)
    ck(sw.alive(), "%s alive after scenario" % label, True, sw.err()[-600:])
    rc = sw.stop(timeout=60)
    ck(rc == 0, "%s exit status 0" % label, 0, rc, logs=(sw.err() + sw.log())[-1500:])
    ck(san_clean(sw), "%s: no sanitizer / valgrind errors" % label, "clean", (sw.err() + sw.log())[-1500:], logs=(sw.err() + sw.log())[-1500:])
    if label == "valgrind":
        ck("ERROR SUMMARY: 0 errors" in sw.err(), "valgrind ERROR SUMMARY 0", "0 errors", sw.err()[-500:])
        ck("All heap blocks were freed" in sw.err() or "no leaks are possible" in sw.err(), "no leaks", "all freed", sw.err()[-500:])

@test("SAN-01", "valgrind (full leak check) on switch under mixed traffic + admin + signals + SIGINT", "roadmap: valgrind-clean", 400)
def _():
    if not shutil.which("valgrind"): raise Skip("valgrind not installed")
    _san("switch", ["valgrind", "--leak-check=full", "--show-leak-kinds=all", "--errors-for-leak-kinds=all", "--error-exitcode=1", "--track-fds=yes"], "valgrind", 10)

@test("SAN-02", "ThreadSanitizer build under mixed traffic + admin + signals + SIGINT", "roadmap: TSan-clean", 400)
def _():
    if not os.path.exists(ROOT + "/switch_tsan"): raise Skip("switch_tsan missing")
    _san("switch_tsan", ["setarch", "-R"], "tsan", 10)

@test("SAN-03", "valgrind: hostile scenario (noread host with full queue, waiting host, truncated frames, EMFILE-free storm) then SIGINT", "", 400)
def _():
    if not shutil.which("valgrind"): raise Skip("valgrind not installed")
    sw = Switch(4, conf="port_wait_timeout=20\nqueue_capacity=8\n", pre=["valgrind", "--leak-check=full", "--show-leak-kinds=all", "--errors-for-leak-kinds=all", "--error-exitcode=1", "--track-fds=yes"])
    sw.start(timeout=60)
    a = sw.host(M(1)); b = sw.host(M(2)); c = sw.host(M(3)); d = sw.host(M(4)); settle(sw, 4, 30); w = sw.host(M(5))
    b.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    for i in range(1500): a.send(BCAST, payload=b"y" * 1400)
    c.raw(pack(M(3), BCAST)[:9]); c.close()
    d.raw(pack(M(4), BCAST, 1, 1, b"", length=2000))
    time.sleep(1); rc = sw.stop(timeout=60)
    ck(rc == 0, "exit 0 under valgrind", 0, rc, logs=sw.err()[-1500:]); ck("ERROR SUMMARY: 0 errors" in sw.err(), "0 errors", "0", sw.err()[-400:])
