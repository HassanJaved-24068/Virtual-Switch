#!/usr/bin/env python3
"""
Part 12 end-to-end tests: rate limiting (token bucket + token timer thread) with the real ./switch.

  python3 tests/test_rate.py            normal build
  python3 tests/test_rate.py tsan       SWITCH="setarch -R ./switch_tsan"
  python3 tests/test_rate.py valgrind   valgrind on ./switch (no errors, no leaks, no descriptor left open)

The token bucket itself (burst, cap, wake-ups, rate changes) is tested deterministically in tests/test_rate.c.
Here the whole path is measured: host A floods frames towards host B (port 2, rate limited) and host C
(port 3); B and C read everything and note the arrival time of every frame. The number that B received in a
time window is compared with what the limit allows:  at most rate * T + rate (the burst of one second) and,
because the sender of B is always waiting for a token while A floods, nearly rate * T.
Nothing here depends on how fast the machine is: the limits are in frames per second of real time, and the
unlimited host is only compared with the limited one (ratio), never with a fixed count.
"""
import os, re, select, shlex, signal, socket, struct, subprocess, sys, tempfile, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts

ME, BCAST = ts.ME, ts.BCAST
passed = failed = 0


def check(cond, what):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", what)


ts.check = check

TMP = tempfile.mkdtemp(prefix="vswrate.")
conf_no = 0
SLOW = False                 # tsan / valgrind: the machine is slower, the lower bounds are looser
CLK_TCK = os.sysconf("SC_CLK_TCK")


def conf(text):
    global conf_no
    conf_no += 1
    return write_conf(os.path.join(TMP, "r%03d.conf" % conf_no), text)


def write_conf(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


class Counter(threading.Thread):
    """Reads a raw socket all the time. Remembers (arrival time, first payload byte) of every frame."""

    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock = sock
        self.events = []
        self.stop = False

    def run(self):
        buf = b""
        # select() instead of a socket timeout: the socket is shared with the Flood thread of host A, whose
        # sendall() must be allowed to block for as long as a slow switch (valgrind) needs.
        while not self.stop:
            try:
                if not select.select([self.sock], [], [], 0.5)[0]:
                    continue
                d = self.sock.recv(1 << 20)
            except (OSError, ValueError):
                return
            if not d:
                return
            now = time.monotonic()
            buf += d
            pos = 0
            while len(buf) - pos >= 17:
                ln = struct.unpack("!H", buf[pos:pos + 2])[0]
                if len(buf) - pos < 17 + ln:
                    break
                self.events.append((now, buf[pos + 17:pos + 18]))
                pos += 17 + ln
            buf = buf[pos:]

    def count(self, tag, t0=0.0, t1=1e18):
        return sum(1 for (t, c) in list(self.events) if c == tag and t0 <= t <= t1)


class Flood(threading.Thread):
    """Host A: sends unicast frames to B (and to C if wanted) as fast as the switch takes them, for `seconds`.
    Records the longest time one sendall() took: the receiver of the switch must never be blocked by a throttle."""

    def __init__(self, sock, seconds, to_c=False):
        super().__init__(daemon=True)
        self.sock = sock
        self.seconds = seconds
        self.blk_b = ts.frame(ME[3], ME[4], 1, 1, b"R" + b"x" * 99) * 20
        self.blk_c = ts.frame(ME[3], ME[5], 1, 1, b"R" + b"x" * 99) * 20
        self.to_c = to_c
        self.sent = 0
        self.max_block = 0.0
        self.t0 = self.t1 = 0.0

    def run(self):
        self.t0 = time.monotonic()
        end = self.t0 + self.seconds
        while True:
            now = time.monotonic()
            if now >= end:
                break
            for blk in ((self.blk_b, self.blk_c) if self.to_c else (self.blk_b,)):
                a = time.monotonic()
                self.sock.sendall(blk)
                self.max_block = max(self.max_block, time.monotonic() - a)
                self.sent += 20
        self.t1 = time.monotonic()


def scenario(sw):
    """A = port 1 (sender), B = port 2, C = port 3. B and C say hello (broadcast), so their MACs are known."""
    a = ts.connect(sw)
    sw.wait_log("port 1: host connected", 10)
    b = ts.connect(sw)
    sw.wait_log("port 2: host connected", 10)
    c = ts.connect(sw)
    sw.wait_log("port 3: host connected", 10)
    ca, cb, cc = Counter(a), Counter(b), Counter(c)
    for t in (ca, cb, cc):
        t.start()
    b.sendall(ts.frame(ME[4], BCAST, 1, 1, b"H-from-B"))
    c.sendall(ts.frame(ME[5], BCAST, 1, 1, b"H-from-C"))
    end = time.time() + 20
    while time.time() < end and ca.count(b"H") < 2:       # A got both hellos: the switch has learned both MACs
        time.sleep(0.02)
    check(ca.count(b"H") == 2, "hello frames of B and C arrived at A (MACs learned)")
    return (a, b, c), (ca, cb, cc)


def finish(socks, counters):
    for t in counters:
        t.stop = True
    for s in socks:
        s.close()


def bounds(rate, T):
    """What B may have received in a window of T seconds: the burst (rate) + rate * T, never more; and nearly
    rate * T (the queue of B never runs dry while A floods)."""
    low = rate * T * (0.4 if SLOW else 0.75)
    high = rate * T + rate + max(3, rate * 0.2)
    return low, high


def thread_cpu_s(pid, name):
    """CPU seconds used so far by the thread of `pid` called `name` (from /proc), or None."""
    for tid in os.listdir("/proc/%d/task" % pid):
        try:
            with open("/proc/%d/task/%s/comm" % (pid, tid)) as f:
                if f.read().strip() != name:
                    continue
            with open("/proc/%d/task/%s/stat" % (pid, tid)) as f:
                rest = f.read().rsplit(")", 1)[1].split()
            return (int(rest[11]) + int(rest[12])) / CLK_TCK      # utime + stime
        except (OSError, IndexError, ValueError):
            continue
    return None


def measure(sw, counters, seconds, to_c=False, a_sock=None):
    """Flood from A for `seconds`; returns (flood, count of B, count of C) in the flood window."""
    fl = Flood(a_sock, seconds, to_c)
    fl.start()
    fl.join(seconds + 60)
    check(not fl.is_alive(), "the flooding host A did not get stuck")
    t0, t1 = fl.t0, fl.t1
    nb = counters[1].count(b"R", t0, t1)
    nc = counters[2].count(b"R", t0, t1)
    return fl, nb, nc


# ----------------------------------------------------------------------------------------------
def test_port_rate():
    print("1. port.2.rate = 50: B receives about rate * T frames, C and the receiver of the switch are not affected")
    sw = ts.Switch(3, extra=["-c", conf("port.2.rate = 50\n")])
    (a, b, c), cnt = scenario(sw)
    check("port 2: rate limit 50 frames/s" in sw.log(), "the log says that port 2 is limited to 50 frames/s")
    check("port 3: rate limit" not in sw.log() and "port 1: rate limit" not in sw.log(), "the other ports are not limited")
    names = sw.threads()
    check(names.count("token-timer") == 1, "ps -T shows exactly one token timer thread: %s" % names)

    T = 5.0
    cpu0 = thread_cpu_s(sw.p.pid, "tx-port2")
    tm0 = thread_cpu_s(sw.p.pid, "token-timer")
    fl, nb, nc = measure(sw, cnt, T, to_c=True, a_sock=a)
    T = fl.t1 - fl.t0              # the real length of the window (a slow switch makes the last sendall last)
    cpu1 = thread_cpu_s(sw.p.pid, "tx-port2")
    tm1 = thread_cpu_s(sw.p.pid, "token-timer")
    low, high = bounds(50, T)
    print("    A sent %d frames; B (limited) got %d, expected %.0f..%.0f; C (unlimited) got %d" % (fl.sent, nb, low, high, nc))
    check(low <= nb <= high, "B received %d frames in %.0f s: between %.0f and %.0f" % (nb, T, low, high))
    check(nc > 4 * nb, "C (not limited) got far more than B: %d against %d" % (nc, nb))
    check(fl.max_block < 5.0, "A's sendall never stalled (longest %.2f s): the receiver is never blocked by the throttle" % fl.max_block)
    check("port 2: output queue full" in sw.log(), "B's queue overflowed and the frames were dropped (logged)")
    if not SLOW and cpu0 is not None and cpu1 is not None:
        used = cpu1 - cpu0
        check(used < 0.05 * T, "the sender thread of the throttled port used %.3f s of CPU in %.0f s: it sleeps, it does not spin" % (used, T))
        check(tm1 is not None and tm0 is not None and tm1 - tm0 < 0.05 * T,
              "the token timer thread used %.3f s of CPU in %.0f s" % ((tm1 or 0) - (tm0 or 0), T))
    finish((a, b, c), cnt)
    sw.stop()


def test_global_rate_and_override():
    print("2. rate_limit = 40 for every port, port.3.rate = 0 overrides it (unlimited)")
    sw = ts.Switch(3, extra=["-c", conf("rate_limit = 40\nport.3.rate = 0\n")])
    (a, b, c), cnt = scenario(sw)
    log = sw.log()
    check("port 1: rate limit 40 frames/s" in log and "port 2: rate limit 40 frames/s" in log, "ports 1 and 2 use rate_limit: %s" % [l for l in log.splitlines() if "rate limit" in l])
    check("port 3: rate limit" not in log, "port 3: its own rate 0 means unlimited")
    T = 4.0
    fl, nb, nc = measure(sw, cnt, T, to_c=True, a_sock=a)
    T = fl.t1 - fl.t0              # the real length of the window (a slow switch makes the last sendall last)
    low, high = bounds(40, T)
    print("    B got %d (limit 40/s, expected %.0f..%.0f), C got %d" % (nb, low, high, nc))
    check(low <= nb <= high, "B (inherits rate_limit) received %d in %.0f s: between %.0f and %.0f" % (nb, T, low, high))
    check(nc > 4 * nb, "C (port.3.rate = 0) is not limited: %d against %d" % (nc, nb))
    finish((a, b, c), cnt)
    sw.stop()


def test_reload():
    print("3. SIGHUP changes the rate at once: unlimited -> 30 -> unlimited; an unrelated reload keeps the tokens")
    path = conf("mac_aging_seconds = 300\n")
    sw = ts.Switch(3, extra=["-c", path])
    (a, b, c), cnt = scenario(sw)
    T = 2.0
    fl, nb0, _ = measure(sw, cnt, T, a_sock=a)
    T = fl.t1 - fl.t0              # the real length of the window (a slow switch makes the last sendall last)
    check("rate limit" not in sw.log(), "no rate limit before the reload")

    n = sw.count("configuration reloaded from")
    write_conf(path, "port.2.rate = 30\n")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded from", 30, n + 1), "reload 1 done")
    check("reload: port 2 rate limit 0 -> 30 frames/s" in sw.log(), "the reload log names port 2: 0 -> 30")
    T = 4.0
    fl, nb1, _ = measure(sw, cnt, T, a_sock=a)
    T = fl.t1 - fl.t0              # the real length of the window (a slow switch makes the last sendall last)
    low, high = bounds(30, T)
    print("    before: %d frames in 2 s; limited to 30/s: %d in %.0f s (expected %.0f..%.0f)" % (nb0, nb1, T, low, high))
    check(low <= nb1 <= high, "after the reload B received %d in %.0f s: between %.0f and %.0f" % (nb1, T, low, high))
    check(nb0 / 2.0 > 4 * (nb1 / T), "unlimited it was much faster than 30/s (%.0f/s against %.0f/s)" % (nb0 / 2.0, nb1 / T))

    lines = sw.log().count("rate limit")
    n = sw.count("configuration reloaded from")
    write_conf(path, "port.2.rate = 30\nmac_aging_seconds = 250\n")           # nothing about rates changes
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded from", 30, n + 1), "reload 2 done")
    check(sw.log().count("rate limit") == lines, "a reload that does not change a rate does not touch the buckets (no 'rate limit' line)")

    n = sw.count("configuration reloaded from")
    write_conf(path, "port.2.rate = -1\n")                                      # invalid: nothing changes
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("reload failed", 30), "an invalid rate is refused by the reload")
    fl, nb2, _ = measure(sw, cnt, 3.0, a_sock=a)
    T = fl.t1 - fl.t0              # the real length of the window (a slow switch makes the last sendall last)
    low, high = bounds(30, T)
    check(low <= nb2 <= high, "the previous rate (30/s) is still in force: %d frames in 3 s (%.0f..%.0f)" % (nb2, low, high))

    n = sw.count("configuration reloaded from")
    write_conf(path, "mac_aging_seconds = 300\n")                               # no rate at all: back to unlimited
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded from", 30, n + 1), "reload 3 done")
    check("reload: port 2 rate limit 30 -> 0 frames/s" in sw.log(), "the reload log names port 2: 30 -> 0")
    fl, nb3, _ = measure(sw, cnt, 2.0, a_sock=a)
    T = fl.t1 - fl.t0              # the real length of the window (a slow switch makes the last sendall last)
    check(nb3 / 2.0 > 4 * 30, "unlimited again: %.0f frames/s received (limit was 30)" % (nb3 / 2.0))
    finish((a, b, c), cnt)
    sw.stop()


def test_reload_global():
    print("4. reload of rate_limit applies to every port without its own rate and leaves the others alone")
    path = conf("port.3.rate = 500\n")
    sw = ts.Switch(3, extra=["-c", path])
    (a, b, c), cnt = scenario(sw)
    n = sw.count("configuration reloaded from")
    write_conf(path, "rate_limit = 25\nport.3.rate = 500\n")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded from", 30, n + 1), "reload done")
    log = sw.log()
    check("reload: rate_limit 0 -> 25" in log, "rate_limit change logged")
    check("port 1 rate limit 0 -> 25" in log and "port 2 rate limit 0 -> 25" in log, "ports 1 and 2 follow rate_limit")
    check("port 3 rate limit" not in log, "port 3 has its own rate: no change")
    T = 4.0
    fl, nb, nc = measure(sw, cnt, T, to_c=True, a_sock=a)
    T = fl.t1 - fl.t0              # the real length of the window (a slow switch makes the last sendall last)
    low, high = bounds(25, T)
    check(low <= nb <= high, "B follows rate_limit = 25: %d in %.0f s (%.0f..%.0f)" % (nb, T, low, high))
    check(nc <= 500 * T + 500 + 100, "C stays within its own rate (500/s): %d frames in %.0f s" % (nc, T))
    finish((a, b, c), cnt)
    sw.stop()


def test_edges():
    print("5. edge values: rate 1 (a burst of one frame), a huge rate, a new host on a limited port")
    sw = ts.Switch(3, extra=["-c", conf("port.2.rate = 1\nport.3.rate = 1000000\n")])
    (a, b, c), cnt = scenario(sw)
    T = 4.0
    fl, nb, nc = measure(sw, cnt, T, to_c=True, a_sock=a)
    T = fl.t1 - fl.t0              # the real length of the window (a slow switch makes the last sendall last)
    print("    rate 1: B got %d frames in %.0f s; rate 1000000: C got %d" % (nb, T, nc))
    check(1 <= nb <= T + 3, "rate 1: %d frames in %.0f s (the burst of one + about one per second)" % (nb, T))
    check(nc > 100, "rate 1000000: practically unlimited, C received %d" % nc)
    finish((a, b, c), cnt)

    sw.stop()

    # B disconnects; a new host gets port 2 again and is limited again (a new queue reads the configuration)
    sw = ts.Switch(3, extra=["-c", conf("port.2.rate = 20\n")])
    (a, b, c), cnt = scenario(sw)
    b.close()
    cnt[1].stop = True
    check(sw.wait_log("port 2: teardown complete", 10), "B left: port 2 is free again")
    time.sleep(0.5)
    b2 = ts.connect(sw)
    check(sw.wait_log("port 2: rate limit 20 frames/s", 10, 2), "the new host on port 2 is limited again (second 'rate limit' line)")
    cb2 = Counter(b2)
    cb2.start()
    b2.sendall(ts.frame(ME[4], BCAST, 1, 1, b"H-again"))
    end = time.time() + 10
    while time.time() < end and cnt[0].count(b"H") < 3:
        time.sleep(0.02)
    T = 4.0
    fl, nb, _ = measure(sw, (cnt[0], cb2, cnt[2]), T, a_sock=a)
    T = fl.t1 - fl.t0              # the real length of the window (a slow switch makes the last sendall last)
    low, high = bounds(20, T)
    check(low <= nb <= high, "the new host on port 2 received %d in %.0f s (%.0f..%.0f)" % (nb, T, low, high))
    cb2.stop = True
    finish((a, b2, c), cnt)
    sw.stop()


def test_bad_config():
    print("6. rate settings out of range are start-up errors")
    bad = [
        ("rate_limit too big", "rate_limit = 1000001\n", "rate_limit"),
        ("rate_limit negative", "rate_limit = -1\n", "rate_limit"),
        ("rate_limit not a number", "rate_limit = fast\n", "rate_limit"),
        ("port rate negative", "port.1.rate = -3\n", "port.1.rate"),
        ("port rate too big", "port.2.rate = 2000000\n", "port.2.rate"),
        ("port 0", "port.0.rate = 5\n", "port.0.rate"),
        ("port beyond num_ports", "port.4.rate = 5\n", "port.4.rate"),
    ]
    for name, text, key in bad:
        p = subprocess.run(shlex.split(ts.SWITCH) + [str(ts.free_port()), "3", "-c", conf(text)], capture_output=True, timeout=60)
        check(p.returncode == 1 and key.encode() in p.stderr and b"line 1" in p.stderr,
              "%s: exit status 1 and a message naming line 1 and '%s' (rc=%s): %s" % (name, key, p.returncode, p.stderr.decode().strip()[:120]))
    # the largest valid values start fine
    sw = ts.Switch(3, extra=["-c", conf("rate_limit = 1000000\nport.1.rate = 0\nport.3.rate = 1000000\n")])
    check(sw.p.poll() is None, "rate_limit = 1000000 is accepted")
    sw.stop()


def test_shutdown_throttled():
    print("7. SIGINT while a throttled sender waits for tokens (and frames are queued): prompt clean exit")
    sw = ts.Switch(3, extra=["-c", conf("port.2.rate = 1\nport.3.rate = 1\nqueue_capacity = 16\n")])
    (a, b, c), cnt = scenario(sw)
    fl = Flood(a, 1.5, to_c=True)
    fl.start()
    fl.join(60)
    check("port 2: output queue full" in sw.log(), "B's queue is full: its sender is waiting for a token")
    t0 = time.monotonic()
    sw.p.send_signal(signal.SIGINT)
    try:
        rc = sw.p.wait(30)
    except subprocess.TimeoutExpired:
        rc = None
    dt = time.monotonic() - t0
    check(rc == 0, "exit status 0 (got %s)" % rc)
    check(dt < (15.0 if SLOW else 3.0), "the shutdown did not wait for the tokens: %.2f s" % dt)
    log = sw.log()
    check("shutdown: complete" in log, "shutdown: complete is logged")
    finish((a, b, c), cnt)
    sw.stop()


def test_valgrind():
    print("8. valgrind: rate limited flood, reload to unlimited and back, SIGINT: no errors, no leaks, no descriptor left open")
    path = conf("port.2.rate = 50\n")
    sw = ts.Switch(3, extra=["-c", path])
    (a, b, c), cnt = scenario(sw)
    T = 5.0
    fl, nb, _ = measure(sw, cnt, T, a_sock=a)
    T = fl.t1 - fl.t0              # the real length of the window (a slow switch makes the last sendall last)
    low, high = bounds(50, T)
    check(nb <= high and nb > 0, "B received %d (never more than %.0f, and something)" % (nb, high))
    for text in ("port.2.rate = 0\n", "port.2.rate = 5\nrate_limit = 3\n"):
        n = sw.count("configuration reloaded from")
        write_conf(path, text)
        sw.p.send_signal(signal.SIGHUP)
        check(sw.wait_log("configuration reloaded from", 60, n + 1), "reload under valgrind")
    fl = Flood(a, 1.5)
    fl.start()
    fl.join(120)
    finish((a, b, c), cnt)
    sw.p.send_signal(signal.SIGINT)
    rc = sw.p.wait(120)
    check(rc == 0, "exit status 0 under valgrind (got %s)" % rc)
    log = sw.log()
    check("ERROR SUMMARY: 0 errors from 0 contexts" in log, "valgrind: 0 errors")
    check("in use at exit: 0 bytes in 0 blocks" in log, "valgrind: 0 bytes in use at exit")
    check(re.search(r"(definitely|indirectly|possibly) lost: [1-9]", log) is None, "valgrind: nothing lost")
    fds = [f for f in re.findall(r"Open (?:AF_INET socket|file descriptor) (\d+)", log) if int(f) > 2]
    check(not fds, "valgrind: no descriptor left open at exit %s" % fds)
    if "ERROR SUMMARY: 0 errors" not in log:
        print(log[-3000:])
    sw.stop()


# ----------------------------------------------------------------------------------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    try:
        if mode == "all":
            for t in (test_port_rate, test_global_rate_and_override, test_reload, test_reload_global, test_edges,
                      test_bad_config, test_shutdown_throttled):
                t()
        elif mode == "tsan":
            SLOW = True
            ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
            for t in (test_port_rate, test_reload, test_shutdown_throttled):
                t()
        elif mode == "valgrind":
            SLOW = True
            ts.SWITCH = os.environ.get("SWITCH") or (
                "valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
                "--error-exitcode=1 --track-fds=yes ./switch")
            test_valgrind()
        else:
            print("usage: test_rate.py [all|tsan|valgrind]")
            sys.exit(2)
    finally:
        subprocess.run(["rm", "-rf", TMP])
    print("%d checks passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
