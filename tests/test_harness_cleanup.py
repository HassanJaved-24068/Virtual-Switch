#!/usr/bin/env python3
"""
Tests of the TEST HARNESS cleanup in tests/test_admin.py (run_scenario / cleanup / usock / track), used by test_admin.py and test_e2e.py.

Background: scenarios that raised used to leave their switch running (two orphaned ./switch processes were found during the end-to-end
verification). These controlled failures prove that now

  1. a scenario that completes normally leaves no switch (with and without its own sw.stop());
  2. an assertion failure in a scenario stops its switch, closes its sockets and its swctl --watch child, and the AssertionError still propagates;
  3. any other exception (and KeyboardInterrupt) does the same, including a real ./host child of the switch;
  4. a switch that ignores SIGTERM is killed (SIGKILL) after the timeout and reaped (no zombie);
  5. a switch that never reports "listening" (the constructor raises while the process is alive) is stopped too;
  6. an UNRELATED switch (started outside the harness, same program name) is not touched;
  7. an error inside cleanup does not hide the original exception and does not skip the remaining cleanup steps.

  python3 tests/test_harness_cleanup.py          (about 40 s; needs ./switch, ./host and ./swctl)
"""
import os, shlex, signal, subprocess, sys, tempfile, textwrap, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_admin as ta

check = ta.check
WORK = tempfile.mkdtemp(prefix="vswclean.")
FAKE = os.path.join(WORK, "fake_switch.py")
with open(FAKE, "w") as f:
    f.write(textwrap.dedent('''
        import os, signal, sys, time
        mode, port = sys.argv[1], sys.argv[2]          # loud: writes the "listening" line; silent: never does. Both ignore SIGTERM and SIGINT.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if mode == "loud":
            with open(os.environ["VSWITCH_LOG_FILE"], "a") as f:
                f.write("switch listening on TCP port %s\\n" % port)
        while True:
            time.sleep(0.2)
    '''))


def gone(pid):
    """Really gone: no process AND no zombie (a zombie still has a /proc entry until it is reaped)."""
    return not os.path.exists("/proc/%d" % pid)


def stubborn(mode):
    return "python3 %s %s" % (shlex.quote(FAKE), mode)


def expect_raises(fn, exc, **kw):
    try:
        ta.run_scenario(fn, **kw)
    except BaseException as e:                       # noqa: B902 - KeyboardInterrupt is part of the test
        return e if isinstance(e, exc) else AssertionError("wrong exception %r" % (e,))
    return None


# ----------------------------------------------------------------------------------------------------------------------------------------------
def main():
    box = {}

    # an unrelated switch of the same program name, started OUTSIDE the harness
    port_u = ts.free_port()
    logu = open(os.path.join(WORK, "unrelated.log"), "wb")
    unrelated = subprocess.Popen([ts.SWITCH, str(port_u), "4"], stdout=subprocess.DEVNULL, stderr=logu,
                                 env=dict(os.environ, VSWITCH_LOG_FILE=os.path.join(WORK, "unrelated_sw.log")))
    time.sleep(1.0)
    check(unrelated.poll() is None, "(setup) the unrelated switch runs")

    print("1. normal completion")
    def normal_stops():
        sw = ta.start(4)
        box["p1"] = sw.p.pid
        ta.mk_hosts(sw, 2)
        sw.stop()
    ta.run_scenario(normal_stops)
    check(gone(box["p1"]), "a scenario that stops its own switch: the process is gone")

    def normal_forgets():
        sw = ta.start(4)
        box["p1b"] = sw.p.pid
        box["sock1b"] = ta.usock()
        box["sock1b"].connect(ta.sock_path(sw))
    ta.run_scenario(normal_forgets)
    check(gone(box["p1b"]), "a scenario that returns normally WITHOUT sw.stop(): cleanup stops the switch")
    check(box["sock1b"].fileno() == -1, "... and closes its socket")
    check(ta.STARTED == [] and ta.CLIENTS == [], "the registries are empty afterwards")

    print("2. assertion failure")
    def assertion():
        sw = ta.start(4)
        box["p2"], box["port2"] = sw.p.pid, sw.port
        box["host2"] = ta.Host(sw, 1)
        box["usock2"] = ta.usock()
        box["usock2"].connect(ta.sock_path(sw))
        box["watch2"] = ta.track(subprocess.Popen([ta.SWCTL, "-p", str(sw.port), "stats", "--watch"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(0.5)
        assert 1 == 2, "boom-assertion"
    e = expect_raises(assertion, AssertionError)
    check(isinstance(e, AssertionError) and "boom-assertion" in str(e), "the original AssertionError propagates unchanged (%r)" % (e,))
    check(gone(box["p2"]), "the switch is gone")
    check(box["usock2"].fileno() == -1 and box["host2"].sock.fileno() == -1, "the admin client socket and the host socket are closed")
    check(box["watch2"].returncode is not None and gone(box["watch2"].pid), "the swctl --watch child is stopped and reaped (rc=%s)" % box["watch2"].returncode)
    check(not os.path.exists("/tmp/vswitch_%d.sock" % box["port2"]), "the socket path is removed (SIGTERM = clean shutdown)")

    print("3. other exceptions")
    def exception():
        sw = ta.start(4)
        box["p3"] = sw.p.pid
        h = sw.host("02:00:00:00:00:31")                # a real ./host process
        box["host3"] = h.pid if h else None
        raise RuntimeError("boom-exception")
    e = expect_raises(exception, RuntimeError)
    check(isinstance(e, RuntimeError) and "boom-exception" in str(e), "the original RuntimeError propagates (%r)" % (e,))
    check(gone(box["p3"]), "the switch is gone")
    check(box["host3"] is not None and gone(box["host3"]), "the real ./host process of the switch is gone too")

    def interrupted():
        sw = ta.start(4)
        box["p3b"] = sw.p.pid
        raise KeyboardInterrupt()
    e = expect_raises(interrupted, KeyboardInterrupt)
    check(isinstance(e, KeyboardInterrupt), "KeyboardInterrupt propagates")
    check(gone(box["p3b"]), "... and the switch is gone")

    print("4. a switch that ignores SIGTERM")
    def deaf():
        sw = ts.Switch(4, cmd=stubborn("loud"))
        box["p4"] = sw.p.pid
    t = time.time()
    ta.run_scenario(deaf, term_timeout=2, kill_timeout=5)
    dt = time.time() - t
    check(gone(box["p4"]), "killed and reaped (no zombie)")
    check(2.0 <= dt < 7.0, "it was given the 2 s SIGTERM timeout first, then SIGKILL (%.1f s)" % dt)

    print("5. a switch that never reports 'listening' (constructor raises while the process lives)")
    def silent():
        ts.Switch(4, cmd=stubborn("silent"))
    t = time.time()
    e = expect_raises(silent, RuntimeError, term_timeout=2, kill_timeout=5)
    check(isinstance(e, RuntimeError) and "did not start" in str(e), "the constructor's RuntimeError propagates (%r)" % (str(e)[:40],))
    leftovers = [pid for pid in os.listdir("/proc") if pid.isdigit() and FAKE in _cmdline(pid)]
    check(leftovers == [], "no fake switch process is left (%s)" % leftovers)

    print("6. an unrelated switch is not touched")
    check(unrelated.poll() is None, "the unrelated switch (same program name) is still running")
    p = subprocess.run([ta.SWCTL, "-p", str(port_u), "show", "ports"], capture_output=True, text=True, timeout=30)
    check(p.returncode == 0, "... and still answers swctl (rc=%s)" % p.returncode)

    print("7. an error inside cleanup")
    class Bad:
        def close(self):
            raise OSError("boom-in-cleanup")
    recorded = []
    real_check = ta.check
    ta.check = lambda cond, what: recorded.append((cond, what))      # run_scenario reports the cleanup error through check(): observe it, do not count it
    try:
        def orig_fail():
            ta.CLIENTS.append(Bad())
            sw = ta.start(4)
            box["p7"] = sw.p.pid
            ta.usock()
            raise AssertionError("original-failure")
        e = expect_raises(orig_fail, AssertionError)
    finally:
        ta.check = real_check
    check(isinstance(e, AssertionError) and "original-failure" in str(e), "the ORIGINAL exception is not replaced by the cleanup error (%r)" % (e,))
    check(gone(box["p7"]), "the remaining cleanup steps still ran: the switch is gone")
    check(any(not c and "cleanup finished without errors" in w for c, w in recorded), "the cleanup error was reported as a failed check")

    # stop the unrelated switch (this test started it)
    unrelated.terminate()
    try:
        unrelated.wait(30)
    except subprocess.TimeoutExpired:
        unrelated.kill()
        unrelated.wait()
    logu.close()
    check(gone(unrelated.pid), "(teardown) the unrelated switch was stopped by the test that started it")
    check(not [pid for pid in os.listdir("/proc") if pid.isdigit() and WORK in _cmdline(pid)], "no process of this test is left")
    leftover_sock = [f for f in os.listdir("/tmp") if f == "vswitch_%d.sock" % port_u]
    check(not leftover_sock, "the unrelated switch removed its socket on SIGTERM")


def _cmdline(pid):
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


if __name__ == "__main__":
    ta.install_signal_cleanup()
    try:
        main()
    finally:
        ta.cleanup()
        subprocess.run(["rm", "-rf", WORK, ta.TMP])
    print("%d checks passed, %d failed" % (ta.passed, ta.failed))
    sys.exit(1 if ta.failed else 0)
