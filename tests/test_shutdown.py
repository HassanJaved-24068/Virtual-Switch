#!/usr/bin/env python3
"""
Part 7 end-to-end tests: signal thread and clean SIGINT/SIGTERM shutdown.

  python3 tests/test_shutdown.py            normal build: tests 1-8
  python3 tests/test_shutdown.py tsan       SWITCH="setarch -R ./switch_tsan" (test 9)
  python3 tests/test_shutdown.py valgrind   valgrind on ./switch (test 10)

Every wait has a timeout, so a deadlock fails a test instead of hanging the suite.
A "clean" exit means: exit status 0, the log ends with "shutdown: complete", the log
has no [ERR] line and no sanitizer report.
"""
import os, re, shlex, signal, socket, subprocess, sys, tempfile, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_forward as tf

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

EXIT_LIMIT = float(os.environ.get("EXIT_LIMIT", "1.5"))   # "approximately one second"


def kill_and_wait(sw, sig, limit=EXIT_LIMIT):
    """Send `sig`; return (seconds until the switch exited, exit status or None on timeout)."""
    t0 = time.time()
    sw.p.send_signal(sig)
    try:
        rc = sw.p.wait(limit)
    except subprocess.TimeoutExpired:
        return time.time() - t0, None
    return time.time() - t0, rc


def clean_log(sw, what, expect_ports=None):
    log = sw.log()
    check("shutdown: complete" in log, what + ": log has 'shutdown: complete'")
    bad = [l for l in log.splitlines() if "[ERR]" in l]
    check(not bad, what + ": no [ERR] lines %s" % bad[:3])
    check("Sanitizer" not in log, what + ": no sanitizer report")
    if expect_ports is not None:
        check(log.count("teardown complete, port is FREE") == expect_ports,
              what + ": %d ports torn down by the reaper (saw %d)" %
              (expect_ports, log.count("teardown complete, port is FREE")))


def finish(sw, sig, what, expect_ports=None, limit=EXIT_LIMIT):
    dt, rc = kill_and_wait(sw, sig, limit)
    check(rc is not None, what + ": exited within %.1f s (took %.2f s)" % (limit, dt))
    check(rc == 0, what + ": exit status 0 (got %s)" % rc)
    clean_log(sw, what, expect_ports)
    return dt, rc


def wait_threads(sw, names, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        t = sw.threads()
        if all(n in t for n in names):
            return True
        time.sleep(0.02)
    return False


def flood_frames(src, n, payload):
    return tf.flood_frames(src, n, payload)


# --------------------------------------------------------------------------
def test_traffic_then_sigint():
    print("1. four hosts, traffic, SIGINT")
    sw = ts.Switch(4)
    hs = [tf.H(sw, ME[i]) for i in range(4)]
    check(all(h.ok for h in hs), "four hosts connected")
    check(wait_threads(sw, ["signal", "reaper", "aging", "accept", "rx-port4", "tx-port4"]),
          "signal, reaper, aging, accept and all port threads are running")
    for i in range(4):
        hs[i].say("send bcast hello-%d" % i)
    check(all(h.wait_rx(3, 5) for h in hs), "every host received the 3 other broadcasts")
    fds = os.listdir("/proc/%d/fd" % sw.p.pid)
    check(len(fds) >= 9, "ports are open before the signal (%d fds)" % len(fds))
    finish(sw, signal.SIGINT, "SIGINT with 4 hosts", expect_ports=4)
    for h in hs:                       # the switch closed every socket: each host sees EOF and ends
        try:
            h.p.wait(5)
        except subprocess.TimeoutExpired:
            check(False, "a host did not end after the switch closed its connection")
    check(all(h.p.returncode is not None for h in hs), "all hosts noticed the closed connection")
    sw.stop()
    for h in hs:
        h.t.join(2)


def test_no_hosts():
    print("2. SIGINT with no hosts (accept waits in poll)")
    sw = ts.Switch(4)
    check(wait_threads(sw, ["signal", "reaper", "aging", "accept"]), "all four service threads run")
    dt, rc = finish(sw, signal.SIGINT, "idle switch", expect_ports=0, limit=1.0)
    check(dt < 0.5, "exit is immediate (%.3f s)" % dt)
    sw.stop()


def test_sigint_right_after_start(rounds=40):
    print("3. SIGINT immediately after start-up (%d rounds)" % rounds)
    bad = 0
    for i in range(rounds):
        sw = ts.Switch(2)
        # as soon as the listening log line exists: threads may not be started and
        # main is not in pthread_cond_wait yet. The signal must not be lost.
        dt, rc = kill_and_wait(sw, signal.SIGINT, 3.0)
        if rc != 0 or "shutdown: complete" not in sw.log() or "[ERR]" in sw.log():
            bad += 1
            print("  round %d: rc=%s dt=%.2f\n%s" % (i, rc, dt, sw.log()))
            if sw.p.poll() is None:
                sw.p.kill()
                sw.p.wait()
        sw.stop()
    check(bad == 0, "every round exited cleanly with status 0 (%d bad)" % bad)


def test_waiting_host():
    print("4. SIGINT while a host waits for a port")
    sw = ts.Switch(2)
    a = tf.H(sw, ME[0])
    b = tf.H(sw, ME[1])
    check(a.ok and b.ok, "two hosts hold both ports")
    w = ts.connect(sw)                                       # third host: waits for a port
    check(sw.wait_log("no free port: host waits", 5), "the third host is waiting (sem_timedwait)")
    w.settimeout(5)
    finish(sw, signal.SIGINT, "waiting host", expect_ports=2, limit=1.5)
    try:
        data = w.recv(100)
    except OSError as e:
        data = b""
    check(data == b"", "the waiting host's socket was closed without a SWITCH FULL reply (got %r)" % data)
    check("closing a connection that was waiting for a port" in sw.log(), "switch logged the closed waiter")
    check("no port" not in sw.log().split("shutdown: started")[-1], "no port was allocated during shutdown")
    w.close()
    sw.stop()
    a.quit(5)
    b.quit(5)


def test_noread_host():
    print("5. SIGINT with a host that does not read (full queue)")
    sw = ts.Switch(4)
    a = tf.H(sw, ME[0], "--quiet")
    n = tf.H(sw, ME[1], "--noread")
    c = tf.H(sw, ME[2], "--quiet")
    check(a.ok and n.ok and c.ok, "three hosts connected")
    s = ts.connect(sw)
    check(sw.wait_log("port 4: host connected"), "flooding socket connected")
    block = flood_frames(ME[3], 200, b"z" * 1000)
    for _ in range(60):
        s.sendall(block)
    check(sw.wait_log("output queue full", 20), "the non-reading host's queue is full")
    finish(sw, signal.SIGINT, "non-reading host", expect_ports=4, limit=1.5)
    s.close()
    sw.stop()
    for h in (a, n, c):
        h.quit(5)


def test_waiting_and_noread():
    print("5b. SIGINT with a waiting host AND a non-reading host with a full queue (roadmap criterion)")
    sw = ts.Switch(3)
    n = tf.H(sw, ME[0], "--noread")
    a = tf.H(sw, ME[1], "--quiet")
    s = ts.connect(sw)
    check(n.ok and a.ok and sw.wait_log("port 3: host connected"), "all three ports are in use")
    w = ts.connect(sw)
    check(sw.wait_log("no free port: host waits", 5), "a fourth host waits for a port")
    block = flood_frames(ME[2], 200, b"z" * 1000)
    for _ in range(60):
        s.sendall(block)
    check(sw.wait_log("output queue full", 20), "the non-reading host's queue is full")
    w.settimeout(5)
    finish(sw, signal.SIGINT, "waiting + non-reading host", expect_ports=3, limit=1.5)
    try:
        data = w.recv(100)
    except OSError:
        data = b""
    check(data == b"", "the waiting host's socket was closed (got %r)" % data)
    w.close()
    s.close()
    sw.stop()
    for h in (n, a):
        h.quit(5)


def test_sigterm():
    print("6. SIGTERM")
    sw = ts.Switch(3)
    hs = [tf.H(sw, ME[i], "--quiet") for i in range(3)]
    check(all(h.ok for h in hs), "three hosts connected")
    finish(sw, signal.SIGTERM, "SIGTERM", expect_ports=3)
    check("SIGTERM received" in sw.log(), "SIGTERM was reported by the signal thread")
    sw.stop()
    for h in hs:
        h.quit(5)


def test_other_signals():
    print("7. SIGHUP and SIGUSR1 do not stop the switch (Part 10: they reload the configuration / dump the MAC table)")
    tmp = tempfile.mkdtemp(prefix="vswsig.")
    conf = os.path.join(tmp, "v.conf")
    with open(conf, "w") as f:
        f.write("mac_dump_file = %s\n" % os.path.join(tmp, "dump.txt"))          # keep the dump out of the repository
    sw = ts.Switch(2, extra=["-c", conf])
    h = tf.H(sw, ME[0])
    for sig, name, text in ((signal.SIGHUP, "SIGHUP", "SIGHUP received: reloading the configuration"),
                            (signal.SIGUSR1, "SIGUSR1", "SIGUSR1 received: dumping the MAC table")):
        sw.p.send_signal(sig)
        check(sw.wait_log(text, 5), name + " was handled: '%s'" % text)
        check(sw.p.poll() is None, name + ": switch still running")
    check(sw.wait_log("configuration reloaded from", 5), "SIGHUP reloaded the configuration")
    check(sw.wait_log("MAC table dumped to", 5), "SIGUSR1 dumped the MAC table")
    h.say("send bcast still-alive")
    h2 = tf.H(sw, ME[1])
    check(h2.ok, "a new host can still connect")
    h.say("send bcast after-signals")
    check(h2.wait_rx(1, 5), "traffic still forwarded")
    finish(sw, signal.SIGINT, "after SIGHUP/SIGUSR1", expect_ports=2)
    sw.stop()
    h.quit(5)
    h2.quit(5)
    subprocess.run(["rm", "-rf", tmp])


def test_repeated(rounds=40):
    print("8. repeated start / traffic / shutdown (%d rounds)" % rounds)
    bad = 0
    for i in range(rounds):
        sw = ts.Switch(2)
        hosts = []
        k = i % 3                              # 0, 1 or 2 hosts; sometimes a third one waits
        for j in range(k):
            hosts.append(tf.H(sw, ME[j], "--quiet"))
        extra = ts.connect(sw) if (i % 5 == 4 and k == 2) else None
        if hosts:
            hosts[0].say("send bcast x%d" % i)
        if i % 4 == 1:
            time.sleep(0.01 * (i % 7))
        sig = signal.SIGTERM if i % 2 else signal.SIGINT
        dt, rc = kill_and_wait(sw, sig, 3.0)
        log = sw.log()
        ok = rc == 0 and "shutdown: complete" in log and "[ERR]" not in log and dt < 1.5
        # every connected host must have been torn down exactly once by the reaper
        ok = ok and log.count("teardown complete, port is FREE") == log.count("host connected")
        if not ok:
            bad += 1
            print("  round %d: rc=%s dt=%.2f\n%s" % (i, rc, dt, log))
            if sw.p.poll() is None:
                sw.p.kill()
                sw.p.wait()
        if extra:
            extra.close()
        sw.stop()
        for h in hosts:
            h.quit(5)
    check(bad == 0, "every round exited cleanly, every port torn down once (%d bad)" % bad)


# --------------------------------------------------------------------------
def test_tsan(rounds=4):
    print("9. SIGINT during flooding (ThreadSanitizer build)")
    for r in range(rounds):
        sw = ts.Switch(4)
        a = tf.H(sw, ME[0], "--quiet")
        b = tf.H(sw, ME[1], "--quiet")
        n = tf.H(sw, ME[2], "--noread")
        s = ts.connect(sw)
        check(sw.wait_log("port 4: host connected", 20), "round %d: flooder connected" % r)
        stop = threading.Event()

        def flood():
            block = flood_frames(ME[3], 100, b"t" * 500)
            try:
                while not stop.is_set():
                    s.sendall(block)
            except OSError:
                pass

        th = threading.Thread(target=flood, daemon=True)
        th.start()
        check(sw.wait_log("RX src=%s" % ME[3], 20, 200), "round %d: flood is running" % r)
        time.sleep(0.2 * r)
        finish(sw, signal.SIGINT, "TSan round %d" % r, limit=10.0)
        stop.set()
        th.join(5)
        s.close()
        sw.stop()
        for h in (a, b, n):
            h.quit(5)


def test_valgrind():
    print("10. valgrind: traffic, SIGINT, no errors and no leaks")
    sw = ts.Switch(4)
    hs = [tf.H(sw, ME[i]) for i in range(4)]
    check(all(h.ok for h in hs), "four hosts connected under valgrind")
    for i in range(4):
        hs[i].say("send bcast hello-%d" % i)
    check(all(h.wait_rx(3, 20) for h in hs), "traffic was forwarded")
    s = ts.connect(sw)                       # a fifth connection: waits in sem_timedwait
    check(sw.wait_log("no free port: host waits", 10), "a host waits for a port")
    dt, rc = kill_and_wait(sw, signal.SIGINT, 30.0)
    check(rc == 0, "valgrind run exited with status 0 (got %s, took %.1f s)" % (rc, dt))
    log = sw.log()
    check("shutdown: complete" in log, "shutdown completed")
    check("ERROR SUMMARY: 0 errors from 0 contexts" in log, "valgrind: 0 errors")
    # With no leak at all valgrind omits the "definitely/indirectly/possibly lost" lines.
    check("in use at exit: 0 bytes in 0 blocks" in log, "valgrind: 0 bytes in use at exit")
    check(not re.search(r"(definitely|indirectly|possibly) lost: [1-9]", log), "valgrind: nothing lost")
    check("All heap blocks were freed -- no leaks are possible" in log, "valgrind: all heap blocks were freed")
    leaked_fds = re.findall(r"Open (?:AF_INET socket|file descriptor) (\d+)", log)
    leaked_fds = [f for f in leaked_fds if int(f) > 2]
    check(not leaked_fds, "valgrind: no descriptor left open at exit %s" % leaked_fds)
    if "ERROR SUMMARY: 0 errors" not in log:
        print(log[-3000:])
    s.close()
    sw.stop()
    for h in hs:
        h.quit(5)


# --------------------------------------------------------------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "all":
        for t in (test_traffic_then_sigint, test_no_hosts, test_sigint_right_after_start,
                  test_waiting_host, test_noread_host, test_waiting_and_noread, test_sigterm, test_other_signals,
                  test_repeated):
            t()
    elif mode == "tsan":
        ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
        test_tsan()
        test_no_hosts()
    elif mode == "valgrind":
        ts.SWITCH = os.environ.get("SWITCH") or (
            "valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
            "--error-exitcode=1 --track-fds=yes ./switch")
        test_valgrind()
    else:
        print("usage: test_shutdown.py [all|tsan|valgrind]")
        sys.exit(2)
    print("%d checks passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
