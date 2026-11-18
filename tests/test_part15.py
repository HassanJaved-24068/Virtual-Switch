#!/usr/bin/env python3
"""
TEMPORARY dedicated test file for Part 15 (admin socket and swctl). It is deleted when Part 16 starts
(tests/test_part16.py replaces it).

It runs ONLY the Part 15 tests, which live permanently in the main suite (tests/run_unit.sh -> make test):
  tests/test_admin.c   admin_parse(): the eight commands, ranges, odd input, 256-byte limit, texts of the reasons, small error buffers
  tests/test_swctl.c   swctl stats against a segment: counters, the data mapping is read-only (a write gets SIGSEGV), the control mapping
                       is writable (mutex), kill -9 while the lock is held (EOWNERDEAD), waiting for the lock, no segment -> exit 2
  tests/test_admin.py  the real switch: socket path / mode / stale path / ss -xlp / removal, protocol (partial lines, 256 bytes, NUL, CRLF),
                       vanishing / silent / trickling clients, SIGINT with the admin thread idle, waiting, with silent clients, show mac /
                       show ports / clear mac (also under traffic, flooding after clear), validation and exit statuses 0/1/2, swctl stats,
                       stats --watch (1 s, whole seconds, SIGINT, /proc maps r--s), damaged / foreign segments, valgrind
so the dedicated and the permanent coverage are the same tests by construction.

  python3 tests/test_part15.py            normal + ThreadSanitizer + valgrind
  python3 tests/test_part15.py quick      normal only
"""
import os, shutil, subprocess, sys, time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOASLR = ["setarch", "-R"] if subprocess.run(["setarch", "-R", "true"], capture_output=True).returncode == 0 else []
VG = ["valgrind", "--leak-check=full", "--show-leak-kinds=all", "--errors-for-leak-kinds=all", "--error-exitcode=1", "--track-fds=yes"]
quick = len(sys.argv) > 1 and sys.argv[1] == "quick"
results = []


def run(name, cmd, env=None):
    t = time.time()
    print("=== %s: %s" % (name, " ".join(cmd)), flush=True)
    p = subprocess.run(cmd, env=dict(os.environ, **(env or {})))
    results.append((name, p.returncode == 0, time.time() - t))
    return p.returncode == 0


ok = run("build", ["make", "-s", "all", "switch_tsan", "host_tsan", "build/test_admin", "build/test_admin_tsan", "build/test_swctl"])
if ok:
    run("unit: command parser", ["./build/test_admin"])
    run("unit: swctl and the segment", ["./build/test_swctl"])
    run("end-to-end: real switch", ["python3", "tests/test_admin.py"])
    if not quick:
        run("unit: parser, TSan", NOASLR + ["./build/test_admin_tsan"])
        run("end-to-end: TSan", ["python3", "tests/test_admin.py", "tsan"], {"SWITCH": " ".join(NOASLR + ["./switch_tsan"])})
        if shutil.which("valgrind"):
            run("unit: parser, valgrind", VG + ["./build/test_admin"])
            run("end-to-end: valgrind", ["python3", "tests/test_admin.py", "valgrind"])
        else:
            print("valgrind not installed: skipping the valgrind tests")

print("\nPart 15 dedicated tests:")
for name, good, dt in results:
    print("  %-34s %s  (%.0f s)" % (name, "PASS" if good else "FAIL", dt))
bad = [n for n, g, _ in results if not g]
print("RESULT: %s" % ("ALL PASSED" if not bad and results else "FAILED: " + ", ".join(bad or ["build"])))
sys.exit(0 if not bad and results else 1)
