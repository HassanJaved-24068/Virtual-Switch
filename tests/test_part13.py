#!/usr/bin/env python3
"""
TEMPORARY dedicated test file for Part 13 (VLANs and the trunk port). It is deleted when Part 14 starts
(tests/test_part14.py replaces it).

It runs ONLY the Part 13 tests, which live permanently in the main suite (tests/run_unit.sh -> make test):
  tests/test_vlan.c    forward_frame with real port structures, no sockets: the roadmap's layout (ports 1,3 VLAN 10; 2,4 VLAN 20;
                       port 5 trunk), broadcast/unicast eligibility, (VLAN, MAC) lookup, same MAC in two VLANs, invalid trunk VLANs
                       (0, 4095, 65535 dropped and counted; 1 and 4094 valid), access ports overwrite f->vlan AND wire bytes 14-15,
                       trunk exempt from port security, port_set_vlan + flush, the window before the flush, 4 threads + a flipper
  tests/test_vlan.py   the real switch: the criterion with real sockets, default VLAN 1, invalid trunk frames, SIGHUP VLAN moves,
                       a new host on a port, bad values, port security of the trunk, SIGINT under traffic, valgrind
so the dedicated and the permanent coverage are the same tests by construction.

  python3 tests/test_part13.py            normal + ThreadSanitizer + valgrind
  python3 tests/test_part13.py quick      normal only
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


ok = run("build", ["make", "-s", "all", "switch_tsan", "host_tsan", "build/test_vlan", "build/test_vlan_tsan"])
if ok:
    run("unit: VLAN forwarding", ["./build/test_vlan"])
    run("end-to-end: real switch", ["python3", "tests/test_vlan.py"])
    if not quick:
        run("unit: VLAN forwarding, TSan", NOASLR + ["./build/test_vlan_tsan"])
        run("end-to-end: TSan", ["python3", "tests/test_vlan.py", "tsan"], {"SWITCH": " ".join(NOASLR + ["./switch_tsan"])})
        if shutil.which("valgrind"):
            run("unit: VLAN forwarding, valgrind", VG + ["./build/test_vlan"])
            run("end-to-end: valgrind", ["python3", "tests/test_vlan.py", "valgrind"])
        else:
            print("valgrind not installed: skipping the valgrind tests")

print("\nPart 13 dedicated tests:")
for name, good, dt in results:
    print("  %-34s %s  (%.0f s)" % (name, "PASS" if good else "FAIL", dt))
bad = [n for n, g, _ in results if not g]
print("RESULT: %s" % ("ALL PASSED" if not bad and results else "FAILED: " + ", ".join(bad or ["build"])))
sys.exit(0 if not bad and results else 1)
