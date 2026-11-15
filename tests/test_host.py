#!/usr/bin/env python3
"""
Part 2 end-to-end tests for ./host.

A fake switch (a plain Python TCP server that knows the wire format by itself,
independently of frame.c) accepts the host and checks the exact bytes it sends,
or sends frames to the host and checks what the host prints.

Set HOST=./host_tsan (run with `setarch -R`) to test another binary.
"""
import os, shlex, signal, socket, struct, subprocess, sys, time

HOST = os.environ.get("HOST", "./host")
ME = "02:00:00:00:00:0a"
OTHER = "02:00:00:00:00:0b"
BCAST = "ff:ff:ff:ff:ff:ff"

passed = failed = 0


def check(cond, what):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", what)


def mac(s):
    return bytes(int(x, 16) for x in s.split(":"))


def frame(src, dst, vlan, prio, payload, length=None):
    n = len(payload) if length is None else length
    return struct.pack("!H", n) + mac(src) + mac(dst) + struct.pack("!HB", vlan, prio) + payload


def recv_exact(conn, n, timeout=5):
    conn.settimeout(timeout)
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    return data


def recv_frame(conn, timeout=5):
    """Return (src, dst, vlan, prio, payload) or None on EOF."""
    h = recv_exact(conn, 17, timeout)
    if len(h) < 17:
        return None
    n, = struct.unpack("!H", h[0:2])
    payload = recv_exact(conn, n, timeout)
    return (h[2:8], h[8:14], struct.unpack("!H", h[14:16])[0], h[16], payload)


class Fake:
    """Fake switch + a running host process."""

    def __init__(self, *opts, args=None, stdin=subprocess.PIPE):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        port = self.srv.getsockname()[1]
        cmd = shlex.split(HOST) + ["127.0.0.1", str(port), ME] if args is None else args
        cmd = cmd + list(opts)
        self.p = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=False)
        self.buf = b""
        self.srv.settimeout(5)
        self.conn, _ = self.srv.accept()

    def say(self, text):
        self.p.stdin.write(text.encode())
        self.p.stdin.flush()

    def out_line(self, timeout=5):
        """One line of host stdout (own buffer, so select() stays truthful)."""
        import select
        fd = self.p.stdout.fileno()
        deadline = time.time() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.time()
            if left <= 0:
                return None
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                return None
            chunk = os.read(fd, 65536)
            if not chunk:
                return None
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode() + "\n"

    def finish(self, timeout=5, wait_first=False):
        """Collect the result. wait_first: give the host time to exit by itself
        (e.g. after the switch hung up) before closing its stdin."""
        if wait_first:
            try:
                self.p.wait(timeout)
            except subprocess.TimeoutExpired:
                pass
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            rc = self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            rc = "timeout"
        out = self.buf.decode() + self.p.stdout.read().decode()
        err = self.p.stderr.read().decode()
        self.conn.close()
        self.srv.close()
        # a sanitizer or valgrind report in the host's stderr is a failure
        check("ThreadSanitizer" not in err and "Invalid read" not in err and
              "Invalid write" not in err and "uninitialised" not in err,
              "no sanitizer/valgrind report in host stderr: " + err[:200])
        return rc, out, err


# --------------------------------------------------------------------------
def test_send_commands():
    print("host: send / sendx / quit")
    f = Fake()
    f.say("send bcast hello world\n")
    check(recv_frame(f.conn) == (mac(ME), mac(BCAST), 1, 1, b"hello world"),
          "send bcast: header and payload")
    f.say("send %s hi there  two spaces\n" % OTHER)
    check(recv_frame(f.conn) == (mac(ME), mac(OTHER), 1, 1, b"hi there  two spaces"),
          "send unicast keeps inner spaces")
    f.say("sendx %s 10 0 urgent\n" % OTHER)
    check(recv_frame(f.conn) == (mac(ME), mac(OTHER), 10, 0, b"urgent"), "sendx vlan 10 prio 0")
    f.say("sendx bcast 4094 2 low\n")
    check(recv_frame(f.conn) == (mac(ME), mac(BCAST), 4094, 2, b"low"), "sendx max vlan, prio 2")
    f.say("sendx bcast 1 1 \n")
    check(recv_frame(f.conn) == (mac(ME), mac(BCAST), 1, 1, b""), "sendx empty text is a zero-length frame")
    f.say("send bcast\n")
    check(recv_frame(f.conn) == (mac(ME), mac(BCAST), 1, 1, b""), "send without text -> zero-length frame")
    f.say("send %s AA:BB:CC:DD:EE:FF\n" % "AA:BB:CC:DD:EE:FF")   # upper-case MAC accepted
    check(recv_frame(f.conn)[1] == mac("aa:bb:cc:dd:ee:ff"), "upper-case MAC accepted")
    text = "x" * 1500
    f.say("send bcast %s\n" % text)
    r = recv_frame(f.conn)
    check(r is not None and len(r[4]) == 1500, "1500-byte text is allowed")
    f.say("\n   \n")                                    # blank lines are ignored
    f.say("send bcast crlf\r\n")
    check(recv_frame(f.conn)[4] == b"crlf", "CRLF line ending stripped")
    f.say("quit\n")
    check(recv_frame(f.conn, 3) is None, "after quit the host closes the connection (EOF)")
    rc, out, err = f.finish()
    check(rc == 0, "quit -> exit code 0 (got %r)" % rc)
    check(out == "", "send commands print nothing on stdout")


def test_bad_commands():
    print("host: invalid commands are rejected and nothing is sent")
    f = Fake()
    bad = [
        "send\n", "send nothex hi\n", "send 02:00:00:00:00 hi\n",
        "sendx bcast 0 1 x\n", "sendx bcast 4095 1 x\n", "sendx bcast abc 1 x\n",
        "sendx bcast -1 1 x\n", "sendx bcast 10 3 x\n", "sendx bcast 10 -1 x\n",
        "sendx bcast 10 x y\n", "sendx bcast 10\n", "sendx bcast\n",
        "bogus command\n", "SEND bcast hi\n",
        "send bcast %s\n" % ("y" * 1501),              # too long
    ]
    for b in bad:
        f.say(b)
    f.say("send bcast marker\n")
    r = recv_frame(f.conn)
    check(r is not None and r[4] == b"marker", "first frame on the wire is the marker: bad commands sent nothing")
    f.say("quit\n")
    rc, out, err = f.finish()
    check(rc == 0, "host survived bad commands, exit 0 (got %r)" % rc)
    check(err.count("host:") >= len(bad), "an error message was printed for each bad command")
    check("too long" in err and "unknown command" in err, "error text mentions the problem")


def test_rx_format():
    print("host: receive output format")
    f = Fake()
    f.conn.sendall(frame(OTHER, ME, 10, 2, b"hello there"))
    check(f.out_line() == "RX src=%s dst=%s vlan=10 prio=2 len=11 data=hello there\n" % (OTHER, ME),
          "exact RX line")
    f.conn.sendall(frame(OTHER, BCAST, 4094, 0, b""))
    check(f.out_line() == "RX src=%s dst=%s vlan=4094 prio=0 len=0 data=\n" % (OTHER, BCAST),
          "zero-length payload prints empty data=")
    f.conn.sendall(frame(OTHER, ME, 1, 1, b"a\x01\xffb\\c\n"))
    check(f.out_line() == "RX src=%s dst=%s vlan=1 prio=1 len=7 data=a\\x01\\xffb\\\\c\\x0a\n" % (OTHER, ME),
          "unprintable bytes and backslash are escaped, one line per frame")
    # several frames in a single send
    f.conn.sendall(b"".join(frame(OTHER, ME, 1, 1, b"m%d" % i) for i in range(5)))
    lines = [f.out_line() for _ in range(5)]
    check(all(l and l.endswith("data=m%d\n" % i) for i, l in enumerate(lines)), "5 frames in one write -> 5 lines")
    # a frame dribbled in one byte at a time
    raw = frame(OTHER, ME, 3, 1, b"dribble")
    for i in range(len(raw)):
        f.conn.sendall(raw[i:i + 1])
        time.sleep(0.002)
    check(f.out_line() == "RX src=%s dst=%s vlan=3 prio=1 len=7 data=dribble\n" % (OTHER, ME),
          "frame arriving 1 byte at a time is printed once, complete")
    # frame split across the header/payload boundary
    raw = frame(OTHER, ME, 5, 1, b"split")
    f.conn.sendall(raw[:17]); time.sleep(0.05); f.conn.sendall(raw[17:])
    check(f.out_line().endswith("data=split\n"), "split at header/payload boundary")
    # max payload
    f.conn.sendall(frame(OTHER, ME, 1, 1, b"m" * 1500))
    l = f.out_line()
    check(l is not None and "len=1500 data=" + "m" * 1500 + "\n" in l, "1500-byte payload")
    f.say("quit\n")
    rc, out, err = f.finish()
    check(rc == 0 and out == "", "clean exit, nothing extra printed (rc=%r)" % rc)


def test_connection_events():
    print("host: switch closes / misbehaves")
    f = Fake()
    f.conn.close()
    rc, out, err = f.finish(wait_first=True)
    check(rc == 0 and "closed" in err, "clean EOF from switch -> exit 0 (rc=%r)" % rc)

    f = Fake()
    raw = frame(OTHER, ME, 1, 1, b"0123456789")
    f.conn.sendall(raw[:12]); f.conn.close()
    rc, out, err = f.finish(wait_first=True)
    check(rc == 1 and "truncated header" in err, "truncated header from switch -> exit 1 (rc=%r)" % rc)

    f = Fake()
    f.conn.sendall(raw[:20]); f.conn.close()
    rc, out, err = f.finish(wait_first=True)
    check(rc == 1 and "truncated payload" in err, "truncated payload -> exit 1 (rc=%r)" % rc)

    f = Fake()
    f.conn.sendall(frame(OTHER, ME, 1, 1, b"", length=1501))
    rc, out, err = f.finish(wait_first=True)
    check(rc == 1 and "protocol" in err, "length 1501 from switch -> exit 1 (rc=%r)" % rc)


def test_switch_full():
    print("host: SWITCH FULL")
    f = Fake()
    f.conn.sendall(frame("00:00:00:00:00:00", BCAST, 1, 0, b"switch full"))
    f.conn.close()
    rc, out, err = f.finish(wait_first=True)
    check(rc == 2, "exit code 2 (got %r)" % rc)
    check(out == "SWITCH FULL\n", "prints SWITCH FULL (got %r)" % out)

    # a look-alike (different src) is an ordinary frame
    f = Fake()
    f.conn.sendall(frame(OTHER, BCAST, 1, 0, b"switch full"))
    check((f.out_line() or "").startswith("RX src=%s" % OTHER), "look-alike frame is just a normal frame")
    f.say("quit\n")
    rc, out, err = f.finish()
    check(rc == 0, "look-alike does not exit with 2")


def test_partial():
    print("host: --partial")
    f = Fake("--partial")
    f.say("send bcast slow\n")
    t0 = time.time()
    f.conn.settimeout(5)
    sizes, data = [], b""
    while len(data) < 17 + 4:
        c = f.conn.recv(100)
        if not c:
            break
        sizes.append(len(c))
        data += c
    dt = time.time() - t0
    check(data == frame(ME, BCAST, 1, 1, b"slow"), "bytes are still a correct frame")
    check(dt >= 0.015, "takes >= ~21 ms for 21 bytes at 1 ms each (took %.3f s)" % dt)
    check(len(sizes) >= 10, "arrives in many small pieces (%d recv calls)" % len(sizes))
    f.say("quit\n")
    rc, out, err = f.finish()
    check(rc == 0, "exit 0")


def test_flood():
    print("host: --flood N --to MAC")
    f = Fake("--flood", "1000", "--to", OTHER)
    ok = True
    for i in range(1000):
        r = recv_frame(f.conn)
        if r != (mac(ME), mac(OTHER), 1, 1, b"flood %d" % i):
            ok = False
            break
    check(ok, "1000 correct frames, in order")
    time.sleep(0.3)
    check(f.p.poll() is None, "host waits for stdin EOF after flooding")
    f.conn.settimeout(0.3)
    try:
        extra = f.conn.recv(10)
    except socket.timeout:
        extra = b"timeout"
    check(extra == b"timeout", "no extra frames after the flood")
    rc, out, err = f.finish()
    check(rc == 0, "stdin EOF -> exit 0 (got %r)" % rc)

    f = Fake("--flood", "0", "--to", "bcast")
    time.sleep(0.2)
    check(f.p.poll() is None, "--flood 0 sends nothing and waits")
    rc, out, err = f.finish()
    check(rc == 0, "--flood 0 exits 0")


def test_noread():
    print("host: --noread")
    f = Fake("--noread")
    for i in range(20):
        f.conn.sendall(frame(OTHER, ME, 1, 1, b"ignored %d" % i))
    time.sleep(0.3)
    check(f.out_line(0.2) is None, "received frames are not read or printed")
    f.say("send bcast still-sending\n")
    check((recv_frame(f.conn) or (0, 0, 0, 0, b""))[4] == b"still-sending", "can still send")
    # the sockets receive buffer is not drained: keep pushing until our side blocks
    f.conn.setblocking(False)
    big = frame(OTHER, ME, 1, 1, b"b" * 1500)
    sent = 0
    try:
        for _ in range(200000):
            sent += f.conn.send(big)
    except BlockingIOError:
        pass
    check(sent > 0 and f.p.poll() is None, "the host never drains the socket, but stays alive")
    f.conn.setblocking(True)
    f.say("quit\n")
    rc, out, err = f.finish()
    check(rc == 0 and out == "", "exits 0, prints nothing (rc=%r)" % rc)


def test_die_midframe():
    print("host: --die-midframe")
    f = Fake("--die-midframe", stdin=subprocess.DEVNULL)
    data = b""
    f.conn.settimeout(5)
    while True:
        c = f.conn.recv(100)
        if not c:
            break
        data += c
    full = frame(ME, BCAST, 1, 1, b"half-frame-payload")
    check(len(data) == len(full) // 2 and data == full[:len(data)], "exactly half a frame, then EOF (got %d of %d bytes)" % (len(data), len(full)))
    check(0 < len(data) < len(full), "a real partial frame")
    rc, out, err = f.finish()
    check(rc == 0, "host exits (0), got %r" % rc)


def test_quiet():
    print("host: --quiet")
    f = Fake("--quiet")
    f.conn.sendall(b"".join(frame(OTHER, ME, 1, 1, b"n%d" % i) for i in range(50)))
    time.sleep(0.5)
    check(f.out_line(0.2) is None, "no RX lines while running")
    f.say("quit\n")
    rc, out, err = f.finish()
    check(out == "RX_TOTAL 50\n", "prints the total on exit (got %r)" % out)
    check(rc == 0, "exit 0")

    f = Fake("--quiet")
    rc, out, err = f.finish()
    check(out == "RX_TOTAL 0\n", "total 0 when nothing arrived (got %r)" % out)


def test_sigpipe():
    print("host: SIGPIPE is ignored")
    f = Fake("--noread")
    # close the switch side with a reset, then keep sending
    f.conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    f.conn.close()
    time.sleep(0.1)
    for _ in range(5):
        try:
            f.say("send bcast after-close\n")
        except BrokenPipeError:
            break
        time.sleep(0.05)
    rc, out, err = f.finish()
    check(rc != -signal.SIGPIPE, "host was not killed by SIGPIPE (rc=%r)" % rc)
    check(rc == 1, "host exits with code 1 after a failed send (rc=%r)" % rc)
    check("send failed" in err, "error message printed: %r" % err.strip()[:80])


def run_plain(args, stdin=subprocess.DEVNULL):
    p = subprocess.run(shlex.split(HOST) + args, stdin=stdin, capture_output=True, timeout=10)
    return p.returncode, p.stdout.decode(), p.stderr.decode()


def test_arguments():
    print("host: command-line errors")
    port = "9"
    cases = {
        "no arguments": [],
        "too few arguments": ["127.0.0.1", port],
        "bad ip": ["999.1.1.1", port, ME],
        "ip not a number": ["localhost", port, ME],
        "port 0": ["127.0.0.1", "0", ME],
        "port too big": ["127.0.0.1", "65536", ME],
        "port not a number": ["127.0.0.1", "80x", ME],
        "bad mac": ["127.0.0.1", port, "02:00:00:00:00:0z"],
        "short mac": ["127.0.0.1", port, "02:00:00:00:00"],
        "unknown option": ["127.0.0.1", port, ME, "--bogus"],
        "--flood without --to": ["127.0.0.1", port, ME, "--flood", "5"],
        "--to without --flood": ["127.0.0.1", port, ME, "--to", OTHER],
        "--flood without number": ["127.0.0.1", port, ME, "--flood"],
        "--flood not a number": ["127.0.0.1", port, ME, "--flood", "abc", "--to", OTHER],
        "--flood negative": ["127.0.0.1", port, ME, "--flood", "-3", "--to", OTHER],
        "--to bad mac": ["127.0.0.1", port, ME, "--flood", "1", "--to", "nope"],
    }
    for name, args in cases.items():
        rc, out, err = run_plain(args)
        check(rc == 1 and ("host:" in err or "usage" in err), "%s -> exit 1 (rc=%r)" % (name, rc))
        check(out == "", "%s -> nothing on stdout" % name)
    # connection refused: find a closed port
    s = socket.socket(); s.bind(("127.0.0.1", 0)); closed = s.getsockname()[1]; s.close()
    rc, out, err = run_plain(["127.0.0.1", str(closed), ME])
    check(rc == 1 and "connect" in err, "connection refused -> exit 1 with message (rc=%r)" % rc)


def test_stdin_eof():
    print("host: end of stdin behaves like quit")
    f = Fake()
    f.say("send bcast last\n")
    check(recv_frame(f.conn)[4] == b"last", "frame sent")
    rc, out, err = f.finish()      # closes stdin
    check(rc == 0, "EOF on stdin -> exit 0 (rc=%r)" % rc)


if __name__ == "__main__":
    if not os.path.exists(shlex.split(HOST)[-1]):
        sys.exit("host binary not found: " + HOST)
    for t in [test_send_commands, test_bad_commands, test_rx_format, test_connection_events,
              test_switch_full, test_partial, test_flood, test_noread, test_die_midframe,
              test_quiet, test_sigpipe, test_arguments, test_stdin_eof]:
        t()
    print("\n%d checks, %d failures" % (passed + failed, failed))
    print("ALL PASSED" if failed == 0 else "SOME FAILED")
    sys.exit(0 if failed == 0 else 1)
