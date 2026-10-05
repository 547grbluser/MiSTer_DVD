#!/usr/bin/env bash
# Gate for disp_vscale on the PROGRESSIVE FRAME path (feature/progressive-aspect,
# docs/crt_anamorphic.md §11 + §13). See bench/dvd/disp_vscale_frame_tb.sv for what is
# scored and why.
#
#   ./bench/dvd/run_vscale_frame.sh          GREEN arms
#   ./bench/dvd/run_vscale_frame.sh --red    + mutations, each of which must fail its own arm
#
# What the arms prove:
#   frame_lb*   Letterbox on a frame scan: H*3/4 lines, golden blend values, tags
#               ROW_0 / ROW_1 / ROW_X (the mixer's contract; a ROW_X on line 1 is a black
#               line under the top bar). NTSC 480 and PAL 576, with and without back-pressure.
#   field_lb*   the Interlaced (field) path: tags ROW_0-or-ROW_1 / ROW_X / ROW_X -- unchanged.
#   frame_fit   vscale_en = 0: frame scans pass through untouched.
#   toggle      a Letterbox change landing between a frame's line 0 and line 1 takes effect
#               on the NEXT scan, in both directions.
set -u
cd "$(dirname "$0")/../.."
OUT=.sim/vscale_frame; mkdir -p "$OUT"
SRC="rtl/mpeg2/wrappers.v rtl/mpeg2/fwft.v rtl/mpeg2/xfifo_sc.v rtl/mpeg2/xilinx_fifo_dc.v"
TB=bench/dvd/disp_vscale_frame_tb.sv
fail=0

build() {   # build <name> <disp_vscale.sv>
  iverilog -g2012 -I rtl/mpeg2 -o "$OUT/$1" $SRC "$2" $TB 2>"$OUT/$1.build.log" \
    || { echo "  BUILD FAILED: $1 (see $OUT/$1.build.log)"; return 1; }
}
PENDING=()
run() {     # run <sim> <label> <expect pass|fail> <plusargs...>
  local sim=$1 label=$2 want=$3; shift 3
  ( vvp -n "$OUT/$sim" "$@" >"$OUT/$label.log" 2>&1; echo $? >"$OUT/$label.rc" ) &
  PENDING+=("$label:$want")
}
score() {
  wait
  local e label want rc got
  for e in "${PENDING[@]}"; do
    label=${e%%:*}; want=${e##*:}; rc=$(cat "$OUT/$label.rc" 2>/dev/null || echo 99)
    got=fail
    if [ "$rc" = 0 ] && grep -q "RESULT: PASS" "$OUT/$label.log"; then got=pass; fi
    if [ "$got" = "$want" ]; then echo "  ok    $label (${got})"
    else echo "  FAIL  $label: want $want, got $got (see $OUT/$label.log)"; fail=1; fi
  done
  PENDING=()
}
green_arms() {   # green_arms <sim> <prefix> <expect>
  local sim=$1 p=$2 w=$3
  run "$sim" "${p}frame_lb"      "$w" +case=frame_lb
  run "$sim" "${p}frame_lb_pal"  "$w" +case=frame_lb +h=576
  run "$sim" "${p}frame_lb_bp"   "$w" +case=frame_lb +bp=1
  run "$sim" "${p}field_lb"      "$w" +case=field_lb
  run "$sim" "${p}field_lb_pal"  "$w" +case=field_lb +h=288
  run "$sim" "${p}field_lb_bp"   "$w" +case=field_lb +bp=1
  run "$sim" "${p}frame_fit"     "$w" +case=frame_fit
  run "$sim" "${p}frame_fit_bp"  "$w" +case=frame_fit +bp=1
  run "$sim" "${p}toggle"        "$w" +case=toggle
  run "$sim" "${p}toggle_bp"     "$w" +case=toggle +bp=1
}

echo "== disp_vscale frame path: GREEN arms"
build green dvd/disp_vscale.sv || exit 1
green_arms green "" pass
score

if [ "${1:-}" = "--red" ]; then
  mutate() {  # mutate <name> <sed-expr>
    local dst="$OUT/mut_$1.sv"
    sed "$2" dvd/disp_vscale.sv >"$dst"
    if cmp -s dvd/disp_vscale.sv "$dst"; then echo "  FAIL  mutation $1 did not apply (anchor moved)" >&2; return 1; fi
    build "mut_$1" "$dst"
  }
  echo "== RED: mutations (each must fail its own arm)"
  # M1: head side takes the paired ROW_1 for a new scan again (the 359-line re-arm)
  if mutate M1_head_rearm 's/wire        h_ft   = (h_pos == ROW_0_COL_0) || ((h_pos == ROW_1_COL_0) \&\& ~h_prev_row0);/wire        h_ft   = (h_pos == ROW_0_COL_0) || (h_pos == ROW_1_COL_0);/'; then
    run mut_M1_head_rearm M1_head_rearm fail +case=frame_lb
    run mut_M1_head_rearm M1_head_rearm_field pass +case=field_lb      # the field path never pairs
  else fail=1; fi
  # M2: input side re-decides route/mode at the paired ROW_1 (a mid-frame change tears)
  if mutate M2_input_rearm 's/wire        in_ft   = (in_pos == ROW_0_COL_0) || ((in_pos == ROW_1_COL_0) \&\& ~in_prev_row0);/wire        in_ft   = (in_pos == ROW_0_COL_0) || (in_pos == ROW_1_COL_0);/'; then
    run mut_M2_input_rearm M2_input_rearm fail +case=toggle
  else fail=1; fi
  # M3: output line 1 of a frame scan tagged ROW_X (the black line under the top bar)
  if mutate M3_no_row1_tag 's/(e_line_second \&\& scan_frame) ? ROW_1_COL_0 : ROW_X_COL_0/ROW_X_COL_0/'; then
    run mut_M3_no_row1_tag M3_no_row1_tag fail +case=frame_lb
  else fail=1; fi
  # M4: ROW_1 tag on line 1 of EVERY scan, field scans included (changes the Interlaced path)
  if mutate M4_row1_on_fields 's/(e_line_second \&\& scan_frame) ? ROW_1_COL_0/e_line_second ? ROW_1_COL_0/'; then
    run mut_M4_row1_on_fields M4_row1_on_fields fail +case=field_lb
  else fail=1; fi
  score
fi
[ $fail -eq 0 ] && echo "ALL OK" || { echo "SOME ARMS FAILED"; exit 1; }
