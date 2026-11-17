#!/bin/bash
# port_security.sh - a port may learn at most M MACs (3 in switch_small). The 4th MAC disables the
# port (state DISABLED, logged); the other ports keep working and nothing deadlocks.
. "$(dirname "$0")/lib.sh"

sw_start 4 small
host_start A "$(mac 1)"           # port 1
host_start B "$(mac 2)"           # port 2
raw_open S                        # port 3: a raw sender that can use any source MAC
wait_file "$SWLOG" "port 3: host connected" || bad "the raw sender did not get port 3"

echo "port 3 sends from 3 different MACs: all are learned and forwarded"
for k in 1 2 3; do
    raw_send S "$(mac $((0x80 + k)))" ff:ff:ff:ff:ff:ff 1 1 ok-$k
done
for k in 1 2 3; do wait_out B "data=ok-$k" || bad "B did not get ok-$k"; done
check "B got the 3 frames" test "$(out_count B data=ok-)" -eq 3
check "port 3 is not disabled yet" test "$(cnt "$SWLOG" 'MAC flooding')" -eq 0

echo "a 4th MAC on port 3: the frame is dropped and the port is shut down"
raw_send S "$(mac $((0x84)))" ff:ff:ff:ff:ff:ff 1 1 too-many
wait_file "$SWLOG" "port 3 shut down: MAC flooding" || bad "the security event was not logged"
wait_file "$SWLOG" "port 3: teardown complete, port is DISABLED" || bad "port 3 did not become DISABLED"
check "the security event is logged for port 3" test "$(cnt "$SWLOG" 'port 3 shut down: MAC flooding')" -eq 1
check "port 3 ended DISABLED (not FREE)" test "$(cnt "$SWLOG" 'port 3: teardown complete, port is DISABLED')" -eq 1
check "the 4th frame was not forwarded" test "$(out_count A data=too-many)$(out_count B data=too-many)" = 00

echo "the other ports keep forwarding; no deadlock"
say A "send $(mac 2) a-to-b"
say B "send bcast b-to-all"
wait_out B "data=a-to-b" && wait_out A "data=b-to-all" || bad "A and B cannot talk after the shutdown of port 3"
check "A and B still talk" test "$(out_count B data=a-to-b)$(out_count A data=b-to-all)" = 11
check "the switch is still running" kill -0 "$SWPID"

echo "the DISABLED port is not handed out again: only port 4 is left"
host_start C "$(mac 3)"
check "the next host got port 4, not the disabled port 3" test "$(cnt "$SWLOG" 'port 4: host connected')" -eq 1
host_spawn D "$(mac 4)"
host_reap D
check "with 3 usable ports taken the next host is told SWITCH FULL (status ${HRC[D]})" test "${HRC[D]}" -eq 2

raw_close S
sw_stop
hosts_reap
finish
