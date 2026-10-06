#!/usr/bin/env python3
"""ac3_model.py -- the AC-3 parse as dvd/ac3/'s RTL computes it, bit for bit.

The hardware-order model of the AC-3 front end (sync, BSI, the audio block's side
information, exponent decode, bit allocation, mantissa dequantisation, coupling
recombine, rematrixing), up to the seam where `imdct_512` takes over:
per block, `coeff_mem` (signed Q1.23 for every channel and bin), `blksw` and
`dynrng`. It is the golden an engine-based parse is scored against
(docs/ac3_engine.md), and it is itself scored bit-exact against the RTL's own
output (bench/ac3/golden_main.cpp dumps) by `compare` below.

Sources, kept separate on purpose:
  - the integer stages follow liba52 0.8.0 (`parse.c`, `bit_allocate.c`), which
    the RTL documents as a literal transcription;
  - the fixed-point stage follows the RTL (`mantissa_dequant.sv`): Q1.23
    coefficients `(m16 << 8) >>> exp`, its own 16-bit rounded level tables, a
    dither of round(ns * 23170 / 2^(7+exp)), the saturating Q5.18 coupling
    recombine and the saturating rematrix; and its tables are PARSED out of
    dvd/ac3/*.svh, so they cannot drift from what the RTL holds.
  - where the RTL deviates from liba52, the model follows the RTL and says so
    (DEVIATION below): delta bit allocation resets to NONE every block and is
    applied only when the block says NEW.

The functions are at the granularity of the engine's planned vector ops
(docs/ac3_engine.md), so an emulator can call the same code.

Usage:
    tools/ac3_model.py compare STREAM.ac3 GOLDEN.gold [--frames N]
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
AC3 = os.path.join(REPO, 'dvd', 'ac3')

# Model options (empty = the RTL's behaviour): 'liba52_deltba' applies liba52's
# per-frame delta-BA persistence instead of the RTL's per-block reset, for every
# Decoder (a Decoder(liba52_deltba=True) does the same for one instance).
OPT = set()
STATS = ('blocks', 'cpl', 'remat', 'dynrnge', 'short', 'dith', 'deltba_new', 'deltba_reuse',
         'deltbaie_off_after_new', 'skip', 'zero_snr', 'phsflginu', 'phsflg', 'recomb_sat',
         'remat_sat', 'cpl_ch0_uncoupled', 'cplmerge', 'dualmono')
# (phsflg: bands whose phase flag was set; *_sat: coefficients that saturated;
#  dualmono: 1+1 frames)


class Ac3Error(Exception):
    """A frame the RTL refuses (err_unsupported) -- or one the model cannot vouch
    for (code 'UNMODELLED'), which the comparison reports instead of guessing."""

    def __init__(self, code, msg):
        super().__init__(f'{code}: {msg}')
        self.code = code


# ------------------------------------------------------------------ RTL tables
def _svh_arrays(path):
    """name -> [ints] for every `name = '{ ... };` in an .svh (SystemVerilog
    literals: 16'h00a0, -8'sd3, 21845, ...)."""
    src = re.sub(r'//[^\n]*', '', open(path).read())
    out = {}
    for m in re.finditer(r"(\w+)\s*=\s*'\{(.*?)\}\s*;", src, re.S):
        vals = []
        for tok in m.group(2).split(','):
            tok = tok.strip().replace('_', '')
            if not tok:
                continue
            neg = tok.startswith('-')
            tok = tok.lstrip('-+')
            mm = re.fullmatch(r"(?:\d+)?'s?([dhbo])([0-9a-fA-F]+)", tok)
            if mm:
                v = int(mm.group(2), {'d': 10, 'h': 16, 'b': 2, 'o': 8}[mm.group(1)])
            else:
                v = int(tok, 0)
            vals.append(-v if neg else v)
        out[m.group(1)] = vals
    return out


_MT = _svh_arrays(os.path.join(AC3, 'ac3_mant_tables.svh'))
_BT = _svh_arrays(os.path.join(AC3, 'ac3_tables.svh'))
Q1LEV, Q2LEV, Q3LEV, Q4LEV, Q5LEV = (_MT[k] for k in ('q1lev', 'q2lev', 'q3lev', 'q4lev', 'q5lev'))
DITHER_LUT = _MT['dither_lut']
BAPTAB = _BT['baptab']                       # indexed 156 + k
LATAB = _BT['latab']
HTH = _BT['hthtab0']                         # fscod 0 (48 kHz) only, as the RTL
BNDTAB = _BT['bndtab']
SLOWGAIN, DBPBTAB, FLOORTAB = _BT['slowgain_t'], _BT['dbpbtab_t'], _BT['floortab_t']
assert len(BAPTAB) == 305 and len(LATAB) == 256 and len(HTH) == 50 and len(BNDTAB) == 30
assert len(DITHER_LUT) == 256

NFCHANS = [2, 1, 2, 3, 3, 4, 4, 5]
FRMSIZ_48K = [64, 64, 80, 80, 96, 96, 112, 112, 128, 128, 160, 160, 192, 192, 224, 224,
              256, 256, 320, 320, 384, 384, 448, 448, 512, 512, 640, 640, 768, 768, 896, 896,
              1024, 1024, 1152, 1152, 1280, 1280]          # 16-bit words a frame
REMATRIX_BAND = [25, 37, 61, 253]
CPL_BNDTAB = [31, 35, 37, 39, 41, 42, 43, 44, 45, 45, 46, 46, 47, 47, 48, 48]
EXP_REUSE, DELTA_NEW, DELTA_NONE, DELTA_REUSE = 0, 1, 2, 0
CH_CPL, CH_LFE = 5, 6


def s24(v):
    v &= 0xFFFFFF
    return v - (1 << 24) if v & 0x800000 else v


def sat24(v):
    return 8388607 if v > 8388607 else -8388608 if v < -8388608 else v


def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class BitReader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def bits(self, n):
        v = 0
        for _ in range(n):
            byte = self.pos >> 3
            if byte >= len(self.data):
                raise Ac3Error('UNMODELLED', 'read past the frame')
            v = (v << 1) | ((self.data[byte] >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def sbits(self, n):
        v = self.bits(n)
        return v - (1 << n) if v >> (n - 1) else v


# ------------------------------------------------------------------ the ops
def ungroup_exps(br, expstr, ngrps, absexp):
    """Exponent decode (EXPD): `ngrps` 7-bit group codes -> the absolute exponents,
    each repeated per the strategy (D15 1, D25 2, D45 4). The RTL clamps to
    [0, 24] where liba52 refuses the frame; a legal stream never reaches it."""
    rep = (1, 2, 4)[expstr - 1]
    out, e = [], absexp
    for _ in range(ngrps):
        code = br.bits(7)
        for d in (code // 25, (code % 25) // 5, code % 5):
            e += d - 2
            if e < 0 or e > 24:
                raise Ac3Error('UNMODELLED', f'exponent {e} outside 0..24')
            out.extend([e] * rep)
    return out


def compute_mask(psd, mask, i, dbknee, snroffset, deltba_i, floor):
    if psd > dbknee:
        mask -= (psd - dbknee) >> 2
    if mask > HTH[i]:
        mask = HTH[i]
    mask -= snroffset + 128 * deltba_i
    mask = 0 if mask > 0 else ((-mask) >> 5)
    return mask - floor


def ba_band_psd(exp, j, endband):
    """The band's integrated PSD: liba52's log-add over bins [j, endband) (the
    engine's band-integration op)."""
    psd = 128 * exp[j]
    j += 1
    while j < endband:
        nxt = 128 * exp[j]
        j += 1
        delta = nxt - psd
        sw = delta >> 9
        if -6 <= sw <= -2:
            psd = nxt
        elif sw == -1:
            psd = nxt + LATAB[min((-delta) >> 1, 255)]       # the RTL's la_addr clamp
        elif sw == 0:
            psd += LATAB[min(delta >> 1, 255)]
    return psd


def ba_bap_fill(mask, exp, j, endband):
    """bap[k] = baptab[156 + mask + 4 exp[k]] for k in [j, endband) (the engine's
    bap-lookup op)."""
    return {k: BAPTAB[min(max(156 + mask + 4 * exp[k], 0), 304)]     # the RTL's bl_addr clamp
            for k in range(j, endband)}


def bit_allocate(g, bai_ch, deltba, bndstart, start, end, fastleak, slowleak, exp):
    """liba52 a52_bit_allocate(), fscod 0 (halfrate 0): -> {bin: bap} for bins in
    [start, end). `g` holds the frame-shared parameters (bai, csnroffst);
    `bai_ch` the channel's 7-bit fsnroffst/fgaincod; `deltba` 50 band offsets."""
    fdecay = 63 + 20 * ((g['bai'] >> 7) & 3)
    fgain = 128 + 128 * (bai_ch & 7)
    sdecay = 15 + 2 * (g['bai'] >> 9)
    sgain = SLOWGAIN[(g['bai'] >> 5) & 3]
    dbknee = DBPBTAB[(g['bai'] >> 3) & 3]
    floor = FLOORTAB[g['bai'] & 7]
    snroffset = 960 - 64 * g['csnroffst'] - 4 * (bai_ch >> 3) + floor
    floor >>= 5
    bap = {}

    def lut(mask, e):
        return BAPTAB[156 + mask + 4 * e]
    i, j = bndstart, start
    psd = 0
    if start == 0:
        lowcomp = 0
        j = end - 1
        while True:
            if i < j:
                if exp[i + 1] == exp[i] - 2:
                    lowcomp = 384
                elif lowcomp and exp[i + 1] > exp[i]:
                    lowcomp -= 64
            psd = 128 * exp[i]
            mask = compute_mask(psd, psd + fgain + lowcomp, i, dbknee, snroffset, deltba[i], floor)
            bap[i] = lut(mask, exp[i])
            i += 1
            if not ((i < 3) or ((i < 7) and (exp[i] > exp[i - 1]))):
                break
        fastleak = psd + fgain
        slowleak = psd + sgain
        while i < 7:
            if i < j:
                if exp[i + 1] == exp[i] - 2:
                    lowcomp = 384
                elif lowcomp and exp[i + 1] > exp[i]:
                    lowcomp -= 64
            psd = 128 * exp[i]
            fastleak = min(fastleak + fdecay, psd + fgain)
            slowleak = min(slowleak + sdecay, psd + sgain)
            m = fastleak + lowcomp if fastleak + lowcomp < slowleak else slowleak
            bap[i] = lut(compute_mask(psd, m, i, dbknee, snroffset, deltba[i], floor), exp[i])
            i += 1
        if end == 7:                                    # LFE
            return bap
        while True:
            if exp[i + 1] == exp[i] - 2:
                lowcomp = 320
            elif lowcomp and exp[i + 1] > exp[i]:
                lowcomp -= 64
            psd = 128 * exp[i]
            fastleak = min(fastleak + fdecay, psd + fgain)
            slowleak = min(slowleak + sdecay, psd + sgain)
            m = fastleak + lowcomp if fastleak + lowcomp < slowleak else slowleak
            bap[i] = lut(compute_mask(psd, m, i, dbknee, snroffset, deltba[i], floor), exp[i])
            i += 1
            if i >= 20:
                break
        while lowcomp > 128:
            lowcomp -= 128
            psd = 128 * exp[i]
            fastleak = min(fastleak + fdecay, psd + fgain)
            slowleak = min(slowleak + sdecay, psd + sgain)
            m = fastleak + lowcomp if fastleak + lowcomp < slowleak else slowleak
            bap[i] = lut(compute_mask(psd, m, i, dbknee, snroffset, deltba[i], floor), exp[i])
            i += 1
        j = i
    while True:
        startband = j
        endband = BNDTAB[i - 20] if BNDTAB[i - 20] < end else end
        psd = ba_band_psd(exp, j, endband)
        fastleak = min(fastleak + fdecay, psd + fgain)
        slowleak = min(slowleak + sdecay, psd + sgain)
        mask = fastleak if fastleak < slowleak else slowleak
        mask = compute_mask(psd, mask, i, dbknee, snroffset, deltba[i], floor)
        i += 1
        bap.update(ba_bap_fill(mask, exp, startband, endband))
        j = endband
        if j >= end:
            break
    return bap


def scale_coeff(m16, e):
    """(m16 << 8) >>> exp, the low 24 bits (mantissa_dequant scale_coeff)."""
    return s24((m16 << 8) >> e)


class Mantissas:
    """The mantissa reader (XQ-like): the grouped-quantizer caches persist across
    every read of a block (fbw channels, the coupling read, LFE)."""

    def __init__(self, br):
        self.br = br
        self.q1, self.q2, self.q4 = [], [], []

    def m16(self, bap):
        """-> the signed 16-bit mantissa value of one coefficient, reading bits
        only when the class's cache is empty (liba52 coeff_get)."""
        br = self.br
        if bap == -1:
            if not self.q1:
                c = br.bits(5)
                if c >= 27:
                    raise Ac3Error('UNMODELLED', f'3-level group code {c} (27..31 are invalid)')
                self.q1 = [Q1LEV[c % 3], Q1LEV[(c // 3) % 3]]        # popped from the end
                return Q1LEV[c // 9]
            return self.q1.pop()
        if bap == -2:
            if not self.q2:
                c = br.bits(7)
                if c >= 125:
                    raise Ac3Error('UNMODELLED', f'5-level group code {c} (125..127 are invalid)')
                self.q2 = [Q2LEV[c % 5], Q2LEV[(c // 5) % 5]]
                return Q2LEV[c // 25]
            return self.q2.pop()
        if bap == -3:
            if not self.q4:
                c = br.bits(7)
                if c >= 121:
                    raise Ac3Error('UNMODELLED', f'11-level group code {c} (121..127 are invalid)')
                self.q4 = [Q4LEV[c % 11]]
                return Q4LEV[c // 11]
            return self.q4.pop()
        if bap == 3:
            return Q3LEV[br.bits(3)]
        if bap == 4:
            return Q5LEV[br.bits(4)]
        v = br.bits(bap)                                # direct, bap 5..16
        return s16((v << (16 - bap)) & 0xFFFF)


def dither_next(lfsr):
    return (DITHER_LUT[lfsr >> 8] ^ ((lfsr << 8) & 0xFFFF)) & 0xFFFF


def dither_coeff(lfsr_new, e):
    """round(ns * 23170 / 2^(7+exp)) as Q1.23 (mantissa_dequant dither_coeff)."""
    sh = 7 + e
    if sh > 30:
        return 0
    return s24((s16(lfsr_new) * 23170 + (1 << (sh - 1))) >> sh)


def recombine(cc, co):
    """(cc x cplco) >>> 18, saturated to Q1.23."""
    return sat24((cc * co) >> 18)


def cplco_q518(cplcoexp, cplcomant, mstrcplco):
    full = (cplcomant << 14) if cplcoexp == 15 else ((cplcomant | 0x10) << 13)
    return ((full << 3) >> (cplcoexp + mstrcplco)) & 0xFFFFFF


# ------------------------------------------------------------------ the decoder
class Decoder:
    """Frame bytes -> per block {blksw, dynrng, coeff[ch][256], lfe[7]}. State
    persists across blocks and frames, as the RTL's does (reset only at start)."""

    def __init__(self, liba52_deltba=None):
        self.liba52_deltba = ('liba52_deltba' in OPT) if liba52_deltba is None else liba52_deltba
        self.stats = dict.fromkeys(STATS, 0)
        self.fields = None           # a list: (name, bit position, width) of fields read
        self.snapshot = False        # blocks also return their exps and baps
        self.new_in_frame = False
        self.lfsr = 1
        self.exp = {ch: [0] * 256 for ch in range(7)}
        self.endmant = [0] * 5
        self.chincpl = 0
        self.cpl = dict(begf=0, endf=0, strtmant=0, endmant=0, strtbnd=0, ncplbnd=0,
                        bndstrc=0, phsflginu=0)
        self.cplco = [[0] * 18 for _ in range(5)]
        self.phsneg = [0] * 18
        self.rematflg = 0
        self.g = dict(bai=0, csnroffst=0)
        self.bai = [0] * 5
        self.cplbai = 0
        self.lfebai = 0
        self.cplfleak = self.cplsleak = 0
        self.deltbae = [DELTA_NONE] * 5
        self.cpldeltbae = DELTA_NONE
        self.deltba = [[0] * 50 for _ in range(5)]
        self.dynrng = 0

    # -- the frame header and BSI -----------------------------------------
    def frame(self, data):
        br = BitReader(data)
        if br.bits(16) != 0x0B77:
            raise Ac3Error('SYNC', 'no sync word')
        br.bits(16)                                     # crc1
        fscod = br.bits(2)
        frmsizcod = br.bits(6)
        if fscod != 0 or frmsizcod >= 38:
            raise Ac3Error('FSCOD', f'fscod {fscod} frmsizcod {frmsizcod}')
        br.bits(5)                                      # bsid
        br.bits(3)                                      # bsmod
        acmod = br.bits(3)
        # acmod 0 (1+1 dual mono) decodes since docs/lpcm_full.md §7: two independent
        # channels, Ch1 to the left and Ch2 to the right (nfchans 2, no downmix);
        # bsi() repeats dialnorm/compr/langcod/audprodi for Ch2, below.
        cmix = surmix = 0
        if (acmod & 1) and acmod != 1:
            cmix = br.bits(2)
        if acmod & 4:
            surmix = br.bits(2)
        if acmod == 2:
            br.bits(2)                                  # dsurmod
        lfeon = br.bits(1)
        br.bits(5)                                      # dialnorm
        if br.bits(1):
            br.bits(8)                                  # compr
        if br.bits(1):
            br.bits(8)                                  # langcod
        if br.bits(1):
            br.bits(7)                                  # audprodi
        if acmod == 0:                                  # 1+1: Ch2's copy of the four
            self.stats['dualmono'] += 1
            br.bits(5)                                  # dialnorm2
            if br.bits(1):
                br.bits(8)                              # compr2
            if br.bits(1):
                br.bits(8)                              # langcod2
            if br.bits(1):
                br.bits(7)                              # mixlevel2, roomtyp2
        br.bits(2)                                      # copyrightb, origbs
        if br.bits(1):
            br.bits(14)
        if br.bits(1):
            br.bits(14)
        if br.bits(1):
            n = br.bits(6)
            br.bits(8 * (n + 1))
        hdr = dict(acmod=acmod, lfeon=lfeon, cmixlev=cmix, surmixlev=surmix,
                   frame_bytes=2 * FRMSIZ_48K[frmsizcod])
        blocks = [self.block(br, acmod, lfeon, blk == 0) for blk in range(6)]
        return hdr, blocks

    # -- one audio block --------------------------------------------------
    def block(self, br, acmod, lfeon, first):
        nf = NFCHANS[acmod]
        blksw = [br.bits(1) for _ in range(nf)]
        dith = [br.bits(1) for _ in range(nf)]
        st = self.stats
        st['blocks'] += 1
        st['short'] += any(blksw)
        st['dith'] += any(dith)
        if first:
            self.dynrng = 0
            self.new_in_frame = False
        if br.bits(1):
            self.dynrng = br.bits(8)
            st['dynrnge'] += 1
        if acmod == 0 and br.bits(1):                   # 1+1: Ch2's dynrng2e. As liba52
            self.dynrng = br.bits(8)                    # (parse.c), the last one sent
            st['dynrnge'] += 1                          # applies to both channels
        cpl = self.cpl
        if br.bits(1):                                  # cplstre
            self.chincpl = 0
            if br.bits(1):                              # cplinu
                for i in range(nf):
                    self.chincpl |= br.bits(1) << i
                if acmod == 2:
                    cpl['phsflginu'] = br.bits(1)
                begf, endf = br.bits(4), br.bits(4)
                if endf + 3 - begf < 0:
                    raise Ac3Error('CPLBND', 'cplendf + 3 < cplbegf')
                cpl.update(ncplbnd=endf + 3 - begf, strtbnd=CPL_BNDTAB[begf],
                           strtmant=begf * 12 + 37, endmant=endf * 12 + 73, bndstrc=0)
                for i in range(endf + 3 - begf - 1):
                    if br.bits(1):
                        cpl['bndstrc'] |= 1 << i
                        cpl['ncplbnd'] -= 1
        if self.chincpl:
            cplcoe = 0
            for i in range(nf):
                if (self.chincpl >> i) & 1 and br.bits(1):
                    cplcoe = 1
                    if self.fields is not None:
                        self.fields.append(('mstrcplco', br.pos, 2))
                    mstr = 3 * br.bits(2)
                    for j in range(cpl['ncplbnd']):
                        if self.fields is not None:
                            self.fields.append(('cplcoexp', br.pos, 4))
                        e, m = br.bits(4), br.bits(4)
                        self.cplco[i][j] = cplco_q518(e, m, mstr)
                        if i == 1:
                            self.phsneg[j] = 0
            if acmod == 2 and cpl['phsflginu'] and cplcoe:
                st['phsflginu'] += 1
                for j in range(cpl['ncplbnd']):
                    if br.bits(1):
                        self.phsneg[j] ^= 1
                        st['phsflg'] += 1
            if not self.chincpl & 1:
                st['cpl_ch0_uncoupled'] += 1
        if acmod == 2 and br.bits(1):                   # rematstr
            self.rematflg = 0
            end = cpl['strtmant'] if self.chincpl else 253
            i = 0
            while True:
                self.rematflg |= br.bits(1) << i
                i += 1
                if not REMATRIX_BAND[i - 1] < end:
                    break
        cplexpstr = br.bits(2) if self.chincpl else EXP_REUSE
        chexpstr = [br.bits(2) for _ in range(nf)]
        lfeexpstr = br.bits(1) if lfeon else EXP_REUSE
        for i in range(nf):
            if chexpstr[i] != EXP_REUSE:
                if (self.chincpl >> i) & 1:
                    self.endmant[i] = cpl['strtmant']
                else:
                    chbwcod = br.bits(6)
                    if chbwcod > 60:
                        raise Ac3Error('UNMODELLED', f'chbwcod {chbwcod}')
                    self.endmant[i] = chbwcod * 3 + 73
        if cplexpstr != EXP_REUSE:
            ngrps = (cpl['endmant'] - cpl['strtmant']) // (3 << (cplexpstr - 1))
            absexp = br.bits(4) << 1
            ex = ungroup_exps(br, cplexpstr, ngrps, absexp)
            self.exp[CH_CPL][cpl['strtmant']:cpl['strtmant'] + len(ex)] = ex
        for i in range(nf):
            if chexpstr[i] != EXP_REUSE:
                gs = 3 << (chexpstr[i] - 1)
                ngrps = (self.endmant[i] + gs - 4) // gs
                e0 = br.bits(4)
                ex = ungroup_exps(br, chexpstr[i], ngrps, e0)
                self.exp[i][0] = e0
                self.exp[i][1:1 + len(ex)] = ex
                br.bits(2)                              # gainrng
        if lfeexpstr != EXP_REUSE:
            e0 = br.bits(4)
            ex = ungroup_exps(br, 1, 2, e0)
            self.exp[CH_LFE][0] = e0
            self.exp[CH_LFE][1:7] = ex
        if br.bits(1):                                  # baie
            self.g['bai'] = br.bits(11)
        if br.bits(1):                                  # snroffste
            self.g['csnroffst'] = br.bits(6)
            if self.chincpl:
                self.cplbai = br.bits(7)
            for i in range(nf):
                self.bai[i] = br.bits(7)
            if lfeon:
                self.lfebai = br.bits(7)
        if self.chincpl and br.bits(1):                 # cplleake
            self.cplfleak = 9 - br.bits(3)
            self.cplsleak = 9 - br.bits(3)
        # DEVIATION (the RTL): delta-BA resets to NONE at every block; only NEW applies.
        # liba52 resets it once a frame and keeps NEW (and REUSE) across blocks.
        if first or not self.liba52_deltba:
            self.deltbae = [DELTA_NONE] * 5
            self.cpldeltbae = DELTA_NONE
        deltbaie = br.bits(1)
        if not deltbaie and self.new_in_frame:
            st['deltbaie_off_after_new'] += 1
        if deltbaie:
            if self.chincpl:
                self.cpldeltbae = br.bits(2)
                if self.cpldeltbae == 3:
                    raise Ac3Error('DELTBAE', 'reserved')
            for i in range(nf):
                self.deltbae[i] = br.bits(2)
                if self.deltbae[i] == 3:
                    raise Ac3Error('DELTBAE', 'reserved')
            if self.chincpl and self.cpldeltbae == DELTA_NEW:
                raise Ac3Error('CPLDELTBA', 'coupling delta bit allocation is refused')
            for i in range(nf):
                if self.deltbae[i] == DELTA_NEW:
                    self.deltba[i] = self.parse_deltba(br)
            if any(self.deltbae[i] == DELTA_NEW for i in range(nf)):
                st['deltba_new'] += 1
                self.new_in_frame = True
            if any(self.deltbae[i] == DELTA_REUSE for i in range(nf)):
                st['deltba_reuse'] += 1
        if br.bits(1):                                  # skiple
            br.bits(8 * br.bits(9))
            st['skip'] += 1
        st['cpl'] += bool(self.chincpl)
        st['cplmerge'] += bool(self.chincpl and cpl['bndstrc'])
        st['remat'] += bool(acmod == 2 and self.rematflg)
        # ---- bit allocation (the RTL computes every channel, every block)
        bap = self.allocate(nf, lfeon)
        coeff, lfe = self.mantissas(br, nf, lfeon, acmod, dith, bap)
        out = dict(blksw=sum(b << i for i, b in enumerate(blksw)), dynrng=self.dynrng,
                   coeff=coeff, lfe=lfe)
        if self.snapshot:            # the block's exponents and baps (the A1a score)
            c = self.cpl
            out['exp'] = {ch: list(self.exp[ch][:self.endmant[ch]]) for ch in range(nf)}
            out['bap'] = {ch: [bap[ch].get(k, 0) for k in range(self.endmant[ch])]
                          for ch in range(nf)}
            if self.chincpl:
                rng = range(c['strtmant'], c['endmant'])
                out['exp'][CH_CPL] = [self.exp[CH_CPL][k] for k in rng]
                out['bap'][CH_CPL] = [bap[CH_CPL].get(k, 0) for k in rng]
            if lfeon:
                out['exp'][CH_LFE] = list(self.exp[CH_LFE][:7])
                out['bap'][CH_LFE] = [bap[CH_LFE].get(k, 0) for k in range(7)]
        return out

    def mantissas(self, br, nf, lfeon, acmod, dith, bap):
        """The block's mantissas -> (coeff[ch][256], lfe[7] or None), from the
        exponents, baps and coupling state in self; advances the dither LFSR."""
        mq = Mantissas(br)
        coeff = [[0] * 256 for _ in range(nf)]
        done_cpl = False
        for ch in range(nf):
            coupled = (self.chincpl >> ch) & 1
            em = self.endmant[ch]
            for k in range(em):
                coeff[ch][k] = self.one(mq, bap[ch].get(k, 0), self.exp[ch][k], dith[ch])
            if coupled and not done_cpl:
                done_cpl = True
                self.coupling(mq, nf, bap[CH_CPL], dith, coeff)
        lfe = None
        if lfeon:
            lfe = [self.one(mq, bap[CH_LFE].get(k, 0), self.exp[CH_LFE][k], 0) for k in range(7)]
        if acmod == 2 and self.rematflg:
            self.rematrix(coeff)
        return coeff, lfe

    def parse_deltba(self, br):
        d = [0] * 50
        nseg = br.bits(3)
        j = 0
        for _ in range(nseg + 1):
            j += br.bits(5)
            ln = br.bits(4)
            v = br.bits(3)
            v -= 3 if v >= 4 else 4
            if not ln:
                continue
            if j + ln >= 50:
                raise Ac3Error('DELTBA', 'a delta segment past band 49')
            for _ in range(ln):
                d[j] = v
                j += 1
        return d

    def allocate(self, nf, lfeon):
        zero = [0] * 50
        bap = {}
        zsnr = (not self.g['csnroffst'] and not (self.chincpl and self.cplbai >> 3) and
                not (lfeon and self.lfebai >> 3) and not any(self.bai[i] >> 3 for i in range(nf)))
        self.stats['zero_snr'] += zsnr
        for ch in range(nf):
            if zsnr:
                bap[ch] = {}
                continue
            d = self.deltba[ch] if self.deltbae[ch] == DELTA_NEW or (
                self.liba52_deltba and self.deltbae[ch] == DELTA_REUSE) else zero
            bap[ch] = bit_allocate(self.g, self.bai[ch], d, 0, 0, self.endmant[ch], 0, 0,
                                   self.exp[ch])
        bap[CH_CPL] = {}
        if self.chincpl and not zsnr:
            c = self.cpl
            bap[CH_CPL] = bit_allocate(self.g, self.cplbai, zero, c['strtbnd'], c['strtmant'],
                                       c['endmant'], self.cplfleak << 8, self.cplsleak << 8,
                                       self.exp[CH_CPL])
        bap[CH_LFE] = {}
        if lfeon and not zsnr:
            bap[CH_LFE] = bit_allocate(self.g, self.lfebai, zero, 0, 0, 7, 0, 0, self.exp[CH_LFE])
        return bap

    def one(self, mq, bap, e, dith):
        """One coefficient (Q1.23): zero, dither, or a dequantised mantissa."""
        return one_coeff(self, mq, bap, e, dith)

    def coupling(self, mq, nf, bap, dith, coeff):
        c = self.cpl
        i, bnd, strc = c['strtmant'], 0, c['bndstrc']
        while i < c['endmant']:
            iend = i + 12
            while strc & 1:
                strc >>= 1
                iend += 12
            strc >>= 1
            co = [(-self.cplco[ch][bnd] if ch == 1 and self.phsneg[bnd] else self.cplco[ch][bnd])
                  for ch in range(nf)]
            bnd += 1
            cpl_bins(self, mq, nf, self.chincpl, i, iend, co, lambda k: bap.get(k, 0),
                     lambda k: self.exp[CH_CPL][k], dith, coeff)
            i = iend

    def rematrix(self, coeff):
        rematrix_coeffs(self, coeff, min(self.endmant[0], self.endmant[1]), self.rematflg)


# ------------------------------------------------------------------ the mantissa ops
# Module-level so the engine emulator (tools/ac3_isa.py) runs the same code. `st` is
# whatever holds the dither LFSR (.lfsr) and the counters (.stats).
def one_coeff(st, mq, bap, e, dith):
    """One coefficient (Q1.23): zero, dither, or a dequantised mantissa."""
    if bap == 0:
        if dith:
            st.lfsr = dither_next(st.lfsr)
            return dither_coeff(st.lfsr, e)
        return 0
    return scale_coeff(mq.m16(bap), e)


def cpl_bins(st, mq, nf, chincpl, i, iend, co, bap_of, exp_of, dith, coeff):
    """Coupling bins [i, iend) of one band: each mantissa read ONCE and scattered into
    every coupled channel with its coordinate co[ch]; a bap-0 bin dithers each coupled,
    dithered channel in channel order (one LFSR step each) or zeroes it."""
    while i < iend:
        b, e = bap_of(i), exp_of(i)
        cc = None if b == 0 else scale_coeff(mq.m16(b), e)
        for ch in range(nf):
            if (chincpl >> ch) & 1:
                if cc is None:
                    if dith[ch]:
                        st.lfsr = dither_next(st.lfsr)
                        coeff[ch][i] = recombine(dither_coeff(st.lfsr, e), co[ch])
                    else:
                        coeff[ch][i] = 0
                else:
                    coeff[ch][i] = recombine(cc, co[ch])
                    if abs((cc * co[ch]) >> 18) > 8388607:
                        st.stats['recomb_sat'] += 1
        i += 1


def rematrix_coeffs(st, coeff, end, flags):
    """2/0 rematrixing of bins [13, end) by the active rematrix bands (saturating)."""
    if end <= 13:
        return
    j, i, flg = 13, 0, flags
    while j < end:
        if not flg & 1:
            flg >>= 1
            j = REMATRIX_BAND[i]
            i += 1
            continue
        flg >>= 1
        band = min(REMATRIX_BAND[i], end)
        i += 1
        while j < band:
            l, r = coeff[0][j], coeff[1][j]
            coeff[0][j], coeff[1][j] = sat24(l + r), sat24(l - r)
            if sat24(l + r) != l + r or sat24(l - r) != l - r:
                st.stats['remat_sat'] += 1
            j += 1


# ------------------------------------------------------------------ streams
def frames(buf):
    """Yield (offset, frame bytes) of consecutive 48 kHz frames, resyncing on
    0x0B77 at a byte boundary (the RTL's own search)."""
    i, n = 0, len(buf)
    while i + 5 <= n:
        if buf[i] != 0x0B or buf[i + 1] != 0x77:
            i += 1
            continue
        fs, code = buf[i + 4] >> 6, buf[i + 4] & 63
        if fs != 0 or code >= 38:
            i += 1
            continue
        ln = 2 * FRMSIZ_48K[code]
        if i + ln > n:
            return
        yield i, buf[i:i + ln]
        i += ln


def read_golden(path):
    """-> [frame dict {hdr, blocks:[{blksw, dynrng, coeff:{ch: [...]}}], err}]"""
    out = []
    for line in open(path):
        t = line.split()
        if not t:
            continue
        if t[0] == 'F':
            out.append(dict(start=int(t[2]), hdr=dict(acmod=int(t[4]), lfeon=int(t[5]),
                                                       cmixlev=int(t[6]), surmixlev=int(t[7])),
                            blocks=[], err=False))
        elif t[0] == 'B':
            out[-1]['blocks'].append(dict(blksw=int(t[3]), dynrng=int(t[4]), coeff={}))
        elif t[0] == 'C':
            out[-1]['blocks'][-1]['coeff'][int(t[1])] = [s24(int(v, 16)) for v in t[2:]]
        elif t[0] == 'E':
            k = int(t[1])
            while k >= len(out):        # refused before its header record (sync / BSI)
                out.append(dict(start=None, hdr=None, blocks=[], err=False))
            out[k]['err'] = True
    return out


def compare(stream, golden, nframes=0):
    """-> (frames compared, blocks, mismatching values, first mismatch text)."""
    gold = read_golden(golden)
    dec = Decoder()
    nf = nb = bad = 0
    first = None
    for k, (off, fr) in enumerate(frames(open(stream, 'rb').read())):
        if k >= len(gold) or (nframes and k >= nframes):
            break
        g = gold[k]
        if g['start'] is not None and g['start'] != off:
            return nf, nb, bad + 1, f'frame {k}: the RTL found it at byte {g["start"]}, the model at {off}'
        try:
            hdr, blocks = dec.frame(fr)
        except Ac3Error as e:
            if g['err']:
                nf += 1
                break                                    # both refuse: the RTL halts here
            return nf, nb, bad + 1, f'frame {k}: the model refused ({e}), the RTL did not'
        if g['err'] and hdr['acmod'] == 0 and not g['blocks']:
            # dvd/ac3 (the RTL the goldens come from, retired from the core) refuses 1+1
            # dual mono; the engine and this model decode it (docs/lpcm_full.md §7). It
            # halts there, so the comparison ends, as when both refuse.
            nf += 1
            break
        if g['hdr'] is None:
            return nf, nb, bad + 1, f'frame {k}: the RTL refused it before its header, the model did not'
        for key in ('acmod', 'lfeon', 'cmixlev', 'surmixlev'):
            if hdr[key] != g['hdr'][key]:
                bad += 1
                first = first or f'frame {k}: {key} {hdr[key]} vs the RTL {g["hdr"][key]}'
        for b, (mb, gb) in enumerate(zip(blocks, g['blocks'])):
            nb += 1
            for key in ('blksw', 'dynrng'):
                if mb[key] != gb[key]:
                    bad += 1
                    first = first or f'frame {k} block {b}: {key} {mb[key]} vs the RTL {gb[key]}'
            chans = dict(enumerate(mb['coeff']))
            if mb['lfe'] is not None:
                chans[CH_LFE] = mb['lfe']
            for ch, vals in gb['coeff'].items():
                mv = chans.get(ch)
                if mv is None:
                    bad += len(vals)
                    first = first or f'frame {k} block {b}: the model has no channel {ch}'
                    continue
                for i, (x, y) in enumerate(zip(mv, vals)):
                    if x != y:
                        bad += 1
                        first = first or (f'frame {k} block {b} ch {ch} bin {i}: model {x}, '
                                          f'the RTL {y}')
        if len(blocks) != len(g['blocks']):
            bad += 1
            first = first or f'frame {k}: {len(g["blocks"])} RTL blocks, model {len(blocks)}'
        nf += 1
    return nf, nb, bad, first


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('compare')
    p.add_argument('stream')
    p.add_argument('golden')
    p.add_argument('--frames', type=int, default=0)
    p.add_argument('--liba52-deltba', action='store_true')
    a = ap.parse_args()
    if a.liba52_deltba:
        OPT.add('liba52_deltba')
    nf, nb, bad, first = compare(a.stream, a.golden, a.frames)
    print(f'ac3_model: {os.path.basename(a.stream)}: {nf} frames, {nb} blocks, '
          f'{bad} values differ from the RTL' + (f'; first: {first}' if first else ''))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
