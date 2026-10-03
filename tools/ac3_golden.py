#!/usr/bin/env python3
"""ac3_golden.py -- goldens for the AC-3 program's RTL benches (docs/ac3_engine.md A2).

The emulator (tools/ac3_isa.py) runs a window of a raw .ac3 stream. Up to the first
refusal it must agree with tools/ac3_model.py (itself bit-exact with dvd/ac3/'s RTL,
tools/test_ac3_model.py) block by block: no golden is written from an emulator that
disagrees. Past a refusal the engine goes on with the next frame, which neither the
model nor dvd/ac3 does (both halt), so those frames are the emulator's alone: they
score the RTL's restart path, not the decode.

Writes what bench/dvd/dts_seq_tb.sv scores (tools/dts_golden.py's formats):
    STEM.bytes    the frames' bytes, hex, one a line
    STEM.frames   each frame's length in bytes, one a line (the engine's descriptor)
    STEM.trace    "kind pc addr value" hex, one event a line (Machine.trace: register
                  writes, stores, the mantissa unit's items, the units' record writes)
    STEM.meta     "bytes frames events 0 0 refusals overrun-bits 0 0"

    tools/ac3_golden.py STREAM.ac3 --out STEM [--skip N] [--frames N] [--truncate K:N]
                        [--badexp K]

--truncate K:N delivers frame K N bytes short (its descriptor says the shorter length):
the engine reads past its end as zeros (counted). The model is not consulted from frame
K on.
--badexp K rewrites frame K's first five exponent group codes (EXPD's 7-bit reads) to
124 -- digits 4 4 4, +6 an exponent a group -- so the exponent passes 24 and EXPD
refuses (E_EXP), which no disc window does. The model refuses it too.
Exit 0 written; 5 the emulator disagrees with the model.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ac3_isa as A    # noqa: E402
import ac3_model as M  # noqa: E402


def expd_code_bitpos(words, labels, before, fr, n=5):
    """The bit positions of frame `fr`'s first n EXPD group codes, found by running
    the program over the frames before it and then it, noting where EXPD reads."""
    m = A.Machine(words, labels)
    for f in before:
        m.feed(f)
        m.run()
    pos = []
    run_op = m.run_op

    def hooked(name, a):
        if name != 'expd':
            return run_op(name, a)
        bits = m.bits

        def b(k):
            if k == 7 and len(pos) < n:
                pos.append(m.bitpos)
            return bits(k)
        m.bits = b
        try:
            return run_op(name, a)
        finally:
            m.bits = bits
    m.run_op = hooked
    m.feed(fr)
    m.run()
    if len(pos) < n:
        raise SystemExit(f'ac3_golden: only {len(pos)} exponent codes in the frame')
    return pos


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('stream')
    ap.add_argument('--out', required=True)
    ap.add_argument('--skip', type=int, default=0)
    ap.add_argument('--frames', type=int, default=4)
    ap.add_argument('--truncate', action='append', default=[])
    ap.add_argument('--badexp', type=int, action='append', default=[])
    a = ap.parse_args(argv)
    words, labels = A.load_program()
    sel = [fr for _, fr in M.frames(open(a.stream, 'rb').read())][a.skip:a.skip + a.frames]
    if not sel:
        print(f'ac3_golden: no frames in {a.stream} past {a.skip}')
        return 5
    for k in a.badexp:
        fr = bytearray(sel[k])
        for p in expd_code_bitpos(words, labels, sel[:k], bytes(sel[k])):
            for i in range(7):                       # the code 124 = 1111100
                bit = (124 >> (6 - i)) & 1
                q = p + i
                fr[q >> 3] = (fr[q >> 3] & ~(0x80 >> (q & 7))) | (bit << (7 - (q & 7)))
        sel[k] = bytes(fr)
    first_cut = None
    for t in a.truncate:
        k, n = (int(x) for x in t.split(':'))
        sel[k] = sel[k][:-n]
        first_cut = k if first_cut is None else min(first_cut, k)

    m = A.Machine(words, labels)
    m.trace = []
    ref = M.Decoder()
    ref.snapshot = True
    checked, refused_at = 0, None
    for k, fr in enumerate(sel):
        n0, e0 = len(m.blocks), sum(m.errors.values())
        m.feed(fr)
        m.run()
        refused = sum(m.errors.values()) > e0
        if refused_at is not None or (first_cut is not None and k >= first_cut):
            refused_at = k if refused and refused_at is None else refused_at
            continue
        try:
            _, gblocks = ref.frame(fr)
            model_refused = False
        except M.Ac3Error:
            model_refused = True
        if refused != model_refused:
            print(f'ac3_golden: frame {k}: the emulator refused {refused}, the model {model_refused}')
            return 5
        if refused:
            refused_at = k
            continue
        eb = m.blocks[n0:]
        if len(eb) != len(gblocks) or any(
                x['coeff'] != y['coeff'] or x['lfe'] != y['lfe'] or x['blksw'] != y['blksw'] or
                x['dynrng'] != y['dynrng'] for x, y in zip(eb, gblocks)):
            print(f'ac3_golden: frame {k}: the emulator\'s blocks differ from the model\'s')
            return 5
        checked += 1

    stem = a.out
    os.makedirs(os.path.dirname(os.path.abspath(stem)), exist_ok=True)
    data = b''.join(sel)
    with open(stem + '.bytes', 'w') as f:
        f.write(''.join(f'{b:02x}\n' for b in data))
    with open(stem + '.frames', 'w') as f:
        f.write(''.join(f'{len(fr):x}\n' for fr in sel))
    with open(stem + '.trace', 'w') as f:
        f.write(''.join(f'{k:x} {pc:03x} {ad:x} {v:06x}\n' for k, pc, ad, v in m.trace))
    nerr = sum(m.errors.values())
    with open(stem + '.meta', 'w') as f:
        f.write(f'{len(data)} {len(sel)} {len(m.trace)} 0 0 {nerr} {m.overrun} 0 0\n')
    kinds = [0, 0, 0, 0]
    for e in m.trace:
        kinds[e[0]] += 1
    print(f'ac3_golden: {os.path.basename(a.stream)} frames {a.skip}..{a.skip + len(sel) - 1}: '
          f'{len(data)} bytes, {len(m.trace)} events (kinds {kinds}), {nerr} refused '
          f'{dict(m.errors) or ""}, {m.overrun} overrun bits; {checked} frames match the model')
    return 0


if __name__ == '__main__':
    sys.exit(main())
