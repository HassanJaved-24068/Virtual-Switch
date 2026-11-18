#!/usr/bin/env python3
"""
Part 14 end-to-end tests: the counters in POSIX shared memory of the real ./switch.

  python3 tests/test_shm.py            normal build
  python3 tests/test_shm.py tsan       SWITCH="setarch -R ./switch_tsan"
  python3 tests/test_shm.py valgrind   valgrind on ./switch (no errors, no leaks, no descriptor left open, no segment left)

The segment layout, the process-shared robust mutex and every failure path are unit tested in tests/test_stats.c
(real processes, kill -9 while the lock is held, ...). Here the whole switch runs and this script reads the segment
/dev/shm/vswitch_stats_P as another process would: PAGE bytes of control area, then the data area with one
{in, out, dropped, mirrored, state, vlan, mirror_to} record per port (48 bytes each).
Readers here only look at the segment when the traffic has settled (a Python process cannot lock a pthread mutex).
"""
import mmap, os, re, select, shlex, signal, socket, stat, struct, subprocess, sys, tempfile, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts

BCAST = ts.BCAST
SEG = "/dev/shm/vswitch_stats_P"
MAGIC, VERSION = 0x56535453, 1
PAGE = os.sysconf("SC_PAGESIZE")
MAX_PORTS = 64
REC = struct.Struct("<QQQQiii4x")                      # in, out, dropped, mirrored, state, vlan, mirror_to (+ padding)
FREE, UP, CLOSING, DISABLED = 0, 1, 2, 3
passed = failed = 0


def check(cond, what):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", what)


ts.check = check
TMP = tempfile.mkdtemp(prefix="vswshm.")
conf_no = 0


def conf(text):
    global conf_no
    conf_no += 1
    path = os.path.join(TMP, "s%03d.conf" % conf_no)
    with open(path, "w") as f:
        f.write(text)
    return path


# ---- reading the segment ----

def expected_size():
    data = REC.size * (MAX_PORTS + 1)
    return PAGE + (data + PAGE - 1) // PAGE * PAGE


def read_segment():
    """(magic, version, num_ports, [record of port 0..64]) from a fresh mapping, or None if there is no segment."""
    try:
        fd = os.open(SEG, os.O_RDONLY)
    except OSError:
        return None
    try:
        size = os.fstat(fd).st_size
        m = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ)
    finally:
        os.close(fd)
    try:
        magic, version, nports = struct.unpack_from("<IIi", m, 0)
        recs = [REC.unpack_from(m, PAGE + i * REC.size) for i in range(MAX_PORTS + 1)]
    finally:
        m.close()
    return magic, version, nports, recs


def rec(port):
    seg = read_segment()
    return seg[3][port] if seg else None


def counters(port):
    r = rec(port)
    return {"in": r[0], "out": r[1], "dropped": r[2], "mirrored": r[3]} if r else None


def wait_for(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def ino():
    try:
        return os.stat(SEG).st_ino
    except OSError:
        return None


class Host(threading.Thread):
    """A raw socket host that counts the frames it receives."""

    def __init__(self, sw, idx, read=True):
        super().__init__(daemon=True)
        self.sock = ts.connect(sw)
        self.idx, self.mac = idx, ts.ME[idx]
        self.frames = 0
        self.stop = False
        self.reading = read
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

    def send(self, dst, vlan, payload, prio=1):
        self.sock.sendall(ts.frame(self.mac, dst, vlan, prio, payload))

    def close(self):
        self.stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def mk_hosts(sw, n, read=True):
    hs = []
    for i in range(1, n + 1):
        hs.append(Host(sw, i, read))
        check(sw.wait_log("port %d: host connected" % i, 60), "port %d connected" % i)
    return hs


def sigint_and_wait(sw, timeout=120):
    sw.p.send_signal(signal.SIGINT)
    try:
        return sw.p.wait(timeout)
    except subprocess.TimeoutExpired:
        sw.p.kill()
        return None


def start(n, conf_text=None):
    return ts.Switch(n, extra=["-c", conf(conf_text)] if conf_text else [])


# ----------------------------------------------------------------------------------------------
def test_object():
    print("1. during a run: the object exists in /dev/shm with mode 0600, the exact size, a valid header")
    check(not os.path.exists(SEG), "no segment before the switch starts")
    sw = start(4)
    ls = subprocess.run(["ls", "-l", "/dev/shm"], capture_output=True, text=True).stdout
    check("vswitch_stats_P" in ls, "ls -l /dev/shm shows vswitch_stats_P")
    st = os.stat(SEG)
    check(stat.S_IMODE(st.st_mode) == 0o600, "mode is 0600 (got %o)" % stat.S_IMODE(st.st_mode))
    check(st.st_uid == os.getuid(), "owned by the user who started the switch")
    check(st.st_size == expected_size(), "size is PAGE + round_up(sizeof(stats_data_t), PAGE) = %d (got %d)" % (expected_size(), st.st_size))
    check(st.st_size % PAGE == 0, "the size is a whole number of pages")
    seg = read_segment()
    check(seg is not None and seg[0] == MAGIC and seg[1] == VERSION, "magic and version are published")
    check(seg is not None and seg[2] == MAX_PORTS, "num_ports field = %d" % MAX_PORTS)
    check(seg is not None and all(r[:4] == (0, 0, 0, 0) for r in seg[3]), "every counter starts at 0")
    check(seg is not None and all(r[4] == FREE and r[6] == 0 for r in seg[3][1:5]), "ports 1..4 are FREE, mirror_to 0")
    sw.stop()
    check(not os.path.exists(SEG), "the segment is gone after the switch stopped")


def test_counters_match_hosts():
    print("2. four counters per port match what the hosts sent and received (no drops, no mirroring)")
    sw = start(4)
    hs = mk_hosts(sw, 4)
    n = 15                                              # 3 senders x 15 = 45 frames per output queue: below its capacity (64)
    for h in hs:
        for i in range(n):
            h.send(BCAST, 1, b"c%d-%d" % (h.idx, i))
    check(wait_for(lambda: all(h.frames == 3 * n for h in hs), 60), "every host received %d frames (got %s)" % (3 * n, [h.frames for h in hs]))
    check(wait_for(lambda: all(counters(p)["out"] == 3 * n for p in range(1, 5)), 30), "out counters reach %d" % (3 * n))
    for p in range(1, 5):
        c = counters(p)
        check(c == {"in": n, "out": 3 * n, "dropped": 0, "mirrored": 0}, "port %d: %s (expected in=%d out=%d dropped=0 mirrored=0)" % (p, c, n, 3 * n))
    check(counters(5) == {"in": 0, "out": 0, "dropped": 0, "mirrored": 0}, "a port without host stays at 0")
    check(counters(0) == {"in": 0, "out": 0, "dropped": 0, "mirrored": 0}, "slot 0 stays at 0")
    check(sum(h.frames for h in hs) == sum(counters(p)["out"] for p in range(1, 5)), "sum of host receive counts = sum of out counters")
    for h in hs:
        h.close()
    sw.stop()


def test_dropped():
    print("3. frames dropped because a queue is full are counted in `dropped` (a host that never reads)")
    sw = start(3)
    h1 = Host(sw, 1)
    check(sw.wait_log("port 1: host connected", 30), "port 1 connected")
    h2 = Host(sw, 2, read=False)                        # never reads
    check(sw.wait_log("port 2: host connected", 30), "port 2 connected")
    total = 12000
    blk = ts.frame(h1.mac, BCAST, 1, 1, b"d" * 1400)
    sent = 0
    deadline = time.time() + 120
    while sent < total and time.time() < deadline and counters(2)["dropped"] == 0:
        h1.sock.sendall(blk * 200)
        sent += 200
    # keep sending a little more so that the counter is certainly beyond zero, then let it settle
    h1.sock.sendall(blk * 200)
    sent += 200
    check(wait_for(lambda: counters(1)["in"] == sent, 120), "port 1 `in` = %d frames sent (got %s)" % (sent, counters(1)["in"]))
    c2 = counters(2)
    check(c2["dropped"] >= 1, "port 2 dropped counter > 0 (got %d)" % c2["dropped"])
    check(c2["out"] + c2["dropped"] <= sent, "port 2: out + dropped <= frames flooded to it (%d + %d <= %d)" % (c2["out"], c2["dropped"], sent))
    check(counters(1)["dropped"] == 0, "port 1 (a reading host) dropped nothing")
    h1.close()
    h2.close()
    sw.stop()


def test_port_fields():
    print("4. state, VLAN and mirror_to follow the port: connect, SIGHUP VLAN change, disconnect")
    path = conf("port.1.vlan = 10\nport.2.vlan = 20\nport.3.vlan = 10\ntrunk_port = 4\n")
    sw = ts.Switch(4, extra=["-c", path])
    seg = read_segment()
    check(all(seg[3][p][4] == FREE for p in (1, 2, 3, 4)), "all FREE at the start")
    check(all(seg[3][p][5] == 1 for p in (1, 2, 3, 4)), "a FREE port shows the default VLAN 1 (the VLAN is applied when a host connects)")
    hs = mk_hosts(sw, 4)
    check(wait_for(lambda: [rec(p)[4] for p in (1, 2, 3, 4)] == [UP] * 4), "all four ports UP in the segment (got %s)" % [rec(p)[4] for p in (1, 2, 3, 4)])
    check([rec(p)[5] for p in (1, 2, 3)] == [10, 20, 10], "VLANs 10, 20, 10 (got %s)" % [rec(p)[5] for p in (1, 2, 3)])
    check(all(rec(p)[6] == 0 for p in (1, 2, 3, 4)), "mirror_to is 0 everywhere (mirroring is Part 17)")
    # SIGHUP moves port 3 to VLAN 20
    n = sw.count("configuration reloaded from")
    with open(path, "w") as f:
        f.write("port.1.vlan = 10\nport.2.vlan = 20\nport.3.vlan = 20\ntrunk_port = 4\n")
    sw.p.send_signal(signal.SIGHUP)
    check(sw.wait_log("configuration reloaded from", 30, n + 1), "reload logged")
    check(wait_for(lambda: rec(3)[5] == 20), "port 3 is in VLAN 20 in the segment after SIGHUP (got %s)" % rec(3)[5])
    check(rec(1)[5] == 10 and rec(2)[5] == 20, "the other ports are unchanged")
    # a host leaves: the port goes back to FREE
    hs[1].close()
    check(wait_for(lambda: rec(2)[4] == FREE, 30), "port 2 is FREE again after its host left (got %s)" % rec(2)[4])
    check(rec(1)[4] == UP and rec(3)[4] == UP, "the other ports stay UP")
    # a new host takes the lowest free port and the state follows
    h = Host(sw, 2)
    check(wait_for(lambda: rec(2)[4] == UP and rec(2)[5] == 20, 30), "a new host: port 2 UP in VLAN 20 again (got %s)" % (rec(2),))
    h.close()
    for x in hs:
        x.close()
    sw.stop()


def test_shutdown_unlinks():
    print("5. SIGINT and SIGTERM remove the segment; a reader that mapped it before keeps a readable, invalidated mapping")
    for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
        sw = start(3)
        hs = mk_hosts(sw, 2)
        hs[0].send(BCAST, 1, b"x")
        check(wait_for(lambda: counters(1)["in"] == 1), "%s: a frame was counted" % name)
        fd = os.open(SEG, os.O_RDONLY)
        m = mmap.mmap(fd, expected_size(), mmap.MAP_SHARED, mmap.PROT_READ)
        os.close(fd)
        check(struct.unpack_from("<I", m, 0)[0] == MAGIC, "%s: the old mapping sees the magic while the switch runs" % name)
        for h in hs:
            h.close()
        sw.p.send_signal(sig)
        try:
            rc = sw.p.wait(60)
        except subprocess.TimeoutExpired:
            rc = None
            sw.p.kill()
        check(rc == 0, "%s: exit status 0 (got %s)" % (name, rc))
        check(not os.path.exists(SEG), "%s: the segment is gone from /dev/shm" % name)
        check(struct.unpack_from("<I", m, 0)[0] == 0, "%s: the old mapping still works and says 'shut down' (magic 0)" % name)
        check(REC.unpack_from(m, PAGE + 1 * REC.size)[0] == 1, "%s: the counters in the old mapping are still readable" % name)
        m.close()
        sw.stop()


def test_stale():
    print("6. start-up replaces a stale segment: junk left behind, and the segment of a switch killed with SIGKILL")
    # (a) junk with the wrong mode and size
    fd = os.open(SEG, os.O_CREAT | os.O_RDWR, 0o644)
    os.ftruncate(fd, 100)
    os.write(fd, b"garbage")
    os.close(fd)
    os.chmod(SEG, 0o644)
    old = ino()
    sw = start(3)
    check(ino() != old, "the junk object was replaced (new inode)")
    seg = read_segment()
    check(seg is not None and seg[0] == MAGIC and seg[1] == VERSION, "the new segment is valid")
    check(stat.S_IMODE(os.stat(SEG).st_mode) == 0o600 and os.stat(SEG).st_size == expected_size(), "mode 0600 and the right size")
    # (b) a crash: SIGKILL leaves the segment (and its counters) behind
    hs = mk_hosts(sw, 2)
    hs[0].send(BCAST, 1, b"y")
    check(wait_for(lambda: counters(1)["in"] == 1), "a frame was counted")
    sw.p.kill()
    sw.p.wait()
    for h in hs:
        h.close()
    check(os.path.exists(SEG), "SIGKILL leaves the segment behind (as a crash does)")
    check(read_segment()[3][1][0] == 1, "... with the old counters")
    old = ino()
    sw.stop()                                           # (already dead: only cleans the log files)
    sw2 = start(3)
    check(ino() != old, "the next start replaces the stale segment")
    check(counters(1) == {"in": 0, "out": 0, "dropped": 0, "mirrored": 0}, "and starts with zero counters")
    h = Host(sw2, 1)
    h.send(BCAST, 1, b"z")
    check(wait_for(lambda: counters(1)["in"] == 1), "the new switch counts normally")
    h.close()
    sw2.stop()
    check(not os.path.exists(SEG), "and removes it at its own shutdown")


def test_failed_start():
    print("7. a switch that cannot start leaves no segment, and never touches the segment of a running one")
    # (a) nothing running: a bad configuration file and a TCP port in use
    bad = conf("this is not a valid line\n")
    sw0 = subprocess.run(shlex.split(ts.SWITCH) + [str(ts.free_port()), "3", "-c", bad], capture_output=True, timeout=30)
    check(sw0.returncode != 0 and not os.path.exists(SEG), "bad configuration file: exit status != 0, no segment")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    r = subprocess.run(shlex.split(ts.SWITCH) + [str(port), "3"], capture_output=True, timeout=30)
    check(r.returncode != 0 and not os.path.exists(SEG), "TCP port in use: exit status != 0, no segment (got %s)" % r.returncode)
    s.close()
    r = subprocess.run(shlex.split(ts.SWITCH) + ["9000", "99"], capture_output=True, timeout=30)
    check(r.returncode != 0 and not os.path.exists(SEG), "bad number of ports: no segment")
    # (b) a running switch: a second one that fails must leave the first one's segment alone
    sw = start(3)
    before = ino()
    h = Host(sw, 1)
    h.send(BCAST, 1, b"k")
    check(wait_for(lambda: counters(1)["in"] == 1), "counted")
    r = subprocess.run(shlex.split(ts.SWITCH) + [str(sw.port), "3"], capture_output=True, timeout=30)       # same TCP port: bind fails
    check(r.returncode != 0, "the second switch fails (port in use)")
    check(ino() == before and counters(1)["in"] == 1, "the running switch's segment is untouched")
    h.close()
    sw.stop()
    check(not os.path.exists(SEG), "and it is removed when that switch stops")


def test_shutdown_under_traffic():
    print("8. SIGINT while hosts flood: clean exit, segment removed")
    sw = start(4)
    hs = mk_hosts(sw, 3)
    stop = threading.Event()

    def flood(h):
        blk = ts.frame(h.mac, BCAST, 1, 1, b"f" * 100) * 20
        while not stop.is_set():
            try:
                h.sock.sendall(blk)
            except OSError:
                return

    ths = [threading.Thread(target=flood, args=(h,), daemon=True) for h in hs]
    for t in ths:
        t.start()
    check(wait_for(lambda: (counters(1) or {"in": 0})["in"] > 200, 60), "counters grow while flooding")
    rc = sigint_and_wait(sw, 60)
    stop.set()
    check(rc == 0, "exit status 0 (got %s)" % rc)
    check(not os.path.exists(SEG), "segment removed")
    for h in hs:
        h.close()
    sw.stop()


def test_valgrind():
    print("9. valgrind: counters, fields, shutdown: no errors, no leaks, no descriptor left open, no segment left")
    sw = start(4)
    hs = mk_hosts(sw, 3)
    for h in hs:
        for i in range(5):
            h.send(BCAST, 1, b"v%d-%d" % (h.idx, i))
    check(wait_for(lambda: all(counters(p)["out"] == 10 for p in (1, 2, 3)), 120), "out counters = 10 each (got %s)" % [counters(p)["out"] for p in (1, 2, 3)])
    check(all(counters(p)["in"] == 5 for p in (1, 2, 3)), "in counters = 5 each")
    check(wait_for(lambda: [rec(p)[4] for p in (1, 2, 3, 4)] == [UP, UP, UP, FREE]), "states UP UP UP FREE")
    for h in hs:
        h.close()
    rc = sigint_and_wait(sw, 180)
    check(rc == 0, "exit status 0 under valgrind (got %s)" % rc)
    check(not os.path.exists(SEG), "segment removed")
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
            for t in (test_object, test_counters_match_hosts, test_dropped, test_port_fields, test_shutdown_unlinks,
                      test_stale, test_failed_start, test_shutdown_under_traffic):
                t()
        elif mode == "tsan":
            ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
            for t in (test_counters_match_hosts, test_port_fields, test_shutdown_under_traffic):
                t()
        elif mode == "valgrind":
            ts.SWITCH = os.environ.get("SWITCH") or (
                "valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
                "--error-exitcode=1 --track-fds=yes ./switch")
            test_valgrind()
        else:
            print("usage: test_shm.py [all|tsan|valgrind]")
            sys.exit(2)
    finally:
        subprocess.run(["rm", "-rf", TMP])
    print("%d checks passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
