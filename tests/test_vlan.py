#!/usr/bin/env python3
"""
Part 13 end-to-end tests: VLANs and the trunk port with the real ./switch.

  python3 tests/test_vlan.py            normal build
  python3 tests/test_vlan.py tsan       SWITCH="setarch -R ./switch_tsan"
  python3 tests/test_vlan.py valgrind   valgrind on ./switch (no errors, no leaks, no descriptor left open)

The forwarding rules themselves are unit tested in tests/test_vlan.c (no sockets). Here whole frames go through TCP:
hosts are raw sockets that record every frame they receive (source, destination, VLAN, payload).
Layout of the roadmap's criterion: ports 1 and 3 in VLAN 10, ports 2 and 4 in VLAN 20, port 5 the trunk.
"""
import os, re, select, shlex, signal, socket, struct, subprocess, sys, tempfile, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts

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
TMP = tempfile.mkdtemp(prefix="vswvlan.")
conf_no = 0
LAYOUT = "port.1.vlan = 10\nport.3.vlan = 10\nport.2.vlan = 20\nport.4.vlan = 20\ntrunk_port = 5\n"


def conf(text):
    global conf_no
    conf_no += 1
    return write_conf(os.path.join(TMP, "v%03d.conf" % conf_no), text)


def write_conf(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


class Host(threading.Thread):
    """A raw socket host: remembers every frame it receives as (src_index, vlan, payload)."""

    def __init__(self, sw, idx):
        super().__init__(daemon=True)
        self.sock = ts.connect(sw)
        self.idx = idx
        self.mac = ME[idx]
        self.frames = []
        self.stop = False
        self.lock = threading.Lock()
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
                src = buf[2:8]
                vlan = struct.unpack("!H", buf[14:16])[0]
                with self.lock:
                    self.frames.append((src, vlan, buf[17:17 + ln]))
                buf = buf[17 + ln:]

    def send(self, dst, vlan, payload, prio=1):
        self.sock.sendall(ts.frame(self.mac, dst, vlan, prio, payload))

    def got(self, payload):
        with self.lock:
            return [(s, v) for s, v, p in self.frames if p == payload]

    def close(self):
        self.stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def mk_hosts(sw, n=5):
    hs = []
    for i in range(1, n + 1):
        hs.append(Host(sw, i))
        check(sw.wait_log("port %d: host connected" % i, 10), "port %d connected" % i)
    return hs


def settle(hosts, seconds=0.5):
    """Frames of one host reach another host's socket in the order the switch handles them. To know that
    everything sent so far has been forwarded, every host sends a sync frame in VLAN-less broadcast and waits for
    it at the others of ITS VLAN; here a short fixed wait is enough because we only use it before NEGATIVE
    checks, which cannot make a correct switch fail."""
    time.sleep(seconds)


def wait_for(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def vlan_of(h, payload):
    r = h.got(payload)
    return r[0][1] if r else None


def reached(hosts, payload):
    return sorted(h.idx for h in hosts if h.got(payload))


def finish(sw, hosts):
    for h in hosts:
        h.close()
    sw.stop()


# ----------------------------------------------------------------------------------------------
def test_roadmap_criterion():
    print("1. ports 1,3 in VLAN 10; 2,4 in VLAN 20; 5 the trunk: broadcast, trunk injection, no cross-VLAN unicast")
    sw = ts.Switch(5, extra=["-c", conf(LAYOUT)])
    hs = mk_hosts(sw)
    h1, h2, h3, h4, h5 = hs
    h1.send(BCAST, 77, b"bc-from-1")                       # the host writes vlan 77: an access port overrides it
    check(wait_for(lambda: reached(hs, b"bc-from-1") == [3, 5]), "a broadcast from port 1 reaches ports 3 and 5 (got %s)" % reached(hs, b"bc-from-1"))
    check(vlan_of(h5, b"bc-from-1") == 10, "the trunk host sees vlan=10 (got %s)" % vlan_of(h5, b"bc-from-1"))
    check(vlan_of(h3, b"bc-from-1") == 10, "the access host sees its own VLAN 10")
    h5.send(BCAST, 20, b"trunk-20")                        # injected on the trunk with vlan 20
    check(wait_for(lambda: reached(hs, b"trunk-20") == [2, 4]), "a frame injected on the trunk with vlan 20 reaches ports 2 and 4 only (got %s)" % reached(hs, b"trunk-20"))
    check(vlan_of(h2, b"trunk-20") == 20, "the access host in VLAN 20 sees vlan 20")
    h2.send(BCAST, 1, b"bc-from-2")
    check(wait_for(lambda: reached(hs, b"bc-from-2") == [4, 5]), "a broadcast from port 2 reaches 4 and the trunk (got %s)" % reached(hs, b"bc-from-2"))
    check(vlan_of(h5, b"bc-from-2") == 20, "... and the trunk sees vlan=20")
    # unicast across VLANs: everybody has spoken now. Port 3 (VLAN 10) -> the MAC of host 2 (VLAN 20)
    h3.send(ME[2], 1, b"cross-3-to-2")
    check(wait_for(lambda: reached(hs, b"cross-3-to-2") == [1, 5]), "unicast to a MAC of another VLAN is not delivered there: it floods inside VLAN 10 (got %s)" % reached(hs, b"cross-3-to-2"))
    h3.send(ME[1], 1, b"uni-3-to-1")                       # in the VLAN, known: only the owner
    check(wait_for(lambda: reached(hs, b"uni-3-to-1") == [1]), "unicast inside the VLAN goes to the owner only (got %s)" % reached(hs, b"uni-3-to-1"))
    # the trunk host's MAC is known in VLAN 10 (it sent in 10 below) -> unicast from port 1 goes to the trunk only
    h5.send(ME[1], 10, b"trunk-10-to-1")
    check(wait_for(lambda: reached(hs, b"trunk-10-to-1") == [1]), "trunk -> a host of VLAN 10: that host only (got %s)" % reached(hs, b"trunk-10-to-1"))
    h1.send(ME[5], 1, b"uni-1-to-trunk")
    check(wait_for(lambda: reached(hs, b"uni-1-to-trunk") == [5]), "host of VLAN 10 -> a MAC learned behind the trunk in VLAN 10: the trunk only (got %s)" % reached(hs, b"uni-1-to-trunk"))
    finish(sw, hs)


def test_default_vlan_and_invalid():
    print("2. without VLAN settings every port is in VLAN 1; the trunk drops a frame with an invalid VLAN")
    sw = ts.Switch(4, extra=["-c", conf("trunk_port = 4\n")])
    hs = mk_hosts(sw, 4)
    h1, h2, h3, h4 = hs
    h1.send(BCAST, 33, b"d1")
    check(wait_for(lambda: reached(hs, b"d1") == [2, 3, 4]), "all ports are in VLAN 1: a broadcast reaches everybody (got %s)" % reached(hs, b"d1"))
    check(all(vlan_of(h, b"d1") == 1 for h in (h2, h3, h4)), "every receiver sees vlan 1, whatever the sender wrote")
    n = len(h4.frames)
    for bad in (0, 4095, 65535):
        h4.send(BCAST, bad, b"bad-%d" % bad)
    h4.send(BCAST, 1, b"ok-after-bad")                      # in order: when this arrives the bad ones were handled
    check(wait_for(lambda: reached(hs, b"ok-after-bad") == [1, 2, 3]), "a valid trunk frame still goes through (got %s)" % reached(hs, b"ok-after-bad"))
    check(all(not h.got(b"bad-%d" % b) for h in hs for b in (0, 4095, 65535)), "trunk frames with VLAN 0, 4095 and 65535 are dropped")
    check(sw.wait_log("port 4: trunk frame with invalid VLAN 0 dropped", 10), "the drop is logged (first one)")
    h4.send(BCAST, 4094, b"vlan4094")                       # valid: but no access port is in VLAN 4094
    h1.send(BCAST, 1, b"sync-after-4094")
    check(wait_for(lambda: reached(hs, b"sync-after-4094") == [2, 3, 4]), "sync")
    check(not any(h.got(b"vlan4094") for h in hs), "VLAN 4094 is valid on the trunk; nobody is in it")
    finish(sw, hs)


def test_reload_vlan():
    print("3. SIGHUP moves a port to another VLAN at once; its old MAC entries are flushed; no change = no flush")
    path = conf("port.1.vlan = 10\nport.2.vlan = 10\nport.3.vlan = 20\n")
    sw = ts.Switch(3, extra=["-c", path])
    hs = mk_hosts(sw, 3)
    h1, h2, h3 = hs
    h1.send(BCAST, 1, b"r1")
    check(wait_for(lambda: reached(hs, b"r1") == [2]), "before: a broadcast from 1 reaches 2 only (got %s)" % reached(hs, b"r1"))
    h2.send(BCAST, 1, b"r2")
    check(wait_for(lambda: reached(hs, b"r2") == [1]), "sync: 2 -> 1")
    n = sw.count("configuration reloaded from")
    write_conf(path, "port.1.vlan = 10\nport.2.vlan = 20\nport.3.vlan = 20\n")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded from", 30, n + 1), "reload done")
    check("reload: port 2 VLAN 10 -> 20, its old MAC entries were flushed" in sw.log(), "the log names port 2: 10 -> 20, flushed")
    check("port 1 VLAN" not in sw.log() and "port 3 VLAN" not in sw.log(), "ports whose VLAN did not change are not touched")
    h1.send(BCAST, 1, b"r3")
    h3.send(BCAST, 1, b"r4")
    check(wait_for(lambda: reached(hs, b"r4") == [2]), "after: 3 and 2 are together in VLAN 20 (got %s)" % reached(hs, b"r4"))
    check(reached(hs, b"r3") == [], "after: a broadcast from 1 (VLAN 10) reaches nobody (got %s)" % reached(hs, b"r3"))
    # an invalid file changes nothing
    n = sw.count("reload failed")
    write_conf(path, "port.1.vlan = 4095\n")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("reload failed", 30, n + 1), "VLAN 4095 is refused by the reload")
    h3.send(BCAST, 1, b"r5")
    check(wait_for(lambda: reached(hs, b"r5") == [2]), "the previous configuration is in force")
    # back
    n = sw.count("configuration reloaded from")
    write_conf(path, "port.1.vlan = 10\nport.2.vlan = 10\nport.3.vlan = 20\n")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded from", 30, n + 1), "reload back")
    h1.send(BCAST, 1, b"r6")
    check(wait_for(lambda: reached(hs, b"r6") == [2]), "port 2 is back in VLAN 10 with port 1 (got %s)" % reached(hs, b"r6"))
    finish(sw, hs)


def test_reconnect_keeps_vlan():
    print("4. a new host on a port gets the port's VLAN from the configuration; the VLAN of a free port is fixed when a host connects")
    sw = ts.Switch(3, extra=["-c", conf("port.1.vlan = 30\nport.2.vlan = 30\n")])
    hs = mk_hosts(sw, 3)
    h1, h2, h3 = hs
    h1.send(BCAST, 1, b"a")
    check(wait_for(lambda: reached(hs, b"a") == [2]), "ports 1 and 2 in VLAN 30, port 3 in VLAN 1 (got %s)" % reached(hs, b"a"))
    h2.close()
    check(sw.wait_log("port 2: teardown complete", 10), "host 2 left")
    h2b = Host(sw, 2)
    check(sw.wait_log("port 2: host connected", 10, 2), "a new host took port 2")
    hs = [h1, h2b, h3]
    h1.send(BCAST, 1, b"b")
    check(wait_for(lambda: reached(hs, b"b") == [2]), "the new host is in VLAN 30 again (got %s)" % reached(hs, b"b"))
    finish(sw, hs)


def test_bad_config():
    print("5. trunk_port and port.N.vlan: invalid values are start-up errors; trunk_port is startup-only")
    bad = [("trunk beyond ports", "trunk_port = 4\n", "trunk_port"),
           ("vlan 0", "port.1.vlan = 0\n", "port.1.vlan"),
           ("vlan 4095", "port.1.vlan = 4095\n", "port.1.vlan"),
           ("vlan not a number", "port.1.vlan = ten\n", "port.1.vlan"),
           ("port beyond", "port.9.vlan = 5\n", "port.9.vlan")]
    for name, text, key in bad:
        p = subprocess.run(shlex.split(ts.SWITCH) + [str(ts.free_port()), "3", "-c", conf(text)], capture_output=True, timeout=60)
        check(p.returncode == 1 and key.encode() in p.stderr, "%s: exit status 1 naming '%s' (rc=%s)" % (name, key, p.returncode))
    path = conf("trunk_port = 3\nport.1.vlan = 4094\nport.2.vlan = 1\n")
    sw = ts.Switch(3, extra=["-c", path])
    check(sw.p.poll() is None, "the extreme valid values start")
    write_conf(path, "trunk_port = 1\n")
    n = sw.count("ignored until restart")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("trunk_port = 1 in the file, running with 3: ignored until restart", 30), "a changed trunk_port is 'ignored until restart'")
    sw.stop()


def test_port_security_trunk():
    print("6. the trunk is exempt from max_macs_per_port; an access port that exceeds it is disabled")
    sw = ts.Switch(3, extra=["-c", conf("trunk_port = 3\nmax_macs_per_port = 2\n")])
    hs = mk_hosts(sw, 3)
    h1, h2, h3 = hs
    for i in range(10):                                  # ten different source MACs behind the trunk
        h3.sock.sendall(ts.frame(ME[10 + i], BCAST, 1, 1, b"t%d" % i))
    h1.send(BCAST, 1, b"sync")
    check(wait_for(lambda: reached(hs, b"t9") == [1, 2]), "all ten frames from the trunk were forwarded (got %s)" % reached(hs, b"t9"))
    check("port 3 shut down: MAC flooding" not in sw.log(), "no port security event for the trunk, which learned 10 MACs with a limit of 2")
    for i in range(3):                                   # an access port: the third MAC is a violation
        h2.sock.sendall(ts.frame(ME[30 + i], BCAST, 1, 1, b"a%d" % i))
    check(sw.wait_log("port 2 shut down: MAC flooding", 10), "the access port exceeding the limit is shut down")
    check(sw.wait_log("port 2: teardown complete, port is DISABLED", 10), "... and ends DISABLED")
    check("port 3: teardown" not in sw.log(), "the trunk port is still up")
    finish(sw, hs)


def test_shutdown_traffic():
    print("7. SIGINT while VLAN traffic floods through: clean exit")
    sw = ts.Switch(5, extra=["-c", conf(LAYOUT)])
    hs = mk_hosts(sw)
    stop = threading.Event()

    def flood(h, vlan):
        blk = ts.frame(h.mac, BCAST, vlan, 1, b"f" * 100) * 20
        while not stop.is_set():
            try:
                h.sock.sendall(blk)
            except OSError:
                return

    ths = [threading.Thread(target=flood, args=(hs[i], v), daemon=True) for i, v in ((0, 1), (1, 1), (4, 10))]
    for t in ths:
        t.start()
    time.sleep(1.5)
    t0 = time.monotonic()
    sw.p.send_signal(signal.SIGINT)
    try:
        rc = sw.p.wait(30)
    except subprocess.TimeoutExpired:
        rc = None
    stop.set()
    check(rc == 0, "exit status 0 (got %s)" % rc)
    check("shutdown: complete" in sw.log(), "shutdown: complete")
    finish(sw, hs)


def test_valgrind():
    print("8. valgrind: VLAN forwarding, reload, invalid trunk frames, SIGINT: no errors, no leaks, no descriptor left open")
    path = conf("port.1.vlan = 10\nport.2.vlan = 20\nport.3.vlan = 10\ntrunk_port = 4\n")
    sw = ts.Switch(4, extra=["-c", path])
    hs = mk_hosts(sw, 4)
    h1, h2, h3, h4 = hs
    h1.send(BCAST, 5, b"v1")
    check(wait_for(lambda: reached(hs, b"v1") == [3, 4], 60), "broadcast from VLAN 10 reaches 3 and the trunk (got %s)" % reached(hs, b"v1"))
    h4.send(BCAST, 0, b"bad")
    h4.send(BCAST, 20, b"v2")
    check(wait_for(lambda: reached(hs, b"v2") == [2], 60), "trunk frame in VLAN 20 reaches 2 only (got %s)" % reached(hs, b"v2"))
    check(not any(h.got(b"bad") for h in hs), "invalid trunk VLAN dropped")
    n = sw.count("configuration reloaded from")
    write_conf(path, "port.1.vlan = 20\nport.2.vlan = 20\nport.3.vlan = 10\ntrunk_port = 4\n")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded from", 60, n + 1), "reload under valgrind")
    h1.send(BCAST, 1, b"v3")
    check(wait_for(lambda: reached(hs, b"v3") == [2, 4], 60), "after the reload port 1 is with port 2 (got %s)" % reached(hs, b"v3"))
    for h in hs:
        h.close()
    sw.p.send_signal(signal.SIGINT)
    rc = sw.p.wait(120)
    check(rc == 0, "exit status 0 under valgrind (got %s)" % rc)
    log = sw.log()
    check("ERROR SUMMARY: 0 errors from 0 contexts" in log, "valgrind: 0 errors")
    check("in use at exit: 0 bytes in 0 blocks" in log, "valgrind: 0 bytes in use at exit")
    check(re.search(r"(definitely|indirectly|possibly) lost: [1-9]", log) is None, "valgrind: nothing lost")
    fds = [f for f in re.findall(r"Open (?:AF_INET socket|file descriptor) (\d+)", log) if int(f) > 2]
    check(not fds, "valgrind: no descriptor left open at exit %s" % fds)
    if "ERROR SUMMARY: 0 errors" not in log:
        print(log[-3000:])
    sw.stop()


# ----------------------------------------------------------------------------------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    try:
        if mode == "all":
            for t in (test_roadmap_criterion, test_default_vlan_and_invalid, test_reload_vlan, test_reconnect_keeps_vlan,
                      test_bad_config, test_port_security_trunk, test_shutdown_traffic):
                t()
        elif mode == "tsan":
            ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
            for t in (test_roadmap_criterion, test_reload_vlan, test_shutdown_traffic):
                t()
        elif mode == "valgrind":
            ts.SWITCH = os.environ.get("SWITCH") or (
                "valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
                "--error-exitcode=1 --track-fds=yes ./switch")
            test_valgrind()
        else:
            print("usage: test_vlan.py [all|tsan|valgrind]")
            sys.exit(2)
    finally:
        subprocess.run(["rm", "-rf", TMP])
    print("%d checks passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
