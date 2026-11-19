# Part 22 - final check of the submission

Done on 2026-11-19 on commit `a90f348` (the Part 22 README and report; the commit that adds this file changes documentation only).

Method: `git archive HEAD | tar -x` into an empty directory (so exactly the committed files, nothing built, no leftovers), then in that directory, one after the other:

| command | result |
|---|---|
| `make` (strict flags, `-Werror`) | exit 0, no warning |
| `make test` | exit 0, "All tests passed" (unit and regression tests of all parts + 19 integration scripts) |
| `make test-tsan` | exit 0, 19 of 19 integration scripts passed |
| `make test-valgrind` | exit 0, 19 of 19 integration scripts passed |

Every run passed the first time (no rerun, no flaky test failure). `/dev/shm` was empty afterwards.

Contents of the submission: the archive holds `Makefile`, `README.md`, `src/`, `include/`, `tests/`, `bench/` (sources, `run.sh`, `plot.py` and the committed results), `config/`, `docs/` and `notes/`. It contains no binary and no object file
(`.gitignore` excludes `build/`, `*.o` and every built program). `bench/report.pdf` and `bench/throughput.png` are results, not build products, and are included on purpose.

Not part of the submission (untracked, left out on purpose): `.claude/`, `Virtual_Switch_Implementation_Roadmap.pdf`, `output.txt`.
