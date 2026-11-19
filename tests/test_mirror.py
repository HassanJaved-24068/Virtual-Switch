#!/usr/bin/env python3
"""
Part 17 end-to-end tests: port mirroring (swctl mirror <port> <sniffer> | off) with the real ./switch.

  python3 tests/test_mirror.py            normal build
  python3 tests/test_mirror.py tsan       SWITCH="setarch -R ./switch_tsan"
  python3 tests/test_mirror.py valgrind   valgrind on ./switch

The forwarding rules (both directions, the same frame, VLAN ignored, never back to the ingress port, a full sniffer queue, no copy to a sniffer that is not UP)
are unit tested exactly in tests/test_mirror.c. Here: the whole switch with hosts: what a sniffer receives, the error replies, mirroring that ends when either
host goes away, a sniffer that never reads (accounting identity: every frame of the mirrored port is either a mirrored copy or a drop on the SNIFFER, and the
mirrored port and its destination lose nothing), the real `./host --sniff`, SIGINT with a stuck sniffer, and a storm of operations.
"""
import os, random, re, select, signal, socket, struct, subprocess, sys, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_admin as ta
import test_reconfig as tr

check = ta.check
BCAST = ts.BCAST
SLOW = 1


class Recorder(threading.Thread):
    """A sniffer host: connects (optionally with a tiny receive buffer), records every frame as (src, dst, vlan, prio, payload); read=False never reads."""

    def __init__(self, sw, idx, read=True, rcvbuf=None):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        if rcvbuf:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.connect(("127.0.0.1", sw.port))
        ta.CLIENTS.append(self)
        self.idx = idx
        self.got = []
        self.stop = False
        if read:
            self.start()

    def run(self):
        buf = b""
        while not self.stop:
            try:
                if not select.select([self.sock], [], [], 0.2)[0]:
                    continue
                d = self.sock.recv(65536)
            except (OSError, ValueError):
                return
            if not d:
                return
            buf += d
            while len(buf) >= 17:
                n = struct.unpack("!H", buf[:2])[0]
                if len(buf) < 17 + n:
                    break
                src = ":".join("%02x" % b for b in buf[2:8])
                dst = ":".join("%02x" % b for b in buf[8:14])
                vlan, prio = struct.unpack("!HB", buf[14:17])
                self.got.append((src, dst, vlan, prio, buf[17:17 + n]))
                buf = buf[17 + n:]

    def close(self):
        self.stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def mirror_of(sw, p):
    return tr.ports(sw).get(p, ("?",) * 5)[4]


def stats_of(sw):
    return ta.parse_stats(ta.run_stats("stats")[1])        # {port: (state, vlan, in, out, dropped, mirrored)}


# ----------------------------------------------------------------------------------------------
def test_both_directions():
    print("1. the sniffer sees the traffic of the mirrored port in BOTH directions, unchanged, although it is in another VLAN")
    sw = ta.start(4, "port.3.vlan = 99\n")
    a = tr.connect(sw, 1)
    b = tr.connect(sw, 2)
    s = Recorder(sw, 3)
    check(sw.wait_log("port 3: host connected", 20), "the sniffer connected on port 3")
    rc, out, err = ta.ctl(sw, "mirror", "1", "3")
    check(rc == 0 and "port 1 mirrored to port 3" in out, "mirror 1 3: OK (rc=%s %r)" % (rc, out.strip()))
    check(mirror_of(sw, 1) == "3" and mirror_of(sw, 2) == "-", "show ports: port 1 mirrors to 3, port 2 does not")
    a.send(BCAST, 1, b"A-bcast")                             # received on port 1 (a), flooded to B (not mirrored: B is not the mirrored port)
    b.send(BCAST, 1, b"B-bcast")                             # queued to port 1 (b): mirrored
    time.sleep(0.4)
    a.send(ts.ME[2], 1, b"A-to-B")                           # received on port 1 (a)
    b.send(ts.ME[1], 1, b"B-to-A")                           # queued to port 1 (b)
    check(tr.wait_for(lambda: len(s.got) >= 4), "the sniffer received 4 frames (%d)" % len(s.got))
    time.sleep(0.5)
    payloads = sorted(g[4] for g in s.got)
    check(payloads == sorted([b"A-bcast", b"B-bcast", b"A-to-B", b"B-to-A"]), "exactly the frames sent by A and the frames queued to A, each once: %s" % payloads)
    src = {g[4]: (g[0], g[1], g[2]) for g in s.got}
    check(src.get(b"A-to-B") == (ts.ME[1], ts.ME[2], 1) and src.get(b"B-to-A") == (ts.ME[2], ts.ME[1], 1),
          "addresses and the VLAN field are those of the original frames (the sniffer's own VLAN 99 does not change them)")
    st = stats_of(sw)
    check(st[1][5] == 4 and st[3][4] == 0, "stats: port 1 mirrored = 4, port 3 dropped = 0 (%s)" % [st[1][5], st[3][4]])
    check(st[3][2] == 0 and st[3][3] == 4, "the sniffer sent nothing (in 0) and was sent 4 frames (out 4)")
    check(a.frames == 2 and b.frames == 2, "A and B got their normal frames: A 2 (B's two), B 2 (A's two): %d, %d" % (a.frames, b.frames))
    # mirror off: the copies stop
    rc, out, err = ta.ctl(sw, "mirror", "1", "off")
    check(rc == 0 and mirror_of(sw, 1) == "-", "mirror 1 off: OK, the MIRROR column shows '-'")
    a.send(ts.ME[2], 1, b"after-off")
    time.sleep(0.5)
    check(len(s.got) == 4, "no more copies after 'off' (%d)" % len(s.got))
    check(ta.ctl(sw, "mirror", "1", "off")[0] == 0, "off twice is fine")
    for h in (a, b, s):
        h.close()
    sw.stop()


def test_errors():
    print("2. error replies: a port with no host, the trunk as sniffer, the same port, bad numbers")
    sw = ta.start(4, "trunk_port = 4\n")
    a = tr.connect(sw, 1)
    s = tr.connect(sw, 2)
    cases = [(("mirror", "1", "1"), "itself"), (("mirror", "1", "4"), "trunk"), (("mirror", "3", "2"), "mirrored port"), (("mirror", "1", "3"), "sniffer port has no host"),
             (("mirror", "1", "9"), "out of range"), (("mirror", "0", "2"), "out of range"), (("mirror", "1"), "usage"), (("mirror", "1", "2", "3"), "usage")]
    for args, want in cases:
        rc, out, err = ta.ctl(sw, *args)
        check(rc == 1 and want in err, "%s -> ERR '%s' (rc=%s %r)" % (" ".join(args), want, rc, err.strip()))
    check(all(mirror_of(sw, p) == "-" for p in (1, 2, 3, 4)), "no mirroring was set by any of them")
    check(ta.ctl(sw, "mirror", "1", "2")[0] == 0 and mirror_of(sw, 1) == "2", "a valid one still works")
    check(ta.ctl(sw, "mirror", "4", "2")[0] == 1, "the trunk has no host here: refused as 'not UP' (the trunk itself may be mirrored when connected)")
    a.close()
    s.close()
    sw.stop()


def test_ends_when_a_port_goes_down():
    print("3. mirroring ends when the sniffer or the mirrored host goes away; a new host on those ports is not mirrored")
    sw = ta.start(4, "port.3.vlan = 99\n")
    a = tr.connect(sw, 1)
    b = tr.connect(sw, 2)
    s = Recorder(sw, 3)
    check(sw.wait_log("port 3: host connected", 20), "sniffer on port 3")
    check(ta.ctl(sw, "mirror", "1", "3")[0] == 0 and ta.ctl(sw, "mirror", "2", "3")[0] == 0, "ports 1 and 2 both mirror to 3")
    check(mirror_of(sw, 1) == "3" and mirror_of(sw, 2) == "3", "show ports: both columns say 3")
    s.close()                                              # the sniffer host disconnects
    check(tr.wait_for(lambda: mirror_of(sw, 1) == "-" and mirror_of(sw, 2) == "-"), "the sniffer left: both references were cleared by the reaper")
    check(sw.wait_log("port 1: mirroring to port 3 stopped", 10) and sw.wait_log("port 2: mirroring to port 3 stopped", 10), "logged for each port")
    a.frames = b.frames = 0
    a.send(ts.ME[2], 1, b"still works")
    check(tr.wait_for(lambda: b.frames == 1), "A and B keep talking")
    s2 = Recorder(sw, 3)
    check(sw.wait_log("port 3: host connected", 20, 2), "a new sniffer on port 3")
    time.sleep(0.3)
    a.send(ts.ME[2], 1, b"not mirrored")
    time.sleep(0.5)
    check(s2.got == [] and mirror_of(sw, 1) == "-", "the new sniffer gets nothing: mirroring is not resumed by itself")
    # the mirrored port goes down
    check(ta.ctl(sw, "mirror", "1", "3")[0] == 0, "mirror again")
    a.close()
    check(tr.wait_for(lambda: tr.state(sw, 1) == "FREE" and mirror_of(sw, 1) == "-"), "the mirrored host left: its own mirroring is gone")
    a2 = tr.connect(sw, 1)
    time.sleep(0.2)
    check(mirror_of(sw, 1) == "-", "a new host on port 1 is not mirrored")
    # an admin shutdown of the sniffer's port clears the reference too
    check(ta.ctl(sw, "mirror", "2", "3")[0] == 0, "mirror 2 3")
    check(ta.ctl(sw, "shutdown", "3")[0] == 0, "shutdown 3 (the sniffer's port)")
    check(mirror_of(sw, 2) == "-", "the reference to the shut down port is gone")
    check(ta.ctl(sw, "mirror", "2", "3")[0] == 1, "and no new mirroring to a DISABLED port")
    for h in (a2, b, s2):
        h.close()
    sw.stop()


def test_stuck_sniffer():
    print("4. a sniffer that never reads: its copies are dropped on the SNIFFER's counter; the mirrored port and its destination lose nothing")
    sw = ta.start(3, "port.3.vlan = 99\nqueue_capacity = 256\n")
    a = tr.connect(sw, 1)
    b = tr.connect(sw, 2)
    s = Recorder(sw, 3, read=False, rcvbuf=4096)             # connected, never reads, a tiny window
    check(sw.wait_log("port 3: host connected", 20), "the stuck sniffer is on port 3")
    # learn both MACs so that A -> B is a known unicast
    a.send(BCAST, 1, b"learn-a")
    b.send(BCAST, 1, b"learn-b")
    time.sleep(0.5)
    a.frames = b.frames = 0

    def run(gap, seconds, payload):
        sent = [0]
        end = time.time() + seconds
        while time.time() < end:
            a.send(ts.ME[2], 1, payload)
            sent[0] += 1
            time.sleep(gap)
        return sent[0]

    base0 = stats_of(sw)
    n1 = run(0.002, 4 * SLOW, b"x" * 1400)                    # baseline: no mirroring (about 400 frames/s)
    time.sleep(1.5)
    check(b.frames == n1, "baseline: B received all %d frames (%d)" % (n1, b.frames))
    check(ta.ctl(sw, "mirror", "1", "3")[0] == 0, "mirror 1 3")
    st0 = stats_of(sw)
    b.frames = 0
    n2 = run(0.002, 7 * SLOW, b"y" * 1400)                    # now every frame of A is also queued to the stuck sniffer
    check(tr.wait_for(lambda: b.frames == n2, 30), "mirrored to a stuck sniffer: B still received all %d frames (%d)" % (n2, b.frames))
    st1 = stats_of(sw)
    mir = st1[1][5] - st0[1][5]
    drop_s = st1[3][4] - st0[3][4]
    print("  A sent %d frames; mirrored (counted on port 1) %d, dropped on the sniffer's port %d, dropped on port 1 %d and on port 2 %d" % (n2, mir, drop_s, st1[1][4] - st0[1][4], st1[2][4] - st0[2][4]))
    check(mir + drop_s == n2, "accounting: every frame of A produced exactly one copy: mirrored %d + dropped on the sniffer %d = %d" % (mir, drop_s, n2))
    check(drop_s > 0, "the sniffer's queue really filled (its port dropped %d copies)" % drop_s)
    check(st1[2][4] - st0[2][4] == 0 and st1[1][4] - st0[1][4] == 0, "no drops on the mirrored port or on B's port")
    check(st1[1][2] - st0[1][2] == n2, "port 1 received every frame of A (in counter %d)" % (st1[1][2] - st0[1][2]))
    for h in (a, b, s):
        h.close()
    sw.stop()


def test_real_host_sniff():
    print("5. the real ./host --sniff: prints every frame it is sent, sends nothing, ignores stdin (/dev/null), cannot be combined with sending options")
    sw = ta.start(4, "port.3.vlan = 99\n")
    a = tr.connect(sw, 1)
    b = tr.connect(sw, 2)
    out = os.path.join(ta.TMP, "sniff.out")
    f = open(out, "wb")
    p = ta.track(subprocess.Popen([ts.HOST if hasattr(ts, "HOST") else "./host", "127.0.0.1", str(sw.port), ts.ME[3], "--sniff"], stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.DEVNULL))
    check(sw.wait_log("port 3: host connected", 20), "the sniffer host connected")
    time.sleep(0.5)
    check(p.poll() is None, "it is still running although its stdin is /dev/null (it does not read commands)")
    check(ta.ctl(sw, "mirror", "2", "3")[0] == 0, "mirror 2 3")
    a.send(ts.ME[2], 1, b"to B")
    b.send(BCAST, 1, b"from B")
    check(tr.wait_for(lambda: open(out, "rb").read().count(b"\nRX ") + (1 if open(out, "rb").read().startswith(b"RX ") else 0) >= 2), "two lines printed")
    text = open(out, "rb").read().decode()
    check("src=%s dst=%s" % (ts.ME[1], ts.ME[2]) in text and "data=to B" in text, "it printed A's frame to B (queued to the mirrored port 2): %r" % text[:80])
    check("src=%s dst=ff:ff:ff:ff:ff:ff" % ts.ME[2] in text and "data=from B" in text, "and B's broadcast (received on the mirrored port 2)")
    p.terminate()
    check(p.wait(10) is not None, "SIGTERM ends it")
    f.close()
    for args in (["--noread"], ["--partial"], ["--die-midframe"], ["--flood", "3", "--to", "bcast"]):
        r = subprocess.run(["./host", "127.0.0.1", str(sw.port), ts.ME[4], "--sniff"] + args, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        check(r.returncode == 1 and "--sniff only prints" in r.stderr, "--sniff %s: exit 1 and says why (rc=%s)" % (" ".join(args), r.returncode))
    for h in (a, b):
        h.close()
    sw.stop()


def test_sigint_with_stuck_sniffer():
    print("6. SIGINT with mirroring active, a sniffer that never reads and traffic flowing: clean exit, not delayed")
    sw = ta.start(3, "port.3.vlan = 99\n")
    a = tr.connect(sw, 1)
    b = tr.connect(sw, 2)
    s = Recorder(sw, 3, read=False, rcvbuf=4096)
    check(sw.wait_log("port 3: host connected", 20), "stuck sniffer on port 3")
    check(ta.ctl(sw, "mirror", "1", "3")[0] == 0 and ta.ctl(sw, "mirror", "2", "3")[0] == 0, "ports 1 and 2 mirror to 3")
    stop = threading.Event()

    def flood(h, dst):
        while not stop.is_set():
            try:
                h.send(dst, 1, b"f" * 1200)
            except OSError:
                return
            time.sleep(0.002)

    ths = [threading.Thread(target=flood, args=(a, ts.ME[2]), daemon=True), threading.Thread(target=flood, args=(b, ts.ME[1]), daemon=True)]
    for t in ths:
        t.start()
    time.sleep(3 * SLOW)
    rc, dt = ta.sigint_and_wait(sw, 60)
    stop.set()
    check(rc == 0, "SIGINT: exit status 0 (got %s)" % rc)
    check(dt < 4 * SLOW + 1, "the switch left in %.2f s" % dt)
    check(not os.path.exists(ta.sock_path(sw)) and not os.path.exists(ta.SEG), "socket and segment removed")
    for t in ths:
        t.join(10)
    for h in (a, b, s):
        h.close()
    sw.stop()


def test_storm():
    print("7. a storm of mirror / shutdown / up / vlan operations with hosts connecting and leaving: every reference points at a live port; at the end nothing is mirrored")
    sw = ta.start(5, "port_wait_timeout = 1\n")
    stop = threading.Event()
    bad = []
    ops = [0]

    def admin():
        r = random.Random(17)
        while not stop.is_set():
            x, y = r.randint(1, 5), r.randint(1, 5)
            line = r.choice(["mirror %d %d" % (x, y), "mirror %d %d" % (x, y), "mirror %d off" % x, "shutdown %d" % x, "up %d" % x, "vlan %d 7" % x])
            try:
                ta.raw(sw, (line + "\n").encode(), timeout=30)
            except OSError as e:
                bad.append(repr(e))
            ops[0] += 1

    def churn(seed):
        r = random.Random(seed)
        while not stop.is_set():
            try:
                h = ta.Host(sw, r.randint(1, 9))
                for _ in range(r.randint(1, 5)):
                    h.send(BCAST if r.random() < 0.5 else ts.ME[r.randint(1, 9)], 1, b"s" * r.randint(1, 200))
                    time.sleep(r.random() * 0.02)
                h.close()
            except OSError:
                pass

    def watcher():
        while not stop.is_set():
            rows = tr.ports(sw)
            for p, r in rows.items():
                m = r[4]
                if m != "-" and r[0] == "UP" and rows.get(int(m), ("?",))[0] not in ("UP", "CLOSING"):
                    bad.append("port %d mirrors to port %s which is %s" % (p, m, rows.get(int(m), ("?",))[0]))
            time.sleep(0.05)

    ths = [threading.Thread(target=admin, daemon=True), threading.Thread(target=watcher, daemon=True)] + [threading.Thread(target=churn, args=(s,), daemon=True) for s in (1, 2, 3)]
    for t in ths:
        t.start()
    time.sleep(10 * SLOW)
    stop.set()
    for t in ths:
        t.join(30)
    print("  %d admin commands during the storm" % ops[0])
    check(not bad, "no command failed at the socket level and no reference pointed at a dead port (%s)" % bad[:3])
    check(sw.p.poll() is None, "the switch is alive")
    for p in range(1, 6):
        ta.ctl(sw, "up", str(p))
    check(tr.wait_for(lambda: all(r[0] == "FREE" for r in tr.ports(sw).values()) and len(tr.ports(sw)) == 5, 30), "all ports FREE after the storm")
    check(all(r[4] == "-" for r in tr.ports(sw).values()), "no mirroring is left once no host is connected (%s)" % [r[4] for r in tr.ports(sw).values()])
    rc, dt = ta.sigint_and_wait(sw, 60)
    check(rc == 0, "SIGINT: exit status 0 (got %s)" % rc)
    sw.stop()


def test_valgrind():
    print("8. valgrind: mirroring with a sniffer, mirrored host and sniffer leaving, SIGINT: no errors, no leaks, no descriptor left open")
    sw = ta.start(3, "port.3.vlan = 99\n")
    a = tr.connect(sw, 1)
    b = tr.connect(sw, 2)
    s = Recorder(sw, 3)
    check(sw.wait_log("port 3: host connected", 60), "sniffer connected")
    check(ta.ctl(sw, "mirror", "1", "3", timeout=120)[0] == 0, "mirror 1 3")
    for i in range(20):
        a.send(ts.ME[2], 1, b"v%d" % i)
        b.send(ts.ME[1], 1, b"w%d" % i)
    check(tr.wait_for(lambda: len(s.got) >= 40, 120), "the sniffer got the 40 copies (%d)" % len(s.got))
    s.close()
    check(tr.wait_for(lambda: mirror_of(sw, 1) == "-", 60), "the reference was cleared when the sniffer left")
    check(ta.ctl(sw, "mirror", "2", "1", timeout=120)[0] == 0, "mirror 2 1")
    a.close()
    check(tr.wait_for(lambda: mirror_of(sw, 2) == "-", 60), "cleared when the sniffer port went down")
    rc, dt = ta.sigint_and_wait(sw, 180)
    check(rc == 0, "SIGINT: exit status 0 under valgrind (got %s)" % rc)
    b.close()
    log = sw.log()
    check("ERROR SUMMARY: 0 errors from 0 contexts" in log, "valgrind: 0 errors")
    check("in use at exit: 0 bytes in 0 blocks" in log, "valgrind: 0 bytes in use at exit")
    check(re.search(r"(definitely|indirectly|possibly) lost: [1-9]", log) is None, "valgrind: nothing lost")
    fds = [x for x in re.findall(r"Open (?:AF_INET socket|AF_UNIX socket|file descriptor) (\d+)", log) if int(x) > 2]
    check(not fds, "valgrind: no descriptor left open at exit %s" % fds)
    sw.stop()


# ----------------------------------------------------------------------------------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    ta.install_signal_cleanup()
    try:
        if mode == "all":
            ta.run_tests((test_both_directions, test_errors, test_ends_when_a_port_goes_down, test_stuck_sniffer, test_real_host_sniff,
                          test_sigint_with_stuck_sniffer, test_storm))
        elif mode == "tsan":
            ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
            SLOW = 2
            tr.SLOW = 3
            ta.run_tests((test_both_directions, test_ends_when_a_port_goes_down, test_stuck_sniffer, test_sigint_with_stuck_sniffer, test_storm))
        elif mode == "valgrind":
            ts.SWITCH = os.environ.get("SWITCH") or (
                "valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
                "--error-exitcode=1 --track-fds=yes ./switch")
            ta.run_tests((test_valgrind,))
        else:
            print("usage: test_mirror.py [all|tsan|valgrind]")
            sys.exit(2)
    finally:
        ta.cleanup()
        subprocess.run(["rm", "-rf", ta.TMP])
    print("%d checks passed, %d failed" % (ta.passed, ta.failed))
    sys.exit(1 if ta.failed else 0)
