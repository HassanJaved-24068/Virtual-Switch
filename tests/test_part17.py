#!/usr/bin/env python3
"""
TEMPORARY dedicated test file for Part 17 (port mirroring). It is deleted when Part 18 starts (tests/test_part18.py replaces it).

It runs ONLY the Part 17 tests, which live permanently in the main suite (tests/run_unit.sh -> make test):
  tests/test_mirror.c   forward_frame and admin_mirror without sockets: validation, both directions, the SAME frame (no copy), VLAN ignored, never back to the
                        ingress port, a full sniffer queue drops on the sniffer, a sniffer that is not UP, two mirrored ports, 3 threads while the mirror is toggled
  tests/test_mirror.py  the real switch: what a sniffer receives, error replies, mirroring ends when either host goes away, a sniffer that never reads
                        (accounting identity), the real ./host --sniff, SIGINT with a stuck sniffer, a storm of operations, valgrind
so the dedicated and the permanent coverage are the same tests by construction.

  python3 tests/test_part17.py            normal + ThreadSanitizer + valgrind
  python3 tests/test_part17.py quick      normal only
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


ok = run("build", ["make", "-s", "all", "switch_small", "switch_tsan", "host_tsan", "switch_small_tsan", "build/test_mirror", "build/test_mirror_tsan"])
if ok:
    run("unit: mirroring", ["./build/test_mirror"])
    run("end-to-end: real switch", ["python3", "tests/test_mirror.py"])
    if not quick:
        run("unit: mirroring, TSan", NOASLR + ["./build/test_mirror_tsan"])
        run("end-to-end: TSan", ["python3", "tests/test_mirror.py", "tsan"], {"SWITCH": " ".join(NOASLR + ["./switch_tsan"])})
        if shutil.which("valgrind"):
            run("unit: mirroring, valgrind", VG + ["./build/test_mirror"])
            run("end-to-end: valgrind", ["python3", "tests/test_mirror.py", "valgrind"])
        else:
            print("valgrind not installed: skipping the valgrind tests")

print("\nPart 17 dedicated tests:")
for name, good, dt in results:
    print("  %-30s %s  (%.0f s)" % (name, "PASS" if good else "FAIL", dt))
bad = [n for n, g, _ in results if not g]
print("RESULT: %s" % ("ALL PASSED" if not bad and results else "FAILED: " + ", ".join(bad or ["build"])))
sys.exit(0 if not bad and results else 1)
