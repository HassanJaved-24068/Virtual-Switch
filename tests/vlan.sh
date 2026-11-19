#!/bin/bash
# vlan.sh - VLANs and the trunk port with real hosts (Part 13 as an integration script, Part 18).
#   ports 1 and 3 in VLAN 10, port 2 in VLAN 20, port 4 in the default VLAN 1, port 5 the trunk.
#   A broadcast / unicast stays inside its VLAN; the trunk sees every VLAN and keeps the vlan field; a trunk frame with an invalid VLAN is dropped.
. "$(dirname "$0")/lib.sh"

CONF=$TMPD/vlan.conf
printf 'port.1.vlan = 10\nport.2.vlan = 20\nport.3.vlan = 10\ntrunk_port = 5\n' > "$CONF"
SW_ARGS=(-c "$CONF")
sw_start 5
A=$(mac 1); B=$(mac 2); C=$(mac 3); D=$(mac 4); Tm=$(mac 5)
for h in A B C D T; do
    case $h in A) m=$A ;; B) m=$B ;; C) m=$C ;; D) m=$D ;; T) m=$Tm ;; esac
    host_start $h "$m"
done
check "five hosts got ports 1-5" test "$(connected)" -eq 5

# settle: send one frame to every access VLAN through the trunk; when its receiver has printed it, every earlier frame to that port has been printed too
settle() {
    say T "sendx bcast 20 1 settle-b$1"; wait_out B "data=settle-b$1" || bad "B never printed settle-b$1"
    say T "sendx bcast 10 1 settle-c$1"; wait_out C "data=settle-c$1" || bad "C never printed settle-c$1"
    wait_out A "data=settle-c$1" || bad "A never printed settle-c$1"
    say T "sendx bcast 1 1 settle-d$1"; wait_out D "data=settle-d$1" || bad "D never printed settle-d$1"
}

echo "A broadcasts in VLAN 10: C and the trunk get it, B (VLAN 20) and D (VLAN 1) do not"
say A "send bcast a-bcast"
wait_out C "data=a-bcast"; wait_out T "data=a-bcast"
settle 1
check "C (VLAN 10) got it"            test "$(out_count C data=a-bcast)" -eq 1
check "the trunk got it, vlan=10"     test "$(out_count T 'vlan=10 prio=1 len=7 data=a-bcast')" -eq 1
check "B (VLAN 20) did not"           test "$(out_count B data=a-bcast)" -eq 0
check "D (VLAN 1) did not"            test "$(out_count D data=a-bcast)" -eq 0
check "A did not get its own frame"   test "$(out_count A data=a-bcast)" -eq 0

echo "B broadcasts in VLAN 20: only the trunk gets it"
say B "send bcast b-bcast"
wait_out T "data=b-bcast"
settle 2
check "the trunk got it, vlan=20"     test "$(out_count T 'vlan=20 prio=1 len=7 data=b-bcast')" -eq 1
check "nobody else did" test "$(( $(out_count A data=b-bcast) + $(out_count C data=b-bcast) + $(out_count D data=b-bcast) ))" -eq 0

echo "unicast across VLANs: A -> B is flooded inside VLAN 10 (C and the trunk), B never gets it"
say A "send $B a-to-b"
wait_out C "data=a-to-b"
settle 3
check "C and the trunk saw the flooded frame" test "$(( $(out_count C data=a-to-b) + $(out_count T data=a-to-b) ))" -eq 2
check "B did not get a frame from another VLAN" test "$(out_count B data=a-to-b)" -eq 0

echo "the trunk injects frames: the vlan field decides where they go"
barrier C c-learn A T                    # C is learned in VLAN 10, so a unicast to it is not flooded
say T "sendx bcast 20 1 t-vlan20"
say T "sendx bcast 10 1 t-vlan10"
say T "sendx $C 10 1 t-to-c"
wait_out B "data=t-vlan20"; wait_out A "data=t-vlan10"; wait_out C "data=t-to-c"
settle 4
check "VLAN 20 frame reached B only" test "$(out_count B data=t-vlan20)$(out_count A data=t-vlan20)$(out_count C data=t-vlan20)$(out_count D data=t-vlan20)" = 1000
check "VLAN 10 frame reached A and C, not B or D" test "$(out_count A data=t-vlan10)$(out_count C data=t-vlan10)$(out_count B data=t-vlan10)$(out_count D data=t-vlan10)" = 1100
check "an access host sees its own VLAN: C prints vlan=10" test "$(out_count C 'vlan=10 prio=1 len=8 data=t-vlan10')" -eq 1
check "a unicast to C (known in VLAN 10) reached C only" test "$(out_count C data=t-to-c)$(out_count A data=t-to-c)$(out_count B data=t-to-c)" = 100

echo "swctl shows the layout"
T ./swctl -p "$PORT" show ports > "$TMPD/ports.txt"
check "show ports: port 1 VLAN 10, port 2 VLAN 20, port 5 trunk" test "$(T awk '$1==1 && $3==10 {a++} $1==2 && $3==20 {a++} $1==5 && $4=="yes" {a++} END {print a+0}' "$TMPD/ports.txt")" -eq 3

echo "an invalid VLAN on the trunk is dropped and counted (a raw connection takes the trunk port: the real host cannot send one)"
check "shutdown 5 disconnects the trunk host" T ./swctl -p "$PORT" shutdown 5
host_reap T
check "up 5" T ./swctl -p "$PORT" up 5
raw_open TR
wait_file "$SWLOG" "port 5: host connected" 2 || bad "the raw connection did not get port 5"
raw_send TR "$Tm" ff:ff:ff:ff:ff:ff 5000 1 bad-vlan
raw_send TR "$Tm" ff:ff:ff:ff:ff:ff 20 1 tr-ok            # processed after the invalid one by the same receiver thread
wait_out B "data=tr-ok" || bad "B never got the valid trunk frame"
wait_file "$SWLOG" "trunk frame with invalid VLAN 5000" || bad "no log line for the invalid VLAN"
check "the valid trunk frame (vlan 20) reached B" test "$(out_count B 'vlan=20 prio=1 len=5 data=tr-ok')" -eq 1
check "nobody received the invalid frame" test "$(( $(out_count A data=bad-vlan) + $(out_count B data=bad-vlan) + $(out_count C data=bad-vlan) + $(out_count D data=bad-vlan) ))" -eq 0
raw_close TR

sw_stop
hosts_reap
finish
