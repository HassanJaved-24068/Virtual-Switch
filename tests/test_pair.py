#!/usr/bin/env python3
"""
Part 19 end-to-end tests: two switches joined by two pipes (./switch <tcpA> <num_ports> --pair <tcpB> [-c a.conf] [--peer-config b.conf]).

  python3 tests/test_pair.py            normal build
  python3 tests/test_pair.py tsan       SWITCH="setarch -R ./switch_tsan"
  python3 tests/test_pair.py valgrind   valgrind on ./switch (the forked child B is instrumented too)

Hosts are raw sockets (they see the exact frames). Switch B's log file is A's log file + ".<tcpB>" (VSWITCH_LOG_FILE is set by the test).

  1. forwarding across the link: VLAN 10 broadcast and unicast cross, VLAN 20 hosts of either switch never see them, no echo, vlan field kept, 1500-byte payload intact,
     swctl show mac lists B's hosts on port 0, show ports and stats show port 0, the shared counters of both switches exist.
  2. the trunk is exempt from port security (more MACs than max_mac_per_port behind port 0).
  3. clean shutdown: SIGINT to both (process group), SIGINT to A only (A waits for B), nothing is left in /dev/shm or /tmp, both exit 0.
  4. the peer dies (kill -9 B): A logs it, port 0 is DISABLED, A's hosts keep talking; A exits 0 and reports B's fate.  A dies (kill -9): B follows (PDEATHSIG).
  5. a stalled peer (SIGSTOP B while A floods it): A still shuts down promptly (the sender wakes from poll), B is killed after the grace period.
  6. a flood across the link and SIGINT: both exit quickly.
  7. bad command lines (exit status 1, nothing left running).
"""
import os, re, select, shlex, signal, socket, struct, subprocess, sys, tempfile, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts

SWITCH = ts.SWITCH
SWCTL = os.environ.get("SWCTL", "./swctl")
SLOW = 1
passed = failed = 0
TMP = tempfile.mkdtemp(prefix="vswpair.")
LIVE = []                                    # processes and sockets to clean up


def check(cond, what):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", what)


class Host(threading.Thread):
    """A raw host: connects, records every frame as (src, dst, vlan, prio, payload)."""

    def __init__(self, port, mac_str):
        super().__init__(daemon=True)
        self.mac = mac_str
        self.sock = socket.create_connection(("127.0.0.1", port), 10)
        self.got = []
        self.eof = False
        self.lock = threading.Lock()
        LIVE.append(self)
        self.start()

    def run(self):
        buf = b""
        while True:
            try:
                if not select.select([self.sock], [], [], 0.2)[0]:
                    continue
                d = self.sock.recv(65536)
            except (OSError, ValueError):
                self.eof = True
                return
            if not d:
                self.eof = True
                return
            buf += d
            while len(buf) >= 17:
                n = struct.unpack("!H", buf[:2])[0]
                if len(buf) < 17 + n:
                    break
                fr = (":".join("%02x" % b for b in buf[2:8]), ":".join("%02x" % b for b in buf[8:14]),
                      struct.unpack("!H", buf[14:16])[0], buf[16], buf[17:17 + n])
                with self.lock:
                    self.got.append(fr)
                buf = buf[17 + n:]

    def send(self, dst, payload, vlan=1, prio=1, src=None):
        self.sock.sendall(ts.frame(src or self.mac, dst, vlan, prio, payload))

    def frames(self, payload):
        with self.lock:
            return [f for f in self.got if f[4] == payload]

    def wait_for(self, payload, timeout=5):
        end = time.time() + timeout * SLOW
        while time.time() < end:
            if self.frames(payload):
                return True
            time.sleep(0.02)
        return False

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Pair:
    def __init__(self, conf_a="", conf_b="", extra=(), nports=8, own_group=True):
        self.a = ts.free_port()
        self.b = ts.free_port()
        while self.b == self.a:
            self.b = ts.free_port()
        self.log_a = tempfile.NamedTemporaryFile("w+b", suffix=".log", delete=False, dir=TMP).name
        self.log_b = "%s.%d" % (self.log_a, self.b)
        self.err = open(os.path.join(TMP, "err.%d" % self.a), "w+b")
        args = [str(self.a), str(nports), "--pair", str(self.b)]
        if conf_a is not None:
            self.ca = os.path.join(TMP, "a.%d.conf" % self.a)
            open(self.ca, "w").write(conf_a)
            args += ["-c", self.ca]
        if conf_b:
            self.cb = os.path.join(TMP, "b.%d.conf" % self.a)
            open(self.cb, "w").write(conf_b)
            args += ["--peer-config", self.cb]
        self.p = subprocess.Popen(shlex.split(SWITCH) + args + list(extra), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.err,
                                  env=dict(os.environ, VSWITCH_LOG_FILE=self.log_a), start_new_session=own_group)
        LIVE.append(self)
        self.hosts = []
        ok = self.wait("listening on TCP port %d" % self.a, 30) and self.wait("listening on TCP port %d" % self.b, 30, b=True)
        ok = ok and self.wait("trunk to the peer switch attached", 30) and self.wait("trunk to the peer switch attached", 30, b=True)
        if not ok:
            raise RuntimeError("the pair did not start:\nA:\n%s\nB:\n%s\nerr:\n%s" % (self.text(), self.text(b=True), self.errtext()))
        self.pid_b = self.child_pid()

    def child_pid(self):
        out = subprocess.run(["ps", "-o", "pid=", "--ppid", str(self.p.pid)], capture_output=True, text=True).stdout.split()
        return int(out[0]) if out else None

    def text(self, b=False):
        try:
            return open(self.log_b if b else self.log_a, "rb").read().decode(errors="replace")
        except OSError:
            return ""

    def errtext(self):
        self.err.flush()
        with open(self.err.name, "rb") as f:
            return f.read().decode(errors="replace")

    def wait(self, text, timeout=5, count=1, b=False):
        end = time.time() + timeout * SLOW
        while time.time() < end:
            if self.text(b).count(text) >= count:
                return True
            if self.p.poll() is not None:
                return self.text(b).count(text) >= count
            time.sleep(0.02)
        return False

    def host(self, side, mac_str, nth):
        """side 'a' or 'b'; nth = how many hosts that switch had before (to wait for 'host connected')."""
        port = self.a if side == "a" else self.b
        h = Host(port, mac_str)
        self.hosts.append(h)
        if not self.wait("host connected", 10, nth + 1, b=(side == "b")):
            raise RuntimeError("host %s did not get a port" % mac_str)
        return h

    def ctl(self, side, *cmd):
        r = subprocess.run([SWCTL, "-p", str(self.a if side == "a" else self.b)] + list(cmd), capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def stop(self, sig=signal.SIGINT, group=True, timeout=60):
        """Signal A (or the whole group) and wait. Returns A's exit status."""
        for h in self.hosts:
            h.close()
        if self.p.poll() is None:
            try:
                if group:
                    os.killpg(self.p.pid, sig)
                else:
                    self.p.send_signal(sig)
            except ProcessLookupError:
                pass
        try:
            return self.p.wait(timeout * SLOW)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
            return None

    def cleanup(self):
        for h in self.hosts:
            h.close()
        try:
            os.killpg(self.p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            pass
        for f in (self.a, self.b):
            for path in ("/tmp/vswitch_%d.sock" % f,):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        for n in ("/dev/shm/vswitch_stats_P", "/dev/shm/vswitch_stats_P_%d" % self.b):
            try:
                os.unlink(n)
            except OSError:
                pass


def gone(pair):
    """Nothing of the pair is left: no shared-memory segment, no admin socket."""
    return (not os.path.exists("/dev/shm/vswitch_stats_P") and not os.path.exists("/dev/shm/vswitch_stats_P_%d" % pair.b) and
            not os.path.exists("/tmp/vswitch_%d.sock" % pair.a) and not os.path.exists("/tmp/vswitch_%d.sock" % pair.b))


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                                 # a zombie is not alive
        return open("/proc/%d/stat" % pid).read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def clean(pair, what="no sanitizer / valgrind report in either switch"):
    t = pair.text() + pair.text(b=True) + pair.errtext()
    bad = re.search(r"Sanitizer|ERROR SUMMARY: [1-9]|definitely lost: [1-9]|indirectly lost: [1-9]|Invalid (read|write)|Open file descriptor", t)
    check(not bad, what + (" (found %r)" % bad.group(0) if bad else ""))


MA1, MA2, MA3, MB1, MB2 = "02:aa:00:00:00:01", "02:aa:00:00:00:02", "02:aa:00:00:00:03", "02:bb:00:00:00:01", "02:bb:00:00:00:02"
CONF = "port.1.vlan = 10\nport.2.vlan = 20\nport.3.vlan = 10\n"
CONF10 = "port.1.vlan = 10\nport.2.vlan = 10\n"                 # the first two hosts of a switch share VLAN 10


def test_forwarding():
    print("1. forwarding across the link, VLANs, show mac / ports / stats")
    p = Pair(CONF, CONF)
    a1, a2 = p.host("a", MA1, 0), p.host("a", MA2, 1)
    b1, b2 = p.host("b", MB1, 0), p.host("b", MB2, 1)

    check(os.path.exists("/dev/shm/vswitch_stats_P") and os.path.exists("/dev/shm/vswitch_stats_P_%d" % p.b), "both switches have their shared counters")
    check(os.path.exists("/tmp/vswitch_%d.sock" % p.a) and os.path.exists("/tmp/vswitch_%d.sock" % p.b), "both switches have their admin socket")
    check(p.pid_b is not None and p.pid_b != p.p.pid, "B is a child process of A")

    a1.send(ts.BCAST, b"bc-from-a1", vlan=1)                           # the vlan the host writes is replaced by its port's (10)
    check(b1.wait_for(b"bc-from-a1"), "a VLAN 10 broadcast of A reaches the VLAN 10 host of B")
    b2.send(ts.BCAST, b"settle-b2")
    a2.wait_for(b"settle-b2")
    check(not a2.frames(b"bc-from-a1") and not b2.frames(b"bc-from-a1"), "the VLAN 20 hosts of both switches do not see it")
    check(not a1.frames(b"bc-from-a1"), "the sender does not get its own broadcast back")
    fr = b1.frames(b"bc-from-a1")
    check(len(fr) == 1 and fr[0][2] == 10 and fr[0][0] == MA1, "B's host sees it once, with vlan=10 and the original source")

    b1.send(ts.BCAST, b"bc-from-b1")                                   # B1 broadcasts: A1 gets it, and the whole path teaches both switches
    check(a1.wait_for(b"bc-from-b1"), "a VLAN 10 broadcast of B reaches the VLAN 10 host of A")
    a1.send(MB1, b"uni-a1-b1")
    check(b1.wait_for(b"uni-a1-b1"), "unicast from A's host to B's host")
    check(len(b1.frames(b"uni-a1-b1")) == 1, "it arrives exactly once")
    b2.send(ts.BCAST, b"settle2")
    time.sleep(0.2 * SLOW)
    check(not b2.frames(b"uni-a1-b1") and not a2.frames(b"uni-a1-b1"), "the VLAN 20 hosts did not get the unicast")

    a1.send(MB2, b"to-b2-other-vlan")                                  # B2 is in VLAN 20: a VLAN 10 frame to its MAC never reaches it
    b1.wait_for(b"to-b2-other-vlan")                                   # (it is flooded inside VLAN 10)
    time.sleep(0.2 * SLOW)
    check(not b2.frames(b"to-b2-other-vlan"), "a unicast to a MAC of another VLAN is never delivered")

    a2.send(ts.BCAST, b"bc-vlan20-a")
    check(b2.wait_for(b"bc-vlan20-a"), "VLAN 20 crosses the link too")
    fr = b2.frames(b"bc-vlan20-a")
    check(fr and fr[0][2] == 20, "... with vlan=20")
    check(not a1.frames(b"bc-vlan20-a") and not b1.frames(b"bc-vlan20-a"), "VLAN 10 hosts do not see VLAN 20 traffic")

    big = bytes(range(256)) * 5 + b"X" * 220                          # 1500 bytes
    a1.send(MB1, big)
    check(b1.wait_for(big), "a 1500-byte payload (frame of 1517 bytes) crosses the link intact")

    rc, out = p.ctl("a", "show", "mac")
    check(rc == 0 and re.search(r"^10 %s 0 " % MB1, out, re.M), "swctl show mac on A lists B's host on port 0 (got: %r)" % out)
    check(re.search(r"^10 %s 1 " % MA1, out, re.M), "... and A's own host on its port")
    rc, out = p.ctl("b", "show", "mac")
    check(rc == 0 and re.search(r"^10 %s 0 " % MA1, out, re.M), "swctl show mac on B lists A's host on port 0")
    rc, out = p.ctl("a", "show", "ports")
    row0 = [l for l in out.splitlines() if l.split()[:1] == ["0"]]
    check(rc == 0 and row0 and "UP" in row0[0] and "yes" in row0[0], "show ports lists port 0 as an UP trunk (got %r)" % (row0,))
    rc, out = p.ctl("a", "stats")
    row0 = [l for l in out.splitlines() if l.split()[:1] == ["0"]]
    nums = [int(x) for x in row0[0].split()[3:]] if row0 else []
    check(rc == 0 and len(nums) == 4 and nums[0] > 0 and nums[1] > 0, "stats shows port 0 with traffic in both directions (got %r)" % (row0,))
    rc, outb = p.ctl("b", "stats")
    check(rc == 0 and [l for l in outb.splitlines() if l.split()[:1] == ["0"]], "swctl -p <B> stats reads B's own counters")

    rc, out = p.ctl("a", "shutdown", "0")
    check(rc == 1, "port 0 is not an admin port (swctl shutdown 0 -> ERR)")

    rc = p.stop(signal.SIGINT, group=True)
    check(rc == 0, "A exited with status 0 after SIGINT to the group (got %s)" % rc)
    check(p.wait("shutdown: complete", 20, b=True) and "shutdown: complete" in p.text(), "both logs end with 'shutdown: complete'")
    check(p.pid_b is None or not alive(p.pid_b), "B is gone")
    check("the peer switch" in p.text() and "exited with status 0" in p.text(), "A's log says that B exited with status 0")
    check(gone(p), "no shared memory and no socket left")
    check("[ERR]" not in p.text() and "[ERR]" not in p.text(b=True), "no [ERR] line in either log")
    clean(p)
    p.cleanup()


def test_trunk_security():
    print("2. the trunk port is exempt from port security")
    p = Pair("max_macs_per_port = 100\n", "max_macs_per_port = 3\n")        # B would shut a normal port after 3 MACs
    a1 = p.host("a", MA1, 0)
    b1 = p.host("b", MB1, 0)
    for i in range(12):
        a1.send(ts.BCAST, b"flood-%d" % i, src="02:cc:00:00:00:%02x" % i)
    check(b1.wait_for(b"flood-11"), "B's host got all of them")
    rc, out = p.ctl("b", "show", "mac")
    n = len(re.findall(r"^1 02:cc:00:00:00:\w\w 0 ", out, re.M))
    check(n == 12, "B learned 12 MACs behind port 0 although its limit is 3 (got %d)" % n)
    rc, out = p.ctl("b", "show", "ports")
    row0 = [l for l in out.splitlines() if l.split()[:1] == ["0"]]
    check(row0 and "UP" in row0[0], "port 0 of B is still UP")
    check("MAC flooding" not in p.text(b=True), "no 'MAC flooding' shutdown on B")
    check(p.stop() == 0, "clean exit")
    clean(p)
    p.cleanup()


def test_shutdown_modes():
    print("3. shutdown: SIGINT to A only (A signals and waits for B); SIGTERM to B only (A keeps working)")
    p = Pair(CONF10, CONF10)
    a1 = p.host("a", MA1, 0)
    pid_b = p.pid_b
    rc = p.stop(signal.SIGINT, group=False)
    check(rc == 0, "A exited with status 0 (got %s)" % rc)
    check(not alive(pid_b), "B was stopped and reaped by A")
    check("exited with status 0" in p.text(), "A logged B's exit status 0")
    check(p.text(b=True).count("shutdown: complete") == 1, "B shut down cleanly")
    check(gone(p), "no shared memory and no socket left")
    clean(p)
    p.cleanup()

    p = Pair(CONF10, CONF10)
    a1 = p.host("a", MA1, 0)
    a3 = p.host("a", MA3, 1)
    os.kill(p.pid_b, signal.SIGTERM)
    check(p.wait("port 0: ", 20) and p.wait("peer", 20), "A noticed the peer going away")
    check(p.wait("teardown complete, port is DISABLED", 20), "port 0 of A is DISABLED")
    rc, out = p.ctl("a", "show", "ports")
    row0 = [l for l in out.splitlines() if l.split()[:1] == ["0"]]
    check(row0 and "DISABLED" in row0[0], "show ports says so (got %r)" % (row0,))
    a1.send(MA3, b"still-works")
    check(a3.wait_for(b"still-works"), "A's hosts still talk to each other")
    check(p.stop(signal.SIGINT, group=False) == 0, "A exits 0")
    check("exited with status 0" in p.text(), "A reaped B (status 0)")
    clean(p)
    p.cleanup()


def test_peer_dies():
    print("4. the peer is killed (kill -9 B); A is killed (kill -9 A)")
    p = Pair(CONF10, CONF10)
    a1, a3 = p.host("a", MA1, 0), p.host("a", MA3, 1)
    b1 = p.host("b", MB1, 0)
    a1.send(ts.BCAST, b"before")
    check(b1.wait_for(b"before"), "traffic crosses before the kill")
    os.kill(p.pid_b, signal.SIGKILL)
    check(p.wait("teardown complete, port is DISABLED", 20), "A: port 0 went DISABLED (EOF / EPIPE)")
    check("port 0:" in p.text() and ("host disconnected" in p.text() or "write failed" in p.text()), "the reason is logged")
    a1.send(MA3, b"after")
    check(a3.wait_for(b"after"), "A keeps switching for its own hosts")
    a1.send(ts.BCAST, b"after-bcast")
    check(a3.wait_for(b"after-bcast"), "a broadcast still reaches A's hosts (nothing is queued to the dead port)")
    rc, out = p.ctl("a", "show", "mac")
    check(rc == 0 and "# vlan mac port age_s" in out, "swctl still works")
    rc = p.stop(signal.SIGINT, group=False)
    check(rc == 0, "A exits 0 (got %s)" % rc)
    check("killed by signal 9" in p.text(), "A reports that B was killed by signal 9")
    p.cleanup()

    p = Pair(CONF10, CONF10)
    pid_b = p.pid_b
    os.kill(p.p.pid, signal.SIGKILL)
    p.p.wait(10)
    end = time.time() + 30 * SLOW
    while alive(pid_b) and time.time() < end:
        time.sleep(0.1)
    check(not alive(pid_b), "after kill -9 of A the switch B stops by itself (it does not live on as an orphan)")
    p.cleanup()


def test_stalled_peer():
    print("5. a stalled peer (SIGSTOP B) while A floods: A still shuts down promptly")
    p = Pair(CONF, CONF)
    a1 = p.host("a", MA1, 0)
    b1 = p.host("b", MB1, 0)
    a1.send(ts.BCAST, b"warm")
    check(b1.wait_for(b"warm"), "traffic crosses")
    os.kill(p.pid_b, signal.SIGSTOP)
    payload = b"s" * 1400
    for i in range(300):                                               # far more than the pipe (64 KiB) and the queue of port 0 hold
        try:
            a1.send(ts.BCAST, payload)
        except OSError:
            break
    time.sleep(0.5 * SLOW)
    t = time.time()
    rc = p.stop(signal.SIGINT, group=False, timeout=120)
    dt = time.time() - t
    check(rc == 0, "A exited with status 0 (got %s)" % rc)
    check(dt < 40 * SLOW, "A did not wait for the stalled peer's pipe (%.1f s)" % dt)
    check("killed by signal 9" in p.text() or "did not exit" in p.text(), "A gave up waiting for B and killed it (grace period)")
    check(p.pid_b is None or not alive(p.pid_b), "B is gone")
    p.cleanup()


def test_flood_and_sigint():
    print("6. a flood across the link, then SIGINT to both")
    p = Pair(CONF, CONF)
    a1 = p.host("a", MA1, 0)
    b1 = p.host("b", MB1, 0)
    stop = threading.Event()

    def flood():
        i = 0
        while not stop.is_set():
            try:
                a1.send(ts.BCAST, b"f%d" % i)
            except OSError:
                return
            i += 1
    th = threading.Thread(target=flood, daemon=True)
    th.start()
    time.sleep(1.5 * SLOW)
    n = len(b1.got)
    check(n > 100, "B's host receives the flood (%d frames)" % n)
    t = time.time()
    rc = p.stop(signal.SIGINT, group=True)
    stop.set()
    dt = time.time() - t
    check(rc == 0, "A exited with status 0 (got %s)" % rc)
    check(dt < 30 * SLOW, "the shutdown took %.1f s" % dt)
    check(p.text(b=True).count("shutdown: complete") == 1, "B shut down cleanly")
    check(gone(p), "nothing left in /dev/shm or /tmp")
    clean(p)
    p.cleanup()


def test_bad_command_lines():
    print("7. bad command lines")
    cases = {
        "--peer-config without --pair": ["8", "--peer-config", "/nonexistent"],
        "--pair with the same port": ["8", "--pair", "SAME"],
        "--pair without a value": ["8", "--pair"],
        "--pair with a bad port": ["8", "--pair", "70000"],
        "--pair twice": ["8", "--pair", "FREE1", "--pair", "FREE2"],
        "a bad peer configuration file": ["8", "--pair", "FREE1", "--peer-config", os.path.join(TMP, "bad.conf")],
    }
    open(os.path.join(TMP, "bad.conf"), "w").write("no_such_key = 1\n")
    for name, args in cases.items():
        port = ts.free_port()
        args = [str(port)] + [(str(port) if a == "SAME" else str(ts.free_port()) if a.startswith("FREE") else a) for a in args]
        env = dict(os.environ, VSWITCH_LOG_FILE=os.path.join(TMP, "bad.log"))
        try:
            r = subprocess.run(shlex.split(SWITCH) + args, capture_output=True, text=True, timeout=60 * SLOW, env=env, stdin=subprocess.DEVNULL)
            check(r.returncode == 1 and r.stderr.strip() != "", "%s: exit status 1 with a message (got %s)" % (name, r.returncode))
        except subprocess.TimeoutExpired:
            check(False, "%s: the switch did not exit" % name)
    check(not os.path.exists("/dev/shm/vswitch_stats_P_1"), "nothing was created")


TESTS = (test_forwarding, test_trunk_security, test_shutdown_modes, test_peer_dies, test_stalled_peer, test_flood_and_sigint, test_bad_command_lines)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "tsan":
        SWITCH = ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
        SLOW = 3
    elif mode == "valgrind":
        SWITCH = ts.SWITCH = os.environ.get("SWITCH") or ("valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
                                                          "--error-exitcode=1 --track-fds=yes ./switch")
        SLOW = 4
    elif mode != "all":
        print("usage: test_pair.py [all|tsan|valgrind]")
        sys.exit(2)
    try:
        for t in TESTS:
            try:
                t()
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(False, "%s raised %r" % (t.__name__, e))
            for o in LIVE:
                if isinstance(o, Pair):
                    o.cleanup()
            del LIVE[:]
    finally:
        for o in LIVE:
            if isinstance(o, Pair):
                o.cleanup()
        subprocess.run(["rm", "-rf", TMP])
    print("%d checks passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
