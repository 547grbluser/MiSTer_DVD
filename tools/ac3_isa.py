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
  MANTMODEL  A1a's STAND-IN for the mantissa, coupling and rematrix ops (A1b): it
           decodes them with the model's code from THIS program's exponents and
           baps, so a whole stream runs and every block is scored. Its charge is
           an estimate of the A1b ops (1 a code bit + 2 a coefficient written),
           reported separately.

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
UMEM = os.path.join(REPO, 'dvd', 'dts', 'ac3_ucode.mem')
CMEM = os.path.join(REPO, 'dvd', 'dts', 'ac3_const.mem')

VOPS = {'cnt': 8, 'expd': 9, 'bapsd': 10, 'bapfill': 11, 'bapzero': 12, 'mantmodel': 15}
VOP_NAME = {v: k for k, v in VOPS.items()}
BINBASE = [0x100, 0x200, 0x300, 0x400, 0x500, 0x600 - 37, 0x6D8]   # slots 0-4, cpl, lfe
F_PSD = 0x019
E_EXP, E_GROUP = 9, 10
CYC_HEADROOM = 1.25            # on the modelled op charges (no RTL yet)
CYC = {'expd': 4, 'bapsd': 4, 'bapfill': 4, 'bapzero': 3, 'mant_bit': 1, 'mant_coeff': 2}


def build_const():
    words, base = [], {}

    def put(name, vals):
        base[name] = D.MEM_CONST + len(words)
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
    words, labels, _ = D.assemble(open(UASM).read(), mutate, vops=VOPS, cbase=CBASE)
    return words, labels


class _Bits:
    """The model's BitReader interface over the engine's bit reader."""

    def __init__(self, m):
        self.m = m

    def bits(self, n):
        return self.m.bits(n)


class Machine(D.Machine):
    def __init__(self, words, labels):
        super().__init__(words, labels)
        self.const = CONST
        self.op_err = None
        self.shim = M.Decoder()          # the stand-in mantissa stage's state (the LFSR)
        self.blocks = []                 # per block: blksw, dynrng, exp, bap, coeff, lfe
        self.mant_cycles = 0

    def rd(self, a):
        return self.rec[a & 0x7FF]

    def exps(self, base):
        class View:
            def __getitem__(_, k):
                return self.rec[(base + k) & 0x7FF] & 31
        return View()

    def vop(self, op):
        a = [self.reg[i] for i in range(8, 16)]
        name = VOP_NAME[op]
        c0, b0 = self.cycles, self.bitpos
        op_t = 0
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
                        return
                    for _ in range(rep):
                        self.rec[(base + idx) & 0x7FF] = e
                        idx += 1
            op_t = CYC['expd'] + ngrps * (max(7, 3 * rep) + 1)
        elif name == 'bapsd':
            base, j, eb = a[0], a[1], a[2]
            self.rec[F_PSD] = M.ba_band_psd(self.exps(base), j, eb) & 0xFFFF
            op_t = CYC['bapsd'] + (eb - j)
        elif name == 'bapfill':
            base, j, eb, mask = a[0], a[1], a[2], a[3]
            for k, b in M.ba_bap_fill(mask, self.exps(base), j, eb).items():
                w = self.rec[(base + k) & 0x7FF]
                self.rec[(base + k) & 0x7FF] = ((b & 63) << 8) | (w & 31)
            op_t = CYC['bapfill'] + (eb - j)
        elif name == 'bapzero':
            base, j, eb = a[0], a[1], a[2]
            for k in range(j, eb):
                self.rec[(base + k) & 0x7FF] &= 31
            op_t = CYC['bapzero'] + (eb - j)
        elif name == 'mantmodel':
            op_t = self.mantmodel()
            if op_t is None:
                return
        elif name == 'cnt':
            op_t = D.CYC['cnt']
        self.cycles = c0 + D.CYC['vop'] - D.CYC['instr'] + op_t
        self.by_cat[name] = self.by_cat.get(name, 0) + self.cycles - c0
        if self.vop_hook is not None:
            self.vop_hook(self, op)

    def side(self):
        """The record's side information, as the model's Decoder holds it."""
        r = self.rec
        nf, acmod, lfeon = r[0x001], r[0x000], r[0x002]
        sh = self.shim
        sh.chincpl = r[0x007]
        for ch in range(nf):
            sh.endmant[ch] = r[0x058 + ch]
            sh.exp[ch] = [self.rec[BINBASE[ch] + k] & 31 for k in range(256)]
        sh.exp[M.CH_CPL] = [0] * 256
        if sh.chincpl:
            for k in range(r[0x00C], r[0x00D]):
                sh.exp[M.CH_CPL][k] = self.rec[BINBASE[5] + k] & 31
        sh.exp[M.CH_LFE] = [self.rec[BINBASE[6] + k] & 31 for k in range(7)] + [0] * 249
        nsub = r[0x009]
        sh.cpl.update(strtmant=r[0x00C], endmant=r[0x00D], ncplbnd=r[0x00A],
                      bndstrc=sum((r[0x070 + i] & 1) << i for i in range(max(nsub - 1, 0))))
        for ch in range(nf):
            for b in range(18):
                w = r[0x0A0 + ch * 18 + b]
                idx, f15, m = w & 31, (w >> 5) & 1, (w >> 6) & 15
                full = (m << 14) if f15 else ((m | 0x10) << 13)
                sh.cplco[ch][b] = ((full << 3) >> idx) & 0xFFFFFF
        sh.phsneg = [r[0x088 + b] & 1 for b in range(18)]
        sh.rematflg = r[0x00E]
        return nf, acmod, lfeon

    def bap_of(self, base, lo, hi):
        out = {}
        for k in range(lo, hi):
            b = (self.rec[(base + k) & 0x7FF] >> 8) & 63
            out[k] = b - 64 if b & 32 else b
        return out

    def mantmodel(self):
        nf, acmod, lfeon = self.side()
        sh, r = self.shim, self.rec
        bap = {ch: self.bap_of(BINBASE[ch], 0, sh.endmant[ch]) for ch in range(nf)}
        bap[M.CH_CPL] = self.bap_of(BINBASE[5], sh.cpl['strtmant'], sh.cpl['endmant']) \
            if sh.chincpl else {}
        bap[M.CH_LFE] = self.bap_of(BINBASE[6], 0, 7) if lfeon else {}
        dith = [r[0x048 + ch] for ch in range(nf)]
        b0 = self.bitpos
        try:
            coeff, lfe = sh.mantissas(_Bits(self), nf, lfeon, acmod, dith, bap)
        except M.Ac3Error:
            self.op_err = E_GROUP
            return None
        ncoef = sum(sh.endmant[ch] for ch in range(nf)) + (7 if lfeon else 0)
        if sh.chincpl:
            ncoef += (sh.cpl['endmant'] - sh.cpl['strtmant']) * bin(sh.chincpl).count('1')
        t = CYC['mant_bit'] * (self.bitpos - b0) + CYC['mant_coeff'] * ncoef
        self.mant_cycles += t
        exp = {ch: list(sh.exp[ch][:sh.endmant[ch]]) for ch in range(nf)}
        bp = {ch: [bap[ch][k] for k in range(sh.endmant[ch])] for ch in range(nf)}
        if sh.chincpl:
            rng = range(sh.cpl['strtmant'], sh.cpl['endmant'])
            exp[M.CH_CPL] = [sh.exp[M.CH_CPL][k] for k in rng]
            bp[M.CH_CPL] = [bap[M.CH_CPL][k] for k in rng]
        if lfeon:
            exp[M.CH_LFE] = list(sh.exp[M.CH_LFE][:7])
            bp[M.CH_LFE] = [bap[M.CH_LFE][k] for k in range(7)]
        self.blocks.append(dict(blksw=sum(r[0x040 + ch] << ch for ch in range(nf)),
                                dynrng=r[0x006], exp=exp, bap=bp, coeff=coeff, lfe=lfe))
        return t


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
    words, labels = load_program()
    dw, _ = D.load_program()
    files = {UMEM: [f'{w:010x}' for w in words], CMEM: [f'{w:04x}' for w in CONST]}
    bad = []
    for path, lines in files.items():
        text = '\n'.join(lines) + '\n'
        if check:
            if not os.path.exists(path) or open(path).read() != text:
                bad.append(os.path.relpath(path, REPO))
        else:
            open(path, 'w').write(text)
    print(f'ac3_isa: {len(words)} microcode words ({len(dw)} DTS + {len(words)} AC-3 = '
          f'{len(dw) + len(words)} in one ROM), {len(CONST)} constant words')
    if check:
        print('ac3_isa: ' + ('FAIL -- stale: ' + ', '.join(bad) if bad else 'PASS -- generated files match'))
        return 1 if bad else 0
    return 0


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
                  f'(of which the A1a mantissa stand-in {m.mant_cycles // max(len(fc), 1)} a frame); '
                  f'by op {dict(sorted(m.by_cat.items()))}')
        return 1 if bad else 0
    ap.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
