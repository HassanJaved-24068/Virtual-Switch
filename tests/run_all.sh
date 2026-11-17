#!/bin/bash
# run_all.sh - the Tier 1 checkpoint (Part 8).
#
#   ./tests/run_all.sh                     normal variant: Parts 1-7 suite + the ten integration scripts
#   VARIANT=valgrind ./tests/run_all.sh    the ten scripts with the switch under valgrind
#   VARIANT=tsan ./tests/run_all.sh        the ten scripts with the ThreadSanitizer build of the switch
#   ./tests/run_all.sh forward mac_move    only the named scripts (no Parts 1-7 suite)
#
# Variables: VARIANT (normal|valgrind|tsan), TMO (seconds for each command and wait inside a
# script, default 30), SCRIPT_TIMEOUT (seconds for one whole script, default 300; a script
# can legitimately take longer than one command: port_full and many_hosts wait for the 5 s
# port timeout), LEGACY=0 (skip the Parts 1-7 suite in the normal variant).
# Exit status: 0 only if everything passed. The failed scripts are listed at the end.
cd "$(dirname "$0")/.." || exit 1

VARIANT=${VARIANT:-normal}
SCRIPT_TIMEOUT=${SCRIPT_TIMEOUT:-300}
export VARIANT

ALL=(forward broadcast slow_host partial midframe_disconnect port_full mac_aging mac_move port_security many_hosts)
if [ $# -gt 0 ]; then SCRIPTS=("$@"); else SCRIPTS=("${ALL[@]}"); fi

case "$VARIANT" in
normal)   BUILD=(all switch_small) ;;
valgrind) BUILD=(all switch_small) ;;
tsan)     BUILD=(all switch_small switch_tsan switch_small_tsan) ;;
*) echo "run_all: unknown VARIANT '$VARIANT' (normal, valgrind or tsan)" >&2; exit 2 ;;
esac
timeout 600 make -s "${BUILD[@]}" || { echo "run_all: build failed" >&2; exit 1; }

failed=()
passed=()

if [ "$VARIANT" = normal ] && [ "${LEGACY:-1}" != 0 ] && [ $# -eq 0 ]; then
    echo "################ Parts 1-7 unit and regression tests ################"
    if timeout 3000 ./tests/run_unit.sh; then passed+=(parts-1-7); else failed+=(parts-1-7); fi
fi

echo "################ Part 8 integration scripts, VARIANT=$VARIANT ################"
for s in "${SCRIPTS[@]}"; do
    echo "=== $s ($VARIANT) ==="
    start=$SECONDS
    timeout "$SCRIPT_TIMEOUT" bash "tests/$s.sh"
    rc=$?
    took=$((SECONDS - start))
    if [ "$rc" -eq 0 ]; then
        passed+=("$s")
    else
        [ "$rc" -eq 124 ] && echo "run_all: $s timed out after ${SCRIPT_TIMEOUT}s (killed)"
        echo "run_all: $s FAILED (exit status $rc, $took s)"
        failed+=("$s")
    fi
done

echo
echo "================ summary: VARIANT=$VARIANT ================"
echo "passed: ${#passed[@]}  (${passed[*]})"
if [ ${#failed[@]} -ne 0 ]; then
    echo "FAILED: ${#failed[@]}  (${failed[*]})"
    exit 1
fi
echo "All tests passed."
if [ "$VARIANT" = normal ]; then
    echo "note: there are no official public tests; forward.sh and broadcast.sh are our own tests (docs/Part8_Explanation.txt)"
fi
exit 0
