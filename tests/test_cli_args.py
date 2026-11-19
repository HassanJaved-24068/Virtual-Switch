#!/usr/bin/env python3
"""
Command-line argument validation (the fix of the independent verification case CLI-01).

  python3 tests/test_cli_args.py [tsan|valgrind]

strtol() accepts leading white space and a '+' / '-' sign, so "+9000" and " 9000" used to be taken for a TCP port. Every numeric argument is now parsed by parse_uint()
(util.c, unit-tested by build/test_parse): only the characters 0-9. This test starts the real programs:

  1. ./switch <tcp_port> <num_ports>: a table of good and bad values for the TCP port; a bad one must end with exit status 1, a message on stderr, no listening socket and
     no process left; a good one must listen and end with status 0 on SIGINT (the original CLI-01 cases are kept and extended: the full list of the independent suite is in
     verification/t_cfg.py);
  2. the same for the number of ports;
  3. the same for the peer port of --pair (and: a bad peer port must not start the first switch either);
  4. the other programs that parse a TCP port: supervisor, host, swctl -p (and bench / bench_mac when built);
  5. the other numeric arguments: host --flood, the vlan and priority of the host command sendx, the port numbers of the swctl commands (admin socket of a live switch).
"""
import os, re, shlex, signal, socket, struct, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
VARIANT = sys.argv[1] if len(sys.argv) > 1 else "normal"
if VARIANT == "tsan":
    SWITCH = shlex.split(os.environ.get("SWITCH", "setarch -R ./switch_tsan"))
    HOST = shlex.split(os.environ.get("HOST", "setarch -R ./host_tsan"))
elif VARIANT == "valgrind":
    SWITCH = shlex.split(os.environ.get("SWITCH", "valgrind -q --error-exitcode=99 --leak-check=full ./switch"))
    HOST = shlex.split("./host")
else:
    SWITCH = shlex.split(os.environ.get("SWITCH", "./switch"))
    HOST = shlex.split(os.environ.get("HOST", "./host"))
TMP = tempfile.mkdtemp(prefix="vswcli.")
REJECT_WAIT = int(os.environ.get("CLI_REJECT_WAIT", "60"))      # how long a REFUSED command may take to end before it counts as accepted (shortened to check the test against the old code)
LOGSEQ = [0]
passed = failed = 0


def check(cond, what):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", what)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def listening(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def kill_group(p):
    """Kill the process and its children (a wrongly accepted supervisor / --pair command leaves a child that keeps the pipes open)."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        p.kill()


def logfile():
    LOGSEQ[0] += 1
    return os.path.join(TMP, "sw%d.log" % LOGSEQ[0])


def run_switch(args, expect_ok, ports_to_watch):
    """Run ./switch with args. expect_ok: it must listen on ports_to_watch[0] (and end with 0 on SIGINT); else: exit status 1, a message, nothing listens."""
    log = logfile()
    p = subprocess.Popen(SWITCH + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ, VSWITCH_LOG_FILE=log),
                         start_new_session=True)
    what = "switch %r" % (args,)
    if expect_ok:
        end = time.time() + 90
        while time.time() < end and p.poll() is None and not (os.path.exists(log) and "listening on TCP port" in open(log).read()):
            time.sleep(0.05)
        check(p.poll() is None and listening(ports_to_watch[0]), what + ": accepted, listening on %d" % ports_to_watch[0])
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
        try:
            out, err = p.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            kill_group(p)
            out, err = p.communicate()
        check(p.returncode == 0, what + ": exit status 0 on SIGINT (got %s)" % p.returncode)
    else:
        try:
            out, err = p.communicate(timeout=REJECT_WAIT)
        except subprocess.TimeoutExpired:
            kill_group(p)
            out, err = p.communicate()
            check(False, what + ": was ACCEPTED (still running after %d s)" % REJECT_WAIT)
            return
        check(p.returncode == 1, what + ": exit status 1 (got %s)" % p.returncode)
        check(err.strip() != "", what + ": a message on stderr")
        check(not any(listening(x) for x in ports_to_watch), what + ": nothing listens")


def sweep_clean(what, ports):
    """No switch started by THIS test is left: no process whose command line names one of its TCP ports (other switches on the machine are not our business)."""
    left = []
    for port in ports:
        out = subprocess.run(["pgrep", "-f", r"switch[a-z_]* %d( |$)" % port], capture_output=True, text=True).stdout.split()
        left += [x for x in out if x != str(os.getpid())]
    check(left == [], "%s: no switch process left (%s)" % (what, left))


# ---------------------------------------------------------------------------
TCP_BAD = ["+{p}", "-{p}", " {p}", "{p} ", "\t{p}", "{p}abc", "abc", "", "0", "00", "65536", "-1", "+0", "{p}.0", "0x{p:x}", "{p}\n", "99999999999999999999", "99999999999999999999999999999999999",
           "18446744073709551616", "4294967296", "4294967297"]


def test_tcp_port():
    print("1. ./switch <tcp_port> <num_ports>: the TCP port")
    for tpl in TCP_BAD:
        p = free_port()
        a = tpl.format(p=p)
        run_switch([a, "4"], False, [p])
    p = free_port()
    run_switch([str(p), "4"], True, [p])                       # 9000
    p = free_port()
    run_switch(["0%d" % p, "4"], True, [p])                    # 09000: digits, in range
    p = free_port()
    run_switch(["000000000000000000000%d" % p, "4"], True, [p])    # many leading zeros: not an overflow
    run_switch(["65535", "4"], True, [65535])                  # the maximum


def test_num_ports():
    print("2. the number of ports")
    p = free_port()
    for a in ("1", "64"):
        run_switch([str(p), a], True, [p])
    for a in ("0", "65", "+4", "-4", " 4", "4 ", "\t4", "4x", "x4", "abc", "", "4.0", "99999999999999999999", "18446744073709551620", "4294967300", "-0", "+64", "064x"):
        p = free_port()
        run_switch([str(p), a], False, [p])
    p = free_port()
    run_switch([str(p), "04"], True, [p])                      # leading zero: digits, in range


def test_peer_port():
    print("3. the peer port of --pair")
    p, q = free_port(), free_port()
    run_switch([str(p), "4", "--pair", str(q)], True, [p, q])
    used = []
    for tpl in ("+{q}", "-{q}", " {q}", "{q} ", "0", "00", "65536", "abc", "{q}abc", "", "99999999999999999999", "18446744073709551616", "4294967296", "-1", "+0"):
        p, q = free_port(), free_port()
        run_switch([str(p), "4", "--pair", tpl.format(q=q)], False, [p, q])     # neither switch may start
        used += [p, q]
    p = free_port()
    run_switch([str(p), "4", "--pair", str(p)], False, [p])    # the same TCP port twice: refused by an earlier check or by the second bind, but it must not run
    sweep_clean("--pair", used + [p])


def run_plain(cmd, stdin=None, timeout=20):
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def test_other_programs():
    print("4. supervisor, host, swctl -p: TCP port arguments")
    p = free_port()
    for tpl in ("+{p}", " {p}", "{p} ", "-{p}", "0", "65536", "{p}x", "", "99999999999999999999"):
        a = tpl.format(p=p)
        sp = subprocess.Popen(["./supervisor", "./switch", a, "4"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=dict(os.environ, VSWITCH_LOG_FILE=logfile()), start_new_session=True)
        try:
            out, err = sp.communicate(timeout=REJECT_WAIT)
            check(sp.returncode == 1 and err.strip() != "" and not listening(p), "supervisor %r: exit status 1, a message, nothing listens (rc %s)" % (a, sp.returncode))
        except subprocess.TimeoutExpired:
            kill_group(sp)
            sp.communicate()
            check(False, "supervisor %r: ACCEPTED" % a)
    for tpl in ("+{p}", " {p}", "{p} ", "-{p}", "0", "65536", "{p}x", "", "99999999999999999999"):
        a = tpl.format(p=p)
        rc, out, err = run_plain(HOST + ["127.0.0.1", a, "aa:bb:cc:dd:ee:01"])
        check(rc == 1 and "bad port" in err, "host %r: exit status 1 'bad port' (rc %s: %s)" % (a, rc, err.strip()[:60]))
        rc, out, err = run_plain(["./swctl", "-p", a, "show", "ports"])
        check(rc == 1 and "bad TCP port" in err, "swctl -p %r: exit status 1 'bad TCP port' (rc %s: %s)" % (a, rc, err.strip()[:60]))
    # a valid port that nobody listens on is NOT an argument error: connect refused (host: 1), switch not running (swctl: 2)
    rc, out, err = run_plain(HOST + ["127.0.0.1", str(p), "aa:bb:cc:dd:ee:01"])
    check(rc == 1 and "bad port" not in err and "connect" in err, "host with a good port that nobody listens on: a connect error (rc %s: %s)" % (rc, err.strip()[:60]))
    rc, out, err = run_plain(["./swctl", "-p", "0%d" % p, "show", "ports"])
    check(rc == 2 and "not running" in err, "swctl -p 0%d (digits, in range): the argument is fine, the switch is not running (rc %s: %s)" % (p, rc, err.strip()[:60]))
    # the benchmark programs, when they are built: bench <ip> <port> <hosts> <seconds> <payload> [seed]; bench_mac_<variant> <threads> <seconds> [seed]
    if os.access("bench/bench", os.X_OK):
        base = ["127.0.0.1", str(p), "2", "1", "10"]
        cases = [(1, "+%d" % p), (1, " %d" % p), (1, "0"), (1, "65536"), (2, "+2"), (2, " 2"), (2, "1"), (2, "65"), (3, "+1"), (3, "1 "), (3, "nan"), (3, "inf"), (3, "-1"), (3, "0"),
                 (3, "1e2x"), (3, "99999999"), (4, "+10"), (4, " 10"), (4, "-10"), (4, "1501"), (4, "x"), (4, "99999999999999999999999")]
        for idx, val in cases:
            a = list(base)
            a[idx] = val
            rc, out, err = run_plain(["bench/bench"] + a, timeout=30)
            check(rc == 1 and err.strip() != "", "bench %r: exit status 1 with a message (rc %s)" % (a, rc))
        for val in ("+7", " 7", "-7", "x", "99999999999999999999999"):
            rc, out, err = run_plain(["bench/bench"] + base + [val], timeout=30)
            check(rc == 1 and "bad seed" in err, "bench seed %r: exit status 1 (rc %s)" % (val, rc))
    if os.access("bench/bench_mac_striped", os.X_OK):
        for a in (["+1", "1"], [" 1", "1"], ["-1", "1"], ["0", "1"], ["257", "1"], ["99999999999999999999", "1"], ["1", "+1"], ["1", " 1"], ["1", "nan"], ["1", "inf"], ["1", "0"],
                  ["1", "-1"], ["1", "1", "+7"], ["1", "1", " 7"], ["1", "1", "-7"], ["1", "1", "x"], ["1", "1", "99999999999999999999999"]):
            rc, out, err = run_plain(["bench/bench_mac_striped"] + a, timeout=30)
            check(rc == 1 and err.strip() != "", "bench_mac %r: exit status 1 with a message (rc %s)" % (a, rc))
        rc, out, err = run_plain(["bench/bench_mac_striped", "01", "0.2", "007"], timeout=30)
        check(rc == 0 and "threads=1" in out, "bench_mac with digits and leading zeros is accepted (rc %s: %s)" % (rc, (out + err).strip()[:70]))


def test_other_numbers():
    print("5. host --flood / sendx vlan and priority, swctl port numbers against a live switch")
    p = free_port()
    for a in ("+3", " 3", "3 ", "-3", "x", "", "99999999999999999999", "1000000001"):
        rc, out, err = run_plain(HOST + ["127.0.0.1", str(p), "aa:bb:cc:dd:ee:01", "--flood", a, "--to", "aa:bb:cc:dd:ee:02"])
        check(rc == 1 and "--flood needs a number" in err, "host --flood %r: exit status 1 (rc %s: %s)" % (a, rc, err.strip()[:60]))
    # sendx against a fake switch: only the frames with a good vlan / priority arrive
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    h = subprocess.Popen(HOST + ["127.0.0.1", str(port), "aa:bb:cc:dd:ee:01"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    srv.settimeout(20)
    conn, _ = srv.accept()
    conn.settimeout(3)
    bad = ["sendx bcast +5 1 hi", "sendx bcast  5x 1 hi", "sendx bcast -5 1 hi", "sendx bcast 5 +1 hi", "sendx bcast 5 -1 hi", "sendx bcast 5 3 hi", "sendx bcast 0 1 hi",
           "sendx bcast 4095 1 hi", "sendx bcast 99999999999999999999 1 hi", "sendx bcast 5 99999999999999999999 hi", "sendx bcast 5 x hi", "sendx bcast x 1 hi"]
    for line in bad:
        h.stdin.write((line + "\n").encode())
    h.stdin.write(b"sendx bcast 05 02 ok\n")             # good: digits, leading zero, in range -> exactly ONE frame arrives
    h.stdin.flush()
    got = b""
    try:
        while len(got) < 17 + 2:
            chunk = conn.recv(100)
            if not chunk:
                break
            got += chunk
    except socket.timeout:
        pass
    check(len(got) == 19, "sendx: exactly one frame arrived (%d bytes), the %d bad commands sent nothing" % (len(got), len(bad)))
    if len(got) >= 17:
        ln, = struct.unpack("!H", got[:2])
        vlan, = struct.unpack("!H", got[14:16])
        check(ln == 2 and vlan == 5 and got[16] == 2 and got[17:19] == b"ok", "the good sendx has vlan 5, priority 2, text 'ok' (len %d vlan %d prio %d)" % (ln, vlan, got[16]))
    h.stdin.write(b"quit\n")
    h.stdin.close()
    try:
        h.wait(timeout=20)
    except subprocess.TimeoutExpired:
        h.kill()
        h.wait()
    err = h.stderr.read()
    h.stderr.close()
    h.stdout.close()
    check(err.decode().count("usage: sendx") == len(bad), "every bad sendx printed the usage message (%d of %d)" % (err.decode().count("usage: sendx"), len(bad)))
    conn.close()
    srv.close()
    # swctl against a live switch: the port numbers are parsed by the admin thread (digits only); the answer must be ERR and the switch must go on
    log = logfile()
    q = free_port()
    sw = subprocess.Popen(SWITCH + [str(q), "4"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ, VSWITCH_LOG_FILE=log))
    try:
        end = time.time() + 90
        while time.time() < end and "listening on TCP port" not in (open(log).read() if os.path.exists(log) else ""):
            time.sleep(0.05)
        for cmd in (["shutdown", "+1"], ["shutdown", " 1"], ["shutdown", "1x"], ["shutdown", "-1"], ["shutdown", "99999999999999999999"], ["up", "+1"], ["vlan", "+1", "5"],
                    ["vlan", "1", "+5"], ["vlan", "1", " 5"], ["vlan", "1", "-5"], ["vlan", "1", "99999999999999999999"], ["mirror", "+1", "2"], ["mirror", "1", "+2"]):
            rc, out, err = run_plain(["./swctl", "-p", str(q)] + cmd)
            check(rc == 1, "swctl %r against a live switch: ERR, exit status 1 (rc %s: %s)" % (cmd, rc, (out + err).strip()[:70]))
        rc, out, err = run_plain(["./swctl", "-p", str(q), "vlan", "1", "05"])
        check(rc == 0, "swctl vlan 1 05: digits, in range: OK (rc %s: %s)" % (rc, (out + err).strip()[:70]))
        rc, out, err = run_plain(["./swctl", "-p", "0%d" % q, "show", "ports"])
        check(rc == 0, "swctl -p 0%d show ports works (rc %s)" % (q, rc))
    finally:
        sw.send_signal(signal.SIGINT)
        try:
            check(sw.wait(60) == 0, "the live switch still ends with status 0 after the bad commands")
        except subprocess.TimeoutExpired:
            sw.kill()
            sw.wait()
            check(False, "the live switch did not end")


def test_config_values():
    print("6. numeric values of the configuration file (-c): same strictness, \"-0\" included")
    for line in ("rate_limit = -0", "trunk_port = -0", "rate_limit = +5", "rate_limit = -5", "port_wait_timeout = -0", "port.1.rate = -0", "port.1.vlan = +5", "queue_capacity = 0x10",
                 "mac_aging_seconds = 99999999999999999999", "rate_limit = 5x", "rate_limit ="):
        p = free_port()
        cf = os.path.join(TMP, "c.conf")
        with open(cf, "w") as f:
            f.write(line + "\n")
        run_switch([str(p), "4", "-c", cf], False, [p])
    for line in ("rate_limit = 05", "rate_limit =   7   ", "trunk_port = 0"):
        p = free_port()
        cf = os.path.join(TMP, "c.conf")
        with open(cf, "w") as f:
            f.write(line + "\n")
        run_switch([str(p), "4", "-c", cf], True, [p])


if __name__ == "__main__":
    try:
        for t in (test_tcp_port, test_num_ports, test_peer_port, test_other_programs, test_other_numbers, test_config_values):
            try:
                t()
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(False, "%s raised %r" % (t.__name__, e))
    finally:
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)
    print("cli args [%s]: %d checks passed, %d failed" % (VARIANT, passed, failed))
    sys.exit(1 if failed else 0)
