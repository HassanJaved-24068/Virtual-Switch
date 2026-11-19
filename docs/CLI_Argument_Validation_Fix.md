# Fix: strict numeric command-line arguments (independent verification case CLI-01)

## 1. The original failure
Independent verification case CLI-01 (`verification/t_cfg.py`): `./switch +9000 4` and `./switch " 9000" 4` were **accepted** and the switch listened on 9000. Expected: exit status 1, an error message, nothing started.
Reproduced before any change (exit 124 = still running after the 2 s timeout).

## 2. Root cause
Five private copies of "`strtol`, check `end`, check the range" (`switch.c` `parse_range`, `host.c` `parse_long`, `supervisor.c`, `swctl.c -p`, and `bench`). `strtol` skips leading white space and accepts a `+`/`-` sign, so the checks that came after it never saw them.
The weakness was **not limited to the TCP port**. Reproduced before the fix (all accepted): the number of ports (`+4`, `" 4"`), the `--pair` port (`+Q`, `" Q"`), `supervisor` (`+P`), and the numeric arguments of `host` (port, `--flood`, `sendx` vlan and priority), `swctl -p`, `bench` and `bench_mac` (also `nan` / `inf` seconds). Two further related leniencies were found:
* `swctl` joins its words with spaces and the switch splits the line again: `swctl shutdown " 1"` was taken for `shutdown 1`.
* The configuration parser accepted `-0` (`rate_limit = -0`, `trunk_port = -0`): `strtol("-0")` is 0, which is in range.
Already strict and unchanged: the admin thread (`parse_num`: digits only, at most 6) and the configuration parser for `+`, white space and letters.

## 3. The correction (source files changed)
* `include/common.h`, `src/util.c`: one shared helper `int parse_uint(const char *s, unsigned long lo, unsigned long hi, long *out)`: only the characters `0`-`9` (at least one; leading zeros are digits), overflow detected **while accumulating** (no `strtol`, no wrap-around, not even for `ULONG_MAX + 1`), the value must be in `[lo, hi]` (`hi <= LONG_MAX`), returns 0 or -1 with `errno` = `EINVAL` (empty / NULL / not only digits) or `ERANGE` (too big / out of range); `*out` is untouched on error.
* `src/switch.c` (TCP port, number of ports, `--pair` port), `src/host.c` (port, `--flood`, `sendx` vlan and priority), `src/supervisor.c`, `src/swctl.c` (`-p`), `bench/bench.c`, `bench/bench_mac.c`: all use `parse_uint`; the private copies are gone. The documented ranges are unchanged. In the bench programs `seconds` (a decimal) must start with a digit, be finite and be in (0, 86400]; the seed is parsed by `parse_uint`.
* `src/swctl.c`: an argument that is empty or begins or ends with white space is refused (`" 1"`, `"1 "`). White space *inside* an argument is still accepted: `swctl 'show ports'` (one argument) is relied on by `tests/test_admin.py`. (My first version refused every argument that contained white space; the full `make test` caught the regression and the rule was narrowed.)
* `src/config.c`: a value that starts with `-` is "out of range" when the setting's minimum is not negative, so `-0` is refused like `-5`.
* Documentation: `README.md`, `notes/bugs.md` item 39.

## 4. Regression tests added (nothing was weakened or removed; the CLI-01 vectors of the independent suite are kept in `verification/t_cfg.py`)
* `tests/test_parse.c` (unit, 55 checks; in `make test` also under valgrind): the TCP-port table (`9000` ok, `+9000`, `-9000`, `" 9000"`, `"9000 "`, `09000` ok, `0`, `65535` ok, `65536`, `abc`, `9000abc`, empty, NULL, a huge number), many leading zeros, overflow cases that would wrap (`2^32`, `2^32+65535`, `ULONG_MAX+1`, `ULONG_MAX+2`), `LONG_MAX`, the port-count range 1-64, other ranges, errno values.
* `tests/test_cli_args.py` (the real programs; 322 checks; normal, TSan and valgrind variants in `tests/run_unit.sh`):
  1. `./switch` TCP port: 21 bad forms (sign, white space incl. tab / newline, letters, empty, 0, 65536, hex, decimal point, overflow, `2^32`) each must give exit status 1, a message on stderr and **no listener**; `9000`, `09000`, many leading zeros and `65535` must listen and end with status 0 on SIGINT;
  2. the number of ports (valid minimum 1 and maximum 64, `04`; `0`, `65`, `+4`, `-4`, leading / trailing white space, letters, overflow, empty, ...);
  3. the `--pair` peer port (valid; `+Q`, `-Q`, `" Q"`, `"Q "`, `0`, `65536`, letters, overflow, empty): **neither** switch may start, and no process of this test is left;
  4. `supervisor`, `host`, `swctl -p`, `bench`, `bench_mac`: bad ports / counts / seconds / seeds are refused, a valid port that nobody listens on is not an *argument* error;
  5. `host --flood`, the vlan and priority of `sendx` (12 bad commands send nothing, the good one sends exactly one frame with the right fields), and the port numbers of `swctl` commands against a live switch (ERR, switch unharmed);
  6. configuration values (`-0`, `+5`, overflow, hex, empty, ...: refused; `05`, padded `7`: accepted).
  Check of the test itself: run against the **old** code (a scratch copy of `HEAD`) it reports 36 failures (switch, `--pair`, supervisor, host, swctl, bench); on the fixed code 0.

## 5. Results (final code, `make clean` first; every run executed, none assumed)
| Run | Result |
|---|---|
| `make clean && make` (`-std=c11 -Wall -Wextra -Werror`) | exit 0, no warning |
| `make tsan bench` | exit 0 |
| `tests/test_parse.c` | 55 checks, 0 failures (also under valgrind: 0 errors, no leak) |
| `tests/test_cli_args.py` | 322 checks, 0 failures in each of normal, TSan, valgrind |
| `python3 verification/run.py` (independent suite) | **100 of 100 test cases passed**, 743 checks; CLI-01 passes (66 checks) |
| `make test` | exit 0, "All tests passed" |
| `make test-tsan` | exit 0, 19 of 19 scripts |
| `make test-valgrind` | exit 0, 19 of 19 scripts |

The independent suite was run twice: once on the first version of the fix (100 of 100) and once, as listed, on the final code (after the `config.c` and `swctl.c` follow-ups).

## 6. Related issues found and corrected
The other numeric arguments listed in section 2; the `swctl` white-space leniency; `-0` in the configuration file; the benchmark programs accepting `nan` / `inf` seconds (which would have meant a `(long)NaN` conversion, undefined behaviour); my own regression (a too strict `swctl` rule) found and fixed during this work; the new test's first sweep for leftover processes was too broad (it saw an unrelated switch) and now looks only at the ports it started.

## 7. Limits
`verification/` and `Test Cases Result.md` are the independent verifier's files; the results file was updated (section 0), the code was not modified. `tests/test_cli_args.py` needs free TCP ports and uses the fixed shared-memory names like every switch test (do not run switches in parallel).
