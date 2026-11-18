#!/usr/bin/env python3
"""
Part 10 end-to-end tests: configuration file, SIGHUP reload, SIGUSR1 MAC dump, with the real ./switch.

  python3 tests/test_config_signals.py            normal build
  python3 tests/test_config_signals.py tsan       SWITCH="setarch -R ./switch_tsan"   (reloads and dumps under traffic)
  python3 tests/test_config_signals.py valgrind   valgrind on ./switch (no errors, no leaks, no descriptor left open)

The parser itself is tested in tests/test_config.c. Every wait has a timeout.
"""
import os, re, shlex, signal, subprocess, sys, tempfile, threading, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_switch as ts
import test_forward as tf
import test_logger as tl

ME, BCAST = ts.ME, ts.BCAST
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
tl.check = check

TMP = tempfile.mkdtemp(prefix="vswcfg.")
conf_no = 0


def conf(text):
    """Write a configuration file (atomically: write + rename) and return its path."""
    global conf_no
    conf_no += 1
    path = os.path.join(TMP, "c%03d.conf" % conf_no)
    return write_conf(path, text)


def write_conf(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def hup(sw):
    sw.p.send_signal(signal.SIGHUP)


def reload_and_wait(sw, path, text, expect="configuration reloaded from", timeout=15):
    """Write `text` to the config file, send SIGHUP and wait for the next line with `expect`."""
    n = sw.count(expect)
    write_conf(path, text)
    hup(sw)
    return sw.wait_log(expect, timeout, n + 1)


def probe_until_flooded(a, b, c_mac, tag, limit):
    """A sends a unicast for C every 0.25 s until B (not the owner) gets one, i.e. C's entry is gone.
    -> seconds it took, or None after `limit` seconds."""
    t0 = time.time()
    i = 0
    while time.time() - t0 < limit:
        i += 1
        a.say("send %s %s-%d" % (c_mac, tag, i))
        time.sleep(0.25)
        if b.p.poll() is None and any(("data=%s-" % tag) in l for l in b.rx()):
            return time.time() - t0
    return None


def hosts_abc(sw):
    a, b, c = tf.H(sw, ME[0]), tf.H(sw, ME[1]), tf.H(sw, ME[2])
    check(a.ok and b.ok and c.ok, "three hosts connected")
    c.say("send bcast c-hello")                                 # C is learned now
    check(a.wait_rx(1, 10) and b.wait_rx(1, 10), "C's first frame was flooded")
    return a, b, c


def wait_line(h, text, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if any(text in l for l in h.rx()):
            return True
        time.sleep(0.02)
    return False


def quit_all(*hs):
    for h in hs:
        h.quit(10)


# ----------------------------------------------------------------------------------------------
def test_startup():
    print("1. start-up: values from the file, defaults without a file, bad files are start-up errors")
    sw = ts.Switch(4, extra=["-c", conf("mac_aging_seconds = 42\nmax_macs_per_port = 3\nmac_table_size = 64\nqueue_capacity = 8\n")])
    log = sw.log()
    check("aging 42s" in log and "at most 3 MACs per port" in log and "64 entries, 64 buckets" in log,
          "the file's values are in use (aging 42 s, 3 MACs per port, 64 entries / 64 buckets)")
    check("configuration file: " in log, "the file is named in the log")
    sw.stop()

    sw = ts.Switch(4)
    log = sw.log()
    check("no configuration file: using the defaults" in log and "aging 300s" in log and "1024 entries, 256 buckets" in log,
          "without -c the defaults of the roadmap are used")
    sw.stop()

    bad = [
        ("unknown key", "bogus = 1\n", [1]),
        ("bad number", "# c\nmac_aging_seconds = abc\n", [2]),
        ("out of range", "max_macs_per_port = 0\n", [1]),
        ("several errors", "mac_aging_seconds = 5\nfoo = 1\n\nwrr_weights = 5,3\nport.99.vlan = 3\n", [2, 4, 5]),
        ("bad wrr", "wrr_weights = 1,2,3,4\n", [1]),
        ("bad port setting", "port.2.vlan = 5000\n", [1]),
    ]
    for name, text, lines in bad:
        p = subprocess.run(shlex.split(ts.SWITCH) + [str(ts.free_port()), "4", "-c", conf(text)], capture_output=True, timeout=30)
        err = p.stderr.decode()
        check(p.returncode == 1, "%s: exit status 1 (got %s)" % (name, p.returncode))
        check(all(("line %d:" % n) in err for n in lines), "%s: the message names line(s) %s: %s" % (name, lines, err.strip()[:150]))
        check(b"listening" not in p.stdout + p.stderr, "%s: the switch did not start" % name)
    p = subprocess.run(shlex.split(ts.SWITCH) + [str(ts.free_port()), "4", "-c", os.path.join(TMP, "missing.conf")], capture_output=True, timeout=30)
    check(p.returncode == 1 and b"cannot open" in p.stderr, "a missing file is a start-up error")
    p = subprocess.run(shlex.split(ts.SWITCH) + [str(ts.free_port()), "4", "-c", TMP], capture_output=True, timeout=30)
    check(p.returncode == 1, "a directory as configuration file is a start-up error")
    p = subprocess.run(shlex.split(ts.SWITCH) + [str(ts.free_port()), "4", "-c"], capture_output=True, timeout=30)
    check(p.returncode == 1 and b"usage" in p.stderr, "-c without a file name: usage")
    p = subprocess.run(shlex.split(ts.SWITCH) + [str(ts.free_port()), "4", "-x", "f"], capture_output=True, timeout=30)
    check(p.returncode == 1 and b"usage" in p.stderr, "an unknown option: usage")
    p = subprocess.run(shlex.split(ts.SWITCH) + [str(ts.free_port()), "4", "-c", conf("trunk_port = 5\n")], capture_output=True, timeout=30)
    check(p.returncode == 1 and b"only 4 ports" in p.stderr, "trunk_port above the number of ports is rejected")


def test_sighup_aging():
    print("2. SIGHUP: a new mac_aging_seconds takes effect without a restart (both directions)")
    path = conf("mac_aging_seconds = 300\n")
    sw = ts.Switch(4, extra=["-c", path])
    a, b, c = hosts_abc(sw)
    # T = 300: C's entry does not age. Probing for 4.5 s must never flood (T = 3 would have removed it).
    took = probe_until_flooded(a, b, ME[2], "before", 4.5)
    check(took is None, "T = 300: C's entry is still there after 4.5 s (no probe was flooded)")
    check(reload_and_wait(sw, path, "mac_aging_seconds = 3\n"), "SIGHUP: the reload is in the log")
    check(sw.count("reload: mac_aging_seconds 300 -> 3") == 1, "the log shows the new aging time: 300 -> 3")
    check("(mac aging 3s)" in sw.log(), "the summary line shows the aging time in use")
    took = probe_until_flooded(a, b, ME[2], "after", 12)
    check(took is not None, "after the reload to T = 3 the old entry of C ages out and a probe is flooded (%s s)" %
          ("%.1f" % took if took else "never"))
    check(took is not None and took <= 6, "...within the next aging cycles (%.1f s)" % (took or 99))
    # the other direction: raise T again, relearn C, nothing ages
    check(reload_and_wait(sw, path, "mac_aging_seconds = 300\n"), "SIGHUP: back to 300")
    c.say("send bcast c-again")
    check(wait_line(a, "data=c-again"), "C announced itself again (learned again)")
    took = probe_until_flooded(a, b, ME[2], "late", 5)
    check(took is None, "T = 300 again: C stays learned for 5 s (no probe flooded)")
    quit_all(a, b, c)
    sw.stop()


def test_sighup_invalid():
    print("3. SIGHUP with a broken file: 'reload failed, line N' and the whole previous configuration stays")
    path = conf("mac_aging_seconds = 3\nmax_macs_per_port = 7\nwrr_weights = 2,2,2\n")
    sw = ts.Switch(4, extra=["-c", path])
    a, b, c = hosts_abc(sw)
    # line 1 is valid (and would change T to 300), line 3 is not: nothing at all may be applied
    n = sw.count("reload failed")
    write_conf(path, "mac_aging_seconds = 300\nmax_macs_per_port = 9\nbogus_key = 1\nwrr_weights = 5,3\n")
    hup(sw)
    check(sw.wait_log("reload failed", 10, n + 1), "SIGHUP with an invalid file: 'reload failed' is logged")
    log = sw.log()
    check("reload failed, line 3: unknown key 'bogus_key'" in log, "the first bad line is named (line 3): ...")
    check("line 4:" in log.split("reload failed")[-1] and "(2 errors)" in log, "...and the second one (line 4); 2 errors counted")
    check("the previous configuration stays in force" in log, "the log says that the previous configuration stays")
    check("[ERR] reload failed" in log, "logged as an error")
    check(sw.count("reload: mac_aging_seconds") == 0 and sw.count("reload: max_macs_per_port") == 0,
          "no setting of the broken file was applied (the valid lines before the error neither)")
    # behaviour: T is still 3 (a T of 300 would keep C's entry for ever)
    took = probe_until_flooded(a, b, ME[2], "stillthree", 12)
    check(took is not None and took <= 7, "C's entry still ages out in about 3 s: T = 3 is still in force (%s)" %
          ("%.1f s" % took if took else "never"))
    # an unreadable file
    n = sw.count("reload failed")
    os.unlink(path)
    hup(sw)
    check(sw.wait_log("reload failed", 10, n + 1) and "cannot open" in sw.log().split("reload failed")[-1],
          "a file that has disappeared: reload failed, cannot open")
    # ... and a good file makes the next reload work again
    check(reload_and_wait(sw, path, "mac_aging_seconds = 11\n"), "a fixed file reloads normally")
    check("reload: mac_aging_seconds 3 -> 11" in sw.log(), "3 -> 11 (the failed reloads left 3 in place)")
    # an empty file is valid: everything goes back to the defaults
    check(reload_and_wait(sw, path, "# nothing\n"), "an empty file reloads: all keys are the defaults")
    check("reload: mac_aging_seconds 11 -> 300" in sw.log(), "keys that the file does not mention are the defaults (300)")
    check(sw.p.poll() is None, "the switch is still running")
    quit_all(a, b, c)
    sw.stop()


def test_sighup_startup_only():
    print("4. SIGHUP: startup-only keys are 'ignored until restart'; VLANs are stored, not applied; weights, aging (Part 11) and rates (Part 12) are applied")
    path = conf("mac_aging_seconds = 50\n")
    sw = ts.Switch(4, extra=["-c", path])
    ok = reload_and_wait(sw, path, "mac_aging_seconds = 60\nmac_table_size = 2048\nqueue_capacity = 8\ntrunk_port = 2\n"
                                   "log_file = other.log\nport.3.vlan = 20\nport.3.rate = 100\nrate_limit = 500\n"
                                   "wrr_weights = 6,4,2\nprio_aging_ms = 150\nport_wait_timeout = 9\nmax_macs_per_port = 4\n")
    check(ok, "reload with startup-only and not-yet-used keys succeeded")
    log = sw.log()
    for key in ("mac_table_size = 2048 in the file, running with 1024", "queue_capacity = 8 in the file, running with 64",
                "trunk_port = 2 in the file, running with 0", "log_file = 'other.log' in the file"):
        check(key in log and "ignored until restart" in log.split(key)[1].split("\n")[0], "'%s ... ignored until restart'" % key)
    check(sw.count("ignored until restart") == 4, "exactly the 4 startup-only keys are reported")
    check("reload: mac_aging_seconds 50 -> 60" in log and "reload: max_macs_per_port 16 -> 4" in log and
          "reload: port_wait_timeout 5 -> 9" in log, "the reloadable keys with an effect now are applied and reported")
    # Part 13 made the VLANs effective: the change is applied and logged, no longer 'stored'
    check("reload: port.3.vlan 0 -> 20" in log and "VLANs are not applied" not in log, "port.3.vlan is reported as a setting, no longer as 'stored' (Part 13)")
    check("reload: port 3 VLAN 1 -> 20, its old MAC entries were flushed" in log, "the effective VLAN of port 3 (1 -> 20) is applied and logged (Part 13)")
    # Part 12 made the rates effective: the log must say that they are applied, not 'stored'
    check("reload: port.3.rate -1 -> 100 (frames/s" in log and "stored; rate" not in log, "port.3.rate is reported as a rate, no longer as 'stored' (Part 12)")
    check("reload: rate_limit 0 -> 500 (frames/s" in log and "no effect until the rate limiting" not in log,
          "rate_limit is reported as applied, no longer as 'not used yet' (Part 12)")
    check("reload: port 3 rate limit -1 -> 100" not in log and "port 3 rate limit 0 -> 100 frames/s" in log,
          "the effective rate of port 3 (0 -> 100) is applied and logged (Part 12)")
    # Part 11 made these two effective: the log must say that they are applied, not 'stored'
    check("wrr_weights 5,3,1 -> 6,4,2 (applies at the next scheduling round of every port)" in log and
          "prio_aging_ms 200 -> 150 (applies to the next frame picked)" in log,
          "wrr_weights and prio_aging_ms are applied to the priority queues (Part 11)")
    check("wrr_weights 5,3,1 -> 6,4,2 (stored" not in log and "prio_aging_ms 200 -> 150 (stored" not in log,
          "...and no longer reported as 'stored'")
    # the same file again: the startup-only keys are reported again (they are still not in force), nothing else changes
    n = sw.count("ignored until restart")
    hup(sw)
    check(sw.wait_log("ignored until restart", 10, n + 4), "a second reload of the same file reports the startup-only keys again")
    check("0 settings changed" in sw.log(), "...and no other setting changed: '0 settings changed'")
    check(sw.count("[ERR]") == 0, "no error line")
    sw.stop()


def test_sighup_no_file():
    print("5. SIGHUP without a configuration file (started without -c)")
    sw = ts.Switch(2)
    hup(sw)
    check(sw.wait_log("no configuration file (the switch was started without -c): nothing to reload", 10), "logged, nothing to reload")
    check(sw.p.poll() is None, "the switch is still running")
    sw.stop()


def test_sighup_under_traffic():
    print("6. repeated SIGHUP (valid and invalid files) while a flood of frames goes through the switch")
    path = conf("mac_aging_seconds = 300\n")
    sw = ts.Switch(4, extra=["-c", path])
    res = {}

    def run():
        try:
            res["r"] = tl.flood(sw.port, int(os.environ.get("CFG_FLOOD", "60000")))
        except Exception as e:           # reported by the check below
            res["e"] = repr(e)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(0.5)
    # Part 13: a changed port.N.vlan is applied now. The flood uses ports 1-3, so the VLAN setting that comes and goes is port 4's.
    texts = ["mac_aging_seconds = 3\nmax_macs_per_port = 3\n", "mac_aging_seconds = 300\nmax_macs_per_port = 16\nport.4.vlan = 5\n",
             "mac_aging_seconds = 17\nbogus = 1\n", "mac_aging_seconds = 5\nwrr_weights = 9,9,9\n"]
    sent = 0
    end = time.time() + 40
    i = 0
    while th.is_alive() and time.time() < end:
        write_conf(path, texts[i % len(texts)])
        hup(sw)
        sent += 1
        i += 1
        time.sleep(0.03)
    th.join(60)
    check("r" in res and res["r"][0] is not None, "the flood went through while the switch was reloading (%s)" % str(res.get("r") or res.get("e")))
    # signals do not queue: several SIGHUPs may merge, so only 'at least one' can be asserted; the LAST file must win
    write_conf(path, "mac_aging_seconds = 29\nmax_macs_per_port = 12\n")
    n = sw.count("configuration reloaded from")
    hup(sw)
    check(sw.wait_log("configuration reloaded from", 15, n + 1), "a last SIGHUP is handled after the burst")
    log = sw.log()
    check(sw.count("configuration reloaded from") >= 2, "several reloads happened (%d)" % sw.count("configuration reloaded from"))
    last = [l for l in log.splitlines() if "reloaded from" in l][-1]
    check("(mac aging 29s)" in last, "the last reload's values (29 s) are the ones in force: %s" % last[-60:])
    check(sw.p.poll() is None, "the switch is alive")
    errs = [l for l in log.splitlines() if "[ERR]" in l]
    check(all("reload failed" in l for l in errs), "the only error lines are the expected 'reload failed' (%d)" % len(errs))
    good, bad = tl.parse(log)
    check(not bad, "every log line is whole (%d bad)" % len(bad))
    sw.p.send_signal(signal.SIGINT)
    check(sw.p.wait(30) == 0, "SIGINT: exit status 0 after the reload storm")
    check("shutdown: complete" in sw.log(), "clean shutdown")
    sw.stop()


# ---- SIGUSR1 ----
LINE = re.compile(r"^(\d+) ([0-9a-f]{2}(?::[0-9a-f]{2}){5}) (\d+) (\d+)$")


def read_dump(path):
    """-> (header lines, [(vlan, mac, port, age)], bad lines)"""
    with open(path) as f:
        lines = f.read().splitlines()
    hdr = [l for l in lines if l.startswith("#")]
    rows, bad = [], []
    for l in lines:
        if l.startswith("#"):
            continue
        m = LINE.match(l)
        if m:
            rows.append((int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))))
        else:
            bad.append(l)
    return hdr, rows, bad


def dump_and_wait(sw, path, timeout=15):
    n = sw.count("MAC table dumped to")
    sw.p.send_signal(signal.SIGUSR1)
    return sw.wait_log("MAC table dumped to", timeout, n + 1)


def test_sigusr1_dump():
    print("7. SIGUSR1: the MAC table is dumped to mac_dump_file: header with the time, 'vlan mac port age_s'")
    dump = os.path.join(TMP, "dump.txt")
    # Part 13: only the trunk port keeps the VLAN of a frame, so B (port 2) is the trunk to be able to appear in a second VLAN
    sw = ts.Switch(4, extra=["-c", conf("mac_dump_file = %s\ntrunk_port = 2\n" % dump)])
    a, b, c = hosts_abc(sw)                                    # C is learned (vlan 1, port 3)
    a.say("send bcast a-hello")                                # A: vlan 1, port 1
    b.say("sendx bcast 100 1 b-vlan100")                       # B (trunk), in a second VLAN: its own entry (not seen by C: VLAN 1)
    b.say("send bcast b-hello")                                # B again, vlan 1; B's receiver handles its frames in order
    check(wait_line(c, "data=a-hello") and wait_line(c, "data=b-hello"),
          "all frames were forwarded (so all four entries are learned: b-hello follows b-vlan100 from the same host)")
    time.sleep(2.2)                                            # let the entries age a little
    t_before = time.time()
    check(dump_and_wait(sw, dump), "SIGUSR1: 'MAC table dumped to' is logged")
    hdr, rows, bad = read_dump(dump)
    check(len(hdr) == 2 and hdr[0].startswith("# MAC table dump, ") and hdr[1] == "# vlan mac port age_s", "two header lines: %s" % hdr)
    m = re.match(r"# MAC table dump, (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)$", hdr[0]) if hdr else None
    stamp = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) if m else 0
    check(m is not None and abs(stamp - time.time()) < 10, "the header has the current time (%s)" % (m.group(1) if m else None))
    check(not bad, "every other line is 'vlan mac port age_s': %s" % bad[:2])
    keyed = {(v, mac): (port, age) for v, mac, port, age in rows}
    check(len(rows) == len(keyed) == 4, "four entries, none twice: %s" % rows)
    check(keyed.get((1, ME[2])) is not None and keyed[(1, ME[2])][0] == 3, "C: vlan 1, port 3")
    check(keyed.get((1, ME[0])) is not None and keyed[(1, ME[0])][0] == 1, "A: vlan 1, port 1")
    check(keyed.get((1, ME[1])) is not None and keyed[(1, ME[1])][0] == 2, "B: vlan 1, port 2")
    check(keyed.get((100, ME[1])) is not None and keyed[(100, ME[1])][0] == 2, "B in VLAN 100: vlan 100, port 2 (a separate entry)")
    ages = [age for (_, _), (_, age) in keyed.items()]
    check(all(2 <= x <= 12 for x in ages), "the ages are about 2-3 s (whole seconds since last seen): %s" % ages)
    # age grows
    time.sleep(2.2)
    check(dump_and_wait(sw, dump), "a second dump")
    _, rows2, bad2 = read_dump(dump)
    keyed2 = {(v, mac): (port, age) for v, mac, port, age in rows2}
    check(not bad2 and len(rows2) == 4 and all(keyed2[k][1] >= keyed[k][1] + 2 for k in keyed), "the ages grew by about 2 s")
    # mode "w": the file is rewritten, not appended; old content is gone
    with open(dump, "w") as f:
        f.write("junk line\n" * 200)
    check(dump_and_wait(sw, dump), "a third dump over a file full of junk")
    hdr3, rows3, bad3 = read_dump(dump)
    check(not bad3 and len(rows3) == 4 and "junk line" not in open(dump).read(), "the file was truncated (mode 'w'): no junk left, 4 entries")
    # the table is untouched and forwarding still works: a unicast goes to its owner only
    a.say("send %s uni-after-dump" % ME[2])
    check(wait_line(c, "data=uni-after-dump"), "traffic still flows: C got A's unicast")
    b.say("send bcast sync2")
    check(wait_line(a, "data=sync2"), "sync: everything A sent before reached B")
    check(not any("data=uni-after-dump" in l for l in b.rx()), "unicast forwarding still works after the dumps (B did not get A's frame for C)")
    quit_all(a, b, c)
    check(sw.count("[ERR]") == 0, "no error lines")
    sw.stop()


def test_sigusr1_errors_and_default_name():
    print("8. SIGUSR1: unwritable file is an error (the switch stays up); default name mac_dump.txt in the working directory")
    path = conf("mac_dump_file = /nonexistent-dir/dump.txt\n")
    sw = ts.Switch(2, extra=["-c", path])
    sw.p.send_signal(signal.SIGUSR1)
    check(sw.wait_log("SIGUSR1: cannot write the MAC table to '/nonexistent-dir/dump.txt': No such file or directory", 10),
          "an error is logged with the reason")
    check(sw.p.poll() is None, "the switch is still running")
    # repair it with a SIGHUP: the new file name is used by the next dump (mac_dump_file is reloadable)
    good = os.path.join(TMP, "reloaded_dump.txt")
    check(reload_and_wait(sw, path, "mac_dump_file = %s\n" % good), "reload with another mac_dump_file")
    check(dump_and_wait(sw, good), "the next SIGUSR1 uses the new file name")
    check(os.path.exists(good), "the new dump file exists")
    sw.stop()

    # default name, in the working directory of the switch
    wd = tempfile.mkdtemp(prefix="vswcwd.")
    port = ts.free_port()
    logf = os.path.join(wd, "log")
    env = dict(os.environ, VSWITCH_LOG_FILE=logf)
    p = subprocess.Popen([os.path.join(ROOT, "switch"), str(port), "2"], cwd=wd, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check(tl.wait_text(logf, "listening on TCP port", 10), "switch (no configuration file) started in its own directory")
    p.send_signal(signal.SIGUSR1)
    check(tl.wait_text(logf, "MAC table dumped to 'mac_dump.txt' (0 entries)", 10), "the default file name is mac_dump.txt")
    check(os.path.exists(os.path.join(wd, "mac_dump.txt")), "mac_dump.txt was created in the working directory")
    hdr, rows, bad = read_dump(os.path.join(wd, "mac_dump.txt"))
    check(len(hdr) == 2 and rows == [] and bad == [], "an empty table: just the two header lines")
    p.send_signal(signal.SIGINT)
    check(p.wait(10) == 0, "clean exit")
    subprocess.run(["rm", "-rf", wd])


def test_sigusr1_under_traffic():
    print("9. SIGUSR1 dumps while a flood goes through: every dump is whole and consistent")
    dump = os.path.join(TMP, "flooddump.txt")
    sw = ts.Switch(4, extra=["-c", conf("mac_dump_file = %s\n" % dump)])
    res = {}

    def run():
        try:
            res["r"] = tl.flood(sw.port, int(os.environ.get("CFG_FLOOD", "60000")))
        except Exception as e:
            res["e"] = repr(e)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(0.5)
    dumps = bad_dumps = 0
    seen_rows = []
    end = time.time() + 40
    while th.is_alive() and time.time() < end:
        if dump_and_wait(sw, dump, 20):
            dumps += 1
            hdr, rows, bad = read_dump(dump)
            keys = [(v, m) for v, m, _, _ in rows]
            if bad or len(hdr) != 2 or len(keys) != len(set(keys)):
                bad_dumps += 1
            seen_rows.append(rows)
    th.join(60)
    check("r" in res and res["r"][0] is not None, "the flood went through the switch while it was being dumped (%s)" % str(res.get("r") or res.get("e")))
    check(dumps >= 3, "several dumps were made during the flood (%d)" % dumps)
    check(bad_dumps == 0, "every dump was whole: header, 'vlan mac port age_s' lines, no entry twice (%d bad of %d)" % (bad_dumps, dumps))
    # the flood has 3 known sources (B, M, and the raw sender S): every dump that has all of them has the right ports
    full = [r for r in seen_rows if len(r) == 3]
    check(len(full) >= 1, "at least one dump saw all three entries (%d of %d)" % (len(full), len(seen_rows)))
    for rows in full:
        d = {m: (v, p) for v, m, p, _ in rows}
        check(d.get(ME[1], (0, 0))[0] == 1 and d.get(ME[2], (0, 0))[0] == 1 and d.get(ME[3], (0, 0))[0] == 1,
              "all entries are in vlan 1")
        break
    # while flooding S's entry is refreshed all the time: its age stays small
    s_ages = [age for rows in seen_rows for v, m, p, age in rows if m == ME[3]]
    check(not s_ages or max(s_ages) <= 3, "the flooding source's entry is fresh (age <= 3 s): %s" % sorted(set(s_ages)))
    check(sw.p.poll() is None, "the switch is alive after %d dumps under traffic" % dumps)
    log = sw.log()
    good, bad = tl.parse(log)
    check(not bad and "[ERR]" not in log, "the log has only whole lines and no error")
    sw.p.send_signal(signal.SIGINT)
    check(sw.p.wait(30) == 0, "clean exit (no deadlock)")
    sw.stop()


def test_valgrind():
    print("10. valgrind: configuration, SIGHUP (good and bad), SIGUSR1, traffic, SIGINT: no errors, no leaks, no descriptor left open")
    dump = os.path.join(TMP, "vg_dump.txt")
    path = conf("mac_aging_seconds = 60\nmac_dump_file = %s\n" % dump)
    sw = ts.Switch(4, extra=["-c", path])
    a, b, c = hosts_abc(sw)
    a.say("send bcast a-hello")
    check(reload_and_wait(sw, path, "mac_aging_seconds = 61\nmac_dump_file = %s\nport.2.vlan = 9\nmac_table_size = 99\n" % dump, timeout=60),
          "a valid reload")
    n = sw.count("reload failed")
    write_conf(path, "oops\n")
    hup(sw)
    check(sw.wait_log("reload failed", 60, n + 1), "an invalid reload")
    check(dump_and_wait(sw, dump, 60), "a dump")
    hdr, rows, bad = read_dump(dump)
    check(len(hdr) == 2 and not bad and len(rows) >= 2, "the dump is correct (%d entries)" % len(rows))
    quit_all(a, b, c)
    sw.p.send_signal(signal.SIGINT)
    rc = sw.p.wait(90)
    check(rc == 0, "exit status 0 under valgrind (got %s)" % rc)
    log = sw.log()
    check("shutdown: complete" in open(sw.logf.name).read(), "clean shutdown")
    check("ERROR SUMMARY: 0 errors from 0 contexts" in log, "valgrind: 0 errors")
    check("in use at exit: 0 bytes in 0 blocks" in log, "valgrind: 0 bytes in use at exit")
    check(re.search(r"(definitely|indirectly|possibly) lost: [1-9]", log) is None, "valgrind: nothing lost")
    fds = [f for f in re.findall(r"Open (?:AF_INET socket|file descriptor) (\d+)", log) if int(f) > 2]
    check(not fds, "valgrind: no descriptor left open at exit (config, dump and log files closed) %s" % fds)
    if "ERROR SUMMARY: 0 errors" not in log:
        print(log[-3000:])
    sw.stop()


# ----------------------------------------------------------------------------------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    try:
        if mode == "all":
            for t in (test_startup, test_sighup_aging, test_sighup_invalid, test_sighup_startup_only, test_sighup_no_file,
                      test_sighup_under_traffic, test_sigusr1_dump, test_sigusr1_errors_and_default_name,
                      test_sigusr1_under_traffic):
                t()
        elif mode == "tsan":
            ts.SWITCH = os.environ.get("SWITCH") or "setarch -R ./switch_tsan"
            os.environ.setdefault("CFG_FLOOD", "30000")
            for t in (test_sighup_invalid, test_sighup_under_traffic, test_sigusr1_dump, test_sigusr1_under_traffic):
                t()
        elif mode == "valgrind":
            ts.SWITCH = os.environ.get("SWITCH") or (
                "valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all "
                "--error-exitcode=1 --track-fds=yes ./switch")
            test_valgrind()
        else:
            print("usage: test_config_signals.py [all|tsan|valgrind]")
            sys.exit(2)
    finally:
        subprocess.run(["rm", "-rf", TMP])
    print("%d checks passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
