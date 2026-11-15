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
