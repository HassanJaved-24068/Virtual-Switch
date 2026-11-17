#!/bin/bash
# mac_move.sh - the same MAC appears on another port: the switch moves the entry, logs "MAC moved"
# and unicast frames follow the MAC to its new port.
. "$(dirname "$0")/lib.sh"

sw_start 4
X=$(mac 1)
host_start A "$X"                 # port 1: MAC X
host_start B "$(mac 2)"           # port 2
host_start C "$(mac 3)"           # port 3

echo "X is learned on port 1"
barrier A a-hello B C
say B "send $X to-port-1"
wait_out A "data=to-port-1" || bad "A did not get the frame for X"
barrier B m1 A C
check "A (port 1) got the unicast for X" test "$(out_count A data=to-port-1)" -eq 1
check "C did not (X is known on port 1)" test "$(out_count C data=to-port-1)" -eq 0
check "no move was logged yet" test "$(cnt "$SWLOG" 'MAC moved')" -eq 0

echo "the same MAC X now appears on port 4"
host_start D "$X"                 # port 4: MAC X again
say D "send bcast d-hello"
wait_file "$SWLOG" "MAC moved" || bad "no 'MAC moved' in the log"
check "'MAC moved' was logged once, for X, port 1 -> 4" test "$(cnt "$SWLOG" "MAC moved: $X vlan 1 port 1 -> 4")" -eq 1
wait_out B "data=d-hello" || bad "B did not get d-hello"

echo "unicast to X now goes to port 4 only"
say B "send $X to-port-4"
wait_out D "data=to-port-4" || bad "D did not get the frame for X"
barrier B m2 A C D
check "D (port 4) got the unicast" test "$(out_count D data=to-port-4)" -eq 1
check "A (old port 1) did not get it" test "$(out_count A data=to-port-4)" -eq 0
check "C did not get it" test "$(out_count C data=to-port-4)" -eq 0

echo "and it can move back"
barrier A a-again B C D
wait_file "$SWLOG" "MAC moved: $X vlan 1 port 4 -> 1" || bad "the move back was not logged"
say B "send $X to-port-1-again"
wait_out A "data=to-port-1-again" || bad "A did not get the frame after the move back"
barrier B m3 A C D
check "D did not get the frame after the move back" test "$(out_count D data=to-port-1-again)" -eq 0

sw_stop
hosts_reap
finish
