# Bug diary

One entry per real bug: symptom, how it was found, root cause, fix.
One of these goes into the final report.

## 1. Trigraph error in log.c (Part 1)
- Symptom: `make` failed with `trigraph ??- converted to ~ [-Werror=trigraphs]`.
- Found by: the compiler, because of -Wall -Werror.
- Cause: the fallback timestamp string "????-??-?? ??:??:??" contains `??-`,
  which C treats as a trigraph (an old way of typing `~`).
- Fix: use the plain fallback text "unknown-time".

## 2. Test output printed several times (Part 1)
- Symptom: `tests/test_util` printed the same section names 2-3 times when
  its output went to a pipe.
- Found by: reading the output of `make test`.
- Cause: stdout is fully buffered on a pipe; `fork()` copied the unflushed
  buffer into each child process, and each child flushed its copy on exit.
- Fix: `fflush(stdout)` before every `fork()`.

## 3. Host seemed to hang under valgrind (Part 2) - false alarm, understood
- Symptom: `valgrind ./host` did not exit after the switch closed the
  connection (the "connection closed" message was printed, the process stayed).
  5 checks of tests/test_host.py failed with a timeout, only under valgrind.
- Found by: running tests/test_host.py with HOST="valgrind ... ./host";
  `--trace-syscalls=yes` showed exit_group followed by valgrind's
  `__libc_freeres` wrapper waiting on a futex.
- Cause: the receiver thread calls _exit() while the main thread is blocked
  in getline(stdin). valgrind's libc clean-up needs the stdin lock that the
  blocked thread holds. Normal and TSan runs are fine.
- Fix: none needed in the host. Run valgrind on the host with
  --run-libc-freeres=no (documented in docs/Part2_Explanation.txt).
  Result: 109 checks, 0 failures, no valgrind errors.

## 4. Test-script mistakes found while writing the Part 2 host tests
- Python's buffered readline() hid lines from select(): the test helper now
  keeps its own buffer.
- The test closed the host's stdin before the host had noticed the switch
  hanging up (race): finish(wait_first=True) now waits for the host first.
(Both were bugs in the tests, not in the host.)

## 5. `make` stopped building after `switch` (Part 3)
- Symptom: after adding `switch: $(OBJ_DIR)/port.o` to the Makefile, plain
  `make` built only `./switch`; `host` and `swctl` were missing.
- Found by: `ls host swctl` after the build.
- Cause: the first rule in a Makefile becomes the default goal. I had put the
  new line above `all:`, so it became the first rule.
- Fix: moved the extra-object lines below `all:`/`tsan:`.

## 6. Switch killed by SIGPIPE when a sender wrote to a closed socket (Part 4)
- Symptom: in tests/test_forward.py the whole switch process died
  (exit status -13 = SIGPIPE) right after a host that never reads sent an
  invalid frame; the other hosts were disconnected.
- Found by: the test "a host that stops reading, then misbehaves"; the switch
  log simply stopped after "protocol violation", and `poll()` returned -13.
- Cause: the receiver of that port ends the connection with shutdown(fd,
  SHUT_RDWR) to wake its sender, which was blocked in write(). The write then
  fails, and writing to a socket that is shut down raises SIGPIPE, whose
  default action terminates the process. The roadmap ignores SIGPIPE in Part 7,
  but Part 4 is where the switch first writes to sockets.
- Fix: main() now calls sigaction(SIGPIPE, SIG_IGN) before any thread is
  created, so the write returns EPIPE and the sender handles it.

## 7. `port_reset` used `atomic_init` on fields that other threads read (found in Part 5)
- Symptom: none seen in any test. Found by reading `port_reset()` before
  enabling port reuse.
- Cause: `atomic_init()` is an ordinary (non-atomic) initialisation. It was fine
  while a slot was reset once at start-up (Parts 3-4), but with Part 5 the reaper
  resets a slot that other threads may read through the atomic `mirror_to` and
  `mac_count` fields (Parts 6 and 17). A plain write racing with an atomic
  read is a data race (undefined behaviour in C11).
- Fix: `port_reset()` now uses `atomic_store()`.

## 8. The reaper did not flush a port's MAC entries (Part 6)
- Symptom: `tests/test_mactable.c` ("real ports": receiver + reaper + MAC table)
  failed: after a host left, `mac_check_counts` reported "port 2: mac_count is 0 but
  the table has 1 entries". A dump of the table showed every entry still present.
- How it was found: the unit test. The first version of the integration test in
  `tests/test_mac.py` did NOT notice: a stale entry that points at a port that is no
  longer UP makes `forward_frame` flood, which is the same visible result as "unknown MAC".
- Cause: the call `mac_flush_port(id)` was never added to `teardown_port()`. My edit
  script replaced the old comment with a string that did not match the file's wording,
  and `str.replace` fails silently when nothing matches. (The `#include` edit did match,
  so the file looked changed.) `port_reset()` still zeroed `mac_count`, hiding the
  mismatch until `mac_check_counts` compared it with the table.
- Danger if it had stayed: a new host that gets the freed port would receive the unicast
  traffic of the previous host (stale MAC -> port entry).
- Fix: `mac_flush_port(id)` is called in step 6 of the teardown. The integration test
  now connects a NEW host to the freed port and checks that traffic for the old MAC is
  flooded, not delivered to the new host; I verified that this test fails when the call
  is removed. All the other edits of that script were checked with grep afterwards.

## 9. Test fixes in Part 6 (no switch bug)
- The LRU test ran on the 3-second-aging switch with sleeps between probes, so entries
  could legitimately expire during the test (flaky). It now uses a flooded marker frame
  after each probe instead of sleeping.
- The 100,000-frame head-of-line test compared timings with bounds that were tighter than
  the machine's run-to-run noise. Measured: the committed Part 4+5 switch took 3.3-6.3 s
  for the same flood (median 3.58 s), Part 6 took 3.4-7.0 s (median 3.89 s), about 9%
  more in the median. The bounds are now 2.5x + 2 s for the time and 0.6x for the counts.
- "30 connections reset after connect" (Part 5 test) read its counters before a slow
  (TSan) switch had accepted anything; it now waits until the counts are stable.

## 10. Unlocked reads of port state in the unit tests (found in Part 7)
- Symptom: `build/test_port_tsan` exited with status 66 after printing "ALL PASSED"
  (the test redirects stderr to a log file and deletes it, so the report was invisible;
  `TSAN_OPTIONS=log_path=...` showed it): a data race on `sw.ports[1].state`, read by
  the test's main thread at `EXPECT(sw.ports[1].state == PORT_UP)` and written by the
  receiver in `request_down_locked`.
- Cause: the test read `state` without `ports_lock`. The race was already there in
  Part 6; TSan reported it only after Part 7 added a field to `switch_t`, which changed
  the layout of the global `sw` (the Part 6 sources passed 25+ runs).
- Not a switch bug: the switch always reads/writes `state` under `ports_lock`.
- Fix: `port_state(id)` helper in test_port.c and test_mactable.c that takes the read
  lock; all comparisons of `state` in those tests use it.
- Lesson: a test that "passes" can still exit non-zero; run_all.sh checks exit status.

## 11. Shutdown predicate was only an atomic (Part 6 -> Part 7)
- The aging thread and `switch_wait_ms()` waited on `shutdown_cond` testing the atomic
  `shutting_down`. Not wrong in practice, but the roadmap requires a plain predicate
  protected by the condition variable's mutex. `shutdown_requested` (under
  `shutdown_lock`) is now the predicate; the atomic stays as a hint for the accept loop.

## 12. Part 8 test-code bugs (no switch bug found in Part 8)
All four were mistakes in the new shell tests, found while writing them; the switch sources are unchanged.
- `broadcast.sh` hung: a bare `wait` after the four background sender loops also waits for the
  background switch and hosts, which never exit. Fix: `wait "${senders[@]}"` (only the loops' PIDs).
- A host name used twice (`midframe_disconnect.sh` re-used `D`, `slow_host.sh` and `many_hosts.sh`
  started a second set of hosts with the same names): `mkfifo` failed with "File exists". The first
  version of `host_spawn` ignored that error, so `midframe_disconnect.sh` PASSED although its loop had
  not really run (a false pass). Fix: `host_spawn` now fails the test when the FIFO cannot be created, and
  every host gets a unique name.
- `slow_host.sh` asserted that B and D receive at least 2000 of 8000 frames. Under valgrind the switch is much
  slower than the sender, the fast hosts' own queues overflow too, and B/D got 1846-2318 (flaky). The
  roadmap's criterion is relative ("about the same as without C"), so the script now runs the flood
  twice (C reading, C --noread) and compares times and counts (>= 60 %, same as tests/test_forward.py).
- `many_hosts.sh` expected 8 excess hosts to be answered SWITCH FULL within 30 s. The accept thread handles ONE
  waiting host at a time (roadmap Part 5), 5 s each, so the 8th would be answered after about 40 s.
  Not a switch bug (documented design). The script uses 32 hosts on 30 ports (2 excess, 10 s) and checks the
  serial behaviour (took >= 9 s).

## 13. gdb cannot attach on the development machine (Part 8)
`gdb -p PID` fails with "Could not attach to process ... ptrace_scope" (kernel.yama.ptrace_scope = 1: only a parent may
attach). `tests/lib.sh` still tries gdb when a switch does not exit after SIGINT, and falls back to printing each
thread's name and kernel wait point from /proc/PID/task. With `sudo sysctl kernel.yama.ptrace_scope=0` the real
"thread apply all bt" is printed. No stuck switch occurred in any test run; the path was tested with a fake
program that ignores SIGINT.

## 14. slow_host.sh absolute floor failed under valgrind (Part 8 repeat runs)
- Symptom: `VARIANT=valgrind ./tests/run_all.sh` failed `slow_host` in 2 of 3 repeat runs on
  "B and D received something substantial (>= 1000 each)": B/D got 928-1396 of 8000 frames. All the relative checks
  (flood time 1442 ms vs 1449 ms without the slow host; B and D at >= 60 % of their baseline) passed in every run.
- Cause: I had added a fixed minimum count. Valgrind makes the switch 20-50x slower than the sender, so the fast hosts'
  own queues overflow and they legitimately receive only about 12 % of the flood. The same kind of mistake as item 12
  (a number that depends on the speed of the variant). Not a switch bug.
- Fix: the fixed floor is replaced by "received more than 0"; head-of-line blocking is judged by the relative checks only.
- Also found: valgrind runs that are killed (SIGKILL by a stopped test run) leave /tmp/vgdb-pipe-* files; they were
  removed by hand. A run that ends normally leaves none.
- Second failure of the same script under valgrind: the relative count check (>= 60 % of the baseline) failed once
  (B: 1071 vs 1954). Identical valgrind runs differ by a factor of 2 in how many frames B receives (928 ... 2096), so a
  60 % bound is inside the noise there. The count threshold is now 30 % in every variant (a 60 % bound then failed once in 16 normal runs too:
  D 4340 vs 7392, so the counts are noisy everywhere, not only under valgrind). The time check (A's flood is not slower than 2.5 x the baseline + 2 s) is unchanged and passed in all
  runs; it is the actual head-of-line test.

## 15. Part 3 test assumed that every received frame is logged (found in Part 9)
- Symptom: `tests/test_switch.py` "3000 frames from one host" failed intermittently ("all 3000 frames logged") as soon as
  the logger was asynchronous.
- Cause: not a switch defect. The test counted the per-frame "port 1: RX" lines to count frames. With the Part 9 logger a
  fast flood overflows the 1024-record ring and the excess lines are DROPPED on purpose and counted ("N log messages
  dropped"). Before Part 9 the receiver was slowed down by synchronous logging, so every line was written.
- Fix: the test now checks that each frame is either logged or counted as dropped (logged + dropped >= 3000) and that
  none is logged twice. Passed 3 of 3 afterwards, and in the full suite.
- The same assumption was in `tests/test_forward.py` (found by the first full run of the Part 9 suite: it failed, and
  took 1048 s because each wait ran into its 240 s timeout):
  * `run_hol` and `test_dropped_frames_are_counted_and_released` waited until N lines "port X: RX" were logged; with
    drops that never happens. They now use `processed()` = lines written + lines reported as dropped.
  * Absolute counts that only held because synchronous logging throttled the sender: "B and D received >= 50 % of
    100,000 frames" (they now get 30-75 %, identical runs differ by 2x: the fast hosts' own 64-frame queues overflow
    now that A's receiver is faster) and "B received >= 2000 frames" (986 once). Replaced by the real criteria: the flood
    with the frozen host takes about as long as without it (unchanged, never close to failing), B and D keep >= 40 % of
    their baseline and >= 10 % of N, and B still receives a marker frame after the other host was killed.
  * "the first drop was logged exactly once" became "not logged twice": under a flood the logger can drop that one
    warning line itself (a real consequence of the design, see docs/Part9_Explanation.txt, limitations).
  * test_forward.py passed 5 of 5 runs afterwards.

## 16. Mistakes in the first versions of the Part 9 flood test
- `sendall()` of 100,000 frames returned in 0.00 s: it returns when the kernel has the data, not when the switch has
  processed it, so "time to send" measured nothing. The test now sends a marker frame to a second host after the flood
  and measures the time until that host prints it (= the switch has handled every frame before it).
- SIGINT was sent 0.5 s after the send, before the switch had read everything: 12,211 frames were neither logged nor
  dropped (they were never read: the connection was torn down). Fixed by the marker: SIGINT only after the marker.
- The test assumed the raw sender gets port 3, but the port depends on which process connects first (TSan runs: 1 RX line
  counted). It now counts the lines by the sender's source MAC.
- Under TSan the hosts' hello broadcasts were sometimes sent before the (slow) switch had brought the raw sender's port
  up, so the helper waited for nothing. The hosts now announce again until the sender has seen both.
- Under TSan the baseline run (30,000 frames) drops nothing because the TSan switch is slow enough for the logger to keep
  up; the "drops happen" check is only made for the fast variants (the stalled-sink check requires drops everywhere).

## 17. valgrind --track-fds reported the test's own descriptors (Part 9)
- `test_log` under `valgrind --track-fds=yes --error-exitcode=1` reported "1 errors": fd 2 after the test redirected stderr
  with dup2 to capture it. Then, after moving the redirection into a forked child, the child (also run by valgrind)
  exited with open descriptors and its non-zero status failed the test (3 of 3 runs).
- Fix: the child closes the descriptors it opened before `_exit`. The test now passes under valgrind with all flags
  (0 errors in both processes, 0 bytes in use at exit), 3 of 3 runs.
- Not a logger defect: the logger's own file descriptor is closed by log_stop() (the end-to-end valgrind test checks that no
  descriptor is open at exit).

## 18. test_mactable_tsan failed at "mac_size() == 6" (test synchronization, not a switch race)
- Symptom: `make test` once failed `build/test_mactable_tsan` (2 failures, lines 920/921 of tests/test_mactable.c):
  `mac_size() == 6` and the per-port counts (2, 1, 3). The normal build never failed. Not reproducible on an idle machine
  (0 of 40 runs, both builds); reproduced under CPU load: TSan 1 of 30 and 1 of 48 runs, normal build 0 of 30.
- Diagnosis: a temporary print at the failing line showed `mac_size=5 counts=2,1,2 stats_in=2,1,3`: the receiver of port 3 had
  counted its third frame (STAT_IN = 3, so the test's `wait_in(3, 3)` returned) but had not learned its source MAC yet.
  src/port.c `receiver_main` does `stats_inc(STAT_IN)`, formats the addresses, logs the RX line, and only then calls
  `forward_frame()`, which learns the MAC. STAT_IN means "frame READ", not "frame processed". A slow or loaded run (TSan)
  can look at the table inside that window.
- Not an implementation race: every shared value involved is an atomic or is read under its lock (ThreadSanitizer reported nothing),
  the table is consistent at every moment, and the switch is correct; the test used a proxy counter that is incremented one step too early.
- Fix (tests/test_mactable.c only): a `wait_macs(port, n)` helper that waits (bounded, 10 s) until the port's `mac_count` has reached n, used
  before the unchanged assertions. `mac_count` is incremented while the entry is inserted, after the table's own count, so once it
  has reached n, `mac_size()` is final as well. The assertions were not weakened: if a MAC were never learned the wait times out and the check fails.
- Result: `test_mactable_tsan` 0 failures in 150 runs under heavier load (4 busy loops on 2 cores), where the bug appeared in
  about 1 of 30-50 runs before; 353 checks now (one more than before: the new wait).
- Also (Part 9 test, same full run): `test_logger.py` asserted that the baseline 100,000-frame flood must make the logger drop messages. It
  depends on the relative speed of receiver and logger: in one run the logger kept up (all 100,001 lines written, 0 dropped, nothing lost) and
  the assertion failed; over 5 later runs the baseline dropped 0, 761, 2887, 3129, 5329. Removed; the number is only reported. What
  the requirement needs is still asserted: written + dropped >= frames sent, the stalled-sink run is not slower, and the stalled run
  reports drops in every variant.
