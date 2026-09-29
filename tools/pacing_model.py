#!/usr/bin/env python3
"""
pacing_model.py -- the display's pickup discipline, Progressive vs Interlaced,
against a decoder of known per-picture cost (docs/decode_pacing.md).

A model of three mechanisms, each taken from the RTL:

  * ONE-DEEP PICTURE BUFFER. motcomp_picbuf parks the VLD at the next picture
    header until the display picks up the finished picture (STATE_WAIT_0 /
    STATE_IP_FRAME_0). So decoding picture n+1 starts at the PICKUP of n.
  * THE DUE TEST. disp_sched: a picture is due when stc - want >= -half_scan,
    want = its PTS (the timeline never absorbs lateness -- "lateness is never
    answered by moving the clock").
  * WHEN THE TEST RUNS. resample_addrgen decides in STATE_REPEAT, after the
    image list is scanned: every refresh on Progressive (one FRAME image), every
    field PAIR on Interlaced (TOP,BOTTOM). A miss on the pair path re-scans the
    pair and counts 2 (late_ext).
  * THE LEDGER. frame_drop_ctl: +1 debt per late, a B-drop at debt >= 2 debits
    the dropped picture's cost; a dropped picture costs the decoder ~nothing
    and advances the timeline by its duration.

Output: lates/s, drops/s, displayed fps, and the decoder's parked fraction for
each discipline, over a sweep of mean decode cost.

Usage: tools/pacing_model.py [--seconds 60] [--jitter 0.25] [--seed 1]
       tools/pacing_model.py selftest
"""
import argparse
import random
import sys

R = 1001 / 60000.0          # refresh period, 59.94 Hz
F = 2 * R                   # 29.97 content: 2 refreshes per picture
GOP = 'IBBPBBPBBPBBPBB'     # display-order types (only B is droppable)


def simulate(mode, mean_ms, jitter, seconds, seed=1, drop_threshold=2):
    """mode 'prog' | 'ilace'. Returns dict of rates."""
    rng = random.Random(seed)
    cost = lambda: max(0.002, rng.gauss(mean_ms / 1000.0, jitter * mean_ms / 1000.0))
    n = 0                       # index of the picture being decoded next
    pts = lambda i: i * F       # nominal presentation times
    t_ready = cost()            # picture 0 decoded at this time (started at 0)
    shown = -1                  # index on screen
    lates = drops = pickups = 0
    parked = 0.0
    debt = 0
    k = 0
    step = 1 if mode == 'prog' else 2
    t_end = seconds
    t = 0.0
    while t < t_end:
        t = k * R
        nxt = shown + 1
        due = t >= pts(nxt) - R / 2
        if due or shown < 0:
            if t_ready <= t:
                # pickup: decoder was parked from t_ready to t
                parked += t - t_ready
                shown = nxt
                pickups += 1
                # decoder starts the next picture now; drop it if debt allows and it is a B
                nn = shown + 1
                while debt >= drop_threshold and GOP[nn % len(GOP)] == 'B':
                    debt -= 2
                    drops += 1
                    shown = nn          # the timeline advances past the dropped picture
                    nn += 1
                t_ready = t + cost()
            else:
                cnt = 1 if mode == 'prog' else 2
                lates += cnt
                debt = min(debt + cnt, 15)
        k += step
    return {'lates': lates / seconds, 'drops': drops / seconds,
            'fps': pickups / seconds, 'parked': parked / seconds}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', nargs='?', default='sweep')
    ap.add_argument('--seconds', type=float, default=120)
    ap.add_argument('--jitter', type=float, default=0.25)
    ap.add_argument('--seed', type=int, default=1)
    a = ap.parse_args()
    if a.cmd == 'selftest':
        fast = simulate('prog', 10, 0.1, 60)
        slow = simulate('ilace', 45, 0.05, 60)
        # a fast decoder keeps up (one start-up late is allowed); a slow one cannot
        ok = fast['lates'] < 0.05 and fast['fps'] > 29.5 and slow['lates'] > 5
        print('selftest', 'RESULT: PASS' if ok else 'RESULT: FAIL', fast, slow)
        return 0 if ok else 1
    print(f'jitter {a.jitter:.2f} (sigma/mean), {a.seconds:.0f} s, 29.97 content on 59.94 Hz')
    print(f"{'mean ms':>8} | {'PROG late/s drop/s fps parked':>32} | {'ILACE late/s drop/s fps parked':>32}")
    for m in (14, 18, 22, 25, 28, 30, 32, 33, 34, 36):
        p = simulate('prog', m, a.jitter, a.seconds, a.seed)
        i = simulate('ilace', m, a.jitter, a.seconds, a.seed)
        print(f"{m:8d} | {p['lates']:8.2f} {p['drops']:6.2f} {p['fps']:6.2f} {p['parked']:6.2f}   "
              f"| {i['lates']:8.2f} {i['drops']:6.2f} {i['fps']:6.2f} {i['parked']:6.2f}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
