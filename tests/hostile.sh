#!/bin/bash
# hostile.sh - the hidden-test simulation (roadmap Part 18): 32 hosts, several of them --noread, --partial, flooding and killed with kill -9 mid-stream, or dying
# in the middle of a frame; admin commands and signals run throughout. The switch must stay up and the other hosts must keep their traffic.
#
#   ports 1-20   N1-N20  well-behaved hosts (--quiet); N1-N4 send broadcasts all the time, N5-N20 receive
#   ports 21-23  R21-R23 --noread (never read: their queues fill)
#   ports 24-26  P24-P26 --partial (send one byte at a time)
#   ports 27-29  F27-F29 flood broadcasts as fast as they can and are killed with kill -9 after a while
#   ports 30-32  D30-D32 --die-midframe (send half a frame and vanish)
#   throughout:  `mirror 1 21` (a sender mirrored to a sniffer that never reads); HOSTILE_ROUNDS random shutdown / up / vlan / mirror / clear mac commands on the
#                hostile ports 21-32; SIGHUP + SIGUSR1 every 0.2 s.
#   checks:      every swctl command answers (0 or 1); the receivers N5-N20 keep receiving (their `out` counters grow at every sample); no well-behaved host was
#                disconnected; the killed hosts' ports are torn down and come back (new hosts get them); no leaked token (after `up` on every port the free-port
#                semaphore matches); clean exit and no sanitizer / valgrind report.
# A flooder that is killed has megabytes of frames in the kernel's socket buffers; a slow (valgrind / TSan) switch must process that backlog before it sees the end of the
# connection, so under those variants the flooders send a finite number of frames (bounded backlog) and every wait for a teardown gets a generous timeout.
FLOODN=1000000000
case "${VARIANT:-normal}" in tsan) TMO=${TMO:-90}; FLOODN=20000 ;; valgrind) TMO=${TMO:-120}; FLOODN=6000 ;; esac
. "$(dirname "$0")/lib.sh"

ROUNDS=${HOSTILE_ROUNDS:-150}
PER=20; FLOOD_S=2
case "$VARIANT" in valgrind|tsan) ROUNDS=${HOSTILE_ROUNDS:-80}; PER=4; FLOOD_S=3 ;; esac
CONF=$TMPD/hostile.conf
write_conf() { printf 'mac_aging_seconds = %s\nqueue_capacity = 64\nport_wait_timeout = 2\nmac_dump_file = %s/mac.dump\n' "$1" "$TMPD" > "$CONF.tmp" && mv "$CONF.tmp" "$CONF"; }
write_conf 30
SW_ARGS=(-c "$CONF")
sw_start 32
ctl() { T ./swctl -p "$PORT" "$@"; }
out_of() { ctl stats 2>/dev/null | T awk -v p="$1" '$1 == p { print $5 }'; }

EXTRA=()
trap 'for p in "${EXTRA[@]}"; do kill -9 "$p" 2>/dev/null; done; cleanup' EXIT

echo "starting 32 hosts"
for i in $(seq 1 20); do host_start N$i "$(mac $i)" --quiet; done
for i in 21 22 23; do host_start R$i "$(mac $i)" --noread; done
for i in 24 25 26; do host_start P$i "$(mac $i)" --partial; done
check "26 hosts connected" test "$(connected)" -eq 26

stop=$TMPD/stop
traffic() {
    local k=0 h
    while [ ! -e "$stop" ]; do
        for h in N1 N2 N3 N4; do
            for ((j = 0; j < PER; j++)); do printf 'send bcast h%s-%s\n' "$k" "$j"; done >&"${HFD[$h]}" 2>/dev/null || return
        done
        k=$((k + 1))
        sleep 0.1
    done
}
traffic &
EXTRA+=($!)
signal_loop() {
    local n=0
    while [ ! -e "$stop" ] && kill -0 "$SWPID" 2>/dev/null; do
        n=$((n + 1)); write_conf $((30 + n % 2))
        kill -HUP "$SWPID" 2>/dev/null; kill -USR1 "$SWPID" 2>/dev/null
        sleep 0.2
    done
}
signal_loop &
EXTRA+=($!)
sleep 1
check "a sender is mirrored to a sniffer that never reads (mirror 1 21)" ctl mirror 1 21

echo "three flooders join, three hosts die in the middle of a frame"
for i in 27 28 29; do host_start F$i "$(mac $i)" --flood "$FLOODN" --to bcast; done
for i in 30 31 32; do host_start D$i "$(mac $i)" --die-midframe; done
sleep "$FLOOD_S"
echo "kill -9 the three flooders mid-stream"
for i in 27 28 29; do
    kill -9 "${HPID[F$i]}" 2>/dev/null
    wait "${HPID[F$i]}" 2>/dev/null
    unset "HPID[F$i]"
done

echo "$ROUNDS admin commands on the hostile ports, with SIGHUP / SIGUSR1 every 0.2 s and the traffic running"
bad_rc=0; grew=1; last=()
sample() { # sample LABEL: the out counters of the pure receivers must have grown since the last sample
    local p v k=0
    for p in 5 8 12 16 20; do
        v=$(out_of "$p"); v=${v:-0}
        if [ -n "${last[$k]:-}" ] && [ "$v" -le "${last[$k]}" ]; then grew=0; echo "  port $p: out did not grow ($v <= ${last[$k]}) at $1"; fi
        last[$k]=$v; k=$((k + 1))
    done
}
sample start
for ((r = 1; r <= ROUNDS; r++)); do
    p=$((RANDOM % 12 + 21)); q=$((RANDOM % 12 + 21))
    case $((RANDOM % 8)) in
        0) cmd=(shutdown "$p") ;;
        1|2) cmd=(up "$p") ;;
        3) cmd=(vlan "$p" $((RANDOM % 3 + 1))) ;;
        4) cmd=(mirror "$p" "$q") ;;
        5) cmd=(mirror "$p" off) ;;
        6) cmd=(clear mac) ;;
        7) cmd=(show ports) ;;
    esac
    ctl "${cmd[@]}" >/dev/null 2>&1; rc=$?
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then bad_rc=$((bad_rc + 1)); echo "  swctl ${cmd[*]} -> exit $rc (round $r)"; fi
    if [ $((r % (ROUNDS / 3 + 1))) -eq 0 ]; then sample "round $r"; fi
done
sample end
: > "$stop"
for p in "${EXTRA[@]}"; do wait "$p" 2>/dev/null; done
EXTRA=()

check "every admin command answered OK or ERR (exit 0 or 1): $bad_rc bad" test "$bad_rc" -eq 0
check "the switch is still running" kill -0 "$SWPID"
check "the receivers N5-N20 kept receiving: their out counters grew at every sample" test "$grew" -eq 1
alive=0
for i in $(seq 1 20); do kill -0 "${HPID[N$i]}" 2>/dev/null && alive=$((alive + 1)); done
check "all 20 well-behaved hosts are still connected (none was disconnected by the others' behaviour)" test "$alive" -eq 20
wait_file "$SWLOG" "teardown complete" 6 || true       # the backlog of a killed flooder is processed first (slow variants)
check "the killed and dying hosts were torn down (at least 6 teardowns logged)" test "$(torn_down)" -ge 6
check "no [ERR] line so far" test "$(cnt "$SWLOG" '[ERR]')" -eq 0

echo "ports come back: up on every hostile port, then new hosts get them"
no_closing() { # no_closing : wait until no port is CLOSING (the reaper has finished what it started)
    local end=$((SECONDS + TMO))
    while [ "$SECONDS" -lt "$end" ]; do
        ctl show ports | T grep -q CLOSING || return 0
        nap 0.5
    done
    return 1
}
no_closing || bad "a port is still CLOSING after ${TMO}s"
for p in $(seq 21 32); do ctl up "$p" >/dev/null 2>&1; done
sleep 1
no_closing || bad "a port is still CLOSING after ${TMO}s"
for p in $(seq 21 32); do ctl up "$p" >/dev/null 2>&1; done
ctl show ports > "$TMPD/ports.txt"
check "ports 27-32 are FREE or UP again (not stuck in CLOSING or DISABLED)" test "$(T awk '$1 >= 27 && $1 <= 32 && ($2 == "FREE" || $2 == "UP") {a++} END {print a+0}' "$TMPD/ports.txt")" -eq 6
free=$(T awk '$2 == "FREE" {a++} END {print a+0}' "$TMPD/ports.txt")
echo "  $free ports are FREE: $free new hosts must get a port, the next one 'switch full'"
before=$(connected)
for ((i = 1; i <= free; i++)); do host_spawn X$i "$(mac $((40 + i)))" --quiet; done
wait_file "$SWLOG" "host connected" $((before + free)) || true
check "$free new hosts got a port: the semaphore matches the number of FREE ports" test "$(($(connected) - before))" -eq "$free"
host_spawn Z "$(mac 60)" --quiet
wait "${HPID[Z]}" 2>/dev/null; zrc=$?
unset 'HPID[Z]'
check "one more is turned away with SWITCH FULL (exit status 2, got $zrc)" test "$zrc" -eq 2

sw_stop
for i in 21 22 23; do host_quit R$i; done      # --noread hosts never see the end of the connection: they end on quit
hosts_reap
finish
