#!/usr/bin/env bash
# run_chroma_reuse.sh -- gate for F2: the display reuses chroma rows instead of fetching two
# per plane on every line (docs/decode_pacing.md §7 F2).
#
# WHAT IS PROVEN
#   resample_addrgen used to request, for each macroblock-line, the Y pair plus an upper and
#   a lower chroma row for each of U and V (6 words), although one chroma row serves several
#   lines. With CHROMA_REUSE = 1 resample_dta keeps two rows per plane and the address
#   generator requests only rows no slot holds, telling resample_dta which words exist.
#   [1] BIT-EXACT: the full display chain (bench/dvd/resample_chain_tb.sv) is built twice,
#       CHROMA_REUSE = 0 (the F1 structure) and 1 (F2). The memory answers every read with a
#       hash of its OWN address (+addrhash=1), so a wrong row, a dropped, extra or reordered
#       word changes the pixels. Two checksums must match: PIXSUM, every displayed pixel per
#       raster frame, and RSUM, every pixel resample EMITS per scan (independent of raster
#       timing). Arms: progressive; weave (interlaced content on the progressive raster --
#       FRAME scans with interlaced chroma upsampling, the case F2 targets); field blend and
#       progressive bob (H+1-line walks); the pause field still (interpolated half scans);
#       field scans
#       with interlaced and progressive upsampling, both field orders; a height that is not
#       a multiple of 16 (memory_address's clip against the addrgen's mb_height clamps),
#       progressive and field; 720 wide; Crop; SIF 2x line repeat; bursty memory stalls;
#       30->60 pacing; and a mid-line crop toggle (+croptog).
#   [2] THE SAVING, EXACTLY: read words per SCAN, counted from the addrgen's request states.
#       Each expected value is derived by hand from the key arithmetic (the row memory_address
#       fetches: delta_y + ((mv + sign) >>> 1) >>> 1) with two slots per bank (a bank = the
#       row's parity), where every distinct chroma key is fetched exactly once -- except that
#       a scan's first line SKIPS (fetches both rows, files neither: its registered key is one
#       cycle stale, see TIMING in resample_addrgen), so:
#         rows  = 2 (the first line) + the distinct keys the REST of the scan needs
#         words = lines * mb * 2 (Y)  +  rows * mb * 2 (U, V)
#       Rows per line (the chroma-row fix): progressive upsampling reads y/2 and its
#       neighbour (+1 on odd lines, -1 on even); interlaced upsampling reads the field's row
#       2*(y>>2) + (y&1) and the SAME field's neighbour two frame rows away (+2 when
#       (y>>1) is odd). Bottom clamps at vertical_size/2 rows, so an edge row replicates.
#         prog   256 lines, rows 0..127 (128), 4 MB              -> 2048 + 130*8 = 3088 / 1024
#         weave  FRAME, interlaced upsampling, rows 0..127: each field's rows land in their
#                own bank, so none is fetched twice although consecutive lines share no row
#                                                                -> 3088 / 1024
#         il i   a field reads only its own parity: 128 lines, 64 rows
#                                                                -> 1024 + 66*8 = 1552 / 512
#         il p   TOP: the rest needs rows 0..127 (128)           -> 1024 + 130*8 = 2064 / 512
#                BOTTOM: row 0 is read by the first line (y=1) only, the rest needs 1..127
#                (127)                                           -> 1024 + 129*8 = 2056 / 512
#         tall   300 lines (vsz 300), rows 0..149                -> 2400 + 152*8 = 3616 / 1200
#         tall il  TOP 151 lines (0..300): even rows 0..148 (75) plus key 150 for line 300,
#                  one past the picture (memory_address clips it) -> 1208 + 78*8 = 1832 / 604
#                  BOTTOM 150 lines (1..299): odd rows 1..149 (75) -> 1200 + 77*8 = 1816 / 600
#         wide   45 MB x 256 lines, 128 rows                     -> 23040 + 130*90 = 34740 / 11520
#         crop   2 MB (cols 1..2) x 256 lines, 128 rows          -> 1024 + 130*4 = 1544 / 512
#         sif    22 MB x 480 output lines (2x repeat), rows 0..119 -> 21120 + 122*44 = 26488 / 10560
#         blend, bob   the weave walk plus one line: after line 255 the walk steps back to 254,
#                whose row (126, clamped) its bank still holds -> 3088 + 8 = 3096 / 1028
#         still  (pause field still) normal field scans 1552 / 512; the interpolated slot
#                repeats one field line. Pinned TOP repeats the last line (slots hold it);
#                pinned BOTTOM repeats the first, which skipped, so row 1 is fetched there
#                instead of at y=3 -- the same total either way  -> 1552 + 8 = 1560 / 516
#       The F1 structure reads 6 words per macroblock-line in every arm. If a count differs,
#       the model or the slot policy is wrong: do not edit the expectation to match.
#   [3] The seams (one knob, the fifo width, the key against mem_addr.v, the flag layout) --
#       tools/check_chroma_reuse_wiring.py.
#   [4] THE RIGHT ROWS (the chroma-row fix, docs/decode_pacing.md §7 "F2 follow-up"). [1]
#       compares two builds that read the same rows, so it cannot see WHICH rows. With
#       +rowref=1 every chroma word names its own row (the bench decodes it from the
#       address) and every pixel resample emits is scored against the rows the spec wants
#       for its line, computed in the bench from disp_y alone. Upstream's offsets fail it on
#       every odd progressive line and every interlaced line with a neighbour.
#   Mutations: each must fail its own arm.
#     M1 resample_dta pops every chroma word although the addrgen skipped some -> the display
#        stalls waiting for words never requested: no picture (prog)
#     M2 no invalidation on a geometry change -> pixels in +croptog ONLY (prog must stay green:
#        that specificity is what shows the croptog arm reaches the invalidation)
#     M3 the key takes mv/2 as whole rows (+-1/+-2) instead of memory_address's arithmetic:
#        a fetched row is filed under the wrong key and served later -> pixels (prog)
#     M4 the addrgen fetches both rows always (and dta follows the flags): pixels stay
#        identical, only the word count can see it -> [2] (prog)
#     M5 resample_dta's column never restarts at COL_0 -> a line reads another line's
#        columns -> pixels (prog)
#     M6 no banks (every row in bank 0: the F2 two-slot cache): pixels stay identical, the
#        weave walk misses on every line -> [2] (weave) only
#     M7 upstream's offsets (+-2 / +-4): [1] cannot see it (both builds move) -> [4] (prog
#        and il_i)
#   NOT GATED, deliberately: the invalidation at STATE_NEXT_IMG. A scan always opens at the
#   top rows while the slots hold the previous scan's bottom rows, so no memory model can make
#   its removal visible through pixels; it is kept as defence for a rewritten frame slot.
#
# The +il arms FAIL resample_chain_tb's own geometry verdict in BOTH builds (that verdict is
# written for the progressive raster; +il without +crt is a field-path stimulus only). This
# runner reads the checksum lines, not the bench's exit status, exactly as run_osd_read.sh.
#
# Usage: bench/dvd/run_chroma_reuse.sh          (exit 0 = ALL GREEN)
set -uo pipefail
cd "$(dirname "$0")/../.."
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
rc=0

SRC=(rtl/mpeg2/resample.v dvd/resample_addrgen.v rtl/mpeg2/resample_dta.v
     rtl/mpeg2/resample_bilinear.v rtl/mpeg2/mem_addr.v rtl/mpeg2/mixer.v
     rtl/mpeg2/pixel_queue.v rtl/mpeg2/syncgen.v rtl/mpeg2/read_write.v
     rtl/mpeg2/wrappers.v rtl/mpeg2/fwft.v rtl/mpeg2/xilinx_fifo_dc.v rtl/mpeg2/xfifo_sc.v
     dvd/disp_hstretch.sv dvd/disp_vscale.sv bench/dvd/resample_chain_tb.sv)

build() {   # <out> <CHR> [file substitutions: orig=mutated ...]
    local out=$1 chr=$2; shift 2
    local files=("${SRC[@]}")
    for m in "$@"; do
        local o=${m%%=*} n=${m#*=}
        for i in "${!files[@]}"; do [ "${files[$i]}" = "$o" ] && files[$i]=$n; done
    done
    iverilog -g2012 -D__IVERILOG__ -DOSDR=0 -DCHR="$chr" -I rtl/mpeg2 -o "$out" "${files[@]}" 2>"$out.log"
}

# name | plusargs | expected words/mblines per scan (every scan, space-separated set) or -
ARMS=("prog|+frames=4|3088/1024"
      "weave|+weave=1 +frames=4|3088/1024"
      "il_i|+il=1 +pfr=0 +frames=4|1552/512"
      "il_i_tff|+il=1 +pfr=0 +tff=1 +frames=4|1552/512"
      "il_p|+il=1 +pfr=1 +frames=4|2064/512 2056/512"
      "il_p_tff|+il=1 +pfr=1 +tff=1 +frames=4|2064/512 2056/512"
      "tall|+mbh=19 +vsz=300 +frames=4|3616/1200"
      "tall_il|+mbh=19 +vsz=300 +il=1 +pfr=0 +frames=4|1832/604 1816/600"
      "wide|+wide=1 +frames=3|34740/11520"
      "crop|+vsmode=2 +frames=4|1544/512"
      "sif|+sif=1 +frames=3|26488/10560"
      "stall|+stallon=40 +stalloff=200 +frames=4|3088/1024"
      "pace|+pace=2 +frames=6|3088/1024"
      "blend|+weave=1 +blend=1 +frames=4|3096/1028"
      "bob|+weave=1 +bob=1 +frames=4|3096/1028"
      "still|+il=1 +pfr=0 +still=1 +frames=5|1552/512 1560/516"
      "croptog|+croptog=5 +frames=6|-")
# [4] reference arms: name | plusargs (run on the reuse build with +rowref=1)
REFS=("prog|+frames=4"
      "weave|+weave=1 +frames=4"
      "il_i|+il=1 +pfr=0 +frames=4"
      "il_i_tff|+il=1 +pfr=0 +tff=1 +frames=4"
      "il_p|+il=1 +pfr=1 +frames=4"
      "tall|+mbh=19 +vsz=300 +frames=4"
      "tall_il|+mbh=19 +vsz=300 +il=1 +pfr=0 +frames=4"
      "still|+il=1 +pfr=0 +still=1 +frames=5")

run() {   # <sim> <outfile> <plusargs...>
    # 28 simulations run at once; the SIF and wide arms take ~15 min each under that load.
    # A kill must read as TIMEOUT, not as "pixels differ" (a truncated run has fewer lines):
    # that misreport happened once, on a SIF arm that is bit-identical when run alone.
    timeout 2400 vvp "$1" +addrhash=1 "${@:3}" > "$2.raw" 2>&1
    [ $? = 124 ] && echo "TIMEOUT" > "$2.to"
    grep -E '^(PIXSUM|RSUM|RQS) ' "$2.raw" > "$2"; rm -f "$2.raw"
}
runref() {   # <sim> <outfile> <plusargs...>  (+rowref; keeps the ROWREF lines)
    timeout 2400 vvp "$1" +rowref=1 "${@:3}" > "$2.raw" 2>&1
    [ $? = 124 ] && echo "TIMEOUT" > "$2.to"
    grep -E '^ROWREF' "$2.raw" > "$2"; rm -f "$2.raw"
}
# refok <file>: 0 = scored enough lines, some interpolated, no bad pixel; prints the verdict
refok() {
    [ -f "$1.to" ] && { echo "TIMEOUT"; return 3; }
    local l; l=$(grep '^ROWREF lines' "$1")
    [ -n "$l" ] || { echo "no ROWREF summary"; return 2; }
    local lines interp bad
    lines=$(sed -E 's/.*lines=([0-9]+).*/\1/' <<< "$l"); interp=$(sed -E 's/.*interp=([0-9]+).*/\1/' <<< "$l")
    bad=$(sed -E 's/.*bad=([0-9]+).*/\1/' <<< "$l")
    echo "$l"
    [ "$lines" -ge 256 ] && [ "$interp" -ge 128 ] || return 2
    [ "$bad" = 0 ] || return 1
    return 0
}
arm_args() { local a=${1#*|}; echo "${a%%|*}"; }
arm_name() { echo "${1%%|*}"; }
arm_exp()  { echo "${1##*|}"; }

# same(): both checksums identical; the first PIXSUM frame is dropped (it may start mid-scan),
# RSUM is compared over the common prefix (the builds finish a different number of scans).
same() {   # <a> <b> <label> -> 0 identical, 1 differ, 2 vacuous, 3 a run timed out
    local a=$1 b=$2
    [ -f "$a.to" ] || [ -f "$b.to" ] && return 3
    local pa pb na nb n
    pa=$(grep '^PIXSUM' "$a" | tail -n +2); pb=$(grep '^PIXSUM' "$b" | tail -n +2)
    na=$(grep -c '^RSUM' "$a"); nb=$(grep -c '^RSUM' "$b"); n=$(( na < nb ? na : nb ))
    [ "$(printf '%s\n' "$pa" | grep -c PIXSUM)" -lt 2 ] || [ "$n" -lt 2 ] && return 2
    [ "$pa" = "$pb" ] || return 1
    [ "$(grep '^RSUM' "$a" | head -n "$n")" = "$(grep '^RSUM' "$b" | head -n "$n")" ] || return 1
    return 0
}

echo "== build: baseline (CHROMA_REUSE=0) and F2 (CHROMA_REUSE=1) =="
build "$TMP/base" 0 || { echo "  FAIL baseline build"; cat "$TMP/base.log"; exit 1; }
build "$TMP/f2"   1 || { echo "  FAIL F2 build"; cat "$TMP/f2.log"; exit 1; }

echo "== running ${#ARMS[@]} arms x 2 builds =="
for arm in "${ARMS[@]}"; do
    n=$(arm_name "$arm"); read -r -a args <<< "$(arm_args "$arm")"
    run "$TMP/base" "$TMP/$n.base" "${args[@]}" &
    run "$TMP/f2"   "$TMP/$n.f2"   "${args[@]}" &
done
for ref in "${REFS[@]}"; do
    n=${ref%%|*}; read -r -a args <<< "${ref#*|}"
    runref "$TMP/f2" "$TMP/$n.ref" "${args[@]}" &
done
wait

echo "== [1] bit-exact pixels, every arm =="
for arm in "${ARMS[@]}"; do
    n=$(arm_name "$arm")
    same "$TMP/$n.base" "$TMP/$n.f2"; r=$?
    case $r in
        0) echo "  ok   [$n] $(grep -c '^PIXSUM' "$TMP/$n.base") frames, $(grep -c '^RSUM' "$TMP/$n.f2") scans bit-identical" ;;
        1) echo "  FAIL [$n] pixels differ"; diff <(grep -v '^RQS' "$TMP/$n.base") <(grep -v '^RQS' "$TMP/$n.f2") | head -6; rc=1 ;;
        3) echo "  FAIL [$n] TIMEOUT (a simulation was killed; not a verdict on the pixels)"; rc=1 ;;
        *) echo "  FAIL [$n] too few frames/scans to compare -- the arm is vacuous"; rc=1 ;;
    esac
done

echo "== [2] read words per scan, exactly as the key model derives =="
for arm in "${ARMS[@]}"; do
    n=$(arm_name "$arm"); exp=$(arm_exp "$arm")
    [ "$exp" = "-" ] && continue
    got=$(grep '^RQS' "$TMP/$n.f2" | awk '{split($3,w,"="); split($4,m,"="); print w[2] "/" m[2]}' | sort -u | tr '\n' ' ')
    base=$(grep '^RQS' "$TMP/$n.base" | awk '{split($3,w,"="); split($4,m,"="); printf "%.3f\n", w[2]/m[2]}' | sort -u | tr '\n' ' ')
    nscan=$(grep -c '^RQS' "$TMP/$n.f2")
    bad=0
    for g in $got; do [[ " $exp " == *" $g "* ]] || bad=1; done
    if [ "$nscan" -lt 2 ]; then echo "  FAIL [$n] only $nscan scans counted -- vacuous"; rc=1
    elif [ $bad = 0 ] && [ "$base" = "6.000 " ]; then echo "  ok   [$n] $got(F1: 6.000 words/MB-line)"
    else echo "  FAIL [$n] got '$got' want '$exp' (F1 baseline '$base', want 6.000)"; rc=1; fi
done

echo "== [3] seams =="
python3 tools/check_chroma_reuse_wiring.py >/dev/null 2>&1 && echo "  ok   check_chroma_reuse_wiring" \
    || { echo "  FAIL check_chroma_reuse_wiring"; python3 tools/check_chroma_reuse_wiring.py | grep FAIL; rc=1; }

echo "== [4] the right chroma rows (+rowref), every pixel against the spec's rows =="
for ref in "${REFS[@]}"; do
    n=${ref%%|*}
    v=$(refok "$TMP/$n.ref"); r=$?
    case $r in
        0) echo "  ok   [$n] $v" ;;
        1) echo "  FAIL [$n] $v"; grep '^ROWREF-ERR' "$TMP/$n.ref" | head -3; rc=1 ;;
        *) echo "  FAIL [$n] vacuous or killed: $v"; rc=1 ;;
    esac
done

echo "== mutations (each must FAIL its arm) =="
mutate() {   # <src> <dst> <old> <new> [<old> <new> ...] -- every anchor must be unique
    python3 - "$@" <<'PYEOF'
import sys
src, dst, pairs = sys.argv[1], sys.argv[2], sys.argv[3:]
s = open(src).read()
for old, new in zip(pairs[0::2], pairs[1::2]):
    assert s.count(old) == 1, f'anchor not unique in {src}: {old!r} x{s.count(old)}'
    s = s.replace(old, new)
open(dst, 'w').write(s)
PYEOF
}
AG=dvd/resample_addrgen.v; DT=rtl/mpeg2/resample_dta.v
PROG="+frames=4"; TOG="+croptog=5 +frames=6"; WEAVE="+weave=1 +frames=4"; ILI="+il=1 +pfr=0 +frames=4"
# M<n>: <file> and its (old new) pairs
declare -A MF
declare -a M1 M2 M3 M4 M5 M6 M7
MF[1]=$DT; M1=("wire             q_fetch = q_low ? flg[3] : flg[0];" "wire             q_fetch = 1'b1;")
MF[2]=$AG; M2=("wire               cr_chg_now = cr_chg | (cr_sig != cr_sig_q);" "wire               cr_chg_now = 1'b0;")
MF[3]=$AG; M3=("wire signed [12:0] ck_mv_p   = ck_mv_c >>> 1; " "wire signed [12:0] ck_mv_p   = ck_mv >>> 1; ")
MF[4]=$AG; M4=("wire               cr_ok     = (cr_span[7:6] == 2'b00);" "wire               cr_ok     = 1'b0;")
MF[5]=$DT; M5=("(p_dout[2:0] == ROW_X_COL_0)) ? 6'd0 : col + 6'd1;" "(p_dout[2:0] == ROW_X_COL_0)) ? col + 6'd1 : col + 6'd1;")
MF[6]=$AG; M6=("wire               bu    = ck_up_q[0];" "wire               bu    = 1'b0;"
               "wire               bl    = ck_lo_q[0];" "wire               bl    = 1'b0;")
MF[7]=$AG; M7=("? 13'sd0 : -13'sd8;" "? 13'sd0 : -13'sd4_M7;"
               "? 13'sd0 : -13'sd4;" "? 13'sd0 : -13'sd2;"
               "-13'sd4_M7;" "-13'sd4;"
               "? 13'sd0 : 13'sd4;" "? 13'sd0 : 13'sd2;"
               "? 13'sd0 : 13'sd8;" "? 13'sd0 : 13'sd4;")
for k in 1 2 3 4 5 6 7; do
    declare -n mp="M$k"
    if mutate "${MF[$k]}" "$TMP/m$k.v" "${mp[@]}"; then
        build "$TMP/m$k" 1 "${MF[$k]}=$TMP/m$k.v" || { echo "  FAIL M$k build"; rc=1; continue; }
    else echo "  FAIL M$k anchor missing -- arm void"; rc=1; fi
    unset -n mp
done
read -r -a pa <<< "$PROG"; read -r -a ta <<< "$TOG"
for k in 1 3 4 5; do [ -f "$TMP/m$k" ] && run "$TMP/m$k" "$TMP/m$k.prog" "${pa[@]}" & done
[ -f "$TMP/m2" ] && { run "$TMP/m2" "$TMP/m2.tog" "${ta[@]}" & run "$TMP/m2" "$TMP/m2.prog" "${pa[@]}" & }
read -r -a wa <<< "$WEAVE"; read -r -a ia <<< "$ILI"
[ -f "$TMP/m6" ] && { run "$TMP/m6" "$TMP/m6.weave" "${wa[@]}" & }
[ -f "$TMP/m7" ] && { runref "$TMP/m7" "$TMP/m7.pref" "${pa[@]}" & runref "$TMP/m7" "$TMP/m7.iref" "${ia[@]}" &
                      run "$TMP/m7" "$TMP/m7.prog" "${pa[@]}" & }
wait
for k in 1 3 5; do
    [ -f "$TMP/m$k.prog" ] || continue
    # 1 = pixels differ; 2 = the mutant rendered no comparable frames (the baseline arm is
    # proven non-vacuous in [1]), i.e. the picture died -- M1's usual form: popping words
    # that were never requested stalls resample_dta for good.
    same "$TMP/prog.base" "$TMP/m$k.prog"; r=$?
    case $r in
        1) echo "  ok   M$k pixels differ (caught)" ;;
        2) echo "  ok   M$k no picture ($(grep -c '^PIXSUM' "$TMP/m$k.prog") frames: the display stalled -- caught)" ;;
        3) echo "  FAIL M$k TIMEOUT (inconclusive)"; rc=1 ;;
        *) echo "  FAIL M$k survived (pixels identical)"; rc=1 ;;
    esac
done
if [ -f "$TMP/m2.tog" ]; then
    same "$TMP/croptog.base" "$TMP/m2.tog"; r1=$?
    same "$TMP/prog.base" "$TMP/m2.prog"; r2=$?
    if [ $r1 = 1 ] && [ $r2 = 0 ]; then echo "  ok   M2 caught by +croptog only (prog still bit-identical)"
    else echo "  FAIL M2: croptog same=$r1 (want 1 differ), prog same=$r2 (want 0 identical)"; rc=1; fi
fi
if [ -f "$TMP/m4.prog" ]; then
    same "$TMP/prog.base" "$TMP/m4.prog"; r=$?
    w=$(grep '^RQS' "$TMP/m4.prog" | awk '{split($3,w,"="); split($4,m,"="); print w[2] "/" m[2]}' | sort -u | tr '\n' ' ')
    if [ $r = 0 ] && [ "$w" != "3088/1024 " ]; then echo "  ok   M4 pixels identical, words $w (caught by [2] only)"
    else echo "  FAIL M4: same=$r words '$w'"; rc=1; fi
fi

if [ -f "$TMP/m6.weave" ]; then
    same "$TMP/weave.base" "$TMP/m6.weave"; r=$?
    w=$(grep '^RQS' "$TMP/m6.weave" | awk '{split($3,w,"="); split($4,m,"="); print w[2] "/" m[2]}' | sort -u | tr '\n' ' ')
    if [ $r = 0 ] && [ "$w" != "3088/1024 " ]; then echo "  ok   M6 pixels identical, weave words $w (caught by [2] only)"
    else echo "  FAIL M6: same=$r weave words '$w'"; rc=1; fi
fi
if [ -f "$TMP/m7.pref" ]; then
    # [1] cannot see M7 (it compares two builds of the same rows): the mutant still counts
    # 3088 words on prog, and only [4] fails -- on both reference arms.
    vp=$(refok "$TMP/m7.pref"); rp=$?; vi=$(refok "$TMP/m7.iref"); ri=$?
    w=$(grep '^RQS' "$TMP/m7.prog" | awk '{split($3,w,"="); split($4,m,"="); print w[2] "/" m[2]}' | sort -u | tr '\n' ' ')
    if [ $rp = 1 ] && [ $ri = 1 ] && [ "$w" = "3088/1024 " ]; then echo "  ok   M7 caught by [4] only (prog: $vp; il_i: $vi)"
    else echo "  FAIL M7: prog ref=$rp ($vp), il_i ref=$ri ($vi), prog words '$w'"; rc=1; fi
fi

[ $rc = 0 ] && echo "run_chroma_reuse: ALL GREEN" || echo "run_chroma_reuse: FAILURES"
exit $rc
