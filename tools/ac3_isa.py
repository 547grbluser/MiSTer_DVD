#!/usr/bin/env python3
"""ac3_isa.py -- the AC-3 parse as a program for the shared audio engine (A1).

docs/ac3_engine.md. The machine is tools/dts_isa.py's (the same sequencer, ISA
and assembler; its sequencer cycle costs are RTL-calibrated); this file adds
AC-3's program (dvd/dts/ac3.uasm), its constant ROM and record map, and its
vector ops, each of which calls tools/ac3_model.py's own function, so the
emulator is anchored to the model and the model to the RTL.

The ops (numbered after DTS's: one engine, one op space). The sequencer-side units
are BUILT (dvd/dts/dts_seq.sv, A2b) and their charge is the RTL's own cycle count in
the unit's states, structure for structure (unit_cycles below; bench/dvd/
run_ac3_seq.sh's [cycles] arm checks it on every stall-free arm). The vector side is
not built yet (A2c), so its charges are MODELLED and carry the headroom factor
(CYC_HEADROOM); test_ac3_isa.py's budget applies it to those alone:
  EXPD     exponent groups -> the bins' exp fields: per group 7 code bits + 1, two
           in-place divisions by 5 (14), 3 x rep record writes.
  BAPSD    a band's integrated PSD (liba52's log-add) -> F_PSD: 2 a bin.
  BAPFILL  bap = baptab[156 + mask + 4 exp] over a band, pipelined: 1 a bin + 2.
  BAPZERO  bap = 0 over [from, to) (zero SNR offsets): 1 a bin + 1.
  QRST     empty the grouped-quantizer caches: none (done at the vop's dispatch).
  AQ       one channel's bins [from, to): 1 + per bin 2 + its path (bin_cycles: the
           code bits + 1, a fresh grouped code's divisions, the level read). The
           vector engine (scale, dither x 23170) keeps up; modelled: its tail.
  AQC      one coupling band: the header (nf, chincpl, phase, nf dither flags), each
           coupled channel's coordinate (a serial shift, its exponent + 3), then the
           bins as AQ. Modelled: the recombine's one write a coupled channel a bin
           where it outruns the bin's sequencer time, and the tail.
  CZERO    a channel's zero tail: 1 a bin + 2.     REMAT  2/0 rematrix: 4 a bin + 3.
  IMDCT    the block's coefficients are ready (the handshake that starts
           imdct_512, which runs in series): 2.     (all three modelled)

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
CYC_HEADROOM = 1.25            # on the MODELLED (vector-side) charges only
CYC = {'vec_tail': 2, 'czero': 2, 'remat': 3, 'imdct': 2}     # the modelled ones


def bin_cycles(bap, nbits):
    """One bin of AQ / AQC in dts_seq.sv: S_AQ_D and S_AQ_E (2), then its path --
    bap 0 none; a cached grouped digit S_AQ_Q + S_AQ_L; a fresh grouped code its bits +
    1 (S_AQ_G), its divisions in place (S_UV: nb each, two for 3- and 5-level, one for
    11-level), S_AQ_Q + S_AQ_L; bap 3 / 4 the bits + 1 and the level read; a direct
    code the bits + 1."""
    if bap == 0:
        return 2
    if bap in (-1, -2, -3):
        if nbits == 0:
            return 4
        return 2 + (nbits + 1) + nbits * (1 if bap == -3 else 2) + 2
    if bap in (3, 4):
        return 2 + nbits + 2
    return 2 + nbits + 1


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
    put('LATAB', M.LATAB)                 # (read by the BAPSD unit through this ROM's port)
    put('BINBASE', BINBASE)
    # the mantissa unit's level tables, one block (read through this ROM's port):
    # 3-level at +0, 5-level +3, 7-level (bap 3) +8, 11-level +16, 15-level (bap 4) +27
    put('MLEV', M.Q1LEV + M.Q2LEV + M.Q3LEV + M.Q4LEV + M.Q5LEV)
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


class _RecMq:
    """The grouped caches, recording the m16 each coefficient took (the value the
    mantissa unit hands the vector engine)."""

    def __init__(self, mq):
        self.mq, self.last = mq, 0

    def m16(self, bap):
        self.last = self.mq.m16(bap)
        return self.last


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
        self.ucyc = {}                   # op -> cycles in its unit's states (the RTL's)
        self.vec_model = 0               # the modelled (vector-side) cycles, all frames
        self.czero_nz = 0                # CZEROs that zeroed a nonzero coefficient

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

    def op_store(self, a, v):
        """A record write by an op: traced as kind 3, in the order the unit issues it."""
        self.rec[a & 0x7FF] = v & 0xFFFF
        if self.trace is not None:
            self.trace.append((3, self.pc, a & 0x7FF, v & 0xFFFF))

    def emit(self, addr, value):
        """An item the mantissa unit hands the vector engine: traced as kind 2."""
        if self.trace is not None:
            self.trace.append((2, self.pc, addr & 0x7FF, value & 0xFFFFFF))

    def coded(self, bap, b0):
        """A bin's cycles in the mantissa unit (bin_cycles), from the bits it read."""
        return bin_cycles(bap, self.bitpos - b0)

    def charge(self, op, unit, vec=0):
        """-> op_t for vop(): `unit` cycles in the op's own states (the RTL's exact
        count; the vop's dispatch cycle is the instruction's) plus `vec` modelled ones."""
        self.ucyc[op] = self.ucyc.get(op, 0) + unit
        self.vec_model += vec
        return unit + vec - (D.CYC['vop'] - D.CYC['instr'])

    def vop(self, op):
        a = [self.reg[i] for i in range(8, 16)]
        name = VOP_NAME[op]
        c0 = self.cycles
        op_t = 0
        try:
            op_t = self.run_op(name, a, op)
        except M.Ac3Error:
            self.op_err = E_GROUP if name in ('aq', 'aqc') else E_EXP
            return
        if op_t is None:
            return
        self.cycles = c0 + D.CYC['vop'] - D.CYC['instr'] + op_t
        self.by_cat[name] = self.by_cat.get(name, 0) + self.cycles - c0
        if self.vop_hook is not None:
            self.vop_hook(self, op)

    def run_op(self, name, a, op=None):
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
                        self.op_store(base + idx, e)
                        idx += 1
            return self.charge(op, ngrps * (8 + 14 + 3 * rep))
        if name == 'bapsd':
            base, j, eb = a[0], a[1], a[2]
            ex = self.exp_of(base)

            class V:
                def __getitem__(_, k):
                    return ex(k)
            self.op_store(F_PSD, M.ba_band_psd(V(), j, eb))
            return self.charge(op, 2 * max(eb - j, 1))
        if name == 'bapfill':
            base, j, eb, mask = a[0], a[1], a[2], a[3]
            ex = self.exp_of(base)

            class V:
                def __getitem__(_, k):
                    return ex(k)
            for k, b in M.ba_bap_fill(mask, V(), j, eb).items():
                w = self.rec[(base + k) & 0x7FF]
                self.op_store(base + k, ((b & 63) << 8) | (w & 31))
            return self.charge(op, eb - j + 2 if eb > j else 1)
        if name == 'bapzero':
            base, j, eb = a[0], a[1], a[2]
            for k in range(j, eb):
                self.op_store(base + k, self.rec[(base + k) & 0x7FF] & 31)
            return self.charge(op, max(eb - j, 0) + 1)
        if name == 'qrst':
            self.mq = M.Mantissas(_Bits(self))
            return self.charge(op, 0)
        if name == 'aq':                 # one channel's bins [from, to)
            slot, base, lo, hi, dith = a[0], a[1], a[2], a[3], a[4]
            bap, ex = self.bap_of(base), self.exp_of(base)
            t = 1 if lo < hi else 0              # S_AQ_R
            rq = _RecMq(self.mq)
            for k in range(lo, hi):
                b0, bp, e = self.bitpos, bap(k), ex(k)
                rq.last = 0
                self.coef[slot][k] = M.one_coeff(self, rq, bp, e, dith)
                # to the vector engine: {dither [23], bap 0 [22], exp [21:17], m16 [16:0]}
                self.emit((slot << 8) | k, (rq.last & 0x1FFFF) | (e << 17) |
                          ((bp == 0) << 22) | ((bp == 0 and dith) << 23))
                t += self.coded(bp, b0)
            self.mant_cycles += t
            return self.charge(op, t, CYC['vec_tail'])
        if name == 'aqc':                # one coupling band, bins [from, to)
            band, lo, hi = a[0], a[1], a[2]
            nf, chincpl = self.rec[0x001], self.rec[0x007]
            co = [self.cplco(ch, band) for ch in range(nf)]
            dith = [self.rec[0x048 + ch] for ch in range(nf)]
            bap, ex = self.bap_of(BINBASE[5]), self.exp_of(BINBASE[5])
            ncpl = bin(chincpl & ((1 << nf) - 1)).count('1')
            # the header: 4 reads, nf dither flags, the channel set, nf + 1 channel
            # steps, and per coupled channel its coordinate (read, shift by its
            # exponent, send); then S_AQ_R
            t = 4 + nf + 1 + (nf + 1) + 1
            v = 0
            for ch in range(nf):
                if (chincpl >> ch) & 1:
                    t += (self.rec[0x0A0 + ch * 18 + band] & 31) + 3
            # to the vector engine: the band's channel set, then each coupled channel's
            # coordinate (Q5.18, the phase applied), then the bins
            self.emit(0x7F0, sum(d << c for c, d in enumerate(dith)) | (chincpl << 5) | (nf << 10))
            for ch in range(nf):
                if (chincpl >> ch) & 1:
                    self.emit(0x700 + ch, co[ch])
            rq = _RecMq(self.mq)
            for k in range(lo, hi):
                b0, bp, e = self.bitpos, bap(k), ex(k)
                rq.last = 0
                M.cpl_bins(self, rq, nf, chincpl, k, k + 1, co, bap, ex, dith, self.coef)
                self.emit((5 << 8) | k, (rq.last & 0x1FFFF) | (e << 17) | ((bp == 0) << 22))
                c = self.coded(bp, b0)
                t += c
                v += max(0, ncpl - c)           # the recombine's writes, where they outrun it
            self.mant_cycles += t
            return self.charge(op, t, v + CYC['vec_tail'])
        if name == 'czero':
            slot, lo, hi = a[0], a[1], a[2]
            if any(self.coef[slot][k] for k in range(lo, min(hi, 256))):
                self.czero_nz += 1             # it zeroed a live coefficient (a RED arm's need)
            for k in range(lo, min(hi, 256)):
                self.coef[slot][k] = 0
            n = max(0, min(hi, 256) - lo)
            return self.charge(op, 0, CYC['czero'] + n)
        if name == 'remat':
            flags, end = a[0], a[1]
            before = [list(self.coef[0]), list(self.coef[1])]
            M.rematrix_coeffs(self, self.coef, end, flags)
            n = sum(1 for k in range(256) if self.coef[0][k] != before[0][k] or
                    self.coef[1][k] != before[1][k])
            return self.charge(op, 0, CYC['remat'] + 4 * max(n, 0))   # 2 reads + 2 writes a bin
        if name == 'imdct':
            self.snapshot()
            b, r = self.blocks[-1], self.rec
            side = (b['blksw'], b['dynrng'], r[0x000], r[0x002], r[0x004], r[0x005])
            if tuple(x & 0xFFFF for x in a[:6]) != side:       # the args imdct_512 takes
                raise D.EngineError(f'IMDCT args {a[:6]} are not the record\'s {side}')
            b['side'] = side
            return self.charge(op, 0, CYC['imdct'])
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


MLEV_OFF = {-1: 0, -2: 3, 3: 8, -3: 16, 4: 27}
IP_DITH = 768                  # the dither LFSR's table in dts_vec's IMDCT program ROM


def iprog_words(prog):
    """dts_vec's iprog ROM: DTS's IMDCT program, then (at IP_DITH) the dither LFSR's
    table, which AC-3's AQ / AQC read: AC-3 never runs that program."""
    assert len(prog) <= IP_DITH
    return list(prog) + [0] * (IP_DITH - len(prog)) + list(M.DITHER_LUT)


def svh_lines():
    """AC-3's part of dvd/dts/dts_ucode.svh: the constant-ROM tables its sequencer
    units read (word offsets in that ROM), the record words they touch, the op numbers
    and the refusal codes."""
    c = {k: v - D.MEM_CONST for k, v in CBASE.items()}
    return [
        '// AC-3 (docs/ac3_engine.md A2): the units\' constant-ROM tables (word offsets),',
        '// record words, op numbers and refusal codes',
        f"localparam [9:0]  AC_LATAB   = 10'd{c['LATAB']};",
        f"localparam [9:0]  AC_BAPTAB  = 10'd{c['BAPTAB']};",
        f"localparam [9:0]  AC_MLEV    = 10'd{c['MLEV']};       // + 0 / 3 / 8 / 16 / 27",
        f"localparam [10:0] AC_CPLBASE = 11'd{BINBASE[5]};",
        f"localparam [10:0] AC_F_PSD   = 11'd{F_PSD};",
        f"localparam [4:0]  AC_E_EXP   = 5'd{E_EXP};",
        f"localparam [4:0]  AC_E_GROUP = 5'd{E_GROUP};",
    ] + [f"localparam [5:0]  V_{k.upper():8s}= 6'd{v};" for k, v in sorted(VOPS.items(), key=lambda kv: kv[1])
         if k != 'cnt']


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
