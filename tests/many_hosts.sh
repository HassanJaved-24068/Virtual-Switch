#!/bin/bash
# many_hosts.sh - 32 hosts connect at once. (1) a 32-port switch: everybody gets its own port and
# everybody can talk. (2) a 30-port switch: 30 hosts get ports, the other 2 wait and are told
# SWITCH FULL (status 2), one after the other (the accept thread handles ONE waiting host at a
# time, 5 s each: roadmap Part 5); the 30 stay fully functional.
. "$(dirname "$0")/lib.sh"

N=32
P2=30                # ports in run 2
X=$((N - P2))        # hosts without a port in run 2 (each waits 5 s, serially)
spawn_all() { for i in $(seq 1 $N); do host_spawn $1$i "$(mac $i)"; done; }   # spawn_all PREFIX

echo "run 1: $N hosts, $N ports, all connect at once"
sw_start $N
spawn_all H
wait_file "$SWLOG" "host connected" $N || bad "not all hosts connected"
check "$N hosts connected" test "$(connected)" -eq $N
missing=0
for p in $(seq 1 $N); do
    [ "$(cnt "$SWLOG" "port $p: host connected")" -eq 1 ] || missing=$((missing + 1))
done
check "every port 1..$N was given to exactly one host" test "$missing" -eq 0

for i in $(seq 1 $N); do say H$i "send bcast hello-$i"; done
for i in $(seq 1 $N); do
    wait_out H$i "RX" $((N - 1)) || bad "H$i did not receive $((N - 1)) broadcasts"
done
bad_hosts=0
for i in $(seq 1 $N); do
    [ "$(out_count H$i RX)" -eq $((N - 1)) ] && [ "$(out_count H$i src=$(mac $i))" -eq 0 ] || bad_hosts=$((bad_hosts + 1))
done
check "every host got the $((N - 1)) broadcasts of the others and none of its own" test "$bad_hosts" -eq 0
sw_stop
hosts_reap
bad_hosts=0
for i in $(seq 1 $N); do [ "${HRC[H$i]}" = 0 ] || bad_hosts=$((bad_hosts + 1)); done
check "all hosts ended with status 0 when the switch closed their connection" test "$bad_hosts" -eq 0

echo "run 2: $N hosts, $P2 ports: $P2 get a port, $X wait and are told SWITCH FULL, one at a time"
HPID=(); HFD=(); HRC=()
sw_start $P2
start=$SECONDS
spawn_all G
wait_file "$SWLOG" "host connected" $P2 || bad "$P2 hosts did not connect"
alive=(); full=()
# the hosts without a port end by themselves: wait until exactly $X processes are gone
end=$((SECONDS + TMO))
while :; do
    dead=0
    for i in $(seq 1 $N); do kill -0 "${HPID[G$i]}" 2>/dev/null || dead=$((dead + 1)); done
    [ "$dead" -ge "$X" ] && break
    [ "$SECONDS" -ge "$end" ] && break
    nap
done
took=$((SECONDS - start))
for i in $(seq 1 $N); do
    if kill -0 "${HPID[G$i]}" 2>/dev/null; then alive+=(G$i); else full+=(G$i); fi
done
echo "  ${#alive[@]} hosts connected, ${#full[@]} were turned away after $took s"
check "$P2 hosts are still connected" test "${#alive[@]}" -eq "$P2"
check "$X hosts were turned away" test "${#full[@]}" -eq "$X"
check "the port limit was respected: $P2 hosts connected, no port $((P2 + 1)) or above" \
      test "$(connected)" -eq "$P2" -a "$(cnt "$SWLOG" "port $((P2 + 1)):")" -eq 0
check "the $X excess hosts were answered one after the other, 5 s each (took $took s, expected >= $((X * 5 - 1)))" \
      test "$took" -ge $((X * 5 - 1))
check "the switch logged $X SWITCH FULL answers" test "$(cnt "$SWLOG" 'sending SWITCH FULL')" -eq "$X"
codes=0
for h in "${full[@]}"; do host_reap "$h"; [ "${HRC[$h]}" -eq 2 ] && codes=$((codes + 1)); done
check "all $X turned-away hosts exited with status 2" test "$codes" -eq "$X"

for h in "${alive[@]}"; do say "$h" "send bcast second-$h"; done
for h in "${alive[@]}"; do wait_out "$h" "data=second-" $((P2 - 1)) || bad "$h did not receive $((P2 - 1)) broadcasts"; done
bad_hosts=0
for h in "${alive[@]}"; do [ "$(out_count "$h" 'data=second-')" -eq $((P2 - 1)) ] || bad_hosts=$((bad_hosts + 1)); done
check "the $P2 connected hosts all exchange broadcasts ($((P2 - 1)) each)" test "$bad_hosts" -eq 0

sw_stop
hosts_reap
finish
