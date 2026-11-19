#!/usr/bin/env python3
"""
TEMPORARY dedicated test file for Part 20 (supervisor and crash-surviving MAC table). It is not needed after the part is committed; the permanent tests are in the main suite
(tests/run_unit.sh -> make test): tests/test_macshm.c (normal, TSan, valgrind) and tests/test_persist.py (normal, TSan, valgrind), plus the Part 18 static checks.

  python3 tests/test_part20.py            normal + ThreadSanitizer + valgrind
  python3 tests/test_part20.py quick      normal only
"""
import os, shutil, subprocess, sys, time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
quick = len(sys.argv) > 1 and sys.argv[1] == "quick"
results = []


def run(name, cmd):
    t = time.time()
    print("=== %s: %s" % (name, " ".join(cmd)), flush=True)
    p = subprocess.run(cmd)
    results.append((name, p.returncode == 0, time.time() - t))


run("build", ["make", "-s", "all", "tsan", "build/test_macshm", "build/test_macshm_tsan"])
run("static checks", ["python3", "tests/static_checks.py"])
run("macshm", ["./build/test_macshm"])
run("persist", ["python3", "tests/test_persist.py"])
if not quick:
    run("macshm TSan", ["setarch", "-R", "./build/test_macshm_tsan"])
    run("persist TSan", ["python3", "tests/test_persist.py", "tsan"])
    if shutil.which("valgrind"):
        run("macshm valgrind", ["valgrind", "--leak-check=full", "--error-exitcode=1", "--track-fds=yes", "./build/test_macshm"])
        run("persist valgrind", ["python3", "tests/test_persist.py", "valgrind"])

print("\nPart 20 dedicated tests:")
for name, good, dt in results:
    print("  %-18s %s  (%.0f s)" % (name, "PASS" if good else "FAIL", dt))
bad = [n for n, g, _ in results if not g]
print("RESULT: %s" % ("ALL PASSED" if not bad else "FAILED: " + ", ".join(bad)))
sys.exit(0 if not bad else 1)
