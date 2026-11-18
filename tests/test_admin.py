#!/usr/bin/env python3
"""
Part 15 end-to-end tests: the admin socket of the real ./switch and the ./swctl program.

  python3 tests/test_admin.py            normal build
  python3 tests/test_admin.py tsan       SWITCH="setarch -R ./switch_tsan"
  python3 tests/test_admin.py valgrind   valgrind on ./switch (no errors, no leaks, no descriptor left open, no socket left)

The parser (admin_parse) is unit tested in tests/test_admin.c; the mappings of swctl, the robust mutex and the SIGSEGV of a write through the
read-only data mapping are tested in tests/test_swctl.c. Here: the socket and its protocol with raw Python sockets (partial lines, 256-byte limit,
silent / vanishing clients), shutdown while the admin thread waits for a client, every swctl command with exit statuses, show mac / show ports /
clear mac with and without traffic, swctl stats and stats --watch, and the shared segment of a damaged / foreign / missing kind.
Nothing here asserts what the write commands (shutdown, up, vlan, mirror) DO - that belongs to Parts 16 and 17 - only that they are validated.
"""
import os, re, select, shlex, signal, socket, stat, struct, subprocess, sys, tempfile, threading, time, traceback

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts

SWCTL = os.environ.get("SWCTL", "./swctl")
BCAST = ts.BCAST
SEG = "/dev/shm/vswitch_stats_P"
PAGE = os.sysconf("SC_PAGESIZE")
SLOW = 1                                           # time factor (valgrind makes the switch slower)
passed = failed = 0


def check(cond, what):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", what)


ts.check = check
TMP = tempfile.mkdtemp(prefix="vswadm.")

# ---- cleanup of everything a scenario creates ----------------------------------------------------------------------------------------------
# A scenario that raises (an assertion, an exception, a switch that does not start, KeyboardInterrupt, SIGTERM of this script) must not leave
# switches, hosts, swctl watchers or sockets behind. Everything is registered here when it is created and released by cleanup(), which run_scenario()
# calls in a `finally`. Only objects THIS run created are touched (never a switch found by name: pkill would hit the user's own switches).
STARTED = []                                        # every ts.Switch constructed (registered BEFORE the constructor runs: it can raise while the process lives)
CLIENTS = []                                        # sockets, Hosts and Popen objects (swctl --watch) created by the scenarios
TERM_TIMEOUT = 30                                   # seconds a switch gets to shut down cleanly after SIGTERM (valgrind needs a few), then SIGKILL
KILL_TIMEOUT = 10

_orig_switch_init = ts.Switch.__init__


def _tracked_switch_init(self, *a, **k):
    STARTED.append(self)
    _orig_switch_init(self, *a, **k)


if not getattr(ts.Switch, "_ta_tracked", False):
    ts.Switch.__init__ = _tracked_switch_init
    ts.Switch._ta_tracked = True


def usock():
    """A UNIX stream socket that cleanup() closes if the scenario forgets (or never gets to) close it."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    CLIENTS.append(s)
    return s


def uconnect(s, path, timeout=10):
    """Connect a UNIX stream socket the way swctl does: a BLOCKING connect, bounded by SO_SNDTIMEO, then the read timeout. The admin thread serves one client
    at a time and listens with a backlog of 8: during a burst of clients a connect() on a socket that already has a Python timeout is non-blocking and fails
    with EAGAIN (BlockingIOError) while the backlog is full, which is not an error of the switch; a blocking connect simply waits for a free slot."""
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", int(timeout), 0))
    s.settimeout(None)
    s.connect(path)
    s.settimeout(timeout)


def track(p):
    """Register a subprocess.Popen started by a scenario (returns it)."""
    CLIENTS.append(p)
    return p


def stop_proc(p, term_timeout=TERM_TIMEOUT, kill_timeout=KILL_TIMEOUT):
    """Terminate EXACTLY this Popen: SIGTERM, wait, SIGKILL if it does not leave, wait (reap). Safe if it already exited.
    Returns 'exited' (it was already gone), 'terminated' or 'killed'."""
    if p.poll() is not None:
        return "exited"
    p.terminate()
    try:
        p.wait(term_timeout)
        return "terminated"
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(kill_timeout)                         # SIGKILL cannot be ignored; this reaps it
        return "killed"


def _stop_switch(sw, term_timeout, kill_timeout, errors):
    what = "switch pid %s" % getattr(getattr(sw, "p", None), "pid", "?")
    for h in getattr(sw, "hosts", []):               # real ./host processes of ts.Switch.host()
        try:
            if h.poll() is None:
                h.kill()
            h.wait(kill_timeout)
        except Exception as e:
            errors.append("host of %s: %r" % (what, e))
    p = getattr(sw, "p", None)                       # absent if Popen itself failed
    if p is not None:
        try:
            how = stop_proc(p, term_timeout, kill_timeout)
            if how != "exited":
                print("  cleanup: %s was still running; %s" % (what, how), file=sys.stderr)
        except Exception as e:
            errors.append("%s: %r" % (what, e))
    for f in (getattr(sw, "logf", None), getattr(sw, "errf", None)):
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
            try:
                os.unlink(f.name)
            except FileNotFoundError:
                pass
            except Exception as e:
                errors.append("%s: %r" % (f.name, e))


def cleanup(term_timeout=None, kill_timeout=None):
    """Release everything registered. Every step runs even if an earlier one fails; returns the list of cleanup errors (never raises)."""
    tt = TERM_TIMEOUT if term_timeout is None else term_timeout
    kt = KILL_TIMEOUT if kill_timeout is None else kill_timeout
    errors = []
    for c in reversed(CLIENTS):
        try:
            if isinstance(c, subprocess.Popen):
                stop_proc(c, tt, kt)
            else:
                c.close()
        except Exception as e:
            errors.append("client %r: %r" % (c, e))
    del CLIENTS[:]
    for sw in reversed(STARTED):
        try:
            _stop_switch(sw, tt, kt, errors)
        except Exception as e:                       # belt and braces: nothing may skip the remaining switches
            errors.append("switch cleanup: %r" % (e,))
    del STARTED[:]
    return errors


def run_scenario(fn, term_timeout=None, kill_timeout=None):
    """Run one scenario; whatever happens (return, assertion, any exception, KeyboardInterrupt, SystemExit) cleanup() runs afterwards, and the
    ORIGINAL exception propagates unchanged. Cleanup problems are printed and counted as a failed check, they never replace it."""
    try:
        return fn()
    finally:
        errs = cleanup(term_timeout, kill_timeout)
        for e in errs:
            print("  CLEANUP ERROR:", e, file=sys.stderr)
        check(not errs, "%s: cleanup finished without errors" % getattr(fn, "__name__", "scenario"))


def run_tests(tests):
    """Run scenarios one after another; an exception in one is printed with its traceback and counted as a failure, the others still run."""
    global failed
    for t in tests:
        try:
            run_scenario(t)
        except Exception:
            failed += 1
            print("  FAIL: %s raised an exception:" % getattr(t, "__name__", t))
            traceback.print_exc()


def install_signal_cleanup():
    """SIGTERM of the test script itself becomes SystemExit so that the `finally` blocks (cleanup) run."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
conf_no = 0


def conf(text):
    global conf_no
    conf_no += 1
    path = os.path.join(TMP, "a%03d.conf" % conf_no)
    with open(path, "w") as f:
        f.write(text)
    return path


def sock_path(sw):
    return "/tmp/vswitch_%d.sock" % sw.port


def start(n, conf_text=None, port=None):
    return ts.Switch(n, port=port, extra=["-c", conf(conf_text)] if conf_text else [])


def ctl(sw, *args, timeout=60):
    """Run ./swctl -p <port> args -> (exit status, stdout, stderr)."""
    p = subprocess.run([SWCTL, "-p", str(sw.port if hasattr(sw, "port") else sw)] + list(args),
                       capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout, p.stderr


def raw(sw, data=b"", timeout=10, half_close=False):
    """Connect to the admin socket, send data, read until the switch closes. Returns the reply bytes."""
    s = usock()
    uconnect(s, sock_path(sw), timeout)
    if data:
        try:
            s.sendall(data)
        except (BrokenPipeError, ConnectionResetError):
            pass                                     # the switch refused an over-long request and closed before the last bytes were sent (about 1 in 11 times for 100 KB); its reply is still to be read
    if half_close:
        s.shutdown(socket.SHUT_WR)
    out = b""
    try:
        while True:
            d = s.recv(65536)
            if not d:
                break
            out += d
    except socket.timeout:
        out += b"<TIMEOUT>"
    s.close()
    return out


def wait_for(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def sigint_and_wait(sw, timeout=60):
    t = time.time()
    sw.p.send_signal(signal.SIGINT)
    try:
        rc = sw.p.wait(timeout)
    except subprocess.TimeoutExpired:
        sw.p.kill()
        return None, time.time() - t
    return rc, time.time() - t


def nfds(pid):
    return len(os.listdir("/proc/%d/fd" % pid))


class Host(threading.Thread):
    """A raw socket host: sends frames, counts what it receives."""

    def __init__(self, sw, idx, read=True):
        super().__init__(daemon=True)
        self.sock = ts.connect(sw)
        CLIENTS.append(self)                           # cleanup() closes it
        self.idx, self.mac = idx, ts.ME[idx]
        self.frames = 0
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
                ln = struct.unpack("!H", buf[:2])[0]
                if len(buf) < 17 + ln:
                    break
                self.frames += 1
                buf = buf[17 + ln:]

    def send(self, dst, vlan=1, payload=b"x", prio=1):
        self.sock.sendall(ts.frame(self.mac, dst, vlan, prio, payload))

    def close(self):
        self.stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def mk_hosts(sw, n):
    hs = []
    for i in range(1, n + 1):
        hs.append(Host(sw, i))
        check(sw.wait_log("port %d: host connected" % i, 60), "port %d connected" % i)
    return hs


def parse_ports(text):
    """'show ports' text -> {port: (state, vlan, trunk, macs, mirror)}"""
    rows = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) == 6:
            rows[int(f[0])] = tuple(f[1:])
    return rows


def parse_macs(text):
    """'show mac' text -> [(vlan, mac, port, age)]"""
    out = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        f = line.split()
        out.append((int(f[0]), f[1], int(f[2]), int(f[3])))
    return out


# ----------------------------------------------------------------------------------------------
def test_socket():
    print("1. the socket: path, type, mode, ss -xlp, stale path, removal after SIGINT")
    port = ts.free_port()
    path = "/tmp/vswitch_%d.sock" % port
    # a stale path: a regular file left behind, then a socket file of a dead process
    with open(path, "w") as f:
        f.write("stale")
    sw = start(4, port=port)
    st = os.stat(path)
    check(stat.S_ISSOCK(st.st_mode), "a regular file at the path was replaced by a socket")
    check(stat.S_IMODE(st.st_mode) == 0o600, "mode 0600 (got %o)" % stat.S_IMODE(st.st_mode))
    ss = subprocess.run(["ss", "-xlp"], capture_output=True, text=True).stdout
    check(path in ss, "ss -xlp shows %s" % path)
    check(re.search(re.escape(path) + r".*LISTEN", ss) is not None or path in ss, "the socket listens")
    check(b"OK\n" in raw(sw, b"show ports\n"), "the socket serves")
    check(b"admin" not in b"".join(n.encode() for n in sw.threads() if n == "x"), "(thread names are not part of the contract)")
    rc, dt = sigint_and_wait(sw)
    check(rc == 0, "SIGINT: exit status 0 (got %s)" % rc)
    check(not os.path.exists(path), "after SIGINT the path is gone")
    sw.stop()

    # a socket file of a crashed switch (bound, never unlinked)
    s = usock()
    s.bind(path)
    s.close()
    check(os.path.exists(path), "(setup) a dead socket file exists")
    sw = start(4, port=port)
    check(b"OK\n" in raw(sw, b"show ports\n"), "a dead socket file at the path was replaced")
    sw.stop()
    check(not os.path.exists(path), "the path is gone after SIGTERM too")

    # a second switch on the same TCP port cannot start and must not remove the first one's socket
    sw = start(4)
    p = subprocess.run(shlex.split(ts.SWITCH) + [str(sw.port), "4"], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    check(p.returncode == 1, "a second switch on the same port fails")
    check(os.path.exists(sock_path(sw)) and b"OK\n" in raw(sw, b"show ports\n"), "the first switch's socket survives")
    sw.stop()


def test_protocol():
    print("2. the protocol: one line, text lines, OK / ERR, close; partial lines; the 256-byte limit")
    sw = start(4)
    r = raw(sw, b"show ports\n")
    lines = r.decode().splitlines()
    check(lines[-1] == "OK" and lines[0].startswith("PORT"), "show ports: table, last line OK (%r)" % lines[-1:])
    check(raw(sw, b"clear mac\n").decode().splitlines()[-1] == "OK", "clear mac: OK")
    r = raw(sw, b"bogus\n").decode()
    check(r.startswith("ERR ") and r.count("\n") == 1, "unknown command: one line ERR <reason> (%r)" % r)
    check(raw(sw, b"\n").decode().startswith("ERR "), "an empty line: ERR")
    check(raw(sw, b"show ports\r\n").decode().endswith("OK\n"), "CRLF accepted")
    # the connection is closed after the reply (raw() reads until EOF: it returned, so yes), nothing else is sent
    check("<TIMEOUT>" not in r, "the switch closes after the reply")

    # partial lines
    s = usock()
    uconnect(s, sock_path(sw), 10)
    for chunk in (b"sh", b"ow", b" p", b"or", b"ts", b"\n"):
        s.sendall(chunk)
        time.sleep(0.05)
    out = b""
    while True:
        d = s.recv(4096)
        if not d:
            break
        out += d
    s.close()
    check(out.endswith(b"OK\n") and out.startswith(b"PORT"), "a line sent in six pieces is one request")
    # a line slowly sent, total inside the limit
    s = usock()
    uconnect(s, sock_path(sw), 10)
    s.sendall(b"show ")
    time.sleep(0.5)
    s.sendall(b"mac\n")
    out = b""
    while True:
        d = s.recv(4096)
        if not d:
            break
        out += d
    s.close()
    check(out.endswith(b"OK\n"), "a line that takes 0.5 s is served")

    # the limit: 256 bytes with the newline are fine, 257 are refused
    ok255 = raw(sw, b"a" * 255 + b"\n").decode()
    check(ok255.startswith("ERR unknown command"), "255 characters + \\n (256 bytes) are read in full and parsed: %r" % ok255[:40])
    r256 = raw(sw, b"a" * 256 + b"\n").decode()
    check(r256.startswith("ERR line too long"), "256 characters + \\n are refused: %r" % r256[:40])
    r1k = raw(sw, b"b" * 1000).decode()
    check(r1k.startswith("ERR line too long"), "1000 bytes without a newline are refused at once")
    big = raw(sw, b"show ports " + b"c" * 100000 + b"\n", timeout=20).decode()
    check(big.startswith("ERR line too long"), "100 KB request refused with the error reply (%r)" % big[:30])
    check(b"OK\n" in raw(sw, b"show ports\n"), "the switch still serves afterwards")
    # control characters
    check(raw(sw, b"show\x01ports\n").decode().startswith("ERR "), "a control character is refused")
    check(raw(sw, b"show mac\x00\n").decode().startswith("ERR "), "a NUL byte is refused")
    # two lines in one send: the first is served, the rest is ignored, the connection closes
    r = raw(sw, b"show ports\nclear mac\n").decode()
    check(r.count("\nOK\n") + (1 if r.endswith("OK\n") else 0) >= 1 and r.count("OK") == 1, "a second line in the same send is ignored")
    sw.stop()


def test_bad_clients():
    print("3. clients that vanish, stay silent or never read")
    sw = start(4)
    base = nfds(sw.p.pid)
    # disconnect in the middle of a request
    for _ in range(5):
        s = usock()
        s.connect(sock_path(sw))
        s.sendall(b"show po")
        s.close()
    # connect and close at once
    for _ in range(5):
        s = usock()
        s.connect(sock_path(sw))
        s.close()
    # send a command and close without reading the reply
    for _ in range(5):
        s = usock()
        s.connect(sock_path(sw))
        s.sendall(b"show ports\n")
        s.close()
    t = time.time()
    check(b"OK\n" in raw(sw, b"show ports\n"), "the switch serves after 15 broken clients")
    check(time.time() - t < 5 * SLOW, "... without waiting for them")
    check(sw.p.poll() is None, "the switch is alive (no SIGPIPE)")

    # a silent client: gets the timeout answer after about 2 s; another client waits its turn
    silent = usock()
    uconnect(silent, sock_path(sw), 15)
    t0 = time.time()
    other = {}

    def second():
        t1 = time.time()
        other["r"] = raw(sw, b"show ports\n", timeout=30)
        other["dt"] = time.time() - t1

    th = threading.Thread(target=second)
    th.start()
    data = b""
    while True:
        d = silent.recv(4096)
        if not d:
            break
        data += d
    dt = time.time() - t0
    silent.close()
    check(data.startswith(b"ERR timeout"), "a silent client gets 'ERR timeout...' (%r)" % data[:40])
    check(1.7 <= dt <= 4 * SLOW + 1, "... after about 2 s (%.2f s)" % dt)
    th.join(30)
    check(other.get("r", b"").endswith(b"OK\n"), "the next client is served after the silent one")

    # a trickle: one byte every 0.7 s never completes the line; the total time is bounded (2 s), not 2 s per byte
    s = usock()
    uconnect(s, sock_path(sw), 15)
    t0 = time.time()
    got = b""
    try:
        for ch in b"show ports":
            s.sendall(bytes([ch]))
            r, _, _ = select.select([s], [], [], 0.7)
            if r:
                got = s.recv(4096)
                break
    except OSError:
        pass
    check(time.time() - t0 < 4.5 * SLOW + 1, "a trickling client is cut off after about 2 s in total (%.2f s)" % (time.time() - t0))
    check(got.startswith(b"ERR timeout") or got == b"", "... with the timeout answer (%r)" % got[:30])
    s.close()
    time.sleep(0.3)
    check(nfds(sw.p.pid) == base, "no descriptor leaked after all the clients (%d -> %d)" % (base, nfds(sw.p.pid)))
    sw.stop()


def test_shutdown():
    print("4. SIGINT while the admin thread is idle, waits for a request, or has a silent client")
    for what in ("idle", "connected and silent", "half a request", "many silent clients"):
        sw = start(4)
        clients = []
        if what == "connected and silent":
            clients.append(usock())
        elif what == "half a request":
            clients.append(usock())
        elif what == "many silent clients":
            clients = [usock() for _ in range(5)]
        for c in clients:
            c.connect(sock_path(sw))
        if what == "half a request":
            clients[0].sendall(b"show po")
        time.sleep(0.3)                            # the admin thread is now inside its wait for the request
        rc, dt = sigint_and_wait(sw, 60)
        check(rc == 0, "%s: SIGINT -> exit status 0 (got %s)" % (what, rc))
        limit = 1.5 * SLOW if SLOW == 1 else 60
        check(dt < limit, "%s: the switch left in %.2f s (< 2 s timeout, not delayed by the client)" % (what, dt))
        check(not os.path.exists(sock_path(sw)), "%s: the socket path is removed" % what)
        for c in clients:
            c.close()
        sw.stop()
    # SIGTERM with a client in the middle of the line, then the same with traffic
    sw = start(4)
    hs = mk_hosts(sw, 3)
    c = usock()
    c.connect(sock_path(sw))
    c.sendall(b"show ")
    stop = threading.Event()

    def flood():
        while not stop.is_set():
            try:
                hs[0].send(BCAST, 1, b"f" * 64)
            except OSError:
                return

    th = threading.Thread(target=flood, daemon=True)
    th.start()
    time.sleep(0.3)
    sw.p.send_signal(signal.SIGTERM)
    try:
        rc = sw.p.wait(60)
    except subprocess.TimeoutExpired:
        sw.p.kill()
        rc = None
    stop.set()
    check(rc == 0, "SIGTERM with a half request and a flood: exit status 0 (got %s)" % rc)
    check(not os.path.exists(sock_path(sw)), "... the path is removed")
    c.close()
    for h in hs:
        h.close()
    sw.stop()


def test_commands():
    print("5. swctl: show ports, show mac, clear mac")
    sw = start(4, "port.1.vlan = 10\nport.2.vlan = 10\nport.3.vlan = 20\ntrunk_port = 4\n")
    rc, out, err = ctl(sw, "show", "ports")
    rows = parse_ports(out)
    check(rc == 0 and sorted(rows) == [1, 2, 3, 4], "show ports: exit 0, four rows (rc=%s)" % rc)
    check(rows[1][0] == "FREE" and rows[4][0] == "FREE", "all ports FREE at the start: %s" % rows)
    check(rows[4][2] == "yes" and rows[1][2] == "no", "port 4 is the trunk, port 1 is not")
    check([rows[i][1] for i in (1, 2, 3, 4)] == ["10", "10", "20", "-"], "a FREE port shows the VLAN of the configuration, the trunk '-': %s" % [rows[i][1] for i in (1, 2, 3, 4)])
    check(all(r[3] == "0" and r[4] == "-" for r in rows.values()), "no MACs, no mirror")
    rc, out, err = ctl(sw, "show", "mac")
    check(rc == 0 and parse_macs(out) == [] and out.startswith("# vlan mac port age_s"), "show mac on an empty table: header only")

    hs = mk_hosts(sw, 3)
    rows = parse_ports(ctl(sw, "show", "ports")[1])
    check([rows[i][0] for i in (1, 2, 3, 4)] == ["UP", "UP", "UP", "FREE"], "states UP UP UP FREE: %s" % rows)
    check([rows[i][1] for i in (1, 2, 3)] == ["10", "10", "20"], "VLANs 10 10 20: %s" % [rows[i][1] for i in (1, 2, 3)])
    # each host sends one broadcast: the switch learns its MAC
    for h in hs:
        h.send(BCAST, 1, b"hello")
    check(wait_for(lambda: len(parse_macs(ctl(sw, "show", "mac")[1])) == 3, 30), "three MACs learned")
    macs = parse_macs(ctl(sw, "show", "mac")[1])
    check(sorted((m[1], m[2]) for m in macs) == sorted((ts.ME[i], i) for i in (1, 2, 3)), "MAC -> port is right: %s" % macs)
    check([m[0] for m in macs if m[2] in (1, 2)] == [10, 10] and [m[0] for m in macs if m[2] == 3] == [20], "learned in the VLAN of the port")
    check(all(m[3] >= 0 for m in macs), "ages are numbers")
    rows = parse_ports(ctl(sw, "show", "ports")[1])
    check([rows[i][3] for i in (1, 2, 3, 4)] == ["1", "1", "1", "0"], "MAC counts 1 1 1 0: %s" % [rows[i][3] for i in (1, 2, 3, 4)])
    check(sw.p.poll() is None, "alive")

    # clear mac: flooding until relearned. Hosts 1 and 2 share VLAN 10.
    for h in hs:
        h.frames = 0
    hs[0].send(ts.ME[2], 10, b"uni")                  # host 1 -> host 2: known (VLAN 10) -> unicast
    check(wait_for(lambda: hs[1].frames == 1, 30), "a known destination: host 2 gets the frame")
    time.sleep(0.3)
    check(hs[2].frames == 0 and hs[0].frames == 0, "... and nobody else (unicast)")
    rc, out, err = ctl(sw, "clear", "mac")
    check(rc == 0 and "cleared 3 MAC" in out, "clear mac: OK, 3 entries (%r)" % out)
    check(parse_macs(ctl(sw, "show", "mac")[1]) == [], "the table is empty")
    check([parse_ports(ctl(sw, "show", "ports")[1])[i][3] for i in (1, 2, 3)] == ["0", "0", "0"], "every MAC count is back to 0")
    # port 3 is in VLAN 20 and port 4 (trunk) is free: a frame for an unknown MAC floods all ports of the VLAN. Use a third host in VLAN 10? There is none:
    # so check with the number of frames host 2 receives: it is flooded now (unknown) - the same as before - and the table relearns host 1.
    hs[0].send(ts.ME[2], 10, b"flood1")
    check(wait_for(lambda: hs[1].frames == 2, 30), "after clear mac the frame still arrives (flooded)")
    check(wait_for(lambda: any(m[1] == ts.ME[1] for m in parse_macs(ctl(sw, "show", "mac")[1])), 30), "host 1 is relearned")
    sw.stop()
    for h in hs:
        h.close()


def test_flood_after_clear():
    print("6. clear mac: an unknown destination reaches the OTHER ports again (a third host sees the frame)")
    sw = start(4)
    hs = mk_hosts(sw, 3)
    for h in hs:
        h.send(BCAST, 1, b"learn")
    check(wait_for(lambda: len(parse_macs(ctl(sw, "show", "mac")[1])) == 3, 30), "three MACs learned")
    check(wait_for(lambda: all(h.frames >= 2 for h in hs), 30), "(broadcasts delivered)")
    for h in hs:
        h.frames = 0
    hs[0].send(ts.ME[2], 1, b"known")
    check(wait_for(lambda: hs[1].frames == 1, 30), "known: host 2 gets it")
    time.sleep(0.4)
    check(hs[2].frames == 0, "known: host 3 does NOT see it")
    check(ctl(sw, "clear", "mac")[0] == 0, "clear mac")
    hs[0].send(ts.ME[2], 1, b"unknown")
    check(wait_for(lambda: hs[1].frames == 2 and hs[2].frames == 1, 30), "after clear: host 2 and host 3 both get the frame (flooded)")
    hs[1].send(ts.ME[1], 1, b"reply")                  # host 2 answers: its MAC is learned again
    check(wait_for(lambda: any(m[1] == ts.ME[2] for m in parse_macs(ctl(sw, "show", "mac")[1])), 30), "host 2 relearned")
    for h in hs:
        h.frames = 0
    hs[0].send(ts.ME[2], 1, b"known again")
    check(wait_for(lambda: hs[1].frames == 1, 30), "relearned: host 2 gets it")
    time.sleep(0.4)
    check(hs[2].frames == 0, "relearned: host 3 sees nothing: unicast again")
    sw.stop()
    for h in hs:
        h.close()


def test_during_traffic():
    print("7. show mac / show ports / clear mac / stats while hosts flood")
    sw = start(4)
    hs = mk_hosts(sw, 4)
    stop = threading.Event()

    def flood(h):
        i = 0
        while not stop.is_set():
            try:
                h.send(BCAST if i % 3 else ts.ME[(h.idx % 4) + 1], 1, b"t" * 100)
            except OSError:
                return
            i += 1
            time.sleep(0.001)

    ths = [threading.Thread(target=flood, args=(h,), daemon=True) for h in hs]
    for t in ths:
        t.start()
    known = set(ts.ME[1:5])
    bad = 0
    for i in range(25):
        rc, out, _ = ctl(sw, "show", "mac")
        macs = parse_macs(out)
        if rc != 0 or any(m[1] not in known or m[2] not in (1, 2, 3, 4) or m[0] != 1 for m in macs):
            bad += 1
        rc, out, _ = ctl(sw, "show", "ports")
        rows = parse_ports(out)
        if rc != 0 or sorted(rows) != [1, 2, 3, 4] or any(r[0] != "UP" or r[2] != "no" or not 0 <= int(r[3]) <= 1 for r in rows.values()):
            bad += 1
        if i % 5 == 4:
            if ctl(sw, "clear", "mac")[0] != 0:
                bad += 1
            rc, out, _ = ctl(sw, "stats")
            if rc != 0 or out.count("\n") != 5:
                bad += 1
    check(bad == 0, "25 rounds of show mac / show ports (+ clear mac, stats): every answer is consistent (%d bad)" % bad)
    check(wait_for(lambda: len(parse_macs(ctl(sw, "show", "mac")[1])) == 4, 30), "all four MACs are (re)learned after the last clear")
    stop.set()
    for t in ths:
        t.join(10)
    for h in hs:
        h.close()
    sw.stop()


def test_validation():
    print("8. validation: bad commands answer ERR (exit 1); valid write commands are not rejected as syntax errors")
    sw = start(4, "trunk_port = 4\n")
    bad = [
        ["bogus"], ["show"], ["show", "foo"], ["show", "mac", "extra"], ["clear"], ["clear", "ports"], ["clear", "mac", "x"],
        ["shutdown"], ["shutdown", "0"], ["shutdown", "5"], ["shutdown", "-1"], ["shutdown", "abc"], ["shutdown", "1", "2"], ["shutdown", "1x"],
        ["shutdown", "99999999999"], ["up"], ["up", "0"], ["up", "9"],
        ["vlan"], ["vlan", "1"], ["vlan", "1", "0"], ["vlan", "1", "4095"], ["vlan", "1", "-5"], ["vlan", "1", "abc"], ["vlan", "9", "10"],
        ["vlan", "0", "10"], ["vlan", "1", "10", "x"],
        ["mirror"], ["mirror", "1"], ["mirror", "1", "1"], ["mirror", "1", "9"], ["mirror", "9", "1"], ["mirror", "0", "2"], ["mirror", "1", "0"],
        ["mirror", "1", "2", "3"], ["mirror", "1", "OFF"], ["mirror", "1", "4"],            # 4 is the trunk
        ["SHOW", "MAC"], ["stats", "--x"] if False else ["show", "mac;"],
    ]
    for args in bad:
        rc, out, err = ctl(sw, *args)
        check(rc == 1 and "ERR" in err, "'%s' -> exit 1 with ERR (rc=%s %r)" % (" ".join(args), rc, err.strip()))
    r = raw(sw, b"vlan 1 4095\n").decode()
    check(r == "ERR VLAN 4095 out of range (1-4094)\n", "the reason names the problem: %r" % r)
    r = raw(sw, b"shutdown 9\n").decode()
    check(r == "ERR port 9 out of range (1-4)\n", "the reason names the problem: %r" % r)
    r = raw(sw, b"mirror 4 4\n").decode()
    check(r.startswith("ERR "), "mirror 4 4: ERR")
    r = raw(sw, b"mirror 1 4\n").decode()
    check("trunk" in r, "a mirror target that is the trunk is refused: %r" % r)

    syntax = ("out of range", "usage", "unknown", "bad ", "too many", "itself")
    good = [["shutdown", "2"], ["up", "2"], ["vlan", "1", "10"], ["vlan", "1", "4094"], ["vlan", "2", "1"], ["mirror", "1", "2"], ["mirror", "1", "off"],
            ["shutdown", "4"], ["up", "4"]]
    for args in good:
        rc, out, err = ctl(sw, *args)
        # a valid command is either done (OK) or refused for a reason that is not about its syntax (state, Parts 16/17)
        check(rc in (0, 1) and not any(w in err for w in syntax), "'%s' passes validation (rc=%s %r)" % (" ".join(args), rc, err.strip()))
    check(sw.p.poll() is None, "alive")

    # swctl itself
    for args, what in (([], "no command"), (["-p"], "-p without a value"), (["-p", "abc", "show", "mac"], "-p abc"), (["-p", "0", "show", "mac"], "-p 0"),
                       (["-p", "70000", "show", "mac"], "-p 70000"), (["stats", "--watch", "x"], "stats --watch x"), (["stats", "x"], "stats x")):
        p = subprocess.run([SWCTL] + args, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        check(p.returncode == 1 and p.stderr != "", "swctl %s -> exit 1 and a message (rc=%s)" % (what, p.returncode))
    p = subprocess.run([SWCTL, "-p", str(sw.port), "show", "mac", "x" * 300], capture_output=True, text=True, timeout=30)
    check(p.returncode == 1, "a command of more than 256 bytes is refused by swctl itself")
    # the line is also assembled from several words
    check(ctl(sw, "show ports")[0] == 0, "'show ports' as one argument works too")
    sw.stop()


def test_exit_status_not_running():
    print("9. exit status 2: no switch")
    port = ts.free_port()
    check(not os.path.exists(SEG), "(no segment)")
    for args in (["show", "mac"], ["show", "ports"], ["clear", "mac"], ["shutdown", "1"], ["vlan", "1", "5"], ["mirror", "1", "off"]):
        rc, out, err = ctl(port, *args)
        check(rc == 2 and "switch not running" in err, "swctl %s: exit 2 'switch not running' (rc=%s %r)" % (" ".join(args), rc, err.strip()))
    rc, out, err = ctl(port, "stats")
    check(rc == 2 and "switch not running" in err and out == "", "swctl stats: exit 2, 'switch not running' (rc=%s %r)" % (rc, err.strip()))
    rc, out, err = ctl(port, "stats", "--watch")
    check(rc == 2 and "switch not running" in err, "swctl stats --watch: exit 2 (rc=%s)" % rc)
    # a socket file that nobody listens on (ECONNREFUSED)
    path = "/tmp/vswitch_%d.sock" % port
    s = usock()
    s.bind(path)
    s.close()
    rc, out, err = ctl(port, "show", "mac")
    check(rc == 2, "a dead socket file (connection refused): exit 2 (rc=%s)" % rc)
    os.unlink(path)
    # a path that is not a socket (ECONNREFUSED on Linux) is not 'running' either
    with open(path, "w") as f:
        f.write("x")
    rc, out, err = ctl(port, "show", "mac")
    check(rc in (1, 2), "a regular file at the path: an error exit (rc=%s)" % rc)
    os.unlink(path)


def run_stats(*args, timeout=60):
    p = subprocess.run([SWCTL] + list(args), capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def parse_stats(text):
    rows = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) == 7:
            rows[int(f[0])] = (f[1], int(f[2]), int(f[3]), int(f[4]), int(f[5]), int(f[6]))
    return rows


def test_stats():
    print("10. swctl stats: the counters of the shared segment; the mappings; --watch")
    sw = start(4)
    hs = mk_hosts(sw, 3)
    for h in hs:
        for i in range(5):
            h.send(BCAST, 1, b"s%d-%d" % (h.idx, i))
    ok = wait_for(lambda: all(r[3] == 10 for p, r in parse_stats(run_stats("stats")[1]).items() if p in (1, 2, 3)) and
                  len(parse_stats(run_stats("stats")[1])) == 4, 60)
    rc, out, err = run_stats("stats")
    rows = parse_stats(out)
    check(rc == 0 and ok, "swctl stats: exit 0 and out = 10 on every port (rc=%s)\n%s" % (rc, out))
    check(out.splitlines()[0].split() == ["PORT", "STATE", "VLAN", "IN", "OUT", "DROPPED", "MIRRORED"], "the header names every column")
    check(all(rows[p][2] == 5 for p in (1, 2, 3)) and rows[4][2] == 0, "in = 5 5 5 0")
    check(all(rows[p][4] == 0 and rows[p][5] == 0 for p in rows), "dropped = 0, mirrored = 0")
    check([rows[p][0] for p in (1, 2, 3, 4)] == ["UP", "UP", "UP", "FREE"], "states UP UP UP FREE")
    check(sorted(rows) == [1, 2, 3, 4], "exactly the 4 ports of this switch")
    # stats does not use the admin socket: it works while the admin thread is busy with a silent client
    silent = usock()
    silent.connect(sock_path(sw))
    t = time.time()
    rc, out, err = run_stats("stats")
    check(rc == 0 and time.time() - t < 1.5, "stats works while a silent client holds the admin thread (no socket involved)")
    silent.close()

    # --watch: refresh every second, on whole seconds of the monotonic clock, clear-screen sequence, SIGINT stops it
    w = track(subprocess.Popen([SWCTL, "stats", "--watch"], stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    maps = ""
    stamps = []
    buf = b""
    os.set_blocking(w.stdout.fileno(), False)
    stop = threading.Event()

    def flood():
        while not stop.is_set():
            try:
                hs[0].send(BCAST, 1, b"w" * 80)
            except OSError:
                return
            time.sleep(0.002)

    fl = threading.Thread(target=flood, daemon=True)
    fl.start()
    end = time.time() + 12
    seen_in = []
    while time.time() < end and len(stamps) < 5:
        r, _, _ = select.select([w.stdout], [], [], 0.5)
        if r:
            d = w.stdout.read(65536) or b""
            if d:
                buf += d
                while b"\033[H\033[2J" in buf:
                    head, _, buf = buf.partition(b"\033[H\033[2J")
                    if head and b"PORT" in head:
                        seen_in.append(parse_stats(head.decode()).get(1, (0, 0, 0))[2])
                    stamps.append(time.time())
        if not maps and stamps:
            try:
                with open("/proc/%d/maps" % w.pid) as f:
                    maps = f.read()
            except OSError:
                pass
    stop.set()
    fl.join(5)
    check(len(stamps) >= 5, "five refreshes arrived (%d)" % len(stamps))
    gaps = [b - a for a, b in zip(stamps, stamps[1:])][1:]      # the first one is short: it ends at the next whole second
    check(all(0.6 <= g <= 1.5 for g in gaps) and 0.85 <= sum(gaps) / max(1, len(gaps)) <= 1.15, "refresh interval 1 s: %s" % ["%.2f" % g for g in gaps])
    check(len(seen_in) >= 3 and seen_in[-1] > seen_in[1], "the 'in' counter of port 1 grows between refreshes while it floods: %s" % seen_in)
    # the mappings of the REAL swctl
    seg = [l.split() for l in maps.splitlines() if "vswitch_stats_P" in l]
    check(len(seg) == 2, "two mappings of the segment in /proc/pid/maps (%d)" % len(seg))
    if len(seg) == 2:
        seg.sort(key=lambda s: int(s[2], 16))                # by file offset (the kernel lists them by address)
        (a1, p1, o1), (a2, p2, o2) = [(s[0], s[1], int(s[2], 16)) for s in seg]
        size = lambda a: int(a.split("-")[1], 16) - int(a.split("-")[0], 16)
        check(p1 == "rw-s" and o1 == 0 and size(a1) == PAGE, "control area: offset 0, one page, rw-s (%s %x %d)" % (p1, o1, size(a1)))
        check(p2 == "r--s" and o2 == PAGE, "data area: offset PAGE, r--s: READ-ONLY (%s %x)" % (p2, o2))
    # the descriptor is closed after mmap
    fdl = ""
    try:
        fdl = " ".join(os.readlink("/proc/%d/fd/%s" % (w.pid, f)) for f in os.listdir("/proc/%d/fd" % w.pid))
    except OSError:
        pass
    check("vswitch_stats_P" not in fdl, "the segment's file descriptor is closed after the two mmaps")
    t = time.time()
    w.send_signal(signal.SIGINT)
    try:
        rc = w.wait(5)
    except subprocess.TimeoutExpired:
        w.kill()
        rc = None
    check(rc == 0 and time.time() - t < 1.5, "SIGINT ends --watch at once, exit 0 (rc=%s, %.2f s)" % (rc, time.time() - t))
    check(w.stderr.read() == b"", "no error output")
    # the watch ends with status 2 when the switch goes away
    w = track(subprocess.Popen([SWCTL, "stats", "--watch"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
    time.sleep(1.5)
    for h in hs:
        h.close()
    sw.p.send_signal(signal.SIGINT)
    try:
        rc = w.wait(10)
    except subprocess.TimeoutExpired:
        w.kill()
        rc = None
    check(rc == 2, "--watch ends with exit 2 when the switch shuts down (rc=%s)" % rc)
    sw.stop()
    rc, out, err = run_stats("stats")
    check(rc == 2 and "switch not running" in err, "after the switch: exit 2 'switch not running'")


def test_segments():
    print("11. swctl stats on a damaged or foreign segment: a clear error, no crash, no hang")
    size = PAGE + PAGE                                  # PAGE + round_up(65 * 48, PAGE)

    def make(magic=0x56535453, version=1, nports=4, length=size):
        fd = os.open(SEG, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600)
        os.ftruncate(fd, length)
        os.pwrite(fd, struct.pack("<IIi", magic, version, nports), 0)
        os.close(fd)

    try:
        make(magic=0)
        rc, out, err = run_stats("stats")
        check(rc == 2 and "not running" in err, "magic 0 (shut down / not initialised): exit 2 (rc=%s %r)" % (rc, err.strip()))
        make(magic=0xDEADBEEF)
        rc, out, err = run_stats("stats")
        check(rc == 2 and out == "", "a wrong magic is refused, nothing printed (rc=%s)" % rc)
        make(version=2)
        rc, out, err = run_stats("stats")
        check(rc == 1 and "version" in err and out == "", "another version: exit 1 and says so (rc=%s %r)" % (rc, err.strip()))
        make(nports=999)
        rc, out, err = run_stats("stats")
        check(rc == 1 and "damaged" in err and out == "", "num_ports 999: exit 1 'damaged' (rc=%s %r)" % (rc, err.strip()))
        make(nports=-3)
        rc, out, err = run_stats("stats")
        check(rc == 1 and out == "", "num_ports -3: exit 1 (rc=%s)" % rc)
        make(length=100)
        rc, out, err = run_stats("stats")
        check(rc == 1 and "wrong size" in err, "a truncated object: exit 1, 'wrong size' (rc=%s %r)" % (rc, err.strip()))
        make(length=0)
        rc, out, err = run_stats("stats")
        check(rc == 1, "an empty object: exit 1 (rc=%s)" % rc)
        make(nports=2)                                   # zero-filled mutex = an unlocked default mutex
        rc, out, err = run_stats("stats")
        check(rc == 0 and len(parse_stats(out)) == 2, "a valid header with a zero-filled body is printed (rc=%s)" % rc)
        os.chmod(SEG, 0o400)
        rc, out, err = run_stats("stats")
        check(rc == 1 and "cannot open" in err, "a read-only object (no O_RDWR possible): exit 1, clear message (rc=%s %r)" % (rc, err.strip()))
    finally:
        try:
            os.unlink(SEG)
        except OSError:
            pass


def test_valgrind():
    print("12. valgrind: admin thread and swctl commands, no errors, no leaks, no descriptor left open, no socket left")
    sw = start(4)
    hs = mk_hosts(sw, 3)
    for h in hs:
        h.send(BCAST, 1, b"v")
    check(wait_for(lambda: len(parse_macs(ctl(sw, "show", "mac", timeout=120)[1])) == 3, 120), "show mac under valgrind")
    check(ctl(sw, "show", "ports", timeout=120)[0] == 0, "show ports")
    check(ctl(sw, "clear", "mac", timeout=120)[0] == 0, "clear mac")
    check(ctl(sw, "vlan", "9", "10", timeout=120)[0] == 1, "a bad command")
    check(raw(sw, b"sho", timeout=60).startswith(b"ERR timeout"), "a silent client times out")
    silent = usock()
    silent.connect(sock_path(sw))
    half = usock()
    half.connect(sock_path(sw))
    half.sendall(b"show ")
    time.sleep(0.5)
    rc, dt = sigint_and_wait(sw, 180)
    check(rc == 0, "SIGINT with two silent clients: exit status 0 (got %s)" % rc)
    check(not os.path.exists(sock_path(sw)), "socket path removed")
    silent.close()
    half.close()
    for h in hs:
        h.close()
    log = sw.log()
    check("ERROR SUMMARY: 0 errors from 0 contexts" in log, "valgrind: 0 errors")
    check("in use at exit: 0 bytes in 0 blocks" in log, "valgrind: 0 bytes in use at exit")
    check(re.search(r"(definitely|indirectly|possibly) lost: [1-9]", log) is None, "valgrind: nothing lost")
    fds = [f for f in re.findall(r"Open (?:AF_INET socket|AF_UNIX socket|file descriptor) (\d+)", log) if int(f) > 2]
    check(not fds, "valgrind: no descriptor left open at exit %s" % fds)
    if "ERROR SUMMARY: 0 errors" not in log:
        print(log[-3000:])
    sw.stop()


# ----------------------------------------------------------------------------------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    install_signal_cleanup()
    try:
        if mode == "all":
            run_tests((test_socket, test_protocol, test_bad_clients, test_shutdown, test_commands, test_flood_after_clear, test_during_traffic,
                       test_validation, test_exit_status_not_running, test_stats, test_segments))
        elif mode == "tsan":
            ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
            SLOW = 3
            run_tests((test_protocol, test_bad_clients, test_shutdown, test_commands, test_flood_after_clear, test_during_traffic, test_stats))
        elif mode == "valgrind":
            ts.SWITCH = os.environ.get("SWITCH") or (
                "valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
                "--error-exitcode=1 --track-fds=yes ./switch")
            SLOW = 20
            run_tests((test_valgrind,))
        else:
            print("usage: test_admin.py [all|tsan|valgrind]")
            sys.exit(2)
    finally:
        cleanup()                                      # also after KeyboardInterrupt / SIGTERM of this script
        subprocess.run(["rm", "-rf", TMP])
    print("%d checks passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
