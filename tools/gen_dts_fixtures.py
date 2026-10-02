#!/usr/bin/env python3
"""gen_dts_fixtures.py -- synthetic DTS core streams for the DTS gates.

The library's discs (tools/dts_scan.py, docs/dts_decoder.md sec 9) never
exercise some of what the format allows -- Huffman-coded samples, sum/difference
coding, the perfect-reconstruction filter, non-unity Huffman scale adjustment --
but the decoder must still be right on them (CLAUDE.md "Design to the DVD spec
maximum"). These fixtures come from generated signals only, so they are free
of disc content; they are regenerated rather than committed because they depend
on the encoder (FFmpeg n9.0.2's `dca`).

Two kinds:
  encoded -- FFmpeg's DTS encoder on a generated signal. A different signal on
             every channel (noise, a per-channel tone, a gated burst), because
             a silent or duplicated channel hides mapping faults.
  derived -- an encoded stream with fixed-width fields the encoder never sets
             rewritten in every frame. Legal because no frame carries a CRC
             (checked), and a fixed-width field leaves the layout unchanged:
               header bit 88 filter_perfect, 98 sumdiff_front,
               99 sumdiff_surround; the coding header's 2-bit Huffman
               scale-adjustment indices.
             A sum/difference stream is derived from content ENCODED in
             sum/difference form ((a+b)/2, (a-b)/2), so the decoder's
             butterflies reconstruct in-range a and b, as a real one would.
  written -- tools/dts_writer.py, our own syntax-level encoder, for what neither
             of the above can make: joint intensity (it changes the layout), the
             frame-shape spec maxima (npcmblocks 128 as 16 subframes and as 4x4
             subsubframes, frames near 16 KB), all ten channel arrangements, and
             header CRC words, DRC, time code, aux downmix + CRC, predictor
             history off and the lossless step table. Seeded, so reproducible.

Usage: tools/gen_dts_fixtures.py [OUT_DIR]   (default: $DTS_TEST_DIR or ~/dts-streams/gate)
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dts_ref  # noqa: E402
import dts_writer  # noqa: E402

SECONDS = 4
CHANNELS = {'mono': 1, 'stereo': 2, 'quad(side)': 4, '5.0(side)': 5, '5.1(side)': 6}
# FFmpeg's channel order for these layouts: FL FR [FC LFE] SL SR
PAIRS = {'stereo': [(0, 1)], '5.1(side)': [(0, 1), (4, 5)]}

# (name, layout, rate, ADPCM, content). The encoder's rate floors, measured on
# n9.0.2: mono 192k, stereo 320k, quad 640k, 5.x 768k.
ENCODED = [
    ('synth_stereo_320k', 'stereo', '320k', 0, 'plain'),
    ('synth_stereo_768k', 'stereo', '768k', 0, 'plain'),
    ('synth_mono_192k', 'mono', '192k', 0, 'plain'),
    ('synth_quad_640k_adpcm', 'quad(side)', '640k', 1, 'plain'),
    ('synth_50side_768k_adpcm', '5.0(side)', '768k', 1, 'plain'),
    ('synth_51side_768k_adpcm', '5.1(side)', '768k', 1, 'plain'),
    ('synth_51side_1536k', '5.1(side)', '1536k', 0, 'plain'),
    ('synth_stereo_1536k_loud', 'stereo', '1536k', 0, 'loud'),
    ('_sdbase_stereo_768k', 'stereo', '768k', 0, 'sumdiff'),
    ('_sdbase_51side_768k_adpcm', '5.1(side)', '768k', 1, 'sumdiff'),
]

# (name, base, header bits to set, Huffman scale-adjustment index or None)
DERIVED = [
    ('synth_stereo_768k_sumdiff', '_sdbase_stereo_768k', (98,), None),
    ('synth_51side_768k_adpcm_sumdiff', '_sdbase_51side_768k_adpcm', (98, 99), None),
    ('synth_51side_1536k_perfect', 'synth_51side_1536k', (88,), None),
    ('synth_stereo_320k_adj', 'synth_stereo_320k', (), 3),
]


# (fixture name, dts_writer profile, frames)
WRITTEN = [('written_joint', 'joint', 40),
           ('written_max_subframes', 'max_subframes', 10),
           ('written_max_subsubframes', 'max_subsubframes', 10),
           ('written_misc', 'misc', 30),
           ('written_sumdiff51', 'sumdiff51', 30)] + \
          [(f'written_amode{a}', f'amode{a}', 16) for a in range(10)]


def channel_exprs(layout, content):
    n = CHANNELS[layout]
    if content == 'loud':
        # near full scale and broadband: drives the half IMDCT's pre-shift
        return [f'0.9*(2*random({k})-1)' for k in range(n)]
    sig = []
    for k in range(n):
        if layout.startswith('5.1') and k == 3:
            sig.append('0.5*sin(2*PI*60*t)')            # LFE
            continue
        sig.append(f'(0.15*(2*random({k})-1)+0.25*sin(2*PI*{220 + 110 * k}*t)'
                   f'+0.5*sin(2*PI*1500*t)*lt(mod(t+{0.07 * k:.2f}\\,0.5)\\,0.03))')
    if content == 'sumdiff':
        for a, b in PAIRS[layout]:
            sa, sb = sig[a], sig[b]
            sig[a], sig[b] = f'0.5*({sa}+{sb})', f'0.5*({sa}-{sb})'
    return sig


def encode(out, name, layout, rate, adpcm, content):
    path = os.path.join(out, name + '.dts')
    src = (f"aevalsrc=exprs={'|'.join(channel_exprs(layout, content))}"
           f":c={layout}:s=48000:d={SECONDS}")
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', src,
                    '-c:a', 'dca', '-strict', '-2', '-dca_adpcm', str(adpcm), '-b:a', rate,
                    '-f', 'dts', path], check=True)
    return path


def derive(data, header_bits, adj_index):
    out = bytearray(data)
    n = 0
    for off, fr in dts_ref.frames(bytes(data)):
        if (fr[4] >> 1) & 1:                      # crc_present (bit 38)
            raise SystemExit('gen_dts_fixtures: a frame carries a header CRC')
        for b in header_bits:
            out[off + b // 8] |= 0x80 >> (b % 8)
        if adj_index is not None:
            br = dts_ref.BitReader(fr)
            h = dts_ref.parse_frame_header(br)
            c = dts_ref.parse_coding_header(br, h)
            for pos in c['adj_pos']:
                for k in range(2):
                    bit = pos + k
                    mask = 0x80 >> (bit % 8)
                    if (adj_index >> (1 - k)) & 1:
                        out[off + bit // 8] |= mask
                    else:
                        out[off + bit // 8] &= ~mask
        n += 1
    return bytes(out), n


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        'DTS_TEST_DIR', os.path.expanduser('~/dts-streams/gate'))
    os.makedirs(out, exist_ok=True)
    for name, layout, rate, adpcm, content in ENCODED:
        print(f'gen_dts_fixtures: {encode(out, name, layout, rate, adpcm, content)}')
    for name, base, bits, adj in DERIVED:
        data, n = derive(open(os.path.join(out, base + '.dts'), 'rb').read(), bits, adj)
        path = os.path.join(out, name + '.dts')
        with open(path, 'wb') as f:
            f.write(data)
        print(f'gen_dts_fixtures: {path} ({n} frames; header bits {bits}, adjustment {adj})')
    for name, profile, frames in WRITTEN:
        path = os.path.join(out, name + '.dts')
        n = dts_writer.write_stream(profile, path, frames, seed=1)
        print(f'gen_dts_fixtures: {path} ({frames} frames, {n} bytes, written)')
    for name, *_ in ENCODED:                      # bases are not fixtures themselves
        if name.startswith('_'):
            os.remove(os.path.join(out, name + '.dts'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
