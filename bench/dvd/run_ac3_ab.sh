#!/usr/bin/env bash
# run_ac3_ab.sh -- A/B: the AC-3 decoder that ships (dvd/ac3/ac3_front) against the
# engine that replaces it (dvd/audio_engine.sv), bench/dvd/ac3_ab_tb.sv: every block's
# PCM (imdct_512's pcm_mem, what pcm_out drains), lvl_q and acmod, identical, on every
# stream of tools/test_ac3_model.py's set. docs/ac3_engine.md "W1".
#   FRAMES=N frames a stream (default 6); a *refuse* window runs with +refuse (ac3_front
#   halts there; the engine must match it up to the halt and go on).
#   D1: tone 5.1 with a 4,000-cycle drain (the engine runs ahead of a slow pcm_out)
set -u
cd "$(dirname "$0")/../.."
GATE=${AC3_TEST_DIR:-$HOME/ac3-streams/gate}
GEN=.sim/ac3/ab
mkdir -p "$GEN"
FRAMES=${FRAMES:-6}
JOBS=${JOBS:-$(nproc)}
fail=0
shopt -s nullglob globstar
STREAMS=(tools/streams/*.ac3 bench/ac3/vectors/*.ac3 "$GATE"/**/*.ac3)
[ ${#STREAMS[@]} -eq 0 ] && { echo "FAIL: no AC-3 streams"; exit 1; }

python3 - "$GEN" "$FRAMES" "${STREAMS[@]}" <<'PYEOF'
import os, sys
sys.path.insert(0, 'tools')
import ac3_model as M
gen, n = sys.argv[1], int(sys.argv[2])
for p in sys.argv[3:]:
    fr = [f for _, f in M.frames(open(p, 'rb').read())][:n]
    stem = os.path.join(gen, os.path.splitext(os.path.basename(p))[0])
    data = b''.join(fr)
    open(stem + '.bytes', 'w').write(''.join(f'{b:02x}\n' for b in data))
    open(stem + '.frames', 'w').write(''.join(f'{len(f):x}\n' for f in fr))
    open(stem + '.meta', 'w').write(f'{len(data)} {len(fr)}\n')
PYEOF

RTL="dvd/dts/dts_seq.sv dvd/dts/dts_vec.sv dvd/dts/dts_top.sv dvd/audio_engine.sv
     dvd/ac3/ac3_front.sv dvd/ac3/ac3_parse.sv dvd/ac3/bit_fifo.sv dvd/ac3/bit_reader.sv
     dvd/ac3/sync_crc.sv dvd/ac3/bsi_parse.sv dvd/ac3/audblk_parse.sv
     dvd/ac3/exponent_decode.sv dvd/ac3/bit_allocation.sv dvd/ac3/mantissa_dequant.sv
     dvd/ac3/imdct_512.sv"
# shellcheck disable=SC2086
iverilog -g2012 -I dvd/ac3 -o .sim/ac3/ab_sim $RTL bench/dvd/ac3_ab_tb.sv 2>&1 | grep -v sorry | grep . \
  && { echo "FAIL build"; exit 1; }

ARMS=()
for s in "${STREAMS[@]}"; do
  st=$(basename "$s" .ac3)
  case "$s" in *refuse*) ARMS+=("$st|$st|+refuse");; *) ARMS+=("$st|$st|");; esac
done
ARMS+=("D1|tone_5p1_48k_192k|+drain=4000")
n=0
for a in "${ARMS[@]}"; do
  IFS='|' read -r name st args <<<"$a"
  # shellcheck disable=SC2086
  vvp -n .sim/ac3/ab_sim +stem="$GEN/$st" $args 2>&1 | grep -v 'readmem\|Not enough' > "$GEN/$name.log" &
  n=$((n + 1)); [ $((n % JOBS)) -eq 0 ] && wait
done
wait
for a in "${ARMS[@]}"; do
  IFS='|' read -r name _ <<<"$a"
  if grep -q "^PASS: ac3_ab_tb" "$GEN/$name.log"; then
    echo "  PASS $name: $(grep '^ac3_ab_tb:' "$GEN/$name.log" | sed 's/^ac3_ab_tb: //')"
  else echo "  FAIL $name: $(grep -m1 FAIL "$GEN/$name.log")"; fail=1; fi
done
[ $fail = 0 ] && echo "== ALL GREEN ==" || echo "== FAILURES =="
exit $fail
