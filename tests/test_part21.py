#!/usr/bin/env python3
"""
TEMPORARY dedicated test file for Part 21 (benchmark). It is not needed after the part is committed; the permanent tests are in the main suite (tests/run_unit.sh -> make test):
tests/test_bench.py and the MAC table unit tests against the GLOBAL and RWLOCK strategies (build/test_mactable_global, build/test_mactable_rwlock, and _tsan).

  python3 tests/test_part21.py            everything, including ThreadSanitizer
  python3 tests/test_part21.py quick      without ThreadSanitizer
"""
import os, subprocess, sys, time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
quick = len(sys.argv) > 1 and sys.argv[1] == "quick"
results = []


def run(name, cmd):
    t = time.time()
    print("=== %s: %s" % (name, " ".join(cmd)), flush=True)
    p = subprocess.run(cmd)
    results.append((name, p.returncode == 0, time.time() - t))


run("build", ["make", "-s", "all", "tsan", "bench", "build/test_mactable_global", "build/test_mactable_rwlock", "build/test_mactable_global_tsan", "build/test_mactable_rwlock_tsan"])
run("static checks", ["python3", "tests/static_checks.py"])
run("mactable (default)", ["./build/test_mactable"])
run("mactable GLOBAL", ["./build/test_mactable_global"])
run("mactable RWLOCK", ["./build/test_mactable_rwlock"])
if not quick:
    # only the "destroy of a locked mutex" report is off (test_robust kills a thread holding a bucket mutex that these variants do not use; notes/bugs.md item 37)
    run("mactable GLOBAL TSan", ["env", "TSAN_OPTIONS=report_destroy_locked=0", "setarch", "-R", "./build/test_mactable_global_tsan"])
    run("mactable RWLOCK TSan", ["env", "TSAN_OPTIONS=report_destroy_locked=0", "setarch", "-R", "./build/test_mactable_rwlock_tsan"])
run("bench tests", ["python3", "tests/test_bench.py"])

print("\nPart 21 dedicated tests:")
for name, good, dt in results:
    print("  %-24s %s  (%.0f s)" % (name, "PASS" if good else "FAIL", dt))
bad = [n for n, g, _ in results if not g]
print("RESULT: %s" % ("ALL PASSED" if not bad else "FAILED: " + ", ".join(bad)))
sys.exit(0 if not bad else 1)
