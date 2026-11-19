#!/bin/bash
# mirror.sh - port mirroring with real hosts (Part 17 as an integration script, Part 18): the real `./host --sniff` on port 3 (its own VLAN 99) receives all
# traffic of port 1 in both directions; errors; mirroring off; a sniffer killed with kill -9 ends the mirroring and nothing else is affected.
. "$(dirname "$0")/lib.sh"

CONF=$TMPD/mirror.conf
printf 'port.3.vlan = 99\n' > "$CONF"
SW_ARGS=(-c "$CONF")
sw_start 4
A=$(mac 1); B=$(mac 2); S=$(mac 3)
host_start A "$A"; host_start B "$B"
host_start S "$S" --sniff
check "three hosts, the sniffer on port 3" test "$(connected)" -eq 3
ctl() { T ./swctl -p "$PORT" "$@"; }
mirror_col() { ctl show ports | T awk -v p="$1" '$1 == p { print $6 }'; }

echo "mirror 1 3: the sniffer sees A's frames and the frames queued to A"
check "mirror 1 3 is accepted" ctl mirror 1 3
check "show ports: port 1 mirrors to 3" test "$(mirror_col 1)" = 3
say A "send $B a-to-b"
say B "send $A b-to-a"
say A "send bcast a-bcast"
say B "send bcast b-bcast"
wait_out S "data=a-to-b"; wait_out S "data=b-to-a"; wait_out S "data=a-bcast"; wait_out S "data=b-bcast"
barrier B settle1 A
check "the sniffer got exactly the four frames of port 1's traffic (2 sent by A, 2 queued to A)" test "$(out_count S 'RX src=')" -eq 5
check "...and its lines carry the original addresses: A -> B" test "$(out_count S "src=$A dst=$B vlan=1")" -eq 1
check "...B -> A" test "$(out_count S "src=$B dst=$A vlan=1")" -eq 1
check "A and B got their normal frames" test "$(( $(out_count B data=a-to-b) + $(out_count A data=b-to-a) ))" -eq 2
check "stats: port 1 'mirrored' = 5 (the barrier frame of B was queued to A too), nothing dropped" \
      test "$(ctl stats | T awk '$1 == 1 { print $7 "/" $6 }')" = "5/0"

echo "errors"
check "mirror 1 1 is refused (exit 1)"          bash -c "! '$ROOT/swctl' -p $PORT mirror 1 1 2>/dev/null"
check "mirror 1 9 is refused (port out of range)" bash -c "! '$ROOT/swctl' -p $PORT mirror 1 9 2>/dev/null"
check "mirror 4 2 is refused (port 4 has no host)" bash -c "! '$ROOT/swctl' -p $PORT mirror 4 2 2>/dev/null"
check "mirror 1 4 is refused (the sniffer port has no host)" bash -c "! '$ROOT/swctl' -p $PORT mirror 1 4 2>/dev/null"

echo "mirror 1 off: no more copies"
check "mirror 1 off" ctl mirror 1 off
check "show ports: no mirroring" test "$(mirror_col 1)" = "-"
say A "send $B after-off"
barrier B settle2 A
check "the sniffer saw nothing of it" test "$(out_count S data=after-off)" -eq 0
check "B got it" test "$(out_count B data=after-off)" -eq 1

echo "the sniffer is killed (kill -9): the switch ends the mirroring, A and B go on"
check "mirror 2 3 again" ctl mirror 2 3
kill -9 "${HPID[S]}"
wait "${HPID[S]}" 2>/dev/null
unset 'HPID[S]'
wait_file "$SWLOG" "port 2: mirroring to port 3 stopped" || bad "the switch did not log the end of the mirroring"
check "show ports: port 2 no longer mirrors" test "$(mirror_col 2)" = "-"
say A "send $B still-here"
wait_out B "data=still-here"
check "A and B still talk" test "$(out_count B data=still-here)" -eq 1
check "no [ERR] in the log" test "$(cnt "$SWLOG" '[ERR]')" -eq 0

sw_stop
hosts_reap
finish
