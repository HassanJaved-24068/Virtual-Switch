#!/usr/bin/env python3
"""
Part 3 end-to-end tests for ./switch (listener, port table, receiver threads).

The switch writes its log to a file (Part 9: the logger thread; VSWITCH_LOG_FILE names it) and
sanitizer / valgrind reports go to stderr; every test gives each its own file and reads the
lines back (Switch.log() returns both). Hosts are the real ./host program, or raw Python
sockets when a test needs to send something a normal host never would.

Environment: SWITCH (default ./switch), HOST (default ./host). To test the
TSan build:  SWITCH="setarch -R ./switch_tsan" HOST=./host python3 tests/test_switch.py
"""
import os, re, resource, shlex, signal, socket, struct, subprocess, sys, tempfile, time

SWITCH = os.environ.get("SWITCH", "./switch")
HOST = os.environ.get("HOST", "./host")
ME = [("02:00:00:00:00:%02x" % i) for i in range(0x10, 0x60)]
BCAST = "ff:ff:ff:ff:ff:ff"

passed = failed = 0


def check(cond, what):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", what)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def mac(s):
    return bytes(int(x, 16) for x in s.split(":"))


def frame(src, dst, vlan, prio, payload, length=None):
    n = len(payload) if length is None else length
    return struct.pack("!H", n) + mac(src) + mac(dst) + struct.pack("!HB", vlan, prio) + payload


class Switch:
    def __init__(self, num_ports, port=None, extra=(), nofile=None, cmd=None):
        self.port = port or free_port()
        self.logf = tempfile.NamedTemporaryFile("w+b", suffix=".log", delete=False)   # the switch's log file
        self.errf = tempfile.NamedTemporaryFile("w+b", suffix=".err", delete=False)   # its stderr
        env = dict(os.environ, VSWITCH_LOG_FILE=self.logf.name)
        pre = None
        if nofile:
            def pre():
                resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
        self.p = subprocess.Popen(shlex.split(cmd or SWITCH) + [str(self.port), str(num_ports)] + list(extra),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=self.errf, env=env, preexec_fn=pre)
        self.hosts = []
        if not self.wait_log("listening on TCP port %d" % self.port, 10):
            raise RuntimeError("switch did not start; log:\n" + self.log())

    def log(self):
        with open(self.logf.name, "rb") as f:
            text = f.read().decode(errors="replace")
        with open(self.errf.name, "rb") as f:
            return text + f.read().decode(errors="replace")

    def count(self, text):
        return self.log().count(text)

    def wait_log(self, text, timeout=5, count=1):
        end = time.time() + timeout
        while time.time() < end:
            if self.log().count(text) >= count:
                return True
            if self.p.poll() is not None:
                return self.log().count(text) >= count
            time.sleep(0.02)
        return False

    def host(self, mac_str, *opts):
        """Start a real ./host and wait until the switch has given it a port."""
        n = self.count("host connected")
        h = subprocess.Popen(shlex.split(HOST) + ["127.0.0.1", str(self.port), mac_str] + list(opts),
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.hosts.append(h)
        ok = self.wait_log("host connected", 5, n + 1)
        return h if ok else None

    def threads(self):
        out = subprocess.run(["ps", "-T", "-p", str(self.p.pid), "-o", "comm="],
                             capture_output=True, text=True).stdout.split()
        return out

    def stop(self):
        for h in self.hosts:
            try:
                h.stdin.close()
            except Exception:
                pass
            try:
                h.wait(5)
            except Exception:
                h.kill()
        alive = self.p.poll() is None
        if alive:
            self.p.send_signal(signal.SIGTERM)      # Part 7: a clean shutdown, exit status 0
            try:
                rc = self.p.wait(15)
                check(rc == 0, "switch exited with status 0 after SIGTERM (got %s)" % rc)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
                check(False, "switch did not exit within 15 s after SIGTERM (killed)")
        log = self.log()
        check("ThreadSanitizer" not in log and "Sanitizer" not in log,
              "no sanitizer report in the switch log")
        self.logf.close()
        self.errf.close()
        os.unlink(self.logf.name)
        os.unlink(self.errf.name)
        return alive


def send(h, line):
    h.stdin.write((line + "\n").encode())
    h.stdin.flush()


def rx(port, src, dst, vlan, prio, n):
    return "port %d: RX src=%s dst=%s vlan=%d prio=%d len=%d" % (port, src, dst, vlan, prio, n)


def connect(sw):
    s = socket.create_connection(("127.0.0.1", sw.port))
    return s


# --------------------------------------------------------------------------
def test_arguments():
    print("switch: command-line validation")
    cases = {
        "no arguments": [],
        "one argument": ["9000"],
        "port 0": ["0", "4"],
        "port 65536": ["65536", "4"],
        "port negative": ["-1", "4"],
        "port not a number": ["80x", "4"],
        "port empty": ["", "4"],
        "ports 0": ["9000", "0"],
        "ports 65": ["9000", "65"],
        "ports negative": ["9000", "-3"],
        "ports not a number": ["9000", "many"],
        "-c without file": ["9000", "4", "-c"],
        "wrong flag": ["9000", "4", "-x", "f"],
        "extra argument": ["9000", "4", "-c", "f", "g"],
        "stray 3rd argument": ["9000", "4", "foo"],
    }
    for name, args in cases.items():
        p = subprocess.run(shlex.split(SWITCH) + args, capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
        err = p.stderr.decode()
        check(p.returncode == 1, "%s -> exit 1 (rc=%r)" % (name, p.returncode))
        check("usage" in err or "switch:" in err, "%s -> message on stderr" % name)
        check(b"listening" not in p.stderr, "%s -> did not start" % name)

    # the port is already in use
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    bp = busy.getsockname()[1]
    p = subprocess.run(shlex.split(SWITCH) + [str(bp), "4"], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
    check(p.returncode == 1 and b"bind" in p.stderr, "port in use -> exit 1 with bind message (rc=%r)" % p.returncode)
    busy.close()

    # boundary values that are valid
    for ports in ("1", "64"):
        sw = Switch(int(ports))
        check(sw.p.poll() is None, "%s port(s) accepted" % ports)
        check(("with %s ports" % ports) in sw.log(), "log says %s ports" % ports)
        sw.stop()
    sw = Switch(4, extra=["-c", "some.conf"])
    check("config file: some.conf" in sw.log(), "-c <config> accepted and noted")
    sw.stop()


def test_three_hosts():
    print("switch: three hosts get ports 1, 2, 3; frames are logged")
    sw = Switch(8)
    a = sw.host(ME[0], )
    check(a is not None and "port 1: host connected" in sw.log(), "first host -> port 1")
    b = sw.host(ME[1])
    check(b is not None and "port 2: host connected" in sw.log(), "second host -> port 2")
    c = sw.host(ME[2])
    check(c is not None and "port 3: host connected" in sw.log(), "third host -> port 3")

    send(a, "sendx %s 10 2 hi" % ME[1])
    check(sw.wait_log(rx(1, ME[0], ME[1], 10, 2, 2)), "frame from host 1 logged with correct fields")
    send(b, "send bcast hello world")
    check(sw.wait_log(rx(2, ME[1], BCAST, 1, 1, 11)), "broadcast from host 2 logged")
    send(c, "sendx %s 4094 0 " % ME[0])
    check(sw.wait_log(rx(3, ME[2], ME[0], 4094, 0, 0)), "zero-length frame from host 3 logged")
    send(a, "send %s %s" % (ME[2], "x" * 1500))
    check(sw.wait_log(rx(1, ME[0], ME[2], 1, 1, 1500)), "1500-byte frame logged")
    for i in range(5):
        send(c, "send bcast m%d" % i)
    check(sw.wait_log("port 3: RX", 5, 6), "several frames in a row all logged")

    names = sw.threads()
    rxs = sorted(t for t in names if t.startswith("rx-port"))
    check(rxs == ["rx-port1", "rx-port2", "rx-port3"], "ps -T shows one receiver per host: %s" % rxs)
    check(names.count("accept") == 1, "ps -T shows exactly one accept thread")
    txs = sorted(t for t in names if t.startswith("tx-port"))
    check(txs == ["tx-port1", "tx-port2", "tx-port3"], "ps -T shows one sender per host: %s" % txs)
    others = [t for t in names if t not in ("accept", "reaper", "aging", "signal", "logger") and not t.startswith(("rx-port", "tx-port"))]
    # the main thread; the TSan build has one extra runtime thread with the same name
    check(names.count("reaper") == 1, "ps -T shows exactly one reaper thread")
    check(names.count("aging") == 1, "ps -T shows exactly one aging thread")
    check(names.count("signal") == 1, "ps -T shows exactly one signal thread (Part 7)")
    check(names.count("logger") == 1, "ps -T shows exactly one logger thread (Part 9)")
    check(1 <= len(others) <= (2 if "tsan" in SWITCH else 1) and len(names) == 5 + 6 + len(others),
          "main + accept + reaper + aging + signal + logger + 3 receivers + 3 senders (+TSan helper), got %s" % names)

    d = sw.host(ME[3])
    check(d is not None and "port 4: host connected" in sw.log(), "fourth host -> port 4")
    check(len([t for t in sw.threads() if t.startswith("rx-port")]) == 4, "4 receiver threads now")
    check(sw.stop(), "switch was still running at the end")


def test_partial():
    print("switch: a --partial host (one byte at a time)")
    sw = Switch(4)
    h = sw.host(ME[0], "--partial")
    other = sw.host(ME[1])
    send(h, "sendx %s 7 2 slowly" % ME[1])
    check(sw.wait_log(rx(1, ME[0], ME[1], 7, 2, 6)), "partial frame logged correctly, once")
    send(other, "send bcast fast")
    check(sw.wait_log(rx(2, ME[1], BCAST, 1, 1, 4)), "other host unaffected while partial host is slow")
    send(h, "send bcast two")
    send(h, "send bcast three")
    check(sw.wait_log("port 1: RX", 5, 3), "several partial frames in a row")
    check(sw.count("truncated") == 0 and sw.count("protocol") == 0, "no errors logged")
    sw.stop()


def test_disconnects():
    print("switch: disconnects and broken hosts do not hurt others (ports are reused)")
    sw = Switch(16)
    a = sw.host(ME[0])
    b = sw.host(ME[1])
    c = sw.host(ME[2])
    before = len([t for t in sw.threads() if t.startswith("rx-port")])
    check(before == 3, "3 receivers before")

    send(b, "quit")
    check(sw.wait_log("port 2: host disconnected"), "clean disconnect logged")
    check(sw.wait_log("port 2: teardown complete, port is FREE"), "the reaper freed port 2")
    check(sorted(t for t in sw.threads() if t.startswith("rx-port")) == ["rx-port1", "rx-port3"],
          "the receiver of port 2 has exited, the others remain")
    check(sorted(t for t in sw.threads() if t.startswith("tx-port")) == ["tx-port1", "tx-port3"],
          "and so has its sender")
    send(a, "send bcast still-here")
    send(c, "send bcast me-too")
    check(sw.wait_log(rx(1, ME[0], BCAST, 1, 1, 10)) and sw.wait_log(rx(3, ME[2], BCAST, 1, 1, 6)),
          "other hosts still work after a disconnect")

    # Every host below gets the lowest FREE port, which is port 2 again.
    n_connect = [1]                              # connections that used port 2 so far
    n_tear = [1]                                 # teardowns of port 2 so far
    seen = {}

    def expect(text, what):
        seen[text] = seen.get(text, 0) + 1
        ok = sw.wait_log(text, 5, seen[text])
        n_tear[0] += 1
        check(ok, what)
        check(sw.wait_log("port 2: teardown complete, port is FREE", 5, n_tear[0]), what + " -> port 2 is FREE again")

    d = sw.host(ME[3], "--die-midframe")         # port 2: dies in the middle of a frame
    n_connect[0] += 1
    check(sw.count("port 2: host connected") == n_connect[0], "the freed port 2 was given to the next host")
    expect("port 2: truncated frame (payload cut short), discarded", "half a frame -> truncated payload logged")
    check(sw.count("port 2: RX") == 0, "the half frame was not logged as a frame")
    send(a, "send bcast after-midframe")
    check(sw.wait_log("port 1: RX", 5, 2), "others fine after a mid-frame death")

    def hostile(action):
        s = connect(sw)
        n_connect[0] += 1
        sw.wait_log("port 2: host connected", 5, n_connect[0])
        action(s)

    def trunc_hdr(s):
        s.sendall(frame(ME[5], BCAST, 1, 1, b"")[:5])
        s.close()

    def too_long(s):
        s.sendall(frame(ME[6], BCAST, 1, 1, b"", length=1501))

    def way_too_long(s):
        s.sendall(frame(ME[7], BCAST, 1, 1, b"", length=65535))

    def reset(s):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()                                # RST instead of FIN

    def short_payload(s):
        s.sendall(frame(ME[9], BCAST, 1, 1, b"abc", length=10))     # header says 10, only 3 sent
        s.close()

    hostile(trunc_hdr)
    expect("port 2: truncated frame (header cut short), discarded", "truncated header (5 of 17 bytes)")
    hostile(too_long)
    expect("port 2: protocol violation: length 1501 > 1500", "length 1501 -> protocol violation")
    hostile(way_too_long)
    expect("port 2: protocol violation: length 65535 > 1500", "length 65535 -> protocol violation")
    hostile(reset)
    expect("port 2: read error: Connection reset by peer", "TCP reset -> read error logged")
    hostile(short_payload)
    expect("port 2: truncated frame (payload cut short), discarded", "payload shorter than length")

    send(a, "send bcast survivor")
    check(sw.wait_log("port 1: RX", 5, 3), "host 1 still works after all the broken hosts")
    check(sw.count("teardown complete") == sw.count("host connected") - 2,
          "one teardown per connection that ended (hosts 1 and 3 are still connected)")
    check(sw.p.poll() is None, "switch still running")
    check(sw.stop(), "switch survived to the end")


def test_raw_fields():
    print("switch: exact frame fields from a raw host")
    sw = Switch(4)
    s = connect(sw)
    sw.wait_log("port 1: host connected")
    data = (frame(ME[0], ME[1], 1, 0, b"a") +
            frame(ME[0], BCAST, 4094, 2, b"b" * 1500) +
            frame(ME[0], ME[2], 258, 1, b""))
    s.sendall(data)                                       # three frames in one write
    check(sw.wait_log(rx(1, ME[0], ME[1], 1, 0, 1)), "frame 1 of 3 in one write")
    check(sw.wait_log(rx(1, ME[0], BCAST, 4094, 2, 1500)), "frame 2: max vlan, prio 2, 1500 bytes")
    check(sw.wait_log(rx(1, ME[0], ME[2], 258, 1, 0)), "frame 3: zero length, vlan 258")
    # frame split into pieces across the header/payload boundary
    raw = frame(ME[0], ME[3], 5, 1, b"split-me")
    s.sendall(raw[:3]); time.sleep(0.05)
    s.sendall(raw[3:17]); time.sleep(0.05)
    s.sendall(raw[17:])
    check(sw.wait_log(rx(1, ME[0], ME[3], 5, 1, 8)), "frame split in 3 pieces logged once")
    check(sw.count("port 1: RX") == 4, "exactly 4 frames logged")
    s.close()
    sw.stop()


def test_port_full():
    print("switch: no free port -> the host waits, then gets the exact SWITCH FULL frame")
    sw = Switch(2)
    a = sw.host(ME[0])
    b = sw.host(ME[1])
    s = connect(sw)
    s.settimeout(15)
    t0 = time.time()
    check(sw.wait_log("no free port: host waits up to 5 s for one"), "the third host is told to wait")
    want = (struct.pack("!H", 11) + bytes(6) + b"\xff" * 6 + struct.pack("!HB", 1, 0) + b"switch full")
    got = b""
    while len(got) < len(want):
        chunk = s.recv(len(want) - len(got))
        if not chunk:
            break
        got += chunk
    el = time.time() - t0
    check(got == want, "exactly the documented frame: %s" % got.hex())
    check(4.5 <= el <= 8.0, "after about 5 s (%.1f s)" % el)
    check(s.recv(10) == b"", "then the connection is closed")
    check(sw.wait_log("no free port after 5 s: sending SWITCH FULL"), "timeout logged")
    s.close()
    send(a, "send bcast ok")
    check(sw.wait_log(rx(1, ME[0], BCAST, 1, 1, 2)), "existing hosts unaffected")
    check(sw.count("port 3:") == 0, "no port 3 was ever created")
    check(len([t for t in sw.threads() if t.startswith("rx-port")]) == 2, "still only 2 receivers")
    check(sw.count("teardown complete") == 0, "the rejected host never had a port, so no teardown")
    sw.stop()


def logged_and_dropped(sw, text):
    """(lines containing `text`, messages reported as dropped by the logger)."""
    log = sw.log()
    dropped = sum(int(n) for n in re.findall(r"(\d+) log messages dropped", log))
    return log.count(text), dropped


def test_flood_log():
    print("switch: 3000 frames from one host")
    sw = Switch(4)
    h = sw.host(ME[0], "--flood", "3000", "--to", "bcast")
    # Part 9: the logger never slows the receiver down, so a fast flood may overflow its ring
    # (1024 records) and some per-frame lines are DROPPED (and counted). Every frame must be
    # either logged or counted as dropped; none may be logged twice.
    end = time.time() + 20
    while time.time() < end:
        n, d = logged_and_dropped(sw, "port 1: RX")
        if n + d >= 3000:
            break
        time.sleep(0.05)
    time.sleep(0.3)
    n, d = logged_and_dropped(sw, "port 1: RX")
    check(n + d >= 3000, "each of the 3000 frames is logged or counted as dropped (%d logged, %d dropped)" % (n, d))
    check(n <= 3000, "no frame is logged twice (%d RX lines)" % n)
    sw.stop()


def socket_flags(pid):
    """Return {'listen': flags, 'conn': [flags...]} for the switch's sockets."""
    inodes = {}
    for line in open("/proc/net/tcp").read().splitlines()[1:]:
        f = line.split()
        inodes[f[9]] = f[3]                 # inode -> state
    res = {"listen": [], "conn": []}
    for fd in os.listdir("/proc/%d/fd" % pid):
        try:
            link = os.readlink("/proc/%d/fd/%s" % (pid, fd))
        except OSError:
            continue
        m = re.match(r"socket:\[(\d+)\]", link)
        if not m or m.group(1) not in inodes:
            continue
        flags = int(re.search(r"flags:\s*(\d+)", open("/proc/%d/fdinfo/%s" % (pid, fd)).read()).group(1), 8)
        res["listen" if inodes[m.group(1)] == "0A" else "conn"].append(flags)
    return res


def test_socket_flags():
    print("switch: listening socket is non-blocking, accepted sockets are blocking")
    O_NONBLOCK, O_CLOEXEC = 0o4000, 0o2000000
    sw = Switch(4)
    a = sw.host(ME[0])
    b = sw.host(ME[1])
    fl = socket_flags(sw.p.pid)
    check(len(fl["listen"]) == 1 and fl["listen"][0] & O_NONBLOCK, "listener has O_NONBLOCK")
    check(fl["listen"] and fl["listen"][0] & O_CLOEXEC, "listener has O_CLOEXEC")
    check(len(fl["conn"]) == 2 and all(not (x & O_NONBLOCK) for x in fl["conn"]),
          "2 accepted sockets, none non-blocking (%s)" % fl["conn"])
    check(all(x & O_CLOEXEC for x in fl["conn"]), "accepted sockets have O_CLOEXEC")
    out = subprocess.run(["ss", "-ltn", "sport", "=", ":%d" % sw.port], capture_output=True, text=True).stdout
    check("0.0.0.0:%d" % sw.port in out, "bound to INADDR_ANY (0.0.0.0)")
    sw.stop()


def test_reset_before_accept():
    print("switch: connections reset right after connect")
    sw = Switch(64)
    for i in range(30):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.connect(("127.0.0.1", sw.port))
        s.close()
    # Wait until some connections were accepted, and every accepted one has been torn down and
    # nothing changes any more (a slow switch, e.g. the TSan build, may still be accepting).
    end = time.time() + 30
    stable_since, last = None, None
    while time.time() < end:
        now = (sw.count("host connected"), sw.count("teardown complete"))
        if now != last:
            last, stable_since = now, time.time()
        elif now[0] > 0 and now[0] == now[1] and time.time() - stable_since > 1.0:
            break
        time.sleep(0.02)
    check(sw.count("host connected") > 0 and sw.count("teardown complete") == sw.count("host connected"),
          "every connection that was accepted was torn down exactly once (%d)" % sw.count("host connected"))
    check(sw.p.poll() is None, "switch survived 30 reset connections")
    h = sw.host(ME[0])
    check(h is not None, "a normal host can still connect afterwards")
    send(h, "send bcast fine")
    check(sw.wait_log(": RX src=%s" % ME[0]), "and its frames are logged")
    check(sw.count("accept failed") == 0 and sw.count("poll failed") == 0, "no unexpected accept/poll errors")
    sw.stop()


def cpu_ticks(pid):
    f = open("/proc/%d/stat" % pid).read().rsplit(")", 1)[1].split()
    return int(f[11]) + int(f[12])


def test_emfile():
    print("switch: running out of file descriptors (EMFILE)")
    sw = Switch(64, nofile=12)
    clients = []
    for i in range(14):
        clients.append(connect(sw))
    check(sw.wait_log("accept failed: Too many open files", 5), "EMFILE is logged")
    check(sw.count("accept failed: Too many open files") == 1, "logged once, not once per retry")
    t0 = cpu_ticks(sw.p.pid)
    time.sleep(1.0)
    used = cpu_ticks(sw.p.pid) - t0
    ticks = os.sysconf("SC_CLK_TCK")
    check(used <= ticks * 0.15, "no busy loop: %d ticks of CPU in 1 s (limit %d)" % (used, int(ticks * 0.15)))
    check(sw.p.poll() is None, "switch still running")
    accepted = sw.count("host connected")
    check(0 < accepted < 14, "some connections accepted, the rest waiting (%d of 14)" % accepted)
    for c in clients[:5]:                       # these hosts leave: their receivers close their fds
        c.close()
    check(sw.wait_log("host connected", 5, accepted + 1), "accepting resumes once descriptors are free")
    for c in clients[5:]:
        c.close()
    check(sw.p.poll() is None, "switch alive at the end")
    sw.stop()


def test_threads_exit_clean():
    print("switch: many hosts come and go")
    sw = Switch(40)
    hs = [sw.host(ME[i]) for i in range(12)]
    check(all(h is not None for h in hs), "12 hosts connected")
    check(len([t for t in sw.threads() if t.startswith("rx-port")]) == 12, "12 receiver threads")
    for h in hs:
        send(h, "quit")
    check(sw.wait_log("host disconnected", 10, 12), "12 disconnects logged")
    end = time.time() + 5
    while time.time() < end and [t for t in sw.threads() if t.startswith(("rx-port", "tx-port"))]:
        time.sleep(0.05)
    check([t for t in sw.threads() if t.startswith("tx-port")] == [], "all sender threads have exited too")
    check([t for t in sw.threads() if t.startswith("rx-port")] == [], "all receiver threads have exited (none left over)")
    check(sw.threads().count("accept") == 1 and sw.p.poll() is None, "accept thread and switch still alive")
    nfd = len(os.listdir("/proc/%d/fd" % sw.p.pid))
    check(nfd <= 6, "no descriptors leaked: %d open" % nfd)
    sw.stop()


if __name__ == "__main__":
    if not os.path.exists(shlex.split(SWITCH)[-1]) or not os.path.exists(shlex.split(HOST)[-1]):
        sys.exit("switch or host binary not found")
    for t in [test_arguments, test_three_hosts, test_partial, test_disconnects, test_raw_fields,
              test_port_full, test_flood_log, test_socket_flags, test_reset_before_accept,
              test_emfile, test_threads_exit_clean]:
        t()
    print("\n%d checks, %d failures" % (passed + failed, failed))
    print("ALL PASSED" if failed == 0 else "SOME FAILED")
    sys.exit(0 if failed == 0 else 1)
