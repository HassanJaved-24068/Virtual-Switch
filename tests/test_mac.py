#!/usr/bin/env python3
"""
Part 6 end-to-end tests: MAC learning, unicast forwarding, aging, LRU eviction,
MAC moves and port security, with the real switch and real hosts.

Most tests use the normal ./switch (K=1024, T=300 s, M=16). The aging, eviction
and port-security tests use ./switch_small: the same source built with a table
of 4 entries, aging after 3 s and at most 3 MACs per port (there is no config
file before Part 10).

Environment: SWITCH (default ./switch), SWITCH_SMALL (default ./switch_small), HOST.
"""
import os, re, shlex, signal, socket, struct, subprocess, sys, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_forward as tf

SMALL = os.environ.get("SWITCH_SMALL", "./switch_small")
BCAST = ts.BCAST
ME = ts.ME
MCAST = "01:00:5e:00:00:01"


def M(n):
    """A MAC address for any number n (the shared ME list has only 80)."""
    return "02:00:00:aa:%02x:%02x" % (n >> 8, n & 255)

TEARDOWN = "teardown complete, port is FREE"
DISABLED = "teardown complete, port is DISABLED"

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


def fast_wait(sw, text, count=1, timeout=10):
    end = time.time() + timeout
    needle = text.encode()
    while time.time() < end:
        with open(sw.logf.name, "rb") as f:
            if f.read().count(needle) >= count:
                return True
        time.sleep(0.002)
    return False


def data_of(h):
    """The payloads a real host has received, in order."""
    return [tf.parse_rx(l)[5] for l in h.rx()]


def absent(h, text, wait=0.3):
    time.sleep(wait)                      # only used to show that something does NOT arrive
    return text not in data_of(h)


def got(h, text, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if text in data_of(h):
            return True
        time.sleep(0.01)
    return False


def four_hosts(sw):
    hs = [tf.H(sw, ME[i]) for i in range(4)]
    assert all(h.ok for h in hs)
    return hs


# --------------------------------------------------------------------------
def test_unicast_learning():
    print("MAC table: flood until the destination has been seen, then unicast")
    sw = ts.Switch(8)
    A, B, C, D = four_hosts(sw)

    A.say("send %s u1" % ME[2])                         # C has not sent anything yet
    check(got(B, "u1") and got(C, "u1") and got(D, "u1"), "1. A -> C before C was seen: flooded to B, C and D")
    check(data_of(A) == [], "   and not back to A")

    C.say("send bcast c1")                              # 2. C is learned from its source address
    check(got(A, "c1") and got(B, "c1") and got(D, "c1"), "2. C sends once (broadcast reaches everybody)")
    check(fast_wait(sw, "port 3: RX src=%s" % ME[2]), "   the switch saw C's frame")

    A.say("send %s u2" % ME[2])
    check(got(C, "u2"), "3. A -> C now reaches C")
    check(absent(B, "u2") and absent(D, "u2"), "   and ONLY C (B and D never see it)")

    A.say("send %s u3" % "02:00:00:00:00:99")
    check(got(B, "u3") and got(C, "u3") and got(D, "u3"), "4. unknown destination: flooded to B, C, D")

    B.say("send bcast b1")
    check(got(A, "b1") and got(C, "b1") and got(D, "b1"), "5. broadcast floods to everybody else")
    check(absent(B, "b1"), "   but never back to the sender")

    A.say("send %s m1" % MCAST)
    check(got(B, "m1") and got(C, "m1") and got(D, "m1"), "5b. multicast destination: flooded")

    D.say("send %s d1" % ME[0])                         # A is known (it sent u1..): only A
    check(got(A, "d1"), "6. D -> A (A is known) reaches A")
    check(absent(B, "d1") and absent(C, "d1"), "   and only A")

    # the unicast frame arrives unchanged
    A.say("sendx %s 1 0 exact-payload" % ME[2])
    check(got(C, "exact-payload"), "7. payload intact")
    line = [l for l in C.rx() if l.endswith("data=exact-payload")][0]
    check(tf.parse_rx(line)[:5] == (ME[0], ME[2], 1, 0, 13), "   header fields intact: %s" % line)
    for h in (A, B, C, D):
        h.quit()
    check(no_errors(sw), "no error lines in the switch log")
    sw.stop()


def no_errors(sw):
    log = sw.log()
    return not re.search(r"\[ERR\]|close failed|join of|sem_post failed", log)


def test_filter_and_source_rules():
    print("MAC table: filtering, broadcast and multicast sources")
    sw = ts.Switch(8)
    obs = tf.H(sw, ME[5])                               # port 1: observer (real host)
    raw = ts.connect(sw)                                # port 2: a raw host with several MACs
    fast_wait(sw, "port 2: host connected")
    X, Y = ME[10], ME[11]

    raw.sendall(ts.frame(X, BCAST, 1, 1, b"learn-x") + ts.frame(Y, BCAST, 1, 1, b"learn-y"))
    check(got(obs, "learn-x") and got(obs, "learn-y"), "two MACs learned behind port 2")
    raw.sendall(ts.frame(X, Y, 1, 1, b"filtered"))       # Y is behind the SAME port: filtered
    raw.sendall(ts.frame(X, BCAST, 1, 1, b"marker-1"))
    check(got(obs, "marker-1"), "6. marker frame arrives")
    check("filtered" not in data_of(obs), "   the frame for a MAC behind the ingress port was filtered (dropped)")

    # a frame from a broadcast source address is dropped
    raw.sendall(ts.frame("ff:ff:ff:ff:ff:ff", BCAST, 1, 1, b"bcast-source"))
    raw.sendall(ts.frame(X, BCAST, 1, 1, b"marker-2"))
    check(got(obs, "marker-2") and "bcast-source" not in data_of(obs), "a broadcast SOURCE is dropped")

    # a multicast source is forwarded but never learned
    raw.sendall(ts.frame(MCAST, BCAST, 1, 1, b"mcast-source"))
    check(got(obs, "mcast-source"), "a multicast source is forwarded")
    obs.say("send %s to-mcast-src" % MCAST)             # nothing is learned for it: flooded (dst is multicast)
    check(fast_wait(sw, "port 1: RX src=%s" % ME[5]), "observer sent")
    check(sw.count("port 2: RX src=%s" % MCAST) == 1, "multicast source frame was received once")

    # the vlan is part of the key
    raw.sendall(ts.frame(X, BCAST, 7, 1, b"in-vlan-7"))
    obs.say("sendx %s 7 1 to-x-vlan7" % X)
    check(got(obs, "in-vlan-7"), "frame in vlan 7 forwarded")
    raw.settimeout(5)
    d = b""
    while len(d) < 17 + 13 and b"to-x-vlan7" not in d:
        try:
            c = raw.recv(4096)
        except socket.timeout:
            break
        if not c:
            break
        d += c
    check(b"to-x-vlan7" in d, "the raw host got the vlan-7 unicast (X was learned in vlan 7 on its port)")
    raw.close()
    obs.quit()
    sw.stop()


def test_mac_move_and_flush():
    print("MAC table: a MAC moves between ports; a leaving host's MACs are flushed")
    sw = ts.Switch(8)
    P = tf.H(sw, ME[0])                                 # port 1, MAC X
    Q = tf.H(sw, ME[1])                                 # port 2
    R = tf.H(sw, ME[0])                                 # port 3: the SAME MAC X (a host that "moved")
    X = ME[0]
    P.say("send bcast p1")
    check(got(Q, "p1"), "X is learned on port 1")
    Q.say("send %s q1" % X)
    check(got(P, "q1") and absent(R, "q1"), "unicast to X goes to port 1 only")

    R.say("send bcast r1")                              # the same MAC appears on port 3
    check(fast_wait(sw, "MAC moved: %s vlan 1 port 1 -> 3" % X), "'MAC moved: ... vlan 1 port 1 -> 3' is logged")
    check(got(Q, "r1"), "the frame that moved it was forwarded normally")
    Q.say("send %s q2" % X)
    check(got(R, "q2"), "after the move, unicast to X goes to port 3")
    check(absent(P, "q2"), "   and no longer to port 1")
    P.say("send bcast p2")
    check(fast_wait(sw, "MAC moved: %s vlan 1 port 3 -> 1" % X), "and back: 'port 3 -> 1' is logged")
    check(sw.count("MAC moved") == 2, "exactly two moves logged")

    # flush at teardown: Q's MAC is learned, then Q leaves
    Q.say("send bcast q3")
    check(got(P, "q3"), "Q learned")
    S = tf.H(sw, ME[3])                                 # port 4
    S.say("send %s s1" % ME[1])
    check(got(Q, "s1") and absent(P, "s1") and absent(R, "s1"), "unicast to Q reaches only Q")
    Q.quit()
    check(fast_wait(sw, "port 2: " + TEARDOWN), "Q's port was torn down (the reaper flushed its MACs)")
    T = tf.H(sw, ME[7])                                 # a NEW host takes the freed port 2
    check(sw.count("port 2: host connected") == 2, "the new host T got port 2")
    S.say("send %s s2" % ME[1])
    # With a stale entry (Q's MAC -> port 2) the frame would go to T only, to the wrong machine.
    check(got(P, "s2") and got(R, "s2") and got(T, "s2"),
          "Q's MAC was flushed: the unicast to Q is flooded (not sent to T, who now owns the port)")
    for h in (P, R, S, T):
        h.quit()
    check(no_errors(sw), "no error lines")
    sw.stop()


def test_aging():
    print("MAC table: with T = 3 s a MAC disappears after about 3-4 s (switch_small)")
    sw = ts.Switch(8, cmd=SMALL)
    A = tf.H(sw, ME[0])
    B = tf.H(sw, ME[1])
    C = tf.H(sw, ME[2])
    C.say("send bcast c1")
    check(got(A, "c1"), "C learned")
    t0 = time.time()
    A.say("send %s probe-0" % ME[2])
    check(got(C, "probe-0") and absent(B, "probe-0"), "right after learning, unicast to C is exclusive")

    # A probes every 0.25 s. A's own entry stays fresh (its frames refresh it), but the probes
    # are LOOKUPS of C and must not refresh C. When C's entry is gone the probe is flooded to B.
    seen = None
    n = 0
    while time.time() - t0 < 8.0:
        n += 1
        A.say("send %s probe-%d" % (ME[2], n))
        time.sleep(0.25)
        if "probe-%d" % n in data_of(B):
            seen = time.time() - t0
            break
    print("  entry gone after %.2f s" % (seen or -1))
    check(seen is not None, "the entry aged out (a probe reached B)")
    check(seen is not None and 2.9 <= seen <= 4.8, "after about 3-4 s (%.2f s)" % (seen or -1))

    C.say("send bcast c2")                               # C talks again: learned again
    check(got(A, "c2"), "C sends again")
    A.say("send %s probe-after" % ME[2])
    check(got(C, "probe-after") and absent(B, "probe-after"), "and is exclusive again")
    for h in (A, B, C):
        h.quit()
    sw.stop()


def test_active_mac_does_not_age():
    print("MAC table: a MAC that keeps sending is not aged out (T = 3 s, 7 s of traffic)")
    sw = ts.Switch(8, cmd=SMALL)
    A = tf.H(sw, ME[0])
    B = tf.H(sw, ME[1])
    C = tf.H(sw, ME[2])
    t0 = time.time()
    ok = True
    n = 0
    while time.time() - t0 < 7.0:
        n += 1
        C.say("send bcast keep-%d" % n)                  # C is seen as a source every second
        if not got(A, "keep-%d" % n):
            ok = False
        A.say("send %s to-c-%d" % (ME[2], n))
        if not got(C, "to-c-%d" % n):
            ok = False
        time.sleep(1.0)
    check(ok, "all frames arrived")
    check(all(("to-c-%d" % i) not in data_of(B) for i in range(1, n + 1)),
          "every unicast to C stayed exclusive for 7 s (never flooded to B): %d frames" % n)
    for h in (A, B, C):
        h.quit()
    sw.stop()


def test_lru_eviction():
    print("MAC table: with K = 4 and six hosts the oldest entries are evicted (switch_small)")
    sw = ts.Switch(8, cmd=SMALL)
    hs = []
    for i in range(6):                                   # ports 1-6, MACs ME[0..5]
        hs.append(tf.H(sw, ME[i]))
    probe = ts.connect(sw)                               # port 7: sends with a multicast SOURCE, so it
    fast_wait(sw, "port 7: host connected")              # never changes the table (it only looks up)
    marker_n = [0]

    def send_from(i, text):
        """Host i sends a broadcast; returns when the switch has received (and so learned) it."""
        key = "port %d: RX" % (i + 1)
        n = sw.count(key)
        hs[i].say("send bcast %s" % text)
        assert fast_wait(sw, key, n + 1)

    def exclusive(i, tag):
        """True if only host i got the probe for its MAC (it is in the table); False if it was
        flooded. A flooded marker frame sent right after the probe travels through the same
        queues, so when every host has the marker it has the probe too (if it was sent to it)."""
        marker_n[0] += 1
        marker = "marker-%d" % marker_n[0]
        probe.sendall(ts.frame(MCAST, ME[i], 1, 1, tag.encode()))
        probe.sendall(ts.frame(MCAST, "01:00:5e:00:00:02", 1, 1, marker.encode()))
        if not all(got(h, marker) for h in hs):
            return None
        if tag not in data_of(hs[i]):
            return None
        return all(tag not in data_of(hs[j]) for j in range(6) if j != i)

    for i in range(4):                                   # hosts 1-4 fill the table (K = 4)
        send_from(i, "hello-%d" % i)
    check(all(exclusive(i, "p1-%d" % i) is True for i in range(4)), "the 4 learned hosts are all in the table")

    send_from(4, "hello-4")                              # a 5th MAC: the oldest (host 1) is evicted
    check(exclusive(0, "p2-0") is False, "5th MAC: host 1 (the oldest) was evicted -> its unicast floods")
    check(all(exclusive(i, "p2-%d" % i) is True for i in (1, 2, 3, 4)), "   hosts 2, 3, 4 and 5 are in the table")

    send_from(5, "hello-5")                              # a 6th MAC: host 2 goes
    check(exclusive(1, "p3-1") is False, "6th MAC: host 2 (now the oldest) was evicted")
    check(all(exclusive(i, "p3-%d" % i) is True for i in (2, 3, 4, 5)), "   hosts 3, 4, 5, 6 remain")

    # LRU: host 3 sends again (it becomes the newest), then host 1 comes back: the oldest is now host 4
    send_from(2, "again-2")
    send_from(0, "back-0")
    check(exclusive(0, "p4-0") is True, "host 1 is learned again")
    check(exclusive(2, "p4-2") is True, "host 3 survived (it was refreshed)")
    check(exclusive(3, "p4-3") is False, "host 4 (the least recently seen) was evicted")
    probe.close()
    for h in hs:
        h.quit()
    check(no_errors(sw), "no error lines")
    sw.stop()


def test_port_security():
    print("MAC table: with M = 3 a host using 4 source MACs gets its port DISABLED (switch_small)")
    sw = ts.Switch(4, cmd=SMALL)
    obs = tf.H(sw, ME[9])                                 # port 1: observer
    r, ok = ts.connect(sw), fast_wait(sw, "port 2: host connected")   # port 2: the offender
    macs = [ME[20 + i] for i in range(4)]
    for i in range(3):                                    # exactly M = 3: fine
        r.sendall(ts.frame(macs[i], BCAST, 1, 1, b"ok-%d" % i))
    check(all(got(obs, "ok-%d" % i) for i in range(3)), "3 source MACs: all forwarded")
    check(sw.count("shut down") == 0, "3 = M: no violation")
    r.sendall(ts.frame(macs[3], BCAST, 1, 1, b"fourth"))
    check(fast_wait(sw, "port 2 shut down: MAC flooding"), "4th MAC: 'port 2 shut down: MAC flooding'")
    check(fast_wait(sw, "port 2: " + DISABLED), "port 2 ends DISABLED (not FREE)")
    check(absent(obs, "fourth", 0.2), "the frame with the 4th MAC was dropped")
    r.settimeout(5)
    try:
        closed = r.recv(100) == b""
    except OSError:
        closed = True
    check(closed, "the switch closed the offender's connection")
    r.close()
    check(sw.p.poll() is None, "switch alive (no deadlock)")

    # The port is not reused and its token is not returned: of the 4 ports one is the
    # observer, one is DISABLED, so only 2 are left for new hosts.
    h2 = tf.H(sw, ME[10])
    h3 = tf.H(sw, ME[11])
    check(h2.ok and h3.ok, "two more hosts connect without waiting")
    check(sw.count("port 3: host connected") == 1 and sw.count("port 4: host connected") == 1,
          "they got ports 3 and 4: the DISABLED port 2 is skipped")
    check(sw.count("port 2: host connected") == 1, "port 2 was never given out again")
    check(sw.count("host waits") == 0, "no waiting so far")
    h2.say("send bcast alive")
    check(got(obs, "alive") and got(h3, "alive"), "traffic still flows")
    w = ts.connect(sw)                                    # no port is left for this one
    check(fast_wait(sw, "no free port: host waits"), "a 4th host has to wait: the DISABLED port's token was not returned")
    w.close()
    for h in (obs, h2, h3):
        h.quit()
    sw.stop()


def test_port_security_details():
    print("MAC table: multicast sources and repeated MACs do not count; vlans do")
    sw = ts.Switch(4, cmd=SMALL)
    obs = tf.H(sw, ME[9])
    r = ts.connect(sw)
    fast_wait(sw, "port 2: host connected")
    for i in range(6):                                    # six different multicast sources: never learned
        r.sendall(ts.frame("01:00:5e:00:00:%02x" % (i + 1), BCAST, 1, 1, b"mc-%d" % i))
    r.sendall(ts.frame(ME[30], BCAST, 1, 1, b"one-real"))
    for i in range(10):                                   # the same MAC again and again: one entry
        r.sendall(ts.frame(ME[30], BCAST, 1, 1, b"same-%d" % i))
    check(got(obs, "same-9") and got(obs, "mc-5"), "all frames forwarded")
    check(sw.count("shut down") == 0, "6 multicast sources + 1 MAC repeated: no violation")
    r.sendall(ts.frame(ME[31], BCAST, 1, 1, b"two") + ts.frame(ME[32], BCAST, 1, 1, b"three"))
    check(got(obs, "three") and sw.count("shut down") == 0, "3 MACs: still fine")
    r.sendall(ts.frame(ME[30], BCAST, 2, 1, b"vlan-2"))   # the same MAC in another vlan = a 4th entry
    check(fast_wait(sw, "port 2 shut down: MAC flooding"), "the same MAC in a 2nd vlan counts as another entry")
    check(fast_wait(sw, "port 2: " + DISABLED), "-> DISABLED")
    r.close()
    obs.quit()
    sw.stop()


def test_security_under_traffic():
    print("MAC table: three ports violate port security while traffic flows (no deadlock)")
    sw = ts.Switch(8, cmd=SMALL)
    obs = tf.H(sw, ME[9], "--quiet")                      # port 1
    fl = ts.connect(sw)                                   # port 2: a flooder with one MAC
    fast_wait(sw, "port 2: host connected")
    bad = []
    for _ in range(3):
        bad.append(ts.connect(sw))
        fast_wait(sw, "host connected", len(bad) + 2)
    stop = threading.Event()
    one = ts.frame(ME[40], BCAST, 1, 1, b"f" * 50) * 20

    def flood():
        try:
            while not stop.is_set():
                fl.sendall(one)
        except OSError:
            pass

    th = threading.Thread(target=flood, daemon=True)
    th.start()
    time.sleep(0.2)

    def offend(s, k):
        try:
            for i in range(40):
                s.sendall(ts.frame(M(50 + k * 40 + i), BCAST, 1, 1, b"x"))
        except OSError:
            pass

    ths = [threading.Thread(target=offend, args=(s, k), daemon=True) for k, s in enumerate(bad)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(10)
    check(fast_wait(sw, DISABLED, 3, 10), "all three offenders were shut down (3 x DISABLED)")
    check(sw.count("MAC flooding") == 3, "exactly three 'MAC flooding' messages (%d)" % sw.count("MAC flooding"))
    time.sleep(0.5)
    check(sw.p.poll() is None, "switch alive")
    stop.set()
    th.join(10)
    fl.close()
    obs.quit()
    check(obs.total() is not None and obs.total() > 0, "the observer kept receiving the flood (%s frames)" % obs.total())
    for s in bad:
        s.close()
    check(no_errors(sw), "no error lines in the log")
    sw.stop()


def test_default_limits():
    print("MAC table: the normal switch allows 16 MACs per port")
    sw = ts.Switch(4)
    obs = tf.H(sw, ME[9])
    r = ts.connect(sw)
    fast_wait(sw, "port 2: host connected")
    for i in range(16):
        r.sendall(ts.frame(ME[60 + i], BCAST, 1, 1, b"m-%d" % i))
    check(got(obs, "m-15"), "16 MACs: all forwarded")
    check(sw.count("shut down") == 0, "16 = M: still fine")
    r.sendall(ts.frame(M(80), BCAST, 1, 1, b"m-16"))
    check(fast_wait(sw, "port 2 shut down: MAC flooding"), "the 17th MAC shuts the port down")
    check("m-16" not in data_of(obs), "and its frame was dropped")
    r.close()
    obs.quit()
    sw.stop()


if __name__ == "__main__":
    for b in (ts.SWITCH, SMALL, ts.HOST):
        if not os.path.exists(shlex.split(b)[-1]):
            sys.exit("binary not found: " + b)
    for t in [test_unicast_learning, test_filter_and_source_rules, test_mac_move_and_flush,
              test_default_limits, test_port_security, test_port_security_details,
              test_security_under_traffic, test_lru_eviction, test_active_mac_does_not_age, test_aging]:
        t()
    print("\n%d checks, %d failures" % (passed + failed, failed))
    print("ALL PASSED" if failed == 0 else "SOME FAILED")
    sys.exit(0 if failed == 0 else 1)
