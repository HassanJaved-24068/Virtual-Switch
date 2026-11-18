#!/usr/bin/env python3
"""
End-to-end verification (Parts 1-15): CROSS-FEATURE scenarios and resource measurements, with the real switch, hosts and swctl.
This file is separate from the per-part suites (it is not called by run_unit.sh); it was written for docs/End_to_End_Verification_Report.md.

  python3 tests/test_e2e.py [scenario ...]        normal build;   SWITCH="setarch -R ./switch_tsan" python3 tests/test_e2e.py   TSan build

Scenarios (C1..C8 = section C of the verification task) and R (resource / CPU measurements). Every scenario prints its measured numbers.
"""
import os, re, select, signal, socket, struct, subprocess, sys, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_admin as ta

check = ta.check
BCAST = ts.BCAST
CLK = os.sysconf("SC_CLK_TCK")
SWITCH_SMALL = "./switch_small"


def cpu_s(pid):
    with open("/proc/%d/stat" % pid) as f:
        r = f.read().rsplit(")", 1)[1].split()
    return (int(r[11]) + int(r[12])) / CLK


def rss_kb(pid):
    with open("/proc/%d/status" % pid) as f:
        for l in f:
            if l.startswith("VmRSS"):
                return int(l.split()[1])


def nthreads(pid):
    return len(os.listdir("/proc/%d/task" % pid))


def stats(sw):
    rc, out, err = ta.run_stats("stats")
    return ta.parse_stats(out) if rc == 0 else None      # {port: (state, vlan, in, out, dropped, mirrored)}


class Flooder(threading.Thread):
    def __init__(self, host, dst=BCAST, vlan=1, prio=1, payload=100, gap=0.001, count=None, tag=None):
        super().__init__(daemon=True)
        self.h, self.dst, self.vlan, self.prio, self.payload, self.gap, self.count = host, dst, vlan, prio, payload, gap, count
        self.sent = 0
        self.tag = tag                                     # None: plain payload; else the payload starts with !II (tag, sequence number 0, 1, 2 ...)
        self.ev = threading.Event()

    def run(self):
        while not self.ev.is_set() and (self.count is None or self.sent < self.count):
            try:
                if self.tag is None:
                    body = b"e" * self.payload
                else:
                    body = struct.pack("!II", self.tag, self.sent) + b"e" * (self.payload - 8)
                self.h.send(self.dst, self.vlan, body, self.prio)
            except OSError:
                return
            self.sent += 1
            if self.gap:
                time.sleep(self.gap)

    def stop(self):
        self.ev.set()
        self.join(10)


def finish(sw, hosts, flooders=()):
    for f in flooders:
        f.stop()
    for h in hosts:
        h.close()
    sw.stop()


# --------------------------------------------------------------------------------------------------------------------
def c1_vlan_qos():
    print("C1. VLANs + QoS: two access VLANs, a trunk, all three priorities, small queues, weights 5,3,1")
    sw = ta.start(5, "port.1.vlan = 10\nport.2.vlan = 10\nport.3.vlan = 20\nport.4.vlan = 20\ntrunk_port = 5\n"
                     "queue_capacity = 64\nwrr_weights = 5,3,1\nprio_aging_ms = 200\n")
    hs = ta.mk_hosts(sw, 5)
    N = 60
    fl = [Flooder(hs[0], BCAST, 10, p, 50, 0.002, N) for p in (0, 1, 2)] + [Flooder(hs[2], BCAST, 20, p, 50, 0.002, N) for p in (0, 1, 2)]
    for f in fl:
        f.start()
    for f in fl:
        f.join(60)
    time.sleep(1.5)
    # host 1 (VLAN 10) sends 3N: host 2 (VLAN 10) and the trunk (5) get them; hosts 3,4 (VLAN 20) must not
    # host 3 (VLAN 20) sends 3N: host 4 and the trunk get them; hosts 1,2 must not
    check(hs[1].frames == 3 * N, "VLAN 10 neighbour got all %d frames of VLAN 10 (got %d)" % (3 * N, hs[1].frames))
    check(hs[3].frames == 3 * N, "VLAN 20 neighbour got all %d frames of VLAN 20 (got %d)" % (3 * N, hs[3].frames))
    check(hs[4].frames == 6 * N, "the trunk got both VLANs (%d of %d)" % (hs[4].frames, 6 * N))
    check(hs[0].frames == 0 and hs[2].frames == 0, "senders never receive their own frames, and no frame crosses VLANs (%d, %d)" % (hs[0].frames, hs[2].frames))
    s = stats(sw)
    check(s and s[1][2] == 3 * N and s[3][2] == 3 * N and s[2][3] == 3 * N and s[4][3] == 3 * N and s[5][3] == 6 * N and all(r[4] == 0 for r in s.values()),
          "shared-memory counters match: in 180/180, out 180/180/360, dropped 0: %s" % s)
    finish(sw, hs, [])


def c2_rate_isolation():
    print("C2. rate limiting on port 2 while ports 3 and 4 keep forwarding; CPU while throttled")
    sw = ta.start(4, "port.2.rate = 50\nqueue_capacity = 256\n")
    hs = ta.mk_hosts(sw, 4)
    c0, t0 = cpu_s(sw.p.pid), time.time()
    f = Flooder(hs[0], BCAST, 1, 1, 100, 0.002)
    f.start()
    T = 6
    time.sleep(T)
    f.stop()
    time.sleep(1)
    el = time.time() - t0
    cpu = cpu_s(sw.p.pid) - c0
    r2, r3, r4 = hs[1].frames, hs[2].frames, hs[3].frames
    print("  sent %d in %.1f s; received: port2 (50/s) %d, port3 %d, port4 %d; switch CPU %.2f s over %.1f s (%.0f %%)" % (f.sent, el, r2, r3, r4, cpu, el, 100 * cpu / el))
    check(r3 == f.sent and r4 == f.sent, "unlimited ports received everything (%d, %d of %d)" % (r3, r4, f.sent))
    check(r2 < f.sent * 0.8 and 50 <= r2 <= 50 * (T + 3) + 50, "the limited port got about rate*T frames (%d)" % r2)
    check(f.sent > 4 * T * 50 * 0.5, "the sender was not slowed down by the throttled port (%d frames)" % f.sent)
    s = stats(sw)
    check(s and s[2][4] > 0 and s[3][4] == 0 and s[4][4] == 0, "dropped is counted on the throttled port only: %s" % ({p: r[4] for p, r in s.items()} if s else s))
    # idle CPU of the throttled sender: stop traffic, the port's queue is empty; then measure 3 s idle
    time.sleep(1)
    i0 = cpu_s(sw.p.pid)
    time.sleep(3)
    idle = cpu_s(sw.p.pid) - i0
    print("  idle CPU of the whole switch (4 hosts connected, rate limited port): %.2f s in 3 s" % idle)
    check(idle < 0.3, "no busy-spinning when idle (%.2f s CPU in 3 s)" % idle)
    finish(sw, hs, [])


def c3_sighup_traffic():
    print("C3. SIGHUP reloads during traffic: valid changes applied, an invalid file changes nothing, counters stay exact")
    cf = ta.conf("queue_capacity = 256\nwrr_weights = 5,3,1\nprio_aging_ms = 200\n")
    sw = ts.Switch(4, extra=["-c", cf])
    hs = ta.mk_hosts(sw, 4)
    fl = [Flooder(hs[i], BCAST, 1, i % 3, 100, 0.002) for i in range(3)]
    for f in fl:
        f.start()
    n = sw.count("configuration reloaded")
    for i, txt in enumerate(["queue_capacity = 256\nwrr_weights = 1,1,1\nprio_aging_ms = 200\n",
                             "queue_capacity = 256\nwrr_weights = 7,2,1\nprio_aging_ms = 300\nrate_limit = 5000\n",
                             "this is not a valid file\n",
                             "queue_capacity = 256\nwrr_weights = 5,3,1\nprio_aging_ms = 200\n"]):
        with open(cf, "w") as f:
            f.write(txt)
        sw.p.send_signal(signal.SIGHUP)
        time.sleep(0.7)
    log = sw.log()
    check("wrr_weights 5,3,1 -> 1,1,1" in log and "wrr_weights 1,1,1 -> 7,2,1" in log, "valid reloads were applied")
    check(re.search(r"reload failed", log) is not None, "the invalid file was refused ('reload failed')")
    check("wrr_weights 7,2,1 -> 5,3,1" in log, "after the invalid file the previous configuration was intact and the next valid reload applied")
    check(sw.p.poll() is None, "switch alive")
    for f in fl:
        f.stop()
    time.sleep(1.5)
    s = stats(sw)
    sent = [f.sent for f in fl]
    check(s and [s[p][2] for p in (1, 2, 3)] == sent, "in counters = frames sent by the hosts %s (got %s)" % (sent, [s[p][2] for p in (1, 2, 3)] if s else s))
    total = sum(sent)
    exp_out = [total - sent[0], total - sent[1], total - sent[2], total]
    got_out = [s[p][3] for p in (1, 2, 3, 4)] if s else None
    check(s and all(r[4] == 0 for r in s.values()) and got_out == exp_out, "out counters = what the other hosts sent %s (got %s), no drops" % (exp_out, got_out))
    check([h.frames for h in hs] == exp_out, "hosts really received those frames %s" % [h.frames for h in hs])
    finish(sw, hs, [])


def c4_aging_traffic():
    print("C4. MAC aging while hosts send (switch_small: aging 3 s)")
    saved = ts.SWITCH
    ts.SWITCH = SWITCH_SMALL if "tsan" not in saved else "setarch -R ./switch_small_tsan"
    try:
        sw = ts.Switch(4)
        hs = ta.mk_hosts(sw, 3)
        f1 = Flooder(hs[0], BCAST, 1, 1, 60, 0.05)
        f1.start()
        hs[1].send(BCAST, 1, b"once")                    # host 2 speaks once and is silent
        time.sleep(0.5)
        check(len(ta.parse_macs(ta.ctl(sw, "show", "mac")[1])) == 2, "two MACs learned")
        time.sleep(7)
        macs = [m[1] for m in ta.parse_macs(ta.ctl(sw, "show", "mac")[1])]
        check(ts.ME[1] in macs, "the MAC of the host that keeps sending is NOT aged out")
        check(ts.ME[2] not in macs, "the MAC of the silent host IS aged out (%s)" % macs)
        rows = ta.parse_ports(ta.ctl(sw, "show", "ports")[1])
        check(rows[1][3] == "1" and rows[2][3] == "0", "show ports MAC counts follow aging (1, 0): %s" % [rows[i][3] for i in (1, 2)])
        # a flooded unicast to the aged-out MAC goes to every port again
        for h in hs:
            h.frames = 0
        hs[0].send(ts.ME[2], 1, b"to-aged")
        time.sleep(0.8)
        check(hs[1].frames >= 1 and hs[2].frames >= 1, "after aging a frame for the old MAC is flooded (hosts 2 and 3 got %d, %d)" % (hs[1].frames, hs[2].frames))
        finish(sw, hs, [f1])
    finally:
        ts.SWITCH = saved


C5_READER = os.environ.get("C5_READER", "fast")          # fast = recv_into + offsets (default); legacy = the receive loop of ta.Host (+ recording)
C5_RCVBUF = int(os.environ.get("C5_RCVBUF", "0"))         # experiment: SO_RCVBUF (bytes) of the four READING hosts' sockets (0 = the kernel default)
C5_GAP = os.environ.get("C5_GAP")                         # experiment: overrides the pause between two frames of a flooder (seconds)
C5_HOG = int(os.environ.get("C5_HOG", "0"))               # experiment only: number of CPU-hog processes started during the flood (never in a normal run)


class DiagReader(ta.Host):
    """The reader of C5. `legacy` has the same receive algorithm as ta.Host (select, recv(65536), buf = buf[17 + len:]); on top of it only
    O(1) recording per recv call and 8 bytes per frame (its tag), so that the measurement does not change the speed it measures.
    Records: tags of every frame, number of recv calls, bytes, the longest time between two recv returns while measuring, and how many were > 50 ms."""

    def __init__(self, sw, idx, mode="legacy"):
        super().__init__(sw, idx, read=False)
        self.mode = mode
        self.tags = []
        self.recvs = self.nbytes = self.gaps50 = 0
        self.max_gap = 0.0
        self.measure = False
        self.error = None
        self.last_recv = None
        self.tid = None
        self.start()

    def _note(self, n):
        now = time.perf_counter()
        self.recvs += 1
        self.nbytes += n
        if self.measure and self.last_recv is not None:
            g = now - self.last_recv
            if g > self.max_gap:
                self.max_gap = g
            if g > 0.05:
                self.gaps50 += 1
        self.last_recv = now if self.measure else None

    def run(self):
        self.tid = threading.get_native_id()
        try:
            self.run_fast() if self.mode == "fast" else self.run_legacy()
        except Exception as e:                           # recorded, not swallowed: a reader that died is evidence
            self.error = repr(e)

    def run_legacy(self):
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
            self._note(len(d))
            buf += d
            while len(buf) >= 17:
                ln = struct.unpack("!H", buf[:2])[0]
                if len(buf) < 17 + ln:
                    break
                self.frames += 1
                self.tags.append(buf[17:25])
                buf = buf[17 + ln:]

    def run_fast(self):
        buf = bytearray(1 << 20)
        have = 0                                          # bytes in buf[0:have]
        while not self.stop:
            try:
                if not select.select([self.sock], [], [], 0.2)[0]:
                    continue
                n = self.sock.recv_into(memoryview(buf)[have:])
            except (OSError, ValueError):
                return
            if n == 0:
                return
            self._note(n)
            have += n
            pos = 0
            while have - pos >= 17:
                ln = (buf[pos] << 8) | buf[pos + 1]
                if have - pos < 17 + ln:
                    break
                self.frames += 1
                self.tags.append(bytes(buf[pos + 17:pos + 25]))
                pos += 17 + ln
            if pos:
                buf[0:have - pos] = buf[pos:have]          # keep the partial frame (if any) at the start
                have -= pos


def read_tcp_queues():
    """{(local_port, remote_port): (tx_queue, rx_queue)} of every TCP socket of the machine, from /proc/net/tcp (cheap, no subprocess)."""
    out = {}
    try:
        with open("/proc/net/tcp") as f:
            next(f)
            for line in f:
                t = line.split()
                lp, rp = int(t[1].split(":")[1], 16), int(t[2].split(":")[1], 16)
                tx, rx = t[4].split(":")
                out[(lp, rp)] = (int(tx, 16), int(rx, 16))
    except (OSError, ValueError, IndexError):
        pass
    return out


class Sampler(threading.Thread):
    """10 Hz: the kernel's queues of the reader sockets (rx_queue of the host's socket = bytes the kernel holds that Python has not read; tx_queue of the
    switch's socket = bytes the switch's sender has written that the host has not yet read), the frames counted so far, and every 5th tick the
    per-port counters from the shared memory."""

    def __init__(self, sw, readers):
        super().__init__(daemon=True)
        self.sw, self.readers = sw, readers
        self.ev = threading.Event()
        self.max_rx = {r.idx: 0 for r in readers}
        self.max_tx = {r.idx: 0 for r in readers}
        self.timeline = []                                # (t, frames per reader, dropped per port)
        self.t0 = time.time()

    def run(self):
        tick = 0
        while not self.ev.wait(0.1):
            q = read_tcp_queues()
            for r in self.readers:
                hp = r.sock.getsockname()[1]
                rx = q.get((hp, self.sw.port), (0, 0))[1]
                tx = q.get((self.sw.port, hp), (0, 0))[0]
                self.max_rx[r.idx] = max(self.max_rx[r.idx], rx)
                self.max_tx[r.idx] = max(self.max_tx[r.idx], tx)
            tick += 1
            if tick % 5 == 0:
                st = stats(self.sw)
                self.timeline.append((time.time() - self.t0, [r.frames for r in self.readers], {p: v[4] for p, v in st.items()} if st else None))

    def stop(self):
        self.ev.set()
        self.join(5)


def cpu_times():
    """System-wide jiffies from /proc/stat: dict user, nice, system, idle, iowait, irq, softirq, steal."""
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    return dict(zip(("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"), v))


def cpu_report(c0, c1):
    d = {k: c1[k] - c0[k] for k in c0}
    tot = sum(d.values()) or 1
    return "user %.0f%% system %.0f%% softirq %.0f%% idle %.0f%% steal %.0f%% (of %d CPUs)" % (
        100 * (d["user"] + d["nice"]) / tot, 100 * d["system"] / tot, 100 * (d["irq"] + d["softirq"]) / tot, 100 * d["idle"] / tot, 100 * d["steal"] / tot, os.cpu_count())


def sched_of(pid, tid):
    """(cpu_run_ms, runqueue_wait_ms) of one thread from /proc/<pid>/task/<tid>/schedstat: how long it ran and how long it was RUNNABLE but not on a CPU."""
    try:
        with open("/proc/%d/task/%d/schedstat" % (pid, tid)) as f:
            run, wait, _ = f.read().split()
        return int(run) / 1e6, int(wait) / 1e6
    except (OSError, ValueError):
        return None


def switch_threads(pid):
    """{thread name: tid} of the switch (rx-portN, tx-portN, ...)."""
    out = {}
    for tid in os.listdir("/proc/%d/task" % pid):
        try:
            with open("/proc/%d/task/%s/comm" % (pid, tid)) as f:
                out[f.read().strip()] = int(tid)
        except OSError:
            pass
    return out


def ranges(nums):
    """[3, 4, 5, 9] -> '3-5, 9' (compact list of missing sequence numbers)."""
    out, nums = [], sorted(nums)
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else "%d-%d" % (nums[i], nums[j]))
        i = j + 1
    return ", ".join(out) if out else "none"


# Why C5 runs at about 840 frames/s and C5b at about 2100 frames/s (docs/End_to_End_Verification_Report.md, section "C5 investigation"): the switch
# keeps a bounded queue of queue_capacity frames per port, so a READING port loses frames when its sender thread (or the host) is delayed for longer
# than queue_capacity / arrival rate. With queue_capacity = 32 and 2100 frames/s that is 15 ms, which the scheduler of a 2-CPU VM exceeds now and then
# (4 of 12 and 6 of 15 runs lost 1-60 frames on a reading port; every loss was counted as `dropped` by the switch and accounted exactly). At 840 frames/s
# the tolerance is 38 ms: 0 drops in 10 of 10 runs, while the port nobody reads still saturates (>= 4332 drops). C5b keeps the high rate and asserts what is
# true at ANY rate (exact accounting), without the "no drops on reading ports" assumption.
def c5_saturation(gap=0.002, strict=True, name="C5"):
    gap = float(C5_GAP) if C5_GAP else gap
    print("C5%s. one host never reads (queue saturation) while three other ports keep forwarding  [%s, flooder gap %.4f s, reader=%s%s]" % (
        "" if strict else "b", "strict: reading ports must lose nothing" if strict else "stress: accounting exact, losses on reading ports reported", gap, C5_READER, ", HOG=%d" % C5_HOG if C5_HOG else ""))
    QCAP = 32
    sw = ta.start(5, "queue_capacity = %d\n" % QCAP)
    hs = []
    for i in range(1, 6):
        hs.append(DiagReader(sw, i, C5_READER))
        check(sw.wait_log("port %d: host connected" % i, 60), "port %d connected" % i)
    hs[4].stop = True                                     # host 5 stops reading: its socket buffers and then its queue fill
    if C5_RCVBUF:
        for h in hs[:4]:
            h.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, C5_RCVBUF)
    print("  receive buffers (SO_RCVBUF): readers %s, host 5 %d bytes" % ([h.sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) for h in hs[:4]], hs[4].sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)))
    time.sleep(0.5)
    senders, readers = hs[:2], hs[2:4]
    before = stats(sw)
    sampler = Sampler(sw, hs[:4])
    hogs = [subprocess.Popen(["sh", "-c", "while :; do :; done"]) for _ in range(C5_HOG)]
    for h in hogs:
        ta.track(h)
    fl = [Flooder(hs[i], BCAST, 1, 1, 1200, gap, tag=i + 1) for i in range(2)]
    for h in hs[:4]:
        h.measure = True
    pid = sw.p.pid
    sth = switch_threads(pid)
    mypid = os.getpid()
    sched0 = {("switch", n): sched_of(pid, sth[n]) for n in ("tx-port3", "tx-port4", "rx-port1", "rx-port2") if n in sth}
    sched0.update({("reader", h.idx): sched_of(mypid, h.tid) for h in hs[:4] if h.tid})
    cpu0 = cpu_times()
    t_start = time.time()
    sampler.start()
    for f in fl:
        f.start()
    time.sleep(8)
    for f in fl:
        f.stop()
    t_flood = time.time() - t_start
    cpu1 = cpu_times()
    sched1 = {k: (sched_of(pid, sth[k[1]]) if k[0] == "switch" else sched_of(mypid, next(h.tid for h in hs if h.idx == k[1]))) for k in sched0}
    for h in hs[:4]:
        h.measure = False
    sent = [f.sent for f in fl]
    total = sum(sent)
    # COMPLETION: not a fixed sleep. The flood is over when every reading port has received everything the switch wrote to it (frames == out counter)
    # and nothing moved for 1 s; the time that took is reported (it is what the old fixed 1.5 s wait raced against).
    t_d = time.time()
    s = stats(sw)
    stable_since, last = None, None
    while time.time() - t_d < 60:
        s = stats(sw)
        cur = (tuple(h.frames for h in hs[:4]), tuple(s[p][3] for p in (1, 2, 3, 4)) if s else None)
        if s and all(hs[p - 1].frames == s[p][3] for p in (1, 2, 3, 4)):
            if cur == last:
                if time.time() - stable_since >= 1.0:
                    break
            else:
                stable_since = time.time()
        else:
            stable_since = None
        last = cur
        time.sleep(0.1)
    t_drain = time.time() - t_d
    sampler.stop()
    for h in hogs:
        ta.stop_proc(h)
    total_t = time.time() - t_start

    # ---------------- diagnostics (every run) ----------------
    after = s
    def got(h):
        by = {}
        for tag in h.tags:
            fid, seq = struct.unpack("!II", tag)
            by.setdefault(fid, []).append(seq)
        return by
    print("  duration: flood %.2f s, drain %.2f s, total %.2f s; queue_capacity %d (queue occupancy is not exposed by the switch; a counted drop implies a full queue)" % (t_flood, t_drain, total_t, QCAP))
    print("  generated: host1 %d, host2 %d, total %d" % (sent[0], sent[1], total))
    if before and after:
        for p in range(1, 6):
            print("  port %d  in %6d -> %6d   out %6d -> %6d   dropped %6d -> %6d" % (p, before[p][2], after[p][2], before[p][3], after[p][3], before[p][4], after[p][4]))
    missing_total = {}
    for h in hs[:4]:
        by = got(h)
        exp = {}
        for i, f in enumerate(fl):
            if i + 1 != h.idx:                            # a host does not get its own frames
                exp[i + 1] = f.sent
        miss = {fid: sorted(set(range(n)) - set(by.get(fid, []))) for fid, n in exp.items()}
        dups = sum(len(v) - len(set(v)) for v in by.values())
        order = sum(1 for v in by.values() for a_, b_ in zip(v, v[1:]) if b_ < a_)
        missing_total[h.idx] = sum(len(v) for v in miss.values())
        print("  reader port %d: received %d of %d expected, missing %d (%s), duplicates %d, out-of-order %d; recv calls %d, max gap between recvs %.0f ms, gaps>50ms %d, thread alive %s%s"
              % (h.idx, h.frames, sum(exp.values()), missing_total[h.idx],
                 "; ".join("from host %d: seq %s" % (fid, ranges(v)) for fid, v in miss.items() if v) or "none",
                 dups, order, h.recvs, 1000 * h.max_gap, h.gaps50, h.is_alive(), ", ERROR " + h.error if h.error else ""))
    print("  system CPU during the flood: %s" % cpu_report(cpu0, cpu1))
    parts = []
    for k in sorted(sched0, key=str):
        a0, a1 = sched0[k], sched1.get(k)
        if a0 and a1:
            parts.append("%s %s: ran %.0f ms, waited for a CPU %.0f ms" % ("switch" if k[0] == "switch" else "python reader", ("tx-port%d" % int(k[1][-1])) if k[0] == "switch" and k[1].startswith("tx") else (k[1] if k[0] == "switch" else "port %d" % k[1]), a1[0] - a0[0], a1[1] - a0[1]))
    print("  scheduler (flood window %.1f s): %s" % (t_flood, "; ".join(parts)))
    print("  kernel queues (max, 10 Hz): reader rx_queue (unread by Python) %s KB; switch tx_queue (unread by the host) %s KB" %
          ({p: sampler.max_rx[p] // 1024 for p in (3, 4)}, {p: sampler.max_tx[p] // 1024 for p in (3, 4)}))
    first_drop = next((t for t, fr, d in sampler.timeline if d and any(d.get(p, 0) > 0 for p in (1, 2, 3, 4))), None)
    print("  first drop on a reading/sending port seen at %s (sampled every 0.5 s)" % ("%.1f s" % first_drop if first_drop is not None else "never"))

    # ---------------- assertions: the ORIGINAL five, unchanged in meaning ----------------
    dropped_map = {p: r[4] for p, r in after.items()} if after else None
    check(after and after[5][4] > 0, "the saturated port counts dropped frames (%s)" % (after[5][4] if after else None))
    if strict:
        check(after and after[3][4] == 0 and after[4][4] == 0 and after[1][4] == 0 and after[2][4] == 0, "no other port dropped anything (dropped per port: %s)" % dropped_map)
        check(hs[2].frames == total and hs[3].frames == total, "the reading hosts got every frame (%d, %d of %d; switch out counters %s)" % (hs[2].frames, hs[3].frames, total, [after[p][3] for p in (3, 4)] if after else None))
        check(hs[0].frames == sent[1] and hs[1].frames == sent[0], "the two senders got each other's frames")
    else:
        print("  stress run: frames lost on reading ports (counted by the switch as dropped): %s of %d broadcast to them" % ({p: after[p][4] for p in (1, 2, 3, 4)}, total))
        check(sent[0] > 3000 and sent[1] > 3000, "the senders were never blocked by the saturated port (%d, %d frames sent in 8 s)" % (sent[0], sent[1]))
    check(after and after[1][2] - before[1][2] == sent[0] and after[2][2] - before[2][2] == sent[1], "in counters match the senders")
    # ---------------- accounting identities (new): they say WHERE a missing frame went ----------------
    if after and before:
        for p, exp_n in ((1, sent[1]), (2, sent[0]), (3, total), (4, total)):
            o, d = after[p][3] - before[p][3], after[p][4] - before[p][4]
            check(o + d == exp_n, "port %d: out + dropped = frames broadcast to it (%d + %d vs %d)" % (p, o, d, exp_n))
            check(hs[p - 1].frames == o, "port %d: the reader received exactly what the switch counted as out (%d vs %d)" % (p, hs[p - 1].frames, o))
            check(missing_total[p] == d, "port %d: missing sequence numbers = switch's dropped counter (%d vs %d)" % (p, missing_total[p], d))
        # the port nobody reads: what it did not write and did not drop is still queued (queue_capacity) or in the sender's hand (1 frame)
        rest = total - (after[5][3] - before[5][3]) - (after[5][4] - before[5][4])
        check(0 <= rest <= QCAP + 1, "port 5 (never reads): frames neither written nor dropped = still queued or in flight: %d (at most %d + 1)" % (rest, QCAP))
    rc, dt = ta.sigint_and_wait(sw, 30)
    check(rc == 0 and dt < 5, "SIGINT with a full queue: exit 0 in %.2f s" % dt)
    finish(sw, hs, [])


def c6_stats_heavy():
    print("C6. swctl stats during heavy traffic: monotonic, consistent snapshots; final value exact")
    sw = ta.start(4)
    hs = ta.mk_hosts(sw, 4)
    fl = [Flooder(hs[i], BCAST, 1, 1, 200, 0) for i in range(3)]        # no gap: as fast as Python can
    for f in fl:
        f.start()
    prev = None
    bad = 0
    snaps = 0
    t_end = time.time() + 6
    lat = []
    while time.time() < t_end:
        t = time.time()
        s = stats(sw)
        lat.append(time.time() - t)
        if s is None:
            bad += 1
            continue
        snaps += 1
        cur = [s[p][2] for p in (1, 2, 3)] + [s[p][3] for p in (1, 2, 3, 4)]
        if prev and any(c < p for c, p in zip(cur, prev)):
            bad += 1
        # out of port 4 (never sends) can never exceed the in of the others, summed (every frame is broadcast)
        if s[4][3] > sum(s[p][2] for p in (1, 2, 3)):
            bad += 1
        prev = cur
    for f in fl:
        f.stop()
    sent = [f.sent for f in fl]
    # The flooders are faster than a slow (TSan / valgrind) switch: frames still sit in the kernel socket buffers when they stop. Wait until the
    # switch has READ everything that was sent (in == sent) and the counters stopped moving; the time it took is reported.
    t_drain = time.time()
    s = stats(sw)
    while time.time() - t_drain < 90:
        s = stats(sw)
        if s and [s[p][2] for p in (1, 2, 3)] == sent:
            time.sleep(1.5)                                   # the queues of the receivers empty
            s = stats(sw)
            break
        time.sleep(0.25)
    print("  backlog drained %.1f s after the flooders stopped" % (time.time() - t_drain))
    print("  %d snapshots, worst swctl stats latency %.0f ms, frames sent %s" % (snaps, 1000 * max(lat), sent))
    check(bad == 0 and snaps >= 20, "%d snapshots under load, all monotonic and consistent (%d bad)" % (snaps, bad))
    check(s and [s[p][2] for p in (1, 2, 3)] == sent, "final in counters = frames sent")
    check(s and s[4][3] == sum(sent) - s[4][4], "port 4: out + dropped = everything that was sent (out %d, dropped %d, sent %d)" % (s[4][3], s[4][4], sum(sent)))
    check([h.frames for h in hs][3] == s[4][3], "the host on port 4 received exactly the 'out' counter")
    finish(sw, hs, [])


def c7_churn():
    print("C7. show mac / show ports while hosts connect and disconnect")
    sw = ta.start(6)
    stop = threading.Event()
    errors = []
    pings = [0]

    def churn(idx):
        while not stop.is_set():
            try:
                h = ta.Host(sw, idx)
                h.send(BCAST, 1, b"hello")
                time.sleep(0.05)
                h.close()
            except OSError:
                pass
            time.sleep(0.02)

    base = [ta.Host(sw, 1), ta.Host(sw, 2)]
    ths = [threading.Thread(target=churn, args=(i,), daemon=True) for i in (3, 4, 5, 6)]
    for t in ths:
        t.start()
    t_end = time.time() + 10
    n = 0
    while time.time() < t_end:
        rc, out, err = ta.ctl(sw, "show", "ports")
        rows = ta.parse_ports(out)
        if rc != 0 or sorted(rows) != [1, 2, 3, 4, 5, 6] or any(r[0] not in ("FREE", "UP", "CLOSING") for r in rows.values()):
            errors.append(("ports", rc, out))
        rc, out, err = ta.ctl(sw, "show", "mac")
        if rc != 0:
            errors.append(("mac", rc, err))
        else:
            macs = ta.parse_macs(out)
            if any(m[1] not in ts.ME[1:7] or m[2] not in range(1, 7) for m in macs):
                errors.append(("mac-content", out))
        n += 1
    stop.set()
    for t in ths:
        t.join(10)
    print("  %d show ports + show mac rounds during churn" % n)
    check(n >= 20 and not errors, "every answer was OK and well formed (%d rounds, %d errors %s)" % (n, len(errors), errors[:1]))
    time.sleep(1)
    rows = ta.parse_ports(ta.ctl(sw, "show", "ports")[1])
    check([rows[i][0] for i in (1, 2)] == ["UP", "UP"] and all(rows[i][0] == "FREE" for i in (3, 4, 5, 6)), "afterwards: ports 1,2 UP, the rest FREE (%s)" % [r[0] for r in rows.values()])
    check(sw.p.poll() is None, "switch alive")
    for h in base:
        h.close()
    sw.stop()


def c8_sigint_everything():
    print("C8. SIGINT during traffic + rate limiting + a silent admin client + swctl stats --watch")
    sw = ta.start(4, "port.3.rate = 30\nqueue_capacity = 64\n")
    hs = ta.mk_hosts(sw, 4)
    fl = [Flooder(hs[i], BCAST, 1, i % 3, 100, 0.001) for i in range(2)]
    for f in fl:
        f.start()
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent.connect(ta.sock_path(sw))
    w = ta.track(subprocess.Popen(["./swctl", "stats", "--watch"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
    time.sleep(2.5)
    nfd0 = ta.nfds(sw.p.pid)
    rc, dt = ta.sigint_and_wait(sw, 30)
    check(rc == 0, "exit status 0 (got %s)" % rc)
    check(dt < 3, "left in %.2f s with traffic, a throttled port, a silent admin client and a watcher" % dt)
    check(not os.path.exists(ta.sock_path(sw)) and not os.path.exists(ta.SEG), "socket path and shared memory removed")
    try:
        wrc = w.wait(10)
    except subprocess.TimeoutExpired:
        w.kill()
        wrc = None
    check(wrc == 2, "swctl --watch noticed and exited with 2 (got %s)" % wrc)
    silent.close()
    for f in fl:
        f.stop()
    for h in hs:
        h.close()
    sw.stop()


def r_resources():
    print("R. resources: threads, descriptors, memory under sustained traffic; cleanup")
    sw = ta.start(8)
    pid = sw.p.pid
    # the log line "listening" comes BEFORE the threads exist: wait until the thread count has been the same for 1 s
    thr0, last, since = nthreads(pid), None, time.time()
    while time.time() - since < 1.0:
        thr0 = nthreads(pid)
        if thr0 != last:
            last, since = thr0, time.time()
        time.sleep(0.05)
    fd0, rss0 = ta.nfds(pid), rss_kb(pid)
    hs = ta.mk_hosts(sw, 8)
    thr1, fd1 = nthreads(pid), ta.nfds(pid)
    fl = [Flooder(hs[i], BCAST, 1, 1, 400, 0.001) for i in range(4)]
    for f in fl:
        f.start()
    rss_samples = []
    for _ in range(4):
        time.sleep(5)
        rss_samples.append(rss_kb(pid))
    c0 = cpu_s(pid)
    time.sleep(5)
    busy = (cpu_s(pid) - c0) / 5
    thr2, fd2 = nthreads(pid), ta.nfds(pid)
    for f in fl:
        f.stop()
    sent = sum(f.sent for f in fl)
    for h in hs:
        h.close()
    time.sleep(2)
    thr3, fd3 = nthreads(pid), ta.nfds(pid)
    c0 = cpu_s(pid)
    time.sleep(5)
    idle = cpu_s(pid) - c0
    print("  threads: start %d, 8 hosts %d, under load %d, after %d; fds: %d / %d / %d / %d" % (thr0, thr1, thr2, thr3, fd0, fd1, fd2, fd3))
    print("  RSS KB: start %d, under load every 5 s %s; frames sent in 25 s: %d; CPU under load %.0f %% of one core; idle (no hosts) %.2f s CPU in 5 s" % (rss0, rss_samples, sent, 100 * busy, idle))
    check(thr1 == thr0 + 16 and thr2 == thr1, "threads: +2 per host (rx, tx), none created by load (%d, %d, %d)" % (thr0, thr1, thr2))
    check(thr3 == thr0 and fd3 == fd0, "after the hosts left: threads and descriptors are back to the start values (%d/%d, %d/%d)" % (thr3, thr0, fd3, fd0))
    check(max(rss_samples) - min(rss_samples) < 8192, "RSS is flat under sustained traffic (%d KB spread)" % (max(rss_samples) - min(rss_samples)))
    check(idle < 0.2, "idle switch uses no CPU (%.2f s in 5 s)" % idle)
    rc, dt = ta.sigint_and_wait(sw, 30)
    check(rc == 0 and not os.path.exists(ta.SEG) and not os.path.exists(ta.sock_path(sw)), "clean exit, segment and socket gone")
    sw.stop()


def c5b_saturation_stress():
    return c5_saturation(gap=0.0005, strict=False)


SCEN = {"C1": c1_vlan_qos, "C2": c2_rate_isolation, "C3": c3_sighup_traffic, "C4": c4_aging_traffic, "C5": c5_saturation, "C5b": c5b_saturation_stress,
        "C6": c6_stats_heavy, "C7": c7_churn, "C8": c8_sigint_everything, "R": r_resources}

if __name__ == "__main__":
    names = sys.argv[1:] or list(SCEN)
    ta.install_signal_cleanup()
    try:
        ta.run_tests([SCEN[n] for n in names])          # cleanup of every switch / host / socket / watcher is in ta.run_scenario (finally)
    finally:
        ta.cleanup()
        subprocess.run(["rm", "-rf", ta.TMP])
    print("%d checks passed, %d failed" % (ta.passed, ta.failed))
    sys.exit(1 if ta.failed else 0)
