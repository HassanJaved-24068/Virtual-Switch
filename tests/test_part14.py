#!/usr/bin/env python3
"""
TEMPORARY dedicated test file for Part 14 (counters in POSIX shared memory). It is deleted when Part 15 starts
(tests/test_part15.py replaces it).

It runs ONLY the Part 14 tests, which live permanently in the main suite (tests/run_unit.sh -> make test):
  tests/test_stats.c   src/stats.c without a switch, with real processes: name, mode 0600, size = PAGE + round_up(sizeof(stats_data_t), PAGE),
                       control area at offset 0 and data area at PAGE, magic/version and a reader that detects wrong ones, 8 threads and
                       4 other processes without lost increments, all four counters, state/VLAN/mirror fields seen from another process,
                       the robust process-shared mutex (a process killed with SIGKILL while it holds the lock; a waiter woken by another
                       process), stale segments (junk, crash, a crash while locked), the umask, unlink-first shutdown with surviving
                       mappings, an older switch not unlinking a newer one's segment, failure paths (bad arguments and names, a failing
                       ftruncate leaves nothing behind)
  tests/test_shm.py    the real switch: the segment in /dev/shm during the run and not after SIGINT/SIGTERM, counters = what the hosts
                       sent and received, dropped counter, state/VLAN follow connect / SIGHUP / disconnect, stale and SIGKILLed segments,
                       failed starts, SIGINT under traffic, valgrind
so the dedicated and the permanent coverage are the same tests by construction.

  python3 tests/test_part14.py            normal + ThreadSanitizer + valgrind
  python3 tests/test_part14.py quick      normal only
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


ok = run("build", ["make", "-s", "all", "switch_tsan", "host_tsan", "build/test_stats", "build/test_stats_tsan"])
if ok:
    run("unit: shared-memory counters", ["./build/test_stats"])
    run("end-to-end: real switch", ["python3", "tests/test_shm.py"])
    if not quick:
        run("unit: counters, TSan", NOASLR + ["./build/test_stats_tsan"])
        run("end-to-end: TSan", ["python3", "tests/test_shm.py", "tsan"], {"SWITCH": " ".join(NOASLR + ["./switch_tsan"])})
        if shutil.which("valgrind"):
            run("unit: counters, valgrind", VG + ["./build/test_stats"])
            run("end-to-end: valgrind", ["python3", "tests/test_shm.py", "valgrind"])
        else:
            print("valgrind not installed: skipping the valgrind tests")

print("\nPart 14 dedicated tests:")
for name, good, dt in results:
    print("  %-34s %s  (%.0f s)" % (name, "PASS" if good else "FAIL", dt))
bad = [n for n, g, _ in results if not g]
print("RESULT: %s" % ("ALL PASSED" if not bad and results else "FAILED: " + ", ".join(bad or ["build"])))
sys.exit(0 if not bad and results else 1)
