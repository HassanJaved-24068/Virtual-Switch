#!/bin/bash
# rate.sh - rate limiting with real hosts (Part 12 as an integration script, Part 18): port 2 is limited to 50 frames/s; A sends broadcasts at about 400 frames/s;
# C (unlimited) receives (almost) all of them, B (limited) only about burst + rate * time; the receiver of A is not slowed down.
. "$(dirname "$0")/lib.sh"

CONF=$TMPD/rate.conf
printf 'port.2.rate = 50\n' > "$CONF"
SW_ARGS=(-c "$CONF")
sw_start 3
host_start A "$(mac 1)" --quiet
host_start B "$(mac 2)" --quiet
host_start C "$(mac 3)" --quiet
check "the switch logged the limit of port 2" test "$(cnt "$SWLOG" 'port 2: rate limit 50 frames/s')" -eq 1

SECS=${RATE_SECS:-5}
PER=40                                   # lines per 0.1 s = 400 frames/s
echo "A sends $PER broadcasts every 0.1 s for $SECS s"
t0=$(T date +%s.%N)
for ((i = 0; i < SECS * 10; i++)); do
    yes "send bcast r" | head -n "$PER" | T cat >&"${HFD[A]}"
    nap 0.1
done
SENT=$((SECS * 10 * PER))
sleep 1
t1=$(T date +%s.%N)
host_quit B
host_quit C
RB=$(T sed -n 's/^RX_TOTAL //p' "$(out B)"); RC=$(T sed -n 's/^RX_TOTAL //p' "$(out C)")
RB=${RB:-0}; RC=${RC:-0}
el=$(T python3 -c "print(int(float('$t1') - float('$t0')) + 1)")
echo "  A sent $SENT frames in about $el s; C (unlimited) received $RC, B (50/s) received $RB"
check "C, unlimited, received at least 90 % of the frames" test "$RC" -ge $((SENT * 9 / 10))
check "B got at least the burst of one second (50 frames)" test "$RB" -ge 50
check "B got no more than burst + rate * time (50 + 50 * $((el + 1)) = $((50 + 50 * (el + 1))))" test "$RB" -le $((50 + 50 * (el + 1)))
check "B got far fewer frames than C (the limit works)" test "$((RB * 3))" -lt "$RC"
check "the limited port did not stop the others: the switch is running" kill -0 "$SWPID"

say A quit
sw_stop
hosts_reap
finish
