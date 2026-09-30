#!/usr/bin/env bash
# run_seek_rf_pts.sh -- a seek must not hand the audio gate the OLD position's PTS
# (docs/dvd_nav.md §2h "The stale audio PTS"). Real front half of the audio path:
#   ps_demux -> ac3/dts/mp2_reframer -> audio_ring, flush_ctl turning seek_ack into
#   emu's reset pulses, reframers on rf_rst_n. Synthetic packs (no fixture): stream A
#   ends N bytes past a frame sync (N = 0..8, 100, 767), the seek flushes, stream B
#   (every PTS 20 s earlier) lands. Plus tools/check_rf_flush_wiring.py on dvd/emu.sv.
#
#   --red   every claim's mutation must fail ITS arm:
#           PRE     emu's pre-fix wiring (reframers on core reset + track switch)
#                   -> [2]/[3] stale PTS for N = 2..6 (MiB authors N = 3)
#           RESYNC  the clear keyed to aud_resync           -> [2]/[3]
#           NOB     no landing stream                        -> [4] vacuity only
#           W1..W5  emu.sv mutations the wiring check must refuse
set -u
cd "$(dirname "$0")/../.."
RED=0; [ "${1:-}" = "--red" ] && RED=1
D=.sim/seek_rf_pts; mkdir -p "$D"
fail=0

iverilog -g2012 -D__IVERILOG__ -I rtl/mpeg2 -o "$D/sim" \
    dvd/ps_demux.sv dvd/ac3_reframer.sv dvd/dts_reframer.sv dvd/mp2_reframer.sv \
    dvd/flush_ctl.sv dvd/audio_ring.sv bench/dvd/seek_rf_pts_tb.sv 2>&1 | grep -v sorry
[ -x "$D/sim" ] || { echo "  FAIL compile"; exit 1; }

out=$(vvp "$D/sim" 2>&1)
if echo "$out" | grep -q "SEEK_RF_PTS: PASS"; then
    echo "  PASS seek_rf_pts: every landing's first frame and PTS are the landing's (N = 0..8, 100, 767)"
else
    echo "$out" | grep -E '^\s+\[|N=' ; echo "  FAIL seek_rf_pts"; fail=1
fi

if python3 tools/check_rf_flush_wiring.py; then :; else fail=1; fi

if [ "$RED" -eq 1 ]; then
    # arm | plusarg | the check tags that MUST appear | tags that must NOT appear
    red() {
        local name=$1 arg=$2 want=$3 forbid=$4 o
        o=$(vvp "$D/sim" "$arg" 2>&1)
        if echo "$o" | grep -q "SEEK_RF_PTS: PASS"; then
            echo "  FAIL RED $name: passed"; fail=1; return
        fi
        local w
        for w in $want; do
            echo "$o" | grep -qF "$w" || { echo "  FAIL RED $name: did not fail $w"; fail=1; return; }
        done
        for w in $forbid; do
            echo "$o" | grep -qF "$w" && { echo "  FAIL RED $name: also failed $w (not its arm)"; fail=1; return; }
        done
        echo "  PASS RED $name fails exactly: $want"
    }
    red PRE    +PRE    "[2] [3]" "[4]"
    red RESYNC +RESYNC "[2] [3]" "[4]"
    red NOB    +NOB    "[4]"     "[1] [2] [3]"
    # PRE must hit the authored MiB case specifically
    vvp "$D/sim" +PRE 2>&1 | grep -q "N=3: .*BAD" \
        && echo "  PASS RED PRE includes N=3 (MiB's authored pack end)" \
        || { echo "  FAIL RED PRE: N=3 not caught"; fail=1; }

    # wiring mutations on a copy of emu.sv: name | python replace (old ||| new)
    wmut() {
        local name=$1 old=$2 new=$3 t; t=$(mktemp -d)
        python3 - "$t/emu.sv" "$old" "$new" <<'PY'
import sys
dst, old, new = sys.argv[1:4]
s = open('dvd/emu.sv').read()
if old not in s: sys.exit('anchor missed')
open(dst, 'w').write(s.replace(old, new, 1))
PY
        if [ $? -ne 0 ]; then echo "  FAIL wiring $name: anchor missed"; fail=1; rm -rf "$t"; return; fi
        if python3 tools/check_rf_flush_wiring.py "$t/emu.sv" >/dev/null; then
            echo "  FAIL wiring $name: accepted"; fail=1
        else
            echo "  PASS wiring $name refused: $(python3 tools/check_rf_flush_wiring.py "$t/emu.sv" | head -1 | cut -c1-110)"
        fi
        rm -rf "$t"
    }
    wmut W1-prefix   'wire rf_rst_n = reset_n & ~aud_realign_q & ~rf_flush_q;' \
                     'wire rf_rst_n = reset_n & ~aud_realign_q;'
    wmut W2-resync   'else          rf_flush_q <= aud_flush;' \
                     'else          rf_flush_q <= aud_resync;'
    wmut W3-raw      'wire rf_rst_n = reset_n & ~aud_realign_q & ~rf_flush_q;' \
                     'wire rf_rst_n = reset_n & ~aud_realign_q & ~aud_flush;'
    wmut W4-dts      '    .rst_n              (rf_rst_n),       // core reset + track switch + hard audio flush (see ac3_reframer)' \
                     '    .rst_n              (reset_n),'
    wmut W5-pipe     'wire rf_rst_n = reset_n & ~aud_realign_q & ~rf_flush_q;' \
                     'wire rf_rst_n = pipe_rst_n & ~aud_realign_q & ~rf_flush_q;'
fi

[ $fail -eq 0 ] && echo "== SEEK RF PTS: ALL GREEN ==" || echo "== SEEK RF PTS: FAILURES =="
exit $fail
