#!/usr/bin/env bash
#
# run_dac_dither.sh — the analog DAC's ordered dither (dvd/dac_dither.sv,
# bench/dvd/dac_dither_tb.sv; docs/single_raster_analog.md §8).
#
#   bash bench/dvd/run_dac_dither.sh          the bench, then the wiring check
#   bash bench/dvd/run_dac_dither.sh --red    every mutation must FAIL its own arm
#
set -e
cd "$(dirname "$0")/../.."

SIM=bench/dvd/dac_dither_sim
SRC=dvd/dac_dither.sv
LOG=bench/dvd/dac_dither_red_arm.log

build() {   # $1 = module source
  iverilog -g2012 -o "$SIM" "$1" bench/dvd/dac_dither_tb.sv
}

# ---------------------------------------------------------------------------
# RED: sed-mutated copies of the module. Each must fail, and must fail the ARM that
# owns the claim it breaks (the tag is checked), so an arm that cannot fire shows up
# here rather than as a green bench.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--red" ]; then
  mut=bench/dvd/dac_dither_red.sv
  fails=0

  red_case() {   # $1 = label, $2 = the arm tag that must fail, $3 = sed program
    echo "== RED [$1] (expects $2) =="
    sed "$3" "$SRC" > "$mut"
    # A pattern that no longer matches leaves an UNMUTATED copy that passes: hard fail.
    if cmp -s "$mut" "$SRC"; then
      echo "  FAIL: the mutation did not apply — the pattern no longer matches $SRC"
      fails=$((fails+1)); return
    fi
    build "$mut"
    if vvp "$SIM" > "$LOG" 2>&1; then
      echo "  FAIL: the RED arm PASSED — the bench cannot detect this defect"
      fails=$((fails+1))
    elif ! grep -q "^FAIL \[$2\]" "$LOG"; then
      echo "  FAIL: the bench failed, but not on $2:"
      grep -E "^FAIL" "$LOG" | head -3 | sed 's/^/    /'
      fails=$((fails+1))
    else
      echo "  failed as required:"
      grep -E "^FAIL \[$2\]" "$LOG" | head -2 | sed 's/^/    /'
    fi
  }

  # T1: the threshold loses its low bit (t in {0, 2}): the mean is biased low.
  red_case "half: threshold 0/2 only" T1 \
           's/wire \[1:0\] t  = bf\[3:2\];/wire [1:0] t  = {bf[3], 1'"'"'b0};/'
  # T1: the row counter never advances: one Bayer row, thresholds 0 2 0 2.
  red_case "norow: the line never steps the row" T1 \
           's/if (hs \&\& !hs_q) row <= row + 2.d1;//'
  # T2: the threshold doubled (0..6): a multiple of 4 crosses a 6-bit code.
  red_case "wide: threshold 0..6" T2 \
           's/{1.b0, din\[23:16\]} + {7.d0, t}/{1'"'"'b0, din[23:16]} + {6'"'"'d0, t, 1'"'"'b0}/'
  # T3: no saturation: 254 + 3 wraps to 1, a black speck on white.
  red_case "wrap: no clamp at 255" T3 \
           's/s2\[8\] ? 8.hFF : s2\[7:0\]/s2[7:0]/'
  # T4: DE ignored: the dither lands on the caption level and the burst.
  red_case "blank: dithered outside DE" T4 \
           's/if (!en_s2 || !de) dout = din;/if (!en_s2) dout = din;/'
  # T5: the enable ignored: Off still dithers.
  red_case "noen: Off does not bypass" T5 \
           's/if (!en_s2 || !de) dout = din;/if (!de) dout = din;/'
  # T6: no field inversion: a static crosshatch on flat areas.
  red_case "static: the pattern never inverts" T6 \
           's/wire \[3:0\] bf = fld ? (4.d15 - b) : b;/wire [3:0] bf = b;/'

  # Wiring arms: tools/check_dac_dither_wiring.py on mutated copies of emu.sv / sys_top.v /
  # DVD.qsf (in a temp dir, never the tree). Each must fail with its own message.
  tmp=$(mktemp -d)
  wire_case() {   # $1 = label, $2 = file to mutate (emu|top|qsf), $3 = sed program, $4 = expected text
    echo "== RED wiring [$1] =="
    cp dvd/emu.sv "$tmp/emu.sv"; cp sys/sys_top.v "$tmp/sys_top.v"; cp DVD.qsf "$tmp/DVD.qsf"
    case "$2" in emu) f="$tmp/emu.sv"; o=dvd/emu.sv ;; top) f="$tmp/sys_top.v"; o=sys/sys_top.v ;; qsf) f="$tmp/DVD.qsf"; o=DVD.qsf ;; esac
    sed -i "$3" "$f"
    if cmp -s "$f" "$o"; then
      echo "  FAIL: the mutation did not apply to $o"; fails=$((fails+1)); return
    fi
    if python3 tools/check_dac_dither_wiring.py --emu "$tmp/emu.sv" --top "$tmp/sys_top.v" --qsf "$tmp/DVD.qsf" > "$LOG" 2>&1; then
      echo "  FAIL: the check PASSED a broken wiring"; fails=$((fails+1))
    elif ! grep -qF -- "$4" "$LOG"; then
      echo "  FAIL: the check failed, but not with \"$4\":"; sed 's/^/    /' "$LOG" | head -3; fails=$((fails+1))
    else
      echo "  failed as required:"; grep -F -- "$4" "$LOG" | head -1 | sed 's/^/    /'
    fi
  }
  wire_case "one channel's low bits left on vga_o" top 's/: vga_od\[9:8\];/: vga_o[9:8];/' "vga_g reads the undithered word"
  wire_case "a 6-bit pin left on vga_o"            top 's/: vga_od\[23:18\];/: vga_o[23:18];/' "VGA_R reads the undithered word"
  wire_case "dither fed before the yc_out mux"     top 's/\.din(vga_o),/.din(vga_o_t),/' ".din(vga_o_t), want .din(vga_o)"
  wire_case "emu instance port unconnected"        top 's/\.VGA_DITHER(vga_dither_en),/.VGA_DITHER(),/' "emu .VGA_DITHER"
  wire_case "enable from the wrong status bit"     emu 's/assign VGA_DITHER   = status\[8\];/assign VGA_DITHER   = status[9];/' "want status[8]"
  wire_case "status[8] shared with another row"    emu 's/"O\[10:9\],Video Output,/"O[10:8],Video Output,/' "claimed by 2 CONF_STR rows"
  wire_case "module missing from the qsf"          qsf '/dvd\/dac_dither\.sv/d' "does not name dvd/dac_dither.sv"
  rm -rf "$tmp"

  rm -f "$mut" "$LOG"
  if [ "$fails" -ne 0 ]; then echo "RED SUITE FAILED: $fails arm(s)"; exit 1; fi
  echo "RED suite OK: every mutation was caught by its own arm."
  exit 0
fi

# ---------------------------------------------------------------------------
# GREEN
# ---------------------------------------------------------------------------
build "$SRC"
vvp "$SIM" | tee "$LOG"
grep -q "^PASS dac_dither_tb" "$LOG" || { echo "dac_dither: no PASS marker"; exit 1; }
rm -f "$LOG"
python3 tools/check_dac_dither_wiring.py
echo "dac_dither: green."
