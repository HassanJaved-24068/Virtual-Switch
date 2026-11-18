#!/bin/bash
# Parts 1-14 unit and regression tests (called by run_all.sh). Each part added its tests here.
cd "$(dirname "$0")/.." || exit 1

make -s all switch_small build/test_util build/test_frame build/test_queue build/test_refcount \
        build/test_port build/test_mactable build/test_queue_tsan build/test_refcount_tsan \
        build/test_port_tsan build/test_mactable_tsan build/test_log build/test_log_tsan build/test_config build/test_config_tsan build/test_qos build/test_qos_tsan \
        build/test_rate build/test_rate_tsan build/test_vlan build/test_vlan_tsan build/test_stats build/test_stats_tsan || exit 1

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

run python3 tests/test_shutdown.py        # Part 7: SIGINT/SIGTERM, signals, repeated shutdown
make -s switch_tsan host_tsan || exit 1
run env SWITCH="$NOASLR ./switch_tsan" python3 tests/test_shutdown.py tsan   # Part 7: SIGINT while flooding, TSan
if command -v valgrind >/dev/null; then
    run python3 tests/test_shutdown.py valgrind                              # Part 7: no errors, no leaks
else
    echo "valgrind not installed: skipping the Part 7 valgrind test"
fi

run ./build/test_log                      # Part 9: logger unit tests (format, append, truncation, 8 threads, drain, stalled sink)
run $NOASLR ./build/test_log_tsan         # Part 9: the same under ThreadSanitizer
run python3 tests/test_logger.py          # Part 9: switch.log, default file, 100k-frame flood with a stalled log file
run env SWITCH="$NOASLR ./switch_tsan" python3 tests/test_logger.py tsan   # Part 9: flooding + SIGINT under TSan
if command -v valgrind >/dev/null; then
    run python3 tests/test_logger.py valgrind                              # Part 9: no errors, no leaks, log file closed
    run valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all --error-exitcode=1 --track-fds=yes ./build/test_log
fi

run ./build/test_config                   # Part 10: configuration parser (valid/invalid files, ranges, line numbers, atomic failure)
run $NOASLR ./build/test_config_tsan      # Part 10: the same under ThreadSanitizer
run python3 tests/test_config_signals.py  # Part 10: -c, SIGHUP reload (aging, invalid files, startup-only keys, storm), SIGUSR1 dump
run env SWITCH="$NOASLR ./switch_tsan" python3 tests/test_config_signals.py tsan   # Part 10: reloads and dumps under traffic, TSan
if command -v valgrind >/dev/null; then
    run python3 tests/test_config_signals.py valgrind                      # Part 10: no errors, no leaks, no descriptor left open
    run valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all --error-exitcode=1 --track-fds=yes ./build/test_config
    run valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all --error-exitcode=1 --track-fds=yes ./build/test_mactable
fi

run ./build/test_qos                      # Part 11: priority queues: 5:3:1 order, per-class fullness, aging, concurrent producers
run $NOASLR ./build/test_qos_tsan         # Part 11: the same under ThreadSanitizer
run python3 tests/test_qos.py             # Part 11: priority order through a congested port, SIGHUP weights, bad_prio
run env SWITCH="$NOASLR ./switch_tsan" python3 tests/test_qos.py tsan     # Part 11: under TSan
if command -v valgrind >/dev/null; then
    run python3 tests/test_qos.py valgrind                                 # Part 11: no errors, no leaks, no descriptor left open
    run valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all --error-exitcode=1 --track-fds=yes ./build/test_qos
fi

run ./build/test_rate                     # Part 12: token bucket: burst, cap, whole tokens, rate change, close, no spinning, order unchanged
run $NOASLR ./build/test_rate_tsan        # Part 12: the same under ThreadSanitizer
run python3 tests/test_rate.py            # Part 12: port.N.rate / rate_limit, override, SIGHUP, edge rates, new host, bad values, SIGINT while throttled
run env SWITCH="$NOASLR ./switch_tsan" python3 tests/test_rate.py tsan    # Part 12: under TSan
if command -v valgrind >/dev/null; then
    run python3 tests/test_rate.py valgrind                                # Part 12: no errors, no leaks, no descriptor left open
    run valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all --error-exitcode=1 --track-fds=yes ./build/test_rate
fi

run ./build/test_vlan                     # Part 13: VLAN forwarding without sockets: layout, (VLAN, MAC) lookup, trunk, invalid VLANs, set_vlan + flush
run $NOASLR ./build/test_vlan_tsan        # Part 13: the same under ThreadSanitizer
run python3 tests/test_vlan.py            # Part 13: the real switch: criterion, default VLAN, invalid trunk frames, SIGHUP, bad values, port security
run env SWITCH="$NOASLR ./switch_tsan" python3 tests/test_vlan.py tsan   # Part 13: under TSan
if command -v valgrind >/dev/null; then
    run python3 tests/test_vlan.py valgrind                                # Part 13: no errors, no leaks, no descriptor left open
    run valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all --error-exitcode=1 --track-fds=yes ./build/test_vlan
fi

run ./build/test_stats                    # Part 14: shared-memory counters: layout, 0600, magic/version, threads + processes, robust mutex (kill -9 while locked), stale segment, unlink, failure paths
run $NOASLR ./build/test_stats_tsan       # Part 14: the same under ThreadSanitizer
run python3 tests/test_shm.py             # Part 14: the real switch: segment in /dev/shm, counters = host counts, state/VLAN fields, SIGINT unlinks, stale + failed start
run env SWITCH="$NOASLR ./switch_tsan" python3 tests/test_shm.py tsan    # Part 14: under TSan
if command -v valgrind >/dev/null; then
    run python3 tests/test_shm.py valgrind                                 # Part 14: no errors, no leaks, no descriptor left open, no segment left
    run valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all --error-exitcode=1 --track-fds=yes ./build/test_stats
fi

echo "Parts 1-14 unit and regression tests passed."
