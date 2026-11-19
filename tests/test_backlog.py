#!/usr/bin/env python3
"""
Tests that a teardown does not wait for the backlog of frames that a host has already queued in the kernel's socket buffers (found by reconfig_stress.sh under valgrind,
notes/bugs.md item 31). After shutdown(fd) a receiver can still read what is queued in the socket; it must NOT read and forward it while its port is being torn down.

  python3 tests/test_backlog.py            normal build
  python3 tests/test_backlog.py tsan       SWITCH="setarch -R ./switch_tsan"
  python3 tests/test_backlog.py valgrind   valgrind on ./switch (a slow switch makes the backlog take long: this is where the defect shows)

  1. SIGINT with four flooding hosts: the switch exits quickly, and (almost) no frame is forwarded after the teardown has been requested.
  2. swctl shutdown of a flooding port: it answers OK quickly (well inside the 5 s limit), the port is DISABLED, the other flooding hosts keep forwarding.
"""
import os, re, signal, subprocess, sys, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_admin as ta
import test_reconfig as tr

check = ta.check
FLOOD = 300000            # frames per host: far more than the kernel's socket buffers hold, so the hosts are still sending when the switch is told to stop
LIMIT = 4.0               # seconds the shutdown may take (a slow variant gets more)
AFTER = 40                # frames per port that may still be forwarded after the teardown was requested (the one being processed + a margin)


def flooders(sw, n_hosts, base=1):
    out = []
    for i in range(base, base + n_hosts):
        p = ta.track(subprocess.Popen(["./host", "127.0.0.1", str(sw.port), ts.ME[i], "--quiet", "--flood", str(FLOOD), "--to", "bcast"],
                                      stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        out.append(p)
        check(sw.wait_log("host connected", 60, len(out)), "flooding host %d connected" % i)
    return out


def test_sigint_with_backlog():
    print("1. SIGINT while four hosts flood: the shutdown does not process their backlog")
    sw = ta.start(8)
    flooders(sw, 4)
    time.sleep(2.5 * SLOW)                               # the sockets are full now
    t = time.time()
    sw.p.send_signal(signal.SIGINT)
    try:
        rc = sw.p.wait(120)
    except subprocess.TimeoutExpired:
        sw.p.kill()
        rc = None
    dt = time.time() - t
    check(rc == 0, "exit status 0 (got %s)" % rc)
    check(dt < LIMIT * SLOW, "the switch exited %.1f s after SIGINT (limit %.0f s)" % (dt, LIMIT * SLOW))
    log = sw.log()
    key = "shutdown: teardown requested"
    after = log.split(key)[-1].count("RX src=") if key in log else -1
    check(key in log, "the shutdown log shows the teardown request (the logger may drop lines under a flood: %s)" % (key in log))
    if key in log:
        check(after <= AFTER * 4, "frames forwarded after the teardown was requested: %d (at most %d)" % (after, AFTER * 4))
    sw.stop()


def test_admin_shutdown_with_backlog():
    print("2. swctl shutdown of a flooding port answers quickly; the other flooding ports go on")
    sw = ta.start(8)
    flooders(sw, 4)
    time.sleep(2.0 * SLOW)
    before = ta.parse_stats(ta.run_stats("stats")[1])
    t = time.time()
    rc, out, err = ta.ctl(sw, "shutdown", "1", timeout=60)
    dt = time.time() - t
    check(rc == 0, "shutdown 1 (a port with a big backlog): OK (rc=%s %r)" % (rc, err.strip()))
    check(dt < 3.0 * SLOW, "it answered after %.1f s (the limit of the command is 5 s)" % dt)
    check(tr.state(sw, 1) == "DISABLED", "port 1 is DISABLED")
    time.sleep(1.0)
    after = ta.parse_stats(ta.run_stats("stats")[1])
    check(all(after[p][2] > before[p][2] for p in (2, 3, 4)), "the other flooding hosts kept sending: in counters grew %s" % [(after[p][2] - before[p][2]) for p in (2, 3, 4)])
    rc, dt = ta.sigint_and_wait(sw, 120)
    check(rc == 0, "SIGINT afterwards: exit status 0 (got %s)" % rc)
    sw.stop()


SLOW = 1
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    ta.install_signal_cleanup()
    try:
        if mode == "all":
            ta.run_tests((test_sigint_with_backlog, test_admin_shutdown_with_backlog))
        elif mode == "tsan":
            ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
            SLOW = 3
            FLOOD = 60000
            ta.run_tests((test_sigint_with_backlog, test_admin_shutdown_with_backlog))
        elif mode == "valgrind":
            ts.SWITCH = os.environ.get("SWITCH") or (
                "valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
                "--error-exitcode=1 --track-fds=yes ./switch")
            SLOW = 4
            FLOOD = 30000
            ta.run_tests((test_sigint_with_backlog, test_admin_shutdown_with_backlog))
        else:
            print("usage: test_backlog.py [all|tsan|valgrind]")
            sys.exit(2)
    finally:
        ta.cleanup()
        subprocess.run(["rm", "-rf", ta.TMP])
    print("%d checks passed, %d failed" % (ta.passed, ta.failed))
    sys.exit(1 if ta.failed else 0)
