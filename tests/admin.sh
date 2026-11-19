#!/bin/bash
# admin.sh - swctl against a real switch (Parts 15 and 16 as an integration script, Part 18): every command, exit statuses 0 / 1 / 2, the effect of
# shutdown / up / vlan on real hosts, stats, and SIGINT while an admin client is connected and silent.
. "$(dirname "$0")/lib.sh"

sw_start 4
A=$(mac 1); B=$(mac 2)
host_start A "$A"; host_start B "$B"
ctl() { T ./swctl -p "$PORT" "$@"; }
rc_of() { ctl "$@" >/dev/null 2>&1; echo $?; }

barrier A m1 B
barrier B m2 A
echo "show ports / show mac / stats"
ctl show ports > "$TMPD/ports.txt"
check "show ports: ports 1 and 2 UP, 3 and 4 FREE" test "$(T awk '$1<=2 && $2=="UP" {a++} $1>=3 && $2=="FREE" {a++} END {print a+0}' "$TMPD/ports.txt")" -eq 4
ctl show mac > "$TMPD/mac.txt"
check "show mac lists A on port 1 and B on port 2" test "$(cnt "$TMPD/mac.txt" " $A 1 ")$(cnt "$TMPD/mac.txt" " $B 2 ")" = 11
ctl stats > "$TMPD/stats.txt"
check "stats: port 1 received 1 frame (A's) and sent 1 (B's broadcast to it)" test "$(T awk '$1==1 {print $4 "/" $5}' "$TMPD/stats.txt")" = "1/1"
check "stats has a MIRRORED column" test "$(cnt "$TMPD/stats.txt" MIRRORED)" -eq 1

echo "clear mac"
check "clear mac: OK" ctl clear mac
check "the table is empty" test "$(ctl show mac | T grep -vc '^#')" -eq 0
say A "send $B flooded-after-clear"
wait_out B "data=flooded-after-clear"
check "a frame for an unknown destination still arrives (flooded)" test "$(out_count B data=flooded-after-clear)" -eq 1

echo "vlan"
check "vlan 3 30 on a FREE port: OK" ctl vlan 3 30
check "show ports shows VLAN 30 on port 3" test "$(ctl show ports | T awk '$1==3 {print $3}')" = 30
check "vlan 2 20 moves host B" ctl vlan 2 20
say A "send bcast vlan1-frame"
sleep 1                                  # nobody in VLAN 1 is left to print a barrier: give the receiver of A time to forward it
check "B (now VLAN 20) no longer gets A's VLAN 1 broadcast" test "$(out_count B data=vlan1-frame)" -eq 0
check "vlan 2 1 brings it back" ctl vlan 2 1

echo "shutdown and up"
check "shutdown 2: OK" ctl shutdown 2
wait_gone "${HPID[B]}" || bad "B did not end"
wait "${HPID[B]}" 2>/dev/null; HRC[B]=$?
unset 'HPID[B]'
check "host B was disconnected by the switch and ended with status 0" test "${HRC[B]}" -eq 0
check "show ports: port 2 DISABLED" test "$(ctl show ports | T awk '$1==2 {print $2}')" = DISABLED
check "shutdown 2 again is refused (exit 1)" test "$(rc_of shutdown 2)" -eq 1
check "up 2: OK" ctl up 2
check "show ports: port 2 FREE" test "$(ctl show ports | T awk '$1==2 {print $2}')" = FREE
check "up 2 again is refused (exit 1)" test "$(rc_of up 2)" -eq 1
host_start B2 "$B"
check "a new host got port 2 again" test "$(ctl show ports | T awk '$1==2 {print $2}')" = UP

echo "invalid commands answer ERR (exit 1), the switch stays up"
for c in "frobnicate" "show" "show nothing" "shutdown" "shutdown 0" "shutdown 99" "shutdown x" "up 99" "vlan 1" "vlan 1 0" "vlan 1 4095" "vlan 9 5" "mirror 1" "mirror 1 1" "clear ports"; do
    # shellcheck disable=SC2086
    check "swctl $c -> exit 1" test "$(rc_of $c)" -eq 1
done
check "the switch is still running" kill -0 "$SWPID"

echo "a silent admin client does not delay SIGINT"
SOCK=/tmp/vswitch_$PORT.sock
check "the socket exists" test -S "$SOCK"
python3 -c 'import socket, sys, time; s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); time.sleep(60)' "$SOCK" &
SILENT=$!
trap 'kill -9 "$SILENT" 2>/dev/null; cleanup' EXIT
sleep 0.5
t0=$(T date +%s.%N)
sw_stop
t1=$(T date +%s.%N)
kill -9 "$SILENT" 2>/dev/null; wait "$SILENT" 2>/dev/null
el=$(T python3 -c "print('%.1f' % (float('$t1') - float('$t0')))")
check "the switch left in $el s (a silent admin client times out after 2 s, so it must not be waited for)" test "$(T python3 -c "print(1 if float('$el') < 5.0 else 0)")" -eq 1
check "the socket path is gone" test ! -e "$SOCK"
hosts_reap

echo "no switch: exit status 2"
check "show mac: exit 2" test "$(rc_of show mac)" -eq 2
check "shutdown 1: exit 2" test "$(rc_of shutdown 1)" -eq 2
check "stats: exit 2 and the message 'switch not running'" bash -c "'$ROOT/swctl' -p $PORT stats 2>&1 | grep -q 'switch not running'"
check "stats --watch: exit 2" test "$(rc_of stats --watch)" -eq 2
finish
