#!/usr/bin/env python3
"""
Part 5 end-to-end tests: port lifecycle, reaper, free-port semaphore with timeout.

Synchronisation uses the switch's own log lines ("host connected",
"teardown complete, port is FREE", "host waits") and process state, not
fixed sleeps. The only waits that are "real time" are the ones the feature
itself is about: a host waits up to port_wait_timeout (5 s).

Environment: SWITCH (default ./switch), HOST (default ./host).
"""
import os, re, shlex, signal, socket, struct, subprocess, sys, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_forward as tf

ME = ts.ME
BCAST = ts.BCAST

passed = failed = 0


def check(cond, what):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", what)


ts.check = check        # Switch.stop() checks the log for sanitizer reports
tf.check = check

TEARDOWN = "teardown complete, port is FREE"


def fast_wait(sw, text, count=1, timeout=10):
    """Wait until `text` appears `count` times in the log (polls every 2 ms)."""
    end = time.time() + timeout
    needle = text.encode()
    while time.time() < end:
        with open(sw.logf.name, "rb") as f:
            if f.read().count(needle) >= count:
                return True
        time.sleep(0.002)
    return False


def teardowns(sw):
    return sw.count(TEARDOWN)


def connects(sw):
    return sw.count("host connected")


def fds(sw):
    return len(os.listdir("/proc/%d/fd" % sw.p.pid))


def no_errors(sw):
    log = sw.log()
    bad = [l for l in log.splitlines()
           if re.search(r"close failed|join of|shutdown failed|sem_post failed|\[ERR\]", l)]
    return bad


def raw_host(sw):
    n = connects(sw)
    s = ts.connect(sw)
    ok = fast_wait(sw, "host connected", n + 1)
    return s, ok


# --------------------------------------------------------------------------
def test_normal_disconnect():
    print("lifecycle: a host leaves, its port is freed and reused")
    sw = ts.Switch(4)
    a = tf.H(sw, ME[0])
    b = tf.H(sw, ME[1])
    check(sw.count("port 1: host connected") == 1 and sw.count("port 2: host connected") == 1, "ports 1 and 2")

    b.quit()
    check(fast_wait(sw, "port 2: host disconnected"), "disconnect logged")
    check(fast_wait(sw, "port 2: " + TEARDOWN), "the reaper freed port 2")
    names = sw.threads()
    check("rx-port2" not in names and "tx-port2" not in names, "the threads of port 2 are gone: %s" % sorted(names))
    check("rx-port1" in names and "tx-port1" in names, "port 1 is untouched")

    c = tf.H(sw, ME[2])                      # the lowest FREE port is port 2 again
    check(sw.count("port 2: host connected") == 2, "the next host gets the freed port 2")
    c.say("send bcast to-a")
    check(a.wait_rx(1), "the new host on the reused port works (host A received its frame)")
    a.say("send bcast to-c")
    check(c.wait_rx(1), "and receives frames")
    check(parse_ok(c.rx()[0], ME[0]), "with the right source")
    a.quit()
    c.quit()
    check(fast_wait(sw, TEARDOWN, 3), "three teardowns for three hosts")
    check(no_errors(sw) == [], "no error lines in the log: %s" % no_errors(sw)[:2])
    sw.stop()


def parse_ok(line, src):
    return tf.parse_rx(line)[0] == src


def test_die_midframe():
    print("lifecycle: --die-midframe")
    sw = ts.Switch(4)
    a = tf.H(sw, ME[0])
    b = tf.H(sw, ME[1])
    d = tf.H(sw, ME[2], "--die-midframe")
    check(d.ok, "the dying host got port 3")
    check(fast_wait(sw, "port 3: truncated frame (payload cut short), discarded"), "truncated frame detected")
    check(fast_wait(sw, "port 3: " + TEARDOWN), "port 3 is FREE again")
    check(sw.count("port 3: RX") == 0, "the half frame was not treated as a frame")
    check(a.rx() == [] and b.rx() == [], "nothing was forwarded to the other hosts")
    a.say("send bcast still-fine")
    check(b.wait_rx(1), "the other hosts still talk to each other")
    e = tf.H(sw, ME[3])
    check(sw.count("port 3: host connected") == 2, "port 3 is reused")
    a.say("send bcast to-new")
    check(e.wait_rx(1), "the reused port receives frames")
    for h in (a, b, e):
        h.quit()
    check(no_errors(sw) == [], "no error lines")
    sw.stop()


def test_disconnect_while_forwarding():
    print("lifecycle: a host is killed while frames are being forwarded to it")
    sw = ts.Switch(4)
    b = tf.H(sw, ME[1], "--quiet")                      # port 1
    c = tf.H(sw, ME[2])                                 # port 2: prints every frame
    a, ok = raw_host(sw)                                # port 3: the flooder
    check(ok, "flooder connected")
    stop = threading.Event()
    sent = [0]
    one = ts.frame(ME[0], BCAST, 1, 1, b"f" * 100)
    block = one * 50

    def flood():
        try:
            while not stop.is_set():
                a.sendall(block)
                sent[0] += 50
        except OSError:
            pass

    t = threading.Thread(target=flood, daemon=True)
    t.start()
    check(c.wait_rx(20), "frames are flowing to C")
    c.p.send_signal(signal.SIGKILL)                    # C dies while frames are queued for it
    c.p.wait(5)
    check(fast_wait(sw, "port 2: " + TEARDOWN), "port 2 torn down while the flood continues")
    names = sw.threads()
    check("rx-port2" not in names and "tx-port2" not in names, "its threads are gone")

    d = tf.H(sw, ME[3])                                 # takes the freed port 2
    check(sw.count("port 2: host connected") == 2, "the freed port was reused during the flood")
    check(d.wait_rx(20, 15), "the new host receives the flood")
    stop.set()
    t.join(10)
    a.close()
    check(sw.p.poll() is None, "switch alive")
    d.quit()
    b.quit()
    check(b.total() is not None and b.total() > 0, "B received frames all the time (%s)" % b.total())
    check(fast_wait(sw, TEARDOWN, 4), "every host was torn down exactly once")
    check(teardowns(sw) == connects(sw), "teardowns == connections (%d)" % connects(sw))
    check(no_errors(sw) == [], "no error lines: %s" % no_errors(sw)[:2])
    sw.stop()


def test_simultaneous_failures():
    print("lifecycle: receiver read error and sender write error at about the same time (40 rounds)")
    sw = ts.Switch(4)
    a, ok = raw_host(sw)
    check(ok, "port 1 host connected")
    one = ts.frame(ME[0], BCAST, 1, 1, b"s" * 200)
    names0 = sorted(sw.threads())
    fd0 = fds(sw)
    good = True
    for rnd in range(40):
        c, ok = raw_host(sw)                            # port 2 every time
        if not ok:
            good = False
            break
        a.sendall(one * (1 + rnd * 5))                  # frames queue up for C: its sender is writing
        c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        c.close()                                       # RST: C's read and the sender's write both fail
        if not fast_wait(sw, "port 2: " + TEARDOWN, rnd + 1):
            good = False
            break
    check(good, "all 40 rounds finished")
    check(fast_wait(sw, "port 2: " + TEARDOWN, 40) and sw.count("port 2: " + TEARDOWN) == 40,
          "exactly one teardown per round (%d)" % sw.count("port 2: " + TEARDOWN))
    check(sw.count("port 2: host connected") == 40, "port 2 was handed out 40 times")
    check(sorted(sw.threads()) == names0, "thread list unchanged")
    check(fds(sw) == fd0, "descriptor count unchanged (%d)" % fds(sw))
    check(no_errors(sw) == [], "no double close / join errors: %s" % no_errors(sw)[:2])
    check(sw.p.poll() is None, "switch alive")
    a.close()
    sw.stop()


def test_wait_and_get_freed_port():
    print("lifecycle: 2 ports, 3 hosts: the third waits and gets the port freed by a quitting host")
    sw = ts.Switch(2)
    h1 = tf.H(sw, ME[0])
    h2 = tf.H(sw, ME[1])
    check(h1.ok and h2.ok, "hosts 1 and 2 connected immediately")
    check(sw.count("host waits") == 0, "no waiting for the first two")
    n = connects(sw)
    p3 = subprocess.Popen(shlex.split(tf.HOST) + ["127.0.0.1", str(sw.port), ME[2]],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    sw.hosts.append(p3)
    check(fast_wait(sw, "no free port: host waits up to 5 s for one"), "the switch says the third host waits")
    t0 = time.time()
    check(p3.poll() is None and connects(sw) == n, "it has no port yet")
    h1.quit()                                            # a host quits within 5 s
    check(fast_wait(sw, "port 1: host connected", 2), "the third host got the freed port 1")
    el = time.time() - t0
    check(el < 4.0, "without waiting for the timeout (%.2f s)" % el)
    check(sw.count("SWITCH FULL") == 0, "no SWITCH FULL")
    # the third host really works
    p3.stdin.write(b"send bcast hello-from-3\n")
    p3.stdin.flush()
    check(h2.wait_rx(1), "host 2 receives the frame of host 3")
    check(tf.parse_rx(h2.rx()[0])[0:2] == (ME[2], BCAST) and p3.poll() is None, "with the right source")
    h2.say("send bcast to-3")
    check(fast_wait(sw, "port 2: RX", 1) , "and host 2 can send")
    p3.stdin.write(b"quit\n")
    p3.stdin.flush()
    p3.wait(10)
    h2.quit()
    check(fast_wait(sw, TEARDOWN, 3), "three teardowns")
    check(teardowns(sw) == connects(sw), "teardowns == connections")
    check(no_errors(sw) == [], "no error lines")
    sw.stop()


def test_switch_full_timeout():
    print("lifecycle: 2 ports, 3 hosts, nobody quits: SWITCH FULL after about 5 s, exit status 2")
    sw = ts.Switch(2)
    h1 = tf.H(sw, ME[0])
    h2 = tf.H(sw, ME[1])
    t0 = time.time()
    p3 = subprocess.Popen(shlex.split(tf.HOST) + ["127.0.0.1", str(sw.port), ME[2]],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sw.hosts.append(p3)
    p3.wait(20)                                          # stdin stays open: EOF on stdin would mean "quit"
    out = p3.stdout.read()
    el = time.time() - t0
    check(p3.returncode == 2, "the host exits with status 2 (got %r)" % p3.returncode)
    check(out == b"SWITCH FULL\n", "and prints SWITCH FULL (got %r)" % out)
    check(4.5 <= el <= 8.0, "after about 5 s (%.2f s)" % el)
    check(fast_wait(sw, "no free port after 5 s: sending SWITCH FULL"), "the switch logged the timeout")
    check(sw.count("port 3:") == 0 and connects(sw) == 2 and teardowns(sw) == 0,
          "the rejected host never got a port and was never torn down")
    # the first two hosts were never disturbed
    h1.say("send bcast still-here")
    check(h2.wait_rx(1), "the two connected hosts still work")

    # after the timeout the token accounting is still right: a port that frees up is usable
    h1.quit()
    check(fast_wait(sw, "port 1: " + TEARDOWN), "port 1 freed")
    t0 = time.time()
    h3 = tf.H(sw, ME[2])
    check(h3.ok and time.time() - t0 < 2.0, "a new host now gets port 1 at once (%.2f s)" % (time.time() - t0))
    check(sw.count("port 1: host connected") == 2, "it is port 1")
    h2.quit()
    h3.quit()
    check(no_errors(sw) == [], "no error lines")
    sw.stop()


def test_queue_of_waiting_hosts():
    print("lifecycle: a 4th host queues behind the waiting 3rd host (one waiting host at a time)")
    sw = ts.Switch(2)
    h1 = tf.H(sw, ME[0])
    h2 = tf.H(sw, ME[1])
    procs = []
    for i in (2, 3):                                     # hosts 3 and 4 connect one after the other
        p = subprocess.Popen(shlex.split(tf.HOST) + ["127.0.0.1", str(sw.port), ME[i]],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        sw.hosts.append(p)
        procs.append(p)
        if i == 2:
            check(fast_wait(sw, "host waits", 1), "host 3 is waiting")
    check(sw.count("host waits") == 1, "host 4 is not looked at yet: the accept thread serves one waiting host")
    h1.quit()
    check(fast_wait(sw, "port 1: host connected", 2), "host 3 got port 1")
    check(fast_wait(sw, "host waits", 2), "now host 4 waits")
    h2.quit()
    check(fast_wait(sw, "port 2: host connected", 2), "host 4 got port 2")
    for p in procs:
        p.stdin.write(b"quit\n")
        p.stdin.flush()
        p.wait(10)
    check(fast_wait(sw, TEARDOWN, 4), "four teardowns")
    check(teardowns(sw) == connects(sw) == 4, "teardowns == connections")
    check(no_errors(sw) == [], "no error lines")
    sw.stop()


def test_waiting_host_gives_up():
    print("lifecycle: the waiting host hangs up before a port is free")
    sw = ts.Switch(1)
    h1 = tf.H(sw, ME[0])
    w = ts.connect(sw)                                   # waits
    check(fast_wait(sw, "host waits", 1), "it waits")
    w.close()                                            # gives up
    h1.quit()                                            # a port frees: the dead connection is admitted
    check(fast_wait(sw, "port 1: host connected", 2), "the closed connection was admitted")
    check(fast_wait(sw, TEARDOWN, 2), "and torn down again, returning its token")
    h3 = tf.H(sw, ME[2])
    check(h3.ok and sw.count("host waits") == 1, "the next host connects immediately: the token was returned")
    check(teardowns(sw) == connects(sw) - 1, "accounting: one host still connected")
    h3.quit()
    check(fast_wait(sw, TEARDOWN, 3), "all torn down")
    check(no_errors(sw) == [], "no error lines")
    sw.stop()


def test_200_cycles_and_accounting():
    print("lifecycle: 200 connect/disconnect cycles, then the token accounting is checked")
    sw = ts.Switch(2)
    # warm up once so that all lazily created things exist
    s, ok = raw_host(sw)
    s.close()
    fast_wait(sw, TEARDOWN, 1)
    names0 = sorted(sw.threads())
    fd0 = fds(sw)
    ok_all = True
    maxfd = fd0
    for i in range(200):
        s, ok = raw_host(sw)
        if not ok:
            ok_all = False
            break
        maxfd = max(maxfd, fds(sw))
        if i % 3 == 0:
            s.sendall(ts.frame(ME[0], BCAST, 1, 1, b"cycle"))
        if i % 5 == 0:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
        if not fast_wait(sw, TEARDOWN, i + 2):
            ok_all = False
            break
    check(ok_all, "all 200 cycles completed")
    check(sw.count("port 1: host connected") == 201, "every host got port 1 (it was free each time)")
    check(teardowns(sw) == connects(sw) == 201, "one teardown per connection (%d / %d)" % (teardowns(sw), connects(sw)))
    check(sorted(sw.threads()) == names0, "thread list stable: %s" % sorted(sw.threads()))
    check(fds(sw) == fd0, "descriptor count stable (%d, was %d)" % (fds(sw), fd0))
    check(maxfd <= fd0 + 1, "never more than one host descriptor at a time (max %d)" % maxfd)
    check(no_errors(sw) == [], "no double close / join / sem errors: %s" % no_errors(sw)[:2])

    # Token accounting: both ports are usable at once ...
    n_wait = sw.count("host waits")
    h1 = tf.H(sw, ME[0])
    h2 = tf.H(sw, ME[1])
    check(h1.ok and h2.ok and sw.count("host waits") == n_wait, "no token was lost: 2 hosts connect without waiting")
    # ... and not more than two (no extra token): the third host has to wait the full 5 s
    t0 = time.time()
    p3 = subprocess.Popen(shlex.split(tf.HOST) + ["127.0.0.1", str(sw.port), ME[2]],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    sw.hosts.append(p3)
    p3.wait(20)
    out = p3.stdout.read()
    el = time.time() - t0
    check(p3.returncode == 2 and out == b"SWITCH FULL\n", "no extra token: the third host gets SWITCH FULL")
    check(el >= 4.5, "after waiting the full time (%.2f s), not at once" % el)
    h1.quit()
    h2.quit()
    check(fast_wait(sw, TEARDOWN, 203), "the last two hosts torn down")
    check(no_errors(sw) == [], "no error lines")
    sw.stop()


if __name__ == "__main__":
    if not os.path.exists(shlex.split(ts.SWITCH)[-1]) or not os.path.exists(shlex.split(ts.HOST)[-1]):
        sys.exit("switch or host binary not found")
    for t in [test_normal_disconnect, test_die_midframe, test_disconnect_while_forwarding,
              test_simultaneous_failures, test_wait_and_get_freed_port, test_switch_full_timeout,
              test_queue_of_waiting_hosts, test_waiting_host_gives_up, test_200_cycles_and_accounting]:
        t()
    print("\n%d checks, %d failures" % (passed + failed, failed))
    print("ALL PASSED" if failed == 0 else "SOME FAILED")
    sys.exit(0 if failed == 0 else 1)
