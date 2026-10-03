#!/usr/bin/env python3
"""dts_cycles.py -- the DTS engine's real-time margin over many streams, cheaply.

Runs tools/dts_isa.py's emulator, whose cycle model is calibrated on the RTL (within
0.3 % of bench/dvd/run_dts.sh on every gate arm, docs/dts_decoder.md P1b), over every
frame of every stream given, and reports each frame's cycles against its real-time
budget (27 MHz x the frame's duration). The RTL bench can afford 2 frames a stream;
this covers whole census windows.

    tools/dts_cycles.py FILE.dts ... [--jobs N] [--latency CYCLES] [--budget 0.60]
    tools/dts_cycles.py --dir ~/dts-streams/census

Exit 0 when every frame is within the budget and none is refused; 1 otherwise.
"""
import argparse
import glob
import os
import sys
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dts_isa as I  # noqa: E402
import dts_ref as R  # noqa: E402

CLK = 27_000_000


def one(args):
    path, latency = args
    R.OPT.add('lenient_block')                     # D5: the hardware's behaviour
    words, labels = I.load_program()
    m = I.Machine(words, labels)
    m.cb_latency = latency
    worst, worst_k, n = 0.0, -1, 0
    durs = []
    for _, fr in R.frames(open(path, 'rb').read()):
        h = R.parse_frame_header(R.BitReader(fr))
        durs.append(h['npcmblocks'] * R.PCMBLOCK_SAMPLES / 48000)
        m.feed(fr)
        m.run()
        n += 1
    for k, (c, d) in enumerate(zip(m.frame_cycles, durs)):
        f = c / (CLK * d)
        if f > worst:
            worst, worst_k = f, k
    mean = sum(m.frame_cycles) / max(1, CLK * sum(durs[:len(m.frame_cycles)]))
    return path, n, worst, worst_k, mean, sum(m.errors.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('files', nargs='*')
    ap.add_argument('--dir')
    ap.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument('--latency', type=int, default=I.CB_LATENCY)
    ap.add_argument('--budget', type=float, default=0.60)
    a = ap.parse_args()
    files = list(a.files)
    if a.dir:
        files += sorted(glob.glob(os.path.join(os.path.expanduser(a.dir), '*.dts')))
    if not files:
        ap.error('no streams')
    with Pool(a.jobs) as pool:
        rows = pool.map(one, [(f, a.latency) for f in files])
    rows.sort(key=lambda r: -r[2])
    frames = sum(r[1] for r in rows)
    refused = sum(r[5] for r in rows)
    over = [r for r in rows if r[2] > a.budget]
    for p, n, w, k, mean, e in rows[:10]:
        print(f'  {w * 100:5.1f} % worst (frame {k}), mean {mean * 100:5.1f} %, {n} frames, '
              f'{e} refused: {os.path.basename(p)}')
    print(f'dts_cycles: {len(rows)} streams, {frames} frames, codebook latency {a.latency}: '
          f'worst frame {rows[0][2] * 100:.1f} % of real time ({os.path.basename(rows[0][0])}); '
          f'{len(over)} over the {a.budget * 100:.0f} % budget; {refused} refused')
    return 1 if over or refused else 0


if __name__ == '__main__':
    sys.exit(main())
