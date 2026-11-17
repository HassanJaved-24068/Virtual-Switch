#!/bin/bash
# slow_host.sh - a host that never reads (--noread) fills its own bounded queue and loses frames;
# the other hosts keep receiving and the sender is not slowed down (no head-of-line blocking).
# The roadmap's criterion is relative ("about the same as without C"), so the same flood is run
# twice: first with C reading (baseline), then with C --noread, and the two are compared.
. "$(dirname "$0")/lib.sh"

N=${SLOW_FRAMES:-8000}           # frames of ~1 KB: far more than the queue and the socket buffers of C hold
PAYLOAD=$(printf 'z%.0s' $(seq 1 1000))
# How much of the baseline count B and D must keep. Counts are noisy in EVERY variant: identical runs gave
# B = 928 ... 2096 frames under valgrind and D = 4340 ... 7392 in a normal run (a 60 % bound failed once in 16
# normal runs and twice under valgrind). A real head-of-line problem would send them towards 0, so only a large
# loss counts. The time check below (A's flood is not slowed down) is the real head-of-line test.
MINPCT=30
now_ms() { echo $(($(T date +%s%N) / 1000000)); }

# flood_run PREFIX C_OPTION : hosts are named PREFIXA..PREFIXD (a name is used once); sets FLOOD_MS, RB, RD.
# The caller stops the switch (with C still connected) and then the hosts.
flood_run() {
    local p=$1
    HPID=(); HFD=(); HRC=()
    sw_start 4
    host_start ${p}A "$(mac 1)" --quiet
    host_start ${p}B "$(mac 2)" --quiet
    host_start ${p}C "$(mac 3)" $2        # port 3
    host_start ${p}D "$(mac 4)" --quiet
    local t0 t1
    t0=$(now_ms)
    yes "send bcast $PAYLOAD" | head -n "$N" | T cat >&"${HFD[${p}A]}"
    t1=$(now_ms)
    FLOOD_MS=$((t1 - t0))
    host_quit ${p}B
    host_quit ${p}D
    RB=$(T sed -n 's/^RX_TOTAL //p' "$(out ${p}B)")
    RD=$(T sed -n 's/^RX_TOTAL //p' "$(out ${p}D)")
    RB=${RB:-0}; RD=${RD:-0}
}

echo "run 1 (baseline): C reads; A floods $N broadcast frames of 1000 bytes"
flood_run b --quiet
BASE_MS=$FLOOD_MS; BASE_B=$RB; BASE_D=$RD
echo "  flood accepted in $BASE_MS ms; B received $BASE_B, D received $BASE_D"
check "baseline: the switch is running" kill -0 "$SWPID"
sw_stop
host_quit bC
host_quit bA
hosts_reap

echo "run 2: the same flood, C is --noread (its port 3 never drains)"
flood_run s --noread
echo "  flood accepted in $FLOOD_MS ms; B received $RB, D received $RD"
wait_file "$SWLOG" "port 3: output queue full" || bad "the queue of port 3 never filled"
check "the queue of the --noread host (port 3) is full and its frames are dropped" \
      test "$(cnt "$SWLOG" 'port 3: output queue full')" -ge 1
check "the switch is still running" kill -0 "$SWPID"
check "A's flood was not slowed down: $FLOOD_MS ms vs $BASE_MS ms without a slow host (limit 2.5 x + 2 s)" \
      test "$FLOOD_MS" -le $((BASE_MS * 5 / 2 + 2000))
check "B kept receiving: $RB vs $BASE_B in the baseline (at least $MINPCT %)" test "$((RB * 100))" -ge $((BASE_B * MINPCT))
check "D kept receiving: $RD vs $BASE_D in the baseline (at least $MINPCT %)" test "$((RD * 100))" -ge $((BASE_D * MINPCT))
check "B and D received frames at all (the absolute numbers depend on the speed of the variant)" test "$RB" -gt 0 -a "$RD" -gt 0

echo "SIGINT with the slow host still connected and its queue full"
sw_stop
host_quit sC         # a --noread host never reads, so it cannot notice that the switch closed the connection
host_quit sA
hosts_reap
finish
