#!/bin/bash
# Runs every test. Each part adds its scripts here.
cd "$(dirname "$0")/.." || exit 1

make -s all build/test_util build/test_frame || exit 1

run() {
    echo "=== $* ==="
    "$@" || { echo "FAILED: $*"; exit 1; }
}

run ./build/test_util          # Part 1: MAC and time helpers, CHECK, log
run ./build/test_frame         # Part 2: wire protocol unit tests
run python3 tests/test_host.py # Part 2: host program against a fake switch
run tests/wire_bytes.sh        # Part 2: byte order seen with xxd

echo "All tests passed."
