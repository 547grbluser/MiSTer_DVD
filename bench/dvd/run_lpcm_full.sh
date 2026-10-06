#!/usr/bin/env bash
# run_lpcm_full.sh -- every DVD-Video LPCM format (docs/lpcm_full.md).
#
# 48 or 96 kHz, 16/20/24-bit, 1-8 channels: downmixed to stereo (FFmpeg's channel
# order, the AC-3 path's law), 96 kHz decimated by a half-band on a 48 kHz HDMI link
# or played at its own rate on a 96 kHz one, reserved header values muted and
# announced.
#
# GREEN: tools/lpcm_model.py --selftest (the tables re-derived; unpack vs FFmpeg)
#        lpcm_full_tb  (lpcm_unpack + lpcm_hb vs the model, arms A-T)
#        lpcm_dec_tb   (dvd_audio_decode's seams, arms S1-S5)
#        ps_demux_lpcm_tb (the header byte's fields, arms H1-H6)
#        lpcm_unpack_tb (the original stereo path, unchanged)
#        tools/check_lpcm_wiring.py --red (the emu.sv / sys_top seams)
# RED  : sed-mutated copies; each must fail exactly its own arms.
#   lpcm_unpack.sv (lpcm_full_tb):
#     U1 mono-groups   : mono walks 4-sample groups (VLC's reading)  -> B C M
#                        (16-bit has no groups: A is unaffected)
#     U2 c51-gain      : 5.1's centre gain off by one LSB            -> G Q
#     U3 dmx-round     : the downmix truncates (left)                -> E F G H I K O Q
#     U4 dec-ignored   : 96 kHz stereo takes the original path       -> J R
#     U5 fifo-margin   : the new path's `full` has no early margin   -> T
#                        (only mono 16-bit has a pair in flight as the FIFO fills)
#   lpcm_hb.sv (lpcm_full_tb):
#     H1 hb-centre     : the centre tap off by one LSB               -> J K L M R
#     H2 hb-mask       : taps before the first input not masked      -> J K L M R
#     H3 hb-hold       : no back-pressure from the ring              -> R
#     H4 hb-room       : an output starts while the last is written  -> R
#     H5 hb-round      : the half-band truncates (left)              -> J K L M R
#   dvd_audio_decode.sv (lpcm_dec_tb):
#     D1 no-96k-nco    : the NCO never runs at 96 kHz                -> S1
#     D2 dec-no-link   : decimation ignores the link rate            -> S1
#     D3 bad-plays     : a reserved header is not muted              -> S3
#     D4 no-unsup      : lpcm_unsup never rises                      -> S3
#     D5 cdda-nch      : CD-DA takes the DVD header's channel count  -> S5
#     D6 96k-inc       : the 96 kHz increment is the 48 kHz one      -> S1
#   ps_demux.sv (ps_demux_lpcm_tb):
#     P1 fs96-bit4     : 32 kHz (code 3) read as 96 kHz              -> H3
#     P2 bad-no-quant3 : quant 3 is not flagged                      -> H5
#     P3 nch-dropped   : the channel count is never captured         -> H2 H4
#
# Usage: bench/dvd/run_lpcm_full.sh [--red]
set -u
cd "$(dirname "$0")/../.."
RED=0; [ "${1:-}" = "--red" ] && RED=1
OUT=".sim/lpcm_full"; mkdir -p "$OUT"
ENGINE="dvd/dts/cb_host_ram.sv $(ls dvd/ac3/*.sv | tr '\n' ' ') dvd/dts/dts_seq.sv dvd/dts/dts_vec.sv dvd/dts/dts_top.sv dvd/audio_engine.sv"
fail=0

echo "== GREEN =="
if python3 tools/lpcm_model.py --selftest > "$OUT/model.log" 2>&1; then
    echo "  PASS lpcm_model --selftest"; sed 's/^/      /' "$OUT/model.log" | grep -v selftest
else
    echo "  FAIL lpcm_model --selftest"; sed 's/^/      /' "$OUT/model.log"; fail=1
fi
python3 tools/lpcm_model.py --fixture "$OUT" > /dev/null || { echo "  FAIL fixtures"; exit 1; }

# build <name> <tb> <rtl...>   (in $OUT, so the fixtures resolve)
build() {
    local name=$1 tb=$2; shift 2
    iverilog -g2012 -I "$OUT" -I dvd/ac3 -o "$OUT/$name" "$tb" "$@" 2>"$OUT/$name.build"
}
# simulate <name> -> log; prints PASS/FAIL against <marker>
simulate() {
    local name=$1 marker=$2
    (cd "$OUT" && ln -sfn ../../dvd dvd && timeout 1800 vvp -n "$name" > "$name.log" 2>&1)
    if grep -q "$marker" "$OUT/$name.log"; then echo "  PASS $name"; else
        echo "  FAIL $name"; grep -E "^FAIL|FATAL|TIMEOUT" "$OUT/$name.log" | head -12 | sed 's/^/      /'; fail=1; fi
}

build full  bench/dvd/lpcm_full_tb.sv dvd/lpcm_unpack.sv dvd/lpcm_hb.sv \
    && simulate full "LPCM_FULL_TB: ALL TESTS PASSED" || { echo "  FAIL full (build)"; cat "$OUT/full.build"; fail=1; }
build dec   bench/dvd/lpcm_dec_tb.sv dvd/dvd_audio_decode.sv dvd/lpcm_unpack.sv dvd/lpcm_hb.sv $ENGINE \
    && simulate dec "LPCM_DEC_TB: ALL TESTS PASSED" || { echo "  FAIL dec (build)"; grep -v sorry "$OUT/dec.build" | head; fail=1; }
build demux bench/dvd/ps_demux_lpcm_tb.sv dvd/ps_demux.sv \
    && simulate demux "PASS: ps_demux LPCM quant capture + framing + header fields" || { echo "  FAIL demux (build)"; fail=1; }
build unpack bench/dvd/lpcm_unpack_tb.sv dvd/lpcm_unpack.sv dvd/lpcm_hb.sv \
    && simulate unpack "ALL TESTS PASSED\|PASS" || { echo "  FAIL unpack (build)"; fail=1; }
if python3 tools/check_lpcm_wiring.py --red > "$OUT/wiring.log" 2>&1 && python3 tools/check_lpcm_wiring.py >> "$OUT/wiring.log"; then
    echo "  PASS check_lpcm_wiring (+ its red self-test)"
else
    echo "  FAIL check_lpcm_wiring"; sed 's/^/      /' "$OUT/wiring.log"; fail=1
fi

if [ $RED -eq 1 ]; then
    echo "== RED (each mutation must fail exactly its arms) =="
    # mutate <name> <file> <expected arms> <sed-expr>: runs in the background; the
    # verdict lands in $d/verdict and is printed, in order, once all have finished
    NAMES=()
    mutate() { NAMES+=("$1"); mutate_one "$@" > "$OUT/red_$1/verdict" 2>&1 & }
    mutate_one() {
        local name=$1 file=$2 want=$3 expr=$4
        local d="$OUT/red_$name"
        local src="$d/$(basename "$file")"
        sed "$expr" "$file" > "$src"
        if cmp -s "$file" "$src"; then
            echo "  FAIL $name: the sed matched nothing (mutation is stale)"; return
        fi
        local rtl tb
        case "$file" in
            dvd/lpcm_unpack.sv) tb=bench/dvd/lpcm_full_tb.sv;     rtl="$src dvd/lpcm_hb.sv" ;;
            dvd/lpcm_hb.sv)     tb=bench/dvd/lpcm_full_tb.sv;     rtl="dvd/lpcm_unpack.sv $src" ;;
            dvd/dvd_audio_decode.sv) tb=bench/dvd/lpcm_dec_tb.sv; rtl="$src dvd/lpcm_unpack.sv dvd/lpcm_hb.sv $ENGINE" ;;
            dvd/ps_demux.sv)    tb=bench/dvd/ps_demux_lpcm_tb.sv; rtl="$src" ;;
        esac
        # shellcheck disable=SC2086
        if ! iverilog -g2012 -I "$OUT" -I dvd/ac3 -o "$d/sim" "$tb" $rtl 2>"$d/build"; then
            echo "  FAIL $name (build)"; grep -v sorry "$d/build" | head -5; return
        fi
        (cd "$OUT" && timeout 1800 vvp -n "red_$name/sim" > "red_$name/log" 2>&1)
        local got
        got=$(grep -E '^FAIL [A-Z][0-9]?[ :]' "$d/log" | sed -E 's/^FAIL ([A-Z][0-9]?)[ :].*/\1/' \
              | sort -u | tr '\n' ' ' | sed 's/ $//')
        if [ "$got" = "$want" ]; then echo "  ok   $name -> fails [$got]"
        else echo "  FAIL $name: failed [$got], expected [$want]"; fi
    }
    for n in U1_mono_groups U2_c51_gain U3_dmx_round U4_dec_ignored U5_fifo_margin H1_hb_centre \
             H2_hb_mask H3_hb_hold H4_hb_room H5_hb_round D1_no_96k_nco D2_dec_no_link \
             D3_bad_plays D4_no_unsup D5_cdda_nch D6_96k_inc P1_fs96_bit4 P2_bad_noquant3 \
             P3_nch_dropped; do rm -rf "$OUT/red_$n"; mkdir -p "$OUT/red_$n"; done
    U=dvd/lpcm_unpack.sv; H=dvd/lpcm_hb.sv; D=dvd/dvd_audio_decode.sv; P=dvd/ps_demux.sv
    mutate U1_mono_groups  $U "B C M"            "s/wire         mono    = np_cur \&\& (nch_cur == 3'd0);/wire         mono    = 1'b0;/"
    mutate U2_c51_gain     $U "G Q"              "s/6'o52: begin gl = 19'sd38390;/6'o52: begin gl = 19'sd38391;/"
    mutate U3_dmx_round    $U "E F G H I K O Q"  "s/rnd_l = sum_l + 38'sd65536;/rnd_l = sum_l + 38'sd0;/"
    mutate U4_dec_ignored  $U "J R"              "s/!((nch_m1 == 3'd1) \&\& !dec) : npath;/!(nch_m1 == 3'd1) : npath;/"
    mutate U5_fifo_margin  $U "T"                "s/(level >= (DEPTH\[FIFO_AW:0\] - 4))/(level >= (DEPTH[FIFO_AW:0] - 0))/"
    mutate H1_hb_centre    $H "J K L M R"        "s/default: hc = 18'sd65536;/default: hc = 18'sd65535;/"
    mutate H2_hb_mask      $H "J K L M R"        "s/wire         tap_ok = warm || (k <= nlo);/wire         tap_ok = 1'b1;/"
    mutate H3_hb_hold      $H "R"                "s/assign hold = !ahead\[15\] \&\& (ahead >= 16'd41);/assign hold = 1'b0;/"
    mutate H4_hb_room      $H "R"                "s/if (owed \&\& out_room \&\& !out_v \&\& /if (owed \&\& out_room \&\& /"
    mutate H5_hb_round     $H "J K L M R"        "s/rl = al + pl + 40'sd65536;/rl = al + pl + 40'sd0;/"
    mutate D1_no_96k_nco   $D "S1"               "s/(lpcm_src \&\& lpcm_fs96 \&\& link96 \&\& !lpcm_bad) ? 2'd3 : 2'd1;/2'd1;/"
    mutate D2_dec_no_link  $D "S1"               "s/\&\& lpcm_fs96 \&\& !link96),/\&\& lpcm_fs96),/"
    mutate D3_bad_plays    $D "S3"               "s/\&\& !discard_cur \&\& !lpcm_bad;/\&\& !discard_cur;/"
    mutate D4_no_unsup     $D "S3"               "s/assign      lpcm_unsup = lpcm_src \&\& lpcm_bad;/assign      lpcm_unsup = 1'b0;/"
    mutate D5_cdda_nch     $D "S5"               "s/\.nch_m1   ((cdda_mode || eng_pcm) ? 3'd1 : lpcm_nch_m1),/.nch_m1   (lpcm_nch_m1),/"
    mutate D6_96k_inc      $D "S1"               "s/(nco_fs == 2'd3) ? NCO_INC_96K : NCO_INC;/(nco_fs == 2'd3) ? NCO_INC : NCO_INC;/"
    mutate P1_fs96_bit4    $P "H3"               "s/lpcm_fs96_r  <= (in_byte\[5:4\] == 2'b01);/lpcm_fs96_r  <= in_byte[4];/"
    mutate P2_bad_noquant3 $P "H5"               "s/lpcm_bad_r   <= (in_byte\[5\]) || (in_byte\[7:6\] == 2'b11);/lpcm_bad_r   <= (in_byte[5]);/"
    mutate P3_nch_dropped  $P "H2 H4"            "/lpcm_nch_r   <= in_byte\[2:0\];/d"
    wait
    for n in "${NAMES[@]}"; do
        cat "$OUT/red_$n/verdict"
        grep -q "^  ok " "$OUT/red_$n/verdict" || fail=1
    done
fi

if [ $fail -eq 0 ]; then echo "run_lpcm_full: PASS"; else echo "run_lpcm_full: FAIL"; exit 1; fi
