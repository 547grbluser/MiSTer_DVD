#!/usr/bin/env python3
"""ac3_isa.py -- the AC-3 parse as a program for the shared audio engine (A1).

docs/ac3_engine.md. The machine is tools/dts_isa.py's (the same sequencer, ISA
and assembler; its sequencer cycle costs are RTL-calibrated); this file adds
AC-3's program (dvd/dts/ac3.uasm), its constant ROM and record map, and its
vector ops, each of which calls tools/ac3_model.py's own function, so the
emulator is anchored to the model and the model to the RTL.

The ops (numbered after DTS's: one engine, one op space) and the hardware each
charge is DERIVED FROM -- none of it built yet, so the cycle figure carries a
headroom factor (CYC_HEADROOM) and says which part is modelled:
  EXPD     exponent groups -> the bins' exp fields. A sequencer-side unit on the
           bit reader: 7 code bits a group overlap the 3 x rep record writes ->
           max(7, 3 rep) + 1 a group, + 4.
  BAPSD    a band's integrated PSD (liba52's log-add) -> F_PSD. One record read and
           one latab lookup a bin, pipelined: 1 a bin + 4.
  BAPFILL  bap = baptab[156 + mask + 4 exp] over a band: read, look up, write back,
           pipelined on the 1R1W record RAM: 1 a bin + 4.
  BAPZERO  bap = 0 over [from, to) (zero SNR offsets): 1 a bin + 3.
  QRST     empty the grouped-quantizer caches (the block's mantissa stage starts): 2.
  AQ       one channel's bins [from, to): zero, dither or a dequantised mantissa
           (the model's one_coeff). A sequencer-side unit on the bit reader and the
           record, the scale (a shift) and dither (x 23170) on the vector side:
           1 a bin + its code bits + a fresh grouped code's divisions, + 3.
  AQC      one coupling band: each bin read once and scattered into every coupled
           channel (cpl_bins): as AQ, + 1 a coupled channel a bin (the recombine on
           the multiplier, one write each), + 2 + nf coordinate reads.
  CZERO    a channel's zero tail: 1 a bin + 2.     REMAT  2/0 rematrix: 4 a bin + 3.
  IMDCT    the block's coefficients are ready (the handshake that starts
           imdct_512, which runs in series): 2.

Usage:
    tools/ac3_isa.py --asm [--check]            # assemble -> dvd/dts/ac3_*.mem
    tools/ac3_isa.py --run STREAM.ac3 [--frames N]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ac3_model as M   # noqa: E402
import dts_isa as D     # noqa: E402

REPO = os.path.dirname(HERE)
UASM = os.path.join(REPO, 'dvd', 'dts', 'ac3.uasm')

# AC-3's ops are numbered from 16 (the vop field is 6 bits; DTS's take 0-8 and share CNT)
VOPS = {'cnt': 8, 'expd': 16, 'bapsd': 17, 'bapfill': 18, 'bapzero': 19, 'qrst': 20, 'aq': 21,
        'aqc': 22, 'czero': 23, 'remat': 24, 'imdct': 25}
VOP_NAME = {v: k for k, v in VOPS.items()}
BINBASE = [0x100, 0x200, 0x300, 0x400, 0x500, 0x600 - 37, 0x6D8]   # slots 0-4, cpl, lfe
F_PSD = 0x019
E_EXP, E_GROUP = 9, 10
CYC_HEADROOM = 1.25            # on the modelled op charges (no RTL yet)
CYC = {'expd': 4, 'bapsd': 4, 'bapfill': 4, 'bapzero': 3, 'qrst': 2, 'aq': 3, 'aqc': 2,
       'czero': 2, 'remat': 3, 'imdct': 2}
# a fresh grouped code: its digits come out of the restoring divider low digit first
# (the first while the code's bits arrive), and the HIGH digit is used first, so all
# but one division precede the first coefficient: (digits - 1) x nb extra cycles
GROUP_EXTRA = {-1: 2 * 5, -2: 2 * 7, -3: 1 * 7}


def build_const():
    """AC-3's constants, placed after DTS's in the shared constant ROM."""
    words, base = [], {}

    def put(name, vals):
        base[name] = D.MEM_CONST + len(D.CONST) + len(words)
        words.extend(v & 0xFFFF for v in vals)
    put('NFCHANS', M.NFCHANS)
    put('CPLBNDTAB', M.CPL_BNDTAB)
    put('REMATBAND', M.REMATRIX_BAND)
    put('HTH', M.HTH)
    put('BNDTAB', M.BNDTAB)
    put('SLOWGAIN', M.SLOWGAIN)
    put('DBPBTAB', M.DBPBTAB)
    put('FLOORTAB', M.FLOORTAB)
    put('BAPTAB', M.BAPTAB)
    put('BINBASE', BINBASE)
    return words, base


CONST, CBASE = build_const()


def load_program(mutate=None):
    """-> (the whole ROM image: DTS's program then AC-3's, AC-3's labels). AC-3 is
    assembled at the origin where DTS's program ends, so its pcs are the RTL's."""
    dw, _ = D.load_program()
    words, labels, _ = D.assemble(open(UASM).read(), mutate, vops=VOPS, cbase=CBASE,
                                  org=len(dw))
    return dw + words, labels


class _Bits:
    """The model's BitReader interface over the engine's bit reader."""

    def __init__(self, m):
        self.m = m

    def bits(self, n):
        return self.m.bits(n)


class Machine(D.Machine):
    """The sequencer, AC-3's ops and the engine state they keep:
      coef[slot][256]   the coefficient buffer (the vector engine's X buffer, reused:
                        slots 0-4 fbw, 6 LFE; 24-bit Q1.23) -- imdct_512 reads it
      mq                the grouped-quantizer caches (sequencer-side, in the mantissa
                        unit), emptied by QRST at each block's mantissa stage
      lfsr              the dither LFSR (vector-engine register; power-up only)"""

    def __init__(self, words, labels):
        super().__init__(words, labels)
        self.const = D.CONST + CONST       # the shared constant ROM
        self.op_err = None
        self.coef = [[0] * 256 for _ in range(7)]
        self.mq = None
        self.lfsr = 1
        self.stats = dict.fromkeys(M.STATS, 0)
        self.blocks = []                 # per block: blksw, dynrng, exp, bap, coeff, lfe
        self.mant_cycles = 0

    def exp_of(self, base):
        return lambda k: self.rec[(base + k) & 0x7FF] & 31

    def bap_of(self, base):
        def f(k):
            b = (self.rec[(base + k) & 0x7FF] >> 8) & 63
            return b - 64 if b & 32 else b
        return f

    def cplco(self, ch, band):
        """A packed coordinate {m, e == 15, e + mstr} -> Q5.18, ch1's phase applied."""
        w = self.rec[0x0A0 + ch * 18 + band]
        idx, f15, m = w & 31, (w >> 5) & 1, (w >> 6) & 15
        full = (m << 14) if f15 else ((m | 0x10) << 13)
        v = ((full << 3) >> idx) & 0xFFFFFF
        return -v if ch == 1 and self.rec[0x088 + band] & 1 else v

    def coded(self, bap, b0):
        """Cycles for one coefficient: dispatch, its code bits, a fresh code's division."""
        nbits = self.bitpos - b0
        return 1 + nbits + (GROUP_EXTRA.get(bap, 0) if nbits else 0)

    def vop(self, op):
        a = [self.reg[i] for i in range(8, 16)]
        name = VOP_NAME[op]
        c0 = self.cycles
        op_t = 0
        try:
            op_t = self.run_op(name, a)
        except M.Ac3Error:
            self.op_err = E_GROUP if name in ('aq', 'aqc') else E_EXP
            return
        if op_t is None:
            return
        self.cycles = c0 + D.CYC['vop'] - D.CYC['instr'] + op_t
        self.by_cat[name] = self.by_cat.get(name, 0) + self.cycles - c0
        if self.vop_hook is not None:
            self.vop_hook(self, op)

    def run_op(self, name, a):
        if name == 'expd':
            base, start, ngrps, expstr, e = a[0], a[1], a[2], a[3], a[4]
            rep = (1, 2, 4)[expstr - 1]
            idx = start
            for _ in range(ngrps):
                code = self.bits(7)
                for d in (code // 25, (code % 25) // 5, code % 5):
                    e += d - 2
                    if e < 0 or e > 24:
                        self.op_err = E_EXP
                        return None
                    for _ in range(rep):
                        self.rec[(base + idx) & 0x7FF] = e
                        idx += 1
            return CYC['expd'] + ngrps * (max(7, 3 * rep) + 1)
        if name == 'bapsd':
            base, j, eb = a[0], a[1], a[2]
            ex = self.exp_of(base)

            class V:
                def __getitem__(_, k):
                    return ex(k)
            self.rec[F_PSD] = M.ba_band_psd(V(), j, eb) & 0xFFFF
            return CYC['bapsd'] + (eb - j)
        if name == 'bapfill':
            base, j, eb, mask = a[0], a[1], a[2], a[3]
            ex = self.exp_of(base)

            class V:
                def __getitem__(_, k):
                    return ex(k)
            for k, b in M.ba_bap_fill(mask, V(), j, eb).items():
                w = self.rec[(base + k) & 0x7FF]
                self.rec[(base + k) & 0x7FF] = ((b & 63) << 8) | (w & 31)
            return CYC['bapfill'] + (eb - j)
        if name == 'bapzero':
            base, j, eb = a[0], a[1], a[2]
            for k in range(j, eb):
                self.rec[(base + k) & 0x7FF] &= 31
            return CYC['bapzero'] + (eb - j)
        if name == 'qrst':
            self.mq = M.Mantissas(_Bits(self))
            return CYC['qrst']
        if name == 'aq':                 # one channel's bins [from, to)
            slot, base, lo, hi, dith = a[0], a[1], a[2], a[3], a[4]
            bap, ex = self.bap_of(base), self.exp_of(base)
            t = CYC['aq']
            for k in range(lo, hi):
                b0, bp = self.bitpos, bap(k)
                self.coef[slot][k] = M.one_coeff(self, self.mq, bp, ex(k), dith)
                t += self.coded(bp, b0)
            self.mant_cycles += t
            return t
        if name == 'aqc':                # one coupling band, bins [from, to)
            band, lo, hi = a[0], a[1], a[2]
            nf, chincpl = self.rec[0x001], self.rec[0x007]
            co = [self.cplco(ch, band) for ch in range(nf)]
            dith = [self.rec[0x048 + ch] for ch in range(nf)]
            bap, ex = self.bap_of(BINBASE[5]), self.exp_of(BINBASE[5])
            ncpl = bin(chincpl & ((1 << nf) - 1)).count('1')
            t = CYC['aqc'] + nf                 # the band's coordinates, one read each
            for k in range(lo, hi):
                b0, bp = self.bitpos, bap(k)
                M.cpl_bins(self, self.mq, nf, chincpl, k, k + 1, co, bap, ex, dith, self.coef)
                t += self.coded(bp, b0) + ncpl  # + a scatter write a coupled channel
            self.mant_cycles += t
            return t
        if name == 'czero':
            slot, lo, hi = a[0], a[1], a[2]
            for k in range(lo, min(hi, 256)):
                self.coef[slot][k] = 0
            return CYC['czero'] + max(0, min(hi, 256) - lo)
        if name == 'remat':
            flags, end = a[0], a[1]
            before = [list(self.coef[0]), list(self.coef[1])]
            M.rematrix_coeffs(self, self.coef, end, flags)
            n = sum(1 for k in range(256) if self.coef[0][k] != before[0][k] or
                    self.coef[1][k] != before[1][k])
            return CYC['remat'] + 4 * max(n, 0)     # 2 reads + 2 writes a bin
        if name == 'imdct':
            self.snapshot()
            return CYC['imdct']
        if name == 'cnt':
            return D.CYC['cnt']
        raise D.EngineError(f'unknown op {name}')

    def snapshot(self):
        """The block as imdct_512 takes it (and the exps/baps the A1 score reads)."""
        r = self.rec
        nf, lfeon, chincpl = r[0x001], r[0x002], r[0x007]
        exp, bp = {}, {}
        for ch in range(nf):
            em = r[0x058 + ch]
            exp[ch] = [self.exp_of(BINBASE[ch])(k) for k in range(em)]
            bp[ch] = [self.bap_of(BINBASE[ch])(k) for k in range(em)]
        if chincpl:
            rng = range(r[0x00C], r[0x00D])
            exp[M.CH_CPL] = [self.exp_of(BINBASE[5])(k) for k in rng]
            bp[M.CH_CPL] = [self.bap_of(BINBASE[5])(k) for k in rng]
        if lfeon:
            exp[M.CH_LFE] = [self.exp_of(BINBASE[6])(k) for k in range(7)]
            bp[M.CH_LFE] = [self.bap_of(BINBASE[6])(k) for k in range(7)]
        self.blocks.append(dict(blksw=sum(r[0x040 + ch] << ch for ch in range(nf)),
                                dynrng=r[0x006], exp=exp, bap=bp,
                                coeff=[list(self.coef[ch]) for ch in range(nf)],
                                lfe=list(self.coef[6][:7]) if lfeon else None))


def emulate(path, nframes=0, mutate=None):
    """-> (machine, the model's blocks per frame, frames compared, first mismatch)."""
    words, labels = load_program(mutate)
    m = Machine(words, labels)
    ref = M.Decoder()
    ref.snapshot = True
    bad, first, nf = 0, None, 0
    for k, (_, fr) in enumerate(M.frames(open(path, 'rb').read())):
        if nframes and k >= nframes:
            break
        n0, e0 = len(m.blocks), sum(m.errors.values())
        m.feed(fr)
        m.run()
        refused = sum(m.errors.values()) > e0
        try:
            _, gblocks = ref.frame(fr)
        except M.Ac3Error as e:
            if not refused:
                bad += 1
                first = first or f'frame {k}: the model refused ({e}), the engine did not'
            break                                     # both stop: the RTL halts here
        if refused:
            bad += 1
            first = first or f'frame {k}: the engine refused ({m.errors}), the model did not'
            break
        for b, (eb, gb) in enumerate(zip(m.blocks[n0:], gblocks)):
            for key in ('blksw', 'dynrng'):
                if eb[key] != gb[key]:
                    bad += 1
                    first = first or f'frame {k} block {b}: {key} {eb[key]} vs {gb[key]}'
            for key in ('exp', 'bap'):
                for ch in gb[key]:
                    if eb[key].get(ch) != gb[key][ch]:
                        bad += 1
                        if first is None:
                            ev, gv = eb[key].get(ch) or [], gb[key][ch]
                            i = next((i for i, (x, y) in enumerate(zip(ev, gv)) if x != y), None)
                            first = (f'frame {k} block {b} ch {ch}: {key}' +
                                     (f'[{i}] {ev[i]} vs the model {gv[i]}' if i is not None
                                      else f' length {len(ev)} vs {len(gv)}'))
            if eb['coeff'] != gb['coeff'] or eb['lfe'] != gb['lfe']:
                bad += 1
                first = first or f'frame {k} block {b}: coefficients differ'
        if len(m.blocks) - n0 != len(gblocks):
            bad += 1
            first = first or f'frame {k}: {len(m.blocks) - n0} blocks, the model {len(gblocks)}'
        nf += 1
    return m, nf, bad, first


def write_mems(check=False):
    """The engine's images are written by tools/dts_isa.py --asm (both programs, one
    ROM); this checks or writes them the same way."""
    return D.write_mems(check)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--asm', action='store_true')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--run')
    ap.add_argument('--frames', type=int, default=0)
    a = ap.parse_args()
    if a.asm:
        return write_mems(a.check)
    if a.run:
        m, nf, bad, first = emulate(a.run, a.frames)
        fc = m.frame_cycles
        print(f'ac3_isa: {nf} frames, {len(m.blocks)} blocks, {bad} mismatches vs the model'
              + (f' (first: {first})' if first else '') + f'; errors {m.errors or "none"}')
        if fc:
            print(f'  cycles a frame: max {max(fc)}, mean {sum(fc) // len(fc)} '
                  f'(of which the mantissa ops {m.mant_cycles // max(len(fc), 1)} a frame); '
                  f'by op {dict(sorted(m.by_cat.items()))}')
        return 1 if bad else 0
    ap.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
