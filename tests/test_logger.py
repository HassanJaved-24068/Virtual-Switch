#!/usr/bin/env python3
"""
Part 9 end-to-end tests: the asynchronous logger thread, with the real ./switch.

  python3 tests/test_logger.py            normal build
  python3 tests/test_logger.py tsan       SWITCH="setarch -R ./switch_tsan"   (flooding + SIGINT)
  python3 tests/test_logger.py valgrind   valgrind on ./switch               (no errors, no leaks, fds closed)

The switch writes its log to the file named by VSWITCH_LOG_FILE (default switch.log in its
working directory). Every wait has a timeout, so a deadlock fails a test instead of hanging.
"""
import atexit, os, re, shlex, signal, socket, subprocess, sys, tempfile, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_forward as tf

ME, BCAST = ts.ME, ts.BCAST
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

LINE = re.compile(r"^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\.(\d{3}) \[(INFO|WARN|ERR)\] (.*)$")
DROPPED = re.compile(r"(\d+) log messages dropped")


def parse(text):
    """-> (whole lines, bad lines). A line is whole if it has the timestamp and level format."""
    good, bad = [], []
    for ln in text.splitlines():
        (good if LINE.match(ln) else bad).append(ln)
    return good, bad


def dropped_sum(lines):
    return sum(int(DROPPED.search(l).group(1)) for l in lines if DROPPED.search(l))


def abs_switch():
    return os.path.join(ROOT, "switch")


def read_text(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace")
    except FileNotFoundError:           # not created yet (the caller may be waiting for it)
        return ""


def wait_text(path, needle, timeout=10, count=1):
    end = time.time() + timeout
    while time.time() < end:
        if read_text(path).count(needle) >= count:
            return True
        time.sleep(0.02)
    return False


# ----------------------------------------------------------------------------------------------
def test_events_and_format():
    print("1. switch.log has timestamped lines for connect, disconnect, MAC moved and port security")
    sw = ts.Switch(4, cmd="./switch_small")                     # M = 3 MACs per port
    a = tf.H(sw, ME[0], "--quiet")
    b = tf.H(sw, ME[1], "--quiet")
    a.say("send bcast hello")                                   # learns ME[0] on port 1
    a.wait_rx(0, 1)
    c = tf.H(sw, ME[0], "--quiet")                              # the same MAC on port 3: a move
    c.say("send bcast moved")
    check(sw.wait_log("MAC moved", 10), "'MAC moved' logged")
    b.quit(10)
    check(sw.wait_log("port 2: host disconnected", 10), "disconnect logged")
    s = ts.connect(sw)                                          # raw sender on the freed port 2
    sw.wait_log("port 2: host connected", 10, 2)
    for k in range(4):                                          # the 4th MAC exceeds M = 3
        s.sendall(ts.frame("02:00:00:00:99:%02x" % (k + 1), BCAST, 1, 1, b"x"))
    check(sw.wait_log("MAC flooding", 10), "port security event logged")
    s.close()
    sw.p.send_signal(signal.SIGINT)
    rc = sw.p.wait(15)
    check(rc == 0, "exit status 0 (got %s)" % rc)
    text = read_text(sw.logf.name)
    good, bad = parse(text)
    check(not bad, "every line of the log file has the timestamp and level format (%d bad: %s)" % (len(bad), bad[:2]))
    for what in ("host connected", "host disconnected", "MAC moved", "shut down: MAC flooding",
                 "teardown complete", "SIGINT received", "shutdown: complete"):
        check(any(what in l for l in good), "log contains '%s'" % what)
    check(good and "shutdown: complete" in good[-1], "the last line is 'shutdown: complete' (the logger stopped last)")
    # timestamps: taken by the caller before it queues; threads may differ by a few ms but not more
    stamps = [time.mktime(time.strptime("%s-%s-%s %s:%s:%s" % m.groups()[:6], "%Y-%m-%d %H:%M:%S")) * 1000 + int(m.group(7))
              for m in map(LINE.match, good)]
    inv = max([stamps[i] - stamps[i + 1] for i in range(len(stamps) - 1)] + [0])
    check(inv <= 50, "timestamps do not go backwards by more than 50 ms (worst %d ms)" % inv)
    errtxt = read_text(sw.errf.name)
    check("[INFO]" not in errtxt and "[WARN]" not in errtxt, "nothing on stderr: the log goes to the file")
    for h in (a, c):
        h.quit(10)
    sw.stop()


def test_default_file_and_append():
    print("2. default log file switch.log in the working directory; O_APPEND keeps old content")
    tmp = tempfile.mkdtemp(prefix="vswlog.")
    env = {k: v for k, v in os.environ.items() if k != "VSWITCH_LOG_FILE"}
    try:
        for run in (1, 2):
            port = ts.free_port()
            err = open(os.path.join(tmp, "stderr%d" % run), "w+b")
            p = subprocess.Popen([abs_switch(), str(port), "2"], cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=err)
            logp = os.path.join(tmp, "switch.log")
            try:
                ok = wait_text(logp, "listening on TCP port %d" % port, 10)
                check(ok, "run %d: switch.log was created and the switch is listening" % run)
                p.send_signal(signal.SIGINT)
                check(p.wait(10) == 0, "run %d: exit status 0" % run)
            finally:
                if p.poll() is None:              # a failed check must not leave a switch behind
                    p.kill()
                    p.wait()
            err.seek(0)
            check(err.read() == b"", "run %d: nothing written to stderr" % run)
            err.close()
            n = read_text(logp).count("shutdown: complete")
            check(n == run, "after run %d the file has %d 'shutdown: complete' lines (append, not truncate): %d" % (run, run, n))
        good, bad = parse(read_text(os.path.join(tmp, "switch.log")))
        check(not bad, "the appended file has only whole lines")
    finally:
        subprocess.run(["rm", "-rf", tmp])


def test_bad_log_path():
    print("3. an unusable log file is a clean start-up error")
    env = dict(os.environ, VSWITCH_LOG_FILE="/nonexistent-dir/switch.log")
    p = subprocess.run([abs_switch(), str(ts.free_port()), "2"], env=env, capture_output=True, timeout=15)
    check(p.returncode == 1, "exit status 1 (got %s)" % p.returncode)
    check(b"cannot open the log file" in p.stderr, "message on stderr: %r" % p.stderr[:80])


class Stalled:
    """A switch whose log file is a FIFO that nobody reads: the logger thread blocks on it."""

    def __init__(self, nports, cmd=None):
        self.dir = tempfile.mkdtemp(prefix="vswfifo.")
        self.fifo = os.path.join(self.dir, "log.fifo")
        os.mkfifo(self.fifo)
        self.rfd = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)   # so the switch's open() does not block
        self.port = ts.free_port()
        self.err = tempfile.NamedTemporaryFile("w+b", delete=False)
        env = dict(os.environ, VSWITCH_LOG_FILE=self.fifo)
        self.p = subprocess.Popen(shlex.split(cmd or ts.SWITCH) + [str(self.port), str(nports)], env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.err)
        end = time.time() + 20
        while time.time() < end:                                      # ready when it accepts connections
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.05)
        atexit.register(self._kill)                                   # never leave a switch behind
        self.chunks = []
        self.reader = None

    def _kill(self):
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait()

    def start_reading(self):
        fl = os.get_blocking(self.rfd)
        os.set_blocking(self.rfd, True)

        def run():
            while True:
                try:
                    b = os.read(self.rfd, 65536)
                except OSError:
                    break
                if not b:
                    break
                self.chunks.append(b)
        self.reader = threading.Thread(target=run, daemon=True)
        self.reader.start()

    def text(self):
        return b"".join(list(self.chunks)).decode(errors="replace")

    def close(self):
        if self.reader:
            self.reader.join(10)
        os.close(self.rfd)
        self.err.close()
        errtxt = open(self.err.name).read()
        os.unlink(self.err.name)
        subprocess.run(["rm", "-rf", self.dir])
        return errtxt


_seen_buf = {}


def recv_until(sock, needles, timeout=15):
    """Read frames from a raw socket until every needle was seen in some payload (the data read so far is
    kept per socket, so it can be called again). -> True/False."""
    sock.settimeout(timeout)
    buf = _seen_buf.get(sock, b"")
    try:
        while not all(n in buf for n in needles):
            data = sock.recv(65536)
            if not data:
                break
            buf += data
    except OSError:
        pass
    _seen_buf[sock] = buf
    return all(n in buf for n in needles)


def flood(port, frames):
    """Throughput through the switch with per-frame logging.
    B (quiet) and M (prints frames) are hosts, S is a raw sender. S sends `frames` UNICAST frames for B and
    then one marker frame for M: when M prints the marker, the switch has processed all of S's frames.
    -> (seconds from the first frame to the marker, number of frames B received)."""
    b = subprocess.Popen(shlex.split(ts.HOST) + ["127.0.0.1", str(port), ME[1], "--quiet"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    m = subprocess.Popen(shlex.split(ts.HOST) + ["127.0.0.1", str(port), ME[2]], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    mlines, mlock = [], threading.Lock()

    def read_m():
        for raw in m.stdout:
            with mlock:
                mlines.append(raw)
    threading.Thread(target=read_m, daemon=True).start()
    s = socket.create_connection(("127.0.0.1", port))
    # Wait until all three are connected and B and M are learned: both announce themselves with a
    # broadcast, and S (also a port) must see both.
    # A frame is only flooded to ports that are already UP, and a slow (TSan) switch may not have admitted
    # every host yet: the hosts announce themselves again until S has seen both (at most 30 s).
    end = time.time() + 30
    seen = False
    while time.time() < end and not seen:
        for h, name in ((b, b"hello-b"), (m, b"hello-m")):
            h.stdin.write(b"send bcast " + name + b"\n")
            h.stdin.flush()
        seen = recv_until(s, [b"hello-b", b"hello-m"], timeout=1.0)
    if not seen:
        raise RuntimeError("hosts did not connect")
    block = ts.frame(ME[3], ME[1], 1, 1, b"f") * 1000
    t0 = time.time()
    for _ in range(frames // 1000):
        s.sendall(block)
    s.sendall(ts.frame(ME[3], ME[2], 1, 1, b"marker"))
    took = None
    end = time.time() + 120
    while time.time() < end:
        with mlock:
            if any(b"data=marker" in l for l in mlines):
                took = time.time() - t0
                break
        time.sleep(0.005)
    s.close()
    b.stdin.write(b"quit\n")
    b.stdin.flush()
    m.stdin.write(b"quit\n")
    m.stdin.flush()
    out = b.communicate(timeout=30)[0].decode()
    m.wait(30)
    mt = re.search(r"RX_TOTAL (\d+)", out)
    return took, int(mt.group(1)) if mt else -1


FRAMES = int(os.environ.get("FLOOD_FRAMES", "100000"))


def test_flood_stalled_sink():
    print("4. a flood of %d frames with per-frame logging and a STALLED log file does not slow the switch" % FRAMES)
    # baseline: log file on disk
    sw = ts.Switch(4)
    base_t, base_b = flood(sw.port, FRAMES)
    check(base_t is not None, "baseline: the marker frame came through")
    base_t = base_t or 1e9
    sw.p.send_signal(signal.SIGINT)
    check(sw.p.wait(30) == 0, "baseline switch exited with status 0")
    text = read_text(sw.logf.name)
    good, bad = parse(text)
    rx = sum(1 for l in good if "RX src=%s" % ME[3] in l)     # the frames of S (its port number depends on start order)
    dr = dropped_sum(good)
    print("  baseline (file): %.2f s until the marker, B received %d, %d RX lines of S logged, %d dropped" % (base_t, base_b, rx, dr))
    check(not bad, "baseline: every line of the log is whole (%d bad)" % len(bad))
    check(rx + dr >= FRAMES, "baseline: each frame is logged or counted as dropped (%d + %d >= %d)" % (rx, dr, FRAMES))
    # Whether the logger drops anything here depends on the relative speed of the receiver and the logger (a slower
    # or TSan switch lets it keep up: 0 dropped is correct, nothing was lost). So the number is reported, not asserted;
    # "drops are counted exactly" is asserted above and in test_log.c, and the stalled run below MUST report drops.
    sw.stop()

    # the same flood, the log file is a FIFO that nobody reads
    st = Stalled(4)
    stall_t, stall_b = flood(st.port, FRAMES)
    check(stall_t is not None, "stalled: the marker frame came through while the logger was blocked")
    stall_t = stall_t or 1e9
    print("  stalled sink: %.2f s until the marker, B received %d" % (stall_t, stall_b))
    check(st.p.poll() is None, "the switch is alive while its logger is blocked")
    check(stall_t <= base_t * 2.5 + 2.0,
          "the switch is not slowed down by the stalled log: %.2f s vs %.2f s with a normal file (limit 2.5 x + 2 s)" % (stall_t, base_t))
    check(stall_b > 0 and stall_b * 100 >= base_b * 30,
          "B still receives (stalled %d vs baseline %d, at least 30 %%)" % (stall_b, base_b))
    # now the sink starts to drain; wait until the logger has caught up, then stop the switch
    st.start_reading()
    check(wait_stable(st), "the logger drains the FIFO and catches up")
    st.p.send_signal(signal.SIGINT)
    rc = st.p.wait(30)
    check(rc == 0, "exit status 0 after SIGINT (got %s)" % rc)
    st.reader.join(15)
    good, bad = parse(st.text())
    dr = dropped_sum(good)
    check(not bad, "stalled: every line that was written is whole (%d bad: %s)" % (len(bad), bad[:2]))
    check(dr > 0, "stalled: '%d log messages dropped' was reported" % dr)
    check(any("shutdown: complete" in l for l in good), "stalled: the shutdown messages were written (logger drained, joined last)")
    errtxt = st.close()
    check("[INFO]" not in errtxt, "stderr is quiet")


def wait_stable(st, quiet=1.0, timeout=60):
    """True when the reader has received nothing new for `quiet` seconds (the logger has caught up)."""
    end = time.time() + timeout
    last, since = -1, time.time()
    while time.time() < end:
        n = sum(len(c) for c in list(st.chunks))
        if n != last:
            last, since = n, time.time()
        elif n > 0 and time.time() - since >= quiet:
            return True
        time.sleep(0.1)
    return False


def test_sigint_during_flood():
    print("5. SIGINT during a flood: the logger writes what is queued and the switch exits 0")
    for rnd in range(3):
        sw = ts.Switch(4)
        b = tf.H(sw, ME[1], "--quiet")
        s = ts.connect(sw)
        sw.wait_log("port 2: host connected", 10)
        stop = threading.Event()
        block = ts.frame(ME[3], BCAST, 1, 1, b"f") * 500

        def run():
            try:
                while not stop.is_set():
                    s.sendall(block)
            except OSError:
                pass
        th = threading.Thread(target=run, daemon=True)
        th.start()
        time.sleep(0.3 + 0.2 * rnd)
        sw.p.send_signal(signal.SIGINT)
        rc = sw.p.wait(30)
        stop.set()
        th.join(5)
        check(rc == 0, "round %d: exit status 0 (got %s)" % (rnd, rc))
        good, bad = parse(read_text(sw.logf.name))
        check(not bad, "round %d: no torn line (%d bad)" % (rnd, len(bad)))
        check(any("shutdown: complete" in l for l in good) or dropped_sum(good) > 0,
              "round %d: 'shutdown: complete' was written" % rnd)
        s.close()
        b.quit(10)
        sw.stop()


def test_valgrind():
    print("6. valgrind: logger thread, flood, SIGINT: no errors, no leaks, log file closed")
    sw = ts.Switch(4)
    a = tf.H(sw, ME[0])
    b = tf.H(sw, ME[1], "--quiet")
    for i in range(30):
        a.say("send bcast m%d" % i)
    check(a.ok and b.ok, "hosts connected")
    check(sw.wait_log("host connected", 30, 2), "log written under valgrind")
    time.sleep(1.0)
    sw.p.send_signal(signal.SIGINT)
    rc = sw.p.wait(60)
    check(rc == 0, "valgrind run exited with status 0 (got %s)" % rc)
    log = sw.log()
    check("shutdown: complete" in read_text(sw.logf.name), "the shutdown messages reached switch.log")
    check("ERROR SUMMARY: 0 errors from 0 contexts" in log, "valgrind: 0 errors")
    check("in use at exit: 0 bytes in 0 blocks" in log, "valgrind: 0 bytes in use at exit")
    check(re.search(r"(definitely|indirectly|possibly) lost: [1-9]", log) is None, "valgrind: nothing lost")
    fds = [f for f in re.findall(r"Open (?:AF_INET socket|file descriptor) (\d+)", log) if int(f) > 2]
    check(not fds, "valgrind: no descriptor left open at exit (the log file was closed) %s" % fds)
    if "ERROR SUMMARY: 0 errors" not in log:
        print(log[-2500:])
    for h in (a, b):
        h.quit(10)
    sw.stop()


# ----------------------------------------------------------------------------------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "all":
        for t in (test_events_and_format, test_default_file_and_append, test_bad_log_path,
                  test_flood_stalled_sink, test_sigint_during_flood):
            t()
    elif mode == "tsan":
        ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
        FRAMES = int(os.environ.get("FLOOD_FRAMES", "30000"))
        test_flood_stalled_sink()
        test_sigint_during_flood()
    elif mode == "valgrind":
        ts.SWITCH = os.environ.get("SWITCH") or (
            "valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
            "--error-exitcode=1 --track-fds=yes ./switch")
        test_valgrind()
    else:
        print("usage: test_logger.py [all|tsan|valgrind]")
        sys.exit(2)
    print("%d checks passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
