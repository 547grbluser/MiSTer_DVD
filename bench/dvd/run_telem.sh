#!/usr/bin/env bash
# Telemetry bridge + hardware-in-the-loop harness gates.
#
# vvp exits 0 even when a testbench reports failures, so every line below is
# gated on the summary string rather than on the exit code.
set -u
cd "$(dirname "$0")/../.."
fail=0

run_iv() {
    local name=$1; shift
    local log; log=$(mktemp)
    if iverilog -g2012 -o /tmp/${name}_sim "$@" 2>"$log" && vvp /tmp/${name}_sim >>"$log" 2>&1 \
       && grep -q "ALL GREEN" "$log"; then
        echo "  PASS $name"
    else
        echo "  FAIL $name"; tail -15 "$log"; fail=1
    fi
    rm -f "$log"
}

run_py() {
    local name=$1; shift
    if "$@" 2>&1 | grep -q "ALL GREEN"; then echo "  PASS $name"
    else echo "  FAIL $name"; "$@" 2>&1 | tail -12; fail=1; fi
}

echo "== RTL =="
run_iv dvd_telem dvd/dvd_telem.sv bench/dvd/dvd_telem_tb.sv
run_iv dec_duty  dvd/dec_duty.sv  bench/dvd/dec_duty_tb.sv

echo "== dec_duty wiring (docs/decode_pacing.md) =="
if python3 tools/check_decode_duty_wiring.py >/dev/null 2>&1; then echo "  PASS check_decode_duty_wiring"
else echo "  FAIL check_decode_duty_wiring"; python3 tools/check_decode_duty_wiring.py | grep FAIL; fail=1; fi

# Mutations: each must turn its own arm RED, or that arm cannot see the defect.
mut_tmp=$(mktemp -d)
mutate() {   # <src> <old> <new> <dst>: exactly-once textual replace
    python3 - "$@" <<'PYEOF'
import sys
src, old, new, dst = sys.argv[1:5]
s = open(src).read()
assert s.count(old) == 1, f'mutation anchor not unique in {src}: {old!r}'
open(dst, 'w').write(s.replace(old, new))
PYEOF
    [ $? = 0 ] || { echo "  FAIL mutation anchor missing -- the arm below is VOID"; fail=1; return 1; }
}
expect_red_iv() {   # <name> <files...>
    local name=$1; shift
    if iverilog -g2012 -o "$mut_tmp/sim" "$@" 2>/dev/null && vvp "$mut_tmp/sim" 2>&1 | grep -q "ALL GREEN"; then
        echo "  FAIL mutation $name survived (the arm cannot see it)"; fail=1
    else echo "  ok   mutation $name caught"; fi
}
echo "== mutations =="
mutate dvd/dec_duty.sv "wire is_starve = ~picbuf_busy & ~getbits_valid;" \
       "wire is_starve = ~getbits_valid;" "$mut_tmp/dd.sv"
expect_red_iv "M1 starve-ignores-disp" "$mut_tmp/dd.sv" bench/dvd/dec_duty_tb.sv
mutate dvd/dvd_telem.sv "assign src[17] = dec_disp;" "assign src[17] = dec_starve;" "$mut_tmp/tl.sv"
expect_red_iv "M2 word17-swapped" "$mut_tmp/tl.sv" bench/dvd/dvd_telem_tb.sv
mutate dvd/emu.sv ".dec_disp   (core_duty_disp)," ".dec_disp   (core_duty_starve)," "$mut_tmp/emu.sv"
if python3 tools/check_decode_duty_wiring.py --emu "$mut_tmp/emu.sv" >/dev/null 2>&1; then
    echo "  FAIL mutation M3 emu-duty-swap survived the wiring check"; fail=1
else echo "  ok   mutation M3 emu-duty-swap caught"; fi
mutate rtl/mpeg2/mpeg2video.v $'.rst(hard_rst),\n    .picbuf_busy(picbuf_busy_dbg)' \
       $'.rst(sync_rst),\n    .picbuf_busy(picbuf_busy_dbg)' "$mut_tmp/mv.v" && \
if python3 tools/check_decode_duty_wiring.py --mpeg "$mut_tmp/mv.v" >/dev/null 2>&1; then
    echo "  FAIL mutation M4 flush-resets-duty survived the wiring check"; fail=1
else echo "  ok   mutation M4 flush-resets-duty caught"; fi
# per picture (docs/decode_pacing.md §7 "Instrument")
mutate dvd/dec_duty.sv "else if (~picbuf_busy & getbits_valid & ~&c_pic)" \
       "else if (~picbuf_busy & ~&c_pic)" "$mut_tmp/dd5.sv"
expect_red_iv "M5 starved-cycles-count-as-decode" "$mut_tmp/dd5.sv" bench/dvd/dec_duty_tb.sv
mutate dvd/dec_duty.sv "if (c_pic > {5'd0, thr})" "if (c_pic >= {5'd0, thr})" "$mut_tmp/dd6.sv"
expect_red_iv "M6 exactly-one-period-counts-as-over" "$mut_tmp/dd6.sv" bench/dvd/dec_duty_tb.sv
mutate dvd/dvd_telem.sv "5'd22:   dout_r <= q22;" "5'd22:   dout_r <= q23;" "$mut_tmp/tl7.sv"
expect_red_iv "M7 word22-swapped" "$mut_tmp/tl7.sv" bench/dvd/dvd_telem_tb.sv
mutate dvd/emu.sv ".dec_pic_n    (core_pic_n)," ".dec_pic_n    (core_pic_over)," "$mut_tmp/emu8.sv"
if python3 tools/check_decode_duty_wiring.py --emu "$mut_tmp/emu8.sv" >/dev/null 2>&1; then
    echo "  FAIL mutation M8 emu-pic-swap survived the wiring check"; fail=1
else echo "  ok   mutation M8 emu-pic-swap caught"; fi
rm -rf "$mut_tmp"

echo "== host tools (no hardware) =="
run_py hud_read     python3 tools/hud_read.py selftest
run_py lipsync      python3 tools/lipsync_measure.py selftest
run_py dvd_explore  python3 tools/dvd_explore.py selftest
if python3 tools/test_telem_unwrap.py 2>&1 | grep -q "RESULT: PASS"; then echo "  PASS telem_unwrap"
else echo "  FAIL telem_unwrap"; python3 tools/test_telem_unwrap.py | tail -8; fail=1; fi

echo "== derived tables (CONF_STR / kbd_map) =="
if ./tools/tests/run_tests.sh 2>&1 | grep -q "ALL GREEN"; then
    echo "  PASS tools/tests"
else
    echo "  FAIL tools/tests"; ./tools/tests/run_tests.sh 2>&1 | tail -15; fail=1
fi

[ $fail = 0 ] && echo "run_telem: ALL GREEN" || echo "run_telem: FAILURES"
exit $fail
