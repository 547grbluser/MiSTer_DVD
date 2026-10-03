#!/usr/bin/env bash
# gen_mp2_streams.sh -- synthetic MP2 streams for the engine's gate set (docs/mp2_engine.md
# M0): every sample rate, channel mode and allocation table, with and without CRC, from
# twolame (a real Layer II encoder; ffmpeg's cannot code joint or dual). The real disc
# material (tools/mp2_scan.py) covers 44.1 kHz VCD and 48 kHz DVD stereo/joint only.
# Output: $MP2_TEST_DIR/synth (default ~/mp2-streams/gate/synth), local, never committed.
#   per rate (32 / 44.1 / 48 kHz) x mode (stereo, joint, dual, mono):
#     a low rate  (32k or 48k per channel: tables B.2c / B.2d),
#     a mid rate  (64k per channel: B.2a),
#     a high rate (128k+ per channel: B.2a at 48, B.2b at 44.1 / 32)
#   plus CRC-protected variants of the mid rates.
set -euo pipefail
OUT=${MP2_TEST_DIR:-$HOME/mp2-streams/gate}/synth
mkdir -p "$OUT"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
for sr in 32000 44100 48000; do
  # 2 s: left a chirp + noise, right a different chirp + tones (the channels differ, so
  # joint / dual / stereo really code two signals; transients exercise scalefactors)
  ffmpeg -v error -y -f lavfi -i "aevalsrc=0.4*sin(2*PI*(200+3000*t)*t)+0.1*(random(0)-0.5)|0.3*sin(2*PI*(5000-2000*t)*t)+0.2*sin(2*PI*440*t)*gt(mod(t\,0.5)\,0.25):s=$sr:d=2" \
    -f s16le -ac 2 "$TMP/s$sr.raw"
  ffmpeg -v error -y -f s16le -ar $sr -ac 2 -i "$TMP/s$sr.raw" -ac 1 -f s16le "$TMP/m$sr.raw"
  for mode in s j d m; do
    ch=2; [ $mode = m ] && ch=1
    in="$TMP/s$sr.raw"; [ $mode = m ] && in="$TMP/m$sr.raw"
    for per in 32 48 64 128 192; do
      br=$((per * ch)); [ $br -gt 384 ] && continue
      tag="tw_${sr}_${mode}_${br}k"
      twolame -r -s $sr -N $ch -m $mode -b $br "$in" "$OUT/$tag.mp2" > /dev/null 2>&1 || echo "  (twolame refused $tag)"
      if [ $per = 64 ]; then
        twolame -r -s $sr -N $ch -m $mode -b $br -p "$in" "$OUT/${tag}_crc.mp2" > /dev/null 2>&1 || true
      fi
    done
  done
done
ls "$OUT" | wc -l
