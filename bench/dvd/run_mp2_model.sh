#!/usr/bin/env bash
# run_mp2_model.sh -- docs/mp2_engine.md M0: is tools/mp2_ref.py the contract for EVERY
# stream the engine will be scored on, not only run_mp2.sh's five ffmpeg fixtures?
#
# For every *.mp2 under $MP2_TEST_DIR (default ~/mp2-streams/gate; local, never
# committed -- built by tools/mp2_scan.py --extract and tools/gen_mp2_streams.sh), it
# writes a mp2_ref.py fixture of $MP2_FRAMES frames (default 24) and runs mp2_decode_tb
# on it: the RTL must be bit-exact with the model on every one. The engine (M1/M2) is then
# scored against the same model on the same set, and A/B against mp2_decode.
# Parallel over $JOBS (default nproc). Exit 0 only when every stream passes.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

SET="${MP2_TEST_DIR:-$HOME/mp2-streams/gate}"
FRAMES="${MP2_FRAMES:-24}"
JOBS="${JOBS:-$(nproc)}"
WORK="$ROOT/bench/dvd/test_mp2/model"
mkdir -p "$WORK"

mapfile -t STREAMS < <(find "$SET" -name '*.mp2' | sort)
[ "${#STREAMS[@]}" -gt 0 ] || { echo "FAIL: no *.mp2 under $SET"; exit 1; }

iverilog -g2012 -I dvd/ac3 -o bench/dvd/mp2_decode_sim \
    dvd/mp2/mp2_decode.sv dvd/ac3/bit_fifo.sv dvd/ac3/bit_reader.sv \
    bench/dvd/mp2_decode_tb.sv

one () {   # stream -> "PASS name" / "FAIL name: why"
    local f="$1" name d log
    name="$(basename "$(dirname "$f")")_$(basename "$f" .mp2)"
    d="$WORK/$name"; log="$d.log"
    if ! python3 tools/mp2_ref.py fixture "$f" "$d" --frames "$FRAMES" >"$log" 2>&1; then
        echo "FAIL $name: the model refused it ($(tail -1 "$log"))"; return
    fi
    if vvp bench/dvd/mp2_decode_sim +FIXDIR="$d" >>"$log" 2>&1 && grep -q '^PASS: mp2_decode_tb' "$log"; then
        echo "PASS $name: $(grep -o '[0-9]* samples' "$log" | tail -1)"
    else
        echo "FAIL $name: $(grep -m1 -E 'MISMATCH|FAIL|TIMEOUT|fixture' "$log" || tail -1 "$log")"
    fi
}
export -f one
export WORK FRAMES

OUT="$(printf '%s\n' "${STREAMS[@]}" | xargs -P "$JOBS" -I{} bash -c 'one "$1"' _ {})"
echo "$OUT" | sort
np=$(grep -c '^PASS' <<<"$OUT" || true)
nf=$(grep -c '^FAIL' <<<"$OUT" || true)
echo "== $np of ${#STREAMS[@]} streams bit-exact (RTL vs mp2_ref.py, $FRAMES frames each)"
if [ "$nf" -eq 0 ] && [ "$np" -eq "${#STREAMS[@]}" ]; then
    echo "PASS: run_mp2_model"
else
    echo "FAIL: run_mp2_model ($nf failed)"; exit 1
fi
