#!/usr/bin/env python3
"""
test_dts_isa.py -- gate for the DTS engine's microcode and emulator (P1a,
docs/dts_decoder.md sec 10).

Claims:
  [1] the emulator, running dvd/dts/dts.uasm, produces PCM bit-identical to
      tools/dts_fixed.py on every gate stream (spec maxima included), with no
      engine error;
  [2] every frame fits the real-time budget with margin: cycles <= BUDGET_FRAC
      x 27 MHz x the frame's duration. The cycle model (dts_isa.CYC) is calibrated
      on the RTL: within 0.3 % of bench/dvd/run_dts.sh's measured cycles on every
      gate arm (2026-10-02), at the codebook latency dts_isa.CB_LATENCY;
  [3] the committed .mem images match the assembled source (dts_isa.py --asm
      --check).
RED arms: microcode mutations (`;MUT name:` lines in dts.uasm). Each must make
[1] fail on every stream that exercises its stage; on a stream that does not,
it is a gap. An arm that bites on no stream at all is a FAIL.

Streams: as tools/test_dts_ref.py (DTS_TEST_STREAMS / DTS_TEST_DIR).
Usage: python3 tools/test_dts_isa.py [--frames N]     (exit 0 = PASS)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dts_fixed as F  # noqa: E402
import dts_isa as I    # noqa: E402
import dts_ref as R    # noqa: E402
from test_dts_ref import streams  # noqa: E402

CLK = 27_000_000
BUDGET_FRAC = 0.60
# mutation -> the stream feature it needs
# (vq_slice reads slice 0 always: it can only bite with more than one subsubframe)
ARMS = {'transient': 'transient', 'vq_slice': 'vq_multi_ssf', 'no_adpcm': 'pmode_bands',
        'no_joint': 'joint_bands', 'mix_own_bound': 'bfly_uneven'}


def features(path, nframes):
    """What the first nframes exercise: the reference front end's counters, and
    whether a butterflied pair has uneven active counts."""
    feat = dict.fromkeys(ARMS.values(), 0)
    dec = R.CoreDecoder()
    n = 0
    for _, fr in R.frames(open(path, 'rb').read()):
        if n >= nframes:
            break
        n += 1
        r = dec.decode_frame(fr)
        for k in ('transient', 'pmode_bands', 'joint_bands'):
            feat[k] += r['stats'][k]
        if max(r['stats']['subframes']) > 1:
            # NONZERO VQ samples: Shadoan's VQ bands all decode to 0, so no slice
            # choice can change its output
            c_ = r['c']
            feat['vq_multi_ssf'] += sum(1 for ch in range(c_['nchannels'])
                                        for b in range(c_['vq_start'][ch], c_['nsubbands'][ch])
                                        for v in r['sb'][ch][b] if v)
        h, c = r['h'], r['c']
        nact = [max(c['nsubbands'][ch], c['nsubbands'][c['joint_index'][ch] - 1])
                if c['joint_index'][ch] else c['nsubbands'][ch] for ch in range(c['nchannels'])]
        spk = R.PRM_CH_TO_SPKR[h['audio_mode']]
        pairs = []
        if (h['sumdiff_front'] and h['audio_mode'] > 0) or h['audio_mode'] == R.AMODE_STEREO_SUMDIFF:
            pairs.append((R.SPK_L, R.SPK_R))
        if h['sumdiff_surround'] and h['audio_mode'] >= R.AMODE_2F2R:
            pairs.append((R.SPK_LS, R.SPK_RS))
        for p, q in pairs:
            if p in spk and q in spk and nact[spk.index(p)] != nact[spk.index(q)]:
                feat['bfly_uneven'] += 1
    return feat


def golden(path, nframes):
    hw = F.StreamDecoder()
    L, Rr, durs = [], [], []
    n = 0
    for _, fr in R.frames(open(path, 'rb').read()):
        if n >= nframes:
            break
        a, b, h, _ = hw.decode(fr)
        L += a
        Rr += b
        durs.append(h['npcmblocks'] * R.PCMBLOCK_SAMPLES / 48000)
        n += 1
    return L, Rr, durs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--frames', type=int, default=8)
    args = ap.parse_args()
    R.OPT.add('lenient_block')                      # D5, as the hardware
    fails = 0
    rc = I.write_mems(check=True)
    print(f'[3] {"PASS" if rc == 0 else "FAIL"} .mem images match dts.uasm')
    fails += rc != 0
    paths = streams()
    if not paths:
        print('test_dts_isa: no streams (set DTS_TEST_STREAMS or DTS_TEST_DIR)')
        print('RESULT: FAIL')
        return 1
    bitten = dict.fromkeys(ARMS, 0)
    worst = (0.0, '')
    for path in paths:
        name = os.path.basename(path)
        GL, GR, durs = golden(path, args.frames)
        L, Rr, m = I.emulate(path, args.frames)
        bad = sum(1 for x, y in zip(L + Rr, GL + GR) if x != y) + abs(len(L) - len(GL))
        ok1 = bad == 0 and not m.errors and len(L) > 0
        fracs = [c / (CLK * d) for c, d in zip(m.frame_cycles, durs)]
        peak = max(fracs) if fracs else 0.0
        if peak > worst[0]:
            worst = (peak, name)
        ok2 = peak <= BUDGET_FRAC
        print(f'[1] {"PASS" if ok1 else "FAIL"} {name}: {len(durs)} frames, {bad} mismatches, '
              f'errors {m.errors or "none"}; [2] {"PASS" if ok2 else "FAIL"} peak '
              f'{peak * 100:.0f} % of real time')
        fails += (not ok1) + (not ok2)
        feat = features(path, args.frames)
        for mut, need in ARMS.items():
            if not feat[need]:
                continue
            ML, MR, mm = I.emulate(path, args.frames, mutate=mut)
            mbad = sum(1 for x, y in zip(ML + MR, GL + GR) if x != y) + abs(len(ML) - len(GL))
            if mbad or mm.errors:
                bitten[mut] += 1
            else:
                print(f'    RED {mut:14} BLIND -- the stream uses {need} but [1] passed')
                fails += 1
    for mut, n in bitten.items():
        print(f'    RED {mut:14} bites on {n} stream(s)' + ('  <-- NEVER: FAIL' if not n else ''))
        fails += n == 0
    print(f'worst frame: {worst[0] * 100:.1f} % of real time ({worst[1]})')
    print(f'RESULT: {"PASS" if not fails else "FAIL"}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
