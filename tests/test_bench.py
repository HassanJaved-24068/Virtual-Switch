#!/usr/bin/env python3
"""
Part 21 tests: the benchmark (global lock vs striped locks vs rwlock in the MAC table).

  python3 tests/test_bench.py

  1. the three switches (switch_global, switch_striped, switch_rwlock: the same sources, only mactable.c is built with -DMAC_LOCK=GLOBAL | STRIPED | RWLOCK) and the benchmark
     programs exist;
  2. EVERY variant passes tests/forward.sh (the roadmap's completion criterion): the script runs unchanged in a scratch directory whose ./switch is the variant;
  3. bench/bench_mac_<variant> runs (T threads, 90/10 lookup/learn, the table is checked for consistency at the end and every lookup answer is checked);
  4. bench/bench against a real switch of every variant: frames are received, the switch ends with status 0 and no [ERR] line;
  5. bad arguments are refused (exit status 1);
  6. bench/run.sh --quick writes the raw CSV files, results.csv, throughput.png and a one-page report.pdf; plot.py gives byte-identical output for identical input (the
     numbers of the report are a function of the CSV files only);
  7. --persist-mac is refused by the variants that do not support it (the default build is tested by tests/test_persist.py).

The unit tests of the two other locking strategies (tests/test_mactable.c built against them, 8-thread stress included) are build/test_mactable_global and
build/test_mactable_rwlock (and _tsan), run by tests/run_unit.sh.
"""
import os, re, shutil, signal, socket, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
VARIANTS = ["global", "striped", "rwlock"]
passed = failed = 0
TMP = tempfile.mkdtemp(prefix="vswbench.")


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


def test_files():
    print("1. the variants and the benchmark programs exist")
    for v in VARIANTS:
        for f in ("switch_%s" % v, "bench/bench_mac_%s" % v):
            check(os.access(f, os.X_OK), "%s is built" % f)
    check(os.access("bench/bench", os.X_OK), "bench/bench is built")
    for f in ("bench/run.sh", "bench/plot.py", "bench/bench.c", "bench/bench_mac.c"):
        check(os.path.exists(f), "%s exists" % f)


def test_forward_sh():
    print("2. every variant passes tests/forward.sh")
    for v in VARIANTS:
        work = os.path.join(TMP, "fwd_" + v)
        os.makedirs(work)
        for name, target in (("switch", "switch_" + v), ("switch_small", "switch_small"), ("switch_small_tsan", "switch_small_tsan"), ("switch_tsan", "switch_tsan"),
                             ("host", "host"), ("host_tsan", "host_tsan"), ("swctl", "swctl")):
            if os.path.exists(os.path.join(ROOT, target)):
                os.symlink(os.path.join(ROOT, target), os.path.join(work, name))
        r = subprocess.run([os.path.join(ROOT, "tests", "forward.sh")], cwd=work, capture_output=True, text=True, timeout=600)
        m = re.search(r"forward \[normal\]: (\d+) checks passed, (\d+) failed", r.stdout)
        check(r.returncode == 0 and m is not None and m.group(2) == "0" and int(m.group(1)) > 0,
              "forward.sh with switch_%s: %s" % (v, m.group(0) if m else (r.stdout[-300:] + r.stderr[-300:])))


def test_bench_mac():
    print("3. the in-process micro-benchmark")
    for v in VARIANTS:
        r = subprocess.run(["bench/bench_mac_" + v, "4", "1", "7"], capture_output=True, text=True, timeout=60)
        m = re.search(r"bench_mac variant=(\w+) threads=4 seconds=([\d.]+) ops=(\d+) ops_per_sec=(\d+)", r.stdout)
        check(r.returncode == 0 and m is not None and m.group(1) == v and int(m.group(3)) > 1000, "bench_mac_%s: %s" % (v, r.stdout.strip() or r.stderr.strip()))
    r = subprocess.run(["bench/bench_mac_striped", "1", "1", "7"], capture_output=True, text=True, timeout=60)
    check(r.returncode == 0 and "threads=1" in r.stdout, "also with one thread")


def test_bench_net():
    print("4. the load generator against a real switch of every variant")
    for v in VARIANTS:
        port = free_port()
        log = os.path.join(TMP, "sw_%s.log" % v)
        sw = subprocess.Popen(["./switch_" + v, str(port), "8"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=dict(os.environ, VSWITCH_LOG_FILE=log))
        try:
            end = time.time() + 30
            while time.time() < end and "listening on TCP port %d" % port not in (open(log).read() if os.path.exists(log) else ""):
                time.sleep(0.05)
            r = subprocess.run(["bench/bench", "127.0.0.1", str(port), "4", "2", "100", "7"], capture_output=True, text=True, timeout=120)
            m = re.search(r"bench hosts=4 seconds=([\d.]+) payload=100 sent=(\d+) received=(\d+) frames_per_sec=(\d+)", r.stdout)
            check(r.returncode == 0 and m is not None and int(m.group(3)) > 100, "bench against switch_%s: %s" % (v, r.stdout.strip() or r.stderr.strip()))
            check(m is not None and int(m.group(3)) <= int(m.group(2)), "no more frames received than sent (%s)" % (m.groups() if m else None,))
            check("warm-up timeout" not in r.stderr, "the warm-up completed (every MAC learned before the measurement)")
        finally:
            sw.send_signal(signal.SIGINT)
            try:
                rc = sw.wait(30)
            except subprocess.TimeoutExpired:
                sw.kill()
                rc = sw.wait()
        text = open(log).read()
        check(rc == 0, "switch_%s exited with status 0 (got %s)" % (v, rc))
        check("[ERR]" not in text, "no [ERR] line in the log of switch_%s" % v)


def test_bad_arguments():
    print("5. bad arguments")
    for name, cmd in (("bench: one host", ["bench/bench", "127.0.0.1", "9", "1", "1", "10"]),
                      ("bench: payload too big", ["bench/bench", "127.0.0.1", "9", "2", "1", "1501"]),
                      ("bench: bad IP", ["bench/bench", "999.1.1.1", "9", "2", "1", "10"]),
                      ("bench: too few arguments", ["bench/bench", "127.0.0.1"]),
                      ("bench: nobody listens", ["bench/bench", "127.0.0.1", "1", "2", "1", "10"]),
                      ("bench_mac: 0 threads", ["bench/bench_mac_striped", "0", "1"]),
                      ("bench_mac: bad seconds", ["bench/bench_mac_striped", "2", "-1"]),
                      ("bench_mac: no arguments", ["bench/bench_mac_striped"])):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        check(r.returncode == 1 and r.stderr.strip() != "", "%s: exit status 1 with a message (got %s)" % (name, r.returncode))


def test_run_and_plot():
    print("6. bench/run.sh --quick, and the plot / report from the CSV files")
    out = os.path.join(TMP, "out")
    r = subprocess.run(["bash", "bench/run.sh", "--quick"], capture_output=True, text=True, timeout=600, env=dict(os.environ, OUT=out))
    check(r.returncode == 0, "run.sh --quick succeeded (%s)" % (r.stderr[-300:] or r.stdout[-200:]))
    for f in ("raw_mac.csv", "raw_net.csv", "machine.txt", "results.csv", "throughput.png", "report.pdf"):
        check(os.path.exists(os.path.join(out, f)) and os.path.getsize(os.path.join(out, f)) > 0, "%s was written" % f)
    if not os.path.exists(os.path.join(out, "report.pdf")):
        return
    check(open(os.path.join(out, "report.pdf"), "rb").read(5) == b"%PDF-", "report.pdf is a PDF")
    check(open(os.path.join(out, "throughput.png"), "rb").read(8) == b"\x89PNG\r\n\x1a\n", "throughput.png is a PNG")
    if shutil.which("pdfinfo"):
        info = subprocess.run(["pdfinfo", os.path.join(out, "report.pdf")], capture_output=True, text=True).stdout
        check(re.search(r"^Pages:\s+1$", info, re.M) is not None, "the report is ONE page")
    rows = open(os.path.join(out, "raw_mac.csv")).read().strip().splitlines()
    check(len(rows) == 1 + 3 * 2, "raw_mac.csv: 3 variants x 2 thread counts (%d lines)" % len(rows))
    check({l.split(",")[0] for l in rows[1:]} == set(VARIANTS), "all three variants are in the data")
    first = {f: open(os.path.join(out, f), "rb").read() for f in ("results.csv", "throughput.png", "report.pdf")}
    r = subprocess.run([sys.executable, "-I", "bench/plot.py", out], capture_output=True, text=True, timeout=120)
    check(r.returncode == 0, "plot.py runs again on the same data")
    again = {f: open(os.path.join(out, f), "rb").read() for f in first}
    check(first["results.csv"] == again["results.csv"] and first["throughput.png"] == again["throughput.png"],
          "the same CSV files give the same results.csv and the same picture")


def test_persist_refused():
    print("7. --persist-mac is refused by the variants that cannot recover (striped, the default build, is covered by tests/test_persist.py)")
    for v in ("global", "rwlock"):
        port = free_port()
        r = subprocess.run(["./switch_" + v, str(port), "4", "--persist-mac"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, VSWITCH_LOG_FILE=os.path.join(TMP, "p_%s.log" % v)))
        check(r.returncode == 1 and "shared MAC table" in r.stderr, "switch_%s --persist-mac: exit status 1 with a message (got %s: %s)" % (v, r.returncode, r.stderr.strip()[:80]))
    for p in ("/dev/shm/vswitch_stats_P",):
        try:
            os.unlink(p)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        for t in (test_files, test_forward_sh, test_bench_mac, test_bench_net, test_bad_arguments, test_run_and_plot, test_persist_refused):
            try:
                t()
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(False, "%s raised %r" % (t.__name__, e))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("%d checks passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
