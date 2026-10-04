#!/usr/bin/env bash
# run_mp2_ab.sh -- MP2 A/B: the shared engine (dts_top, codec 2) against mp2_decode, pair
# for pair, on every stream of the gate set ($MP2_TEST_DIR, default ~/mp2-streams/gate),
# $MP2_AB_FRAMES frames each (default 12). bench/dvd/mp2_ab_tb.sv; docs/mp2_engine.md M2.
# No model in between: this is the claim "the engine is mp2_decode", scored directly.
#
# --red: two mutations of the engine that must fail [pcm] here (the A/B can fail):
#   A1 the matrix rounds instead of flooring;  A2 MDQ drops the d16 term
set -u
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
SET=${MP2_TEST_DIR:-$HOME/mp2-streams/gate}
GEN=.sim/mp2/ab
FRAMES=${MP2_AB_FRAMES:-12}
JOBS=${JOBS:-$(nproc)}
mkdir -p "$GEN"
fail=0
red=0; [ "${1:-}" = "--red" ] && red=1
SRC="dvd/mp2/mp2_decode.sv dvd/ac3/bit_fifo.sv dvd/ac3/bit_reader.sv dvd/dts/dts_seq.sv dvd/dts/dts_vec.sv dvd/dts/dts_top.sv bench/dvd/mp2_ab_tb.sv"

mapfile -t STREAMS < <(find "$SET" -name '*.mp2' | sort)
[ ${#STREAMS[@]} -gt 0 ] || { echo "FAIL: no streams under $SET"; exit 1; }

# shellcheck disable=SC2086
iverilog -g2012 -I dvd/ac3 -o .sim/mp2/ab_sim $SRC 2>&1 | grep -v sorry | grep . && { echo "FAIL build"; exit 1; }

echo "== GREEN: ${#STREAMS[@]} streams, $FRAMES frames each =="
n=0
for s in "${STREAMS[@]}"; do
  name=$(basename "$(dirname "$s")")_$(basename "$s" .mp2)
  (python3 tools/mp2_golden.py "$s" --out "$GEN/$name" --frames "$FRAMES" > "$GEN/$name.glog" 2>&1 \
     && vvp -n .sim/mp2/ab_sim +stem="$GEN/$name" > "$GEN/$name.log" 2>&1) &
  n=$((n + 1)); [ $((n % JOBS)) -eq 0 ] && wait
done
wait
np=0
for s in "${STREAMS[@]}"; do
  name=$(basename "$(dirname "$s")")_$(basename "$s" .mp2)
  if grep -q "^PASS: mp2_ab_tb" "$GEN/$name.log" 2>/dev/null; then np=$((np + 1))
  else
    echo "  FAIL $name: $(grep -m1 FAIL "$GEN/$name.log" 2>/dev/null || tail -1 "$GEN/$name.glog")"; fail=1
  fi
done
echo "  $np of ${#STREAMS[@]} streams: every pair of the engine is mp2_decode's"

if [ $red = 1 ]; then
  echo "== MUTATIONS (each must fail [pcm]) =="
  arm=synth_tw_44100_s_128k
  mut() {   # name old new
    local d; d=$(mktemp -d)
    mkdir -p "$d/dvd/dts" "$d/dvd/mp2" "$d/dvd/ac3" "$d/bench/dvd"
    cp dvd/dts/*.sv dvd/dts/*.svh dvd/dts/*.mem "$d/dvd/dts/"
    cp dvd/mp2/*.sv "$d/dvd/mp2/"; cp dvd/ac3/*.sv dvd/ac3/*.svh "$d/dvd/ac3/" 2>/dev/null
    cp dvd/mp2/*.mem "$d/dvd/mp2/" 2>/dev/null
    cp bench/dvd/mp2_ab_tb.sv "$d/bench/dvd/"
    python3 - "$d/dvd/dts/dts_vec.sv" "$2" "$3" <<'PYEOF'
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
assert s.count(old) == 1, f"anchor {old!r}"
open(p, "w").write(s.replace(old, new))
PYEOF
    # shellcheck disable=SC2086
    if ! (cd "$d" && iverilog -g2012 -I dvd/ac3 -o sim $SRC 2>build.log); then
      echo "  $1: harness failure"; fail=1; rm -rf "$d"; return
    fi
    (cd "$d" && timeout 900 vvp -n sim +stem="$ROOT/$GEN/$arm" > log 2>&1)
    if grep -q "FAIL \[pcm\]" "$d/log"; then echo "  $1: caught by $(grep -m1 'FAIL \[pcm\]' "$d/log" | cut -c1-90)"
    else echo "  $1: NOT caught as [pcm]: $(grep -m1 'FAIL\|PASS' "$d/log")"; fail=1; fi
    rm -rf "$d"
  }
  mut A1 "rsh = 6'd14; trunc = 1'b1;" "rsh = 6'd14; trunc = 1'b0;" &
  mut A2 "ma = {11'd0, a2}; mb = {10'd0, ic_q[16:0]};" "ma = 27'sd0; mb = {10'd0, ic_q[16:0]};" &
  wait
fi

[ $fail -eq 0 ] && echo "== ALL GREEN ==" || echo "== FAILURES =="
exit $fail
