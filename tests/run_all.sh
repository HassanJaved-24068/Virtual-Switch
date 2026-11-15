#!/bin/bash
# Runs every test. Each part adds its scripts here.
cd "$(dirname "$0")/.." || exit 1

make -s all switch_small build/test_util build/test_frame build/test_queue build/test_refcount \
        build/test_port build/test_mactable build/test_queue_tsan build/test_refcount_tsan \
        build/test_port_tsan build/test_mactable_tsan || exit 1

run() {
    echo "=== $* ==="
    "$@" || { echo "FAILED: $*"; exit 1; }
}

# ThreadSanitizer binaries fail to start on kernels with high address-space
# randomisation; "setarch -R" switches it off for that one process.
if setarch -R true 2>/dev/null; then NOASLR="setarch -R"; else NOASLR=""; fi

run ./build/test_util          # Part 1: MAC and time helpers, CHECK, log
run ./build/test_frame         # Part 2 (+4): wire protocol, frame_recv_f
run python3 tests/test_host.py # Part 2: host program against a fake switch
run tests/wire_bytes.sh        # Part 2: byte order seen with xxd
run python3 tests/test_switch.py   # Part 3: listener, port table, receiver threads
run ./build/test_queue         # Part 4: bounded queue and stats
run ./build/test_refcount      # Part 4: frame_ref/unref with 8 threads
run $NOASLR ./build/test_queue_tsan     # Part 4: the same under ThreadSanitizer
run $NOASLR ./build/test_refcount_tsan  # Part 4: the same under ThreadSanitizer
run python3 tests/test_forward.py  # Part 4: flooding, head-of-line blocking
run ./build/test_port          # Part 5: port lifecycle, reaper, semaphore (unit level)
run $NOASLR ./build/test_port_tsan      # Part 5: the same under ThreadSanitizer
run python3 tests/test_lifecycle.py   # Part 5: lifecycle with the real switch and hosts
run ./build/test_mactable      # Part 6: MAC table unit tests, 8-thread stress, real ports
run $NOASLR ./build/test_mactable_tsan  # Part 6: the same under ThreadSanitizer
run python3 tests/test_mac.py  # Part 6: learning, unicast, aging, LRU, port security (real switch)

echo "All tests passed."
