#!/usr/bin/env python3
"""dts_ref.py -- reference model of the DTS Coherent Acoustics CORE decoder.

The golden chain's first link (docs/dts_decoder.md sec 6): our own code, written
from the bitstream syntax (ETSI TS 102 114) and from reading FFmpeg's decoder,
and checked against the FFmpeg binary's OUTPUT, never copied from it. The
tables come from tools/dts_tables.py (generated, pinned, transcribed with
credit by tools/gen_dts_tables.py).

This module is the FRONT END in FFmpeg's order -- frame header, primary audio
coding header, per subframe the side information then the audio data, then the
inverse ADPCM, high-frequency VQ and joint intensity per subframe -- producing
the dequantised integer subband samples (the 23-bit domain FFmpeg's fixed path
also works in). Synthesis and the stereo downmix are added on top.

Scope: the core only. Extensions inside the core frame (XCh, X96, XXCH) are
noted and skipped: the frame is consumed to frame_size. A sample-rate,
channel-arrangement or field value the core decoder refuses raises DtsError
with a code, the way the RTL refuses a frame: visibly, never silently.

Usage:
    tools/dts_ref.py info <file.dts>          # stream summary + per-frame checks
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dts_tables as T                                           # noqa: E402

SYNC_CORE_BE = 0x7FFE8001
SYNC_AUX = 0x9A1105A0
SUBBANDS = 32
SUBBAND_SAMPLES = 8          # samples per subband per subsubframe
PCMBLOCK_SAMPLES = 32        # PCM samples per subband sample time (32 bands)
ADPCM_COEFFS = 4
ABITS_MAX = 26
CODE_BOOKS = 10
LFE_HISTORY = 8
AMODE_COUNT = 10             # FFmpeg refuses audio_mode >= 10 (user-defined)
AMODE_STEREO_SUMDIFF = 3
AMODE_2F2R = 8


# Mutation hooks for tools/test_dts_ref.py's RED arms. Each names one stage;
# the test proves the bit-exact comparison FAILS when that stage is broken,
# so a PASS cannot be vacuous. Empty in normal use.
MUT = set()

# Behaviour options. 'lenient_block': a block code past levels^4 keeps its four
# low digits and is counted, as libdca does, instead of refusing the frame as
# FFmpeg does (docs/dts_decoder.md D5). Off by default: the reference matches
# the FFmpeg binary.
OPT = set()
LENIENT_COUNT = [0]
IMDCT_SHIFTS = [0]            # half-IMDCT blocks that took the 2-bit pre-shift


class DtsError(Exception):
    def __init__(self, code, msg):
        super().__init__(f'{code}: {msg}')
        self.code = code


def clip23(a):
    return -(1 << 23) if a < -(1 << 23) else (1 << 23) - 1 if a > (1 << 23) - 1 else a


def norm(a, bits):
    """Round half up, then arithmetic shift (FFmpeg dcamath norm__)."""
    return (a + (1 << (bits - 1))) >> bits if bits > 0 else a


def mul(a, b, bits):
    return norm(a * b, bits)


def log2i(x):
    return x.bit_length() - 1


# --------------------------------------------------------------------------
# Huffman: per codebook, {(length, code): symbol}. The codes are NOT canonical
# (tools/gen_dts_tables.py reports 53 of 62 books), so decode by (length, code)
# lookup, shortest first -- the RTL walks a binary tree instead.
# --------------------------------------------------------------------------
class Codebook:
    def __init__(self, name):
        b = T.VLC[name]
        self.name = name
        self.lens = sorted({ln for _, ln, _ in b['codes']})
        self.map = {(ln, c): s for c, ln, s in b['codes']}
        self.maxlen = self.lens[-1]


_BOOKS = {}


def book(name):
    if name not in _BOOKS:
        _BOOKS[name] = Codebook(name)
    return _BOOKS[name]


class BitReader:
    """MSB-first reader over a whole frame held as one Python int."""

    def __init__(self, data):
        self.data = data
        self.nbits = len(data) * 8
        self.val = int.from_bytes(data, 'big')
        self.pos = 0

    def bits(self, n):
        if n == 0:
            return 0
        if self.pos + n > self.nbits:
            raise DtsError('E_OVERREAD', f'read past the frame ({self.pos}+{n} > {self.nbits})')
        v = (self.val >> (self.nbits - self.pos - n)) & ((1 << n) - 1)
        self.pos += n
        return v

    def sbits(self, n):
        v = self.bits(n)
        return v - (1 << n) if v >> (n - 1) else v

    def peek(self, n):
        avail = self.nbits - self.pos
        if avail >= n:
            return (self.val >> (avail - n)) & ((1 << n) - 1)
        return (self.val & ((1 << avail) - 1)) << (n - avail)

    def vlc(self, cb):
        w = self.peek(cb.maxlen)
        for ln in cb.lens:
            s = cb.map.get((ln, w >> (cb.maxlen - ln)))
            if s is not None:
                self.bits(ln)
                return s
        raise DtsError('E_VLC_HOLE', f'no code in {cb.name}')

    def align(self, n):
        self.pos += (-self.pos) % n


# --------------------------------------------------------------------------
# Frame header (TS 102 114 5.3.1)
# --------------------------------------------------------------------------
def parse_frame_header(br):
    h = {}
    if br.bits(32) != SYNC_CORE_BE:
        raise DtsError('E_SYNC', 'no core sync word')
    h['normal_frame'] = br.bits(1)
    h['deficit_samples'] = br.bits(5) + 1
    if h['deficit_samples'] != PCMBLOCK_SAMPLES:
        raise DtsError('E_DEFICIT', 'deficit samples (termination frame) not supported')
    h['crc_present'] = br.bits(1)
    h['npcmblocks'] = br.bits(7) + 1
    if h['npcmblocks'] % SUBBAND_SAMPLES:
        raise DtsError('E_PCM_BLOCKS', f'npcmblocks {h["npcmblocks"]} not a multiple of 8')
    h['frame_size'] = br.bits(14) + 1
    if h['frame_size'] < 96:
        raise DtsError('E_FRAME_SIZE', f'frame size {h["frame_size"]}')
    h['audio_mode'] = br.bits(6)
    if h['audio_mode'] >= AMODE_COUNT:
        raise DtsError('E_AMODE', f'audio mode {h["audio_mode"]} (user-defined)')
    h['sr_code'] = br.bits(4)
    h['sample_rate'] = T.SAMPLE_RATES[h['sr_code']]
    if not h['sample_rate']:
        raise DtsError('E_SAMPLE_RATE', f'sr_code {h["sr_code"]}')
    h['br_code'] = br.bits(5)
    h['bit_rate'] = T.BIT_RATES[h['br_code']]
    if br.bits(1):
        raise DtsError('E_RESERVED', 'reserved bit set')
    h['drc_present'] = br.bits(1)
    h['ts_present'] = br.bits(1)
    h['aux_present'] = br.bits(1)
    h['hdcd_master'] = br.bits(1)
    h['ext_audio_type'] = br.bits(3)
    h['ext_audio_present'] = br.bits(1)
    h['sync_ssf'] = br.bits(1)
    h['lfe_present'] = br.bits(2)
    if h['lfe_present'] == 3:
        raise DtsError('E_LFE_FLAG', 'invalid LFE flag')
    h['predictor_history'] = br.bits(1)
    if h['crc_present']:
        br.bits(16)
    h['filter_perfect'] = br.bits(1)
    h['encoder_rev'] = br.bits(4)
    h['copy_hist'] = br.bits(2)
    h['pcmr_code'] = br.bits(3)
    if not T.BITS_PER_SAMPLE[h['pcmr_code']]:
        raise DtsError('E_PCM_RES', f'pcmr_code {h["pcmr_code"]}')
    h['es_format'] = h['pcmr_code'] & 1
    h['sumdiff_front'] = br.bits(1)
    h['sumdiff_surround'] = br.bits(1)
    h['dn_code'] = br.bits(4)
    return h


# --------------------------------------------------------------------------
# Primary audio coding header (5.3.2)
# --------------------------------------------------------------------------
def parse_coding_header(br, h):
    c = {}
    c['nsubframes'] = br.bits(4) + 1
    nch = br.bits(3) + 1
    if nch != T.CHANNELS[h['audio_mode']]:
        raise DtsError('E_NCHANNELS', f'{nch} channels for audio mode {h["audio_mode"]}')
    c['nchannels'] = nch
    rng = range(nch)
    c['nsubbands'] = []
    for _ in rng:
        n = br.bits(5) + 2
        if n > SUBBANDS:
            raise DtsError('E_SUBBANDS', f'subband activity {n}')
        c['nsubbands'].append(n)
    c['vq_start'] = [br.bits(5) + 1 for _ in rng]
    c['joint_index'] = []
    for _ in rng:
        n = br.bits(3)
        if n > nch:
            raise DtsError('E_JOINT', f'joint intensity index {n}')
        c['joint_index'].append(n)
    c['tmode_sel'] = [br.bits(2) for _ in rng]
    c['scale_sel'] = []
    for _ in rng:
        v = br.bits(3)
        if v == 7:
            raise DtsError('E_SCALE_SEL', 'scale factor code book 7')
        c['scale_sel'].append(v)
    c['balloc_sel'] = []
    for _ in rng:
        v = br.bits(3)
        if v == 7:
            raise DtsError('E_BALLOC_SEL', 'bit allocation quantizer select 7')
        c['balloc_sel'].append(v)
    qsel = [[0] * CODE_BOOKS for _ in rng]
    for n in range(CODE_BOOKS):
        for ch in rng:
            qsel[ch][n] = br.bits(T.QUANT_INDEX_SEL_NBITS[n])
    c['quant_sel'] = qsel
    adj = [[0] * CODE_BOOKS for _ in rng]
    c['adj_pos'] = []             # bit positions of the 2-bit fields (fixtures rewrite them)
    for n in range(CODE_BOOKS):
        for ch in rng:
            if qsel[ch][n] < T.QUANT_INDEX_GROUP_SIZE[n]:
                c['adj_pos'].append(br.pos)
                adj[ch][n] = T.SCALE_FACTOR_ADJ[br.bits(2)]
    c['scale_adj'] = adj
    if h['crc_present']:
        br.bits(16)
    return c


def parse_scale(br, idx, sel):
    """-> (new running index, scale). sel < 5: Huffman delta; else absolute."""
    table = T.SCALE_FACTOR_QUANT7 if sel > 5 else T.SCALE_FACTOR_QUANT6
    if sel < 5:
        idx += br.vlc(book(f'scale_factor_{sel}'))
    else:
        idx = br.bits(sel + 1)
    if not 0 <= idx < len(table):
        raise DtsError('E_SCALE_INDEX', f'scale index {idx}')
    return idx, table[idx]


def parse_joint_scale(br, sel):
    if sel < 5:
        idx = br.vlc(book(f'scale_factor_{sel}'))
    else:
        idx = br.bits(sel + 1)
    idx += 64
    if not 0 <= idx < len(T.JOINT_SCALE_FACTORS):
        raise DtsError('E_JOINT_SCALE', f'joint scale index {idx}')
    return T.JOINT_SCALE_FACTORS[idx]


# --------------------------------------------------------------------------
# Subframe side information (5.4.1)
# --------------------------------------------------------------------------
def parse_subframe_header(br, h, c):
    s = {}
    nch = c['nchannels']
    s['nssf'] = br.bits(2) + 1
    br.bits(3)                                   # partial subsubframe sample count
    s['pmode'] = [[br.bits(1) for _ in range(c['nsubbands'][ch])] for ch in range(nch)]
    s['pvq'] = [[br.bits(12) if s['pmode'][ch][b] else 0
                 for b in range(c['nsubbands'][ch])] for ch in range(nch)]
    abits = []
    for ch in range(nch):
        sel, row = c['balloc_sel'][ch], []
        for _ in range(c['vq_start'][ch]):
            a = br.vlc(book(f'bit_allocation_{sel}')) if sel < 5 else br.bits(sel - 1)
            if a > ABITS_MAX:
                raise DtsError('E_ABITS', f'bit allocation {a}')
            row.append(a)
        abits.append(row)
    s['abits'] = abits
    tmode = []
    for ch in range(nch):
        row = [0] * SUBBANDS
        if s['nssf'] > 1:
            cb = book(f'transition_mode_{c["tmode_sel"][ch]}')
            for b in range(c['vq_start'][ch]):
                if abits[ch][b]:
                    row[b] = br.vlc(cb)
        tmode.append(row)
    s['tmode'] = tmode
    scales = []
    for ch in range(nch):
        sel, idx = c['scale_sel'][ch], 0
        row = [[0, 0] for _ in range(SUBBANDS)]
        for b in range(c['vq_start'][ch]):
            if abits[ch][b]:
                idx, row[b][0] = parse_scale(br, idx, sel)
                if tmode[ch][b]:
                    idx, row[b][1] = parse_scale(br, idx, sel)
        for b in range(c['vq_start'][ch], c['nsubbands'][ch]):
            idx, row[b][0] = parse_scale(br, idx, sel)
        scales.append(row)
    s['scales'] = scales
    jsel = [0] * nch
    for ch in range(nch):
        if c['joint_index'][ch]:
            jsel[ch] = br.bits(3)
            if jsel[ch] == 7:
                raise DtsError('E_JOINT_SEL', 'joint scale code book 7')
    jscale = [[0] * SUBBANDS for _ in range(nch)]
    for ch in range(nch):
        src = c['joint_index'][ch] - 1
        if src >= 0:
            for b in range(c['nsubbands'][ch], c['nsubbands'][src]):
                jscale[ch][b] = parse_joint_scale(br, jsel[ch])
    s['jscale'] = jscale
    if h['drc_present']:
        s['drc'] = br.bits(8)
    if h['crc_present']:
        br.bits(16)
    return s


def extract_audio(br, c, ch, abits):
    """-> (8 quantised samples, huffman_used)."""
    if abits == 0:
        return [0] * SUBBAND_SAMPLES, False
    if abits <= CODE_BOOKS:
        sel = c['quant_sel'][ch][abits - 1]
        if sel < T.QUANT_INDEX_GROUP_SIZE[abits - 1]:
            cb = book(f'quant_index_{abits - 1}_{sel}')
            return [br.vlc(cb) for _ in range(SUBBAND_SAMPLES)], True
        if abits <= 7:                          # block codes: 4 samples a code
            nb = T.BLOCK_CODE_NBITS[abits - 1]
            levels = T.QUANT_LEVELS[abits]
            off = (levels - 1) // 2
            out = []
            for _ in range(2):
                code = br.bits(nb)
                for _ in range(SUBBAND_SAMPLES // 2):
                    code, r = divmod(code, levels)
                    out.append(r - off + (1 if 'block_offset' in MUT else 0))
                if code:
                    if 'lenient_block' not in OPT:
                        raise DtsError('E_BLOCK_CODE', 'block code out of range')
                    LENIENT_COUNT[0] += 1
            return out, False
    if 'raw_unsigned' in MUT:
        return [br.bits(abits - 3) for _ in range(SUBBAND_SAMPLES)], False
    return [br.sbits(abits - 3) for _ in range(SUBBAND_SAMPLES)], False


def dequantize(q, step_size, scale):
    step_scale = step_size * scale
    shift = 0
    if step_scale > (1 << 23):
        shift = log2i(step_scale >> 23) + 1
        step_scale >>= shift
    if 'dequant_trunc' in MUT:
        return [clip23((x * step_scale) >> (22 - shift)) for x in q]
    return [clip23(norm(x * step_scale, 22 - shift)) for x in q]


class CoreDecoder:
    """Holds the state that carries between frames: the ADPCM history (the
    last 4 samples of every band) and the LFE history."""

    def __init__(self):
        self.hist = [[[0] * ADPCM_COEFFS for _ in range(SUBBANDS)] for _ in range(7)]
        self.lfe_hist = [0] * LFE_HISTORY

    def decode_frame(self, data):
        """-> dict with header, coding header, per-channel subband samples
        sb[ch][band] (npcmblocks each), lfe samples, and stats."""
        br = BitReader(data)
        h = parse_frame_header(br)
        if h['frame_size'] > len(data):
            raise DtsError('E_TRUNCATED', f'frame {h["frame_size"]} > {len(data)} bytes')
        c = parse_coding_header(br, h)
        nch, npb = c['nchannels'], h['npcmblocks']
        if not h['predictor_history']:
            for ch in range(7):
                for b in range(SUBBANDS):
                    self.hist[ch][b] = [0] * ADPCM_COEFFS
        # sb[ch][band] = ADPCM history (4) + npcmblocks samples
        sb = [[list(self.hist[ch][b]) + [0] * npb for b in range(SUBBANDS)] for ch in range(nch)]
        lfe = []
        stats = {'pmode_bands': 0, 'vq_bands': 0, 'huff': 0, 'block': 0, 'raw': 0,
                 'transient': 0, 'joint_bands': 0, 'huff_adj': 0, 'subframes': []}
        sub_pos = 0
        for sf in range(c['nsubframes']):
            s = parse_subframe_header(br, h, c)
            nsamples = s['nssf'] * SUBBAND_SAMPLES
            if sub_pos + nsamples > npb:
                raise DtsError('E_SUBBAND_OVERFLOW', 'subband sample buffer overflow')
            stats['subframes'].append(s['nssf'])
            # high-frequency VQ subbands
            for ch in range(nch):
                for b in range(c['vq_start'][ch], c['nsubbands'][ch]):
                    vec = T.HIGH_FREQ_VQ[(br.bits(10) + (1 if 'vq_index' in MUT else 0)) & 1023]
                    sc = s['scales'][ch][b][0]
                    for j in range(nsamples):
                        sb[ch][b][ADPCM_COEFFS + sub_pos + j] = clip23((vec[j] * sc + 8) >> 4)
                    stats['vq_bands'] += 1
            # LFE
            if h['lfe_present']:
                n = 2 * h['lfe_present'] * s['nssf']
                q = [br.sbits(8) for _ in range(n)]
                idx = br.bits(8)
                if idx >= len(T.SCALE_FACTOR_QUANT7):
                    raise DtsError('E_LFE_SCALE', f'LFE scale index {idx}')
                sc = mul(4697620, T.SCALE_FACTOR_QUANT7[idx], 23)
                lfe += [clip23((x * sc) >> 4) for x in q]
            # audio data, per subsubframe
            ofs = sub_pos
            for ssf in range(s['nssf']):
                for ch in range(nch):
                    for b in range(c['vq_start'][ch]):
                        a = s['abits'][ch][b]
                        q, huff = extract_audio(br, c, ch, a)
                        if a:
                            stats['huff' if huff else
                                  'block' if a <= 7 else 'raw'] += 1
                        # bit_rate 3 is the table's 'lossless' marker (br_code 31)
                        step = T.LOSSLESS_QUANT[a] if h['bit_rate'] == 3 else T.LOSSY_QUANT[a]
                        t = s['tmode'][ch][b]
                        scale = s['scales'][ch][b][0 if (t == 0 or ssf < t or 'transient' in MUT) else 1]
                        if t:
                            stats['transient'] += 1
                        if huff and c['scale_adj'][ch][a - 1] != 1 << 22:
                            stats['huff_adj'] += 1
                        if huff and 'huff_adj' not in MUT:
                            scale = clip23((c['scale_adj'][ch][a - 1] * scale) >> 22)
                        out = dequantize(q, step, scale)
                        base = ADPCM_COEFFS + ofs
                        sb[ch][b][base:base + SUBBAND_SAMPLES] = out
                if ssf == s['nssf'] - 1 or h['sync_ssf']:
                    if br.bits(16) != 0xFFFF:
                        raise DtsError('E_DSYNC', f'DSYNC failed (subframe {sf}, ssf {ssf})')
                ofs += SUBBAND_SAMPLES
            # inverse ADPCM, per subframe, every predicted band
            for ch in range(nch):
                for b in range(c['nsubbands'][ch]):
                    if s['pmode'][ch][b] and 'no_adpcm' not in MUT:
                        stats['pmode_bands'] += 1
                        coeff = T.ADPCM_VB[s['pvq'][ch][b]]
                        x = sb[ch][b]
                        for j in range(ADPCM_COEFFS + sub_pos, ADPCM_COEFFS + sub_pos + nsamples):
                            pred = sum(x[j - 1 - i] * coeff[i] for i in range(ADPCM_COEFFS))
                            x[j] = clip23(x[j] + clip23(norm(pred, 13)))
            # joint intensity, per subframe
            for ch in range(nch):
                src = c['joint_index'][ch] - 1
                if src >= 0:
                    for b in range(c['nsubbands'][ch], c['nsubbands'][src]):
                        jsc = s['jscale'][ch][b]
                        stats['joint_bands'] += 1
                        if 'no_joint' in MUT:
                            continue
                        for j in range(ADPCM_COEFFS + sub_pos, ADPCM_COEFFS + sub_pos + nsamples):
                            sb[ch][b][j] = clip23(mul(sb[src][b][j], jsc, 17))
            sub_pos = ofs
        # history for the next frame; bands past the active count are cleared
        for ch in range(nch):
            nact = c['nsubbands'][ch]
            if c['joint_index'][ch]:
                nact = max(nact, c['nsubbands'][c['joint_index'][ch] - 1])
            for b in range(SUBBANDS):
                self.hist[ch][b] = sb[ch][b][-ADPCM_COEFFS:] if b < nact else [0] * ADPCM_COEFFS
                if b >= nact:
                    sb[ch][b] = [0] * (ADPCM_COEFFS + npb)
        end_audio = br.pos
        opt = parse_optional_info(br, h, c)
        return {'h': h, 'c': c, 'sb': [[x[ADPCM_COEFFS:] for x in ch] for ch in sb],
                'lfe': lfe, 'stats': stats, 'end_audio_bits': end_audio, 'opt': opt}


def crc16_ccitt(data):
    """CRC-16/CCITT (poly 0x1021, init 0xFFFF) -- the DTS aux-data check."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def parse_optional_info(br, h, c):
    """Time code and auxiliary data (embedded downmix coefficients)."""
    o = {'aux_ok': None, 'dmix_type': None, 'dmix_coeff': None}
    if h['ts_present']:
        br.bits(32)
    if h['aux_present']:
        br.bits(6)
        br.align(32)
        if br.bits(32) != SYNC_AUX:
            o['aux_ok'] = False
            return o
        aux_pos = br.pos
        if br.bits(1):
            br.bits(47)
        if br.bits(1):
            o['dmix_type'] = br.bits(3)
            if o['dmix_type'] < len(T.DMIX_PRIMARY_NCH):
                m = T.DMIX_PRIMARY_NCH[o['dmix_type']]
                n = T.CHANNELS[h['audio_mode']] + (1 if h['lfe_present'] else 0)
                coeff = []
                for _ in range(m * n):
                    code = br.bits(9)
                    sign = (code >> 8) - 1
                    idx = code & 0xFF
                    v = T.DMIXTABLE[idx] if idx < len(T.DMIXTABLE) else 0
                    coeff.append((v ^ sign) - sign)
                o['dmix_coeff'] = coeff
        br.align(8)
        br.bits(16)
        # the CRC covers aux_pos .. here (bytes); a zero remainder is a pass
        o['aux_ok'] = (aux_pos % 8 == 0 and
                       crc16_ccitt(br.data[aux_pos // 8:br.pos // 8]) == 0)
    return o


# --------------------------------------------------------------------------
# Synthesis: FFmpeg's FIXED-point path (what `-flags bitexact` selects), so the
# reference is bit-exact against the binary and one comparison checks every
# bit of the front end. The hardware's own fixed point (tools/dts_fixed.py)
# is then scored against this within an LSB bound (docs/dts_decoder.md D3).
# C int32 semantics are emulated where FFmpeg relies on them: norm__ casts its
# 64-bit result to int32, and the L/R butterflies wrap as unsigned.
# --------------------------------------------------------------------------
def i32(a):
    a &= 0xFFFFFFFF
    return a - (1 << 32) if a >> 31 else a


def normc(a, bits):
    """FFmpeg norm__: round half up, shift, then (int32_t) cast."""
    return i32((a + (1 << (bits - 1))) >> bits)


def mulc(a, b, bits):
    return normc(a * b, bits)


def _dct_a(x):
    return [normc(sum(T.DCT_A_COS[i][j] * x[j] for j in range(8)), 23) for i in range(8)]


def _dct_b(x):
    return [normc((x[0] << 23) + sum(T.DCT_B_COS[i][j] * x[1 + j] for j in range(7)), 23)
            for i in range(8)]


def _mod_a(x):
    c = T.MOD_A_COS
    return ([mulc(c[i], x[i] + x[8 + i], 23) for i in range(8)] +
            [mulc(c[8 + n], x[7 - n] - x[15 - n], 23) for n in range(8)])


def _mod_b(x):
    c = T.MOD_B_COS
    hi = [mulc(c[i], x[8 + i], 23) for i in range(8)]
    return [x[i] + hi[i] for i in range(8)] + [x[7 - n] - hi[7 - n] for n in range(8)]


def _mod_c(x):
    c = T.MOD_C_COS
    return ([mulc(c[i], x[i] + x[16 + i], 23) for i in range(16)] +
            [mulc(c[16 + n], x[15 - n] - x[31 - n], 23) for n in range(16)])


def _sum_a(x, n):
    return [x[2 * i] + x[2 * i + 1] for i in range(n)]


def _sum_b(x, n):
    return [x[0]] + [x[2 * i] + x[2 * i - 1] for i in range(1, n)]


def _sum_c(x, n):
    return [x[2 * i] for i in range(n)]


def _sum_d(x, n):
    return [x[1]] + [x[2 * i - 1] + x[2 * i + 1] for i in range(1, n)]


def _clp(v):
    return [clip23(a) for a in v]


def imdct_half_32(inp):
    """FFmpeg dcadct.c imdct_half_32, transcribed: 32 subband samples -> 32."""
    mag = sum(abs(a) for a in inp)
    shift = 2 if mag > 0x400000 and 'imdct_noshift' not in MUT else 0
    IMDCT_SHIFTS[0] += mag > 0x400000
    rnd = 1 << (shift - 1) if shift else 0
    a = [(v + rnd) >> shift for v in inp]
    b = _clp(_sum_a(a, 16) + _sum_b(a, 16))
    a = _clp(_sum_a(b[0:16], 8) + _sum_b(b[0:16], 8) + _sum_c(b[16:32], 8) + _sum_d(b[16:32], 8))
    b = _clp(_dct_a(a[0:8]) + _dct_b(a[8:16]) + _dct_b(a[16:24]) + _dct_b(a[24:32]))
    a = _clp(_mod_a(b[0:16]) + _mod_b(b[16:32]))
    b = _mod_c(a)
    b = [clip23(v * (1 << shift)) for v in b]
    return ([clip23(b[i] - b[31 - i]) for i in range(16)] +
            [clip23(b[i] + b[31 - i]) for i in range(16)])


class SynthFixed:
    """One channel's 32-band QMF state: the 512-entry IMDCT ring with its
    offset, and the 32 carried partial sums (FFmpeg synth_filter_fixed)."""

    def __init__(self):
        self.buf = [0] * 512
        self.off = 0
        self.buf2 = [0] * 32

    def run(self, inp, win):
        o = self.off
        self.buf[o:o + 32] = imdct_half_32(inp)
        buf, out = self.buf, [0] * 32

        def at(k):
            return buf[o + k] if o + k < 512 else buf[o + k - 512]
        for i in range(16):
            a = self.buf2[i] << 21
            b = self.buf2[i + 16] << 21
            c = d = 0
            for j in range(0, 512, 64):
                a += win[i + j] * at(i + j)
                b += win[i + j + 16] * at(15 - i + j)
                c += win[i + j + 32] * at(16 + i + j)
                d += win[i + j + 48] * at(31 - i + j)
            out[i] = clip23(normc(a, 21))
            out[i + 16] = clip23(normc(b, 21))
            self.buf2[i] = normc(c, 21)
            self.buf2[i + 16] = normc(d, 21)
        self.off = (o - 32) & 511
        return out


# FFmpeg's DCA speaker indices and the primary-channel map per audio mode.
SPK_C, SPK_L, SPK_R, SPK_LS, SPK_RS, SPK_LFE, SPK_CS = 0, 1, 2, 3, 4, 5, 6
PRM_CH_TO_SPKR = [
    [SPK_C], [SPK_L, SPK_R], [SPK_L, SPK_R], [SPK_L, SPK_R], [SPK_L, SPK_R],
    [SPK_C, SPK_L, SPK_R], [SPK_L, SPK_R, SPK_CS], [SPK_C, SPK_L, SPK_R, SPK_CS],
    [SPK_L, SPK_R, SPK_LS, SPK_RS], [SPK_C, SPK_L, SPK_R, SPK_LS, SPK_RS],
]
# FFmpeg's native (WAV) output order for these speakers: dca2wav_norm.
SPK_TO_WAV = {SPK_C: 2, SPK_L: 0, SPK_R: 1, SPK_LS: 9, SPK_RS: 10, SPK_LFE: 3, SPK_CS: 8}


class Decoder:
    """Frame bytes -> {speaker: [24-bit PCM]} through FFmpeg's fixed path."""

    def __init__(self):
        self.core = CoreDecoder()
        self.synth = [SynthFixed() for _ in range(7)]
        self.lfe_hist = [0] * LFE_HISTORY

    def decode(self, data):
        r = self.core.decode_frame(data)
        h, c = r['h'], r['c']
        shifts0 = IMDCT_SHIFTS[0]
        npb = h['npcmblocks']
        perfect = h['filter_perfect'] ^ ('window_swap' in MUT)
        win = T.FIR_32BANDS_PERFECT_FIXED if perfect else T.FIR_32BANDS_NONPERFECT_FIXED
        out = {}
        for ch in range(c['nchannels']):
            spk = PRM_CH_TO_SPKR[h['audio_mode']][ch]
            pcm, sb, syn = [], r['sb'][ch], self.synth[ch]
            for j in range(npb):
                pcm += syn.run([sb[i][j] for i in range(SUBBANDS)], win)
            out[spk] = pcm
        if h['lfe_present']:
            if h['lfe_present'] == 1:
                raise DtsError('E_LFF128', 'LFE 128x interpolation (FFmpeg fixed path refuses it)')
            n = npb >> 1
            lfe = self.lfe_hist + r['lfe'][:n]
            coef, pcm = T.LFE_FIR_64_FIXED, []
            for i in range(n):
                cur = LFE_HISTORY + i
                blk = [0] * 64
                for j in range(32):
                    a = sum(coef[j * 8 + k] * lfe[cur - k] for k in range(8))
                    b = sum(coef[255 - j * 8 - k] * lfe[cur - k] for k in range(8))
                    blk[j] = clip23(normc(a, 23))
                    blk[32 + j] = clip23(normc(b, 23))
                pcm += blk
            self.lfe_hist = [0] * LFE_HISTORY if 'lfe_nohist' in MUT else lfe[n:n + LFE_HISTORY]
            out[SPK_LFE] = pcm
        # sum/difference decoding (no XCh/XXCH: the core only)
        def butterfly(p, q):
            a, b = out[p], out[q]
            out[p] = [i32(x + y) for x, y in zip(a, b)]
            out[q] = [i32(x - y) for x, y in zip(a, b)]
        if ((h['sumdiff_front'] and h['audio_mode'] > 0) or
                h['audio_mode'] == AMODE_STEREO_SUMDIFF) and 'no_sumdiff' not in MUT:
            butterfly(SPK_L, SPK_R)
        if h['sumdiff_surround'] and h['audio_mode'] >= AMODE_2F2R:
            butterfly(SPK_LS, SPK_RS)
        r['pcm'] = {spk: [clip23(v) for v in v_] for spk, v_ in out.items()}
        r['stats']['imdct_shift'] = IMDCT_SHIFTS[0] - shifts0
        return r


def wav_order(speakers):
    return sorted(speakers, key=lambda s: SPK_TO_WAV[s])


# --------------------------------------------------------------------------
# Raw DTS byte stream -> frames
# --------------------------------------------------------------------------
def frames(buf):
    """Yield (offset, frame bytes) for each core frame in a raw 16-bit BE
    stream, resynchronising on the sync word + a frame-size check."""
    i, n = 0, len(buf)
    while i + 16 <= n:
        if int.from_bytes(buf[i:i + 4], 'big') != SYNC_CORE_BE:
            j = buf.find(b'\x7f\xfe\x80\x01', i + 1)
            if j < 0:
                return
            i = j
            continue
        fsize = (((buf[i + 5] & 0x03) << 12) | (buf[i + 6] << 4) | (buf[i + 7] >> 4)) + 1
        if fsize < 96 or i + fsize > n:
            j = buf.find(b'\x7f\xfe\x80\x01', i + 1)
            if j < 0:
                return
            i = j
            continue
        yield i, buf[i:i + fsize]
        i += fsize


def cmd_info(args):
    buf = open(args.file, 'rb').read()
    dec = CoreDecoder()
    nf, errs, first = 0, {}, None
    agg = {'pmode_bands': 0, 'vq_bands': 0, 'huff': 0, 'block': 0, 'raw': 0}
    aux = {'present': 0, 'ok': 0, 'dmix': 0}
    for off, fr in frames(buf):
        if args.frames and nf >= args.frames:
            break
        nf += 1
        try:
            r = dec.decode_frame(fr)
        except DtsError as e:
            errs[e.code] = errs.get(e.code, 0) + 1
            dec = CoreDecoder()
            continue
        if first is None:
            first = r
        for k in agg:
            agg[k] += r['stats'][k]
        if r['h']['aux_present']:
            aux['present'] += 1
            aux['ok'] += bool(r['opt']['aux_ok'])
            aux['dmix'] += r['opt']['dmix_coeff'] is not None
    if first:
        h, c = first['h'], first['c']
        print(f'{os.path.basename(args.file)}: {h["sample_rate"]} Hz, '
              f'{h["bit_rate"] // 1000} kbit/s, amode {h["audio_mode"]} '
              f'({c["nchannels"]} ch), lfe {h["lfe_present"]}, npcmblocks {h["npcmblocks"]}, '
              f'subframes {c["nsubframes"]}, frame {h["frame_size"]} B, '
              f'filter {"perfect" if h["filter_perfect"] else "nonperfect"}, '
              f'ext {"type " + str(h["ext_audio_type"]) if h["ext_audio_present"] else "none"}')
        print(f'  nsubbands {c["nsubbands"]}  vq_start {c["vq_start"]}  joint {c["joint_index"]}')
    print(f'  frames {nf}, errors {errs or "none"}')
    print(f'  codes: huffman {agg["huff"]}, block {agg["block"]}, raw {agg["raw"]}; '
          f'predicted bands {agg["pmode_bands"]}, VQ bands {agg["vq_bands"]}')
    print(f'  aux data: {aux["present"]} frames, CRC ok {aux["ok"]}, downmix coeffs {aux["dmix"]}')
    return 1 if errs else 0


def ffmpeg_bitexact(path, nframes_samples=None):
    """-> (channels, [interleaved int32]) from the FFmpeg binary's fixed path."""
    import json
    import subprocess
    probe = json.loads(subprocess.run(
        ['ffprobe', '-v', 'error', '-core_only', '1', '-select_streams', 'a:0', '-show_entries',
         'stream=channels', '-of', 'json', path], capture_output=True, check=True).stdout)
    nch = probe['streams'][0]['channels']
    # core_only: this decoder (and the hardware) decodes the core alone; without
    # it FFmpeg adds XCh's sixth channel on a DTS-ES disc
    cmd = ['ffmpeg', '-v', 'error', '-flags', 'bitexact', '-core_only', '1', '-i', path,
           '-f', 's32le', '-acodec', 'pcm_s32le']
    if nframes_samples:
        cmd += ['-frames:a', str(nframes_samples)]
    cmd += ['-']
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    import array
    a = array.array('i')
    a.frombytes(raw[:len(raw) // 4 * 4])
    return nch, a


def run_compare(path, nframes=0):
    """Decode `path` here and with `ffmpeg -flags bitexact`; -> a result dict:
    mismatches, samples, channels, nonzero, peak, and `features` -- which coded
    features the stream exercised (so a mutation arm knows if it can bite)."""
    buf = open(path, 'rb').read()
    dec = Decoder()
    ours, spks, nf = [], None, 0
    feat = {'pmode_bands': 0, 'vq_bands': 0, 'huff': 0, 'block': 0, 'raw': 0,
            'transient': 0, 'joint_bands': 0, 'huff_adj': 0, 'imdct_shift': 0,
            'lfe': 0, 'sumdiff_front': 0,
            'sumdiff_surround': 0, 'filter_perfect': 0, 'filter_nonperfect': 0}
    for _, fr in frames(buf):
        if nframes and nf >= nframes:
            break
        r = dec.decode(fr)
        nf += 1
        for k in ('pmode_bands', 'vq_bands', 'huff', 'block', 'raw', 'transient', 'joint_bands',
                  'huff_adj', 'imdct_shift'):
            feat[k] += r['stats'][k]
        h = r['h']
        feat['lfe'] += bool(h['lfe_present'])
        feat['sumdiff_front'] += bool(h['sumdiff_front'] and h['audio_mode'] > 0)
        feat['sumdiff_surround'] += bool(h['sumdiff_surround'] and h['audio_mode'] >= AMODE_2F2R)
        feat['filter_perfect' if h['filter_perfect'] else 'filter_nonperfect'] += 1
        if spks is None:
            spks = wav_order(r['pcm'])
        for t in range(len(r['pcm'][spks[0]])):
            ours.extend(r['pcm'][sp][t] << 8 for sp in spks)
    nch, theirs = ffmpeg_bitexact(path, nf)
    n = min(len(ours), len(theirs))
    bad = sum(1 for i in range(n) if ours[i] != theirs[i])
    first = next((i for i in range(n) if ours[i] != theirs[i]), None)
    return {'frames': nf, 'channels': nch, 'ours_ch': len(spks or []),
            'samples': n, 'len_ok': len(ours) == len(theirs),
            'mismatches': bad, 'first': first,
            'nonzero': sum(1 for v in theirs[:n] if v),
            'peak': max((abs(v) >> 8 for v in theirs[:n]), default=0),
            'features': feat}


def cmd_compare(args):
    """Bit-exact comparison against `ffmpeg -flags bitexact` (s32, 24-bit << 8)."""
    r = run_compare(args.file, args.frames)
    nch, n = r['channels'], r['samples']
    print(f'compare: {r["frames"]} frames, {n // max(nch, 1)} samples x {nch} ch, '
          f'{r["mismatches"]} mismatches; {r["nonzero"] * 100 // max(n, 1)} % nonzero, '
          f'peak {r["peak"]}')
    print('  features: ' + ', '.join(f'{k} {v}' for k, v in r['features'].items()))
    if nch != r['ours_ch']:
        print(f'compare: FAIL -- {nch} channels from FFmpeg, {r["ours_ch"]} here')
        return 1
    if r['nonzero'] * 2 < n:
        print('compare: FAIL -- under half the samples carry signal; pick a louder stretch')
        return 1
    if r['mismatches']:
        i = r['first']
        print(f'compare: FAIL -- first mismatch at sample {i // nch} ch {i % nch}')
        return 1
    if not r['len_ok']:
        print('compare: FAIL -- lengths differ')
        return 1
    print('compare: PASS -- bit-exact')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('info')
    p.add_argument('file')
    p.add_argument('--frames', type=int, default=0)
    p.set_defaults(fn=cmd_info)
    p = sub.add_parser('compare', help='bit-exact against ffmpeg -flags bitexact')
    p.add_argument('file')
    p.add_argument('--frames', type=int, default=0)
    p.set_defaults(fn=cmd_compare)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
