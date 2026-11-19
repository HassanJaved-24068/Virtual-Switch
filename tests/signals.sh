#!/bin/bash
# signals.sh - SIGHUP, SIGUSR1, SIGINT and SIGTERM with a real switch and traffic (Parts 7 and 10 as an integration script, Part 18).
. "$(dirname "$0")/lib.sh"

CONF=$TMPD/sig.conf
DUMP=$TMPD/mac.dump
write_conf() { printf 'mac_aging_seconds = %s\nmac_dump_file = %s\nqueue_capacity = 128\n' "$1" "$DUMP" > "$CONF.tmp" && mv "$CONF.tmp" "$CONF"; }
write_conf 30
SW_ARGS=(-c "$CONF")
sw_start 4
A=$(mac 1); B=$(mac 2)
host_start A "$A"; host_start B "$B"
barrier A learn-a B
barrier B learn-b A

echo "SIGUSR1: the MAC table is dumped"
kill -USR1 "$SWPID"
wait_file "$DUMP" "$A" || bad "no dump file"
check "the dump has its header"                test "$(cnt "$DUMP" '# vlan mac port age_s')" -eq 1
check "the dump lists A on port 1 and B on port 2" test "$(cnt "$DUMP" " $A 1 ")$(cnt "$DUMP" " $B 2 ")" = 11

echo "SIGHUP: a valid change is applied"
n=$(cnt "$SWLOG" "configuration reloaded")
write_conf 31
kill -HUP "$SWPID"
wait_file "$SWLOG" "configuration reloaded" $((n + 1)) || bad "no reload"
check "the log says mac_aging_seconds 30 -> 31" test "$(cnt "$SWLOG" 'mac_aging_seconds 30 -> 31')" -eq 1

echo "SIGHUP and SIGUSR1 20 times while A and B talk to each other"
for ((i = 1; i <= 20; i++)); do
    say A "send $B t-$i"
    say B "send $A u-$i"
    write_conf $((40 + i % 2))
    kill -HUP "$SWPID"
    kill -USR1 "$SWPID"
done
wait_out B "data=t-20"; wait_out A "data=u-20"
check "every frame arrived (20 each way)" test "$(out_count B data=t-)$(out_count A data=u-)" = 2020
check "the switch is still running" kill -0 "$SWPID"
check "no [ERR] in the log" test "$(cnt "$SWLOG" '[ERR]')" -eq 0

echo "SIGINT with traffic still queued: clean exit (checked by sw_stop)"
sw_stop
hosts_reap
check "both hosts saw the end of the connection and ended with status 0" test "${HRC[A]}${HRC[B]}" = 00

echo "SIGTERM does the same, and an invalid configuration file is refused (logged as an error) while the old one stays"
HPID=(); HFD=(); HRC=()
write_conf 30
sw_start 3
host_start C "$(mac 3)"; host_start D "$(mac 4)"
barrier C c1 D
printf 'this is = not a valid file\nmac_aging_seconds = abc\n' > "$CONF.tmp" && mv "$CONF.tmp" "$CONF"
kill -HUP "$SWPID"
wait_file "$SWLOG" "reload failed" || bad "no 'reload failed'"
check "the invalid file was refused and the switch runs on" kill -0 "$SWPID"
write_conf 32
n=$(cnt "$SWLOG" "configuration reloaded")
kill -HUP "$SWPID"
wait_file "$SWLOG" "configuration reloaded" $((n + 1)) || bad "no reload after the invalid file"
check "the previous configuration was intact: 30 -> 32" test "$(cnt "$SWLOG" 'mac_aging_seconds 30 -> 32')" -eq 1
kill -TERM "$SWPID"
wait_gone "$SWPID" || bad "the switch did not stop after SIGTERM"
wait "$SWPID"; rc=$?
SWPID=""
check "SIGTERM: exit status 0 (got $rc)" test "$rc" -eq 0
check "the log ends with 'shutdown: complete'" test "$(cnt "$SWLOG" 'shutdown: complete')" -eq 1
check "the only [ERR] line is the refused reload (an invalid file is an error event by design)" test "$(cnt "$SWLOG" '[ERR]')$(cnt "$SWLOG" 'ERR] reload failed')" = 11
hosts_reap
finish
