#!/usr/bin/env python3
"""
test_dts_ref.py -- gate for the DTS core reference model (tools/dts_ref.py).

The claim: dts_ref.py decodes the DTS core BIT-EXACTLY like the FFmpeg binary's
fixed-point path (`ffmpeg -flags bitexact`), so it can stand as the golden for
the fixed-point model and the RTL (docs/dts_decoder.md sec 6).

Arms:
  [1] every stream: bit-exact, the same channel count and length, and at least
      half the samples nonzero (a comparison of silence proves nothing).
  RED one mutation per stage (dts_ref.MUT). Each must make the comparison FAIL
      on every stream that EXERCISES its stage. A mutation that a stream does
      not exercise is reported as a COVERAGE GAP, not a pass: the gap list is
      the work list for finding more test streams (tools/dts_scan.py).

Streams: raw DTS core files (16-bit big-endian frames), from
  DTS_TEST_STREAMS  -- a colon-separated list of paths, or
  DTS_TEST_DIR      -- every *.dts under it (default ~/dts-streams/gate: the
                       synthetic fixtures from tools/gen_dts_fixtures.py plus a
                       few disc windows chosen by tools/dts_scan.py for what they
                       cover -- XCh, embedded downmix, 1536k, transients, and a
                       disc whose block codes overflow).
A stream with frames FFmpeg REFUSES (dts_ref raises on them, matching FFmpeg)
cannot be compared bit-exactly and is reported as REFUSED, not passed; the
lenient decode such a disc needs (D5) is gated by tools/test_dts_fixed.py.
They are rips of commercial discs, so they are never committed (like the ISO
library, docs/bug_reports.md). With none found the test FAILS -- an unrun gate
is not a passing one.

Usage: python3 tools/test_dts_ref.py [--frames N]     (exit 0 = PASS)
"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dts_ref  # noqa: E402

# mutation -> the feature counter a stream must show for the mutation to bite
ARMS = {
    'no_adpcm': 'pmode_bands',
    'vq_index': 'vq_bands',
    'block_offset': 'block',
    'raw_unsigned': 'raw',
    'huff_adj': 'huff_adj',          # a Huffman band whose adjustment is not x1.0
    'dequant_trunc': None,           # every coded sample
    'transient': 'transient',
    'no_joint': 'joint_bands',
    'imdct_noshift': 'imdct_shift',  # loud blocks (sum of |input| > 2^22)
    'window_swap': None,             # every stream
    'lfe_nohist': 'lfe',
    'no_sumdiff': 'sumdiff_front',
}


def streams():
    env = os.environ.get('DTS_TEST_STREAMS')
    if env:
        return [p for p in env.split(':') if p]
    root = os.environ.get('DTS_TEST_DIR', os.path.expanduser('~/dts-streams/gate'))
    return sorted(glob.glob(os.path.join(root, '**', '*.dts'), recursive=True))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--frames', type=int, default=300,
                    help='frames per stream per arm (default 300)')
    args = ap.parse_args()
    paths = streams()
    if not paths:
        print('test_dts_ref: no streams (set DTS_TEST_STREAMS or DTS_TEST_DIR)')
        print('RESULT: FAIL')
        return 1
    fails, gaps, refused = 0, {}, []
    for path in paths:
        name = os.path.basename(path)
        dts_ref.MUT.clear()
        try:
            r = dts_ref.run_compare(path, args.frames)
        except dts_ref.DtsError as e:
            print(f'[1] REFUSED {name}: {e} -- FFmpeg refuses such frames too; '
                  f'not comparable bit-exactly (D5)')
            refused.append(name)
            continue
        ok = (r['mismatches'] == 0 and r['len_ok'] and r['channels'] == r['ours_ch']
              and r['nonzero'] * 2 >= r['samples'])
        print(f'[1] {"PASS" if ok else "FAIL"} {name}: {r["frames"]} frames, '
              f'{r["channels"]} ch, {r["mismatches"]} mismatches, '
              f'{r["nonzero"] * 100 // max(r["samples"], 1)} % nonzero')
        fails += not ok
        feat = r['features']
        for mut, need in ARMS.items():
            dts_ref.MUT.clear()
            dts_ref.MUT.add(mut)
            try:
                m = dts_ref.run_compare(path, args.frames)
                bit = m['mismatches'] > 0 or not m['len_ok']
            except dts_ref.DtsError:
                bit = True                       # a mutation that breaks the parse bites too
            exercised = need is None or feat.get(need, 0) > 0
            if bit:
                print(f'    RED {mut:14} bites')
            elif exercised and need is not None:
                print(f'    RED {mut:14} BLIND -- the stream uses {need} but the comparison passed')
                fails += 1
            else:
                print(f'    RED {mut:14} gap   -- not exercised by this stream')
                gaps.setdefault(mut, []).append(name)
        dts_ref.MUT.clear()
    compared = len(paths) - len(refused)
    if not compared:
        print('test_dts_ref: every stream was refused -- nothing was compared')
        fails += 1
    uncovered = [m for m in ARMS if len(gaps.get(m, [])) == compared]
    if uncovered:
        print('COVERAGE GAPS (no stream exercises): ' + ', '.join(uncovered))
    print(f'RESULT: {"PASS" if not fails else "FAIL"}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
