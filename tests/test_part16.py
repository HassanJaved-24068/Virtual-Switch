#!/usr/bin/env python3
"""
TEMPORARY dedicated test file for Part 16 (live reconfiguration). It is deleted when Part 17 starts (tests/test_part17.py replaces it).

It runs ONLY the Part 16 tests, which live permanently in the main suite (tests/run_unit.sh / run_all.sh -> make test):
  tests/test_reconfig.py     the real switch: shutdown of FREE and UP ports, up, error replies, one winner, VLAN rules, clear mac, untouched ports, semaphore
                             invariant after a storm, SIGINT during admin commands, valgrind
  tests/reconfig_stress.sh   the roadmap's criterion: 500 random shutdown/up/vlan/clear-mac operations + SIGHUP and SIGUSR1 every 0.2 s under flooding hosts
so the dedicated and the permanent coverage are the same tests by construction.

  python3 tests/test_part16.py            normal + ThreadSanitizer + valgrind
  python3 tests/test_part16.py quick      normal only
"""
import os, shutil, subprocess, sys, time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOASLR = ["setarch", "-R"] if subprocess.run(["setarch", "-R", "true"], capture_output=True).returncode == 0 else []
quick = len(sys.argv) > 1 and sys.argv[1] == "quick"
results = []


def run(name, cmd, env=None):
    t = time.time()
    print("=== %s: %s" % (name, " ".join(cmd)), flush=True)
    p = subprocess.run(cmd, env=dict(os.environ, **(env or {})))
    results.append((name, p.returncode == 0, time.time() - t))
    return p.returncode == 0


ok = run("build", ["make", "-s", "all", "switch_small", "switch_tsan", "host_tsan", "switch_small_tsan"])
if ok:
    run("end-to-end: real switch", ["python3", "tests/test_reconfig.py"])
    run("stress: 500 operations", ["./tests/reconfig_stress.sh"])
    if not quick:
        run("end-to-end: TSan", ["python3", "tests/test_reconfig.py", "tsan"], {"SWITCH": " ".join(NOASLR + ["./switch_tsan"])})
        run("stress: TSan", ["./tests/reconfig_stress.sh"], {"VARIANT": "tsan"})
        if shutil.which("valgrind"):
            run("end-to-end: valgrind", ["python3", "tests/test_reconfig.py", "valgrind"])
            run("stress: valgrind", ["./tests/reconfig_stress.sh"], {"VARIANT": "valgrind"})
        else:
            print("valgrind not installed: skipping the valgrind tests")

print("\nPart 16 dedicated tests:")
for name, good, dt in results:
    print("  %-30s %s  (%.0f s)" % (name, "PASS" if good else "FAIL", dt))
bad = [n for n, g, _ in results if not g]
print("RESULT: %s" % ("ALL PASSED" if not bad and results else "FAILED: " + ", ".join(bad or ["build"])))
sys.exit(0 if not bad and results else 1)
