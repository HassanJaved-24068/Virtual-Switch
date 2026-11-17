#!/bin/bash
# port_full.sh - all ports taken: the next host waits (port_wait_timeout = 5 s), then gets the
# SWITCH FULL frame and exits with status 2; ports freed meanwhile are handed out; the accounting stays right.
. "$(dirname "$0")/lib.sh"

sw_start 2
host_start A "$(mac 1)"
host_start B "$(mac 2)"
check "both ports are taken" test "$(connected)" -eq 2

echo "a third host waits for a port, then gets SWITCH FULL"
start=$SECONDS
host_spawn C "$(mac 3)"
wait_file "$SWLOG" "no free port: host waits" || bad "the switch did not report a waiting host"
host_reap C
took=$((SECONDS - start))
check "the host exited with status 2 (got ${HRC[C]})" test "${HRC[C]}" -eq 2
check "it printed SWITCH FULL" test "$(out_count C 'SWITCH FULL')" -eq 1
check "the wait was about 5 s (took ${took} s)" test "$took" -ge 4 -a "$took" -le 15
check "the switch logged the SWITCH FULL answer" test "$(cnt "$SWLOG" 'sending SWITCH FULL')" -eq 1
check "no third port was used" test "$(connected)" -eq 2

echo "accounting: when A leaves, the next host gets A's port at once"
host_quit A
wait_file "$SWLOG" "port 1: teardown complete, port is FREE" || bad "port 1 was not freed"
host_start D "$(mac 4)"
check "D got port 1 (the token was returned)" test "$(cnt "$SWLOG" 'port 1: host connected')" -eq 2

echo "a host that waits gets a port that becomes free before the timeout"
host_spawn E "$(mac 5)"
wait_file "$SWLOG" "no free port: host waits" 2 || bad "E is not waiting"
host_quit B
wait_file "$SWLOG" "port 2: host connected" 2 || bad "E did not get B's port"
check "E got port 2 and is still connected (not SWITCH FULL)" kill -0 "${HPID[E]}"
check "the log shows no second SWITCH FULL" test "$(cnt "$SWLOG" 'sending SWITCH FULL')" -eq 1

echo "the two remaining hosts work, and the switch is full again"
say D "send bcast d-to-e"
say E "send bcast e-to-d"
wait_out E "data=d-to-e" && wait_out D "data=e-to-d" || bad "D and E cannot talk"
host_spawn F "$(mac 6)"
host_reap F
check "a host arriving now is answered SWITCH FULL too (status ${HRC[F]})" test "${HRC[F]}" -eq 2
check "D and E are untouched" kill -0 "${HPID[D]}" "${HPID[E]}"

sw_stop
hosts_reap
finish
