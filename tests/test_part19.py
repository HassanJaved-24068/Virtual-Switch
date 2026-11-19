#!/usr/bin/env python3
"""
TEMPORARY dedicated test file for Part 19 (two switches joined by a two-pipe trunk). It is deleted when Part 20 starts (tests/test_part20.py replaces it).

It runs ONLY the Part 19 additions, which live permanently in the main suite (tests/run_unit.sh -> make test): tests/test_pair.py in the normal, TSan and valgrind variants,
plus the static checks of Part 18 (no sleep(), every call result handled), which the new code must also satisfy.

  python3 tests/test_part19.py            normal + ThreadSanitizer + valgrind
  python3 tests/test_part19.py quick      normal only
"""
import os, shutil, subprocess, sys, time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
quick = len(sys.argv) > 1 and sys.argv[1] == "quick"
results = []


def run(name, cmd, env=None):
    t = time.time()
    print("=== %s: %s" % (name, " ".join(cmd)), flush=True)
    p = subprocess.run(cmd, env=dict(os.environ, **(env or {})))
    results.append((name, p.returncode == 0, time.time() - t))


run("build", ["make", "-s", "all", "tsan"])
run("static checks", ["python3", "tests/static_checks.py"])
run("normal", ["python3", "tests/test_pair.py"])
if not quick:
    run("TSan", ["python3", "tests/test_pair.py", "tsan"])
    if shutil.which("valgrind"):
        run("valgrind", ["python3", "tests/test_pair.py", "valgrind"])

print("\nPart 19 dedicated tests:")
for name, good, dt in results:
    print("  %-14s %s  (%.0f s)" % (name, "PASS" if good else "FAIL", dt))
bad = [n for n, g, _ in results if not g]
print("RESULT: %s" % ("ALL PASSED" if not bad else "FAILED: " + ", ".join(bad)))
sys.exit(0 if not bad else 1)
