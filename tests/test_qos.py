#!/usr/bin/env python3
"""
Part 11 end-to-end tests: three priority queues, weighted round robin, per-class fullness, bad priorities,
weights changed by SIGHUP -- with the real ./switch.

  python3 tests/test_qos.py            normal build
  python3 tests/test_qos.py tsan       SWITCH="setarch -R ./switch_tsan"
  python3 tests/test_qos.py valgrind   valgrind on ./switch (no errors, no leaks, no descriptor left open)

The scheduler itself (exact 5:3:1 order, aging, ...) is tested in tests/test_qos.c. Here the whole path is
tested: a host that never reads fills its kernel buffers, so the sender thread of its port blocks in write()
and the rings of that port fill (queue_capacity = 8). Then marked frames of different priorities are sent; when
the host starts reading, the ORDER in which they leave the switch is the scheduler's choice.
"""
import os, re, shlex, signal, socket, struct, subprocess, sys, tempfile, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_forward as tf
import test_logger as tl

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
tf.check = check
tl.check = check

TMP = tempfile.mkdtemp(prefix="vswqos.")
conf_no = 0
LOW = b"z" * 1400                       # the flood: 1400-byte low-priority frames


def conf(text):
    global conf_no
    conf_no += 1
    path = os.path.join(TMP, "q%03d.conf" % conf_no)
    return write_conf(path, text)


def write_conf(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


class Reader(threading.Thread):
    """Reads a raw socket all the time and remembers which MARK-xx payloads it has seen."""

    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock = sock
        self.marks = set()
        self.stop = False

    def run(self):
        buf = b""
        self.sock.settimeout(1.0)
        while not self.stop:
            try:
                d = self.sock.recv(1 << 20)
            except socket.timeout:
                continue
            except OSError:
                return
            if not d:
                return
            buf = (buf + d)[-4096:]
            for m in re.finditer(rb"MARK-(\w+)", buf):
                self.marks.add(m.group(1).decode())


FLOOD_FRAMES = int(os.environ.get("QOS_FLOOD", "12000"))      # 12000 x 1417 bytes = 17 MB


def congest(sw, a, c_sock):
    """A floods low-priority frames (broadcast) for C, which does not read. The volume (17 MB) is far more than
    the socket buffers can take (a few MB), so when the flood is over the sender thread of C's port is blocked in
    write() and every ring of the port is full. (A 'queue full' log line alone does not prove that: A may just
    have been faster than the sender for a moment, and the rings drain again into the buffers when A stops.)"""
    key = "port 1: output queue full"                  # C is port 1 (D's queue may overflow too)
    block = ts.frame(ME[3], BCAST, 1, 2, LOW) * 100
    for _ in range(FLOOD_FRAMES // 100):
        a.sendall(block)
    ok = sw.wait_log(key, 60)
    # the drop counter says how many frames did not fit: with this volume it must be in the thousands
    time.sleep(1.5)                                    # A is quiet; the switch has read everything; the sender is blocked
    drops = tf.last_drop(sw.log().encode(), 1)
    check(drops >= 1000, "C's rings overflowed: %d frames dropped (the sender is blocked, not draining)" % drops)
    return ok


def send_marks(a, kinds):
    """Send small marked frames: kinds is a list like [("n1", 1), ("h1", 0), ...] (name, priority)."""
    for name, prio in kinds:
        a.sendall(ts.frame(ME[3], BCAST, 1, prio, b"MARK-" + name.encode()))


def read_stream(c_sock, names, after=12, timeout=90):
    """Read frames from C until all `names` were seen and `after` more frames came. -> list of kinds in order:
    the mark name, or 'L' for a low-priority flood frame."""
    c_sock.settimeout(5)
    buf = b""
    kinds = []
    seen = 0
    end = time.time() + timeout
    left = None
    while time.time() < end:
        try:
            d = c_sock.recv(1 << 20)
        except socket.timeout:
            break
        if not d:
            break
        buf += d
        pos = 0
        while len(buf) - pos >= 17:
            ln = struct.unpack("!H", buf[pos:pos + 2])[0]
            if len(buf) - pos < 17 + ln:
                break
            payload = buf[pos + 17:pos + 17 + ln]
            if payload.startswith(b"MARK-"):
                kinds.append(payload[5:].decode())
                seen += 1
            else:
                kinds.append("L")
            pos += 17 + ln
            if seen == len(names) and left is None:
                left = after
            elif left is not None:
                left -= 1
                if left <= 0:
                    return kinds
        buf = buf[pos:]
    return kinds


def after_first(kinds, name, count):
    i = kinds.index(name) if name in kinds else -1
    return kinds[i:i + count] if i >= 0 else []


def scenario(sw, extra_check=None):
    """Three hosts: C (port 1, never reads at first), D (port 2, reads everything), A (port 3, the sender)."""
    # C has a tiny receive buffer: its TCP window closes after a few frames, so the sender thread of its port
    # really blocks in write() and the rings of the port fill. (With the default buffers of several MB the switch
    # can swallow the whole flood and the rings drain again as soon as A stops.)
    c = socket.socket()
    c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    c.connect(("127.0.0.1", sw.port))
    sw.wait_log("port 1: host connected", 10)
    d = ts.connect(sw)
    sw.wait_log("port 2: host connected", 10)
    a = ts.connect(sw)
    sw.wait_log("port 3: host connected", 10)
    rd = Reader(d)
    rd.start()
    return c, d, a, rd


def run_round(sw, c, a, rd, kinds, label):
    """Congest C, send marks, wait until the fast reader saw them (so the switch has queued them for C too),
    then read C's stream. -> list of kinds as C received them."""
    check(congest(sw, a, c), label + ": the queue of C is full (the sender of its port is blocked)")
    rd.marks.clear()
    send_marks(a, kinds)
    names = [k for k, _ in kinds]
    end = time.time() + 30
    while time.time() < end and not set(names) <= rd.marks:
        time.sleep(0.02)
    check(set(names) <= rd.marks, label + ": D, which reads, got all marked frames: the switch has forwarded them")
    time.sleep(0.2)
    return read_stream(c, names)


# ----------------------------------------------------------------------------------------------
def test_priority_order():
    print("1. congested port: high before normal before low, 5:3:1 (queue_capacity = 8)")
    sw = ts.Switch(3, extra=["-c", conf("queue_capacity = 8\nprio_aging_ms = 60000\n")])
    c, d, a, rd = scenario(sw)
    # n1 is sent BEFORE h1, but the high ring is served first
    kinds = run_round(sw, c, a, rd, [("n1", 1), ("h1", 0), ("n2", 1), ("h2", 0), ("n3", 1), ("h3", 0)], "5:3:1")
    check(all(k in kinds for k in ("h1", "h2", "h3", "n1", "n2", "n3")), "C received all six marked frames (high and normal are not dropped while the low ring is full): %s" %
          [k for k in kinds if k != "L"])
    order = [k for k in kinds if k != "L"]
    check(order == ["h1", "h2", "h3", "n1", "n2", "n3"],
          "the marked frames leave in priority order, FIFO inside a class (sent n1 h1 n2 h2 n3 h3): %s" % order)
    run = after_first(kinds, "h1", 7)
    check(run == ["h1", "h2", "h3", "n1", "n2", "n3", "L"], "5:3:1: three high, three normal, then a low frame: %s" % run)
    lows_after = len([k for k in kinds[kinds.index("n3"):] if k == "L"])
    check(lows_after >= 5, "queued low frames were overtaken: %d low frames arrived after the last normal frame" % lows_after)
    check(sw.count("output queue full") >= 1, "the full low ring dropped (and counted) low frames")
    rd.stop = True
    for s in (a, c, d):
        s.close()
    sw.stop()


def test_reload_weights():
    print("2. SIGHUP changes the weights: 1,1,1 interleaves, then 5,3,1 again")
    path = conf("queue_capacity = 8\nprio_aging_ms = 60000\n")
    sw = ts.Switch(3, extra=["-c", path])
    c, d, a, rd = scenario(sw)
    marks = [("h1", 0), ("h2", 0), ("h3", 0), ("n1", 1), ("n2", 1), ("n3", 1)]
    n = sw.count("configuration reloaded from")
    write_conf(path, "queue_capacity = 8\nprio_aging_ms = 60000\nwrr_weights = 1,1,1\n")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded from", 15, n + 1), "SIGHUP: reloaded")
    log = sw.log()
    check("reload: wrr_weights 5,3,1 -> 1,1,1 (applies at the next scheduling round of every port)" in log,
          "the log says the weights are applied (no longer 'stored')")
    kinds = run_round(sw, c, a, rd, marks, "1:1:1")
    run = after_first(kinds, "h1", 8)
    check(run == ["h1", "n1", "h2", "n2", "L", "h3", "n3", "L"], "1:1:1 interleaves the classes: %s" % run)
    # the C socket is drained by read_stream only up to a point: drain the rest, then repeat with 5,3,1
    c.settimeout(2)
    try:
        while c.recv(1 << 20):
            pass
    except OSError:
        pass
    n = sw.count("configuration reloaded from")
    write_conf(path, "queue_capacity = 8\nprio_aging_ms = 60000\n")           # back to the defaults 5,3,1
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded from", 15, n + 1) and "wrr_weights 1,1,1 -> 5,3,1" in sw.log(), "SIGHUP: weights back to 5,3,1")
    kinds = run_round(sw, c, a, rd, marks, "5:3:1 again")
    run = after_first(kinds, "h1", 7)
    check(run == ["h1", "h2", "h3", "n1", "n2", "n3", "L"], "5:3:1 again: %s" % run)
    check(sw.p.poll() is None, "the switch is alive")
    rd.stop = True
    for s in (a, c, d):
        s.close()
    sw.stop()


def test_bad_priority():
    print("3. a priority above 2 is queued as low, counted, and logged once per port")
    sw = ts.Switch(4)
    r = ts.connect(sw)                                           # port 1: a reader
    sw.wait_log("port 1: host connected", 10)
    rd = Reader(r)
    rd.start()
    s2 = ts.connect(sw)
    sw.wait_log("port 2: host connected", 10)
    s3 = ts.connect(sw)
    sw.wait_log("port 3: host connected", 10)
    for prio in (3, 9, 255):
        s2.sendall(ts.frame(ME[1], BCAST, 1, prio, b"MARK-bad%d" % prio))
    s3.sendall(ts.frame(ME[2], BCAST, 1, 200, b"MARK-bad3x"))
    s3.sendall(ts.frame(ME[2], BCAST, 1, 1, b"MARK-fine"))
    end = time.time() + 15
    while time.time() < end and not {"bad3", "bad9", "bad255", "bad3x", "fine"} <= rd.marks:
        time.sleep(0.02)
    check({"bad3", "bad9", "bad255", "bad3x", "fine"} <= rd.marks, "all five frames were forwarded (a bad priority does not drop a frame)")
    check(sw.wait_log("bad_prio: a frame with priority", 10, 2), "bad_prio is logged for the two offending ports")
    time.sleep(0.3)
    log = sw.log()
    n2 = len(re.findall(r"port 2: bad_prio: a frame with priority", log))
    n3 = len(re.findall(r"port 3: bad_prio: a frame with priority", log))
    check(n2 == 1 and "port 2: bad_prio: a frame with priority 3 " in log,
          "port 2: ONE log line in all for three bad frames (the first one, priority 3): %d line(s)" % n2)
    check(n3 == 1 and "port 3: bad_prio: a frame with priority 200 " in log, "port 3: one log line: %d" % n3)
    check("port 1: bad_prio" not in log, "the reader's port (valid priorities only) has none")
    s2.close()
    check(sw.wait_log("port 2: bad_prio: 3 frames with a priority above 2 were queued as low priority", 10),
          "at the teardown of port 2 the count of 3 is logged")
    s3.close()
    check(sw.wait_log("port 3: bad_prio: 1 frame with a priority above 2", 10), "port 3: count of 1")
    # the frame is forwarded as it came: priority and all
    r.close()
    rd.stop = True
    sw.stop()


def test_bad_priority_wire_unchanged():
    print("4. the frame on the wire is not changed by the scheduler (a bad priority stays 9)")
    sw = ts.Switch(3)
    r = ts.connect(sw)
    sw.wait_log("port 1: host connected", 10)
    s = ts.connect(sw)
    sw.wait_log("port 2: host connected", 10)
    s.sendall(ts.frame(ME[1], BCAST, 1, 9, b"hello-nine"))
    r.settimeout(10)
    data = b""
    while len(data) < 17 + len(b"hello-nine"):
        data += r.recv(4096)
    check(data[16] == 9 and data[17:] == b"hello-nine", "the receiving host sees prio=9 and the payload unchanged")
    r.close()
    s.close()
    sw.stop()


def test_normal_traffic_unchanged():
    print("5. frames of all three priorities between hosts: all delivered in order per class")
    sw = ts.Switch(3)
    a = tf.H(sw, ME[0])
    b = tf.H(sw, ME[1])
    check(a.ok and b.ok, "two hosts connected")
    b.say("send bcast b-hello")
    check(a.wait_rx(1, 10), "A learned B")
    for i in range(1, 6):
        a.say("sendx %s 1 0 hi-%d" % (ME[1], i))
        a.say("sendx %s 1 1 mid-%d" % (ME[1], i))
        a.say("sendx %s 1 2 lo-%d" % (ME[1], i))
    check(b.wait_rx(15, 15), "B received all 15 frames")
    lines = b.rx()
    for cls, prio in (("hi", 0), ("mid", 1), ("lo", 2)):
        mine = [l for l in lines if "prio=%d " % prio in l and "data=%s-" % cls in l]
        seq = [int(re.search(r"data=%s-(\d+)" % cls, l).group(1)) for l in mine]
        check(seq == [1, 2, 3, 4, 5], "class %s (prio %d): 5 frames, in order: %s" % (cls, prio, seq))
    a.quit(10)
    b.quit(10)
    sw.stop()


def test_valgrind():
    print("6. valgrind: priority scheduling, bad priorities, reload: no errors, no leaks, no descriptor left open")
    path = conf("queue_capacity = 8\nprio_aging_ms = 60000\n")
    sw = ts.Switch(3, extra=["-c", path])
    c, d, a, rd = scenario(sw)
    kinds = run_round(sw, c, a, rd, [("n1", 1), ("h1", 0), ("h2", 0)], "valgrind")
    order = [k for k in kinds if k != "L"]
    check(order == ["h1", "h2", "n1"], "high before normal under valgrind: %s" % order)
    a.sendall(ts.frame(ME[3], BCAST, 1, 7, b"MARK-vgbad"))
    check(sw.wait_log("bad_prio", 60), "bad_prio logged")
    n = sw.count("configuration reloaded from")
    write_conf(path, "queue_capacity = 8\nwrr_weights = 2,2,2\nprio_aging_ms = 500\n")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded from", 60, n + 1), "reload")
    rd.stop = True
    for s in (a, c, d):
        s.close()
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
            for t in (test_priority_order, test_reload_weights, test_bad_priority, test_bad_priority_wire_unchanged,
                      test_normal_traffic_unchanged):
                t()
        elif mode == "tsan":
            ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
            for t in (test_priority_order, test_bad_priority, test_normal_traffic_unchanged):
                t()
        elif mode == "valgrind":
            ts.SWITCH = os.environ.get("SWITCH") or (
                "valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
                "--error-exitcode=1 --track-fds=yes ./switch")
            test_valgrind()
        else:
            print("usage: test_qos.py [all|tsan|valgrind]")
            sys.exit(2)
    finally:
        subprocess.run(["rm", "-rf", TMP])
    print("%d checks passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
