#!/usr/bin/env bash
# run_dts_dec.sh -- DTS through dvd_audio_decode (the T_DTS arm, docs/dts_decoder.md P3):
# bench/dvd/dts_dec_tb.sv, goldens tools/dts_golden.py (bit-exact against tools/dts_fixed.py),
# the engine's codebook port answered from the tables after a latency.
# GREEN:
#   T2   the T2 sample, 3 frames (codebook latency 20)
#   L150 the same at a codebook latency of 150 cycles
#   SH   Shadoan (lenient block codes, D5)
#   R1   T2 with frame 1 refused mid-frame (its partial PCM, then the next frame)
#   SW   3 AC-3 frames (tone 5.1) then T2: the engine changes program mid-stream
#   OFF  dts_tables_ok low: DTS is discarded, every byte consumed, not a pair
# --red: mutations on a COPY of the RTL, each caught by its own arm.
set -u
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
GATE=${DTS_GATE_DIR:-$HOME/dts-streams/gate}
GEN=.sim/dts_dec
mkdir -p "$GEN"
fail=0
red=0; [ "${1:-}" = "--red" ] && red=1
T2=$GATE/disc_t2_sample.dts
SHA=$GATE/disc_blockoverflow_shadoan.dts
[ -f "$T2" ] || { echo "FAIL: no $T2 (DTS_GATE_DIR)"; exit 1; }
python3 tools/dts_isa.py --asm --check | sed 's/^/  /' || fail=1
python3 tools/dts_golden.py --codebooks "$GEN/cb" > /dev/null
python3 tools/dts_golden.py "$T2" --out "$GEN/t2" --frames 3 > "$GEN/t2.glog" || { cat "$GEN/t2.glog"; exit 1; }
python3 tools/dts_golden.py "$T2" --out "$GEN/r1" --frames 3 --refuse 1 > "$GEN/r1.glog" || { cat "$GEN/r1.glog"; exit 1; }
python3 tools/dts_golden.py "$SHA" --out "$GEN/sh" --frames 3 > "$GEN/sh.glog" || { cat "$GEN/sh.glog"; exit 1; }
python3 tools/ac3_golden.py tools/streams/tone_5p1_48k_192k.ac3 --out "$GEN/ac3" --frames 3 > "$GEN/ac3.glog" || { cat "$GEN/ac3.glog"; exit 1; }
SRC="dvd/ac3/*.sv dvd/dts/dts_seq.sv dvd/dts/dts_vec.sv dvd/dts/dts_top.sv dvd/audio_engine.sv
     dvd/lpcm_unpack.sv dvd/mp2/mp2_decode.sv dvd/dvd_audio_decode.sv"
TB=bench/dvd/dts_dec_tb.sv
ARMS=("T2|+stem=$ROOT/$GEN/t2" "L150|+stem=$ROOT/$GEN/t2 +cblat=150" "SH|+stem=$ROOT/$GEN/sh"
      "R1|+stem=$ROOT/$GEN/r1" "SW|+stem=$ROOT/$GEN/t2 +ac3=$ROOT/$GEN/ac3" "OFF|+stem=$ROOT/$GEN/t2 +notables")
# shellcheck disable=SC2086
iverilog -g2012 -I dvd/ac3 -o "$GEN/sim" $SRC $TB 2>&1 | grep -v sorry | grep . && { echo "FAIL build"; exit 1; }
echo "== GREEN =="
for a in "${ARMS[@]}"; do
  IFS='|' read -r name args <<<"$a"
  # shellcheck disable=SC2086
  (vvp -n "$GEN/sim" +cb="$ROOT/$GEN/cb" $args 2>&1 | grep -v 'readmem\|Not enough' > "$GEN/$name.log") &
done
wait
for a in "${ARMS[@]}"; do
  IFS='|' read -r name _ <<<"$a"
  if grep -q "^PASS: dts_dec_tb" "$GEN/$name.log"; then
    echo "  PASS $name: $(grep '^dts_dec_tb:' "$GEN/$name.log" | sed 's/^dts_dec_tb: //;s/  *$//')"
  else echo "  FAIL $name: $(grep -m1 FAIL "$GEN/$name.log")"; fail=1; fi
done

if [ $red = 1 ]; then
  echo "== MUTATIONS (each must be caught by its own arm) =="
  RES=$(mktemp -d)
  mut() {   # name arm-args file old new pattern
    local name=$1 args=$2 file=$3 old=$4 new=$5 pat=$6 d; d=$(mktemp -d)
    mkdir -p "$d/dvd/dts" "$d/dvd/ac3" "$d/dvd/mp2" "$d/bench/dvd"
    cp dvd/*.sv "$d/dvd/"; cp dvd/dts/*.sv dvd/dts/*.svh dvd/dts/*.mem "$d/dvd/dts/"
    cp dvd/ac3/*.sv dvd/ac3/*.svh "$d/dvd/ac3/"; cp dvd/mp2/*.sv "$d/dvd/mp2/"; cp $TB "$d/bench/dvd/"
    python3 - "$d/$file" "$old" "$new" <<'PYEOF' || { echo "  $name: anchor not found" > "$RES/$name"; echo MUTFAIL >> "$RES/$name"; return; }
import sys
p, o, n = sys.argv[1:4]
s = open(p).read()
assert s.count(o) == 1, o
open(p, 'w').write(s.replace(o, n))
PYEOF
    # shellcheck disable=SC2086
    (cd "$d" && iverilog -g2012 -I dvd/ac3 -o sim $SRC $TB 2>/dev/null) || { echo "  $name: build failed" > "$RES/$name"; echo MUTFAIL >> "$RES/$name"; return; }
    # shellcheck disable=SC2086
    (cd "$d" && timeout 1800 vvp -n sim +cb="$ROOT/$GEN/cb" $args 2>&1 | grep -v 'readmem\|Not enough' > log)
    if grep -q "^PASS: dts_dec_tb" "$d/log"; then { echo "  $name: SURVIVED"; echo MUTFAIL; } > "$RES/$name"
    elif grep -qE "FAIL $pat" "$d/log"; then echo "  $name: caught by $(grep -m1 FAIL "$d/log" | sed 's/.*FAIL/FAIL/' | cut -c1-100)" > "$RES/$name"
    else { echo "  $name: failed outside $pat: $(grep -m1 FAIL "$d/log")"; echo MUTFAIL; } > "$RES/$name"; fi
    rm -rf "$d"
  }
  D=dvd/dvd_audio_decode.sv; E=dvd/audio_engine.sv
  mut D1 "+stem=$ROOT/$GEN/t2" $D ".quant    ((cdda_mode || dts_active) ? 2'd0 : lpcm_quant)," ".quant    (cdda_mode ? 2'd0 : lpcm_quant)," "\[(pcm|count|hang)\]" &
  mut D2 "+stem=$ROOT/$GEN/t2" $D "(ser_k == 2'd0) ? ser_pair[31:24] : (ser_k == 2'd1) ? ser_pair[23:16]" "(ser_k == 2'd0) ? ser_pair[23:16] : (ser_k == 2'd1) ? ser_pair[31:24]" "\[pcm\]" &
  mut D3 "+stem=$ROOT/$GEN/t2 +notables" $D "wire         dts_ok_frame = (cur_type == T_DTS) && dts_tables_ok;" "wire         dts_ok_frame = (cur_type == T_DTS);" "\[off\]" &
  wait
  # Two EQUIVALENT mutants, recorded, not arms (the guards stay, as defence):
  # - dropping the descriptor gate during a change of program (e_fr_valid = fr_valid):
  #   the switch starts in the cycle the descriptor arrives, the old program's take is
  #   wiped by the reset, and the dispatcher's register (cleared on the GATED fr_ready)
  #   presents it again;
  # - switching without waiting for idle (idle_at_frame = 1): the dispatcher can pop the
  #   DTS descriptor only once every byte of the last AC-3 frame is consumed, and the
  #   engine consumes the last ones (CRC2) in FEND, after block 5's IMDCT has completed
  #   -- which itself waits for pcm_out's drain. So the engine is already idle at FRAME.
  # The [ac3] arm (every AC-3 block decoded across the change) scores the consequence.
  mut D5 "+stem=$ROOT/$GEN/t2" $D "            else if (dts_tables_ok) cur_codec <= T_LPCM;" "" "\[out\]" &
  mut D6 "+stem=$ROOT/$GEN/t2 +ac3=$ROOT/$GEN/ac3" $D "            if (eng_frame) eng_codec_req <= (cur_type == T_AC3);" "" "\[(pcm|count|hang)\]" &
  wait
  for f in "$RES"/*; do grep -v MUTFAIL "$f"; grep -q MUTFAIL "$f" && fail=1; done
  rm -rf "$RES"
fi
[ $fail = 0 ] && echo "== ALL GREEN ==" || echo "== FAILURES =="
exit $fail
