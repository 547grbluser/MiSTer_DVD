#!/usr/bin/env python3
"""
pacing_matrix.py -- measure decode pacing across output modes on the rig.

One disc per invocation. Launches it, waits until the board says video is live,
then walks a list of output-mode CELLS, changing options LIVE (mister.py osd)
so every cell measures the same scene region, and runs a reset-aware
`telem --watch` window per cell. The Video Output cells are INTERLEAVED
(A/B/C/A/B/C...) so scene content cannot masquerade as a mode effect.

Every window writes its raw rows (.jsonl) and its summary (.json) under --out,
and the run ends with one table. A cell ASSERTS what it measured from
telemetry (sched_ps / sched_pf / sched_frc, flags.menu / still) -- the table
prints them beside the rates, so a window that landed on the wrong content is
visible rather than silently reported (docs/decode_pacing.md).

Usage:
  tools/pacing_matrix.py <image-on-target> --label NAME [--opt "Name=Value"]...
      [--rounds 2] [--window 30] [--settle 15] [--no-variants] [--out DIR]

  --opt      base options for the launch (e.g. "Disc Menus=Off",
             "Title VTS Units=1"); the cells override Video Output, Film 24p
             Out and Deinterlace on top.
  --variants the Progressive-only cells (Film 24p Off/On, Bob, Blend); on by
             default, --no-variants skips them.
  --no-launch  measure whatever is already playing (base options ignored).

Needs `mister.py deploy --agent` first (telemetry armed, osd FIFO present).
The rig is shared: announce the run, and `mister.py restore` afterwards.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MISTER = [sys.executable, os.path.join(HERE, 'mister.py')]

DEFAULTS = {'Video Output': 'Progressive', 'Film 24p Out': 'Auto',
            'Deinterlace': 'Weave', 'Frame Drop': 'On', 'A/V Sync': 'On',
            'Debug Overlay': 'Off'}

VO_CELLS = [('prog', {'Video Output': 'Progressive'}),
            ('ilace', {'Video Output': 'Interlaced'}),
            ('auto', {'Video Output': 'Auto'})]
VARIANT_CELLS = [('prog-film-off', {'Film 24p Out': 'Off'}),
                 ('prog-film-on', {'Film 24p Out': 'On'}),
                 ('prog-bob', {'Deinterlace': 'Bob'}),
                 ('prog-blend', {'Deinterlace': 'Blend'})]


def run(args, check=True, timeout=None):
    p = subprocess.run(MISTER + args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        sys.stderr.write(p.stdout + p.stderr)
        sys.exit(f'pacing_matrix: mister.py {args[0]} failed')
    return p.stdout


def snapshot():
    out = run(['telem'], check=False, timeout=60)
    for line in out.splitlines():
        if line.strip().startswith('{'):
            try:
                return json.loads(line)
            except ValueError:
                pass
    return None


def wait_live(timeout=90):
    """Board-side readiness: video_live, not a still, pickups advancing."""
    deadline = time.time() + timeout
    prev = None
    while time.time() < deadline:
        s = snapshot()
        if s and s['flags'].get('video_live') and not s['flags'].get('still'):
            if prev is not None and ((s['pickups'] - prev) & 0xFFFF) > 0:
                return True
            prev = s['pickups']
        else:
            prev = None
        time.sleep(1.5)
    return False


def apply(cur, want):
    for k, v in want.items():
        if cur.get(k) != v:
            run(['osd', f'{k}={v}'], timeout=60)
            cur[k] = v


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('image')
    ap.add_argument('--label', required=True)
    ap.add_argument('--opt', action='append', default=[])
    ap.add_argument('--rounds', type=int, default=2)
    ap.add_argument('--window', type=float, default=30)
    ap.add_argument('--settle', type=float, default=15)
    ap.add_argument('--no-variants', dest='variants', action='store_false')
    ap.add_argument('--no-launch', dest='launch', action='store_false')
    ap.add_argument('--out', default='.sim/pacing')
    a = ap.parse_args()

    outdir = os.path.join(a.out, a.label)
    os.makedirs(outdir, exist_ok=True)
    cur = dict(DEFAULTS)
    for o in a.opt:
        k, _, v = o.partition('=')
        cur[k.strip()] = v.strip()

    if a.launch:
        print(f'[{a.label}] launch {a.image}')
        run(['launch', a.image, *sum((['--opt', f'{k}={v}'] for k, v in cur.items()), [])],
            timeout=240)
    if not wait_live():
        sys.exit(f'[{a.label}] video never went live -- not measuring a state it did not reach')

    plan = []
    for r in range(a.rounds):
        plan += [(f'{n}#{r + 1}', w) for n, w in VO_CELLS]
    if a.variants:
        plan += [(n, dict(w, **{'Video Output': 'Progressive'})) for n, w in VARIANT_CELLS]

    results = []
    for name, want in plan:
        full = dict(want)
        for k in ('Video Output', 'Film 24p Out', 'Deinterlace'):
            full.setdefault(k, DEFAULTS[k])
        apply(cur, full)
        time.sleep(a.settle)
        if not wait_live(60):
            print(f'[{a.label}] {name}: NOT LIVE after settle -- cell skipped')
            results.append({'cell': name, 'skipped': 'not live'})
            continue
        stem = os.path.join(outdir, name.replace('#', '_'))
        txt = run(['telem', '--watch', str(a.window), '--json', stem + '.json',
                   '--jsonl', stem + '.jsonl'], timeout=a.window + 120)
        with open(stem + '.txt', 'w') as f:
            f.write(txt)
        with open(stem + '.json') as f:
            s = json.load(f)
        s['cell'], s['opts'] = name, dict(full)
        results.append(s)
        print(f"[{a.label}] {name:14s} lates/s {s['lates_per_s']:5.2f} drops/s "
              f"{s['drops_per_s']:5.2f} fps {s['content_fps']:6.3f} raster "
              f"{s['raster_hz']:6.3f} aud {s['audio_hz'] or 0:7.0f} resets "
              f"{sum(s['resets'].values())} ps={s['sched']['sched_ps']} "
              f"pf={s['sched']['sched_pf']} frc={s['sched']['sched_frc']} "
              f"menu={s['frac']['menu']:.2f} still={s['frac']['still']:.2f}")

    # put the three mode options back to the launch values
    apply(cur, {k: DEFAULTS[k] for k in ('Video Output', 'Film 24p Out', 'Deinterlace')})
    with open(os.path.join(outdir, 'matrix.json'), 'w') as f:
        json.dump({'label': a.label, 'image': a.image, 'base': a.opt,
                   'cells': results}, f, indent=1)
    print(f'[{a.label}] wrote {outdir}/matrix.json')


if __name__ == '__main__':
    main()
