#!/bin/bash
# broadcast.sh - broadcast and multicast reach every other host exactly once, never the sender;
# size and header integrity; invalid sources; broadcasts during host churn.
# Our own integration test, written from the roadmap (flooding in Part 4, forwarding decision in
# Part 6). It is NOT an official public test: none has been supplied (status: see docs/Part8_Explanation.txt, section 6).
# (that the frame is shared, not copied, per port is checked by tests/test_refcount.c;
#  the valgrind variant of this script shows that the shared frames are all freed)
. "$(dirname "$0")/lib.sh"

sw_start 5     # 4 hosts + one free port for the raw sender and the hosts that come and go
for i in 1 2 3 4; do host_start H$i "$(mac $i)"; done

echo "every host broadcasts 5 frames, all four at the same time"
senders=()
for i in 1 2 3 4; do
    ( for k in 1 2 3 4 5; do say H$i "send bcast b$i-$k"; done ) &
    senders+=($!)
done
wait "${senders[@]}"            # only the four sender loops (a bare "wait" would also wait for the switch)
for i in 1 2 3 4; do
    wait_out H$i "RX" 15 || bad "H$i did not receive 15 frames"
done
for i in 1 2 3 4; do
    check "H$i received exactly 15 frames (5 from each of the 3 others)" test "$(out_count H$i RX)" -eq 15
    check "H$i never got its own broadcast" test "$(out_count H$i src=$(mac $i))" -eq 0
    for j in 1 2 3 4; do
        [ "$i" = "$j" ] && continue
        check "H$i got the 5 frames of H$j" test "$(out_count H$i "data=b$j-")" -eq 5
    done
done

echo "multicast is flooded like broadcast"
say H1 "send 01:00:5e:00:00:01 mc1"
for i in 2 3 4; do wait_out H$i "data=mc1" || bad "H$i did not get mc1"; done
barrier H1 m1 H2 H3 H4
check "H2, H3, H4 got mc1 once each" test "$(out_count H2 data=mc1)$(out_count H3 data=mc1)$(out_count H4 data=mc1)" = 111
check "H1 did not get mc1" test "$(out_count H1 data=mc1)" -eq 0

echo "size and header: empty, 1500-byte and vlan 4094 / prio 0 broadcasts arrive intact at every other host"
BIG=$(printf 'B%.0s' $(seq 1 1500))
say H1 "send bcast $BIG"
say H1 "send bcast"
say H1 "sendx bcast 4094 0 vlan-test"
for i in 2 3 4; do
    wait_out H$i "len=1500 data=$BIG" || bad "H$i did not get the 1500-byte broadcast"
    wait_out H$i "vlan=4094 prio=0 len=9 data=vlan-test" || bad "H$i did not get vlan 4094 / prio 0 intact"
    wait_out H$i "src=$(mac 1) dst=ff:ff:ff:ff:ff:ff vlan=1 prio=1 len=0 data=" || bad "H$i did not get the empty broadcast"
done
ok "H2, H3 and H4 got all three frames unchanged"
check "H1 did not get them back" test "$(out_count H1 len=1500)$(out_count H1 vlan-test)" = 00

echo "invalid sources: a broadcast source address is dropped; a multicast source is forwarded"
raw_open R
raw_send R ff:ff:ff:ff:ff:ff ff:ff:ff:ff:ff:ff 1 1 bad-src-bcast
raw_send R 01:00:5e:00:00:07 ff:ff:ff:ff:ff:ff 1 1 mcast-src
for i in 1 2 3 4; do wait_out H$i "data=mcast-src" || bad "H$i did not get the frame with a multicast source"; done
check "nobody got the frame whose source was the broadcast address" \
      test "$(out_count H1 bad-src)$(out_count H2 bad-src)$(out_count H3 bad-src)$(out_count H4 bad-src)" = 0000
check "the frame with a multicast source reached all four hosts (it came from a 5th port)" \
      test "$(out_count H1 data=mcast-src)$(out_count H2 data=mcast-src)$(out_count H3 data=mcast-src)$(out_count H4 data=mcast-src)" = 1111
raw_close R
wait_file "$SWLOG" "teardown complete" 1 || bad "the raw connection was not cleaned up"

echo "hosts come and go while H1 broadcasts 40 frames: the stable hosts H2 and H3 get all 40"
sender=$(for k in $(seq 1 40); do echo "send bcast churn-$k"; done)
( while read -r line; do say H1 "$line"; done <<<"$sender" ) &
senders=($!)
for k in 1 2 3 4 5; do
    host_start X$k "$(mac $((20 + k)))" --quiet
    host_quit X$k
done
wait "${senders[@]}"
for i in 2 3; do wait_out H$i "data=churn-40" || bad "H$i did not get churn-40"; done
check "H2 got exactly the 40 churn broadcasts" test "$(out_count H2 data=churn-)" -eq 40
check "H3 got exactly the 40 churn broadcasts" test "$(out_count H3 data=churn-)" -eq 40

echo "a host that left no longer receives; the others still do"
host_quit H4
say H1 "send bcast after-leave"
for i in 2 3; do wait_out H$i "data=after-leave" || bad "H$i did not get after-leave"; done
check "H2 and H3 got after-leave" test "$(out_count H2 data=after-leave)$(out_count H3 data=after-leave)" = 11

sw_stop
hosts_reap
finish
