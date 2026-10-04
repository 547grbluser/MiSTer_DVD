#!/usr/bin/env python3
"""mp2_golden.py -- goldens for the MP2 engine's RTL benches (docs/mp2_engine.md M2).

The emulator (tools/mp2_isa.py) runs a window of an .mp2 stream. It must first agree
with tools/mp2_ref.py pair for pair (and every op with the model's own functions,
which mp2_isa checks as it runs): no golden is written from an emulator that
disagrees. Then this writes what the benches score, in tools/dts_golden.py's format
(bench/dvd/dts_seq_tb.sv and dts_top_tb.sv read both):

    STEM.bytes    the frames' bytes, hex, one a line
    STEM.frames   each frame's length in bytes (the engine's descriptor)
    STEM.trace    "kind pc addr value": every register write and store, in order
    STEM.vops     "op x hist ring buf2": the buffers' checksums after every vector op
                  (mp2_isa.Machine.checksums, at MP2's widths)
    STEM.pcm      "llll rrrr" one stereo pair a line
    STEM.meta     "bytes frames events vops pairs refusals 0 0 long fs": dts_golden's
                  overrun and lenient counts (0 for MP2), CNT counter 0 (frames longer
                  than their header: decoded, the rest drained), and the
                  sampling-frequency index MSYN exports

    tools/mp2_golden.py STREAM.mp2 --out STEM [--skip N] [--frames N] [--short K]

--short K delivers frame K one byte short: its descriptor disagrees with its header,
so the engine refuses it (E_LEN) before any of its PCM; the frames after it prove no
byte of it was taken and the state is untouched.
--long K:N delivers frame K with N junk bytes after it (what mp2_reframer passes when a
frame is not followed by a sync): decoded, the junk drained, CNT counter 0 counts it.
Exit 0 written; 5 the emulator disagrees with the model.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mp2_isa as P   # noqa: E402
import mp2_ref as R   # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('stream')
    ap.add_argument('--out', required=True)
    ap.add_argument('--skip', type=int, default=0)
    ap.add_argument('--frames', type=int, default=4)
    ap.add_argument('--short', type=int, action='append', default=[])
    ap.add_argument('--long', action='append', default=[])
    a = ap.parse_args(argv)
    sel = list(R.iter_frames(open(a.stream, 'rb').read()))[a.skip:a.skip + a.frames]
    if not sel:
        print(f'mp2_golden: no frames in {a.stream} past {a.skip}')
        return 5
    words, labels = P.load_program()
    m = P.Machine(words, labels)
    m.trace = []
    vops = []
    m.vop_hook = lambda mach, op: vops.append((op,) + mach.checksums())
    ref = R.MP2Decoder()
    feed = []
    junk = {int(k): int(n) for k, n in (t.split(':') for t in a.long)}
    for k, (fr, hdr) in enumerate(sel):
        n0, e0 = len(m.pcm[0]), sum(m.errors.values())
        data = fr[:-1] if k in a.short else fr + bytes((0x5A + i * 37) & 0xFF for i in range(junk.get(k, 0)))
        feed.append(data)
        m.feed(data)
        m.run()
        refused = sum(m.errors.values()) - e0
        if refused != (k in a.short):
            print(f'mp2_golden: frame {k}: the emulator refused {refused}, expected {k in a.short}')
            return 5
        if k in a.short:
            continue                       # the model never sees it: the state must not move
        want = ref.decode_frame(fr, hdr)
        if list(zip(m.pcm[0][n0:], m.pcm[1][n0:])) != want:
            print(f'mp2_golden: frame {k}: the emulator\'s PCM differs from mp2_ref')
            return 5
    stem = a.out
    os.makedirs(os.path.dirname(os.path.abspath(stem)), exist_ok=True)
    data = b''.join(feed)
    with open(stem + '.bytes', 'w') as f:
        f.write(''.join(f'{b:02x}\n' for b in data))
    with open(stem + '.frames', 'w') as f:
        f.write(''.join(f'{len(fr):x}\n' for fr in feed))
    with open(stem + '.trace', 'w') as f:
        f.write(''.join(f'{k:x} {pc:03x} {ad:x} {v:06x}\n' for k, pc, ad, v in m.trace))
    with open(stem + '.vops', 'w') as f:
        f.write(''.join(f'{op:x} {x:08x} {h:08x} {r:08x} {b:08x}\n' for op, x, h, r, b in vops))
    with open(stem + '.pcm', 'w') as f:
        f.write(''.join(f'{l & 0xFFFF:04x} {r & 0xFFFF:04x}\n' for l, r in zip(*m.pcm)))
    nerr = sum(m.errors.values())
    with open(stem + '.meta', 'w') as f:
        f.write(f'{len(data)} {len(feed)} {len(m.trace)} {len(vops)} {len(m.pcm[0])} {nerr} '
                f'0 0 {m.counters.get(0, 0)} {m.fs if m.fs is not None else 0}\n')
    print(f'mp2_golden: {os.path.basename(a.stream)} frames {a.skip}..{a.skip + len(sel) - 1}: '
          f'{len(data)} bytes, {len(m.trace)} events, {len(vops)} vector ops, '
          f'{len(m.pcm[0])} pairs, {nerr} refused; matches the model')
    return 0


if __name__ == '__main__':
    sys.exit(main())
