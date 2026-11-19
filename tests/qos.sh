#!/bin/bash
# qos.sh - priorities with real hosts (Part 11 as an integration script, Part 18): all three classes are delivered, in order inside each class; an invalid
# priority is queued as low, counted, logged once per port and NOT changed on the wire; SIGHUP changes the scheduling weights.
. "$(dirname "$0")/lib.sh"

CONF=$TMPD/qos.conf
printf 'wrr_weights = 5,3,1\nprio_aging_ms = 500\n' > "$CONF"
SW_ARGS=(-c "$CONF")
sw_start 4
A=$(mac 1); B=$(mac 2); Rm=$(mac 9)
host_start A "$A"; host_start B "$B"; host_start C "$(mac 3)"
N=${QOS_FRAMES:-30}

echo "A sends $N frames of each priority (0 = high, 1 = normal, 2 = low) to B"
for ((i = 1; i <= N; i++)); do
    say A "sendx $B 1 0 p0-$i"
    say A "sendx $B 1 1 p1-$i"
    say A "sendx $B 1 2 p2-$i"
done
wait_out B "data=p0-$N"; wait_out B "data=p1-$N"; wait_out B "data=p2-$N"
for p in 0 1 2; do
    check "all $N frames of priority $p arrived" test "$(out_count B "data=p$p-")" -eq "$N"
    check "priority $p: in the order they were sent (a queue class is FIFO)" \
          bash -c "grep -o 'data=p$p-[0-9]*' '$(out B)' | sed 's/data=p$p-//' | sort -n -c"
    check "priority $p: the prio field on the wire is $p" test "$(out_count B "prio=$p len=")" -ge "$N"
done

echo "an invalid priority (9), sent by a raw connection on port 4 (the real host cannot send one): delivered unchanged, counted, logged once"
raw_open R
wait_file "$SWLOG" "port 4: host connected" || bad "the raw connection did not get port 4"
raw_send R "$Rm" "$B" 1 9 bad-1
raw_send R "$Rm" "$B" 1 9 bad-2
wait_out B "data=bad-2"
check "both frames arrived at B with prio=9 (the scheduler does not change the frame)" test "$(out_count B 'prio=9 len=5 data=bad-')" -eq 2
check "the first one was logged, once, for port 4" test "$(cnt "$SWLOG" 'port 4: bad_prio: a frame with priority 9')" -eq 1
check "the flooded copies at C are unchanged too" test "$(out_count C 'prio=9 len=5 data=bad-')" -eq 2
raw_close R

echo "SIGHUP: new weights"
printf 'wrr_weights = 1,1,1\nprio_aging_ms = 500\n' > "$CONF"
n=$(cnt "$SWLOG" "configuration reloaded")
kill -HUP "$SWPID"
wait_file "$SWLOG" "configuration reloaded" $((n + 1)) || bad "no reload"
check "the log says wrr_weights 5,3,1 -> 1,1,1" test "$(cnt "$SWLOG" 'wrr_weights 5,3,1 -> 1,1,1')" -eq 1
for ((i = 1; i <= 10; i++)); do say A "sendx $B 1 $((i % 3)) after-$i"; done
wait_out B "data=after-10"
check "traffic goes on with the new weights (10 frames)" test "$(out_count B data=after-)" -eq 10

say A quit
sw_stop
check "the teardown of the raw host logged its bad_prio total (2 frames)" test "$(cnt "$SWLOG" 'port 4: bad_prio: 2 frames')" -eq 1
hosts_reap
finish
