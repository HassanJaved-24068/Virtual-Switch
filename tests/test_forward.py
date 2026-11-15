#!/usr/bin/env python3
"""
Part 4 end-to-end tests: flooding, per-port queues, sender threads.

Real ./host programs are used as hosts (their RX lines are read), plus raw
Python sockets when a test needs big or hostile traffic. The switch log is
read from a file (see test_switch.py, whose Switch helper is reused).

Environment: SWITCH (default ./switch), HOST (default ./host).
TSan:  SWITCH="setarch -R ./switch_tsan" python3 tests/test_forward.py
"""
import os, re, shlex, signal, socket, struct, subprocess, sys, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts                     # Switch helper, frame(), mac(), connect()

SWITCH, HOST = ts.SWITCH, ts.HOST
ME = ts.ME
BCAST = ts.BCAST

passed = failed = 0


def check(cond, what):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", what)


ts.check = check            # Switch.stop() also checks the log for sanitizer reports


class H:
    """A real ./host process; its stdout lines are collected by a thread."""

    def __init__(self, sw, mac_str, *opts):
        n = sw.count("host connected")
        self.p = subprocess.Popen(shlex.split(HOST) + ["127.0.0.1", str(sw.port), mac_str] + list(opts),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.lines = []
        self.lock = threading.Lock()
        self.t = threading.Thread(target=self._read, daemon=True)
        self.t.start()
        self.ok = sw.wait_log("host connected", 5, n + 1)
        sw.hosts.append(self.p)

    def _read(self):
        for raw in self.p.stdout:
            with self.lock:
                self.lines.append(raw.decode(errors="replace").rstrip("\n"))

    def rx(self):
        with self.lock:
            return [l for l in self.lines if l.startswith("RX ")]

    def total(self):
        with self.lock:
            for l in self.lines:
                if l.startswith("RX_TOTAL"):
                    return int(l.split()[1])
        return None

    def say(self, text):
        self.p.stdin.write((text + "\n").encode())
        self.p.stdin.flush()

    def wait_rx(self, n, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if len(self.rx()) >= n:
                return True
            time.sleep(0.01)
        return len(self.rx()) >= n

    def quit(self, timeout=20):
        try:
            self.say("quit")
        except Exception:
            pass
        try:
            self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.t.join(5)


def parse_rx(line):
    m = re.match(r"RX src=(\S+) dst=(\S+) vlan=(\d+) prio=(\d+) len=(\d+) data=(.*)$", line)
    return (m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), int(m.group(5)), m.group(6))


def settle(t=0.3):
    time.sleep(t)           # only used to prove that something does NOT arrive


# --------------------------------------------------------------------------
def test_basic_flooding():
    print("forwarding: 4 hosts, every frame reaches the 3 others and never the sender")
    sw = ts.Switch(8)
    hs = [H(sw, ME[i]) for i in range(4)]
    check(all(h.ok for h in hs), "four hosts connected (ports 1-4)")
    want = [0, 0, 0, 0]                       # frames each host should have received so far

    def sent_by(i, how_many, what):
        """Host i sent how_many frames: everybody else must receive them."""
        for j in range(4):
            if j != i:
                want[j] += how_many
        for j in range(4):
            if j != i:
                check(hs[j].wait_rx(want[j]), "host %d received %s from host %d" % (j, what, i))

    # one frame from each host, in turn
    for i, sender in enumerate(hs):
        sender.say("send bcast from-%d" % i)
        sent_by(i, 1, "the frame")
    settle()
    for j, h in enumerate(hs):
        got = [parse_rx(l) for l in h.rx()]
        expect = [(ME[i], BCAST, 1, 1, 6, "from-%d" % i) for i in range(4) if i != j]
        check(got == expect, "host %d got exactly the 3 frames of the others, in order (got %d)" % (j, len(got)))
        check(all(g[0] != ME[j] for g in got), "host %d never got its own frame" % j)

    # a unicast to an address nobody has used is still flooded (no learning yet)
    hs[0].say("send %s to-unknown" % "02:00:00:00:00:99")
    sent_by(0, 1, "the unicast to an unknown MAC")
    # a unicast to host 2's own address also floods in Part 4 (no MAC table yet)
    hs[0].say("send %s to-host2" % ME[2])
    sent_by(0, 1, "the unicast to host 2's MAC (Part 4 floods everything)")
    settle()
    check(len(hs[0].rx()) == 3, "sender host 0 still has only the frames of others")

    # header fields and payload arrive unchanged
    big = "".join(chr(ord("a") + i % 26) for i in range(1500))
    hs[1].say("sendx %s 4094 2 vlan-and-prio" % ME[3])
    hs[1].say("sendx bcast 10 0 ")
    hs[1].say("sendx bcast 258 1 " + big)
    sent_by(1, 3, "the special frames")
    for j in (0, 2, 3):
        got = [parse_rx(l) for l in hs[j].rx()][-3:]
        check(got[0] == (ME[1], ME[3], 4094, 2, 13, "vlan-and-prio"), "host %d: vlan 4094 prio 2 intact" % j)
        check(got[1] == (ME[1], BCAST, 10, 0, 0, ""), "host %d: zero-length frame intact" % j)
        check(got[2] == (ME[1], BCAST, 258, 1, 1500, big), "host %d: 1500-byte payload intact" % j)

    # a burst from one sender: order is kept (the queue is 64 frames, so a burst of 200
    # may lose frames; then a drop was logged, and the ones that arrive are still in order)
    before = [len(h.rx()) for h in hs]
    for k in range(200):
        hs[0].say("send bcast n%03d" % k)
    time.sleep(1.5)
    dropped = "output queue full" in sw.log()
    for j in (1, 2, 3):
        nums = [parse_rx(l)[5] for l in hs[j].rx()[before[j]:]]
        check(nums == sorted(nums) and len(set(nums)) == len(nums), "host %d: burst frames in order, no duplicates" % j)
        if not dropped:
            check(nums == ["n%03d" % k for k in range(200)], "host %d: all 200 frames of the burst (no drop was logged)" % j)
        else:
            check(len(nums) > 0, "host %d: received part of the burst (drops were logged)" % j)
    check(len(hs[0].rx()) == before[0], "the sender of the burst received none of it")

    names = sw.threads()
    check(sorted(t for t in names if t.startswith("rx-port")) == ["rx-port%d" % i for i in range(1, 5)] and
          sorted(t for t in names if t.startswith("tx-port")) == ["tx-port%d" % i for i in range(1, 5)],
          "ps -T: one receiver and one sender per host: %s" % sorted(names))
    for h in hs:
        h.quit()
    check(sw.stop(), "switch alive at the end")


def test_concurrent_senders():
    print("forwarding: all 4 hosts send 300 frames at the same time")
    sw = ts.Switch(8)
    hs = [H(sw, ME[i]) for i in range(4)]
    for k in range(300):
        for h in hs:
            h.say("send bcast x%d" % k)
    time.sleep(1.5)
    log = sw.log()
    dropped = "output queue full" in log
    counts = [len(h.rx()) for h in hs]
    if not dropped:
        check(counts == [900] * 4, "no queue ever filled: every host received exactly 900 frames (got %s)" % counts)
    else:
        check(all(0 < c <= 900 for c in counts),
              "some drops were logged; every host still received many frames (got %s)" % counts)
        print("  note: queue-full drops happened; counts =", counts)
    check(all(parse_rx(l)[0] != ME[i] for i, h in enumerate(hs) for l in h.rx()), "nobody got its own frame")
    for h in hs:
        h.quit()
    check(sw.stop(), "switch alive at the end")


def flood_frames(src, n, payload):
    one = ts.frame(src, BCAST, 1, 1, payload)
    return one * n


def last_drop(log, port):
    best = 0
    for m in re.finditer(rb"port %d: output queue full, (\d+) frames dropped" % port, log):
        best = max(best, int(m.group(1)))
    return best


def run_hol(with_c, n, payload, nports=4):
    """Hosts B (port 1) and D (port 2) read quietly, C (port 3) never reads (optional),
    then A floods n frames. Returns (seconds A's flood took, B total, D total, C drops)."""
    sw = ts.Switch(nports)
    b = H(sw, ME[1], "--quiet")
    d = H(sw, ME[3], "--quiet")
    c = H(sw, ME[2], "--noread") if with_c else None
    a_port = 4 if with_c else 3
    start = time.time()
    if payload is None:
        a = H(sw, ME[0], "--flood", str(n), "--to", "bcast")       # the real host's flood
        a_sock = None
    else:
        a_sock = ts.connect(sw)
        sw.wait_log("port %d: host connected" % a_port)
        start = time.time()
        block = flood_frames(ME[0], 1000, payload)
        for _ in range(n // 1000):
            a_sock.sendall(block)
    marker = ("port %d: RX " % a_port).encode()
    end = time.time() + 240
    done = None
    while time.time() < end:
        with open(sw.logf.name, "rb") as f:
            cnt = f.read().count(marker)
        if cnt >= n:
            done = time.time() - start
            break
        time.sleep(0.2)
    time.sleep(1.5)                                                # let the queues empty
    log = open(sw.logf.name, "rb").read()
    drops_c = last_drop(log, 3) if with_c else 0
    drops_bd = last_drop(log, 1), last_drop(log, 2)
    b.quit()
    d.quit()
    if a_sock:
        a_sock.close()
    alive = sw.p.poll() is None
    if c:
        c.quit()
    sw.stop()
    return done, b.total(), d.total(), drops_c, drops_bd, alive


def test_head_of_line():
    N = 100000
    print("head-of-line blocking: host C never reads, host A floods %d frames (real host --flood)" % N)
    t0, b0, d0, _, _, alive0 = run_hol(False, N, None)
    t1, b1, d1, dc, dbd, alive1 = run_hol(True, N, None)
    print("  without C: %.2fs  B=%s D=%s | with C: %.2fs  B=%s D=%s  C-drops=%s  B/D-drops=%s" %
          (t0 or -1, b0, d0, t1 or -1, b1, d1, dc, dbd))
    check(t0 is not None and t1 is not None, "A's receiver processed all %d frames in both runs" % N)
    if t0 and t1:
        check(t1 <= t0 * 1.5 + 1.0, "with the frozen host the flood takes about as long (%.2fs vs %.2fs)" % (t1, t0))
    check(alive0 and alive1, "switch stayed up")
    # A bounded queue drops frames when the receiver is faster than a host's reader, with or
    # without a frozen host. What matters is that C does not make it worse for B and D.
    check(dc >= 1000, "C's drop counter grew (last logged value: %d)" % dc)
    for name, tot in (("B", b1), ("D", d1)):
        check(tot is not None and tot >= N * 0.5, "%s kept receiving: %s of %d frames" % (name, tot, N))
    if None not in (b0, b1, d0, d1):
        check(b1 >= b0 * 0.9 and d1 >= d0 * 0.9,
              "B and D receive about as much with the frozen host as without it (%s/%s vs %s/%s)" % (b1, d1, b0, d0))

    P = 1000
    print("head-of-line blocking: same with %d-byte payloads (so C's socket buffers really fill)" % P)
    t0, b0, d0, _, _, alive0 = run_hol(False, N, b"x" * P)
    t1, b1, d1, dc, dbd, alive1 = run_hol(True, N, b"x" * P)
    print("  without C: %.2fs  B=%s D=%s | with C: %.2fs  B=%s D=%s  C-drops=%s  B/D-drops=%s" %
          (t0 or -1, b0, d0, t1 or -1, b1, d1, dc, dbd))
    check(t0 is not None and t1 is not None, "A's receiver processed all %d frames in both runs" % N)
    if t0 and t1:
        check(t1 <= t0 * 1.5 + 1.0, "flood time with C %.2fs vs without %.2fs" % (t1, t0))
    check(alive0 and alive1, "switch stayed up")
    check(dc >= 1000, "C's drop counter grew (last logged value: %d)" % dc)
    check(b1 is not None and b1 >= N * 0.5 and d1 is not None and d1 >= N * 0.5,
          "B and D still received most frames (%s, %s of %d)" % (b1, d1, N))
    if b0 is not None and b1 is not None and d0 is not None and d1 is not None:
        check(b1 >= b0 * 0.8 and d1 >= d0 * 0.8,
              "B and D got about as many frames as in the run without C (%s/%s vs %s/%s)" % (b1, d1, b0, d0))


def thread_names(sw):
    return sw.threads()


def test_dead_and_blocked_hosts():
    print("senders: a host that stops reading, then dies / misbehaves")
    sw = ts.Switch(8)
    b = H(sw, ME[1], "--quiet")
    # C is a raw socket that never reads
    c = ts.connect(sw)
    sw.wait_log("port 2: host connected")
    a = ts.connect(sw)
    sw.wait_log("port 3: host connected")
    block = flood_frames(ME[0], 200, b"y" * 1000)
    for _ in range(40):                                  # 8000 frames x ~1 KB
        a.sendall(block)
    check(sw.wait_log("port 2: output queue full", 20), "C's queue filled and drops were logged")
    names = thread_names(sw)
    check("tx-port2" in names and "rx-port2" in names, "C's sender (stuck in write) and receiver are running")
    fds_before = len(os.listdir("/proc/%d/fd" % sw.p.pid))

    # C now sends an invalid frame: its receiver ends the connection. The sender is
    # stuck in write() to the frozen socket and must be woken by shutdown().
    c.sendall(ts.frame(ME[2], BCAST, 1, 1, b"", length=1501))
    check(sw.wait_log("port 2: protocol violation: length 1501 > 1500"), "protocol violation on port 2 logged")
    end = time.time() + 10
    while time.time() < end and any(t in thread_names(sw) for t in ("tx-port2", "rx-port2")):
        time.sleep(0.05)
    names = thread_names(sw)
    check("tx-port2" not in names and "rx-port2" not in names,
          "the stuck sender and the receiver of port 2 have both ended: %s" % sorted(names))
    check(sw.p.poll() is None, "switch alive")
    nfd = len(os.listdir("/proc/%d/fd" % sw.p.pid))
    check(nfd == fds_before - 1, "the dead host's descriptor was closed (%d open, %d before)" % (nfd, fds_before))
    c.close()

    # the others keep working while frames are still being flooded to the dead port
    for _ in range(10):
        a.sendall(block)
    a.sendall(ts.frame(ME[0], BCAST, 1, 1, b"after-the-death"))
    time.sleep(1.0)
    b.quit()
    check(b.total() is not None and b.total() >= 2000, "B kept receiving (%s frames)" % b.total())
    check(sw.p.poll() is None, "switch alive after flooding a dead port")
    a.close()
    sw.stop()


def test_killed_host_mid_flood():
    print("senders: a reading host is killed (kill -9) while frames flow to it")
    sw = ts.Switch(8)
    b = H(sw, ME[1], "--quiet")
    c = H(sw, ME[2])                                      # prints every frame: reads slowly
    a = ts.connect(sw)
    sw.wait_log("port 3: host connected")
    block = flood_frames(ME[0], 100, b"z" * 500)
    for _ in range(20):
        a.sendall(block)
    c.p.send_signal(signal.SIGKILL)
    c.p.wait(5)
    for _ in range(40):
        a.sendall(block)
    check(sw.wait_log("port 2: ", 5, 1), "the switch noticed the dead host")
    end = time.time() + 10
    while time.time() < end and any(t in thread_names(sw) for t in ("tx-port2", "rx-port2")):
        time.sleep(0.05)
    check("tx-port2" not in thread_names(sw) and "rx-port2" not in thread_names(sw),
          "sender and receiver of the killed host ended")
    check(sw.p.poll() is None, "switch alive")
    time.sleep(0.5)
    b.quit()
    check(b.total() is not None and b.total() >= 2000, "B received %s frames" % b.total())
    a.close()
    sw.stop()


def test_dropped_frames_are_counted_and_released():
    print("queues: a full queue drops frames without blocking the receiver")
    sw = ts.Switch(4)
    c = ts.connect(sw)                                    # never reads
    sw.wait_log("port 1: host connected")
    a = ts.connect(sw)
    sw.wait_log("port 2: host connected")
    block = flood_frames(ME[0], 500, b"q" * 1400)
    t0 = time.time()
    for _ in range(20):                                   # 10,000 frames = 14 MB
        a.sendall(block)
    check(sw.wait_log("port 2: RX ", 60, 10000), "A's receiver read all 10000 frames (it never waited for C)")
    el = time.time() - t0
    check(el < 30, "in %.1fs" % el)
    log = sw.log().encode()
    d = last_drop(log, 1)
    check(d >= 1000, "port 1's drop counter reached %d" % d)
    check(len(re.findall(rb"port 1: output queue full, 1 frames dropped", log)) == 1,
          "the first drop was logged exactly once")
    check(sw.count("port 2: output queue full") == 0, "A's own queue never overflowed (nobody sends to A's port)")
    a.close()
    c.close()
    sw.stop()


if __name__ == "__main__":
    if not os.path.exists(shlex.split(SWITCH)[-1]) or not os.path.exists(shlex.split(HOST)[-1]):
        sys.exit("switch or host binary not found")
    for t in [test_basic_flooding, test_concurrent_senders, test_dropped_frames_are_counted_and_released,
              test_dead_and_blocked_hosts, test_killed_host_mid_flood, test_head_of_line]:
        t()
    print("\n%d checks, %d failures" % (passed + failed, failed))
    print("ALL PASSED" if failed == 0 else "SOME FAILED")
    sys.exit(0 if failed == 0 else 1)
