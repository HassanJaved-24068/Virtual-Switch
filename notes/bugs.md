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

## 19. mac_foreach reported negative ages (found by the Part 10 dump test)
- Symptom: the new `test_dump_file` in tests/test_mactable.c (4 threads learning/aging while the main thread dumps 60 times) failed
  under valgrind: dump lines like `2 02:00:00:00:00:38 1 -1`, ages from -1 to -11 seconds. It passed under the normal and TSan builds
  (they are fast enough that the clock does not move far during a walk).
- Cause: `mac_foreach` (the walk used by `mac_dump_file`, the SIGUSR1 dump) read `now_mono()` ONCE before visiting the buckets. An entry
  that traffic refreshed while the walk was still copying later buckets has `last_seen` later than that timestamp, so
  `age_ms = now - last_seen` was negative. On a real switch the dump takes milliseconds, so the error is small, but a negative age
  is wrong whenever it happens.
- Fix (src/mactable.c): the clock is read after each bucket's lock is taken, once per bucket. Learners refresh `last_seen` under that same
  lock, so every entry of the bucket was last seen before that moment and its age cannot be negative. No clamping (a clamp would hide a
  real ordering error). Existing users of mac_foreach are unaffected.
- Verification: test_mactable under valgrind (`--track-fds=yes --error-exitcode=1`): 366 checks, 0 failures, 0 errors (it failed 1 of 366 before);
  normal 15/15 and TSan 15/15 runs clean.

## 20. Mistakes in the first versions of the Part 10 tests (no switch bug)
- test_config.c built long strings with memset and then called strlen without writing a terminating NUL (undefined behaviour in the test);
  replaced by a `fill()` helper that always terminates. One expectation was off by one: an unterminated last line of 511 characters is
  correctly "line too long" (the limit is 510); the test now checks both sides of the exact boundary.
- test_config.c: for trunk_port the value 9 on an 8-port switch is inside the key's table range but above the port count: it has its own
  message ("only 8 ports"), the range test now expects that one.
- test_config_signals.py: a result tuple used with `%` raised TypeError (fixed with `str()`); a draft had checks that could not fail
  (`... or True`) and a missing precondition (B must send a VLAN 1 frame for the expected four entries): removed / fixed before the
  first complete run.

## 21. Mistakes in the first versions of the Part 11 tests (no switch bug)
- "output queue full" in the log does not prove that the sender thread of the port is blocked in write(). With the default socket buffers (a few MB on
  loopback, even with a small SO_RCVBUF on the receiving side) the switch accepted the whole first flood of ~1,000 frames, the rings drained again when
  the sender stopped, and the marked frames left in arrival order (n1 h1 n2 h2 ...). The order check passed once by luck and failed in other runs.
  Fix: the receiving host has a 4 KB SO_RCVBUF, the flood is 17 MB (far more than any buffer), the test asserts that thousands of frames were dropped, and
  only then sends the marked frames. 5 of 5 normal runs, 2 of 2 TSan runs and a valgrind run passed afterwards.
- The unit test hung when I mutated the queue to ignore priorities: the random-order test asked for a pop that the queue could not give (the model thought
  frames were queued that the mutated queue had refused) and outq_pop sleeps on an empty queue. A failing test must fail, not hang: test pops now check
  the queue first (-2 = empty) and `alarm(300)` is a backstop. With the mutation the test now reports 479 failed checks and ends.
- The bad_prio assertion looked for the text "priority 3 " only, so a switch that logged a line for EVERY bad frame (priority 3, 9, 255) still passed. Found by
  the mutation check; the test now counts all bad_prio lines of the port and requires exactly one.
- Existing fixtures assumed one class: test_queue.c `mk()` (frame_new() has priority 0, now the high ring; fixture sets prio 1) and test_mactable.c `queued()`
  (counted the normal ring only; now sums the three). test_config_signals.py asserted "stored" for wrr_weights/prio_aging_ms, now applied. All adapted without
  loosening any assertion (docs/Part11_Explanation.txt section 7).

## 22. tests/test_forward.py failed on a busy machine: four weak checks (test-side; found from the user's `make test` output)
- Symptom: the user's `make test` (Parts 1-11) failed only in `tests/test_forward.py`: 8 "host N: ... intact" checks and "B kept receiving (961
  frames)". The head-of-line test took 7.78 s instead of ~2.3 s: the machine was loaded (my own Valgrind stress loop with busy-loop CPU hogs overlapped
  the start of that run). A quiet re-run passed everything.
- Reproduced on demand: with 4 busy loops on the 2 cores the UNFIXED test failed in 3 of 4 runs (once with the same 9 failures as the user's file).
- Not a switch defect. Four checks measured the speed of the machine or an ordering the switch no longer promises:
  1. "vlan 4094 prio 2 / zero-length / 1500-byte payload intact": three frames of priorities 2, 0, 1 were expected to arrive in the order they were
     sent. Since Part 11 the sender takes high before normal before low, so when the three are queued together (slow machine) they leave reordered.
     Fix: each frame must arrive exactly once and intact (all fields), matched by content; the last three frames must be exactly these three; the order
     between different priorities is no longer asserted (it is tested in test_qos.c / test_qos.py).
  2. "B kept receiving: >= 2000 frames" after a host died: a count that only measures how fast the machine is. Fix: B prints its frames, and a marker frame
     sent after the death, in the HIGH priority queue (B's normal queue is full of flood frames and would drop a normal one), must arrive; and B's received
     count must have grown.
  3. Head-of-line test: "B and D got about as many frames as in the run without C" compared the counts of two runs made one after the other, i.e.
     under different load (13,150 vs 32,548 frames), and also differs by design: with the frozen host the switch has less to do, A floods faster (3.0 s
     against 5.5 s) and B's and D's own queues overflow more. Fix: both scenarios run AT THE SAME TIME on two switches; the check compares the COMBINED RATE
     (frames per second) of B and D with a lenient bound (0.25). Per host is too noisy: identical hosts of the same run received 8,342 and 29,964 frames
     because one host process was starved of the CPU. Real head-of-line blocking stops the fast hosts (a few percent) or stalls A's flood (the time check).
  4. "B and D received at least 10 % of the frames" (my own floor from Part 9): 8.2 % on a busy machine. It is now a 1 % sanity floor; the comparison above is
     the real check.
- Results under the same load (4 busy loops on 2 cores), runs passing: unfixed 1 of 4; after fixes 1-2 and 3 (counts, concurrent): 6 of 8; after fix 4: 10 of 12
  (the remaining failures were the per-run count comparison: counts depend on A's speed); rates: 11 of 14 (per-host noise); combined rate: 16 of 16. Quiet machine:
  103 checks, 0 failures every time (it was 99 checks; +4 from the new checks).
- The fixed checks still catch real faults (mutation check, sources restored): a queue that drops high-priority frames fails the "intact", "exactly these three"
  and "received the special frames" checks; a queue that waits for space instead of dropping (head-of-line blocking) fails "A's receiver processed all 100000
  frames" and the switch does not shut down.
- Left as it is: test_dropped_frames_are_counted_and_released asserts a drop counter >= 1000 after 10,000 frames of 1400 bytes to a host that never reads; that
  depends on how much the kernel buffers (a few MB), not on the load. It passed in every run.

## 23. Part 12 tests: three test-side mistakes (no switch defect)
- test_rate.c: a producer leaked a frame whose push failed (the caller keeps its reference when try_push returns false). Found by the frame_live_count check.
- test_rate.py: the reader thread put a 0.5 s timeout on host A's socket, which the flooder also uses; under valgrind sendall() timed out. Now select().
- test_rate.py: the bounds used the nominal window (5 s) instead of the real one (valgrind made the last sendall last longer): 324 frames in a longer window
  looked like a violation of "at most 310". Bounds now use the measured window.
- Also: the Part 10 test that asserted "stored; ... until Part 12" was updated to the Part 12 behaviour (docs/Part12_Explanation.txt section 4).

## 24. Part 13: old tests that assumed the VLAN of a frame is kept, and test-side mistakes (no switch defect)
- Part 13 is a deliberate behaviour change (access ports overwrite the VLAN; port.N.vlan and trunk_port are applied), so tests written for Parts 4-10
  failed: test_forward.py, forward.sh, broadcast.sh, test_port.c, test_mac.py (2 tests), test_mactable.c, test_config_signals.py (3 places). Each was
  adapted with the user's approval, keeping its intent (docs/Part13_Explanation.txt section 4). One adaptation also corrected a hidden assumption: a frame
  written with vlan 4094 for a MAC that is learned in VLAN 1 is now a unicast, not a flood.
- test_vlan.c: the wire image of a test frame was only partly initialised (valgrind: "conditional jump depends on uninitialised value"); a failed check
  dereferenced a NULL frame (segfault instead of a failed check); a mutation showed that nothing tested a stale unicast entry (test added).
- test_vlan.py: the host index was off by one in four unicast checks.
- Open: tests/test_mac.py failed two checks in one run of ten (an aging step, output not kept); 12 later runs passed. Cause not found.

## 25. Part 14: design flaws caught before they shipped, and test-side mistakes (no defect in a released behaviour)
- Design: a first version created the segment early in main(). A second switch that failed to start (TCP port in use, bad log file) would have
  replaced the running switch's segment and then unlinked it on exit. Fix: stats_init runs after the listener, the eventfd and the log file; the
  unlink at shutdown / exit only happens if the name still refers to OUR object (device + inode). Tests: test_stats.c "an older switch does not
  unlink the segment of a newer one", test_shm.py test 7.
- Design: a start-up error that ends in exit() (CHECK, bind) would leave /dev/shm/vswitch_stats_P behind; an atexit handler now unlinks the name.
- test_stats.c: children forked by the test inherited four pipe descriptors, which valgrind --track-fds reported as 4 errors per child (exit status 1);
  the children now close them. stdout is line buffered so a forked child cannot replay the parent's unflushed output.
- Equivalent mutant: removing PTHREAD_PROCESS_SHARED while keeping ROBUST is not detectable on glibc (robust mutexes use shared futexes).
- Item 24 (test_mac.py flake): did not reappear in the full make test of Part 14 (normal, TSan and valgrind runs). Still open, cause unknown.
- Known limitation: the segment name is fixed, so two simultaneously running switches cannot both be visible (docs/Part14_Explanation.txt section 6).

## 26. Part 15: one switch defect found by a test, and a gap closed before it shipped
- Defect (admin.c): a request longer than 256 bytes was answered with "ERR line too long" and the connection was closed with the rest of the request
  still unread. close() with unread data resets the connection, and the client saw ECONNRESET instead of the reply. Found by test_admin.py (1000 bytes,
  no newline). Fix: every path of serve() now ends with shutdown(SHUT_WR) and a bounded non-blocking read of what the client sent.
- Gap: a NUL byte inside a request ("show mac\0junk") ended the C string and passed as "show mac". read_line() now refuses it ("bad character").
- Gap: the reply loop waited up to 2 s on a client that never reads, which would have delayed a shutdown. It now polls {client, wake_efd}.
- Display: "show ports" for a port without a host used the port's stale defaults (the VLAN and trunk role are copied from the configuration when a host
  connects, port.c); it now shows the configured values for such ports. The behaviour of Part 13/14 itself was not changed.
- Test-side: test_admin.py read one recv() where the reply comes in pieces; the first --watch interval is short by design (it ends at the next whole
  second); /proc/<pid>/maps lists the mappings by address, not by file offset. No switch defect.
- Test change: test_shm.py expected num_ports = 64 in the segment header; the switch now stores its real number of ports (4).
- Test change (Part 15, test_switch.py): the exact thread list now includes the "admin" thread (7 fixed threads instead of 6) and the descriptor
  bound after "many hosts come and go" is 7 instead of 6 (the admin listening socket; the wake eventfd is shared). Intent unchanged: no thread or
  descriptor leaks per host.

## 27. Test harness: orphaned switches after a failed scenario; C5 "intermittent loss" (both test-side; no switch defect)
**A. test_admin.py left switches running when a scenario raised (found during the end-to-end verification: two idle `./switch` processes, 55 min old).**
- Cause: every scenario ended with a plain `sw.stop()` and no try/finally; the raw UNIX sockets and the `swctl --watch` children were never registered
  anywhere; the main block only removed the temporary directory; a switch whose constructor raised ("did not start") was never reachable because the
  object did not exist yet. A Python traceback therefore ended the script with the switch (and its log files) still alive.
- Fix (tests/test_admin.py): a registry of everything a run creates (`STARTED` switches, registered BEFORE the constructor runs; `CLIENTS` sockets, hosts,
  swctl children), `cleanup()` and `run_scenario()` (cleanup in a `finally`, the original exception propagates unchanged, a cleanup error is printed and
  counted but never replaces it, every step runs even if an earlier one fails), `stop_proc()` (SIGTERM, wait, SIGKILL on timeout, wait = reaped; safe if it
  already exited), per-scenario exception handling in `run_tests()`, SIGTERM of the test script itself turned into SystemExit. Only the exact processes the
  run started are signalled (no pkill by name). test_e2e.py uses the same mechanism (its private tracking was removed).
- Proof: tests/test_harness_cleanup.py (38 checks, in `make test`): normal completion with and without sw.stop(); an AssertionError, a RuntimeError and a
  KeyboardInterrupt in a scenario; a real ./host child; a swctl --watch child; a switch that ignores SIGTERM (killed after the timeout, reaped, no
  zombie); a switch that never prints "listening" (constructor raises while the process lives); an unrelated switch of the same program name keeps running;
  a failing cleanup step neither hides the original exception nor skips the rest. Mutation check: with the switch-stopping step disabled 11 of the 38
  checks fail.
**B. C5 (tests/test_e2e.py) failed in about 1 of 5 normal runs: 1-60 frames lost on a READING port.**
- The switch was not at fault and neither was TCP or the counting. Instrumented C5 (sequence numbers in every frame, per-port counters before/after, TCP
  queues, per-thread scheduler statistics) over 59 runs of the original load: in every run `out + dropped` = frames broadcast to the port, the reader received exactly `out`,
  and the missing sequence numbers = the switch's `dropped` counter (0 violations of these identities). So frames are lost only by being counted as
  dropped by the bounded output queue (queue_capacity 32) of a reading port; they are not lost in TCP and not miscounted.
- Why the queue filled: a drop needs the consumer to be delayed for longer than queue_capacity / arrival rate. At the original load (about 2100
  frames/s into each reading port) that is 15 ms. Failing runs lost a contiguous burst, often the SAME sequence numbers on 2-3 ports at once (a common stall),
  and a thread-level scheduler log showed the switch's sender threads (`tx-port3`: ran 360 ms, waited 1160 ms for a CPU in 8 s) and the Python readers both
  waiting for a CPU on this 2-vCPU VM (system 36-69 % idle, steal 0 %: scheduling latency, not saturation). A faster reader (recv_into, offsets) did not remove it
  (4 of 12 failed, legacy reader 6 of 15), and 8 MB receive buffers on the readers did not either (2 of 12; legacy reader + 8 MB 5 of 12): the reader was not the bottleneck.
  Lowering only the offered load did: about 840 frames/s: 0 of 10 runs with a drop on ports 1-4 (the unread port still dropped >= 4332); 357 frames/s: 0 of 10;
  at 186 frames/s the unread port no longer saturated within 8 s (so that is too slow for C5).
- Classification: test defect (an environment-dependent assumption, "a reading host never falls behind a 32-slot queue", at a load that leaves 15 ms of
  tolerance) plus a test completion race (fixed 1.5 s sleep before reading the counters; with TSan the backlog takes about 5 s to drain). The switch did what the
  bounded-queue policy says: drop and count, never block the receiver.
- Fix: C5 now floods at about 840 frames/s (38 ms of tolerance), with every original assertion unchanged and strict; completion waits for the actual condition
  (frames received = the switch's `out` counter, stable for 1 s) instead of sleeping; new exact accounting checks; C5b keeps the original high rate and
  asserts what is true at any rate (accounting identities, saturated port drops, senders not blocked), reporting losses on reading ports instead of failing.
  Queue capacity, the number of hosts and the assertions were not changed to hide the problem.
- Confirmation: C5 30 of 30 runs passed with 0 drops on ports 1-4; C5b 8 of 8; under TSan C5 5 of 5 and C5b 4 of 4 (drain at most 1.6 s). 30 runs
  show it is rare at this load, not that it cannot happen: a long enough scheduling stall would still drop frames, by design.
- Not measured: the switch does not expose queue occupancy or a high-water mark, so "the queue was full" is inferred from the dropped counter and the
  burst lengths. Which of the switch's sender wake-up delay or the reader delay caused each individual burst was not separated; what was separated is that
  neither a faster reader nor a larger receive buffer removed it and that a lower frame rate did.
**C. tests/test_rate.c "cpu < 50.0" flaky under valgrind (Part 12 unit test) - FIXED (approved by the user).**
- `make test` stopped at `valgrind ... ./build/test_rate`: "FAIL line 223: cpu < 50.0 / sender blocked for 300 ms, cpu used by it: 75.05 ms" (twice in full runs, 3 of
  10 standalone runs). The test measured the sender thread's CUMULATIVE CPU since its creation. Valgrind's thread start-up costs 28-83 ms (cumulative CPU at
  100 ms, 15 runs), so the 50 ms bound was inside the noise. A spinning thread would use about 300 ms; the code (src/queue.c, unchanged) is fine.
- First attempt, as approved: a 150 ms bound under valgrind on the cumulative value: still 2 of 15 runs failed (170.6, 172.6 ms): the noise is wider than my first 10-run
  sample (35-63 ms) had suggested.
- Fix: the CPU is measured over a 300 ms window that starts after a 100 ms settling time (the same intent: blocked, not spinning); the bound is 150 ms under
  valgrind (detected through `LD_PRELOAD` containing `vgpreload`, no valgrind headers needed) and 50 ms for the normal and TSan builds. The settled window costs
  5-63 ms under valgrind (40 runs: max 62.6 ms), 0.02-0.19 ms in the normal and TSan builds. Mutation check: a busy-loop wait (timeout 0 in `outq_pop`) is caught in both
  builds (294 ms in the normal build, 297 ms under valgrind, against limits of 50 and 150 ms). Result: 0 failures in 25 consecutive valgrind runs (max 38.9 ms) and a
  green full `make test` (window 13.8 ms under valgrind).
**D. test_admin.py::test_bad_clients failed once in the final run: `BlockingIOError: [Errno 11]` on connect() to the admin socket (test race, no switch defect).**
- Cause: after a burst of 15 broken clients the test connected with a socket that already had a Python timeout. Such a connect() is non-blocking; the admin
  thread serves one client at a time and listens with a backlog of 8, so while the backlog is full connect() answers EAGAIN. Reproduced against a real switch
  with the same burst: 19 of 150 connects failed with the old method, 0 of 150 with a blocking connect (SO_SNDTIMEO 10 s, then the read timeout).
  swctl itself connects blocking, so it waits for a slot and is not affected.
- Fix: `uconnect()` in test_admin.py (blocking connect bounded by SO_SNDTIMEO) at the five places that connected after settimeout().
- Property worth knowing (not a defect): more than 8 clients connecting at once while the admin thread is busy make a NON-blocking connect fail with EAGAIN.
**E. test_admin.py::test_protocol failed once: `BrokenPipeError` in sendall() of a 100 KB request (test race, no switch defect).**
- The switch reads 256 bytes, answers "ERR line too long" and closes (draining what is already queued); a client that is still inside one sendall() of 100 KB
  can get EPIPE. Measured against a real switch: 36 of 400 sends failed, and in all 400 cases the reply "ERR line too long" WAS received.
- Fix: raw() tolerates EPIPE/ECONNRESET on send and still reads the reply. The assertion was tightened, not loosened: the 100 KB case used to accept an
  empty reply ("or big == ''"); it now requires "ERR line too long".
**F. Historical harness: ts.Switch left its process running when the switch did not start in 10 s; one unexplained slow-machine episode.**
- `tests/test_switch.py::Switch.__init__` raised "switch did not start" without stopping the process it had just started. One slow start under valgrind therefore left an
  orphan, and the orphans slowed every following run, which then also failed to start (seen as 7 cascading failures of `test_qos.py valgrind`, 3 orphaned valgrind
  switches, terminated by exact PID). Fix: the constructor now stops exactly its own process (SIGTERM, 15 s, SIGKILL), closes and removes its temp files, then raises.
  Checked with a fake switch that ignores SIGTERM: RuntimeError after 25 s, no process left.
- **CAUSE UNKNOWN: the `test_qos.py valgrind` failure and the slow episode around it.** Original evidence, preserved verbatim (the raw log files were lost when the session's
  scratchpad directory was cleared; this text is copied from the session transcript of the third full `make test` of the follow-up, 11 min 14 s, rc=2):
  ```
  === python3 tests/test_qos.py valgrind ===
  6. valgrind: priority scheduling, bad priorities, reload: no errors, no leaks, no descriptor left open
    FAIL: valgrind: D, which reads, got all marked frames: the switch has forwarded them
  11 checks passed, 1 failed
  FAILED: python3 tests/test_qos.py valgrind
  ```
  The check (tests/test_qos.py, `run_round`, lines 174-184) sends marker frames from host A and waits up to 30 s for the fast reader D to have seen all of them, with
  `queue_capacity = 8`. Immediately afterwards, standalone runs of the same test gave: run 1 passed in 57 s (it normally takes 16-17 s), runs 2-8 failed in about 16 s each with
  `RuntimeError: switch did not start` (valgrind had not printed "listening" within the 10 s of `ts.Switch.__init__`; valgrind normally needs 2.0-2.6 s, 6 measured starts), and left 3
  orphaned valgrind switches (pids 268539, 268577, 268610, 1:47-2:16 old) which were terminated by exact pid. After that 8 of 8 standalone runs passed at 16-17 s each.
- **Why it was not reproduced:** it passed in 8 of 8 standalone valgrind runs, in 6 of 6 runs with two CPU-hog processes on the 2-CPU VM (17-19 s each; so plain CPU
  contention does not trigger it), in the next full `make test` (green) and in the final one. The slowness that came with the failure (57 s instead of 16 s) is the only trace of
  what was different, and its cause is unidentified (an earlier note called it "heavy load"; that is an interpretation, not a measurement). A plausible mechanism for the
  failed check (the marker frames dropped on the 8-slot queue of the reading host while valgrind was slow) was NOT shown: the test prints no counters, and the log was not kept.
  Status: unexplained, not reproduced, test and switch unchanged. If it recurs, the first things to capture are the switch's `dropped` counters (`swctl stats`) at the moment of the
  failure and the run time of the test.

## 28. Part 16: a data race in the new `admin_up`, found by TSan (genuine defect, fixed before the commit)
- Symptom: `python3 tests/test_reconfig.py tsan` ended with status 66 and two ThreadSanitizer reports ("data race", `port_start` / `teardown_port` writing
  `port.state` against `admin_up` reading it). The roadmap stress script under TSan did NOT show it; only the targeted tests that call `up` on ports that are
  not disabled while hosts connect and leave did.
- Cause: in the error path of `admin_up` the reply text was chosen with `p->state` AFTER `pthread_rwlock_unlock(&sw.ports_lock)`:
  `return p->state == PORT_FREE ? ... `. The state changes as soon as the lock is released.
- Fix: the state is copied into a local variable while the lock is held, the reply is chosen from the copy (src/port.c). Three further TSan runs: 0 reports.
- Test-side lesson (mutation checks of the Part 16 tests): (1) "FREE shutdown forgets sem_trywait" was invisible to the first version of test 1 (the third
  host gets 'switch full' either way), only to its timing: it now asserts that the host waited port_wait_timeout. (2) "shutdown answers before the teardown has
  finished" cannot be seen by polling (the teardown is faster than the next round trip): the test now asserts the LOG ORDER (the reaper's "teardown
  complete" precedes the admin's answer), which is deterministic. All five mutations are caught.
- Working-practice note: a first mutation attempt on the real source tree broke `src/port.c` (a mutation text that opened a `/*` comment) while the backup
  directory had been cleaned up by the session; it was restored by reversing the exact replacement and verified by a build and the tests. Mutation checks now run
  in a separate copy of the project.

## 29. Part 17: no defect in the code found by the tests; one design correction and two test-side mistakes
- Design: the first version did not queue a mirrored copy to a sniffer that had already got the frame through the normal forwarding (no duplicates). The smoke test showed that
  `mirrored` then stayed at 0 for broadcasts when the sniffer shares the VLAN, against the roadmap's "the mirrored counter rises"; the roadmap's algorithm has no such rule (only
  "s is not the frame's ingress port"). Removed; the duplicate for a sniffer inside the VLAN is documented (README, forward.c, docs/Part17_Explanation.txt).
- Test (mutation harness): a mutation that left a parameter unused failed to compile under -Werror and the harness silently ran the binary of the previous mutation (a wrong "caught").
  Mutations that must compile now keep every variable used; the result of every mutation is the result of ITS OWN build.
- Test (test_mirror.c): the first null-safe version released only one of the two references to the shared frame (a == b), which leaked and failed the live-frame check; both are released.
