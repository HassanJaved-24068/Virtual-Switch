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
