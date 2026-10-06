#!/usr/bin/env bash
# run_imdct_xcheck.sh -- imdct_512 against tools/imdct_model.py, bit for bit, on real
# AC-3 blocks (docs/logic_reclaim.md §10a). The model is the golden an engine IMDCT
# program will be scored against, so it must equal the RTL first.
#   FRAMES=N frames a stream (default 4 = 24 blocks; bbb_mono is silent for 2); streams: the arguments, else a
#   set that covers 2/0, mono, 3/0, 2/1, 3/1, 2/2, 3/2, short blocks and DRC.
#   --red: mutate the model (one product rounded instead of floored) and require FAIL.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$ROOT/.sim/imdct_xcheck"
FRAMES="${FRAMES:-4}"
RED=0
if [[ "${1:-}" == "--red" ]]; then RED=1; shift; fi
if [[ $# -gt 0 ]]; then
    STREAMS=("$@")
else
    STREAMS=("$ROOT"/tools/streams/{sweep_192k,tone_5p1_48k_192k,noise_5p1_48k_640k,acmod3_30_48k_640k,acmod4_21_48k_640k,acmod5_31_48k_640k,acmod6_22_48k_640k,dualmono_440_1k_48k_192k}.ac3
             "$ROOT"/bench/ac3/vectors/{bbb_mono,bbb_short_5p1}.ac3)
    GATE="${AC3_TEST_DIR:-$HOME/ac3-streams/gate}"
    for f in "$GATE"/*dynrnge*.ac3 "$GATE"/*short*.ac3; do [[ -f "$f" ]] && STREAMS+=("$f"); done
fi
mkdir -p "$OUT"
NBLK=$((FRAMES * 6))
iverilog -g2012 -D AC3_COSIM -P imdct_xcheck_tb.NBLK=$NBLK -I "$ROOT/dvd/ac3" -o "$OUT/sim" \
    "$ROOT/dvd/ac3/imdct_512.sv" "$ROOT/bench/ac3/imdct_xcheck_tb.sv"
fail=0
ran=0
caught=0
for s in "${STREAMS[@]}"; do
    d="$OUT/$(basename "$s" .ac3)"
    msg=$(IMDCT_MODEL_RED=$RED python3 "$ROOT/tools/imdct_model.py" vec "$s" --frames "$FRAMES" --out "$d")
    if [[ "$msg" == *" 0 nonzero coefficients"* ]]; then
        echo "SKIP $(basename "$s"): silent for $FRAMES frames (nothing to compare)"; continue
    fi
    n=$(wc -l < "$d/params.mem")
    if [[ $n -lt $NBLK ]]; then echo "SKIP $(basename "$s"): only $n blocks before a refusal"; continue; fi
    ran=$((ran + 1))
    if ( cd "$d" && vvp -n "$OUT/sim" ) > "$d/log" 2>&1 && grep -q '^PASS' "$d/log"; then
        echo "PASS $(basename "$s"): $(grep '^PASS' "$d/log" | sed 's/^PASS: //')"
    else
        echo "FAIL $(basename "$s")"; grep -v sorry "$d/log" | sed -n '1,10p'; fail=1
        caught=$((caught + 1))
    fi
done
if [[ $RED == 1 ]]; then
    # every stream that ran must catch the mutation, not just one
    [[ $caught == "$ran" && $ran -gt 0 ]] && { echo "PASS (red): the mutated model is caught on all $ran streams"; exit 0; }
    echo "FAIL (red): the mutation was caught on $caught of $ran streams"; exit 1
fi
[[ $fail == 0 ]] && echo "PASS: all streams" || { echo "FAIL"; exit 1; }
