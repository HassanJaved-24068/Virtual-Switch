#!/bin/bash
# idle.sh - "no busy waiting" (roadmap Part 18): an idle switch with hosts connected, a rate-limited port and the admin socket uses (almost) no CPU.
# Measured from /proc/<pid>/stat over 5 s. Skipped under valgrind (valgrind itself burns CPU); the ThreadSanitizer build has its own overhead, so the limit is the same
# generous 5 %.
. "$(dirname "$0")/lib.sh"

if [ "$VARIANT" = valgrind ]; then echo "idle [valgrind]: skipped (valgrind's own CPU use is not the switch's)"; exit 0; fi
CONF=$TMPD/idle.conf
printf 'port.2.rate = 30\nrate_limit = 100\n' > "$CONF"
SW_ARGS=(-c "$CONF")
sw_start 4
host_start A "$(mac 1)"; host_start B "$(mac 2)"; host_start C "$(mac 3)" --noread
barrier A hello B
say A "send bcast queued-for-the-limited-port"
sleep 1
HZ=$(T getconf CLK_TCK)
ticks() { T awk '{ print $14 + $15 }' "/proc/$SWPID/stat"; }
t0=$(ticks)
sleep 5
t1=$(ticks)
used=$((t1 - t0))
echo "  CPU used by the idle switch in 5 s: $used ticks of 1/$HZ s ($(T python3 -c "print('%.2f' % ($used / $HZ))") s = $(T python3 -c "print('%.1f' % (100.0 * $used / $HZ / 5))") %)"
check "the idle switch used at most 5 % of one CPU (<= $((HZ * 5 / 20)) ticks in 5 s)" test "$used" -le $((HZ * 5 / 20))
check "it has the expected threads (no thread was created by being idle)" test "$(T ls "/proc/$SWPID/task" | T wc -l)" -ge 8
sw_stop
host_quit C                             # --noread never sees the end of the connection: it ends on quit
hosts_reap
finish
