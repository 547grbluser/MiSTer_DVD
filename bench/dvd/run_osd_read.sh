#!/usr/bin/env bash
# run_osd_read.sh -- gate for F1: the display no longer reads the dead OSD words
# (docs/decode_pacing.md §7).
#
# WHAT IS PROVEN
#   The fork ties the upstream OSD layer off (mpeg2video.v dot_osd_enable = 1'b0), yet
#   resample_addrgen opened every macroblock-line with two read requests for the OSD
#   frame: 2 of 8 words, a quarter of all display reads. F1 (OSD_READS = 0) skips them
#   in the address generator and in resample_dta together.
#   [1] BIT-EXACT: the full display chain (resample -> vscale -> hstretch -> pixel_queue
#       -> mixer, bench/dvd/resample_chain_tb.sv) is built twice, OSD_READS = 1 (the
#       original structure) and 0 (F1). The memory answers every read with a hash of its
#       OWN address (+addrhash=1), so a dropped, extra, reordered or misaligned word
#       changes the pixels; the per-frame order-sensitive checksum of every displayed
#       Y/U/V pixel must match line for line, across progressive, interlaced, full-width
#       and bursty-stall geometries.
#   [2] THE SAVING IS REAL: display read words per macroblock-line are 8 in the baseline
#       and exactly 6 with F1.
#   [3] The seams (FIRST_RQ duties, one shared parameter) -- tools/check_osd_read_wiring.py.
#   Mutations: each must fail its own arm.
#     M1 resample_dta still reads an OSD word while addrgen skips it (the halves disagree)
#     M2 the position code still keyed on the OSD state (never visited under F1)
#     M3 the address generator still requests the OSD words (no saving)
#
# Usage: bench/dvd/run_osd_read.sh          (exit 0 = ALL GREEN)
set -uo pipefail
cd "$(dirname "$0")/../.."
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
rc=0

SRC=(rtl/mpeg2/resample.v dvd/resample_addrgen.v rtl/mpeg2/resample_dta.v
     rtl/mpeg2/resample_bilinear.v rtl/mpeg2/mem_addr.v rtl/mpeg2/mixer.v
     rtl/mpeg2/pixel_queue.v rtl/mpeg2/syncgen.v rtl/mpeg2/read_write.v
     rtl/mpeg2/wrappers.v rtl/mpeg2/fwft.v rtl/mpeg2/xilinx_fifo_dc.v rtl/mpeg2/xfifo_sc.v
     dvd/disp_hstretch.sv dvd/disp_vscale.sv bench/dvd/resample_chain_tb.sv)

build() {   # <out> <OSDR> [file substitutions: orig=mutated ...]
    local out=$1 osdr=$2; shift 2
    local files=("${SRC[@]}")
    for m in "$@"; do
        local o=${m%%=*} n=${m#*=}
        for i in "${!files[@]}"; do [ "${files[$i]}" = "$o" ] && files[$i]=$n; done
    done
    # -DCHR=0: F1 is measured on the F1 structure. F2's chroma reuse (on by default)
    # requires OSD_READS = 0, so the OSD_READS = 1 baseline cannot be built with it;
    # bench/dvd/run_chroma_reuse.sh gates F2 against this CHR=0 structure.
    iverilog -g2012 -D__IVERILOG__ -DOSDR="$osdr" -DCHR=0 -I rtl/mpeg2 -o "$out" "${files[@]}" 2>"$out.log"
}

# geometries: name + plusargs. Each must render real frames in both arms.
MODES=("prog:+frames=4"
       "ilace:+il=1 +frames=4"
       "wide:+wide=1 +frames=3"
       "stall:+stallon=40 +stalloff=200 +frames=4")

sums() {   # <sim> <plusargs...> -> PIXSUM lines (first frame dropped: it may start mid-scan)
    vvp "$1" +addrhash=1 "${@:2}" 2>&1 | grep '^PIXSUM' | tail -n +2
}
words_per_mbl() {  # <sim> <plusargs...> -> words/mbline of the last reported frame
    vvp "$1" +addrhash=1 "${@:2}" 2>&1 | grep '^RQW' | tail -1 |
        awk '{split($3,w,"="); split($4,m,"="); if (m[2]>0) printf "%.3f", w[2]/m[2]; else print "nan"}'
}

echo "== build: baseline (OSD_READS=1) and F1 (OSD_READS=0) =="
build "$TMP/base" 1 || { echo "  FAIL baseline build"; cat "$TMP/base.log"; exit 1; }
build "$TMP/f1"   0 || { echo "  FAIL F1 build"; cat "$TMP/f1.log"; exit 1; }

echo "== [1] bit-exact pixels, every geometry =="
for m in "${MODES[@]}"; do
    name=${m%%:*}; args=${m#*:}
    # shellcheck disable=SC2086
    a=$(sums "$TMP/base" $args); b=$(sums "$TMP/f1" $args)
    n=$(printf '%s\n' "$a" | grep -c PIXSUM)
    if [ "$n" -lt 2 ]; then echo "  FAIL [$name] baseline rendered only $n frames -- the arm is vacuous"; rc=1
    elif [ "$a" != "$b" ]; then echo "  FAIL [$name] pixels differ"; diff <(echo "$a") <(echo "$b") | head -6; rc=1
    else echo "  ok   [$name] $n frames bit-identical ($(printf '%s\n' "$a" | tail -1 | awk '{print $4}') px/frame)"; fi
done

echo "== [2] read words per macroblock-line: 8 -> 6 =="
wb=$(words_per_mbl "$TMP/base" +frames=4); wf=$(words_per_mbl "$TMP/f1" +frames=4)
if [ "$wb" = "8.000" ] && [ "$wf" = "6.000" ]; then echo "  ok   baseline $wb, F1 $wf"
else echo "  FAIL baseline $wb (want 8.000), F1 $wf (want 6.000)"; rc=1; fi

echo "== [3] seams =="
python3 tools/check_osd_read_wiring.py >/dev/null 2>&1 && echo "  ok   check_osd_read_wiring" \
    || { echo "  FAIL check_osd_read_wiring"; python3 tools/check_osd_read_wiring.py | grep FAIL; rc=1; }

echo "== mutations (each must FAIL its arm) =="
mutate() {   # <src> <dst> <old> <new>
    python3 - "$@" <<'PYEOF'
import sys
src, dst, old, new = sys.argv[1:5]
s = open(src).read()
assert s.count(old) == 1, f'anchor not unique in {src}: {old!r} x{s.count(old)}'
open(dst, 'w').write(s.replace(old, new))
PYEOF
}
# M1: resample_dta keeps reading an OSD word while addrgen skips it
if mutate rtl/mpeg2/resample_dta.v "$TMP/m1_dta.v" \
     "localparam [3:0] FIRST_RD = OSD_READS ? STATE_RD_OSD : STATE_RD_Y;" \
     "localparam [3:0] FIRST_RD = STATE_RD_OSD;"; then
    build "$TMP/m1" 0 "rtl/mpeg2/resample_dta.v=$TMP/m1_dta.v"
    a=$(sums "$TMP/base" +frames=4); b=$(timeout 300 vvp "$TMP/m1" +addrhash=1 +frames=4 2>&1 | grep '^PIXSUM' | tail -n +2)
    [ "$a" != "$b" ] && echo "  ok   M1 dta/addrgen disagree -> pixels differ (caught)" \
                     || { echo "  FAIL M1 survived"; rc=1; }
else echo "  FAIL M1 anchor missing -- arm void"; rc=1; fi
# M2: the position code still keyed on the OSD state
if mutate dvd/resample_addrgen.v "$TMP/m2_ag.v" \
     "else if (clk_en) resample_wr_en <= (state == FIRST_RQ);" \
     "else if (clk_en) resample_wr_en <= (state == STATE_WR_OSD_MSB);"; then
    build "$TMP/m2" 0 "dvd/resample_addrgen.v=$TMP/m2_ag.v"
    a=$(sums "$TMP/base" +frames=4); b=$(timeout 300 vvp "$TMP/m2" +addrhash=1 +frames=4 2>&1 | grep '^PIXSUM' | tail -n +2)
    [ "$a" != "$b" ] && echo "  ok   M2 position code on the OSD state -> pixels differ (caught)" \
                     || { echo "  FAIL M2 survived"; rc=1; }
else echo "  FAIL M2 anchor missing -- arm void"; rc=1; fi
# M3: the address generator still requests the OSD words (and dta still reads them):
#     pixels stay identical, so ONLY the word count can see it -- and must.
if mutate dvd/resample_addrgen.v "$TMP/m3_ag.v" \
     "localparam [3:0] FIRST_RQ = OSD_READS ? STATE_WR_OSD_MSB : STATE_WR_Y_MSB;" \
     "localparam [3:0] FIRST_RQ = STATE_WR_OSD_MSB;" &&
   mutate rtl/mpeg2/resample_dta.v "$TMP/m3_dta.v" \
     "localparam [3:0] FIRST_RD = OSD_READS ? STATE_RD_OSD : STATE_RD_Y;" \
     "localparam [3:0] FIRST_RD = STATE_RD_OSD;"; then
    build "$TMP/m3" 0 "dvd/resample_addrgen.v=$TMP/m3_ag.v" "rtl/mpeg2/resample_dta.v=$TMP/m3_dta.v"
    w=$(words_per_mbl "$TMP/m3" +frames=4)
    [ "$w" != "6.000" ] && echo "  ok   M3 OSD words still requested -> $w words/MB-line (caught)" \
                        || { echo "  FAIL M3 survived (6.000)"; rc=1; }
else echo "  FAIL M3 anchor missing -- arm void"; rc=1; fi

[ $rc = 0 ] && echo "run_osd_read: ALL GREEN" || echo "run_osd_read: FAILURES"
exit $rc
