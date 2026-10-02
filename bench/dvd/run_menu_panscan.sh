#!/usr/bin/env bash
# Gate for "a 16:9 menu's permitted display mode overrides Letterbox/Crop"
# (2026-10-01; docs/crt_anamorphic.md §11).
#
# A 4:3 set-top player follows the IFO V_ATR permitted_df field: a 16:9 menu that
# denies letterbox is pan&scanned even when the player is set to Letterbox, and one
# that denies pan&scan is letterboxed even when set to Pan&Scan. The core does the
# same on the analog interlaced raster, for MENUS only (user decision: 935/940 main
# features deny pan&scan, so honouring the title flag would just undo Crop).
#
# Two arms, one per place the feature lives:
#   check_menu_panscan_wiring.py  THE RESOLVE AND ITS SEAMS. emu.sv has no bench, so
#                                 the checker pulls analog_letterbox/analog_crop's
#                                 whole definition closure out of emu.sv and runs it
#                                 over all 1024 input points against a reference
#                                 model (+ titles bit-identical to v0.8.0), then
#                                 checks the port and the downstream consumers.
#   iso_reader_menu_tb  T2/T4     THE CAPTURE. menu_ar_df = V_ATR high byte [1:0],
#                                 VTSM 0x4D -> 1 (P&S only), then VMGM 0x4E -> 2
#                                 (LB only) overwriting it.
#
# --red applies one targeted mutation per claim and requires its own arm to fail:
#   M1  emu: reader port left open           -> "must connect"
#   M2  emu: Letterbox->Crop arm deleted     -> "disagrees with the model" (THE FEATURE)
#   M3  emu: df compare 1 -> 2 (bits swapped)-> "disagrees with the model"
#   M4  emu: menu gate dropped               -> "TITLE resolve changed"
#   M5  emu: crop arm reads want_lb raw      -> "both high"
#   M6  emu: disp_hcrop_en decoupled         -> "disp_hcrop_en"
#   R1  reader: wrong bit pair [3:2]         -> T2 menu_ar_df
#   R2  reader: bit pair reversed            -> T2 menu_ar_df
#
#   bench/dvd/run_menu_panscan.sh          # GREEN arms
#   bench/dvd/run_menu_panscan.sh --red    # GREEN + the mutation arms
set -u
cd "$(dirname "$0")/../.."
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
rc=0

pass()   { echo "  ok   $1"; }
failed() { echo "  FAIL $1"; rc=1; }

# A mutation that does not change the file is a bench that cannot fail.
mut() {   # $1 = source, $2 = sed program, $3 = destination
    sed "$2" "$1" > "$3"
    if cmp -s "$1" "$3"; then
        echo "  FAIL mutation did not apply (stale sed pattern): $2"; rc=1; return 1
    fi
}

run_wire() {  # $1 = emu.sv to check, $2 = label
    python3 tools/check_menu_panscan_wiring.py "$1" > "$TMP/w_$2.out" 2>&1
}
run_reader() {  # $1 = dvd_iso_reader.sv, $2 = label
    iverilog -g2012 -I rtl/mpeg2 -y dvd -Y .sv -o "$TMP/r_$2" "$1" bench/dvd/iso_reader_menu_tb.sv \
        > "$TMP/r_$2.log" 2>&1 \
        || { echo "  FAIL compile failed ($2)"; grep -i error "$TMP/r_$2.log" | head; rc=1; return 2; }
    vvp -n "$TMP/r_$2" > "$TMP/r_$2.out" 2>&1
    grep -q "ISO_READER_MENU_TB: ALL TESTS PASSED" "$TMP/r_$2.out"
}

echo "== GREEN"
if run_wire dvd/emu.sv green; then pass "$(cat "$TMP/w_green.out")"; else failed "check_menu_panscan_wiring.py"; cat "$TMP/w_green.out"; fi
if run_reader dvd/dvd_iso_reader.sv green; then pass "iso_reader_menu_tb (T2 VTSM df=1, T4 VMGM df=2)"; else failed "iso_reader_menu_tb"; grep "ERR" "$TMP/r_green.out" | head; fi

if [ "${1:-}" != "--red" ]; then
    [ $rc -eq 0 ] && echo "run_menu_panscan: PASS" || echo "run_menu_panscan: FAIL"
    exit $rc
fi

echo "== RED (each mutation must be caught by its own arm)"
# ⚠ delimiter is @ throughout -- these expressions contain bitwise |; a literal &
# in a REPLACEMENT must be written \&.
emu_red() {   # $1 = label, $2 = sed program, $3 = a string the failure must name
    mut dvd/emu.sv "$2" "$TMP/$1.sv" || return
    if run_wire "$TMP/$1.sv" "$1"; then
        failed "$1 -- the wiring check PASSED a mutated emu.sv"
    elif grep -q -- "$3" "$TMP/w_$1.out"; then
        pass "$1 caught (names \"$3\")"
    else
        failed "$1 failed, but not for its own reason (wanted \"$3\")"; cat "$TMP/w_$1.out"
    fi
}

emu_red M1 's@\.menu_ar_df     (menu_ar_df_w),@.menu_ar_df     (),@'                        'must connect'
emu_red M2 's@((analog_want_lb   & ~menu_lb_to_crop) |@((analog_want_lb) |@'                'disagrees with the model'
emu_red M3 "s@(menu_ar_df_w == 2'd1)@(menu_ar_df_w == 2'd2)@"                               'disagrees with the model'
emu_red M4 's@wire analog_menu169  = menus_on & menu_active & menu_ar_wide_w;@wire analog_menu169  = menu_ar_wide_w;@' 'TITLE resolve changed'
emu_red M5 's@| (analog_want_lb   & menu_lb_to_crop));@| analog_want_lb);@'                 'both high'
emu_red M6 's@wire       disp_hcrop_en    = analog_crop;@wire       disp_hcrop_en    = analog_crop \& ~menu_active;@' 'disp_hcrop_en'

reader_red() {  # $1 = label, $2 = sed program, $3 = the ERR the bench must print
    mut dvd/dvd_iso_reader.sv "$2" "$TMP/$1.sv" || return
    if run_reader "$TMP/$1.sv" "$1"; then
        failed "$1 -- iso_reader_menu_tb PASSED a mutated reader"
    elif grep -q -- "$3" "$TMP/r_$1.out"; then
        pass "$1 caught (ERR $3)"
    else
        failed "$1 failed, but not on its own check (wanted \"$3\")"; grep "ERR" "$TMP/r_$1.out" | head
    fi
}

reader_red R1 's@menu_ar_df   <= rbuf\[0\]\[1:0\];@menu_ar_df   <= rbuf[0][3:2];@'                  'T2 menu_ar_df'
reader_red R2 's@menu_ar_df   <= rbuf\[0\]\[1:0\];@menu_ar_df   <= {rbuf[0][0], rbuf[0][1]};@'      'T2 menu_ar_df'

[ $rc -eq 0 ] && echo "run_menu_panscan: PASS (green + red)" || echo "run_menu_panscan: FAIL"
exit $rc
