#!/usr/bin/env python3
"""test_ac3_isa.py -- gate for the AC-3 engine program and emulator (A1).

Claims (docs/ac3_engine.md):
  [1] the emulator, running dvd/dts/ac3.uasm, is bit-exact against
      tools/ac3_model.py on every block of every stream -- exponents, baps,
      coefficients, blksw, dynrng -- and refuses exactly the frames the model
      refuses (the model is itself bit-exact against the RTL: test_ac3_model.py);
  [2] every frame fits BUDGET of real time, counting the op charges with their
      headroom factor and imdct_512 in series (13.5K cycles a block, measured on
      the RTL by bench/ac3/golden_main.cpp, for 3+ channels; 4.6K for stereo);
  [3] the generated images match the program (ac3_isa.py --asm --check).
RED arms: microcode mutations (`;MUT name:` lines in ac3.uasm), each tied to the
stream feature it needs; one that bites on no stream with the feature is a FAIL,
one whose feature no stream has is a GAP.
Streams: as tools/test_ac3_model.py.  Usage: python3 tools/test_ac3_isa.py [--frames N]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ac3_isa as A          # noqa: E402
import ac3_model as M        # noqa: E402
from test_ac3_model import streams, features   # noqa: E402

FRAME_CYC = 27_000_000 * 1536 // 48000      # 864,000
BUDGET = 0.60
IMDCT_BLOCK = {1: 4573, 2: 4573}            # by channel count; 3+ -> 13479 (measured)
ARMS = {'p3seed': 'blocks', 'cplseed': 'cpl', 'phsflg': 'phsflg', 'dynreset': 'dynrnge',
        'knee': 'blocks'}


def frame_cost(m, nch):
    """The worst frame: the op charges with headroom, plus the IMDCT in series."""
    if not m.frame_cycles:
        return 0
    seq_and_ops = max(m.frame_cycles)
    ops = sum(v for k, v in m.by_cat.items()) / max(len(m.frame_cycles), 1)
    head = (A.CYC_HEADROOM - 1) * ops
    imdct = 6 * IMDCT_BLOCK.get(nch, 13479)
    return seq_and_ops + head + imdct


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--frames', type=int, default=24)
    a = ap.parse_args()
    fails = 0
    rc = A.write_mems(check=True)
    print(f'[3] {"PASS" if rc == 0 else "FAIL"} the generated images match ac3.uasm')
    fails += rc != 0
    paths = streams()
    worst = (0.0, '')
    base_bad = {}
    for p in paths:
        m, nf, bad, first = A.emulate(p, a.frames)
        base_bad[p] = bad
        nch = max((M.NFCHANS[m.rec[0]] if m.rec[0] else 2), 1)
        frac = frame_cost(m, nch) / FRAME_CYC
        if frac > worst[0]:
            worst = (frac, os.path.basename(p))
        ok1 = bad == 0 and (nf > 0 or 'refuse' in os.path.basename(p))
        ok2 = frac <= BUDGET
        fails += (not ok1) + (not ok2)
        print(f'[1] {"PASS" if ok1 else "FAIL"} {os.path.basename(p)}: {nf} frames, {bad} mismatches'
              + (f' ({first})' if first else '') + f'; [2] {"PASS" if ok2 else "FAIL"} worst frame '
              f'{frac * 100:.0f} %')
    feat = features(paths, a.frames)
    for arm, need in ARMS.items():
        have = [p for p in paths if feat[p].get(need) and not base_bad[p]]
        if not have:
            print(f'    RED {arm:10s} GAP -- no stream has {need}')
            continue
        bit = sum(1 for p in have if A.emulate(p, a.frames, mutate=arm)[2])
        print(f'    RED {arm:10s} ' + (f'bites on {bit} of {len(have)} stream(s) with {need}' if bit
                                       else f'BLIND on {len(have)} stream(s) with {need}: FAIL'))
        fails += not bit
    print(f'worst frame: {worst[0] * 100:.1f} % of real time ({worst[1]})')
    print(f'RESULT: {"PASS" if not fails else "FAIL"}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
