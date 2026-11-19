# lib.sh - helpers for the Part 8 integration scripts. Sourced by them, never run directly.
#
#   VARIANT=normal (default) | valgrind | tsan
#
# Every external command runs under `timeout $TMO` (default 30 s) and every wait loop gives up
# after $TMO seconds, so a hang fails the test instead of blocking the suite. If the switch does
# not exit after SIGINT, a gdb backtrace of all its threads is printed (gdb -p PID,
# thread apply all bt) and the process is killed.

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT" || exit 1

VARIANT=${VARIANT:-normal}
TMO=${TMO:-30}
SCRIPT=$(basename "${0:-script}" .sh)

T() { timeout "$TMO" "$@"; }

# ---- which switch binary, with which tool in front of it ----
SW_PRE=()
case "$VARIANT" in
normal)   SW_BIN=./switch;       SMALL_BIN=./switch_small ;;
valgrind) SW_BIN=./switch;       SMALL_BIN=./switch_small
          SW_PRE=(valgrind --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=all
                  --error-exitcode=1 --track-fds=yes) ;;
tsan)     SW_BIN=./switch_tsan;  SMALL_BIN=./switch_small_tsan
          # TSan binaries need address-space randomisation off on some kernels.
          if setarch -R true 2>/dev/null; then SW_PRE=(setarch -R); fi ;;
*)        echo "$SCRIPT: unknown VARIANT '$VARIANT' (normal, valgrind or tsan)" >&2; exit 2 ;;
esac
HOST_BIN=./host
for b in "$SW_BIN" "$SMALL_BIN" "$HOST_BIN"; do
    [ -x "$b" ] || { echo "$SCRIPT: $b is missing: run 'make all switch_small tsan' first" >&2; exit 2; }
done
if [ "$VARIANT" = valgrind ]; then
    command -v valgrind >/dev/null || { echo "$SCRIPT: valgrind is not installed" >&2; exit 2; }
fi

TMPD=$(mktemp -d "${TMPDIR:-/tmp}/vswitch.$SCRIPT.XXXXXX") || exit 2
SWPID=""
SW_ARGS=()                            # extra arguments for the switch, e.g. SW_ARGS=(-c file) before sw_start (Part 16)
SWLOG=""
SWERR=""
PORT=0
declare -A HPID HFD HRC RFD
PASS=0
FAIL=0

# ---- cleanup: runs on every exit, also after a failed assertion, a signal or a timeout ----
cleanup() {
    local n p fd
    for n in "${!HPID[@]}"; do
        p=${HPID[$n]}
        kill -9 "$p" 2>/dev/null
        wait "$p" 2>/dev/null
    done
    for n in "${!HFD[@]}"; do fd=${HFD[$n]}; exec {fd}>&-; done 2>/dev/null
    for n in "${!RFD[@]}"; do fd=${RFD[$n]}; exec {fd}>&- 2>/dev/null; done
    if [ -n "$SWPID" ] && kill -0 "$SWPID" 2>/dev/null; then
        kill -9 "$SWPID" 2>/dev/null
        wait "$SWPID" 2>/dev/null
    fi
    if [ -n "${KEEP:-}" ]; then echo "$SCRIPT: files kept in $TMPD"; else rm -rf "$TMPD"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---- assertions ----
ok()  { PASS=$((PASS + 1)); echo "  ok:   $*"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $*"; }
# check "what" command args...   (the command is the condition)
check() {
    local what=$1
    shift
    if "$@"; then ok "$what"; else bad "$what"; fi
}

finish() {
    echo "$SCRIPT [$VARIANT]: $PASS checks passed, $FAIL failed"
    if [ "$FAIL" -ne 0 ]; then
        echo "---- switch log (last 40 lines) ----"
        T tail -n 40 "$SWLOG" 2>/dev/null
        echo "------------------------------------"
        exit 1
    fi
    exit 0
}

# ---- polling (always bounded by $TMO) ----
nap() { T sleep "${1:-0.02}"; }

cnt() { # cnt FILE TEXT : number of lines of FILE containing TEXT (fixed string)
    local c
    c=$(T grep -c -F -- "$2" "$1" 2>/dev/null) || true
    echo "${c:-0}"
}

wait_file() { # wait_file FILE TEXT [COUNT] : until FILE has at least COUNT matching lines
    local f=$1 pat=$2 n=${3:-1} end=$((SECONDS + TMO))
    while [ "$(cnt "$f" "$pat")" -lt "$n" ]; do
        [ "$SECONDS" -ge "$end" ] && return 1
        nap
    done
    return 0
}

wait_gone() { # wait_gone PID : until the process has exited
    local pid=$1 end=$((SECONDS + TMO))
    while kill -0 "$pid" 2>/dev/null; do
        [ "$SECONDS" -ge "$end" ] && return 1
        nap
    done
    return 0
}

mac() { printf '02:00:00:00:00:%02x' "$1"; }

# ---- the switch ----
sw_start() { # sw_start NPORTS [small]  (small = K=4 entries, aging 3 s, at most 3 MACs per port)
    local n=$1 bin=$SW_BIN
    [ "${2:-}" = small ] && bin=$SMALL_BIN
    PORT=$(T python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
    SWLOG=$TMPD/switch.$PORT.log          # the switch's log file (the logger thread writes it)
    SWERR=$TMPD/switch.$PORT.err          # its stderr: valgrind / ThreadSanitizer reports
    VSWITCH_LOG_FILE="$SWLOG" "${SW_PRE[@]}" "$bin" "$PORT" "$n" ${SW_ARGS[@]+"${SW_ARGS[@]}"} 2>"$SWERR" &
    SWPID=$!
    if ! wait_file "$SWLOG" "listening on TCP port $PORT"; then
        bad "the switch did not start"
        finish
    fi
}

connected() { cnt "$SWLOG" "host connected"; }
torn_down() { cnt "$SWLOG" "teardown complete"; }

# Stop the switch with SIGINT and check how it ended.
sw_stop() {
    local rc
    kill -INT "$SWPID" 2>/dev/null
    if ! wait_gone "$SWPID"; then
        bad "the switch did not exit within ${TMO}s after SIGINT: backtrace of all threads follows"
        local t
        if command -v gdb >/dev/null && ! T gdb -p "$SWPID" -batch -ex "thread apply all bt" 2>&1 | head -n 120 | grep -q "^#0"; then
            # gdb could not attach (kernel.yama.ptrace_scope=1 allows only a parent to attach; try
            # "sudo sysctl kernel.yama.ptrace_scope=0" or run as root). Show where each thread sleeps instead.
            echo "  (gdb -p $SWPID could not attach; threads and what they wait for:)"
            for t in /proc/"$SWPID"/task/*; do
                echo "    $(cat "$t/comm" 2>/dev/null)  wchan=$(cat "$t/wchan" 2>/dev/null)  syscall=$(cut -d' ' -f1 "$t/syscall" 2>/dev/null)"
            done
        fi
        kill -9 "$SWPID" 2>/dev/null
        wait "$SWPID" 2>/dev/null
        SWPID=""
        return 1
    fi
    wait "$SWPID"
    rc=$?
    SWPID=""
    check "the switch exited with status 0 after SIGINT (got $rc)" test "$rc" -eq 0
    check "the log ends with 'shutdown: complete'" test "$(cnt "$SWLOG" 'shutdown: complete')" -eq 1
    check "no [ERR] line in the switch log" test "$(cnt "$SWLOG" '[ERR]')" -eq 0
    check "nothing on the switch's stderr (the log goes to the log file)" test "$(cnt "$SWERR" '[INFO]')$(cnt "$SWERR" '[ERR]')" = 00
    case "$VARIANT" in
    valgrind)
        check "valgrind: 0 errors" test "$(cnt "$SWERR" 'ERROR SUMMARY: 0 errors from 0 contexts')" -eq 1
        check "valgrind: 0 bytes in use at exit" test "$(cnt "$SWERR" 'in use at exit: 0 bytes in 0 blocks')" -eq 1
        check "valgrind: nothing lost" test "$(T grep -cE '(definitely|indirectly|possibly) lost: [1-9]' "$SWERR")" -eq 0
        check "valgrind: no descriptor left open (besides 0, 1, 2)" \
              test "$(T grep -cE 'Open (AF_INET socket|file descriptor) ([3-9]|[1-9][0-9])' "$SWERR")" -eq 0
        ;;
    tsan)
        check "ThreadSanitizer: no report" test "$(cnt "$SWERR" 'ThreadSanitizer')" -eq 0
        ;;
    esac
}

# ---- hosts: each is a real ./host with its stdin on a FIFO that we keep open for writing ----
host_spawn() { # host_spawn NAME MAC [options]   (does not wait for a port)
    local name=$1 m=$2 fd
    shift 2
    mkfifo "$TMPD/$name.in" || { bad "cannot create the FIFO of host $name (a host name must be used once)"; return 1; }
    exec {fd}<>"$TMPD/$name.in"          # a writer of our own: the host sees EOF only when we close it
    HFD[$name]=$fd
    "$HOST_BIN" 127.0.0.1 "$PORT" "$m" "$@" <"$TMPD/$name.in" >"$TMPD/$name.out" 2>"$TMPD/$name.err" &
    HPID[$name]=$!
}

host_start() { # host_start NAME MAC [options] : spawn and wait until the switch gave it a port
    local before
    before=$(connected)
    host_spawn "$@"
    if ! wait_file "$SWLOG" "host connected" $((before + 1)); then
        bad "host $1 did not get a port"
        return 1
    fi
}

say() { printf '%s\n' "$2" >&"${HFD[$1]}"; }   # say NAME "send bcast hello"

out() { echo "$TMPD/$1.out"; }
out_count() { cnt "$(out "$1")" "$2"; }       # out_count NAME TEXT
wait_out() { wait_file "$(out "$1")" "$2" "${3:-1}"; }   # wait_out NAME TEXT [COUNT]

# NAME leaves the way a host does: "quit", then wait for the process and record its exit status.
host_reap() { # host_reap NAME : wait for the host to end by itself, record HRC[NAME]
    local name=$1 pid=${HPID[$1]} fd=${HFD[$1]}
    if ! wait_gone "$pid"; then
        bad "host $name did not exit"
        kill -9 "$pid" 2>/dev/null
    fi
    wait "$pid" 2>/dev/null
    HRC[$name]=$?
    unset 'HPID[$name]'
    if [ -n "$fd" ]; then exec {fd}>&-; unset 'HFD[$name]'; fi
}

host_quit() { say "$1" quit; host_reap "$1"; }

# After the switch has stopped every connected host sees EOF and ends by itself.
hosts_reap() {
    local n
    for n in "${!HPID[@]}"; do host_reap "$n"; done
}

# A barrier: FROM broadcasts TAG; when every listed host has printed it, everything FROM sent
# earlier has been forwarded (one receiver thread per port, one FIFO queue per port).
barrier() { # barrier FROM TAG HOST...
    local from=$1 tag=$2 h
    shift 2
    say "$from" "send bcast $tag"
    for h in "$@"; do
        wait_out "$h" "data=$tag" || { bad "host $h never printed barrier $tag"; return 1; }
    done
}

# ---- raw TCP connections, to send frames a normal host never would ----
raw_open() { # raw_open NAME
    local fd
    exec {fd}<>"/dev/tcp/127.0.0.1/$PORT"
    RFD[$1]=$fd
}

byte() { printf "\\x$(printf '%02x' "$1")"; }        # one raw byte, value 0-255
mac_bytes() { local b; for b in ${1//:/ }; do printf "\\x$b"; done; }

# raw_frame_bytes SRC DST VLAN PRIO TEXT : the wire bytes of one frame, on stdout
raw_frame_bytes() {
    local len=${#5}
    byte $((len >> 8)); byte $((len & 255))
    mac_bytes "$1"
    mac_bytes "$2"
    byte $(($3 >> 8)); byte $(($3 & 255))
    byte "$4"
    printf '%s' "$5"
}

raw_send() { # raw_send NAME SRC DST VLAN PRIO TEXT
    local fd=${RFD[$1]}
    shift
    raw_frame_bytes "$@" >&"$fd"
}

raw_close() { local fd=${RFD[$1]}; exec {fd}>&-; unset 'RFD[$1]'; }
