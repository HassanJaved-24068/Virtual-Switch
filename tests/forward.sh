#!/bin/bash
# forward.sh - learning, unicast, flooding of unknown destinations, filtering, header and payload
# integrity, ordering, stale entries after a host leaves, clean SIGINT shutdown.
# Our own integration test, written from the roadmap's forwarding decision (Part 6) and its
# scenario "A -> C is flooded until C has sent once, then only C gets it". It is NOT an official
# public test: none has been supplied (status: see docs/Part8_Explanation.txt, section 6).
. "$(dirname "$0")/lib.sh"

sw_start 4
A=$(mac 1); B=$(mac 2); C=$(mac 3); D=$(mac 4)
for h in A B C D; do host_start $h "${!h}"; done
check "four hosts got ports 1-4" test "$(connected)" -eq 4

echo "unknown destination: A -> C before C has sent anything is flooded"
say A "send $C u1"
for h in B C D; do wait_out $h "data=u1" || bad "$h did not get the flooded frame u1"; done
check "B, C and D all received u1 (flooded)" test "$(out_count B data=u1)$(out_count C data=u1)$(out_count D data=u1)" = 111
check "A did not get its own frame back" test "$(out_count A data=u1)" -eq 0

echo "learning: C sends once, then A -> C reaches only C"
barrier C c-hello A B D
say A "send $C u2"
wait_out C "data=u2" || bad "C did not get u2"
barrier A m1 B C D        # when B and D have m1, u2 would have reached them already
check "C got u2" test "$(out_count C data=u2)" -eq 1
check "B did not get u2 (learned unicast)" test "$(out_count B data=u2)" -eq 0
check "D did not get u2 (learned unicast)" test "$(out_count D data=u2)" -eq 0

echo "the reverse direction: C -> A is unicast too (A was learned from its first frame)"
say C "send $A u3"
wait_out A "data=u3" || bad "A did not get u3"
barrier C m2 A B D
check "B did not get u3" test "$(out_count B data=u3)" -eq 0
check "D did not get u3" test "$(out_count D data=u3)" -eq 0

echo "another unknown: B -> D is flooded until D has sent, then it is unicast"
say B "send $D u4"
for h in A C D; do wait_out $h "data=u4" || bad "$h did not get u4"; done
check "A, C and D received the flooded u4" test "$(out_count A data=u4)$(out_count C data=u4)$(out_count D data=u4)" = 111
barrier D d-hello A B C
say B "send $D u5"
wait_out D "data=u5" || bad "D did not get u5"
barrier B m3 A C D
check "A and C did not get u5" test "$(out_count A data=u5)$(out_count C data=u5)" = 00

echo "traffic continues: 40 unicast frames in a ring A->B->C->D->A"
for i in $(seq 1 10); do
    say A "send $B ring-a-$i"; say B "send $C ring-b-$i"; say C "send $D ring-c-$i"; say D "send $A ring-d-$i"
done
wait_out B "data=ring-a-10" && wait_out C "data=ring-b-10" && wait_out D "data=ring-c-10" && wait_out A "data=ring-d-10" \
    || bad "the ring did not complete"
check "B got exactly the 10 frames of A" test "$(out_count B data=ring-a-)" -eq 10
check "C got exactly the 10 frames of B" test "$(out_count C data=ring-b-)" -eq 10
check "D got exactly the 10 frames of C" test "$(out_count D data=ring-c-)" -eq 10
check "A got exactly the 10 frames of D" test "$(out_count A data=ring-d-)" -eq 10
check "no ring frame was flooded to a third host" \
      test "$(out_count A ring-a-)$(out_count A ring-b-)$(out_count A ring-c-)$(out_count B ring-b-)$(out_count B ring-c-)$(out_count B ring-d-)" = 000000

echo "ordering: the 10 frames of A for B arrived in the order they were sent"
want=$(for i in $(seq 1 10); do printf 'data=ring-a-%s ' "$i"; done)
got=$(T grep -o 'data=ring-a-[0-9]*' "$(out B)" | T tr '\n' ' ')
check "B got ring-a-1 .. ring-a-10 in order" test "$got" = "$want"

echo "header fields and payload sizes survive the switch (vlan, prio, 0 and 1500 bytes)"
say A "sendx bcast 100 2 hdr-test"
for h in B C D; do wait_out $h "vlan=100 prio=2 len=8 data=hdr-test" || bad "$h did not get vlan 100 / prio 2 intact"; done
ok "B, C and D got the frame with vlan=100 prio=2 unchanged"
BIG=$(printf 'M%.0s' $(seq 1 1500))
say A "send $B $BIG"
say A "send $B"
wait_out B "len=1500 data=$BIG" || bad "B did not get the 1500-byte payload intact"
wait_out B "src=$A dst=$B vlan=1 prio=1 len=0 data=" || bad "B did not get the empty payload"
barrier A m4 B C D
check "the 1500-byte and the empty frame went to B only" \
      test "$(out_count C len=1500)$(out_count D len=1500)$(out_count C 'len=0 ')$(out_count D 'len=0 ')" = 0000

echo "filtering: a frame for a MAC behind the ingress port is dropped, not flooded"
say A "send $A to-myself"
barrier A m5 B C D
check "nobody got A's frame addressed to A (A, B, C, D)" \
      test "$(out_count A to-myself)$(out_count B to-myself)$(out_count C to-myself)$(out_count D to-myself)" = 0000

echo "a host that leaves is forgotten: frames for its MAC are flooded again"
host_quit C
wait_file "$SWLOG" "port 3: teardown complete, port is FREE" || bad "port 3 was not reclaimed"
say A "send $C gone-1"
for h in B D; do wait_out $h "data=gone-1" || bad "$h did not get the frame for the departed C (flood)"; done
check "B and D got it (the entry of C was flushed, so it is unknown: flooded)" \
      test "$(out_count B data=gone-1)$(out_count D data=gone-1)" = 11

sw_stop
hosts_reap
finish
