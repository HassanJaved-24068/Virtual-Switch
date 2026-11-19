#!/usr/bin/env python3
"""
TEMPORARY dedicated test file for Part 18 (Tier 2 checkpoint). It is deleted when Part 19 starts (tests/test_part19.py replaces it).

It runs ONLY the Part 18 additions, which live permanently in the main suite (tests/run_all.sh, tests/run_unit.sh -> make test):
  tests/static_checks.py   no sleep()/usleep()/nanosleep() in the switch; no statement ignores the result of a system or pthread call
  tests/vlan.sh qos.sh rate.sh mirror.sh signals.sh admin.sh   the Tier 2 features with real hosts, as integration scripts
  tests/idle.sh            an idle switch uses (almost) no CPU
  tests/hostile.sh         32 hosts: --noread, --partial, flooders killed with kill -9, hosts dying mid-frame; admin commands and signals throughout
each in the normal, TSan and valgrind variants (idle.sh is skipped under valgrind).

  python3 tests/test_part18.py            normal + ThreadSanitizer + valgrind
  python3 tests/test_part18.py quick      normal only
"""
import os, shutil, subprocess, sys, time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = ["vlan", "qos", "rate", "mirror", "signals", "admin", "idle", "hostile"]
quick = len(sys.argv) > 1 and sys.argv[1] == "quick"
results = []


def run(name, cmd, env=None):
    t = time.time()
    print("=== %s: %s" % (name, " ".join(cmd)), flush=True)
    p = subprocess.run(cmd, env=dict(os.environ, **(env or {})))
    results.append((name, p.returncode == 0, time.time() - t))
    return p.returncode == 0


run("static checks", ["python3", "tests/static_checks.py"])
run("normal: new scripts", ["./tests/run_all.sh"] + SCRIPTS, {"VARIANT": "normal"})
if not quick:
    run("TSan: new scripts", ["./tests/run_all.sh"] + SCRIPTS, {"VARIANT": "tsan"})
    if shutil.which("valgrind"):
        run("valgrind: new scripts", ["./tests/run_all.sh"] + SCRIPTS, {"VARIANT": "valgrind"})
    else:
        print("valgrind not installed: skipping the valgrind variant")

print("\nPart 18 dedicated tests:")
for name, good, dt in results:
    print("  %-24s %s  (%.0f s)" % (name, "PASS" if good else "FAIL", dt))
bad = [n for n, g, _ in results if not g]
print("RESULT: %s" % ("ALL PASSED" if not bad and results else "FAILED: " + ", ".join(bad or ["build"])))
sys.exit(0 if not bad and results else 1)
