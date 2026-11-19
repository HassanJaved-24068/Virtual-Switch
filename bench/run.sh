#!/bin/bash
# bench/run.sh - run the benchmark of Part 21 and produce the CSV files, the graph and the report.
#
#   bench/run.sh            the full run (about 10 minutes)
#   bench/run.sh --quick    1 s per point, 1 run, a few points: a check that everything works (not for the report)
#
# Variables (defaults): SECS=5 seconds per point, REPS=3 runs per point (the report uses the MEDIAN), PAYLOAD=100 bytes, SEED=12345 (the random choices of the load generators
# come from fixed seeds), THREADS="1 2 4 8 16" (in-process benchmark), HOSTS="2 4 8 16" (network benchmark: with one host nothing can be delivered), OUT=bench.
#
# What it does: builds everything (make bench; -O0 like the rest of the project), then for every point and every repetition runs the three variants one after the other
# (so that a drift of the machine hits all three alike):
#   bench/bench_mac_<variant> <threads> <seconds> <seed>        the MAC table alone, T threads, 90 % lookup / 10 % learn
#   ./switch_<variant> <free tcp port> 16  +  bench/bench 127.0.0.1 <port> <hosts> <seconds> <payload> <seed>      the whole switch, H hosts, unicast flood
# Raw numbers: $OUT/raw_mac.csv and $OUT/raw_net.csv. bench/plot.py then writes $OUT/results.csv (medians), $OUT/throughput.png and $OUT/report.pdf.
#
# Do not run it while another switch is running or while the tests run: the switches use the fixed shared-memory name /vswitch_stats_P, and the machine must be quiet.
set -u
cd "$(dirname "$0")/.." || exit 2

SECS=${SECS:-5}; REPS=${REPS:-3}; PAYLOAD=${PAYLOAD:-100}; SEED=${SEED:-12345}
THREADS=${THREADS:-"1 2 4 8 16"}; HOSTS=${HOSTS:-"2 4 8 16"}; OUT=${OUT:-bench}
VARIANTS="global striped rwlock"
if [ "${1:-}" = "--quick" ]; then SECS=1; REPS=1; THREADS="1 2"; HOSTS="2 4"; fi

make -s bench || { echo "run.sh: make bench failed" >&2; exit 1; }
mkdir -p "$OUT"
TMPD=$(mktemp -d "${TMPDIR:-/tmp}/vswbench.XXXXXX") || exit 2
SWPID=""
cleanup() {
    if [ -n "$SWPID" ] && kill -0 "$SWPID" 2>/dev/null; then kill -9 "$SWPID" 2>/dev/null; wait "$SWPID" 2>/dev/null; fi
    rm -rf "$TMPD"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

{
    echo "date: $(date -u '+%Y-%m-%d %H:%M UTC')"
    echo "cpu: $(lscpu | sed -n 's/^Model name: *//p')"
    echo "cores: $(nproc) logical CPUs ($(lscpu | sed -n 's/^Thread(s) per core: *//p') thread(s) per core)"
    echo "kernel: $(uname -sr)"
    echo "compiler: $(gcc --version | head -1) (-O0 -Wall -Wextra -Werror, like the whole project)"
    echo "memory: $(free -m | awk '/^Mem:/ {print $2}') MB"
    echo "parameters: ${SECS} s per point, ${REPS} runs per point (median), payload ${PAYLOAD} bytes, seed ${SEED}, threads: ${THREADS}, hosts: ${HOSTS}"
} > "$OUT/machine.txt"

echo "variant,threads,rep,ops_per_sec" > "$OUT/raw_mac.csv"
echo "variant,hosts,rep,frames_per_sec,sent,received" > "$OUT/raw_net.csv"

free_port() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }

# ---- the MAC table alone ----
for t in $THREADS; do
    for rep in $(seq 1 "$REPS"); do
        for v in $VARIANTS; do
            line=$(timeout $((SECS + 60)) "bench/bench_mac_$v" "$t" "$SECS" "$SEED") || { echo "run.sh: bench_mac_$v $t failed" >&2; exit 1; }
            ops=$(sed -n 's/.*ops_per_sec=\([0-9]*\).*/\1/p' <<<"$line")
            echo "$v,$t,$rep,$ops" >> "$OUT/raw_mac.csv"
            echo "mac  $v threads=$t run=$rep: $ops ops/s"
        done
    done
done

# ---- the whole switch ----
run_switch_point() {       # run_switch_point VARIANT HOSTS REP
    local v=$1 h=$2 rep=$3 port log line
    port=$(free_port); log=$TMPD/sw.$v.$h.$rep.log
    VSWITCH_LOG_FILE=$log "./switch_$v" "$port" 16 >/dev/null 2>"$TMPD/sw.err" &
    SWPID=$!
    local end=$((SECONDS + 30))
    until grep -q "listening on TCP port $port" "$log" 2>/dev/null; do
        [ "$SECONDS" -ge "$end" ] && { echo "run.sh: switch_$v did not start" >&2; return 1; }
        kill -0 "$SWPID" 2>/dev/null || { echo "run.sh: switch_$v died at start" >&2; return 1; }
        timeout 1 sleep 0.05
    done
    line=$(timeout $((SECS + 90)) bench/bench 127.0.0.1 "$port" "$h" "$SECS" "$PAYLOAD" "$SEED") || { echo "run.sh: bench failed ($v, $h hosts)" >&2; return 1; }
    kill -INT "$SWPID"; wait "$SWPID"; local rc=$?; SWPID=""
    [ "$rc" -eq 0 ] || { echo "run.sh: switch_$v exited with status $rc" >&2; return 1; }
    local fps sent rcv
    fps=$(sed -n 's/.*frames_per_sec=\([0-9]*\).*/\1/p' <<<"$line"); sent=$(sed -n 's/.* sent=\([0-9]*\).*/\1/p' <<<"$line"); rcv=$(sed -n 's/.* received=\([0-9]*\).*/\1/p' <<<"$line")
    echo "$v,$h,$rep,$fps,$sent,$rcv" >> "$OUT/raw_net.csv"
    echo "net  $v hosts=$h run=$rep: $fps frames/s (sent $sent, received $rcv)"
}
for h in $HOSTS; do
    for rep in $(seq 1 "$REPS"); do
        for v in $VARIANTS; do
            run_switch_point "$v" "$h" "$rep" || exit 1
        done
    done
done

python3 bench/plot.py "$OUT" || exit 1
echo "done: $OUT/results.csv $OUT/throughput.png $OUT/report.pdf"
