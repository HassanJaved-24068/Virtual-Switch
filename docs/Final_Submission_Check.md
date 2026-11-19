# Final submission check

Authority: the official assignment PDF (`Virtual_Switch_Project_OS_Fall2026.pdf`) was **not available** on the development machine; the implementation roadmap derived from it (`Virtual_Switch_Implementation_Roadmap.pdf`, v1) was used as the specification. Nothing in this check claims more than what was run.

## Deliverables (all in `virtual-switch.tar.gz`, made by `make dist` from the committed files)
| Deliverable | Where |
|---|---|
| All source files and the Makefile | `src/`, `include/`, `Makefile`; tests `tests/`; benchmark sources `bench/`; `config/` |
| README (build, run of every program, configuration file format with every key) | `README.md` |
| 2-3 page report (thread diagram, every lock and its data, lock order, one real bug, Tier 2 / Tier 3 list) | `Report.pdf` (2 pages; source `report/Report.ms`, built by `report/build.sh`) |
| Supporting material the README and notes refer to | `notes/` (locks, bug diary), `docs/` (design write-ups of every part) |

Not in the archive: built programs, object files, `build/`, `verification/` (the independent suite, committed in the repository), local tool folders. `tar -tzf` shows 150 files; `find` finds no `*.o` and no executable other than `.sh` / `.py` scripts in an unpacked copy.

## Check done on the unpacked archive (commit b86fa52, empty directory)
| Step | Result |
|---|---|
| `make` with `gcc -std=c11 -Wall -Wextra -Werror -g -O0 -D_GNU_SOURCE -pthread` | exit 0, 0 warnings (the flags were read from `make -B -n`) |
| `make clean` | exit 0; afterwards no `*.o` and no program (only sources, scripts and documents) |
| `make` again | exit 0, 0 warnings |
| `make tsan` | exit 0 (`switch_tsan`, `host_tsan`, `switch_small_tsan`) |
| `make test` | exit 0, "All tests passed" (unit and regression suites incl. their TSan / valgrind unit runs; 20 groups: the unit suite and 19 integration scripts) |
| `make test-tsan` | exit 0, 19 of 19 integration scripts, "All tests passed" |
| `make test-valgrind` | exit 0, 19 of 19 integration scripts, "All tests passed" |

After the check only documents changed (`report/Report.ms`, `Report.pdf`, this file): no source, header, test or Makefile file differs between the verified commit and the submitted one
(`git diff --stat b86fa52 HEAD -- src include tests bench Makefile config` is empty).

## Earlier verification of the same code
The independent suite (`verification/run.py`, 100 test cases, 743 checks) passed 100 of 100 on the code of commit 3c0ea20, which is the last commit that changed `src/`, `include/`, `tests/` or `bench/` (see docs/CLI_Argument_Validation_Fix.md); the Makefile later only gained the `dist` target.

## Limits
No official public tests were supplied; `--pair` has one link; `--persist-mac` needs the striped table and cannot be combined with `--pair`; the benchmark was measured on a 2-core machine at `-O0`; the report was built with groff because no other typesetting tool was installed.
