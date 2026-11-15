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
