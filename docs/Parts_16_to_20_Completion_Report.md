# Parts 16-20 Completion Report

Written 2026-11-19 after the interrupted overnight run was audited (docs/Project_Status_Audit.md) and Parts 18-20 were finished. Roadmap used: `Virtual_Switch_Implementation_Roadmap.pdf`
(the file `..._v2.pdf` named in the task does not exist on this machine).

## 1. What was found and what was done

| Part | State found by the audit | Work done in this session |
|---|---|---|
| 16 | implemented + committed (17d7851), **not pushed** | re-verified inside the full Part 18 suite; pushed with Part 18 |
| 17 | implemented + committed (217ff67), **not pushed** | re-verified inside the full Part 18 suite; pushed with Part 18 |
| 18 | **partially implemented, uncommitted**, interrupted while verifying the "teardown vs socket backlog" fix | finished (see 2.1), full verification, bug #31, committed, tag `tier2`, pushed |
| 19 | not implemented | implemented, tested, committed, pushed |
| 20 | not implemented | implemented, tested, committed, pushed (hash in section 5) |

Parts 16 and 17 were pushed together with Part 18 (one push of three commits) because they were verified together: `make test`, `make test-tsan` and `make test-valgrind` ran on the tree that contains all
three before the push.

## 2. What was implemented

### 2.1 Part 18 - Tier 2 checkpoint
* Integration scripts `vlan.sh qos.sh rate.sh mirror.sh signals.sh admin.sh idle.sh hostile.sh` (hostile = 32 hosts with `--noread`, `--partial`, kill -9, mid-frame death, admin commands and signals throughout).
* `tests/static_checks.py`: no sleeping call in the switch; no statement ignores the result of a system / pthread call. It found 53 unchecked results (log.c, mactable.c, queue.c, util.c, stats.c, admin.c, swctl.c, host.c), all fixed.
* Fix of a genuine defect found by `reconfig_stress.sh` under valgrind (bug #31): a teardown processed the whole kernel socket backlog of a flooding host. `port.closing` (atomic) makes the receiver stop at once. `tests/test_backlog.py` (21 checks).
* Idle CPU 0.2 %. Tag `tier2` created and pushed.

### 2.2 Part 19 - two switches joined by a two-pipe trunk
`./switch A n --pair B [-c a.conf] [--peer-config b.conf]`: pipes + fork before any thread; child = switch B (own TCP port, admin socket, counters `/vswitch_stats_P_<B>`, log `<A's log>.<B>`).
Port 0 = pipe trunk (rfd != wfd), same receiver/sender/queue/VLAN/trunk code, exempt from port security, no semaphore token, ends DISABLED when the peer dies. A pipe cannot be `shutdown()`:
the reaper writes an eventfd and both threads `poll()` {pipe, eventfd}. A reaps B with a pidfd wait (15 s, then SIGKILL); `PR_SET_PDEATHSIG` stops B if A is killed. swctl: row 0 in `stats`/`show ports`, `-p <B>`.

### 2.3 Part 20 - supervisor and crash-surviving MAC table
`src/supervisor.c` (single thread, `poll()` on a `signalfd`, restart after 1 s, give up after 5 restarts in 60 s, clean-up of `/vswitch_mac_<port>` and, after a crash, the counters and admin socket).
`--persist-mac`: the MAC table in `/vswitch_mac_<tcp_port>`; `mac_attach_shm` (reuse if size/magic/K/buckets match, else create), `recover_table` (robust locks incl. EOWNERDEAD, chain validation, free list /
count / per-port `mac_count` rebuilt by a scan, damaged entries dropped), `mac_detach` (no mutex destroy on a clean exit), magic written last. Log: `MAC table recovered: N entries`.

## 3. Files

| Part | Added | Modified |
|---|---|---|
| 18 | tests/{vlan,qos,rate,mirror,signals,admin,idle,hostile}.sh, tests/static_checks.py, tests/test_backlog.py, docs/Part18_Explanation.txt, docs/Project_Status_Audit.md | include/port.h, src/{port,log,mactable,queue,util,stats,admin,swctl,host}.c, tests/run_all.sh, tests/run_unit.sh, README.md, notes/bugs.md; removed tests/test_part17.py (temporary) |
| 19 | tests/test_pair.py, docs/Part19_Explanation.txt | src/{switch,port,forward,admin,swctl}.c, include/{port,stats}.h, tests/run_unit.sh, README.md, notes/{bugs,locks}.md; removed temporary tests/test_part18.py |
| 20 | src/supervisor.c, tests/test_macshm.c, tests/test_persist.py, docs/Part20_Explanation.txt | src/{mactable,switch}.c, include/{mactable,port,common}.h, Makefile, tests/run_unit.sh, README.md, notes/{bugs,locks}.md; removed temporary tests/test_part19.py |

(`tests/test_part20.py` is the temporary dedicated runner of Part 20, to be removed when Part 21 starts; Part 21 is not started.)

## 4. Tests and results

| Part | Dedicated tests | normal | TSan | valgrind |
|---|---|---|---|---|
| 18 | 8 new integration scripts, static_checks.py, test_backlog.py (21 checks) | pass | pass | pass |
| 19 | tests/test_pair.py | 76/76 | 76/76 | 76/76 (both processes instrumented) |
| 20 | tests/test_macshm.c | 135/135 | 135/135 | 135/135 |
| 20 | tests/test_persist.py | 56/56 | 56/56 | 25/25 (no supervisor scenarios: it cannot exec valgrind) |

Full suites (every run on the tree that was then committed):

| Commit | `make test` | `make test-tsan` | `make test-valgrind` |
|---|---|---|---|
| Part 18 (993d75e) | EXIT 0, 20 scripts + unit suite | EXIT 0, 19 scripts | EXIT 0, 19 scripts |
| Part 19 (c8a8d87) | EXIT 0 | EXIT 0 | EXIT 0 |
| Part 20 (5266fbc) | EXIT 0 on the 3rd run (see the note below) | EXIT 0, 19 scripts | EXIT 0, 19 scripts |

Note on the Part 20 `make test`: the 1st run failed at `tests/test_persist.py valgrind` (a race in my own new test, fixed; 3 valgrind passes afterwards); the 2nd failed in
`tests/test_reconfig.py` scenario 7 (Part 16, historical): a pre-existing test-side race, about 1 run in 4, one frame over (notes/bugs.md item 34); the 3rd passed. The historical test was NOT
modified. A fix was tried and reverted (8 consecutive passes with it): wait until every host has received the three learn broadcasts before resetting the frame counters; it needs your approval.

`make test-tsan` / `make test-valgrind` run the integration scripts under the sanitizer; the unit tests with TSan / valgrind variants are inside `make test` (run_unit.sh).
`idle.sh` is skipped under valgrind (valgrind's own CPU use).

## 5. Commits and pushes

| Part | Commit | Push verified (`git ls-remote origin`) |
|---|---|---|
| 16 | 17d7851 | yes, pushed with Part 18 (origin/main moved a06fd92 -> 993d75e) |
| 17 | 217ff67 | yes, same push |
| 18 | 993d75e (+ tag `tier2`) | yes, main and tag `tier2` both 993d75e |
| 19 | c8a8d87 | yes, origin/main = c8a8d87 |
| 20 | 5266fbc | yes, origin/main = 5266fbc |

## 6. Bugs found

* #31 (Part 18, genuine): teardown processed the kernel socket backlog; shutdown of a flooding port timed out (5 s) / SIGINT took 12-34 s under valgrind. Fixed with `port.closing`.
* #30 (Part 18): 53 unchecked call results found by the new static check; fixed. The hostile simulation found no switch defect.
* #28 (Part 16, earlier): a data race in `admin_up` found by TSan, fixed before that commit.
* #32 (Part 19): no defect; the design point that pipes cannot be `shutdown()` (found before the first run), one test-side mistake.
* #33 (Part 20): `mac_attach_shm` accepted a name without a leading `/` (found by test_macshm.c, fixed); three wrong test expectations corrected (a clean exit flushes entries, hosts must stay
  connected at crash time, TSan handles SIGSEGV itself). A test race in `test_persist.py` (a cumulative log counted hosts of the previous run) was found by the valgrind variant and fixed.

## 7. Remaining limitations

* `Virtual_Switch_Implementation_Roadmap_v2.pdf` was not available; the v1 roadmap was used.
* No official public tests were supplied (forward.sh / broadcast.sh are our own, see docs/Part8_Explanation.txt).
* Part 19: one link only; `swctl -p <B> stats` falls back to the fixed segment name when B's own segment is absent; the file names of the configuration (e.g. `mac_dump_file`) are shared by A and B
  unless `--peer-config` changes them. `--persist-mac` cannot be combined with `--pair`.
* Part 20: the valgrind variant does not run the supervisor scenarios. A table recovered after a CLEAN exit keeps only the entries of ports that never had a host (teardown flushes the rest): by design, a crash is
  what the table survives. Entries of a recovered table keep their old time stamps (CLOCK_MONOTONIC of the same boot); a different boot has no /dev/shm object anyway.
* The flake of `tests/test_reconfig.py` scenario 7 (Part 16, test-side, bug #34) was fixed afterwards with the owner's approval (see section 9).
* Parts 21 (benchmark) and 22 (report/README final) were not started, as instructed.

## 8. Parts 1-20 completion table

Status of Parts 1-15 comes from their own commits (all pushed before this session) and from the full suite, which includes their tests and passed in every run listed above.

| Part | Implementation | Testing | Commit | Push |
|---|---|---|---|---|
| 1-15 | complete | in the full suite, passing normal / TSan / valgrind at the Part 18, 19 and 20 commits | 950974e ... a06fd92 | pushed (before this session) |
| 16 | complete | full suite + test_reconfig.py + reconfig_stress.sh, 3 variants | 17d7851 | pushed |
| 17 | complete | full suite + test_mirror.c/.py + mirror.sh, 3 variants | 217ff67 | pushed |
| 18 | complete | see section 4 | 993d75e, tag tier2 | pushed |
| 19 | complete | see section 4 | c8a8d87 | pushed |
| 20 | complete | see section 4 | 5266fbc | pushed |

## 9. Follow-up: the test_reconfig flake

Applied with the owner's approval: scenario 7 now waits until every host has received the three learn broadcasts of the others before it resets the frame counters (one added check, 156 checks
instead of 155). Before: about 1 failure in 4 runs. After: 12 consecutive normal runs passed (156/156), the TSan variant passed (122/122) and the valgrind variant passed (17/17).
