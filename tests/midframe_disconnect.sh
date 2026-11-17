#!/bin/bash
# midframe_disconnect.sh - a host that disconnects in the middle of a frame: the switch sees the
# truncated frame, frees the port, never forwards the half frame, and the other hosts go on.
. "$(dirname "$0")/lib.sh"

sw_start 3
host_start A "$(mac 1)"
host_start B "$(mac 2)"
check "ports 1 and 2 are taken" test "$(cnt "$SWLOG" 'port 1: host connected')$(cnt "$SWLOG" 'port 2: host connected')" = 11

echo "a host connects, writes half a frame and exits (--die-midframe)"
host_start X "$(mac 7)" --die-midframe
host_reap X
wait_file "$SWLOG" "port 3: truncated frame" || bad "the switch did not report a truncated frame"
wait_file "$SWLOG" "port 3: teardown complete, port is FREE" || bad "port 3 was not reclaimed"
check "the truncated frame was detected on port 3" test "$(cnt "$SWLOG" 'port 3: truncated frame')" -eq 1
check "port 3 was reclaimed (FREE)" test "$(cnt "$SWLOG" 'port 3: teardown complete, port is FREE')" -eq 1

echo "nobody received the half frame; A and B still work"
say A "send $(mac 2) after-1"
say B "send bcast after-2"
wait_out B "data=after-1" && wait_out A "data=after-2" || bad "A and B cannot talk after the disconnect"
check "no host got any part of the half frame" test "$(out_count A half-frame)$(out_count B half-frame)" = 00
check "the switch is still running" kill -0 "$SWPID"

echo "the freed port is reused by the next host"
host_start D "$(mac 4)"
check "the new host got port 3 again" test "$(cnt "$SWLOG" 'port 3: host connected')" -eq 2
say D "send bcast hello-from-d"
wait_out A "data=hello-from-d" && wait_out B "data=hello-from-d" || bad "D's frame did not reach A and B"

echo "the same again, three times in a row"
prev=D
for i in 1 2 3; do
    host_quit $prev
    wait_file "$SWLOG" "port 3: teardown complete, port is FREE" $((2 * i)) || bad "port 3 not freed in round $i"
    host_start X$i "$(mac 7)" --die-midframe
    host_reap X$i
    wait_file "$SWLOG" "port 3: truncated frame" $((i + 1)) || bad "round $i: no truncated frame"
    wait_file "$SWLOG" "port 3: teardown complete, port is FREE" $((2 * i + 1)) || bad "round $i: port 3 not freed"
    host_start D$i "$(mac 4)"
    prev=D$i
done
check "the port still has exactly one owner at the end" test "$(connected)" -eq $((2 + 1 + 1 + 3 + 3))

sw_stop
hosts_reap
finish
