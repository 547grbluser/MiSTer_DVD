#!/usr/bin/env python3
"""dts_writer.py -- a syntax-level DTS core ENCODER for test fixtures.

It writes frames from a structured description of every field tools/dts_ref.py
parses, in the same order, so it can make streams no disc and no encoder
produces (docs/dts_decoder.md sec 9):

  - joint intensity coding (no disc in the library, not FFmpeg's encoder);
  - the spec maxima of the frame's shape: npcmblocks 128 (4,096 PCM samples a
    frame), 16 subframes, 4 subsubframes, 32 subbands, frames near 16 KB;
  - every channel arrangement (AMODE 0-9), every bit-allocation, scale-factor,
    transient and quantiser-index codebook selector, bit allocations up to 26,
    the lossless step table, header CRC words, DRC, time code and auxiliary
    data with embedded downmix coefficients (and its CRC), predictor history
    off, sync_ssf.

It is not an audio encoder: the coded values are chosen by a seeded generator,
legal and moderate in level (nothing clips, so the downmix comparison of
tools/test_dts_fixed.py stays meaningful), and the decoders are judged on
whether they agree on them -- tools/dts_ref.py against the FFmpeg binary, bit
for bit. A written frame is also parsed back here and every field compared, so
the writer and the parser check each other.

Usage:
    tools/dts_writer.py <profile> <out.dts> [--frames N] [--seed S]
    profiles: joint, max_subframes, max_subsubframes, amode0 .. amode9, misc
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dts_ref as R                                              # noqa: E402
import dts_tables as T                                           # noqa: E402

SR_48K = 13                                   # sample-rate code for 48 kHz
BR_POS = 32 + 1 + 5 + 1 + 7 + 14 + 6 + 4         # the bit-rate field's position
LOSSLESS_BR = 31                              # bit-rate code whose value is 3


class BitWriter:
    def __init__(self):
        self.v = 0
        self.n = 0

    def put(self, val, n):
        if n == 0:
            return
        if val < 0:
            val &= (1 << n) - 1
        assert 0 <= val < (1 << n), (val, n)
        self.v = (self.v << n) | val
        self.n += n

    def vlc(self, book, sym):
        code, ln = ENC[book][sym]
        self.put(code, ln)

    def align(self, k):
        self.put(0, (-self.n) % k)

    def bytes(self):
        pad = (-self.n) % 8
        return (self.v << pad).to_bytes((self.n + pad) // 8, 'big')


# symbol -> (code, length) for every core codebook
ENC = {name: {s: (c, ln) for c, ln, s in b['codes']} for name, b in T.VLC.items()}


# --------------------------------------------------------------------------
# Serialisation: the mirror of dts_ref's parse, field for field.
# --------------------------------------------------------------------------
def write_frame(f):
    h, c = f['h'], f['c']
    w = BitWriter()
    w.put(R.SYNC_CORE_BE, 32)
    w.put(1, 1)                                   # normal frame
    w.put(R.PCMBLOCK_SAMPLES - 1, 5)              # deficit samples
    w.put(h['crc_present'], 1)
    w.put(h['npcmblocks'] - 1, 7)
    fsize_pos = w.n
    w.put(0, 14)                                  # frame size, patched below
    w.put(h['audio_mode'], 6)
    w.put(SR_48K, 4)
    w.put(0, 5)                                   # bit rate, patched below
    w.put(0, 1)                                   # reserved
    for k in ('drc_present', 'ts_present', 'aux_present'):
        w.put(h[k], 1)
    w.put(0, 1)                                   # HDCD
    w.put(0, 3)                                   # extension type
    w.put(0, 1)                                   # no extension
    w.put(h['sync_ssf'], 1)
    w.put(h['lfe_present'], 2)
    w.put(h['predictor_history'], 1)
    if h['crc_present']:
        w.put(0, 16)                              # header CRC (skipped by decoders)
    w.put(h['filter_perfect'], 1)
    w.put(h['encoder_rev'], 4)
    w.put(0, 2)                                   # copy history
    w.put(h['pcmr_code'], 3)
    w.put(h['sumdiff_front'], 1)
    w.put(h['sumdiff_surround'], 1)
    w.put(0, 4)                                   # dialogue normalisation
    # primary audio coding header
    nch = c['nchannels']
    w.put(c['nsubframes'] - 1, 4)
    w.put(nch - 1, 3)
    for ch in range(nch):
        w.put(c['nsubbands'][ch] - 2, 5)
    for ch in range(nch):
        w.put(c['vq_start'][ch] - 1, 5)
    for ch in range(nch):
        w.put(c['joint_index'][ch], 3)
    for k, nb in (('tmode_sel', 2), ('scale_sel', 3), ('balloc_sel', 3)):
        for ch in range(nch):
            w.put(c[k][ch], nb)
    for n in range(R.CODE_BOOKS):
        for ch in range(nch):
            w.put(c['quant_sel'][ch][n], T.QUANT_INDEX_SEL_NBITS[n])
    for n in range(R.CODE_BOOKS):
        for ch in range(nch):
            if c['quant_sel'][ch][n] < T.QUANT_INDEX_GROUP_SIZE[n]:
                w.put(c['adj_index'][ch][n], 2)
    if h['crc_present']:
        w.put(0, 16)
    for s in f['subframes']:
        write_subframe(w, h, c, s)
    if h['ts_present']:
        w.put(f['timecode'], 32)
    if h['aux_present']:
        write_aux(w, h, f['aux'])
    data = bytearray(w.bytes())
    size = f.get('frame_size') or len(data) + (-len(data)) % 4
    assert len(data) <= size <= 16384, (len(data), size)
    data += bytes(size - len(data))
    v = int.from_bytes(data, 'big')
    nb = len(data) * 8

    def patch(v, pos, width, val):
        sh = nb - pos - width
        return (v & ~(((1 << width) - 1) << sh)) | (val << sh)
    v = patch(v, fsize_pos, 14, size - 1)          # the frame size
    br = h['br_code']
    if br is None:                                 # the bit rate the frames really carry
        rate = size * 8 * 48000 // (h['npcmblocks'] * R.PCMBLOCK_SAMPLES)
        br = next((i for i, r in enumerate(T.BIT_RATES[:29]) if r >= rate), 28)
    v = patch(v, BR_POS, 5, br)
    return v.to_bytes(len(data), 'big')


def write_scale(w, sel, prev, idx):
    if sel < 5:
        w.vlc(f'scale_factor_{sel}', idx - prev)
    else:
        w.put(idx, sel + 1)
    return idx


def write_subframe(w, h, c, s):
    nch = c['nchannels']
    w.put(s['nssf'] - 1, 2)
    w.put(0, 3)                                   # partial subsubframe count
    for ch in range(nch):
        for b in range(c['nsubbands'][ch]):
            w.put(s['pmode'][ch][b], 1)
    for ch in range(nch):
        for b in range(c['nsubbands'][ch]):
            if s['pmode'][ch][b]:
                w.put(s['pvq'][ch][b], 12)
    for ch in range(nch):
        sel = c['balloc_sel'][ch]
        for b in range(c['vq_start'][ch]):
            a = s['abits'][ch][b]
            if sel < 5:
                w.vlc(f'bit_allocation_{sel}', a)
            else:
                w.put(a, sel - 1)
    if s['nssf'] > 1:
        for ch in range(nch):
            for b in range(c['vq_start'][ch]):
                if s['abits'][ch][b]:
                    w.vlc(f'transition_mode_{c["tmode_sel"][ch]}', s['tmode'][ch][b])
    for ch in range(nch):
        sel, prev = c['scale_sel'][ch], 0
        for b in range(c['vq_start'][ch]):
            if s['abits'][ch][b]:
                prev = write_scale(w, sel, prev, s['scale_idx'][ch][b][0])
                if s['tmode'][ch][b]:
                    prev = write_scale(w, sel, prev, s['scale_idx'][ch][b][1])
        for b in range(c['vq_start'][ch], c['nsubbands'][ch]):
            prev = write_scale(w, sel, prev, s['scale_idx'][ch][b][0])
    for ch in range(nch):
        if c['joint_index'][ch]:
            w.put(s['jsel'][ch], 3)
    for ch in range(nch):
        src = c['joint_index'][ch] - 1
        if src >= 0:
            sel = s['jsel'][ch]
            for b in range(c['nsubbands'][ch], c['nsubbands'][src]):
                v = s['jscale'][ch][b]               # the coded value; index = v + 64
                if sel < 5:
                    w.vlc(f'scale_factor_{sel}', v)
                else:
                    w.put(v, sel + 1)
    if h['drc_present']:
        w.put(s['drc'], 8)
    if h['crc_present']:
        w.put(0, 16)
    # audio data
    for ch in range(nch):
        for b in range(c['vq_start'][ch], c['nsubbands'][ch]):
            w.put(s['vq'][ch][b], 10)
    if h['lfe_present']:
        for q in s['lfe_q']:
            w.put(q, 8)
        w.put(s['lfe_scale'], 8)
    for ssf in range(s['nssf']):
        for ch in range(nch):
            for b in range(c['vq_start'][ch]):
                write_samples(w, c, ch, s['abits'][ch][b], s['q'][ssf][ch][b])
        if ssf == s['nssf'] - 1 or h['sync_ssf']:
            w.put(0xFFFF, 16)


def write_samples(w, c, ch, a, q):
    if a == 0:
        return
    if a <= R.CODE_BOOKS:
        sel = c['quant_sel'][ch][a - 1]
        if sel < T.QUANT_INDEX_GROUP_SIZE[a - 1]:
            for v in q:
                w.vlc(f'quant_index_{a - 1}_{sel}', v)
            return
        if a <= 7:
            levels, nb = T.QUANT_LEVELS[a], T.BLOCK_CODE_NBITS[a - 1]
            off = (levels - 1) // 2
            for half in (q[:4], q[4:]):
                code = 0
                for v in reversed(half):
                    code = code * levels + (v + off)
                w.put(code, nb)
            return
    for v in q:
        w.put(v, a - 3)


def write_aux(w, h, aux):
    w.put(0, 6)                                   # byte count (decoders ignore it)
    w.align(32)
    w.put(R.SYNC_AUX, 32)
    start = w.n
    w.put(0, 1)                                   # no decode time stamp
    w.put(1 if aux.get('dmix') else 0, 1)
    if aux.get('dmix'):
        w.put(aux['dmix_type'], 3)
        for code in aux['dmix']:
            w.put(code, 9)
    w.align(8)
    # CRC-16/CCITT over the aux payload, appended so the whole checks to zero
    nbytes = (w.n - start) // 8
    payload = (w.v & ((1 << (w.n - start)) - 1)).to_bytes(nbytes, 'big')
    w.put(R.crc16_ccitt(payload), 16)


# --------------------------------------------------------------------------
# A seeded generator of legal, moderate-level frames.
# --------------------------------------------------------------------------
STABLE_PVQ = [i for i, v in enumerate(T.ADPCM_VB) if sum(abs(x) for x in v) < 7800]


def book_range(name):
    syms = list(ENC[name])
    return min(syms), max(syms)


def gen_samples(rng, c, ch, a):
    if a == 0:
        return [0] * 8
    if a <= R.CODE_BOOKS:
        sel = c['quant_sel'][ch][a - 1]
        if sel < T.QUANT_INDEX_GROUP_SIZE[a - 1]:
            lo, hi = book_range(f'quant_index_{a - 1}_{sel}')
            return [rng.randint(lo, hi) for _ in range(8)]
        if a <= 7:
            off = (T.QUANT_LEVELS[a] - 1) // 2
            return [rng.randint(-off, off) for _ in range(8)]
    m = (1 << (a - 4)) - 1
    return [rng.randint(-m, m) for _ in range(8)]


def gen_frame(rng, p):
    """p: profile dict. -> a frame description."""
    amode = p['amode']
    nch = T.CHANNELS[amode]
    npb = p['npcmblocks']
    h = {'crc_present': p.get('crc', 0), 'npcmblocks': npb, 'audio_mode': amode,
         'br_code': p.get('br_code'), 'drc_present': p.get('drc', 0),
         'ts_present': p.get('ts', 0), 'aux_present': 1 if p.get('aux') else 0,
         'sync_ssf': p.get('sync_ssf', 0), 'lfe_present': p.get('lfe', 0),
         'predictor_history': p.get('pred_hist', 1),
         'filter_perfect': p.get('perfect', rng.randint(0, 1)),
         'encoder_rev': 7, 'pcmr_code': p.get('pcmr', 0),
         'sumdiff_front': p.get('sumdiff', 0), 'sumdiff_surround': p.get('sumdiff', 0)}
    nsub = [rng.randint(p.get('min_bands', 8), 32) for _ in range(nch)]
    joint = [0] * nch
    for ch, src in p.get('joint', []):            # (channel, source channel)
        if ch < nch and src < nch:
            joint[ch] = src + 1
            nsub[src] = max(nsub[src], 24)
            nsub[ch] = rng.randint(4, nsub[src] - 4)
    vq_start = [rng.randint(max(1, n - p.get('vq_max', 6)), n) if p.get('vq', 1) else n
                for n in nsub]
    c = {'nsubframes': len(p['ssf_plan']), 'nchannels': nch, 'nsubbands': nsub,
         'vq_start': vq_start, 'joint_index': joint,
         'tmode_sel': [rng.randint(0, 3) for _ in range(nch)],
         'scale_sel': [rng.choice(p.get('scale_sels', range(7))) for _ in range(nch)],
         'balloc_sel': [rng.choice(p.get('balloc_sels', range(7))) for _ in range(nch)]}
    c['quant_sel'] = [[rng.randint(0, (1 << T.QUANT_INDEX_SEL_NBITS[n]) - 1)
                       for n in range(R.CODE_BOOKS)] for _ in range(nch)]
    c['adj_index'] = [[rng.randint(0, 3) for _ in range(R.CODE_BOOKS)] for _ in range(nch)]
    lo_idx, hi_idx = p.get('scale_range', (24, 40))
    subframes = []
    for nssf in p['ssf_plan']:
        s = {'nssf': nssf, 'pmode': [], 'pvq': [], 'abits': [], 'tmode': [],
             'scale_idx': [], 'jsel': [0] * nch, 'jscale': [], 'vq': [],
             'drc': rng.randint(0, 255)}
        for ch in range(nch):
            pm = [1 if rng.random() < p.get('pred', 0.15) else 0 for _ in range(nsub[ch])]
            s['pmode'].append(pm)
            s['pvq'].append([rng.choice(STABLE_PVQ) if x else 0 for x in pm])
            sel = c['balloc_sel'][ch]
            amax = 12 if sel < 5 else min((1 << (sel - 1)) - 1, p.get('abits_max', 26))
            amin = 1 if sel < 5 else 0
            ab = [rng.randint(amin, amax) for _ in range(vq_start[ch])]
            s['abits'].append(ab)
            tm = [rng.randint(0, nssf - 1) if (nssf > 1 and a) else 0 for a in ab] + \
                 [0] * (32 - vq_start[ch])
            s['tmode'].append(tm)
            # scale indices: table range by selector, kept moderate
            ssel = c['scale_sel'][ch]
            span = (lo_idx, hi_idx) if ssel != 6 else (2 * lo_idx, 2 * hi_idx)
            s['scale_idx'].append([[rng.randint(*span), rng.randint(*span)] for _ in range(32)])
            s['vq'].append([rng.randint(0, 1023) for _ in range(32)])
        for ch in range(nch):
            js = rng.randint(0, 6)
            s['jsel'][ch] = js
            rng_v = (-10, 10) if js < 5 else (0, 10) if js == 5 else (0, 10)
            s['jscale'].append([rng.randint(*rng_v) for _ in range(32)])
        if h['lfe_present']:
            s['lfe_q'] = [rng.randint(-60, 60) for _ in range(2 * h['lfe_present'] * nssf)]
            s['lfe_scale'] = rng.randint(lo_idx, hi_idx)
        s['q'] = [[[gen_samples(rng, c, ch, s['abits'][ch][b]) for b in range(vq_start[ch])]
                   for ch in range(nch)] for _ in range(nssf)]
        subframes.append(s)
    f = {'h': h, 'c': c, 'subframes': subframes, 'timecode': rng.getrandbits(32)}
    if p.get('aux'):
        dt = rng.randint(0, 6)
        n = nch + (1 if h['lfe_present'] else 0)
        f['aux'] = {'dmix_type': dt,
                    'dmix': [rng.randint(0, 241) | (rng.randint(0, 1) << 8)
                             for _ in range(T.DMIX_PRIMARY_NCH[dt] * n)]}
    return f


def plan(npb, nsubframes):
    """Split npcmblocks/8 subsubframes over nsubframes, each 1..4."""
    total = npb // 8
    base = [1] * nsubframes
    left = total - nsubframes
    i = 0
    while left > 0:
        if base[i] < 4:
            base[i] += 1
            left -= 1
        i = (i + 1) % nsubframes
    assert sum(base) == total and all(1 <= x <= 4 for x in base), (npb, nsubframes)
    return base


PROFILES = {
    # joint intensity: Rs from Ls, Ls from L, R from C (several shapes at once)
    'joint': dict(amode=9, lfe=2, npcmblocks=16, ssf_plan=[2],
                  joint=[(4, 3), (3, 1), (2, 0)], pred=0.1),
    # the frame-shape maxima: 128 blocks as 16 subframes of 1, and as 4 of 4
    'max_subframes': dict(amode=9, lfe=2, npcmblocks=128, ssf_plan=plan(128, 16),
                          abits_max=10, scale_range=(22, 34), min_bands=20),
    'max_subsubframes': dict(amode=9, lfe=2, npcmblocks=128, ssf_plan=plan(128, 4),
                             sync_ssf=1, abits_max=10, scale_range=(22, 34), min_bands=20),
    # everything else the discs never set
    'misc': dict(amode=9, lfe=2, npcmblocks=32, ssf_plan=[4], crc=1, drc=1, ts=1, aux=1,
                 pred_hist=0, br_code=LOSSLESS_BR, pcmr=5, sync_ssf=1),
}
for _a in range(10):
    PROFILES[f'amode{_a}'] = dict(amode=_a, lfe=2 if _a >= 5 else 0, npcmblocks=16,
                                  ssf_plan=[2])


def roundtrip(data, f):
    """Parse a written frame back and compare the fields that matter."""
    br = R.BitReader(data)
    h = R.parse_frame_header(br)
    c = R.parse_coding_header(br, h)
    for k in ('npcmblocks', 'audio_mode', 'lfe_present', 'filter_perfect', 'crc_present',
              'sumdiff_front', 'sumdiff_surround', 'predictor_history', 'sync_ssf'):
        assert h[k] == f['h'][k], k
    assert h['frame_size'] == len(data)
    for k in ('nsubbands', 'vq_start', 'joint_index', 'quant_sel'):
        assert c[k] == f['c'][k], k
    for s in f['subframes']:
        p = R.parse_subframe_header(br, h, c)
        assert p['nssf'] == s['nssf'] and p['pmode'] == s['pmode']
        assert p['abits'] == s['abits'], 'abits'
        # skip the audio data the way the decoder reads it
        for ch in range(c['nchannels']):
            for b in range(c['vq_start'][ch], c['nsubbands'][ch]):
                assert br.bits(10) == s['vq'][ch][b]
        if h['lfe_present']:
            br.bits(8 * len(s['lfe_q']) + 8)
        for ssf in range(s['nssf']):
            for ch in range(c['nchannels']):
                for b in range(c['vq_start'][ch]):
                    q, _ = R.extract_audio(br, c, ch, s['abits'][ch][b])
                    assert q == s['q'][ssf][ch][b], ('samples', ssf, ch, b)
            if ssf == s['nssf'] - 1 or h['sync_ssf']:
                assert br.bits(16) == 0xFFFF
    o = R.parse_optional_info(br, h, c)
    if h['aux_present']:
        assert o['aux_ok'], 'aux CRC'


def write_stream(profile, path, frames, seed):
    rng = random.Random(seed)
    p = PROFILES[profile]
    out = bytearray()
    for _ in range(frames):
        f = gen_frame(rng, p)
        data = write_frame(f)
        roundtrip(data, f)
        out += data
    with open(path, 'wb') as fo:
        fo.write(out)
    return len(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('profile', choices=sorted(PROFILES))
    ap.add_argument('out')
    ap.add_argument('--frames', type=int, default=40)
    ap.add_argument('--seed', type=int, default=1)
    args = ap.parse_args()
    n = write_stream(args.profile, args.out, args.frames, args.seed)
    print(f'dts_writer: {args.profile} -> {args.out} ({args.frames} frames, {n} bytes)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
