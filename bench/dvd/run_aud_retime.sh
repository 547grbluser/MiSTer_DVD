#!/usr/bin/env bash
# run_aud_retime.sh — the in-band timeline re-time in dvd/dvd_audio_decode.sv
# (docs/nonseamless_audio.md 4a), plus the regression suites it touches.
#
#   GREEN  bench/dvd/aud_retime_tb.sv       S1-S8 (join shapes, forward gap, control, liveness,
#                                           the T2 late head, a stale latch, a seamless restart, audio leading video)
#          bench/dvd/dvd_audio_decode_tb.sv the drain gate / catch-up / de-click contract
#          bench/dvd/flush_ctl_tb.sv        disc_rephase no longer resets audio
#   RED    (--red) each arm removes ONE step of the re-time and must fail exactly
#          the scenario that step exists for.
set -u
cd "$(dirname "$0")/../.."
fail=0
SRC="dvd/ac3/*.sv dvd/lpcm_unpack.sv dvd/mp2/mp2_decode.sv"
iv() { iverilog -g2012 -D__IVERILOG__ -I rtl/mpeg2 -I dvd/ac3 -o "$@" 2>&1 | grep -v "sorry:" ; }

green() {  # name pass-regex tb [dut-override]
    local name=$1 pat=$2 tb=$3 dut=${4:-dvd/dvd_audio_decode.sv}
    local d; d=$(mktemp -d)
    iv "$d/sim" $SRC "$dut" "$tb" > "$d/build"
    if [ -f "$d/sim" ] && vvp "$d/sim" > "$d/log" 2>&1 && grep -q "$pat" "$d/log"; then
        echo "  PASS $name"; grep -E '^\s+\[' "$d/log" | sed 's/^/      /'
    else
        echo "  FAIL $name"; tail -15 "$d/build" "$d/log" 2>/dev/null | sed 's/^/      /'; fail=1
    fi
    rm -rf "$d"
}

red() {    # name  expected-FAIL-regex  python-replacements (old|||new per line)
    local name=$1 pat=$2 subs=$3
    local d; d=$(mktemp -d)
    if ! python3 - "$d/dvd_audio_decode.sv" "$subs" <<'PYEOF'
import sys
s = open('dvd/dvd_audio_decode.sv').read()
for line in sys.argv[2].strip().split('\n'):
    old, new = line.split('|||')
    if old not in s:
        sys.exit("RED anchor moved: " + old)
    s = s.replace(old, new)
open(sys.argv[1], 'w').write(s)
PYEOF
    then echo "  FAIL $name: mutation did not apply"; fail=1; rm -rf "$d"; return; fi
    iv "$d/sim" $SRC "$d/dvd_audio_decode.sv" bench/dvd/aud_retime_tb.sv > "$d/build"
    if [ ! -f "$d/sim" ]; then
        echo "  FAIL $name: mutated module did not build -- the arm proves nothing"; fail=1
    else
        vvp "$d/sim" > "$d/log" 2>&1 || true
        if [ ! -s "$d/log" ]; then
            echo "  FAIL $name: no output -- not a verdict"; fail=1
        elif grep -q "PASS: aud_retime_tb" "$d/log"; then
            echo "  FAIL $name: the bench PASSED without this step"; fail=1
        elif grep -qE "$pat" "$d/log"; then
            echo "  PASS $name"; grep -E "^FAIL S" "$d/log" | head -3 | sed 's/^/      /'
        else
            echo "  FAIL $name: failed, but not on its own scenario"; grep "^FAIL" "$d/log" | head -3 | sed 's/^/      /'; fail=1
        fi
    fi
    rm -rf "$d"
}

echo "== GREEN =="
green aud_retime       "PASS: aud_retime_tb (S1-S10)" bench/dvd/aud_retime_tb.sv
green dvd_audio_decode "PASS: dvd_audio_decode"   bench/dvd/dvd_audio_decode_tb.sv
d=$(mktemp -d)
if iverilog -g2012 -o "$d/sim" dvd/flush_ctl.sv bench/dvd/flush_ctl_tb.sv && vvp "$d/sim" > "$d/log" 2>&1 \
   && grep -q "ALL TESTS PASSED" "$d/log"; then echo "  PASS flush_ctl"
else echo "  FAIL flush_ctl"; tail -8 "$d/log" | sed 's/^/      /'; fail=1; fi
rm -rf "$d"
# the emu.sv seam (no bench can see a wrong wire): decoder resync_req -> flush_ctl
if python3 tools/check_aud_rephase_wiring.py > /dev/null; then echo "  PASS check_aud_rephase_wiring"
else python3 tools/check_aud_rephase_wiring.py | sed 's/^/      /'; fail=1; fi

if [ "${1:-}" = "--red" ]; then
    # the wiring check must name both wrong wirings
    d=$(mktemp -d)
    sed 's/\.aud_rephase_req (aud_resync_req)/.aud_rephase_req (aud_disc_rephase)/' dvd/emu.sv > "$d/emu.sv"
    sed 's/else if (aud_switch || aud_rephase_req) aud_resync_cnt/else if (aud_switch) aud_resync_cnt/' dvd/flush_ctl.sv > "$d/fc.sv"
    if cmp -s "$d/emu.sv" dvd/emu.sv || cmp -s "$d/fc.sv" dvd/flush_ctl.sv; then
        echo "  FAIL wiring RED: a mutation did not apply (anchor moved)"; fail=1
    else
        python3 tools/check_aud_rephase_wiring.py "$d/emu.sv" | grep -q "DISPLAY pulse" \
            && echo "  PASS wiring RED (display pulse wired back in is named)" \
            || { echo "  FAIL wiring RED: the display-pulse wiring passed"; fail=1; }
        python3 tools/check_aud_rephase_wiring.py dvd/emu.sv "$d/fc.sv" | grep -q "does not include aud_rephase_req" \
            && echo "  PASS wiring RED (request dropped from aud_resync is named)" \
            || { echo "  FAIL wiring RED: a dropped request passed"; fail=1; }
        # the seamless stamp (step 7): tied off at the ring, or the decoder not reading it
        sed "s/\.aud_frame_seamless  (cell_seamless)/.aud_frame_seamless  (1'b0)/" dvd/emu.sv > "$d/e3.sv"
        sed "s/\.frame_seamless  (aud_frame_seamless_w)/.frame_seamless  (1'b0)/" dvd/emu.sv > "$d/e4.sv"
        if cmp -s "$d/e3.sv" dvd/emu.sv || cmp -s "$d/e4.sv" dvd/emu.sv; then
            echo "  FAIL wiring RED: a seamless mutation did not apply (anchor moved)"; fail=1
        else
            python3 tools/check_aud_rephase_wiring.py "$d/e3.sv" | grep -q "not the reader's cell_seamless" \
                && echo "  PASS wiring RED (seamless stamp tied off at the ring is named)" \
                || { echo "  FAIL wiring RED: a tied-off seamless stamp passed"; fail=1; }
            python3 tools/check_aud_rephase_wiring.py "$d/e4.sv" | grep -q "not the same named net" \
                && echo "  PASS wiring RED (decoder not reading the stamp is named)" \
                || { echo "  FAIL wiring RED: an unread seamless stamp passed"; fail=1; }
        fi
    fi
    rm -rf "$d"
    echo "== RED =="
    # 1. no detector: the new timeline plays straight on after the old tail, on the
    #    OLD clock (S2), and a forward-gap head plays early (S4)
    red detect-off "FAIL S2: new-timeline audio played while the clock was on the OLD" \
        "wire  head_disc = sched_en && last_pts_v|||wire  head_disc = 1'b0 && sched_en && last_pts_v"
    # 2. no hold: the head dispatches into the still-draining gate, sample-continuous
    red hold-off "FAIL S2: new-timeline audio played while the clock was on the OLD" \
        "wire  disc_hold = head_disc && (draining || play_pts_valid) && !(&hold_tmr);|||wire  disc_hold = 1'b0;"
    # 3. no timeline window: the re-armed head reads 'late' against the old clock and
    #    releases at once -- the Thayer E-state release (2b step 3)
    red window-off "FAIL S2: new-timeline audio played while the clock was on the OLD" \
        "(!retime || (start_delta < RETIME_WIN))|||1'b1"
    # 4. no overlap trim: the new clip resumes a whole overlap late
    red overlap-off "FAIL S3: new timeline resumed more than STALE late" \
        "assign head_retime_stale = head_disc|||assign head_retime_stale = 1'b0 && head_disc"
    # 6. the ULTIMATE_T2 regression (HW 2026-09-29): without the arrivals test a head
    #    late by >= RETIME_WIN on the clock's own timeline is held as "other
    #    timeline" and goes out 2.5 s late through the fallback
    red agree-off "FAIL S7: the new segment played LATE" \
        "((head_delta < RETIME_WIN) || arr_agree)|||(head_delta < RETIME_WIN)"
    # 7. no stale-latch request: a latched head that the clock left behind is never
    #    dropped
    red resync-off "FAIL S8: no resync_req for a stale latch" \
        "resync_req  <= 1'b1;|||resync_req  <= 1'b0;"
    # 8. The Matrix regression (HW 2026-09-29): ignore the seamless stamp and a
    #    white-rabbit restart is re-timed -- a gap, then the head 0.2 s late
    red seamless-off "FAIL S9: a SEAMLESS join was re-timed" \
        "frame_pts_valid && !frame_seamless &&|||frame_pts_valid &&"
    # 9. no wait for the clock: the head dispatches on the old clock, latches, and
    #    releases a whole audio-leads-video offset late (HW round 2 rework)
    red agree-hold-off "FAIL S10: the new clip played LATE" \
        "(draining || play_pts_valid || !arr_agree) && !(&hold_tmr)|||(draining || play_pts_valid) && !(&hold_tmr)"
    # 5. over-trigger: a detector that fires on ordinary forward steps must be caught
    #    by the continuous control
    red over-trigger "FAIL S5: a continuous stream triggered a re-time" \
        "(pts_step < -DISC_BACK_TICKS)|||(pts_step < DISC_BACK_TICKS)"
fi

[ $fail -eq 0 ] && echo "ALL GREEN" || echo "FAILURES"
exit $fail
