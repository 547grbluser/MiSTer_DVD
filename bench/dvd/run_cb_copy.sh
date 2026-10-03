#!/usr/bin/env bash
# run_cb_copy.sh -- the DTS codebooks: the copy out of the three host FIFOs into DDR3 and
# the engine's fetch (dvd/dts/dts_cb_mem.sv, bench/dvd/cb_copy_tb.sv; docs/dts_decoder.md
# D4, P2).
# GREEN:
#   G1  the copy byte for byte, 2,000 random fetches, no second copy after a reset
#   G2  the same with DDR3 stalling 70 % of cycles and up to 20 cycles of read latency
#   C1  a host carrying the wrong image (lpcm's and mp2's swapped): tables_ok stays LOW
# --red: mutations on a COPY of the RTL, each caught by its own arm.
set -u
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
GEN=.sim/cb_copy
mkdir -p "$GEN"
fail=0
red=0; [ "${1:-}" = "--red" ] && red=1
echo "== GREEN: the host images and the checksum are current =="
python3 tools/dts_isa.py --asm --check | sed 's/^/  /' || fail=1
python3 tools/dts_golden.py --codebooks "$GEN/cb" > /dev/null || { echo "FAIL codebooks"; exit 1; }
SRC="dvd/audio_ring.sv dvd/lpcm_unpack.sv dvd/mp2/mp2_decode.sv dvd/ac3/bit_fifo.sv dvd/ac3/bit_reader.sv dvd/dts/dts_cb_mem.sv"
TB=bench/dvd/cb_copy_tb.sv
build() {   # dir tb -> sim
  (cd "$1" && iverilog -g2012 -I dvd/ac3 -o sim $SRC "$2" 2>build.log) || { cat "$1/build.log"; return 1; }
}
run() {     # dir args... -> log
  local d=$1; shift
  (cd "$d" && timeout 1200 vvp -n sim +cb="$ROOT/$GEN/cb" "$@" 2>&1 | grep -v 'readmem\|Not enough')
}
mkdir -p "$GEN/w/dvd/dts" "$GEN/w/dvd/ac3" "$GEN/w/dvd/mp2" "$GEN/w/bench/dvd"
stage() {   # dir: a private copy of the sources (mutations edit it)
  rm -rf "$1"; mkdir -p "$1/dvd/dts" "$1/dvd/ac3" "$1/dvd/mp2" "$1/bench/dvd"
  cp dvd/audio_ring.sv dvd/lpcm_unpack.sv "$1/dvd/"; cp dvd/mp2/mp2_decode.sv "$1/dvd/mp2/"
  cp dvd/ac3/*.sv dvd/ac3/*.svh "$1/dvd/ac3/"; cp dvd/dts/dts_cb_mem.sv dvd/dts/dts_cb.svh dvd/dts/cb_host_*.mem "$1/dvd/dts/"
  cp $TB "$1/bench/dvd/"
}
stage "$GEN/w"; build "$GEN/w" $TB || exit 1
# C1's bench: the lpcm and mp2 host images swapped
stage "$GEN/c1"
sed -i 's#INIT_FILE("dvd/dts/cb_host_lpcm.mem")#INIT_FILE("dvd/dts/cb_host_MP2X.mem")#; s#INIT_FILE("dvd/dts/cb_host_mp2.mem")#INIT_FILE("dvd/dts/cb_host_lpcm.mem")#; s#cb_host_MP2X#cb_host_mp2#' "$GEN/c1/$TB"
build "$GEN/c1" $TB || exit 1
echo "== GREEN =="
for a in "G1|w|" "G2|w|+busy=70 +lat=20" "C1|c1|+expect_bad"; do
  IFS='|' read -r name d args <<<"$a"
  # shellcheck disable=SC2086
  run "$GEN/$d" $args > "$GEN/$name.log"
  if grep -q "^PASS: cb_copy_tb" "$GEN/$name.log"; then
    echo "  PASS $name: $(grep '^cb_copy_tb:' "$GEN/$name.log" | head -1 | sed 's/^cb_copy_tb: //')"
  else echo "  FAIL $name: $(grep -m1 FAIL "$GEN/$name.log")"; fail=1; fi
done

if [ $red = 1 ]; then
  echo "== MUTATIONS (each must be caught by its own arm) =="
  mut() {   # name arm-dir arm-args file old new pattern
    local name=$1 ad=$2 args=$3 file=$4 old=$5 new=$6 pat=$7 d="$GEN/m_$1"
    stage "$d"
    [ "$ad" = c1 ] && cp "$GEN/c1/$TB" "$d/$TB"
    python3 - "$d/$file" "$old" "$new" <<'PYEOF' || { echo "  $name: anchor not found"; fail=1; return; }
import sys
p, o, n = sys.argv[1:4]
s = open(p).read()
assert s.count(o) == 1, o
open(p, 'w').write(s.replace(o, n))
PYEOF
    build "$d" $TB > /dev/null || { echo "  $name: build failed"; fail=1; return; }
    # shellcheck disable=SC2086
    run "$d" $args > "$d/log"
    if grep -q "^PASS: cb_copy_tb" "$d/log"; then echo "  $name: SURVIVED"; fail=1
    elif grep -qE "FAIL $pat" "$d/log"; then echo "  $name: caught by $(grep -m1 FAIL "$d/log" | sed 's/.*FAIL/FAIL/' | cut -c1-100)"
    else echo "  $name: failed outside $pat: $(grep -m1 FAIL "$d/log")"; fail=1; fi
  }
  CB=dvd/dts/dts_cb_mem.sv
  mut M1 w "" $CB "        if (!rst_n) begin
            ddr_read <= 1'b0; rd_pend <= 1'b0;" "        if (!rst_n) begin
            copied <= 1'b0; cst <= C_STEP; phase <= 2'd0; row <= 12'd0; first <= 1'b1; s1 <= 32'd0; s2 <= 32'd0;
            ddr_read <= 1'b0; rd_pend <= 1'b0;" "\[once\]" &
  mut M2 w "" $CB "if (!copied && cst == C_STEP && !first)" "if (!copied && cst == C_STEP)" "\[copy\]" &
  mut M3 w "" dvd/mp2/mp2_decode.sv "if (pcm_pop || cp_step) pcm_rp" "if (pcm_pop) pcm_rp" "\[copy\]" &
  mut M4 c1 "+expect_bad" $CB "tables_ok <= ((s2 ^ s1) == CB_SUM);" "tables_ok <= 1'b1;" "\[copy\]" &
  wait
  mut M5 w "" $CB "if (phase == 2'd2) acc <= {cp_ring_q, acc[63:8]};" "if (phase == 2'd2) acc <= {acc[55:0], cp_ring_q};" "\[copy\]" &
  mut M6 w "" $CB "if (!copied && !hosts_ready) begin" "if (1'b0) begin" "\[copy\]" &
  mut M7 w "" $CB "ddr_addr <= CB_BASE + {16'd0, cb_sel, cb_addr};" "ddr_addr <= CB_BASE + {16'd0, 1'b0, cb_addr};" "\[fetch\]" &
  mut M8 w "" dvd/lpcm_unpack.sv "                if (cp_step) rptr <= rptr + 1'b1;" "" "\[copy\]" &
  wait
fi
[ $fail = 0 ] && echo "== ALL GREEN ==" || echo "== FAILURES =="
exit $fail
