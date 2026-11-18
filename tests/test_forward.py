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
    # every host has sent a frame, so every MAC is learned: a unicast to host 2 goes to host 2 ONLY
    hs[0].say("send %s to-host2" % ME[2])
    want[2] += 1
    check(hs[2].wait_rx(want[2]), "the unicast to host 2's learned MAC reached host 2")
    settle()
    check(len(hs[1].rx()) == want[1] and len(hs[3].rx()) == want[3],
          "and was NOT flooded to hosts 1 and 3 (Part 6: unicast forwarding)")
    check(len(hs[0].rx()) == 3, "sender host 0 still has only the frames of others")

    # header fields and payload arrive unchanged
    big = "".join(chr(ord("a") + i % 26) for i in range(1500))
    hs[1].say("sendx %s 4094 2 vlan-and-prio" % ME[3])
    hs[1].say("sendx bcast 10 0 ")
    hs[1].say("sendx bcast 258 1 " + big)
    # Part 13: the first frame is written with vlan 4094 but rewritten to the port's VLAN 1, where host 3's MAC is already learned:
    # it is a unicast to host 3 only (before Part 13 the lookup in VLAN 4094 failed and it was flooded). The two broadcasts reach all.
    sent_by(1, 2, "the special broadcasts")
    want[3] += 1
    check(hs[3].wait_rx(want[3]), "host 3 received the special unicast from host 1")
    # These three frames have THREE DIFFERENT PRIORITIES (2, 0, 1). Since Part 11 the sender of a port takes high before
    # normal before low: when the three are queued together (a busy machine, a slow sender) they leave in that order,
    # not in the order they were sent. Each frame must arrive exactly once and intact (all fields); the order between
    # different priorities is the scheduler's choice and is not asserted here (tests/test_qos.c and test_qos.py do).
    # Part 13: the VLAN of a frame from an access port is the port's VLAN (1 here), whatever the host wrote (4094, 10, 258 are
    # sent, VLAN 1 must arrive); addresses, priority, length and payload still arrive exactly as sent.
    special = (("vlan 4094 -> 1, prio 2", (ME[1], ME[3], 1, 2, 13, "vlan-and-prio")),
               ("zero-length frame (sent vlan 10)", (ME[1], BCAST, 1, 0, 0, "")),
               ("1500-byte payload (sent vlan 258)", (ME[1], BCAST, 1, 1, 1500, big)))
    for j in (0, 2, 3):
        mine = special if j == 3 else special[1:]                 # hosts 0 and 2 do not get the unicast for host 3
        got = [parse_rx(l) for l in hs[j].rx()][-len(mine):]
        for name, frame in mine:
            check(got.count(frame) == 1, "host %d: %s intact" % (j, name))
        check(sorted(got) == sorted(f for _, f in mine), "host %d: the last %d frames are exactly these (nothing else, none missing)" % (j, len(mine)))
        if j != 3:
            check(not any(f == special[0][1] for f in [parse_rx(l) for l in hs[j].rx()]), "host %d did not get the unicast meant for host 3" % j)

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


def processed(text, marker):
    """Frames the switch has handled: their per-frame log lines (marker) plus the lines the logger reports as dropped.
    (Part 9: under a flood the logger drops per-frame lines on purpose and counts them.)"""
    return text.count(marker) + sum(int(x) for x in re.findall(rb"(\d+) log messages dropped", text))


def last_drop(log, port):
    best = 0
    for m in re.finditer(rb"port %d: output queue full, (\d+) frames dropped" % port, log):
        best = max(best, int(m.group(1)))
    return best


_make_switch_lock = threading.Lock()


def run_hol(with_c, n, payload, nports=4):
    """Hosts B (port 1) and D (port 2) read quietly, C (port 3) never reads (optional),
    then A floods n frames. Returns (seconds A's flood took, B total, D total, C drops, B/D drops, alive, switch):
    the switch is NOT stopped here (the caller stops it, in the main thread)."""
    with _make_switch_lock:                                        # two switches may be started at the same moment
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
            text = f.read()
        if processed(text, marker) >= n:
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
    return done, b.total(), d.total(), drops_c, drops_bd, alive, sw


def rate_ok(totals0, t0, totals1, t1, frac=0.25):
    """B and D receive at 'full speed' with the frozen host if their COMBINED RATE (frames per second) is at least `frac`
    of the rate in the run without it. Why rates and why combined:
      - RATES, not counts: the run with the frozen host has less to do, so A's flood is faster (3.0 s against 5.5 s were
        measured) and B's and D's own bounded queues overflow more: they receive fewer FRAMES in total although nothing
        blocks them.
      - COMBINED (B + D), not one by one: on a busy 2-core machine the host process of ONE of them can be starved of the
        CPU for seconds (identical hosts in the same run received 8,342 and 29,964 frames), which says nothing about the
        switch.
      - a lenient bound (0.25; the measured combined ratios under load were 0.5 and more): a real head-of-line blocking
        does not slow the fast hosts down a little, it stops them (a few percent) or stalls A's whole flood (the time check).
    (1.5 s: the drain time that run_hol waits after the flood.)"""
    return sum(totals1) / (t1 + 1.5) >= frac * sum(totals0) / (t0 + 1.5)


def run_hol_pair(n, payload):
    """The baseline (no C) and the frozen-C scenario run AT THE SAME TIME, each on its own switch. They compete for the
    same CPUs, so a busy machine slows both equally; run one after the other (as before) the second run could meet
    a different load and its counts differed by a factor of 2-3 (B/D received 13,000 instead of 32,000 frames with
    4 busy loops running). -> (results without C, results with C), each without the switch."""
    out = {}

    def one(with_c):
        try:
            out[with_c] = run_hol(with_c, n, payload)
        except Exception as e:                                     # reported by the caller below
            out[with_c] = e
    ths = [threading.Thread(target=one, args=(w,), daemon=True) for w in (False, True)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(600)
    for w in (False, True):
        if w not in out or isinstance(out[w], Exception):
            raise RuntimeError("head-of-line run (with_c=%s) failed: %r" % (w, out.get(w)))
        out[w][-1].stop()                                          # in the main thread: check() is not thread safe
    return out[False][:-1], out[True][:-1]


def test_head_of_line():
    N = 100000
    print("head-of-line blocking: host C never reads, host A floods %d frames (real host --flood); both runs at the same time" % N)
    (t0, b0, d0, _, _, alive0), (t1, b1, d1, dc, dbd, alive1) = run_hol_pair(N, None)
    print("  without C: %.2fs  B=%s D=%s | with C: %.2fs  B=%s D=%s  C-drops=%s  B/D-drops=%s" %
          (t0 or -1, b0, d0, t1 or -1, b1, d1, dc, dbd))
    check(t0 is not None and t1 is not None, "A's receiver processed all %d frames in both runs" % N)
    if t0 and t1:
        # Run-to-run noise on a 2-core VM is up to 2x (the same flood took 3.3-6.3 s in repeated runs
        # of the same binary). A receiver that really stalled would not finish at all (240 s timeout).
        check(t1 <= t0 * 2.5 + 2.0, "with the frozen host the flood takes about as long (%.2fs vs %.2fs)" % (t1, t0))
    check(alive0 and alive1, "switch stayed up")
    # A bounded queue drops frames when the receiver is faster than a host's reader, with or
    # without a frozen host. What matters is that C does not make it worse for B and D.
    check(dc >= 1000, "C's drop counter grew (last logged value: %d)" % dc)
    # Since Part 9 A's receiver is no longer slowed down by synchronous logging, so the fast hosts' own bounded queues
    # overflow more (B and D get 8-75 % of the frames depending on the load of the machine): an absolute share of N
    # measures the speed of the machine, not head-of-line blocking (a share of 10 % failed on a busy machine, 8.2 %).
    # The time check above and the comparison with the run without C (both runs at the same time, so with the same load)
    # are the head-of-line tests; a real HOL blocking would stall A's whole flood and fail the time check. Here only a
    # sanity floor: the fast hosts did receive a meaningful number of frames (1 %).
    for name, tot in (("B", b1), ("D", d1)):
        check(tot is not None and tot >= N * 0.01, "%s kept receiving: %s of %d frames (sanity floor 1 %%)" % (name, tot, N))
    if None not in (b0, b1, d0, d1) and t0 and t1:
        check(rate_ok((b0, d0), t0, (b1, d1), t1),
              "B and D together receive at a comparable RATE with the frozen host as without it (%s+%s in %.1fs vs %s+%s in %.1fs)" %
              (b1, d1, t1, b0, d0, t0))

    P = 1000
    print("head-of-line blocking: same with %d-byte payloads (so C's socket buffers really fill)" % P)
    (t0, b0, d0, _, _, alive0), (t1, b1, d1, dc, dbd, alive1) = run_hol_pair(N, b"x" * P)
    print("  without C: %.2fs  B=%s D=%s | with C: %.2fs  B=%s D=%s  C-drops=%s  B/D-drops=%s" %
          (t0 or -1, b0, d0, t1 or -1, b1, d1, dc, dbd))
    check(t0 is not None and t1 is not None, "A's receiver processed all %d frames in both runs" % N)
    if t0 and t1:
        check(t1 <= t0 * 2.5 + 2.0, "flood time with C %.2fs vs without %.2fs" % (t1, t0))
    check(alive0 and alive1, "switch stayed up")
    check(dc >= 1000, "C's drop counter grew (last logged value: %d)" % dc)
    check(b1 is not None and b1 >= N * 0.01 and d1 is not None and d1 >= N * 0.01,
          "B and D still received frames (%s, %s of %d; sanity floor 1 %%, the comparison below is the real check)" % (b1, d1, N))
    if None not in (b0, b1, d0, d1) and t0 and t1:
        check(rate_ok((b0, d0), t0, (b1, d1), t1),
              "B and D together receive at a comparable RATE as in the run without C (%s+%s in %.1fs vs %s+%s in %.1fs)" %
              (b1, d1, t1, b0, d0, t0))


def thread_names(sw):
    return sw.threads()


def test_dead_and_blocked_hosts():
    print("senders: a host that stops reading, then dies / misbehaves")
    sw = ts.Switch(8)
    b = H(sw, ME[1])                 # prints every frame: the marker frame below must be seen (Part 11 test, was --quiet)
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
    before = len(b.rx())
    for _ in range(10):
        a.sendall(block)
    # A marker frame in the HIGH priority queue (Part 11). B's normal queue is full of flood frames, which would drop a
    # normal frame; the high queue is empty. The marker proves that B is still served after C died, whatever the speed
    # of the machine; a fixed number of received frames (was >= 2000) only measured how fast the machine was.
    a.sendall(ts.frame(ME[0], BCAST, 1, 0, b"after-the-death"))
    end = time.time() + 30
    while time.time() < end and not any("data=after-the-death" in l for l in b.rx()):
        time.sleep(0.05)
    check(any("data=after-the-death" in l for l in b.rx()), "B kept receiving: the marker frame sent after C died arrived")
    check(len(b.rx()) > before, "B kept receiving: %d frames before the flood after C's death, %d after" % (before, len(b.rx())))
    b.quit()
    check(sw.p.poll() is None, "switch alive after flooding a dead port")
    a.close()
    sw.stop()


def test_killed_host_mid_flood():
    print("senders: a reading host is killed (kill -9) while frames flow to it")
    sw = ts.Switch(8)
    b = H(sw, ME[1])
    c = H(sw, ME[2])                                      # prints every frame: reads slowly
    a = ts.connect(sw)
    sw.wait_log("port 3: host connected")
    b.say("send bcast b-hello")
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
    # B must still be served after C died: a marker frame sent now reaches it. (A count of frames would depend on
    # the speed of the switch: since Part 9 the receiver is faster, and B's bounded queue drops more.)
    time.sleep(0.5)
    a.sendall(ts.frame(ME[0], BCAST, 1, 1, b"after-the-death"))
    end = time.time() + 15
    while time.time() < end and not any("data=after-the-death" in l for l in b.rx()):
        time.sleep(0.05)
    check(any("data=after-the-death" in l for l in b.rx()), "B still receives after C was killed")
    b.quit()
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
    end = time.time() + 60
    while time.time() < end and processed(open(sw.logf.name, "rb").read(), b"port 2: RX ") < 10000:
        time.sleep(0.1)
    check(processed(open(sw.logf.name, "rb").read(), b"port 2: RX ") >= 10000,
          "A's receiver read all 10000 frames (it never waited for C)")
    el = time.time() - t0
    check(el < 30, "in %.1fs" % el)
    log = sw.log().encode()
    d = last_drop(log, 1)
    check(d >= 1000, "port 1's drop counter reached %d" % d)
    # at most once: under a flood the logger may itself drop that one warning line (Part 9), but never repeat it
    check(len(re.findall(rb"port 1: output queue full, 1 frames dropped", log)) <= 1,
          "the first drop was not logged twice")
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
