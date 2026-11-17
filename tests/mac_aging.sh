#!/bin/bash
# mac_aging.sh - a learned MAC disappears after the aging time T (3 s in switch_small) and
# frames for it are flooded again. Uses switch_small (T = 3 s, K = 4 entries, M = 3 MACs per port).
. "$(dirname "$0")/lib.sh"

sw_start 4 small
A=$(mac 1); B=$(mac 2); C=$(mac 3)
host_start A "$A"; host_start B "$B"; host_start C "$C"

echo "C is learned: A -> C is unicast"
barrier C c-hello A B
say A "send $C before-aging"
wait_out C "data=before-aging" || bad "C did not get the frame"
barrier A m1 B C
check "B did not get A's frame for C (C is known)" test "$(out_count B data=before-aging)" -eq 0
learned=$SECONDS

echo "C stays silent. A keeps probing; once C's entry has aged the probe is flooded to B"
start=$(T date +%s.%N)
n=0
while [ "$(out_count B 'data=probe-')" -eq 0 ]; do
    n=$((n + 1))
    say A "send $C probe-$n"
    nap 0.25
    [ "$((SECONDS - learned))" -ge "$TMO" ] && break
done
took=$(T python3 -c "import sys; print('%.1f' % (float(sys.argv[2]) - float(sys.argv[1])))" "$start" "$(T date +%s.%N)")
echo "  the first probe that reached B was number $n, after $took s"
check "an aged-out MAC is flooded again (B got a probe for C)" test "$(out_count B 'data=probe-')" -ge 1
check "the entry lived at least 2 s (T = 3 s, not removed early): $took s" \
      test "$(T python3 -c "print(1 if float('$took') >= 2.0 else 0)")" -eq 1
check "the entry was gone within 10 s (T = 3 s + aging period + slow variants): $took s" \
      test "$(T python3 -c "print(1 if float('$took') <= 10.0 else 0)")" -eq 1
check "C received every probe (flooding reaches the real owner too)" \
      test "$(out_count C 'data=probe-')" -eq "$n"

echo "C talks again: it is learned again and unicast resumes"
barrier C c-again A B
say A "send $C after-relearn"
wait_out C "data=after-relearn" || bad "C did not get the frame after relearning"
barrier A m2 B C
check "B did not get the frame for the relearned C" test "$(out_count B data=after-relearn)" -eq 0

sw_stop
hosts_reap
finish
