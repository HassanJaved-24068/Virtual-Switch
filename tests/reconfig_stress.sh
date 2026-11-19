#!/bin/bash
# reconfig_stress.sh - Part 16 completion criterion (roadmap): live reconfiguration under traffic, no hang, no race.
#
#   8 hosts; hosts 5-8 flood broadcasts all the time; a loop runs shutdown / up / vlan / clear mac on random ports 1-4
#   ROUNDS times (default 500), while SIGHUP (with a changed configuration file) and SIGUSR1 arrive every 0.2 s; then
#   SIGINT. Checks: every swctl command answers (exit 0 = OK or 1 = ERR, never 2 = switch gone, never a timeout); the hosts
#   on the untouched ports 5-8 keep receiving throughout (their `out` counters grow between every sample); afterwards the
#   free-port semaphore still equals the number of FREE ports (4 hosts get the 4 ports, the 5th gets "switch full"); the
#   switch exits 0 and, under the valgrind / TSan variants, with no error. The whole script runs under a 60 s * 5 budget:
#   every command and wait inside it has its own timeout (lib.sh), so a deadlock fails the test instead of hanging it.
#
#   ROUNDS=500 ./tests/reconfig_stress.sh        VARIANT=tsan|valgrind ./tests/reconfig_stress.sh
. "$(dirname "$0")/lib.sh"

ROUNDS=${ROUNDS:-500}
CONF=$TMPD/stress.conf
write_conf() { # write_conf AGING : an atomic replace, so that SIGHUP never reads half a file
    printf 'mac_aging_seconds = %s\nqueue_capacity = 64\nmac_dump_file = %s/mac.dump\nport_wait_timeout = 2\n' "$1" "$TMPD" > "$CONF.tmp" && mv "$CONF.tmp" "$CONF"
}
ctl() { T ./swctl -p "$PORT" "$@"; }
# out_of PORT : the `out` counter of a port from the shared memory (swctl stats): PORT STATE VLAN IN OUT DROPPED MIRRORED
out_of() { ctl stats 2>/dev/null | awk -v p="$1" '$1 == p { print $5 }'; }

write_conf 30
SW_ARGS=(-c "$CONF")
sw_start 8
for i in 1 2 3 4 5 6 7 8; do host_start H$i "$(mac $i)" --quiet; done
check "eight hosts got ports 1-8" test "$(connected)" -eq 8

# the flooders: hosts 5-8 send broadcasts for the whole test (a real ./host, --flood N, ends by itself or at EOF)
echo "hosts 5-8 flood broadcasts; $ROUNDS random admin operations on ports 1-4; SIGHUP + SIGUSR1 every 0.2 s"
for i in 5 6 7 8; do say H$i "send bcast warm-up"; done
sleep 1

signal_loop() {
    local n=0
    while kill -0 "$SWPID" 2>/dev/null; do
        n=$((n + 1))
        write_conf $((30 + n % 2))               # a changed value, so that every SIGHUP really reloads
        kill -HUP "$SWPID" 2>/dev/null
        kill -USR1 "$SWPID" 2>/dev/null
        sleep 0.2
    done
}
signal_loop &
SIGPID=$!

# frames from hosts 5-8 to each other keep flowing: every host sends 1 broadcast per command, 4 hosts in parallel
flood_loop() {
    local i
    for ((n = 0; n < 100000; n++)); do
        for i in 5 6 7 8; do printf '%s\n' "send bcast f$n" >&"${HFD[H$i]}" 2>/dev/null || return; done
    done
}
flood_loop &
FLPID=$!

bad_rc=0
samples=()
last=(0 0 0 0)
grew=1
for ((r = 1; r <= ROUNDS; r++)); do
    p=$((RANDOM % 4 + 1))
    case $((RANDOM % 5)) in
        0) cmd=(shutdown "$p") ;;
        1) cmd=(up "$p") ;;
        2) cmd=(vlan "$p" $((RANDOM % 3 + 1))) ;;
        3) cmd=(clear mac) ;;
        4) cmd=(up "$p") ;;                     # up twice as often as shutdown: the ports come back
    esac
    ctl "${cmd[@]}" >/dev/null 2>&1
    rc=$?
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
        bad_rc=$((bad_rc + 1))
        echo "  swctl ${cmd[*]} -> exit $rc (round $r)"
    fi
    if [ $((r % 100)) -eq 0 ]; then              # the untouched ports 5-8 must keep receiving
        k=0
        for i in 5 6 7 8; do
            v=$(out_of "$i")
            v=${v:-0}
            if [ "$v" -le "${last[$k]}" ]; then grew=0; echo "  port $i: out counter did not grow ($v <= ${last[$k]}) at round $r"; fi
            last[$k]=$v
            k=$((k + 1))
        done
    fi
done
kill "$FLPID" 2>/dev/null; wait "$FLPID" 2>/dev/null
kill "$SIGPID" 2>/dev/null; wait "$SIGPID" 2>/dev/null

check "all $ROUNDS admin commands answered OK or ERR (exit 0 or 1), none timed out and the switch never went away" test "$bad_rc" -eq 0
check "the switch is still running" kill -0 "$SWPID"
check "the hosts on the untouched ports 5-8 kept receiving: their out counters grew at every sample" test "$grew" -eq 1
check "the switch reloaded its configuration during the run (SIGHUP)" test "$(cnt "$SWLOG" 'configuration reloaded')" -ge 5
check "no 'reload failed' although the file changed all the time" test "$(cnt "$SWLOG" 'reload failed')" -eq 0

# The invariant (free-port semaphore = FREE ports): bring every port up, then 4 new hosts must get ports 1-4 and the 5th "switch full".
echo "after $ROUNDS operations: the semaphore still matches the FREE ports"
for p in 1 2 3 4; do ctl up "$p" >/dev/null 2>&1; done
sleep 1                                          # the reaper finishes teardowns that were still queued
for p in 1 2 3 4; do ctl up "$p" >/dev/null 2>&1; done
for i in 1 2 3 4; do host_quit H$i 2>/dev/null; done     # the old hosts of ports 1-4 (if they are still connected)
sleep 1
free=$(ctl show ports | awk '$1 >= 1 && $1 <= 4 && $2 == "FREE" { n++ } END { print n + 0 }')
check "ports 1-4 are all FREE again (got $free)" test "$free" -eq 4
before=$(connected)
for i in 1 2 3 4; do host_spawn G$i "$(mac $((20 + i)))" --quiet; done
wait_file "$SWLOG" "host connected" $((before + 4)) || true
check "four new hosts got the four ports (the semaphore had 4 tokens)" test "$(($(connected) - before))" -eq 4
host_spawn X "$(mac 30)" --quiet
HPX=${HPID[X]}
wait "$HPX" 2>/dev/null; xrc=$?
check "a fifth host was turned away with SWITCH FULL (exit status 2, got $xrc): no token too many" test "$xrc" -eq 2

sw_stop
hosts_reap
finish
