#!/usr/bin/env python3
"""
test_dts_fixed.py -- gate for the hardware-order DTS model (tools/dts_fixed.py).

Claims (docs/dts_decoder.md sec 3 and D3):
  [1] the STREAMING front end (per subsubframe) produces subband samples
      bit-identical to dts_ref's FFmpeg-order front end;
  [2] mixing to stereo BEFORE synthesis stays within MAX_LSB of mixing after
      it (the same Q15 gains), at s16.
RED arms (dts_fixed.MUT) must fail exactly their own claim:
  stale_history -> [1]   (the ADPCM history skips unpredicted bands)
  mix_trunc     -> [2]   (the downmix truncates instead of rounding, at a
                          coarse scale, so the error must exceed the bound)

Streams: as tools/test_dts_ref.py (DTS_TEST_STREAMS / DTS_TEST_DIR); none = FAIL.
Usage: python3 tools/test_dts_fixed.py [--frames N]     (exit 0 = PASS)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dts_fixed  # noqa: E402
from test_dts_ref import streams  # noqa: E402

MAX_LSB = 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--frames', type=int, default=120)
    args = ap.parse_args()
    paths = streams()
    if not paths:
        print('test_dts_fixed: no streams (set DTS_TEST_STREAMS or DTS_TEST_DIR)')
        print('RESULT: FAIL')
        return 1
    fails = 0
    for path in paths:
        name = os.path.basename(path)
        dts_fixed.MUT.clear()
        v = dts_fixed.run_verify(path, args.frames)
        ok1 = v['fe_bad'] == 0 and v['frames'] > 0
        ok2 = v['worst'] <= MAX_LSB
        print(f'[1] {"PASS" if ok1 else "FAIL"} {name}: {v["frames"]} frames, '
              f'{v["fe_bad"]} subband samples differ')
        print(f'[2] {"PASS" if ok2 else "FAIL"} {name}: worst {v["worst"]} LSB, '
              f'rms {v["rms"]:.4f}')
        fails += (not ok1) + (not ok2)
        for mut, arm in (('stale_history', 1), ('mix_trunc', 2)):
            dts_fixed.MUT.clear()
            dts_fixed.MUT.add(mut)
            m = dts_fixed.run_verify(path, args.frames)
            b1, b2 = m['fe_bad'] > 0, m['worst'] > MAX_LSB
            own, other = (b1, b2) if arm == 1 else (b2, b1)
            # stale_history corrupts the samples, so [2] may also move; only the
            # arm's OWN claim is required to fail (and mix_trunc must not touch [1])
            good = own and (arm == 1 or not other)
            print(f'    RED {mut:14} {"bites" if good else "BLIND"} '
                  f'([1] {"fail" if b1 else "pass"}, [2] {"fail" if b2 else "pass"})')
            fails += not good
        dts_fixed.MUT.clear()
    print(f'RESULT: {"PASS" if not fails else "FAIL"}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
