# Test Cases Result: Virtual Switch

Date: 2026-11-19. Scope: whole project, checked against `Virtual_Switch_Implementation_Roadmap.pdf`.
Only failed test cases are listed in section 1, as requested. Section 2 lists what could not be run. Section 3 says exactly what was executed.

## Summary

| Item | Value |
|---|---|
| Independent verification tests written and run | 100 test cases, 741 individual checks |
| Test cases passed | 99 |
| Test cases failed | **1** (CLI-01, a minor leniency in argument parsing) |
| Tests blocked by the environment | 0 |
| Project's own `make test` (run in a scratch copy of the repository) | All passed: unit tests, valgrind and TSan unit runs, `test_*.py`, and the 20 integration scripts in the normal variant |

Beyond the single failure, the live switch showed no crashes, hangs, leaks, data races or protocol violations in any scenario I could construct.

## 0. Re-run after the fix of CLI-01 (2026-11-19, final code)

The failure below (CLI-01) was fixed (see `docs/CLI_Argument_Validation_Fix.md`). `python3 verification/run.py` was run again on the fixed tree, after `make clean && make && make tsan bench`, and after the project's own suites:

| Item | Result of the re-run |
|---|---|
| Independent verification test cases | **100 of 100 passed, 0 failed** (743 individual checks; CLI-01 now runs 66 checks: the two former failures plus the other forms) |
| `make test` | EXIT 0, "All tests passed" (unit suite incl. the new `test_parse` and `test_cli_args.py` normal / TSan / valgrind, plus 20 integration scripts) |
| `make test-tsan` | EXIT 0, 19 of 19 integration scripts |
| `make test-valgrind` | EXIT 0, 19 of 19 integration scripts |

The original run (99 passed, 1 failed) is kept below unchanged as the record of what was found.

## 1. Failed test cases

### CLI-01: Malformed `<tcp_port>` argument is accepted when it has a leading `+` or a leading space

| Field | Detail |
|---|---|
| Test case ID / description | CLI-01: the switch must reject bad command lines (exit 1, no listening socket). 22 bad argument vectors were tried. Two of them failed. |
| What it was supposed to verify | Roadmap Part 3: "parse `<tcp_port> <num_ports>` (validate the ranges 1–65535 and 1–MAX_PORTS)". A bad or non-numeric argument should be a start-up error (exit 1, message on stderr). |
| Actual result | `./switch +9000 4` and `./switch " 9000" 4` start normally and listen on port 9000. The process kept running until it was killed. |
| Expected result | Exit status 1 with an error message, as for `9000abc`, `abc`, `-1`, `0`, `65536` and an empty string, all of which were correctly rejected. |
| Exact point of failure | `src/switch.c:63`, `long v = strtol(s, &end, 10);`. `strtol` itself accepts leading whitespace and a `+` or `-` sign. The code checks `end` and the range but does not require the first character to be a digit. The check is incomplete, but it is not exploitable. |
| Error messages / logs | None. The switch logged `switch listening on TCP port 9000 ...` as if the argument were valid. |
| Possible cause | The argument parsing relies on `strtol` and only tests the trailing garbage and the range. Adding a `isdigit(s[0])` test would reject the two forms. |
| Severity | Low (cosmetic or strictness). Forwarding, security and stability are not affected. The same helper presumably parses `num_ports` and the `--pair` port, so the same leniency probably applies there. I did not test those separately. |

## 2. Tests that could not be executed because of the environment

None. valgrind, ThreadSanitizer (`switch_tsan`), `setarch -R` and `pgrep`/`/proc` were all available, and every planned test ran.

Items not tested, and why:

* Running as another user or with root-only ports above the single check of port 80 (only one unprivileged user is available).
* System clock changes (wall clock stepping) and kernel-level failure injection such as a full disk or an out-of-memory kill. These were not available without changing the machine.
* `bench/run.sh` (full 9 minute benchmark). Only the quick mode and the `bench` programs were exercised, through the project's own `test_bench.py` and my BENCH-01.

## 3. What was executed

My tests are independent of the project's own test helpers. They use raw TCP sockets that speak the wire format directly, plus the real `host`, `swctl` and `supervisor` programs. The code is in `verification/` (`vt.py` is the harness; `t_core.py`, `t_cfg.py`, `t_adm.py`, `t_adv.py`, `t_stress.py`, `t_bonus.py` and `t_more.py` are the tests). Run everything with `cd verification && python3 run.py`, or select tests by id prefix, e.g. `python3 run.py MAC QOS`. No existing project file was modified. The only new files are `verification/` and this report.

Areas covered:

* Build: pristine-copy `make`, `-Wall -Wextra -Werror`, `make clean`, `make tsan`, `make bench`.
* Wire protocol: byte order and layout, payload lengths 0, 1, 1499 and 1500, lengths 1501 and 65535 (protocol violation), 1-byte writes, 1.5 frames per write, truncated frames, garbage fuzz, VLAN rewrite, priorities above 2, broadcast, multicast and zero source MACs.
* Ports and lifecycle: lowest-free allocation, "switch full" frame (exact bytes and timing), `port_wait_timeout` 0, waiting host admitted, 200 connect/disconnect cycles (threads, fds, RSS), 64 ports, 3000-connection storm, RST storm, descriptor exhaustion (`ulimit -n 40`), 20 half-header hosts.
* Forwarding and the MAC table: learning, unicast filtering, flooding, host moves, aging, LRU eviction (K=1, 2, 3, 4, 5, 1000, 1024, 65536), port security (M=1 and M=3), per-VLAN keys, 20,000-entry `show mac` and SIGUSR1 dump.
* Head-of-line blocking: frozen reader against a control run, with drop counters.
* QoS and rate limiting: WRR 5:3:1 ordering, starvation aging, per-class drops, rate 50 frames/s over 4 s, idle CPU while throttled, rate reload.
* VLANs and the trunk: the exact roadmap scenario, invalid trunk VLANs dropped and counted, trunk exempt from port security.
* Mirroring: both directions, counters, `off`, shutdown of the sniffer, trunk and invalid targets, frozen sniffer, mirror loops.
* Admin and `swctl`: all commands, 40 invalid argument forms, exit codes 0, 1 and 2, hostile protocol input (long line, no newline, NUL, binary, silent client, 30 parallel clients), `--watch`, 300 random operations under load.
* Config and signals: 43 invalid configs rejected, 22 edge configs accepted, SIGHUP storm, bad reload, SIGUSR1, SIGINT/SIGTERM with stuck hosts, SIGINT racing admin commands (20 rounds), closed stdin/stdout/stderr.
* Counters in shared memory: exact IN/OUT/DROP accounting, stale segment replaced, mode 0600, removal on exit, killed readers.
* Hidden-test simulation: 32 hosts (`--noread`, `--partial`, `kill -9`, `--die-midframe`, flooders) with admin commands and signals running throughout; 64 hosts broadcasting at once; data integrity with CRC and ordering checks; 100,000-frame flood.
* Sanitizers: valgrind (full leak check) and the TSan build under mixed traffic, admin commands and signals. Both were clean.
* Bonus: two switches over pipes (VLANs across the link, MAC on port 0, peer death, `PDEATHSIG`, argument validation, load, valgrind on both processes), supervisor and `--persist-mac` (SEGV recovery, crash-loop give-up, clean exits, corrupt segment), benchmark programs and the three lock variants.

## 4. Observations that are not test failures

These are for information only. None of them broke a requirement.

* **Leftover admin sockets in `/tmp`.** 19 `/tmp/vswitch_<port>.sock` files from earlier sessions (switches stopped with `kill -9`) were present before my tests started. A new switch on the same port replaces such a file correctly (tested). Nothing cleans them otherwise.
* **Every received frame is logged at INFO level** (`port N: RX src=... len=...`). Under a flood this makes the log very large. The asynchronous logger drops lines instead of slowing forwarding (verified in LOG-02), so throughput is not affected. Log file size is unbounded.
* **Fixed stats shared-memory name** (`/vswitch_stats_P`). Only one switch per machine and user can be visible to `swctl stats`. This is already documented in the README. It is also why my suites and the project's own `make test` cannot run at the same time.
* **Test-harness corrections made during the work.** Several early failures turned out to be mistakes in my tests, not in the switch. Examples: counting a payload length wrong, ignoring the documented drop behaviour when a host floods faster than it reads, and a host that exits at stdin EOF. I fixed those tests and re-ran them. They are not reported as failures. The final complete run shows 99 passed and 1 failed (CLI-01).
