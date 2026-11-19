#!/usr/bin/env python3
"""
Part 20 end-to-end tests: the supervisor (./supervisor ./switch <tcp_port> <num_ports> [opts]) and the crash-surviving MAC table (./switch ... --persist-mac).

  python3 tests/test_persist.py            normal build
  python3 tests/test_persist.py tsan       the switch is ./switch_tsan (the supervisor runs under `setarch -R`, the child inherits it)
  python3 tests/test_persist.py valgrind   the switch under valgrind, started directly (the supervisor cannot exec valgrind): persistence only, no supervisor scenarios

The recovery itself (a dead owner of a bucket / pool lock, a damaged table) is unit tested exactly in tests/test_macshm.c. Here: the whole switch.

  1. --persist-mac without the supervisor: learn, kill -9, start again: "MAC table recovered: N entries", show mac / show ports are the same, a stale entry whose
     port is not UP floods, a clean exit leaves the segment (the switch never unlinks it).
  2. the supervisor: kill -SEGV the switch -> restarted after 1 s, "recovered: 4 entries", swctl show mac lists them; SIGINT to the supervisor: exit 0, nothing in /dev/shm or /tmp.
  3. a clean end of the switch (SIGINT to the child) ends the supervisor, which removes the segment.
  4. SIGTERM to the supervisor during the restart delay: it stops at once, nothing is left (even the crashed switch's own files).
  5. giving up: a switch that cannot start (its TCP port is taken) is restarted 5 times, then the supervisor exits 1; a program that cannot be executed likewise.
  6. bad command lines.
"""
import os, re, shlex, signal, socket, subprocess, sys, tempfile, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_pair as tp

SWITCH = "./switch"
SUPERVISOR = "./supervisor"
SWCTL = os.environ.get("SWCTL", "./swctl")
SLOW = 1
passed = failed = 0
TMP = tempfile.mkdtemp(prefix="vswpers.")
PROCS = []
HOSTS = []


def check(cond, what):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", what)


M = ["02:aa:00:00:00:%02x" % i for i in range(1, 8)]
CONF = "max_macs_per_port = 100\n"


class Run:
    """A switch started directly, or by the supervisor, with its own log files; remembers what it has to clean up."""

    def __init__(self, supervised, nports=4, conf=CONF, port=None, program=None):
        self.port = port or ts.free_port()
        self.log = os.path.join(TMP, "sw.%d.log" % self.port)
        self.errf = open(os.path.join(TMP, "err.%d.%d" % (self.port, len(PROCS))), "w+b")
        self.conf = os.path.join(TMP, "c.%d.conf" % self.port)
        open(self.conf, "w").write(conf)
        self.supervised = supervised
        self.nports = nports
        self.hosts = []
        self.p = None
        self.program = program
        self.start()

    def args(self):
        return [str(self.port), str(self.nports), "-c", self.conf]

    def start(self):
        env = dict(os.environ, VSWITCH_LOG_FILE=self.log)
        if self.supervised:
            cmd = shlex.split(SUPERVISOR) + [self.program or SWITCH_PROGRAM] + self.args()
        else:
            cmd = shlex.split(SWITCH) + self.args() + ["--persist-mac"]
        self.p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.errf, env=env, start_new_session=True)
        PROCS.append(self)

    def text(self):
        try:
            return open(self.log, "rb").read().decode(errors="replace")
        except OSError:
            return ""

    def err(self):
        self.errf.flush()
        with open(self.errf.name, "rb") as f:
            return f.read().decode(errors="replace")

    def wait(self, text, timeout=10, count=1, where=None):
        end = time.time() + timeout * SLOW
        while time.time() < end:
            t = self.text() if where is None else where()
            if t.count(text) >= count:
                return True
            if self.p.poll() is not None:
                t = self.text() if where is None else where()
                return t.count(text) >= count
            time.sleep(0.02)
        return False

    def up(self, n=1, timeout=30):
        return self.wait("listening on TCP port %d" % self.port, timeout, n)

    def switch_pid(self):
        if not self.supervised:
            return self.p.pid
        end = time.time() + 10 * SLOW
        while time.time() < end:
            out = subprocess.run(["ps", "-o", "pid=", "--ppid", str(self.p.pid)], capture_output=True, text=True).stdout.split()
            if out:
                return int(out[0])
            time.sleep(0.05)
        return None

    def host(self, mac_str, nth):
        seen = self.text().count("host connected")          # the log is cumulative over restarts: wait for ONE MORE line than there is now (nth is only a label)
        h = tp.Host(self.port, mac_str)
        HOSTS.append(h)
        self.hosts.append(h)
        if not self.wait("host connected", 10, seen + 1):
            raise RuntimeError("host %s did not get a port" % mac_str)
        return h

    def drop_hosts(self):
        for h in self.hosts:
            h.close()
        self.hosts = []

    def ctl(self, *cmd):
        r = subprocess.run([SWCTL, "-p", str(self.port)] + list(cmd), capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def macs(self):
        rc, out = self.ctl("show", "mac")
        return sorted((int(m.group(1)), m.group(2), int(m.group(3))) for m in re.finditer(r"^(\d+) (\S+) (\d+) \d+$", out, re.M)) if rc == 0 else None

    def kill_switch(self, sig):
        pid = self.switch_pid()
        os.kill(pid, sig)
        return pid

    def stop(self, sig=signal.SIGINT, timeout=60):
        self.drop_hosts()
        if self.p.poll() is None:
            try:
                os.kill(self.p.pid, sig)
            except ProcessLookupError:
                pass
        try:
            return self.p.wait(timeout * SLOW)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
            return None

    def cleanup(self):
        self.drop_hosts()
        try:
            os.killpg(self.p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            pass
        leftovers(self.port, remove=True)


def mac_seg(port):
    return "/dev/shm/vswitch_mac_%d" % port


def leftovers(port, remove=False):
    """The files a switch (or its supervisor) may leave behind. Returns the ones that exist."""
    found = [p for p in (mac_seg(port), "/dev/shm/vswitch_stats_P", "/tmp/vswitch_%d.sock" % port) if os.path.exists(p)]
    if remove:
        for p in found:
            try:
                os.unlink(p)
            except OSError:
                pass
    return found


def learn(run, pairs):
    """pairs: [(host, src_mac)]: each host sends one broadcast from the given source."""
    for i, (h, src) in enumerate(pairs):
        h.send(ts.BCAST, b"learn-%d" % i, src=src)
    time.sleep(0.3 * SLOW)


def test_direct_persist():
    print("1. --persist-mac without the supervisor: kill -9 and start again")
    r = Run(False)
    check(r.up(), "the switch started")
    check(r.wait("no usable shared table found, a new one was created"), "first start: a new table is created")
    h1, h2, h3 = r.host(M[0], 0), r.host(M[1], 1), r.host(M[2], 2)
    learn(r, [(h1, M[0]), (h1, M[3]), (h2, M[1]), (h3, M[2])])
    before = r.macs()
    check(before is not None and len(before) == 4, "4 MACs learned (got %r)" % (before,))
    check(os.path.exists(mac_seg(r.port)), "the shared table /dev/shm/vswitch_mac_<port> exists")
    r.stop(signal.SIGKILL)
    check(os.path.exists(mac_seg(r.port)), "after kill -9 the table is still there")
    leftovers(r.port, remove=False)
    try:
        os.unlink("/dev/shm/vswitch_stats_P")                       # the dead switch's counters: the new switch replaces stale ones anyway
    except OSError:
        pass
    r.start()
    check(r.up(2), "the switch started again")
    check(r.wait("MAC table recovered: 4 entries"), "the log says 'MAC table recovered: 4 entries'")
    after = r.macs()
    check(after is not None and [(v, m, p) for v, m, p in after] == [(v, m, p) for v, m, p in before], "swctl show mac lists the same entries on the same ports (%r)" % (after,))
    rc, out = r.ctl("show", "ports")
    rows = {int(l.split()[0]): l.split() for l in out.splitlines()[1:] if l.split() and l.split()[0].isdigit()}
    check(rows[1][4] == "2" and rows[2][4] == "1" and rows[3][4] == "1", "show ports: the MAC counts of the ports were rebuilt from the entries (%s)" % [rows[i][4] for i in (1, 2, 3)])

    # the old hosts are gone; two new ones connect to ports 1 and 2. Port 3 is not UP: the stale entry of M[2] must flood, not be sent into the void
    a, b = r.host(M[0], 0), r.host(M[1], 1)
    a.send(M[2], b"to-stale-port3")
    check(b.wait_for(b"to-stale-port3"), "a unicast to a MAC whose recovered port is not UP is flooded")
    a.send(M[1], b"to-b")
    check(b.wait_for(b"to-b"), "traffic works as usual")
    b.send(M[0], b"to-a")
    check(a.wait_for(b"to-a"), "... in both directions")
    a.send(ts.BCAST, b"learn-new", src=M[4])
    time.sleep(0.2 * SLOW)
    check(len(r.macs() or []) == 5, "new MACs are learned in the recovered table")

    rc = r.stop(signal.SIGINT)
    check(rc == 0, "a clean SIGINT exits with status 0 (got %s)" % rc)
    check(os.path.exists(mac_seg(r.port)), "a switch started with --persist-mac does NOT unlink the table itself")
    t = r.text()
    check("[ERR]" not in t and "Sanitizer" not in t + r.err() and not re.search(r"ERROR SUMMARY: [1-9]|definitely lost: [1-9]", r.err()), "no [ERR] line, no sanitizer / valgrind report")

    r.start()                                                       # a third start: a clean exit tears every port down, and a teardown flushes the MACs of its port
    check(r.up(3), "third start")
    check(r.wait("MAC table recovered: 1 entries"), "after a CLEAN exit the same table is found again: the teardown flushed the ports that had a host, only the stale entry of port 3 (never "
                                                    "reconnected, so never torn down) is left")
    check(r.stop(signal.SIGINT) == 0, "exit 0")
    r.cleanup()


def sup(r, what=None):
    return r.err()


def test_supervisor_crash():
    print("2. the supervisor restarts the switch after kill -SEGV; the MAC table survives")
    r = Run(True)
    check(r.up(), "the supervisor started the switch")
    check("started" in r.err(), "the supervisor says so")
    first = r.switch_pid()
    h1, h2 = r.host(M[0], 0), r.host(M[1], 1)
    learn(r, [(h1, M[0]), (h1, M[3]), (h2, M[1]), (h2, M[4])])
    before = r.macs()
    check(before is not None and len(before) == 4, "4 MACs learned (got %r)" % (before,))
    os.kill(first, CRASH)                                  # the hosts are still connected: a crash flushes nothing (a disconnect would)
    time.sleep(0.2 * SLOW)
    r.drop_hosts()
    check(r.wait("killed by signal %d" % CRASH, 10, where=r.err), "the supervisor saw the crash (killed by signal %d)" % CRASH)
    check(r.up(2, 30), "the switch is back (restarted after 1 s)")
    check("restarted" in r.err(), "the supervisor says 'restarted'")
    second = r.switch_pid()
    check(second is not None and second != first, "a new switch process (pid %s -> %s)" % (first, second))
    check(r.wait("MAC table recovered: 4 entries"), "the log says 'MAC table recovered: 4 entries'")
    after = r.macs()
    check(after is not None and after == before, "swctl show mac lists the 4 entries again (%r)" % (after,))

    a, b = r.host(M[0], 0), r.host(M[1], 1)
    a.send(M[1], b"hello-again")
    check(b.wait_for(b"hello-again"), "the restarted switch forwards traffic")
    t0 = time.time()
    rc = r.stop(signal.SIGINT)
    check(rc == 0, "SIGINT to the supervisor: exit status 0 (got %s)" % rc)
    check(time.time() - t0 < 30 * SLOW, "... promptly")
    check(not leftovers(r.port), "nothing left in /dev/shm or /tmp (%s)" % leftovers(r.port))
    check(not tp.alive(second), "the switch is gone")
    check("[ERR]" not in r.text() and not re.search(r"Sanitizer", r.text() + r.err()), "no [ERR] line, no sanitizer report")
    r.cleanup()


def test_clean_exit_ends_supervisor():
    print("3. a switch that ends cleanly (SIGINT to the child itself) ends the supervisor, which removes the table")
    r = Run(True)
    check(r.up(), "started")
    h = r.host(M[0], 0)
    learn(r, [(h, M[0])])
    check(os.path.exists(mac_seg(r.port)), "the table exists")
    pid = r.switch_pid()
    r.drop_hosts()
    os.kill(pid, signal.SIGINT)
    try:
        rc = r.p.wait(40 * SLOW)
    except subprocess.TimeoutExpired:
        rc = None
    check(rc == 0, "the supervisor exited with status 0 (got %s)" % rc)
    check("exited with status 0" in r.err() and "restarting" not in r.err(), "it did not restart a switch that ended cleanly")
    check(not leftovers(r.port), "nothing left (%s)" % leftovers(r.port))
    r.cleanup()


def test_stop_during_restart_delay():
    print("4. SIGTERM to the supervisor right after a crash: it stops at once and removes the crashed switch's files")
    r = Run(True)
    check(r.up(), "started")
    h = r.host(M[0], 0)
    learn(r, [(h, M[0])])
    pid = r.switch_pid()
    os.kill(pid, CRASH)
    check(r.wait("killed by signal %d" % CRASH, 10, where=r.err), "crash seen")
    t0 = time.time()
    rc = r.stop(signal.SIGTERM)
    check(rc == 0, "exit status 0 (got %s)" % rc)
    check(time.time() - t0 < 20 * SLOW, "it did not wait for the restart delay (%.1f s)" % (time.time() - t0))
    check(not leftovers(r.port), "nothing left: the table, the counters and the admin socket of the crashed switch are removed (%s)" % leftovers(r.port))
    r.cleanup()


def test_give_up():
    print("5. giving up after 5 restarts within 60 s")
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]                                  # the switch cannot bind this port: it exits with status 1 at once
    r = Run(True, port=port)
    t0 = time.time()
    try:
        rc = r.p.wait(90 * SLOW)
    except subprocess.TimeoutExpired:
        rc = None
    blocker.close()
    check(rc == 1, "the supervisor exited with status 1 (got %s)" % rc)
    e = r.err()
    check(e.count("restarting in") == 5 and "giving up" in e, "5 restarts, then 'giving up' (%d restarts)" % e.count("restarting in"))
    check(e.count("exited with status 1") == 6, "the switch was started 6 times in all (%d)" % e.count("exited with status 1"))
    check(4 < time.time() - t0 < 60 * SLOW, "with a 1 s delay between the starts (%.1f s)" % (time.time() - t0))
    check(not leftovers(port), "nothing left")
    r.cleanup()

    r = Run(True, program="./no_such_switch")
    try:
        rc = r.p.wait(90 * SLOW)
    except subprocess.TimeoutExpired:
        rc = None
    check(rc == 1 and "cannot execute" in r.err() and "giving up" in r.err(), "a program that cannot be executed: restarted, then given up (exit %s)" % rc)
    r.cleanup()


def test_bad_command_lines():
    print("6. bad command lines")
    for name, args in (("no arguments", []), ("too few", ["./switch", "9000"]), ("bad port", ["./switch", "99999", "4"]), ("port 0", ["./switch", "0", "4"]),
                       ("port not a number", ["./switch", "abc", "4"])):
        r = subprocess.run(shlex.split(SUPERVISOR) + args, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        check(r.returncode == 1 and r.stderr.strip() != "", "%s: exit status 1 with a message (got %s)" % (name, r.returncode))
    r = subprocess.run(shlex.split(SWITCH) + ["9000", "4", "--persist-mac", "--pair", "9001"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    check(r.returncode == 1 and "cannot be combined" in r.stderr, "--persist-mac with --pair is refused")


SWITCH_PROGRAM = "./switch"
CRASH = signal.SIGSEGV                                               # TSan handles SIGSEGV itself (a report, exit status 66): its variant uses SIGKILL
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    supervised = True
    if mode == "tsan":
        SWITCH = "setarch -R ./switch_tsan"
        SUPERVISOR = "setarch -R ./supervisor"
        SWITCH_PROGRAM = "./switch_tsan"
        CRASH = signal.SIGKILL
        SLOW = 3
    elif mode == "valgrind":
        SWITCH = ("valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all --error-exitcode=1 --track-fds=yes ./switch")
        SLOW = 4
        supervised = False
    elif mode != "all":
        print("usage: test_persist.py [all|tsan|valgrind]")
        sys.exit(2)
    tests = [test_direct_persist]
    if supervised:
        tests += [test_supervisor_crash, test_clean_exit_ends_supervisor, test_stop_during_restart_delay, test_give_up]
    tests.append(test_bad_command_lines)
    try:
        for t in tests:
            try:
                t()
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(False, "%s raised %r" % (t.__name__, e))
            for o in PROCS:
                o.cleanup()
            del PROCS[:]
    finally:
        for o in PROCS:
            o.cleanup()
        for h in HOSTS:
            h.close()
        subprocess.run(["rm", "-rf", TMP])
    print("%d checks passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
