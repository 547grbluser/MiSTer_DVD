#!/usr/bin/env python3
"""
test_telem_unwrap.py -- gate for mister.py's reset-aware telemetry rates.

The defect this guards: `telem --watch` unwrapped every 16-bit counter modulo
65536, so a counter ZEROED mid-window (aud_play by every seek / jump / mode
switch / non-seamless re-anchor; pickups/lates/drops by a soft flush) read as a
forward jump of up to 65535 counts. On Thayer's Quest that printed "~67 kHz"
audio (docs/decode_pacing.md). The fix treats a delta the counter could not
physically reach in that interval as a reset and excludes the interval.

Arms:
  [1] reset in aud_play and in pickups/lates/drops -> the rates are the true
      ones and each reset is counted
  [2] a genuine 16-bit WRAP (no reset) is still unwrapped, not called a reset
  [3] duplicate rows (same t, as the 0.5 s poll of a 250 ms file produces) are
      harmless
  RED  the old plain unwrap (max_rate=None) must report the phantom rate --
       proves arm [1]'s scenario actually contains the trap.

Usage: python3 tools/test_telem_unwrap.py      (exit 0 = PASS)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mister  # noqa: E402

fails = 0


def check(name, ok, detail=''):
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        fails += 1


def rows_for(seconds, aud_reset_at=None, sync_reset_at=None, aud0=0, dup=False):
    """Synthetic telemetry: 59.94 Hz raster, 29.97 fps, 48 kHz, 2 lates/s."""
    rows, t = [], 100.0
    aud_base, sync_base = 0.0, 0.0
    for i in range(int(seconds / 0.5) + 1):
        el = i * 0.5
        if aud_reset_at is not None and el >= aud_reset_at and aud_base == 0.0:
            aud_base = el        # counter zeroed at this instant
        if sync_reset_at is not None and el >= sync_reset_at and sync_base == 0.0:
            sync_base = el
        a_el = el - aud_base
        s_el = el - sync_base
        r = {'t': t + el,
             'refreshes': int(el * 60000 / 1001) & 0xFFFF,
             'pickups': int(s_el * 30000 / 1001) & 0xFFFF,
             'lates': int(s_el * 2) & 0xFFFF,
             'drops': int(s_el * 2) & 0xFFFF,
             'aud_play': (aud0 + int(a_el * 3000)) & 0xFFFF,
             'aud_gate': 0,
             'vid_err': 0,
             'flags': {'video_live': 1}}
        rows.append(r)
        if dup:
            rows.append(dict(r))
    return rows


def near(v, want, tol):
    return abs(v - want) / want < tol


print('== [1] mid-window resets are excluded, not unwrapped ==')
rows = rows_for(30, aud_reset_at=10.0, sync_reset_at=20.0, aud0=20000)
s = mister.telem_summary(rows)
check('audio ~48 kHz', near(s['audio_hz'], 48000, 0.01), f"{s['audio_hz']:.0f}")
check('content ~29.97 fps', near(s['content_fps'], 29.97, 0.01), f"{s['content_fps']:.3f}")
check('raster ~59.94 Hz', near(s['raster_hz'], 59.94, 0.005), f"{s['raster_hz']:.3f}")
check('lates ~2/s', near(s['lates_per_s'], 2.0, 0.05), f"{s['lates_per_s']:.2f}")
check('aud_play reset counted once', s['resets']['aud_play'] == 1, str(s['resets']))
check('pickups reset counted once', s['resets']['pickups'] == 1)
check('refreshes never reset', s['resets']['refreshes'] == 0)

print('== [2] a genuine 16-bit wrap is still unwrapped ==')
rows = rows_for(30, aud0=65536 - 3000 * 5)    # wraps ~5 s in
s = mister.telem_summary(rows)
check('audio ~48 kHz across the wrap', near(s['audio_hz'], 48000, 0.01), f"{s['audio_hz']:.0f}")
check('no reset reported', s['resets']['aud_play'] == 0, str(s['resets']))

print('== [3] duplicate rows are harmless ==')
s = mister.telem_summary(rows_for(30, aud_reset_at=10.0, aud0=20000, dup=True))
check('audio ~48 kHz with duplicates', near(s['audio_hz'], 48000, 0.01), f"{s['audio_hz']:.0f}")
check('reset still counted once', s['resets']['aud_play'] == 1, str(s['resets']))

print('== RED: the old plain unwrap reports the phantom rate ==')
rows = rows_for(30, aud_reset_at=10.0, aud0=20000)
n, sp, _ = mister.telem_count(rows, 'aud_play', None)
old_hz = n * 16 / (rows[-1]['t'] - rows[0]['t'])
check('old unwrap is wrong by > 10 %', old_hz > 48000 * 1.10, f'{old_hz:.0f} Hz')

print('RESULT:', 'PASS' if fails == 0 else f'FAIL ({fails})')
sys.exit(1 if fails else 0)
