#!/usr/bin/env bash
#
# run_colour_matrix.sh -- the colour-matrix gate (docs/status_log.md "BT.601 default
# colour matrix"; DVD Demystified 3rd-edition audit, A/B #6).
#
# Two defects, one per module:
#   rtl/mpeg2/yuv2rgb.v  decoded "no colour description" (matrix_coefficients 0) as
#                        BT.709, the ISO 13818-2 default. DVD permits only 5/6, both
#                        BT.601, and the subpicture palette is BT.601 -- so ~60% of DVD
#                        features (tools/colour_scan.py) decoded with the wrong matrix
#                        and their subtitles disagreed with the picture.
#   rtl/mpeg2/vld.v      loaded matrix_coefficients only from a display extension that
#                        carries one, so MPEG-1, colour_description=0 and a sequence
#                        without the extension kept the PREVIOUS sequence's matrix.
#
# The stream is synthetic on purpose: the junctions under test (709 -> untagged,
# 709 -> MPEG-1) need a 709-tagged sequence, and no disc in the library has one.
# tools/colour_matrix_es.py builds it; bench/dvd/colour_matrix_tb.sv scores six arms.
#
# --red runs three mutated copies first. Each must fail EXACTLY its own arms -- a
# mutation that fails more is a bench whose arms are not independent, and one that
# fails fewer is a vacuous arm:
#   shipped-vld   no per-sequence clear, no picture commit (today's main)  -> {2,4}
#   no-commit     clear at the header, output follows it immediately       -> {5}
#   shipped-table yuv2rgb's 3'd0 back in the BT.709 arm (today's main)     -> {1,6}
#
#   ./bench/dvd/run_colour_matrix.sh          # the gate
#   ./bench/dvd/run_colour_matrix.sh --red    # mutations first, then the gate
set -euo pipefail
cd "$(dirname "$0")/../.."

IV="iverilog -g2012 -D__IVERILOG__ -I rtl/mpeg2"
FIX=bench/dvd/test_vobs/colour_matrix
SIM=bench/dvd/colour_matrix_sim
rc=0

mkdir -p bench/dvd/test_vobs
python3 tools/colour_matrix_es.py "$FIX"

build () {  # build <out> <vld.v> <yuv2rgb.v>
  $IV -o "$1" "$2" rtl/mpeg2/getbits.v "$3" dvd/pgc_palette.sv \
      bench/dvd/colour_matrix_tb.sv 2>&1 | grep -viE 'warning|sorry: constant selects' || true
  [ -f "$1" ] || { echo "  FAIL: $1 did not compile"; exit 1; }
}

failed_arms () {  # the sorted, space-separated list of arms the run reported FAIL/VACUOUS
  echo "$1" | grep -E '^ARM [0-9]: (FAIL|VACUOUS)' | awk '{print $2}' | tr -d ':' | sort -n | tr '\n' ' ' | sed 's/ $//'
}

if [ "${1:-}" = "--red" ]; then
  MUT=$(mktemp -d)
  trap 'rm -rf "$MUT"' EXIT

  # shipped-vld: drop the clear, and let the output follow mc_seq every cycle.
  sed -e '/CM: per-sequence clear/d' \
      -e 's/^\(\s*\)else if (.*matrix_coefficients <= mc_seq; \/\/ CM: commit at picture/\1else if (1) matrix_coefficients <= mc_seq; \/\/ CM: MUTATED/' \
      rtl/mpeg2/vld.v > "$MUT/vld_shipped.v"
  # no-commit: keep the clear, but no picture commit.
  sed -e 's/^\(\s*\)else if (.*matrix_coefficients <= mc_seq; \/\/ CM: commit at picture/\1else if (1) matrix_coefficients <= mc_seq; \/\/ CM: MUTATED/' \
      rtl/mpeg2/vld.v > "$MUT/vld_nocommit.v"
  # shipped-table: 3'd0 back with 3'd1 (BT.709).
  sed -e "/3'd0, \/\* not signalled/d" \
      -e "s|^\(\s*\)3'd1: /\* ITU-R Rec. 709 (1990) \*/|\13'd0, 3'd1: /* MUTATED */|" \
      rtl/mpeg2/yuv2rgb.v > "$MUT/yuv2rgb_shipped.v"

  # Each mutation must actually have applied, or its arm is tested against nothing.
  grep -q 'CM: per-sequence clear' "$MUT/vld_shipped.v" && { echo "  FAIL: shipped-vld clear not removed"; rc=1; }
  grep -q 'CM: MUTATED' "$MUT/vld_shipped.v"   || { echo "  FAIL: shipped-vld commit mutation did not apply"; rc=1; }
  grep -q 'CM: MUTATED' "$MUT/vld_nocommit.v"  || { echo "  FAIL: no-commit mutation did not apply"; rc=1; }
  grep -q 'CM: per-sequence clear' "$MUT/vld_nocommit.v" || { echo "  FAIL: no-commit lost the clear"; rc=1; }
  grep -q "3'd0, 3'd1: /\* MUTATED" "$MUT/yuv2rgb_shipped.v" || { echo "  FAIL: shipped-table mutation did not apply"; rc=1; }
  grep -q "3'd0, /\* not signalled" "$MUT/yuv2rgb_shipped.v" && { echo "  FAIL: shipped-table kept 3'd0 in the 601 arm"; rc=1; }

  for m in "shipped-vld:$MUT/vld_shipped.v:rtl/mpeg2/yuv2rgb.v:2 4" \
           "no-commit:$MUT/vld_nocommit.v:rtl/mpeg2/yuv2rgb.v:5" \
           "shipped-table:rtl/mpeg2/vld.v:$MUT/yuv2rgb_shipped.v:1 6"; do
    IFS=: read -r name vld y2r want <<< "$m"
    echo "== RED ($name): must fail exactly arms {$want} =="
    build "$MUT/sim_$name" "$vld" "$y2r"
    out=$(vvp "$MUT/sim_$name" +ES=$FIX 2>&1 | grep -v '^WARNING') || true
    echo "$out" | grep -E '^(ARM|SUMMARY|VACUOUS|   MISMATCH)' | head -14 || true
    got=$(failed_arms "$out")
    if echo "$out" | grep -q '^RESULT: PASS'; then
      echo "  FAIL: RED $name PASSED -- the gate cannot see this defect"; rc=1
    elif [ "$got" != "$want" ]; then
      echo "  FAIL: RED $name failed arms {$got}, want exactly {$want}"; rc=1
    else
      echo "  RED $name: failed exactly {$got}, as it must"
    fi
  done
fi

echo "== colour_matrix gate =="
build "$SIM" rtl/mpeg2/vld.v rtl/mpeg2/yuv2rgb.v
out=$(vvp "$SIM" +ES=$FIX 2>&1 | grep -v '^WARNING') || true
echo "$out" | grep -E '^(ES|ARM|SUMMARY|VACUOUS|RESULT|   MISMATCH)' || true
echo "$out" | grep -q '^RESULT: PASS' || { echo "  FAIL: colour_matrix gate"; rc=1; }

if [ $rc -eq 0 ]; then echo "run_colour_matrix: PASS"; else echo "run_colour_matrix: FAIL"; fi
exit $rc
