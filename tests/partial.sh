#!/bin/bash
# partial.sh - frames that arrive one byte at a time, or cut anywhere, are rebuilt correctly and
# frame boundaries stay right.
. "$(dirname "$0")/lib.sh"

sw_start 4
host_start A "$(mac 1)" --partial       # sends every byte as its own write, 1 ms apart
host_start B "$(mac 2)"
host_start C "$(mac 3)"
LONG=$(printf 'L%.0s' $(seq 1 200))

echo "A (one byte at a time) and C (normal) send at the same time"
say A "send bcast alpha"
say A "send bcast $LONG"
say A "send bcast"                      # empty payload
say A "send bcast omega"
for k in 1 2 3 4 5 6; do say C "send bcast c$k"; done
wait_out B "data=omega" || bad "B never got A's last frame"
wait_out B "data=c6"    || bad "B never got C's last frame"
check "B got 10 frames in total" test "$(out_count B RX)" -eq 10

T grep "src=$(mac 1)" "$(out B)" | T sed 's/ vlan=.*len=/ len=/' >"$TMPD/a.got"
printf '%s\n' "RX src=$(mac 1) dst=ff:ff:ff:ff:ff:ff len=5 data=alpha" \
              "RX src=$(mac 1) dst=ff:ff:ff:ff:ff:ff len=200 data=$LONG" \
              "RX src=$(mac 1) dst=ff:ff:ff:ff:ff:ff len=0 data=" \
              "RX src=$(mac 1) dst=ff:ff:ff:ff:ff:ff len=5 data=omega" >"$TMPD/a.want"
check "A's four frames arrived intact and in order (lengths 5, 200, 0, 5)" T diff "$TMPD/a.got" "$TMPD/a.want"
check "C's frames c1..c6 arrived in order" test "$(T grep "src=$(mac 3)" "$(out B)" | T sed 's/.*data=//' | T tr '\n' ' ')" = "c1 c2 c3 c4 c5 c6 "

echo "a raw sender cuts frames at arbitrary places: 1.5 frames in one write, then the rest"
raw_open R
RM=$(mac 9)
{ raw_frame_bytes "$RM" ff:ff:ff:ff:ff:ff 1 1 first-frame; raw_frame_bytes "$RM" ff:ff:ff:ff:ff:ff 1 1 second-frame; } >"$TMPD/two.bin"
size=$(T stat -c %s "$TMPD/two.bin")
cut1=$((17 + 11 + 8))                         # all of frame 1 and 8 bytes of the header of frame 2
T head -c "$cut1" "$TMPD/two.bin" >&"${RFD[R]}"
nap 0.1                                       # on purpose: lets TCP deliver the first piece before the rest
T tail -c +$((cut1 + 1)) "$TMPD/two.bin" >&"${RFD[R]}"
raw_send R "$RM" ff:ff:ff:ff:ff:ff 1 1 third-frame
wait_out B "data=third-frame" || bad "B never got the third frame"
check "the two cut frames and the next one arrived whole" \
      test "$(out_count B "data=first-frame")$(out_count B "data=second-frame")$(out_count B "data=third-frame")" = 111
check "A and C got them too" test "$(out_count A data=third-frame)$(out_count C data=third-frame)" = 11
raw_close R

sw_stop
hosts_reap
finish
