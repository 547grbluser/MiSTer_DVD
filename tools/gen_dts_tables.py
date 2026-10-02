#!/usr/bin/env python3
"""gen_dts_tables.py -- transcribe the DTS core decoder's tables out of a pinned
FFmpeg source tree into tools/dts_tables.py.

The tables are format facts a DTS decoder cannot be built without: the Huffman
codebooks, the two trained VQ codebooks (ADPCM predictor and high-frequency VQ),
scale-factor and quantiser-step tables, and the QMF / LFE interpolation
prototypes. They are transcribed with credit (FFmpeg is LGPL-2.1-or-later,
which may be carried under this project's GPL; see NOTICE and the manual's
acknowledgements). The decoder LOGIC in tools/dts_ref.py is our own code,
written from the specification and from reading FFmpeg, and is checked against
the FFmpeg binary's OUTPUT only. docs/dts_decoder.md sec 6.

Pinned to FFmpeg n9.0.2, the version of the installed oracle binary. The DTS
files are identical in n9.0.1 (diffed 2026-10-02; only dcadec.c's channel
reorder loop differs, and nothing here reads dcadec.c). The pin is by content:
every input file's sha256 is recorded below, and a mismatch is an error, not a
warning, because a silently different table is exactly the bug this tool must
not ship.

Usage:
    FFMPEG_SRC_DIR=<ffmpeg source root> tools/gen_dts_tables.py          # write
    FFMPEG_SRC_DIR=<ffmpeg source root> tools/gen_dts_tables.py --check  # compare

Without FFMPEG_SRC_DIR the default is ~/ffmpeg-9.0.2.
"""
import argparse
import hashlib
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, 'tools', 'dts_tables.py')

# The inputs. Their sha256 pin is tools/dts_tables.sha256; re-pin (--pin) only
# deliberately, and say why in the commit.
PINNED = ('libavcodec/dcadata.c', 'libavcodec/dcahuff.c', 'libavcodec/dca_core.c',
          'libavcodec/dca.c', 'libavcodec/dca_sample_rate_tab.h', 'libavcodec/dcadct.c')
PIN_FILE = os.path.join(REPO, 'tools', 'dts_tables.sha256')

# Core codebook layout of ff_dca_vlc_src_tables, in the order ff_dca_init_vlcs()
# consumes it. Everything after these 2,709 entries belongs to the LBR
# extension, which the core decoder never reads.
CODE_BOOKS = 10
BITALLOC_12_COUNT = 5
SCALE_FACTOR_BOOKS = 5
TMODE_BOOKS = 4


def strip_comments(s):
    s = re.sub(r'/\*.*?\*/', ' ', s, flags=re.S)
    return re.sub(r'//[^\n]*', ' ', s)


NUM = re.compile(r'-?(?:0[xX][0-9a-fA-F]+|\d+\.\d*(?:[eE][-+]?\d+)?f?|'
                 r'\d*\.\d+(?:[eE][-+]?\d+)?f?|\d+(?:[eE][-+]?\d+)f?|\d+)')


def parse_num(t):
    t = t.rstrip('fF')
    if re.fullmatch(r'-?0[xX][0-9a-fA-F]+', t):
        return int(t, 16)
    if re.fullmatch(r'-?\d+', t):
        return int(t)
    return float(t)


def c_array(src, name):
    """The flat list of numbers in `name[...] = { ... };` (nesting flattened)."""
    # `name[..] = {` or, for DECLARE_ALIGNED(n, type, name)[..] = {, `name)[..] = {`
    m = re.search(r'\b' + re.escape(name) + r'\)?\s*(\[[^\]]*\]\s*)+=\s*\{', src)
    if not m:
        raise SystemExit(f'gen_dts_tables: array {name} not found')
    i, depth = m.end() - 1, 0
    for k in range(i, len(src)):
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                break
    body = src[i:k + 1]
    return [parse_num(t) for t in NUM.findall(body)]


def c_array_in_func(src, func, name):
    """A function-local `static const ... name[..] = {..}` inside `func` (dcadct.c
    reuses the name cos_mod in every function)."""
    m = re.search(r'\b' + re.escape(func) + r'\s*\(', src)
    if not m:
        raise SystemExit(f'gen_dts_tables: function {func} not found')
    return c_array(src[m.end():], name)


def rows(flat, width):
    assert len(flat) % width == 0, (len(flat), width)
    return [flat[i:i + width] for i in range(0, len(flat), width)]


def assign_codes(entries):
    """FFmpeg's ff_vlc_init_from_lengths(): walk the (symbol, len) list in
    order, giving each entry the next code of its length, MSB first.
    -> [(code, len, symbol)] in list order. Fails on an invalid list."""
    code, out = 0, []
    for sym, ln in entries:
        if ln <= 0:
            raise SystemExit('gen_dts_tables: a hole in a core codebook')
        if code & ((1 << (32 - ln)) - 1):
            raise SystemExit('gen_dts_tables: invalid VLC list')
        out.append((code >> (32 - ln), ln, sym))
        code += 1 << (32 - ln)
    if code != 1 << 32:
        raise SystemExit('gen_dts_tables: incomplete core codebook')
    return out


def is_canonical(coded):
    """True if a decoder can walk the code one bit at a time with per-length
    (first, count) alone: within each length the codes are one contiguous run,
    and first[L] == (first[L-1] + count[L-1]) << 1 for every L."""
    by_len = {}
    for code, ln, _ in coded:
        by_len.setdefault(ln, []).append(code)
    first = 0
    for ln in range(1, max(by_len) + 1):
        codes = sorted(by_len.get(ln, []))
        if codes:
            if codes[0] != first or codes != list(range(first, first + len(codes))):
                return False
        first = (first + len(codes)) << 1
    return True


def generate(root):
    def rd(rel):
        return open(os.path.join(root, rel), 'rb').read()

    hashes = {rel: hashlib.sha256(rd(rel)).hexdigest() for rel in PINNED}
    data = strip_comments(rd('libavcodec/dcadata.c').decode())
    huff = strip_comments(rd('libavcodec/dcahuff.c').decode())
    core = strip_comments(rd('libavcodec/dca_core.c').decode())
    dca = strip_comments(rd('libavcodec/dca.c').decode())
    srt = strip_comments(rd('libavcodec/dca_sample_rate_tab.h').decode())
    dct = strip_comments(rd('libavcodec/dcadct.c').decode())

    t = {}
    # --- header / layout ---
    t['SAMPLE_RATES'] = c_array(srt, 'ff_dca_sample_rates')
    t['BIT_RATES'] = c_array(data, 'ff_dca_bit_rates')
    t['CHANNELS'] = c_array(data, 'ff_dca_channels')
    t['DMIX_PRIMARY_NCH'] = c_array(data, 'ff_dca_dmix_primary_nch')
    t['BITS_PER_SAMPLE'] = c_array(dca, 'ff_dca_bits_per_sample')
    t['BLOCK_CODE_NBITS'] = c_array(core, 'block_code_nbits')
    # --- quantisation ---
    t['QUANT_INDEX_SEL_NBITS'] = c_array(data, 'ff_dca_quant_index_sel_nbits')
    t['QUANT_INDEX_GROUP_SIZE'] = c_array(data, 'ff_dca_quant_index_group_size')
    t['SCALE_FACTOR_QUANT6'] = c_array(data, 'ff_dca_scale_factor_quant6')
    t['SCALE_FACTOR_QUANT7'] = c_array(data, 'ff_dca_scale_factor_quant7')
    t['JOINT_SCALE_FACTORS'] = c_array(data, 'ff_dca_joint_scale_factors')
    t['SCALE_FACTOR_ADJ'] = c_array(data, 'ff_dca_scale_factor_adj')
    t['QUANT_LEVELS'] = c_array(data, 'ff_dca_quant_levels')
    t['LOSSY_QUANT'] = c_array(data, 'ff_dca_lossy_quant')
    t['LOSSLESS_QUANT'] = c_array(data, 'ff_dca_lossless_quant')
    # --- the two trained codebooks (docs/dts_decoder.md D4) ---
    t['ADPCM_VB'] = rows(c_array(data, 'ff_dca_adpcm_vb'), 4)
    t['HIGH_FREQ_VQ'] = rows(c_array(data, 'ff_dca_high_freq_vq'), 32)
    # --- synthesis prototypes ---
    t['FIR_32BANDS_PERFECT'] = c_array(data, 'ff_dca_fir_32bands_perfect')
    t['FIR_32BANDS_NONPERFECT'] = c_array(data, 'ff_dca_fir_32bands_nonperfect')
    t['LFE_FIR_64'] = c_array(data, 'ff_dca_lfe_fir_64')
    t['LFE_FIR_128'] = c_array(data, 'ff_dca_lfe_fir_128')
    t['FIR_32BANDS_PERFECT_FIXED'] = c_array(data, 'ff_dca_fir_32bands_perfect_fixed')
    t['FIR_32BANDS_NONPERFECT_FIXED'] = c_array(data, 'ff_dca_fir_32bands_nonperfect_fixed')
    t['LFE_FIR_64_FIXED'] = c_array(data, 'ff_dca_lfe_fir_64_fixed')
    # --- the fixed-point half IMDCT of the 32-band synthesis (dcadct.c) ---
    t['DCT_A_COS'] = rows(c_array_in_func(dct, 'dct_a', 'cos_mod'), 8)
    t['DCT_B_COS'] = rows(c_array_in_func(dct, 'dct_b', 'cos_mod'), 7)
    t['MOD_A_COS'] = c_array_in_func(dct, 'mod_a', 'cos_mod')
    t['MOD_B_COS'] = c_array_in_func(dct, 'mod_b', 'cos_mod')
    t['MOD_C_COS'] = c_array_in_func(dct, 'mod_c', 'cos_mod')
    # --- embedded downmix coefficients ---
    t['DMIXTABLE'] = c_array(data, 'ff_dca_dmixtable')
    t['INV_DMIXTABLE'] = c_array(data, 'ff_dca_inv_dmixtable')

    # --- Huffman codebooks ---
    sizes = c_array(huff, 'ff_dca_bitalloc_sizes')
    offsets = c_array(huff, 'ff_dca_bitalloc_offsets')
    src = rows(c_array(huff, 'ff_dca_vlc_src_tables'), 2)
    books, pos = {}, 0

    def take(name, n, offset):
        nonlocal pos
        ent = [(s, ln) for s, ln in src[pos:pos + n]]
        pos += n
        coded = assign_codes(ent)
        books[name] = {
            'offset': offset,
            'codes': [(c, ln, s + offset) for c, ln, s in coded],
            'canonical': is_canonical(coded),
        }

    for i in range(CODE_BOOKS):
        for j in range(t['QUANT_INDEX_GROUP_SIZE'][i]):
            take(f'quant_index_{i}_{j}', sizes[i], offsets[i])
    for i in range(BITALLOC_12_COUNT):
        take(f'bit_allocation_{i}', 12, 1)
    for i in range(SCALE_FACTOR_BOOKS):
        take(f'scale_factor_{i}', 129, -64)
    for i in range(TMODE_BOOKS):
        take(f'transition_mode_{i}', 4, 0)
    t['CORE_VLC_SYMBOLS'] = pos
    t['VLC'] = books
    t['BITALLOC_SIZES'] = sizes
    t['BITALLOC_OFFSETS'] = offsets
    return t, hashes


def render(t, hashes):
    out = ['# GENERATED by tools/gen_dts_tables.py -- do not edit.',
           '# Transcribed from FFmpeg (LGPL-2.1-or-later), with credit: see the',
           '# generator header and docs/dts_decoder.md sec 6. Input sha256:']
    for rel, h in sorted(hashes.items()):
        out.append(f'#   {h}  {rel}')
    out.append('')
    for k in sorted(t):
        if k == 'VLC':
            continue
        out.append(f'{k} = {t[k]!r}')
    out.append('')
    out.append('# VLC[name] = {"offset", "canonical", "codes": [(code, len, symbol)]}')
    out.append('# codes in FFmpeg list order; symbol already includes the offset.')
    out.append('VLC = {')
    for name in t['VLC']:
        b = t['VLC'][name]
        out.append(f'    {name!r}: {{"offset": {b["offset"]}, '
                   f'"canonical": {b["canonical"]}, "codes": {b["codes"]!r}}},')
    out.append('}')
    return '\n'.join(out) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true',
                    help='regenerate in memory and compare with tools/dts_tables.py')
    ap.add_argument('--pin', action='store_true',
                    help='record the current inputs\' hashes as the pin (deliberate re-pin only)')
    args = ap.parse_args()
    root = os.environ.get('FFMPEG_SRC_DIR', os.path.expanduser('~/ffmpeg-9.0.2'))
    if not os.path.isdir(os.path.join(root, 'libavcodec')):
        print(f'gen_dts_tables: no FFmpeg source at {root} (set FFMPEG_SRC_DIR)',
              file=sys.stderr)
        return 2
    t, hashes = generate(root)

    if args.pin:
        with open(PIN_FILE, 'w') as f:
            for rel, h in sorted(hashes.items()):
                f.write(f'{h}  {rel}\n')
        print(f'gen_dts_tables: pinned {len(hashes)} inputs -> {os.path.relpath(PIN_FILE, REPO)}')
    pinned = {}
    for line in open(PIN_FILE):
        h, rel = line.split()
        pinned[rel] = h
    bad = [rel for rel in hashes if pinned.get(rel) != hashes[rel]]
    if bad:
        print('gen_dts_tables: FAIL -- inputs differ from the pin: ' + ', '.join(bad),
              file=sys.stderr)
        return 1

    text = render(t, hashes)
    noncanon = [n for n, b in t['VLC'].items() if not b['canonical']]
    print(f'gen_dts_tables: {len(t["VLC"])} core codebooks, '
          f'{t["CORE_VLC_SYMBOLS"]} symbols, max code length '
          f'{max(ln for b in t["VLC"].values() for _, ln, _ in b["codes"])}; '
          f'{len(noncanon)} not canonical' + (': ' + ', '.join(noncanon) if noncanon else ''))
    if args.check:
        cur = open(OUT).read() if os.path.exists(OUT) else ''
        if cur != text:
            print('gen_dts_tables: FAIL -- tools/dts_tables.py differs from a regeneration',
                  file=sys.stderr)
            return 1
        print('gen_dts_tables: PASS -- tools/dts_tables.py matches the pinned source')
        return 0
    with open(OUT, 'w') as f:
        f.write(text)
    print(f'gen_dts_tables: wrote {os.path.relpath(OUT, REPO)} ({len(text)} bytes)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
