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

Usage: tools/gen_dts_fixtures.py [OUT_DIR] [--discs]
       (default OUT_DIR: $DTS_TEST_DIR or ~/dts-streams/gate)

--discs also rebuilds the gate set's disc windows from the local library
($DVD_ISO_DIR), each chosen by tools/dts_scan.py for what it covers (sec 9 of
docs/dts_decoder.md), plus the Terminator 2 sample from $DTS_T2_SAMPLE. They
are rips of commercial discs: local only, never committed.
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


# The gate set's disc windows: (fixture, image, VTS, substream, the dts_scan
# feature whose best window is taken). One window of 500 sectors among 8 spread
# across the title VOBS, exactly as dts_scan.py --windows 8 --extract picks it.
DISC_WINDOWS = [
    ('disc_xch_beastmaster', 'BEAST_MASTER_20260814_034145.iso', 1, 0x89, 'transient'),
    ('disc_dmix_cinderella3', 'CINDERELLA_III_20260820_183206.iso', 1, 0x8b, 'vq_bands'),
    ('disc_es_castaway', 'CASTAWAY_DTS_20260923_140809.iso', 3, 0x88, 'vq_bands'),
    ('disc_1536k_transient_ai', 'AI_20260807_154822.iso', 3, 0x89, 'transient'),
    ('disc_768k_vq_transient_museum', 'A_NIGHT_AT_THE_MUSEUM_D1_FF_20260921_124311.iso', 5,
     0x89, 'transient'),
]
# Shadoan (the D5 overflowed block codes): the first 3,000 sectors of VTS 8's title
# VOBS, substream 0x89 -- a longer window than dts_scan's, for the statistics.
SHADOAN = ('disc_blockoverflow_shadoan', 'SHADOAN_1_2.iso', 8, 0x89, 3000)


def find_image(name):
    import glob
    root = os.environ.get('DVD_ISO_DIR', os.path.expanduser('~/dvd-isos'))
    hits = glob.glob(os.path.join(root, '**', name), recursive=True)
    return hits[0] if hits else None


def disc_windows(out):
    import shutil
    import struct
    import tempfile
    import types
    import dts_scan
    from dvd_vm_ref import IsoNav
    for fixture, image, vts, ssid, feat in DISC_WINDOWS:
        path = find_image(image)
        if not path:
            print(f'gen_dts_fixtures: SKIP {fixture}: {image} not under $DVD_ISO_DIR')
            continue
        with tempfile.TemporaryDirectory() as tmp:
            args = types.SimpleNamespace(windows=8, window=500, extract=tmp, want={feat})
            dts_scan.scan_iso(path, args)
            src = os.path.join(tmp, f'{os.path.splitext(image)[0]}_vts{vts:02d}_{ssid:02x}_{feat}.dts')
            if not os.path.exists(src):
                print(f'gen_dts_fixtures: SKIP {fixture}: no {feat} window in VTS {vts}')
                continue
            shutil.copy(src, os.path.join(out, fixture + '.dts'))
            print(f'gen_dts_fixtures: {fixture} <- {image} VTS {vts} 0x{ssid:02x} ({feat})')
    fixture, image, vts, ssid, nsec = SHADOAN
    path = find_image(image)
    if path:
        nav = IsoNav(path)
        lba = nav.vts_ifo[vts]
        mat = nav.sec(lba)
        tt = struct.unpack('>I', mat[0xC4:0xC8])[0]
        last = struct.unpack('>I', mat[0x0C:0x10])[0]
        data = dts_scan.dts_payloads(nav.f, lba + tt, min(nsec, last - tt - 100)).get(ssid, b'')
        with open(os.path.join(out, fixture + '.dts'), 'wb') as f:
            f.write(bytes(data))
        print(f'gen_dts_fixtures: {fixture} <- {image} VTS {vts} 0x{ssid:02x} ({nsec} sectors)')
    else:
        print(f'gen_dts_fixtures: SKIP {fixture}: {image} not under $DVD_ISO_DIR')
    t2 = os.environ.get('DTS_T2_SAMPLE')
    if t2 and os.path.exists(t2):
        shutil.copy(t2, os.path.join(out, 'disc_t2_sample.dts'))
        print('gen_dts_fixtures: disc_t2_sample <- $DTS_T2_SAMPLE')
    else:
        print('gen_dts_fixtures: SKIP disc_t2_sample: set DTS_T2_SAMPLE to a raw .dts')


def main():
    pos = [a for a in sys.argv[1:] if not a.startswith('--')]
    out = pos[0] if pos else os.environ.get(
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
    if '--discs' in sys.argv:
        disc_windows(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
