#!/bin/bash
# Runs every test script. Part 1 has only the util unit test.
# Later parts add more tests/*.sh scripts here.
cd "$(dirname "$0")/.." || exit 1

make -s build/test_util || exit 1
echo "=== test_util ==="
./build/test_util || { echo "test_util FAILED"; exit 1; }
echo "All tests passed."
