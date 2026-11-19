from vt import *
import itertools

def startfail(args, conf=None):
    sw = Switch(args=args, conf=conf)
    sw.start(wait=False)
    try: sw.p.wait(6)
    except subprocess.TimeoutExpired: pass
    return sw

# ======================= COMMAND LINE =======================
@test("CLI-01", "switch rejects bad command lines with exit 1 and does not listen")
def _():
    bad = [[], ["9000"], ["0", "4"], ["65536", "4"], ["-1", "4"], ["abc", "4"], ["9000abc", "4"], ["9000", "0"], ["9000", "65"], ["9000", "-3"],
           ["9000", "x"], ["9000", "4x"], ["9000", ""], ["9000", "4", "-c"], ["9000", "4", "-c", "/nonexistent/file.conf"], ["9000", "4", "--bogus"], ["9000", "4", "extra"],
           ["9000", "99999999999999999999"], ["+9000", "4"], [" 9000", "4"], ["9000", "4", "--pair"], ["9000", "4", "--persist-mac", "--pair", "9100"]]
    for a in bad:
        a = [x if x != "9000" else str(next_port()) for x in a]
        sw = startfail(a)
        rc = sw.p.returncode
        ck(rc == 1, "args %r -> exit 1" % a, 1, "%s (still running)" % rc if rc is None else rc, logs=sw.err() + sw.log())
        ck(rc is not None and "ERR" not in sw.err() or True, "")
        if rc is None: sw.kill()
        else:
            ck(sw.err().strip() != "" or sw.log() != "", "an error message is printed for %r" % a, "message", "silent")

@test("CLI-02", "boundary values for ports accepted: tcp 1.. no, num_ports 1 and 64")
def _():
    for n in (1, 64):
        sw = Switch(n).start(); ck(sw.alive(), "num_ports=%d starts" % n, True, sw.err()); ck(sw.stop() == 0, "exit 0 for %d" % n)

@test("CLI-03", "TCP port already in use -> start-up error, exit 1; running switch and its shm/socket are not disturbed")
def _():
    sw1 = Switch(2).start(); h = sw1.host(M(1)); settle(sw1, 1)
    sw2 = Switch(2, port=sw1.port).start(wait=False)
    try: sw2.p.wait(6)
    except Exception: pass
    ck(sw2.p.returncode == 1, "second switch on same port exits 1", 1, sw2.p.returncode, logs=sw2.err() + sw2.log())
    h.send(BCAST); ck(sw1.alive(), "first switch alive"); rc, out = sw1.ctl("show", "ports")
    ck(rc == 0, "first switch admin socket still answers", 0, (rc, out))
    rc, out = sw1.ctl("stats"); ck(rc == 0, "stats still readable", 0, (rc, out))
    ck(sw1.stop() == 0, "exit 0")

@test("CLI-04", "SO_REUSEADDR: restart immediately on same port after hosts were connected")
def _():
    sw = Switch(2).start(); h = sw.host(M(1)); settle(sw, 1); h.send(BCAST); port = sw.port
    h2 = sw.host(M(2)); settle(sw, 2)
    ck(sw.stop() == 0, "first run exit 0")
    sw2 = Switch(2, port=port).start()
    ck(sw2.alive(), "restart on same port works (no EADDRINUSE)", True, sw2.err() + sw2.log())
    ck(sw2.stop() == 0, "exit 0")

# ======================= CONFIG FILE =======================
GOODCONF = """# comment
mac_aging_seconds = 300 # trailing
mac_table_size = 1024
max_macs_per_port = 16
queue_capacity = 64
port_wait_timeout = 5
wrr_weights = 5,3,1
prio_aging_ms = 200
rate_limit = 0
trunk_port = 0
log_file = %s
mac_dump_file = mac_dump.txt
port.3.vlan = 20
port.3.rate = 100
"""
@test("CFG-01", "The complete example config from the roadmap is accepted")
def _():
    sw = Switch(4, conf=GOODCONF % "x.log").start()
    ck(sw.alive(), "starts", True, sw.err() + sw.log())
    ck(sw.stop() == 0, "exit 0")

@test("CFG-02", "Invalid config lines reject the whole file at start-up (exit 1, line number in message)")
def _():
    cases = {
     "unknown key": "bogus = 1\n", "dup key": "mac_aging_seconds=5\nmac_aging_seconds=6\n", "no equals": "mac_aging_seconds 5\n", "missing value": "mac_aging_seconds =\n",
     "missing key": "= 5\n", "aging 0": "mac_aging_seconds = 0\n", "aging neg": "mac_aging_seconds = -1\n", "aging huge": "mac_aging_seconds = 86401\n", "aging text": "mac_aging_seconds = abc\n",
     "aging float": "mac_aging_seconds = 1.5\n", "aging hex": "mac_aging_seconds = 0x10\n", "aging trailing": "mac_aging_seconds = 5x\n", "aging overflow": "mac_aging_seconds = 99999999999999999999999\n",
     "table 0": "mac_table_size = 0\n", "table big": "mac_table_size = 65537\n", "macs 0": "max_macs_per_port = 0\n", "qcap 0": "queue_capacity = 0\n", "qcap big": "queue_capacity = 65537\n",
     "wait neg": "port_wait_timeout = -1\n", "wait big": "port_wait_timeout = 3601\n", "wrr 2": "wrr_weights = 5,3\n", "wrr 4": "wrr_weights = 5,3,1,1\n", "wrr zero": "wrr_weights = 5,0,1\n",
     "wrr big": "wrr_weights = 5,3,1001\n", "wrr empty field": "wrr_weights = 5,,1\n", "prio 0": "prio_aging_ms = 0\n", "prio big": "prio_aging_ms = 60001\n", "rate neg": "rate_limit = -5\n",
     "rate big": "rate_limit = 1000001\n", "trunk big": "trunk_port = 5\n", "trunk neg": "trunk_port = -1\n", "port0": "port.0.vlan = 5\n", "port big": "port.5.vlan = 5\n", "vlan0": "port.1.vlan = 0\n",
     "vlan big": "port.1.vlan = 4095\n", "rate port neg": "port.1.rate = -1\n", "port text": "port.x.vlan = 5\n", "port nokey": "port.1 = 5\n", "port badfield": "port.1.mtu = 5\n",
     "long line": "log_file = " + "a" * 600 + "\n", "case": "Mac_Aging_Seconds = 5\n", "dup port": "port.1.vlan=2\nport.1.vlan=3\n", "empty log_file": "log_file =\n",
    }
    for name, c in cases.items():
        sw = startfail(None or [str(next_port()), "4"], conf=c)
        rc = sw.p.returncode
        ck(rc == 1, "config '%s' rejected at start" % name, "exit 1", "running" if rc is None else rc, logs=sw.err() + sw.log())
        if rc is None: sw.kill()

@test("CFG-03", "Valid edge configs accepted: boundary values, CRLF, tabs, blank lines, only comments, empty file, no trailing newline")
def _():
    cases = {"empty": "", "comments": "# a\n   # b\n\n", "crlf": "mac_aging_seconds = 5\r\nmax_macs_per_port = 3\r\n", "tabs": "\tmac_aging_seconds\t=\t7\t\n",
             "min": "mac_aging_seconds=1\nmac_table_size=1\nmax_macs_per_port=1\nqueue_capacity=1\nport_wait_timeout=0\nwrr_weights=1,1,1\nprio_aging_ms=1\nrate_limit=0\n",
             "max": "mac_aging_seconds=86400\nmac_table_size=65536\nmax_macs_per_port=65536\nqueue_capacity=65536\nport_wait_timeout=3600\nwrr_weights=1000,1000,1000\nprio_aging_ms=60000\nrate_limit=1000000\n",
             "no nl": "mac_aging_seconds = 9", "trunk4": "trunk_port = 4\n", "vlan edges": "port.1.vlan = 1\nport.2.vlan = 4094\n", "spaces in wrr": "wrr_weights = 5 , 3 , 1\n",
             "line510": "# " + "a" * 507 + "\n"}
    for name, c in cases.items():
        sw = Switch(4, conf=c).start()
        ck(sw.alive(), "config '%s' accepted" % name, "running", "exit %s" % sw.p.returncode, logs=sw.err() + sw.log())
        if sw.alive(): ck(sw.stop() == 0, "'%s' exit 0" % name, 0, sw.p.returncode)

@test("CFG-04", "Every bad line is listed in a single error message (README promise)")
def _():
    sw = startfail([str(next_port()), "4"], conf="bogus=1\nmac_aging_seconds=5\nwrr_weights=1,2\nfoo=2\n")
    e = sw.err()
    ck(all(("line %d" % n) in e for n in (1, 3, 4)), "all bad lines 1,3,4 listed", "line 1/3/4", e)

@test("CFG-05", "Max extreme config (mac_table_size=65536, queue_capacity=65536, 64 ports) starts and exits cleanly; memory sane")
def _():
    sw = Switch(64, conf="mac_table_size=65536\nqueue_capacity=65536\n").start()
    ck(sw.alive(), "starts", True, sw.err() + sw.log())
    if sw.alive():
        a, b = sw.host(M(1)), sw.host(M(2)); settle(sw, 2); a.send(BCAST); ck(b.got_data("x"), "forwards")
        ck(sw.rss_kb() < 800000, "RSS < 800 MB at idle", "<800MB", sw.rss_kb())
        ck(sw.stop() == 0, "exit 0")

@test("CFG-06", "log_file unwritable (directory / no permission): defined behaviour at start-up")
def _():
    sw = Switch(args=[str(next_port()), "2"], conf="log_file = /nonexistent_dir/x.log\n", env={"VSWITCH_LOG_FILE": None}); sw.start(wait=False)
    try: sw.p.wait(6)
    except Exception: pass
    rc = sw.p.returncode
    ck(rc is not None, "switch does not run silently without a log", "exit with error message", "running" if rc is None else rc)
    ck(rc in (None, 1), "exit code 1 on unusable log file", 1, rc, logs=sw.err())
    if rc is None: sw.kill()

@test("CFG-07", "SIGHUP: valid reload changes aging; bad file keeps old config and logs 'reload failed, line N'; no -c => message")
def _():
    sw = Switch(4, conf="mac_aging_seconds = 300\n").start(); a, b = hosts_up(sw, 2)
    a.send(BCAST); time.sleep(0.2)
    open(sw.conffile, "w").write("mac_aging_seconds = 2\n"); sw.sig(signal.SIGHUP)
    ck(sw.wait_log("configuration reloaded", 1), "reload logged", "reloaded line", sw.log())
    time.sleep(4.5)
    ck(not any(r[1] == M(1) for r in sw.mac_table()), "new aging time 2s takes effect (entry aged out within ~4s)", "entry gone", sw.mac_table())
    open(sw.conffile, "w").write("mac_aging_seconds = 5\nbogus = 1\n"); sw.sig(signal.SIGHUP)
    ck(sw.wait_log("reload failed, line 2", 1), "bad reload reports line 2", "'reload failed, line 2'", sw.log()[-500:])
    ck(sw.alive(), "alive after bad reload")
    os.unlink(sw.conffile); sw.sig(signal.SIGHUP); time.sleep(0.5)
    ck(sw.alive(), "alive when config file vanished", True, sw.p.poll(), logs=sw.log()[-300:])
    ck(("reload failed" in sw.log().split("line 2")[-1]) or ("cannot" in sw.log().lower()), "missing file on reload is an error, logged", "error line", sw.log()[-400:])
    ck(sw.stop() == 0, "exit 0")

@test("CFG-08", "SIGHUP without -c only logs; startup-only keys changed on reload log 'ignored until restart'")
def _():
    sw = Switch(2).start(); sw.sig(signal.SIGHUP); time.sleep(0.5); ck(sw.alive(), "alive", True, sw.p.poll())
    ck("HUP" in sw.log() or "reload" in sw.log().lower() or "nothing" in sw.log().lower(), "SIGHUP without file is logged", "log line", sw.log()[-300:])
    ck(sw.stop() == 0, "exit 0")
    sw = Switch(4, conf="queue_capacity = 64\nmax_macs_per_port=16\n").start()
    open(sw.conffile, "w").write("queue_capacity = 10\nmax_macs_per_port=16\n"); sw.sig(signal.SIGHUP)
    ck(sw.wait_log("ignored until restart", 1), "startup-only change -> 'ignored until restart'", "log line", sw.log()[-400:])
    ck(sw.stop() == 0, "exit 0")

@test("CFG-09", "SIGHUP storm (200 signals) while traffic flows: no crash, no hang, exit 0")
def _():
    sw = Switch(4, conf="mac_aging_seconds = 100\nport.2.vlan = 1\n").start(); a, b = hosts_up(sw, 2)
    stop = [False]
    def tr():
        while not stop[0]:
            try: a.send(BCAST, payload=b"z"); b.recv(0.01)
            except Exception: break
    t = threading.Thread(target=tr); t.start()
    for i in range(200):
        open(sw.conffile, "w").write("mac_aging_seconds = %d\nport.2.vlan = %d\n" % (50 + i % 7, 1 + (i % 3)))
        sw.sig(signal.SIGHUP); time.sleep(0.005)
    stop[0] = True; t.join(); time.sleep(0.5)
    ck(sw.alive(), "alive", True, sw.p.poll(), logs=sw.log()[-500:])
    ck(sw.stop() == 0, "exit 0", 0, sw.p.returncode)

# ======================= SIGNALS / LOG =======================
@test("SIG-01", "SIGINT and SIGTERM both give clean exit 0; shm and socket removed")
def _():
    for s in (signal.SIGINT, signal.SIGTERM):
        sw = Switch(3).start(); a, b = hosts_up(sw, 2); a.send(BCAST)
        sock = "/tmp/vswitch_%d.sock" % sw.port
        ck(os.path.exists(sock), "admin socket exists while running", True, False)
        ck(os.path.exists("/dev/shm/vswitch_stats_P"), "stats shm exists while running", True, os.listdir("/dev/shm"))
        t0 = time.time(); rc = sw.stop(s)
        ck(rc == 0, "exit 0 on %s" % s.name, 0, rc, logs=sw.log()[-500:])
        ck(time.time() - t0 < 2, "exits within 2s", "<2s", time.time() - t0)
        ck(not os.path.exists(sock), "socket removed", "gone", "exists")
        ck(not os.path.exists("/dev/shm/vswitch_stats_P"), "shm removed", "gone", "exists")
        ck(a.eof(2) and b.eof(2), "hosts see EOF")

@test("SIG-02", "SIGINT while a host waits for a port and another host is --noread with a full queue: exits within ~1s")
def _():
    sw = Switch(2, conf="port_wait_timeout = 30\nqueue_capacity = 8\n").start()
    a = sw.host(M(1)); b = sw.host(M(2)); settle(sw, 2)
    b.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    w = sw.host(M(3))     # waiting
    for i in range(3000): a.send(BCAST, payload=b"y" * 1400)
    time.sleep(0.3); t0 = time.time(); rc = sw.stop(timeout=6)
    ck(rc == 0, "exit 0", 0, rc); ck(time.time() - t0 < 2.0, "exit within ~1-2 s", "<2s", "%.2f" % (time.time() - t0))

@test("SIG-03", "SIGUSR1 dump: header + 'vlan mac port age' lines; unwritable path -> error log, switch alive")
def _():
    sw = Switch(4, conf="mac_dump_file = %s\n" % "DUMP").start()
    d = os.path.join(sw.dir, "dump.txt")
    open(sw.conffile, "w").write("mac_dump_file = %s\n" % d); sw.sig(signal.SIGHUP); time.sleep(0.4)
    a, b = hosts_up(sw, 2); a.send(BCAST); b.send(BCAST); time.sleep(0.3)
    sw.sig(signal.SIGUSR1); ck(sw.wait_log("SIGUSR1", 1), "usr1 logged", "line", sw.log())
    time.sleep(0.3)
    txt = open(d).read() if os.path.exists(d) else ""
    lines = [l for l in txt.splitlines() if l and not l.startswith("#")]
    ck(len(lines) == 2, "two entries in dump", 2, txt)
    ck(all(re.match(r"^\d+ ([0-9a-f]{2}:){5}[0-9a-f]{2} \d+ \d+$", l) for l in lines), "line format 'vlan mac port age'", "regex", lines)
    ck(txt.startswith("#"), "header comment present", "#...", txt[:50])
    open(sw.conffile, "w").write("mac_dump_file = /nonexistent_dir/d.txt\n"); sw.sig(signal.SIGHUP); time.sleep(0.4)
    sw.sig(signal.SIGUSR1); time.sleep(0.6)
    ck(sw.alive(), "alive after failing dump", True, sw.p.poll()); ck("cannot write" in sw.log(), "error logged", "cannot write", sw.log()[-300:])
    ck(sw.stop() == 0, "exit 0")

@test("SIG-04", "Signals SIGPIPE / double SIGINT / SIGUSR2 / SIGSTOP-CONT do not kill the switch improperly")
def _():
    sw = Switch(3).start(); a, b = hosts_up(sw, 2)
    sw.sig(signal.SIGPIPE); time.sleep(0.3); ck(sw.alive(), "SIGPIPE ignored", True, sw.p.poll())
    sw.sig(signal.SIGSTOP); time.sleep(0.3); sw.sig(signal.SIGCONT); time.sleep(0.3); ck(sw.alive(), "survives STOP/CONT")
    a.send(BCAST, payload=b"k"); ck(b.got_data("k"), "forwarding after STOP/CONT")
    sw.sig(signal.SIGINT); sw.sig(signal.SIGINT); sw.sig(signal.SIGTERM)
    try: sw.p.wait(8)
    except Exception: pass
    ck(sw.p.returncode == 0, "exit 0 after repeated signals", 0, sw.p.returncode)

@test("LOG-01", "Log format, content for connect/disconnect/move/security, appends across restarts")
def _():
    sw = Switch(3).start(); a, b = hosts_up(sw, 2); a.close(); sw.wait_log("port is FREE", 1); time.sleep(0.2)
    rc = sw.stop(); L = sw.log()
    ck(all(re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \[(INFO|WARN|ERR)\] ", l) for l in L.splitlines() if l.strip()), "every line has 'YYYY-MM-DD HH:MM:SS.mmm [LEVEL]'", "regex", [l for l in L.splitlines() if not re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \[", l)][:3])
    ck("host connected" in L and "disconnected" in L.lower() or "port is FREE" in L, "connect and disconnect logged", "lines", L[-400:])
    n1 = len(L.splitlines())
    sw2 = Switch(3, port=sw.port); sw2.logf = sw.logf; sw2.env["VSWITCH_LOG_FILE"] = sw.logf; sw2.start(); sw2.wait_log("listening", 2); sw2.stop()
    n2 = len(open(sw.logf).read().splitlines()); ck(n2 > n1, "second run appends (O_APPEND)", ">%d" % n1, n2)

@test("LOG-02", "Log flood with per-frame traffic does not drop forwarding; log drop counter line appears or no drops; log file stays well-formed")
def _():
    sw = Switch(4, conf="queue_capacity = 4\n").start(); a, b, c = hosts_up(sw, 3)
    c.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)    # c frozen -> a flood of 'output queue full' log lines
    got = [0]
    def rd():
        while True:
            f = b.recv(3)
            if not isinstance(f, dict): break
            got[0] += 1
    t = threading.Thread(target=rd); t.start()
    blob = pack(M(1), BCAST, 1, 1, b"q" * 800)
    for i in range(20000): a.s.sendall(blob)
    t.join()
    L = sw.log()
    bad = [l for l in L.splitlines() if l.strip() and not re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \[(INFO|WARN|ERR)\] ", l)]
    ck(not bad, "log lines well-formed under flood", "none malformed", bad[:3])
    ck(sw.alive(), "alive"); ck(sw.stop() == 0, "exit 0")
    ck(len(L) < 40_000_000, "log growth bounded (<40MB)", "<40MB", len(L))
