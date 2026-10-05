#!/usr/bin/env bash
# Gate for "an explicit Analog Aspect Letterbox/Crop takes effect on the Progressive
# raster" (feature/progressive-aspect; docs/crt_anamorphic.md §13).
#
# Before this branch analog_letterbox/analog_crop were gated on interlaced_eff, so Analog
# Aspect did nothing on Progressive: a 31 kHz analog display showed 16:9 discs squeezed,
# and a 4:3 HDMI set had no Crop. Now one net, aa_live (interlaced_eff, or an explicit
# Letterbox/Crop), gates both the picture AND player_regs' SPRM14. Hard constraints:
# Auto and Fit on Progressive do not change; the interlaced raster is bit-identical.
#
# Arms:
#   tools/check_prog_aspect_wiring.py  THE RESOLVE, evaluated over every input point
#                                      ([1]-[4]), aa_live and its consumers ([5]), the B15
#                                      button ([6]), ARX/ARY ([7]).
#   bench/dvd/player_regs_tb.sv        the SPRM14 MAPPING for the Progressive profiles
#                                      (aa_live 0 + Auto/Fit -> 0x0C00, 1 + LB -> 0x0200,
#                                      1 + Crop -> 0x0100) -- the half the checker does not
#                                      model, so the two together prove the table.
#   bench/dvd/run_vscale_frame.sh      disp_vscale on the PROGRESSIVE FRAME path, which this
#                                      feature makes reachable (the §11 latent defect).
#
# RED on main is NOT encoded here: a `git show main:` arm goes green the moment the branch
# merges, which is exactly how run_player_regs.sh's arm rotted. It was demonstrated once by
# hand (docs/status_log.md); the mutations below carry the same proof permanently.
#
# --red: one targeted mutation per claim; each must fail its OWN arm.
#   M1  aa_live gains Auto (sel 0)          -> "[2] Auto/Fit now correct"  ("Auto now corrects")
#   M2  aa_live gains Fit (sel 1)           -> "[5] aa_live is not"  (output-EQUIVALENT today:
#       Fit never corrects and maps to SPRM14 0x0C00 with or without aa_live, so only the
#       gate's own truth table can see it -- which is why [5] evaluates aa_live directly)
#   M3  aa_live loses Crop (sel 3)          -> "[3]"
#   M4  the SIF guard dropped               -> "[4]"
#   M5  assigns back on interlaced_eff      -> "[5] analog_letterbox does not read aa_live"
#   M6  player_regs reads interlaced_eff    -> "[5] player_regs_inst"
#   M7  B15 follows aa_live                 -> "[6]"
#   M8  the interlaced menu swap altered    -> "[1]"
#   M9  aa_live used before its declaration -> "before its declaration"
#   M10 ARX stops reading analog_crop       -> "[7]"
#
#   bench/dvd/run_prog_aspect.sh          # GREEN arms
#   bench/dvd/run_prog_aspect.sh --red    # GREEN + the mutation arms
set -u
cd "$(dirname "$0")/../.."
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
rc=0

pass()   { echo "  ok   $1"; }
failed() { echo "  FAIL $1"; rc=1; }

mut() {   # $1 = source, $2 = sed program, $3 = destination
    sed "$2" "$1" > "$3"
    if cmp -s "$1" "$3"; then
        echo "  FAIL mutation did not apply (stale sed pattern): $2"; rc=1; return 1
    fi
}
run_wire() {  # $1 = emu.sv to check, $2 = label
    python3 tools/check_prog_aspect_wiring.py "$1" > "$TMP/w_$2.out" 2>&1
}

echo "== GREEN"
if run_wire dvd/emu.sv green; then pass "$(cat "$TMP/w_green.out")"; else failed "check_prog_aspect_wiring.py"; cat "$TMP/w_green.out"; fi
if iverilog -g2012 -o "$TMP/pr" dvd/player_regs.sv bench/dvd/player_regs_tb.sv > "$TMP/pr.log" 2>&1 \
   && vvp -n "$TMP/pr" > "$TMP/pr.out" 2>&1 && grep -q "RESULT: PASS (player_regs)" "$TMP/pr.out" \
   && grep -q "\[P5\] OK: progressive profiles" "$TMP/pr.out"; then
    pass "player_regs_tb (incl. the Progressive SPRM14 profiles)"
else failed "player_regs_tb"; tail -5 "$TMP/pr.out" "$TMP/pr.log" 2>/dev/null; fi
if bench/dvd/run_vscale_frame.sh > "$TMP/vsf.out" 2>&1; then pass "run_vscale_frame.sh (disp_vscale frame path)"
else failed "run_vscale_frame.sh"; tail -15 "$TMP/vsf.out"; fi

if [ "${1:-}" != "--red" ]; then
    [ $rc -eq 0 ] && echo "run_prog_aspect: PASS" || echo "run_prog_aspect: FAIL"
    exit $rc
fi

echo "== RED (each mutation must be caught by its own arm)"
# ⚠ delimiter is @ throughout -- these expressions contain bitwise |; a literal & in a
# REPLACEMENT must be written \&.
emu_red() {   # $1 = label, $2 = sed program, $3 = a string the failure must name
    mut dvd/emu.sv "$2" "$TMP/$1.sv" || return
    if run_wire "$TMP/$1.sv" "$1"; then
        failed "$1 -- the wiring check PASSED a mutated emu.sv"
    elif grep -q -F -- "$3" "$TMP/w_$1.out"; then
        pass "$1 caught (names \"$3\")"
    else
        failed "$1 failed, but not for its own reason (wanted \"$3\")"; cat "$TMP/w_$1.out"
    fi
}
AA="wire       aa_live = interlaced_eff | (aa_osd_sel == 2'd2) | (aa_osd_sel == 2'd3);"
emu_red M1  "s@$AA@wire       aa_live = interlaced_eff | (aa_osd_sel == 2'd0) | (aa_osd_sel == 2'd2) | (aa_osd_sel == 2'd3);@" '[2] Auto/Fit now correct'
emu_red M2  "s@$AA@wire       aa_live = interlaced_eff | (aa_osd_sel == 2'd1) | (aa_osd_sel == 2'd2) | (aa_osd_sel == 2'd3);@" '[5] aa_live is not'
emu_red M3  "s@$AA@wire       aa_live = interlaced_eff | (aa_osd_sel == 2'd2);@"                    '[3]'
emu_red M4  's@assign analog_letterbox = aa_live \& ~sif_det_s2 \&@assign analog_letterbox = aa_live \&@' '[4]'
emu_red M5  's@assign analog_letterbox = aa_live \& ~sif_det_s2 \&@assign analog_letterbox = interlaced_eff \& ~p240_eff \&@;s@assign analog_crop      = aa_live \& ~sif_det_s2 \&@assign analog_crop      = interlaced_eff \& ~p240_eff \&@' '[5] analog_letterbox does not read aa_live'
emu_red M6  's@\.aa_live              (aa_live),@.aa_live              (interlaced_eff),@'       '[5] player_regs_inst'
emu_red M7  's@\.analog_live (interlaced_eff),@.analog_live (aa_live),@'                         '[6]'
emu_red M8  "s@wire menu_crop_to_lb = analog_menu169 & (menu_ar_df_w == 2'd2);@wire menu_crop_to_lb = analog_menu169 \& (menu_ar_df_w == 2'd3);@" '[1]'
emu_red M9  's@^wire       analog_want;@wire       analog_want;\nwire       aa_early = aa_live;@'  'before its declaration'
emu_red M10 's@^assign VIDEO_ARX    = (analog_letterbox | analog_crop)@assign VIDEO_ARX    = (analog_letterbox)@' '[7]'

[ $rc -eq 0 ] && echo "run_prog_aspect: PASS (green + red)" || echo "run_prog_aspect: FAIL"
exit $rc
