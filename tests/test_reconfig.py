#!/usr/bin/env python3
"""
Part 16 end-to-end tests: live reconfiguration (swctl shutdown / up / vlan / clear mac) of the real ./switch.

  python3 tests/test_reconfig.py            normal build
  python3 tests/test_reconfig.py tsan       SWITCH="setarch -R ./switch_tsan"
  python3 tests/test_reconfig.py valgrind   valgrind on ./switch

The 500-operation stress with signals (the roadmap's completion criterion) is tests/reconfig_stress.sh. Here the semantics, one by one:
shutdown of a FREE port and of an UP port (the host is disconnected, its MACs are flushed, the port stays out of circulation), up, the error
replies, exactly one winner when several shutdowns of the same port race, a host that disconnects at the same moment, the VLAN rules (any state, a free
port keeps it for its next host, the trunk is refused, a configuration change replaces it), clear mac, hosts on untouched ports keep forwarding,
the free-port semaphore invariant after a storm of operations, and SIGINT while admin commands are running.
Uses the cleanup machinery of test_admin.py (run_scenario): a failing scenario leaves no switch behind.
"""
import os, random, re, select, signal, socket, struct, subprocess, sys, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_admin as ta

check = ta.check
BCAST = ts.BCAST
SLOW = 1


def wait_for(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def ports(sw):
    rc, out, err = ta.ctl(sw, "show", "ports")
    return ta.parse_ports(out) if rc == 0 else {}


def state(sw, p):
    return ports(sw).get(p, ("?",))[0]


def macs(sw):
    return ta.parse_macs(ta.ctl(sw, "show", "mac")[1])


def connect(sw, idx, read=True):
    """A raw host on the lowest FREE port (waits until the switch logged it)."""
    n = sw.count("host connected")
    h = ta.Host(sw, idx, read)
    check(sw.wait_log("host connected", 30, n + 1), "host %d got a port" % idx)
    return h


def at_eof(sock, timeout=10):
    """True if the switch closed this connection (a read returns EOF or fails)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            r, _, _ = select.select([sock], [], [], 0.2)
            if r and sock.recv(65536) == b"":
                return True
        except OSError:
            return True
    return False


def switch_full_frame(sock, timeout=15):
    """Read the 'switch full' frame the switch sends to a host that finds no port."""
    sock.settimeout(timeout)
    buf = b""
    try:
        while len(buf) < 17:
            d = sock.recv(4096)
            if not d:
                break
            buf += d
        if len(buf) >= 17:
            n = struct.unpack("!H", buf[:2])[0]
            while len(buf) < 17 + n:
                d = sock.recv(4096)
                if not d:
                    break
                buf += d
            return buf[17:17 + n]
    except OSError:
        pass
    return None


# ----------------------------------------------------------------------------------------------
def test_shutdown_free_port():
    print("1. shutdown of a FREE port: DISABLED, out of circulation, the other ports still work, up brings it back")
    sw = ta.start(3, "port_wait_timeout = 1\n")
    rc, out, err = ta.ctl(sw, "shutdown", "2")
    check(rc == 0 and "port 2 disabled" in out, "shutdown 2: OK (rc=%s %r)" % (rc, out.strip()))
    check(state(sw, 2) == "DISABLED" and state(sw, 1) == "FREE" and state(sw, 3) == "FREE", "port 2 DISABLED, 1 and 3 FREE: %s" % [r[0] for r in ports(sw).values()])
    rc, out, err = ta.ctl(sw, "shutdown", "2")
    check(rc == 1 and "already disabled" in err, "shutdown again: ERR already disabled (%r)" % err.strip())
    h1 = connect(sw, 1)
    h2 = connect(sw, 2)
    st = ports(sw)
    check(st[1][0] == "UP" and st[2][0] == "DISABLED" and st[3][0] == "UP", "the second host got port 3, not the disabled port 2: %s" % [st[i][0] for i in (1, 2, 3)])
    s3 = ts.connect(sw)                                  # no FREE port: waits 1 s, then 'switch full'
    t0 = time.time()
    check(switch_full_frame(s3) == b"switch full", "a third host is told 'switch full' (2 ports usable, 1 disabled)")
    dt = time.time() - t0
    check(dt >= 0.8, "... only AFTER it waited port_wait_timeout = 1 s for a port (%.2f s): the disabled port is not a free port, but no token is left for it either" % dt)
    s3.close()
    rc, out, err = ta.ctl(sw, "up", "2")
    check(rc == 0 and "FREE again" in out, "up 2: OK (rc=%s %r)" % (rc, out.strip()))
    check(state(sw, 2) == "FREE", "port 2 FREE")
    h3 = connect(sw, 3)
    check(state(sw, 2) == "UP", "the next host got port 2 again")
    for h in (h1, h2, h3):
        h.close()
    sw.stop()


def test_shutdown_up_port():
    print("2. shutdown of an UP port: the host is disconnected, its MACs flushed, the port stays DISABLED until up")
    sw = ta.start(4, "port_wait_timeout = 1\n")
    h1 = connect(sw, 1, read=False)
    h2 = connect(sw, 2)
    h3 = connect(sw, 3)
    h1.send(BCAST, 1, b"learn")
    h2.send(BCAST, 1, b"learn")
    check(wait_for(lambda: len(macs(sw)) == 2), "two MACs learned")
    rows = ports(sw)
    check(rows[1][3] == "1" and rows[2][3] == "1", "show ports: one MAC on port 1 and one on port 2")
    rc, out, err = ta.ctl(sw, "shutdown", "1")
    check(rc == 0 and "port 1 disabled" in out, "shutdown 1 (UP): OK, and it answers after the teardown (rc=%s %r)" % (rc, out.strip()))
    check(state(sw, 1) == "DISABLED", "the state is DISABLED when the command answered")
    check(at_eof(h1.sock), "host 1's connection was closed by the switch")
    m = macs(sw)
    check([x[2] for x in m] == [2], "its MAC entry is flushed, port 2's stays (%s)" % m)
    check(ports(sw)[1][3] == "0", "show ports: 0 MACs on port 1")
    check(sw.wait_log("port 1: teardown complete, port is DISABLED", 10), "the reaper logged the teardown as DISABLED")
    check(sw.wait_log("port 1: disabled by an admin", 10), "the admin command logged its success")
    log = sw.log()
    check(log.index("port 1: teardown complete, port is DISABLED") < log.index("port 1: disabled by an admin"),
          "the command answered only AFTER the reaper finished the teardown (log order: teardown complete, then the admin's answer)")
    h4 = connect(sw, 4)
    st = ports(sw)
    check(st[1][0] == "DISABLED" and st[4][0] == "UP", "a new host does not get the disabled port 1: it got port 4 (%s)" % [st[i][0] for i in (1, 2, 3, 4)])
    rc, out, err = ta.ctl(sw, "up", "1")
    check(rc == 0, "up 1: OK")
    check(state(sw, 1) == "FREE", "port 1 FREE")
    h5 = connect(sw, 5)
    check(state(sw, 1) == "UP", "the next host got port 1 (its security counter started from 0)")
    check(ports(sw)[1][3] == "0", "port security: 0 MACs on the new host's port")
    # the other hosts never noticed
    for h in (h2, h3):
        h.frames = 0
    h2.send(BCAST, 1, b"still alive")
    check(wait_for(lambda: h3.frames == 1), "traffic between the untouched ports 2 and 3 goes on")
    for h in (h1, h2, h3, h4, h5):
        h.close()
    sw.stop()


def test_errors():
    print("3. error replies: up on a port that is not disabled, vlan on the trunk, bad arguments are still validated")
    sw = ta.start(4, "trunk_port = 4\n")
    h1 = connect(sw, 1)
    for port, want in ((1, "UP"), (2, "FREE")):
        rc, out, err = ta.ctl(sw, "up", str(port))
        check(rc == 1 and "not disabled" in err and want in err, "up %d (%s): ERR not disabled (%r)" % (port, want, err.strip()))
    rc, out, err = ta.ctl(sw, "vlan", "4", "10")
    check(rc == 1 and "trunk" in err, "vlan 4 10 (the trunk): ERR (%r)" % err.strip())
    check(ta.ctl(sw, "vlan", "1", "4095")[0] == 1 and ta.ctl(sw, "shutdown", "9")[0] == 1, "out-of-range arguments are still refused")
    check(ta.ctl(sw, "shutdown", "3")[0] == 0 and ta.ctl(sw, "up", "3")[0] == 0, "a FREE port goes DISABLED and back")
    h1.close()
    sw.stop()


def test_race_one_winner():
    print("4. several shutdowns of the same UP port at once: exactly one wins; shutdown racing a host that disconnects")
    sw = ta.start(4, "port_wait_timeout = 1\n")
    hs = [connect(sw, i, read=False) for i in (1, 2, 3, 4)]
    results = []

    def shut(p):
        results.append(ta.ctl(sw, "shutdown", str(p)))

    ths = [threading.Thread(target=shut, args=(1,)) for _ in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(60)
    oks = sum(1 for r in results if r[0] == 0)
    errs = [r[2].strip() for r in results if r[0] == 1]
    check(oks == 1 and len(errs) == 7, "8 parallel 'shutdown 1': exactly one OK (%d OK, %d ERR)" % (oks, len(errs)))
    check(all("already" in e for e in errs), "the others say already closing / already disabled: %s" % sorted(set(errs)))
    check(state(sw, 1) == "DISABLED", "port 1 DISABLED")

    # host 2 hangs up while an admin shuts its port down: either order is fine, the end state must be consistent
    for i in range(10):
        h = hs[1]
        t = threading.Thread(target=lambda: results.append(ta.ctl(sw, "shutdown", "2")))
        t.start()
        h.close()
        t.join(60)
        check(wait_for(lambda: state(sw, 2) in ("FREE", "DISABLED")), "round %d: port 2 ended FREE or DISABLED (%s)" % (i, state(sw, 2)))
        if state(sw, 2) == "DISABLED":
            ta.ctl(sw, "up", "2")
        check(wait_for(lambda: state(sw, 2) == "FREE"), "round %d: port 2 FREE" % i)
        hs[1] = connect(sw, 2, read=False)
        check(state(sw, 2) == "UP" or state(sw, 1) in ("FREE", "UP"), "round %d: a new host connected" % i)
        if i == 0:
            break
    for h in hs:
        h.close()
    sw.stop()


def test_vlan():
    print("5. vlan: a connected port moves at once, a free port keeps it for the next host, the trunk is refused, a config change replaces it")
    cf = ta.conf("")
    sw = ts.Switch(4, extra=["-c", cf])
    h1 = connect(sw, 1)
    h2 = connect(sw, 2)
    h3 = connect(sw, 3)
    h1.send(BCAST, 1, b"a")
    h2.send(BCAST, 1, b"b")
    check(wait_for(lambda: len(macs(sw)) == 2), "two MACs learned in VLAN 1")
    for h in (h1, h2, h3):
        h.frames = 0
    rc, out, err = ta.ctl(sw, "vlan", "2", "20")
    check(rc == 0 and "VLAN 20" in out, "vlan 2 20: OK (%r)" % out.strip())
    check(ports(sw)[2][1] == "20", "show ports: port 2 is in VLAN 20")
    check([m[2] for m in macs(sw)] == [1], "port 2's MAC entry of the old VLAN was flushed, port 1's stays")
    h1.send(BCAST, 1, b"vlan1 broadcast")
    check(wait_for(lambda: h3.frames == 1), "a VLAN 1 broadcast reaches port 3 (VLAN 1)")
    time.sleep(0.4)
    check(h2.frames == 0, "... and not port 2 (VLAN 20)")
    h2.send(BCAST, 1, b"vlan20 broadcast")
    time.sleep(0.4)
    check(h1.frames == 0 and h3.frames == 1, "a VLAN 20 broadcast reaches nobody in VLAN 1")
    ta.ctl(sw, "vlan", "2", "1")
    for h in (h1, h2, h3):
        h.frames = 0
    h1.send(BCAST, 1, b"again")
    check(wait_for(lambda: h2.frames == 1 and h3.frames == 1), "vlan 2 1: port 2 is back in VLAN 1")

    # a free port keeps the VLAN for its next host
    h3.close()
    check(wait_for(lambda: state(sw, 3) == "FREE"), "port 3 FREE after its host left")
    rc, out, err = ta.ctl(sw, "vlan", "3", "30")
    check(rc == 0 and ports(sw)[3][1] == "30", "vlan 3 30 on a FREE port: OK and shown (%s)" % ports(sw)[3][1])
    h3 = connect(sw, 3)
    check(ports(sw)[3][1] == "30", "the next host on port 3 is in VLAN 30")
    for h in (h1, h2, h3):
        h.frames = 0
    h1.send(BCAST, 1, b"to vlan 1")
    time.sleep(0.4)
    check(h2.frames == 1 and h3.frames == 0, "it does not see VLAN 1 traffic")
    h3.close()
    check(wait_for(lambda: state(sw, 3) == "FREE" and ports(sw)[3][1] == "30"), "the VLAN stays with the port after that host left too")

    # a configuration reload that changes this port's VLAN replaces the admin setting
    with open(cf, "w") as f:
        f.write("port.3.vlan = 40\n")
    n = sw.count("configuration reloaded")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded", 20, n + 1), "SIGHUP reloaded the configuration")
    check(wait_for(lambda: ports(sw)[3][1] == "40"), "port.3.vlan = 40 replaced the admin VLAN 30 (%s)" % ports(sw)[3][1])
    ta.ctl(sw, "vlan", "3", "50")
    # a reload that does not touch port 3's VLAN keeps the admin setting
    with open(cf, "w") as f:
        f.write("port.3.vlan = 40\nmac_aging_seconds = 99\n")
    n = sw.count("configuration reloaded")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded", 20, n + 1), "second reload")
    check(ports(sw)[3][1] == "50", "an unrelated reload keeps the admin VLAN 50 (%s)" % ports(sw)[3][1])
    for h in (h1, h2):
        h.close()
    sw.stop()


def test_trunk_and_clear():
    print("6. clear mac on a running switch: counters back to 0, the next frames flood and relearn")
    sw = ta.start(3)
    hs = [connect(sw, i) for i in (1, 2, 3)]
    for h in hs:
        h.send(BCAST, 1, b"x")
    check(wait_for(lambda: len(macs(sw)) == 3), "three MACs")
    rc, out, err = ta.ctl(sw, "clear", "mac")
    check(rc == 0 and macs(sw) == [], "clear mac: the table is empty")
    check(all(r[3] == "0" for r in ports(sw).values()), "every port's MAC count is 0 again")
    hs[0].send(BCAST, 1, b"relearn")
    check(wait_for(lambda: [m[2] for m in macs(sw)] == [1]), "the next frame is learned again")
    for h in hs:
        h.close()
    sw.stop()


def test_untouched_ports():
    print("7. hosts on untouched ports keep forwarding every frame while other ports are reconfigured")
    sw = ta.start(4, "queue_capacity = 256\n")
    hs = [connect(sw, i) for i in (1, 2, 3, 4)]
    for h in hs:
        h.send(BCAST, 1, b"learn")
    check(wait_for(lambda: len(macs(sw)) == 4), "four MACs")
    # "four MACs learned" only says that the switch has PROCESSED the four broadcasts: their copies may still be on their way to the hosts. Each host must have received the
    # three broadcasts of the others BEFORE its counter is reset, or a late copy is counted as one of the unicast frames below (a flaw of the test: 1 run in about 4 was one frame over).
    check(wait_for(lambda: all(h.frames == 3 for h in hs), 30), "every host received the three learn broadcasts of the others")
    for h in hs:
        h.frames = 0
    stop = threading.Event()
    sender = [0]

    def traffic():
        while not stop.is_set():
            try:
                hs[2].send(ts.ME[4], 1, b"u" * 200)
            except OSError:
                return
            sender[0] += 1
            time.sleep(0.005)

    th = threading.Thread(target=traffic, daemon=True)
    th.start()
    errors = 0
    for i in range(25):
        for cmd in (("shutdown", "1"), ("up", "1"), ("vlan", "2", "7"), ("vlan", "2", "1"), ("clear", "mac"), ("shutdown", "2"), ("up", "2")):
            rc, out, err = ta.ctl(sw, *cmd)
            if rc not in (0, 1):
                errors += 1
    stop.set()
    th.join(10)
    check(errors == 0, "175 admin commands: every one answered OK or ERR")
    check(wait_for(lambda: hs[3].frames == sender[0], 30), "host 4 received all %d unicast frames of host 3 (%d)" % (sender[0], hs[3].frames))
    rows = ta.run_stats("stats")[1]
    st = ta.parse_stats(rows)
    check(st and st[3][2] == sender[0] + 1 and st[4][4] == 0, "the counters agree: port 3 in = %d, port 4 dropped 0 (%s)" % (sender[0] + 1, st.get(3) if st else st))
    for h in hs:
        h.close()
    sw.stop()


def test_semaphore_invariant():
    print("8. after a storm of operations and connects the free-port semaphore still equals the number of FREE ports")
    sw = ta.start(4, "port_wait_timeout = 1\n")
    stop = threading.Event()
    rnd = random.Random(15)                              # fixed seed
    ops = [0]
    errors = []

    def admin():
        r = random.Random(16)
        while not stop.is_set():
            p = r.randint(1, 4)
            cmd = r.choice(["shutdown %d", "up %d", "up %d", "vlan %d 5", "clear mac"])
            line = (cmd % p if "%d" in cmd else cmd) + "\n"
            try:
                ta.raw(sw, line.encode(), timeout=30)
            except OSError as e:                         # not expected here: the switch runs; report it
                errors.append(repr(e))
            ops[0] += 1

    def churn(seed):
        r = random.Random(seed)
        while not stop.is_set():
            try:
                h = ta.Host(sw, r.randint(1, 8))
                time.sleep(r.random() * 0.05)
                h.send(BCAST, 1, b"hi")
                time.sleep(r.random() * 0.05)
                h.close()
            except OSError:
                pass

    ths = [threading.Thread(target=admin, daemon=True)] + [threading.Thread(target=churn, args=(s,), daemon=True) for s in (1, 2, 3)]
    for t in ths:
        t.start()
    time.sleep(12 * SLOW)
    stop.set()
    for t in ths:
        t.join(30)
    print("  %d admin commands during the storm" % ops[0])
    check(not errors, "no admin command failed at the socket level during the storm (%s)" % errors[:2])
    for p in range(1, 5):
        ta.ctl(sw, "up", str(p))
    time.sleep(1.0)
    for p in range(1, 5):
        ta.ctl(sw, "up", str(p))
    check(wait_for(lambda: all(r[0] == "FREE" for r in ports(sw).values()) and len(ports(sw)) == 4, 30), "all four ports FREE after the storm (%s)" % [r[0] for r in ports(sw).values()])
    socks = []
    for i in range(4):
        socks.append(ts.connect(sw))
    check(wait_for(lambda: sum(1 for r in ports(sw).values() if r[0] == "UP") == 4, 30), "four hosts get the four ports: the semaphore holds exactly 4 tokens")
    extra = ts.connect(sw)
    check(switch_full_frame(extra) == b"switch full", "the fifth host is told 'switch full': no token too many")
    extra.close()
    for s in socks:
        s.close()
    sw.stop()


def test_sigint_during_admin():
    print("9. SIGINT while admin shutdown / up / vlan commands are running: clean exit, never delayed")
    for i, delay in enumerate((0.0, 0.05, 0.15, 0.3, 0.6)):
        sw = ta.start(4)
        hs = [connect(sw, k) for k in (1, 2, 3, 4)]
        stop = threading.Event()

        def admin():
            r = random.Random(i)
            while not stop.is_set():
                p = r.randint(1, 4)
                try:
                    ta.raw(sw, ("%s %d\n" % (r.choice(["shutdown", "up", "up"]), p)).encode(), timeout=10)
                except OSError:
                    pass                                 # the switch is shutting down (SIGINT): a reset or a refused connect is the expected end

        ths = [threading.Thread(target=admin, daemon=True) for _ in range(3)]
        for t in ths:
            t.start()
        time.sleep(delay + 0.2)
        rc, dt = ta.sigint_and_wait(sw, 60)
        stop.set()
        check(rc == 0, "SIGINT after %.2f s of admin commands: exit status 0 (got %s)" % (delay + 0.2, rc))
        check(dt < 4 * SLOW + 1, "the switch left in %.2f s" % dt)
        check(not os.path.exists(ta.sock_path(sw)) and not os.path.exists(ta.SEG), "socket and segment removed")
        for t in ths:
            t.join(15)
        for h in hs:
            h.close()
        sw.stop()


def test_valgrind():
    print("10. valgrind: the whole lifecycle with admin operations: no errors, no leaks, no descriptor left open")
    sw = ta.start(3, "port_wait_timeout = 1\n")
    hs = [connect(sw, i) for i in (1, 2, 3)]
    check(ta.ctl(sw, "shutdown", "1", timeout=120)[0] == 0, "shutdown of an UP port")
    check(ta.ctl(sw, "up", "1", timeout=120)[0] == 0, "up")
    check(ta.ctl(sw, "vlan", "2", "9", timeout=120)[0] == 0, "vlan")
    check(ta.ctl(sw, "vlan", "2", "1", timeout=120)[0] == 0, "vlan back")
    for h in hs:
        h.close()
    check(wait_for(lambda: all(r[0] == "FREE" for r in ports(sw).values()), 60), "all ports FREE")
    check(ta.ctl(sw, "shutdown", "3", timeout=120)[0] == 0, "shutdown of a FREE port")
    h = connect(sw, 9)
    rc, dt = ta.sigint_and_wait(sw, 180)
    check(rc == 0, "SIGINT: exit status 0 under valgrind (got %s)" % rc)
    h.close()
    log = sw.log()
    check("ERROR SUMMARY: 0 errors from 0 contexts" in log, "valgrind: 0 errors")
    check("in use at exit: 0 bytes in 0 blocks" in log, "valgrind: 0 bytes in use at exit")
    check(re.search(r"(definitely|indirectly|possibly) lost: [1-9]", log) is None, "valgrind: nothing lost")
    fds = [f for f in re.findall(r"Open (?:AF_INET socket|AF_UNIX socket|file descriptor) (\d+)", log) if int(f) > 2]
    check(not fds, "valgrind: no descriptor left open at exit %s" % fds)
    sw.stop()


# ----------------------------------------------------------------------------------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    ta.install_signal_cleanup()
    try:
        if mode == "all":
            ta.run_tests((test_shutdown_free_port, test_shutdown_up_port, test_errors, test_race_one_winner, test_vlan, test_trunk_and_clear,
                          test_untouched_ports, test_semaphore_invariant, test_sigint_during_admin))
        elif mode == "tsan":
            ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
            SLOW = 3
            ta.run_tests((test_shutdown_up_port, test_race_one_winner, test_vlan, test_untouched_ports, test_semaphore_invariant, test_sigint_during_admin))
        elif mode == "valgrind":
            ts.SWITCH = os.environ.get("SWITCH") or (
                "valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
                "--error-exitcode=1 --track-fds=yes ./switch")
            SLOW = 20
            ta.run_tests((test_valgrind,))
        else:
            print("usage: test_reconfig.py [all|tsan|valgrind]")
            sys.exit(2)
    finally:
        ta.cleanup()
        subprocess.run(["rm", "-rf", ta.TMP])
    print("%d checks passed, %d failed" % (ta.passed, ta.failed))
    sys.exit(1 if ta.failed else 0)
