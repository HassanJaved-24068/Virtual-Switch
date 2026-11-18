# End-to-End Verification Report — Virtual Switch, Parts 1–15

Verification phase only. No Part 16/17 functionality was implemented, nothing was committed or pushed, no historical test was deleted or weakened.

Statuses: **PASS** (a test that ran and passed in this session) · **FAIL** · **NOT TESTED** · **DEFERRED** (belongs to a later part) · **INCONCLUSIVE**.

## 1. Revision and working tree

- `HEAD` = `f382731` ("Part 14: per-port counters in POSIX shared memory"). Part 15 is **not committed**: 10 tracked files modified/deleted
  (`Makefile`, `README.md`, `notes/bugs.md`, `src/swctl.c`, `src/switch.c`, `tests/run_all.sh`, `tests/run_unit.sh`, `tests/test_shm.py`,
  `tests/test_switch.py`, `tests/test_part14.py` deleted) plus new files `src/admin.c`, `include/admin.h`, `tests/test_admin.{c,py}`,
  `tests/test_swctl.c`, `tests/test_part15.py`, `tests/test_e2e.py`, `docs/Part15_Explanation.txt`, and this report.
- Machine: 2 CPUs, 7.9 GB RAM, Linux 7.0 (VMware). All suites were run one after another, never concurrently.
- The roadmap in the project directory is `Virtual_Switch_Implementation_Roadmap.pdf` (v1); `..._v2.pdf` and the original assignment PDF are **not present**.
  The Part 15 text read was the one in that file. Requirements from the hand-over text were used where they go further.

## 2. Preparation

| Item | Status | Detail |
|---|---|---|
| Strict flags `-std=c11 -Wall -Wextra -Werror -g -O0` | PASS | in `CFLAGS` of the Makefile |
| `make clean && make` | PASS | `switch`, `host`, `swctl` built in 2.6 s, no warnings |
| No old processes | **FOUND AND FIXED** | two orphaned `./switch` (ports 50601, 33971, 55 min old, parent systemd) from my own earlier crashed `test_admin.py` runs (Python traceback before `sw.stop()`). Idle, not hung; both exited cleanly on SIGTERM and removed their sockets. Root cause and fix: §16 (follow-up). |
| Stale resources | PASS | a leftover `/tmp/vswitch_34283.sock` (from a SIGKILLed switch of an older test) was replaced by a new switch on that port; `swctl show ports` worked; SIGINT removed both socket and `/dev/shm/vswitch_stats_P` |
| Cleanup rules | PASS | only build outputs (`make clean`), two orphan processes and test-created `/tmp` sockets were removed |

## 3. Test configurations run (clean build)

| Configuration | Command | Result | Time |
|---|---|---|---|
| Dedicated Part 15 | `python3 tests/test_part15.py` | **PASS** (build, parser 79, swctl/segment 28, end-to-end 223, parser TSan 79, end-to-end TSan 121, parser valgrind 79, end-to-end valgrind 15) | 46 s |
| Normal | `make test` | **PASS**: Parts 1–15 suite + 10 integration scripts (11/11 groups) | 889 s |
| ThreadSanitizer | `make test-tsan` | **PASS**: 10/10 scripts, 0 TSan reports | 54 s |
| Valgrind | `make test-valgrind` | **PASS**: 10/10 scripts | 98 s |
| Cross-feature scenarios, normal | `python3 tests/test_e2e.py` | **PASS** (first run 60 checks; after the follow-up: 142 checks, with C5 strict and the new C5b). One earlier run failed in C5: explained and fixed in §16 | ~100 s |
| Cross-feature scenarios, TSan | `SWITCH="setarch -R ./switch_tsan" python3 tests/test_e2e.py` | **PASS** 96 checks (after fixing two test defects, §9) | ~95 s |
| Cross-feature scenarios, Valgrind | — | NOT TESTED (timing thresholds of the scenarios are not meaningful under valgrind) | |

Note on what the suites cover: `make test` runs the unit and end-to-end test of every part **in normal, TSan and (where valgrind is installed) valgrind
variants** (see `tests/run_unit.sh`), then the ten integration scripts. `make test-tsan` / `make test-valgrind` only re-run the ten scripts with the
sanitizer build / valgrind. The Parts 5–15 end-to-end suites under TSan and valgrind therefore ran as part of `make test` (no failures, no sanitizer reports).
There is no separate `test-tsan` target for the Part 1–15 suite beyond that; nothing was invented.

## 4. Individual features (section B)

“Test” = the permanent test that checks it; all PASS in the `make test` run above unless noted.

| # | Feature | Status | Evidence |
|---|---|---|---|
| 1 | Build, start, many hosts, port allocation, connection limits (SWITCH FULL after 5 s), invalid connections | PASS | `test_switch.py` (168→169 checks), `test_lifecycle.py`, `port_full.sh`, `many_hosts.sh`, `test_port.c` |
| 2 | 17-byte header, byte order, partial reads/writes, mid-frame disconnect, unicast, broadcast, flooding, learning, MAC move | PASS | `test_frame.c`, `wire_bytes.sh` (258→`01 02`, 17-byte header), `test_host.py`, `partial.sh`, `midframe_disconnect.sh`, `forward.sh`, `broadcast.sh`, `mac_move.sh`, `test_mac.py` |
| 3 | Bounded queues, queue-full drops, senders, slow/non-reading host, no head-of-line blocking, refcount, concurrent senders | PASS | `test_queue.c`, `test_refcount.c` (+TSan), `test_forward.py`, `slow_host.sh`; scenario C5 below |
| 4 | MAC aging, LRU, port security, admission/timeouts, cleanup and reuse | PASS | `test_mactable.c` (+TSan, valgrind), `test_mac.py`, `mac_aging.sh`, `port_security.sh`, `test_lifecycle.py` |
| 5 | SIGINT: no hosts / traffic / waiting host / full queue; all threads exit; resources freed | PASS | `test_shutdown.py` cases 1–10 (normal, TSan, valgrind: no leaks, no open fds) |
| 6 | Logging: events, heavy traffic, shutdown drain, fast path not blocked | PASS | `test_log.c` (+TSan, valgrind), `test_logger.py` (stalled log file does not slow a 100k-frame flood; SIGINT drains) |
| 7 | Config file, invalid config, SIGHUP in traffic, atomic failure, startup-only keys | PASS | `test_config.c`, `test_config_signals.py` (normal/TSan/valgrind); scenario C3 |
| 8 | QoS classes, 5:3:1 WRR, aging, per-class fullness, reload | PASS | `test_qos.c`, `test_qos.py` |
| 9 | Rate limiting: unlimited, per-port, burst, isolation, no spinning, receiver not blocked | PASS | `test_rate.c`, `test_rate.py`; scenario C2 (CPU) |
| 10 | VLANs and trunk: access VLANs, in-VLAN broadcast/unicast, no crossing, trunk in/out, invalid VLAN, VLAN-aware learning | PASS | `test_vlan.c`, `test_vlan.py`; scenario C1 |
| 11 | Shared memory: created, 0600, layout, counters = traffic, robust mutex, stale cleanup, removed at exit | PASS | `test_stats.c`, `test_shm.py`; scenarios C1/C3/C6 |
| 12 | Admin socket and swctl: socket, `show mac/ports`, `clear mac`, `stats`, `--watch`, argument validation, exit codes 0/1/2, read-only data mapping, silent/bad clients, SIGINT with a client connected, cleanup | PASS | `test_admin.c`, `test_swctl.c`, `test_admin.py` (223 checks), `/proc/<pid>/maps` of the real swctl: `rw-s` offset 0, `r--s` offset PAGE |
| 12b | `shutdown`, `up`, `vlan`, `mirror` | **DEFERRED** (Parts 16/17) | only parsing/validation verified; a valid command answers `ERR not implemented yet (Part 16|17)`, exit 1. A mirror target that is the trunk is refused. |
| — | Mirrored counter | DEFERRED | always 0 until Part 17 |

## 5. Cross-feature scenarios (section C) — `tests/test_e2e.py`

All measured on the normal build unless stated; TSan results in brackets.

| Scenario | Status | Measured |
|---|---|---|
| C1 VLANs + QoS (two VLANs, trunk, 3 priorities, WRR 5,3,1, aging 200 ms) | PASS (TSan PASS) | each VLAN neighbour got all 180 frames, trunk 360, no frame crossed VLANs, no drops; shared-memory counters equal the host counts |
| C2 rate limit 50/s on one port, others forward | PASS (TSan PASS) | 2091 frames sent in 7 s; unlimited ports received all 2091; limited port 400; drops counted on that port only; switch CPU 14 % during the flood (TSan 27 %); idle CPU 0.01 s in 3 s |
| C3 SIGHUP in traffic (valid, valid, invalid, valid) | PASS (TSan PASS) | weights changed 5,3,1→1,1,1→7,2,1→5,3,1; invalid file logged as `reload failed`, previous config kept; `in` = frames sent, `out` = other hosts' frames, 0 drops, hosts received exactly those |
| C4 MAC aging while hosts send (switch_small, T=3 s) | PASS (TSan PASS) | sending host's MAC kept, silent host's MAC aged out, `show ports` counts follow, unicast to the aged MAC flooded again |
| C5 saturation: one host stops reading, others keep forwarding | **PASS** after the follow-up (see §16): 30 of 30 normal runs, 5 of 5 under TSan; C5b stress 8 of 8 normal, 4 of 4 TSan | the original C5 failed in about 1 of 5 (then 6 of 15 instrumented) runs; cause established in §16: queue-full drops of a READING port at 15 ms tolerance on a 2-vCPU VM, fully accounted by the switch; not a switch defect |
| C6 `swctl stats` under heavy traffic | PASS (TSan PASS) | 172 snapshots, all monotonic and consistent, worst `swctl stats` latency 94 ms (TSan 152–195 ms); final `in` = frames sent; port 4 `out + dropped` = frames sent and equals what the host received |
| C7 `show mac`/`show ports` while hosts connect/disconnect | PASS (TSan PASS) | 611 rounds in 10 s, every answer OK and well formed; afterwards ports 1,2 UP, others FREE |
| C8 SIGINT with traffic + throttled port + silent admin client + `stats --watch` | PASS (TSan PASS) | exit 0 in < 3 s; socket and segment removed; the watcher exited with status 2 |

## 6–8. Normal / TSan / Valgrind

See §3. No test failed in `make test`, `make test-tsan` or `make test-valgrind`; no ThreadSanitizer warning and no valgrind error were reported
(grep for `FAIL`, `WARNING: ThreadSanitizer`, `ERROR SUMMARY: [1-9]` over the logs: no hits). `tests/test_mac.py` (bug-list item “open”, intermittent)
**passed** in every run of this session (twice in full runs, plus the Parts 1–15 runs); it did not reappear. Its cause remains unknown.

## 9. Failures investigated

| What | Command / output | Classification | Outcome |
|---|---|---|---|
| `test_admin.py` first runs crashed (`TypeError`, then `ConnectionResetError`) | during Part 15 development | the second one was a **genuine switch defect** (reply lost when closing with unread request bytes) | fixed, recorded in `notes/bugs.md` #26 |
| `test_switch.py` thread list / descriptor count after adding the admin thread | first `make test` | expected behaviour change, old assertions adapted (+1 thread, +1 fd) | recorded in `notes/bugs.md` #26 |
| `test_e2e.py` under TSan, C6: `in` ≠ sent, `out + dropped` ≠ sent | `SWITCH="setarch -R ./switch_tsan" python3 tests/test_e2e.py` | **test defect / timing**: the TSan switch is slower than the Python flooders; 2 s was not enough for the kernel backlog. Measured drain time 4.9 s and 5.7 s. After waiting for `in == sent` all counters were exact | test now waits for the drain and prints its duration |
| same run, R: thread count baseline 5 (later 9) | | **test defect**: baseline sampled before the switch had created its threads (the "listening" log line comes first) | baseline now waits until the count is stable for 1 s |
| `test_e2e.py` C5, normal build, intermittent: `no other port dropped anything` / `the reading hosts got every frame` FAIL | full run of `tests/test_e2e.py` | **EXPLAINED (test defect, environment-dependent load)**; reproduced 6 of 15 times with the instrumented test; see §16 for the evidence | C5 now floods at about 840 frames/s (38 ms tolerance) with every original assertion unchanged, waits for the real completion condition and adds exact accounting checks; C5b keeps the original high rate and asserts the accounting only |

No new genuine switch defect was found in this phase.

## 10. Performance and resources (measured; conditions in brackets)

| Measure | Result |
|---|---|
| Threads | 8 with no host (main, logger, signal, reaper, aging, token-timer, accept, admin); +2 per host (rx, tx); unchanged under load; back to 8 after the hosts left. TSan build: +1 runtime thread (9). |
| Open descriptors | 7 with no host, 15 with 8 hosts (+1 each), back to 7 afterwards. Valgrind `--track-fds` reported none left open in any valgrind test. |
| Memory | RSS 2064 KB at start; 2628 KB flat over 20 s of sustained flooding (4 flooders, 8 hosts, 44497 frames in 25 s); TSan build 21 MB → 40 MB, still flat in the last 10 s. This is a 25 s observation, not a leak proof; valgrind reports 0 bytes in use at exit. |
| CPU | idle switch: 0.00 s in 5 s with and without hosts, 0.01 s in 3 s with a rate-limited port connected. Rate limiting active: 14 % of one core under a flood (the Python flooder on the same 2-CPU VM also runs). 68 % of one core under 4 flooders (8 hosts). |
| Busy waiting | none observed (idle CPU ≈ 0; `test_rate.c` also checks that a throttled sender does not spin). A `grep sleep(` review of the sources was not part of this phase (it is Part 18). |
| Shared memory | `/dev/shm` empty after every normal exit, SIGINT/SIGTERM and failed start; a stale object is replaced |
| Sockets | `/tmp/vswitch_<port>.sock` removed at every clean exit. A **SIGKILLed** switch leaves it (and the shm object) behind; the next switch on that TCP port replaces it (verified). Over the session a stale socket from the SIGKILL tests of older parts is left in `/tmp`; harmless, removed by the next start on that port. |
| Queue behaviour | queue-full drops are counted per port and never block the sender (C5: 13992 drops on the non-reading port, 0 elsewhere in 4 of 5 runs) |
| Logging under load | flood with a stalled log file: no slowdown (`test_logger.py` test 4) |

## 11. Failed or flaky tests

- C5: explained and fixed (§16). Two further intermittent test-side failures were found and fixed during the follow-up (`test_admin.py` connect race and 100 KB send race, §16). The `tests/test_rate.c` valgrind flake was fixed (§16.3). One earlier `test_qos.py valgrind` failure is of UNKNOWN cause and did not reappear (§16.3).
- `tests/test_mac.py` (item “open” in `notes/bugs.md`): did not fail in this session.

## 12. Known limitations

- Shared-memory name `/vswitch_stats_P` is fixed: with several switches, `swctl stats` reads the newest one's segment whatever `-p` says (Part 14 limitation, documented).
- A FREE port has VLAN 1 in the shared segment (port.c applies the configured VLAN when a host connects); `show ports` shows the configured VLAN for such ports, `swctl stats` the segment's value.
- The 2 s limit of a **reply** to a client that never reads could not be exercised: replies are far smaller than the kernel socket buffer. The code path (poll on client and `wake_efd`) is the one exercised by the request-side shutdown tests.
- `swctl shutdown|up|vlan|mirror` are validated only (Parts 16/17).
- A SIGKILLed switch leaves the socket path and the shm object behind (recovered at the next start).
- Verification covers only what the tests assert; for the hidden/official tests nothing is known (none were supplied).

## 13. Remaining bugs

- None known in the switch. Open: the intermittent `test_mac.py` item (cause unknown, not reproduced here), and one `test_qos.py valgrind` failure of UNKNOWN cause, not reproduced (§16.3). The `tests/test_rate.c` valgrind flake is fixed.
- Test hygiene: fixed in the follow-up (§16): every scenario of `test_admin.py` and `test_e2e.py` now cleans up in a `finally`.
- `tests/test_e2e.py` is **not** wired into `run_unit.sh`/`make test` (the permanent suite was left untouched on purpose); it is run by hand.

## 14. Overall assessment

Parts 1–15 build with the strict flags and pass the normal, ThreadSanitizer and Valgrind suites of this project in a clean tree, run sequentially, with one exception (see the caveat below).
Every feature in section B has at least one passing test; the cross-feature scenarios pass, including C5 after the follow-up (§16). Resource measurements show constant threads and descriptors, flat memory over the observed 25 s, and near-zero idle CPU.
`make test` finishes green in the final state (11 of 11 groups); the history of failures on the way is in §16.3 and §16.4.
Verified here is limited to what the tests assert; remaining weak spots are the unexercised reply-side timeout and the unexplained single `test_qos.py valgrind` failure.
The write commands of `swctl` are DEFERRED. Part 15 is verified as implemented; it is ready for the user's review. Nothing was committed.


## 16. Follow-up: test reliability and the C5 investigation

No production code was changed in this follow-up (`src/` and `include/` are exactly as at the end of Part 15). Everything below is in tests and notes (`notes/bugs.md` item 27).

### 16.1 Orphaned switches after a failed `test_admin.py` scenario (Issue A)
- **Root cause:** each scenario ended with a plain `sw.stop()` and no `try/finally`; UNIX client sockets, hosts and `swctl --watch` children were registered nowhere; the main block only removed the temp directory; and a switch whose constructor raised ("did not start") was never reachable because the object did not exist yet.
- **Mechanism now:** a registry (`STARTED`, `CLIENTS`), `run_scenario()` with cleanup in `finally` (the original exception propagates unchanged; cleanup errors are printed and counted, never replace it; every step runs), `stop_proc()` (SIGTERM, wait, SIGKILL on timeout, wait, safe if already exited), per-scenario exception handling in `run_tests()`, SIGTERM of the test script turned into SystemExit. Only exact processes started by the run are signalled. `test_e2e.py` uses the same mechanism.
- **Proof:** `tests/test_harness_cleanup.py` (38 checks, now in `make test`): normal completion with and without `sw.stop()`; AssertionError, RuntimeError, KeyboardInterrupt; a real `./host` child; a `swctl --watch` child; a switch that ignores SIGTERM (killed after the timeout, reaped, no zombie); a switch that never prints "listening"; an unrelated switch with the same program name is untouched; an error inside cleanup neither hides the original exception nor skips the remaining steps. Mutation check: with the switch-stopping step disabled, 11 of the 38 checks fail.
- **Result:** after all runs of the follow-up no `switch`, `host`, `swctl`, `fake_switch` or CPU-hog process was left and `/dev/shm` was empty. Stale `/tmp/vswitch_*.sock` files come from the existing Part 14 test `test_shm.py` (it SIGKILLs a switch on purpose: one socket per run; verified: `test_admin.py` and the harness test leave none). They were removed by hand; the next switch on the same port replaces such a file anyway.

### 16.2 C5: where the frames were lost (Issue B)
**How C5 is instrumented now (every run):** every frame carries `(sender, sequence number)`; the reader records the tags, recv calls, longest gap between recvs; a 10 Hz sampler reads the kernel's TCP queues (`/proc/net/tcp`) and the shared-memory counters; per-thread CPU and run-queue wait of the readers and of the switch's `tx-port`/`rx-port` threads (`/proc/<pid>/task/<tid>/schedstat`); system CPU (`/proc/stat`); per-port `in/out/dropped` before and after; generated, received and missing sequence numbers per reader (compact ranges), duplicates and out-of-order; durations of the flood and of the drain. Completion is no longer a fixed 1.5 s sleep but "every reading host received what the switch counted as `out`, stable for 1 s". The switch does **not** expose queue occupancy; "queue full" is inferred from the `dropped` counter (a counted drop happens only when `outq_try_push` fails, `src/forward.c:97`).

**Original C5 as found (what the old code could and could not show):** it slept a fixed 1.5 s before comparing counters (a completion race when a reader has a backlog; under TSan the backlog took 4.9-5.7 s to drain), frames carried no sequence numbers, and it asserted zero drops on the reading ports.

**Reproduction (normal build, sequential, original load about 2000 frames/s into each reading port; each row is a full set of runs):**

| Configuration | Runs | Runs with a drop on ports 1-4 | Accounting identity violations |
|---|---|---|---|
| legacy reader (the receive loop of `ta.Host`) | 15 | 6 | 0 |
| faster reader (`recv_into`, offsets) | 12 | 4 | 0 |
| faster reader + 8 MB receive buffers on the readers | 12 | 2 | 0 |
| legacy reader + 8 MB receive buffers | 12 | 5 | 0 |
| legacy reader + one CPU-hog process | 8 | 1 | 0 |
| flooders slowed to about 836 frames/s | 10 | **0** | 0 |
| about 357 frames/s | 10 | **0** | 0 |
| about 186 frames/s | 10 | 0, but the unread port no longer saturates in 8 s | 0 |

**Evidence for the loss location:** in every one of the 59 original-load runs, on every port: `out + dropped` = frames broadcast to it; the reader received exactly the switch's `out` counter; the number of missing sequence numbers = the switch's `dropped` counter. Example (legacy run 1): host 1's frames 6098-6117 are missing on ports 3 **and** 4 (20 frames each), ports 3/4 counters `dropped` 20 and 42, readers received 16786/16764 of 16806 = switch `out`. So loss = **bounded-queue overflow counted by the switch (category A)**; not TCP (C), not accounting or completion (D), and no unexplained switch behaviour (E: the switch dropped and counted, never blocked the receiver, exactly as designed).

**Why the queue filled:** a drop needs the consumer to be delayed for longer than `queue_capacity / arrival rate`; at 32 slots and about 2100 frames/s that is 15 ms. Missing sequence numbers come in contiguous bursts, often the same range on 2-3 ports at once (a common stall). Per-thread scheduler statistics show that on this 2-vCPU VM both the Python readers and the switch's sender threads spend long times runnable but not running (for example `tx-port3`: ran 358 ms, waited 1162 ms in 8 s) while the machine is 36-69 % idle with 0 % steal: scheduling latency under bursty load. A more efficient reader and 8 MB receive buffers (which absorb about 1.7 s of reader stall) did **not** remove the drops, so the Python reader is not the bottleneck (category B rejected); lowering only the offered rate did. **Category: test defect (an environment-dependent assumption at a load that leaves 15 ms of tolerance) plus a completion race; resource/scheduling contention in the environment.**

**What was not established:** which of the switch's sender wake-up delay or a reader delay caused each individual burst (the switch exposes no per-queue occupancy or per-wakeup latency); only that neither a faster reader nor larger receive buffers removes it and that lower load does. The instrumented runs reproduced the failure at a higher rate (6 of 15, 4 of 12) than the original 1 in 5; the instrumentation (8 bytes per frame, O(1) per recv, a 10 Hz sampler) may add some load, and this was not separated from run-to-run variation.

**Fix:** C5 now floods at about 840 frames/s (38 ms of tolerance) with **all original assertions unchanged and strict** (port 5 drops > 0; ports 1-4 drop nothing; the reading hosts receive every frame; the two senders receive each other's frames; `in` counters match), waits for the real completion condition, and adds exact accounting checks per port. **C5b** keeps the original high rate and asserts what is true at any rate (exact accounting, the saturated port drops, the senders are never blocked), reporting losses on reading ports. Queue capacity (32), the number of hosts and the assertions were not changed to hide the problem; the lower rate is justified by the sweep above.

**Confirmation:** C5 (strict): 30 of 30 runs passed, 0 drops on ports 1-4 (frames 3749-6915 per run, drain at most 1.6 s). C5b: 8 of 8. Under TSan: C5 5 of 5, C5b 4 of 4. Thirty clean runs show that it is rare at this load, not that it cannot happen: a long enough scheduler stall still drops frames, by design.

### 16.3 Other intermittent failures found while confirming (all test-side)
- **`test_admin.py` `BlockingIOError` on `connect()`** after a burst of 15 clients: the admin thread serves one client at a time with `listen(8)`; a connect on a socket with a Python timeout is non-blocking and gets `EAGAIN` while the backlog is full. Reproduced 19 of 150 times; with a blocking connect (as `swctl` does) 0 of 150. Fixed with `uconnect()`.
- **`test_admin.py` `BrokenPipeError`** while sending a 100 KB request that the switch refuses after 256 bytes: the send failed 36 of 400 times, but the reply `ERR line too long` was received 400 of 400. `raw()` now tolerates EPIPE on send; the assertion was tightened (it used to accept an empty reply).
- **`tests/test_rate.c` line 223 `cpu < 50.0` under valgrind (fixed, approved):** the Part 12 check that a token-starved sender thread is blocked, not spinning, measured its cumulative CPU over 300 ms, which includes valgrind's thread start-up (28-83 ms). Failed in 3 of 10 standalone runs and in two full `make test` runs (75 and 79 ms); a spinning thread would use about 300 ms. A first fix, a 150 ms bound on the cumulative value, still failed 2 of 15 runs (170.6, 172.6 ms). Final fix: measure the CPU over the settled 300 ms window (after 100 ms), bound 150 ms under valgrind (detected via `LD_PRELOAD`), 50 ms otherwise. Settled-window cost under valgrind: max 62.6 ms in 40 runs; normal/TSan 0.02-0.19 ms. Mutation check: a busy-loop wait is caught in both builds (294 ms normal, 297 ms valgrind). 0 of 25 consecutive valgrind runs failed. `src/` untouched.
- **Orphans from the historical harness (fixed):** `ts.Switch.__init__` left its process running if the switch did not report "listening" within 10 s. During a slow period of the machine (cause unidentified) one slow start cascaded: 7 consecutive `test_qos.py valgrind` runs failed to start and left 3 orphaned valgrind switches (terminated by exact PID). The constructor now stops its own process; tested with a fake switch that ignores SIGTERM.
- **CAUSE UNKNOWN - not reproduced (`test_qos.py valgrind`):** the third full `make test` of the follow-up failed once at this step. Original output, preserved verbatim (raw logs lost with the cleared scratchpad; copied from the session transcript): `FAIL: valgrind: D, which reads, got all marked frames: the switch has forwarded them` / `11 checks passed, 1 failed` / `FAILED: python3 tests/test_qos.py valgrind` (the check waits 30 s for marker frames to reach reading host D with `queue_capacity = 8`). Right afterwards the same test took 57 s once (normally 16-17 s) and then failed 7 times in about 16 s each with `switch did not start` (valgrind did not print "listening" within 10 s; normally 2.0-2.6 s), leaving 3 orphaned valgrind switches that were terminated by exact pid. It then passed 8 of 8 standalone runs, 6 of 6 runs with two CPU-hog processes on the 2-CPU VM (so plain CPU contention does not trigger it), and two later full `make test` runs. What made the machine slow during that episode is unidentified; a drop of the marker frames on the small queue under valgrind is plausible but was not shown (no counters were printed, no log was kept). Test and switch unchanged. Details and what to capture if it recurs: `notes/bugs.md` item 27 F.

### 16.4 Test runs of the follow-up (sequential, final state)
| Run | Result |
|---|---|
| `tests/test_harness_cleanup.py` | PASS, 38 checks |
| `python3 tests/test_part15.py` | PASS: parser, swctl/segment, end-to-end 234, TSan, valgrind |
| `python3 tests/test_admin.py` ×5 consecutive | PASS ×5, 234 checks each |
| `python3 tests/test_e2e.py` (normal, with C5 and C5b) | PASS, 142 checks |
| `valgrind ./build/test_rate` ×25 | PASS ×25 (settled-window CPU 5-39 ms, limit 150 ms) |
| `make test` (final state) | **PASS**: 11 of 11 groups (Parts 1-15 + harness test + the 10 integration scripts), 11 min 11 s |
| `make test-tsan` | PASS, 10/10 scripts, 0 ThreadSanitizer warnings |
| `make test-valgrind` | PASS, 10/10 scripts |

History on the way to the green run: `make test` failed twice at `valgrind ./build/test_rate` (fixed), once at `test_qos.py valgrind` (unexplained, §16.3), and the Part 13-15 groups were once run from a scratchpad copy of `run_unit.sh` because the script stops at the first failure. The final `make test` above is the unmodified target.

Cleanup after the follow-up: no `switch`, `host`, `swctl`, valgrind or fake-switch process left; `/dev/shm` empty; the stale `/tmp/vswitch_*.sock` that the existing Part 14 test `test_shm.py` leaves (it SIGKILLs a switch on purpose) was removed.

### 16.5 Final review of the follow-up (re-run in the final state; nothing in `src/` or the tests was changed by this review)
- **`tests/test_rate.c`:** diff reviewed (only the CPU measurement of `test_burst_and_block` changed: the cumulative CPU after 300 ms became the CPU used in a 300 ms window that starts after a 100 ms settling time; a helper detects valgrind through `LD_PRELOAD`). The burst, token-count, half-token and "one frame per whole token" assertions around it are unchanged. Mutation checks against `src/queue.c` (each applied alone, restored byte-for-byte afterwards): bucket twice too large (initial burst) -> 7 failed checks; a frame costs half a token (excess throughput) -> 4; refill twice too fast -> 9; token check disabled (no rate limiting) -> 1 (`!popper_wait(&p, 100)`); busy-loop wait (spinner, timeout 0) -> 1 (`cpu < limit`: 294 ms in the normal build, 297 ms under valgrind). Repeated runs, final state: normal 0 failures of 20 (window CPU 0.04-0.19 ms), TSan 0 of 20 (0.04-0.44 ms), valgrind 0 of 25 (3.2-6.4 ms on a calm machine; 5-63 ms in the earlier, slower period, 40 runs, max 62.6 ms). The 150 ms bound is a judgement, not a statistic: it is about half of what a spinning thread uses (about 295 ms) and 2.4 times the largest noise seen (62.6 ms).
- **Harness cleanup:** `run_scenario()`/`cleanup()` only touch what the run registered (switches through the patched `ts.Switch` constructor, sockets through `usock()`, hosts, `track()`ed children); signalling uses the `Popen` handle of that child (no pid reuse hazard, no `pkill`/`killall` anywhere in the tests); verified by the harness test (an unrelated switch of the same program name keeps running). Known minor limitation: the pipe file objects of a tracked `Popen` (`swctl --watch`) are not closed explicitly; they die with the test process.
- **C5, fresh runs in the final state:** normal build 30 of 30 consecutive passes (26 checks each, frames per run 5442-6555, 0 drops on ports 1-4, drain at most 1.2 s); TSan 10 of 10; C5b 5 of 5 (24 checks each). The assertions were not changed in this review.
- **Unexplained QoS/valgrind failure:** labelled CAUSE UNKNOWN above (§16.3); one more attempt to reproduce it, 6 runs with two CPU-hog processes, passed 6 of 6.
