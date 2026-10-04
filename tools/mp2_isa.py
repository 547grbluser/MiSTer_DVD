#!/usr/bin/env python3
"""mp2_isa.py -- MPEG-1 Layer II as a program for the shared audio engine (M1).

docs/mp2_engine.md. The machine is tools/dts_isa.py's (the same sequencer, ISA and
assembler). This file adds MP2's program (dvd/dts/mp2.uasm), its constant ROM and
record map, and its vector ops. Each op is computed TWICE: the way the RTL will
compute it (the decomposition below, on the engine's 27 x 27 multiplier) and through
tools/mp2_ref.py's own functions. An op whose two answers differ raises, so the
fixed-point decomposition is proved here, on every op of every stream, before the
RTL exists. The model is bit-exact with mp2_decode (bench/dvd/run_mp2_model.sh, all
89 gate streams), so the engine inherits that contract.

The ops (numbered after AC-3's: one engine, one op space; XCLR is DTS's):
  XCLR (0)  zero X: the frame's sample region (a frame's allocation holds for all its
            granules, so an unallocated subband reads 0 in every slot).
  MDQ  (26) a0 X address {k, ch, sb}, a1 x16 = (q << (16 - nb)) ^ 0x8000, a2 d16 =
            2^(15 - dsh), a3 the class, a4 the scalefactor index:
              S = floor(floor((x16 + d16) C / 2^7) SCF / 2^20)
            = mp2_ref's sat27(requantize(q) x SCF >> 20). (x16 + d16) 2^9 is the
            model's s = (x << (25 - nb)) + 2^(24 - dsh) exactly, so (s C) >> 16 is
            floor((x16 + d16) C / 2^7). The engine issues x16 C and d16 C into one
            accumulator (no 17-bit adder), floors by 7, then multiplies by SCF.
            C (17 bits) and SCF (22 bits) are ROM words in the vector engine.
            The model's sat27 can never act: |S| <= 33,553,920 < 2^25 (asserted).
  MSYN (27) a0 k, a1 mono: per channel (channel 0 only if mono, its PCM duplicated, as
            mp2_decode), voff -= 64, the matrix V[i] = floor(sum_k N[i][k] S[k] / 2^14)
            into the ring (the model's sat32 can never act: |V| <= 2^30, asserted),
            then the window in two passes, because V is 31 bits and the multiplier
            takes 27: the low halves first, L = sum D x (V & 0xFFFF), carried as
            floor(L / 2^16) (DTS's carried-sum buffer); then the high halves,
            H = sum D x (V >> 16) on top of it, floored by 1, clipped to 24 bits, and
            the PCM stage DTS already has, sat16((p + 128) >> 8). That equals the
            model's sat16((T + 2^24) >> 25), T = sum D x V: floor((H 2^16 + L) /
            2^17) = floor((H + floor(L / 2^16)) / 2), and floor((floor(T / 2^17) +
            2^7) / 2^8) = floor((T + 2^24) / 2^25). Then the 32 pairs out.
  RCLR (28) zero the V ring (2 x 1,024) and the carried sums: the program's RESET,
            as mp2_decode clears V after every reset.
  MFS  (29) a0 the sampling-frequency index: no vector work; dts_top latches it for the
            output NCO. Issued once the header is accepted, before the frame's PCM.
  CNT  (8)  a0 0: a frame longer than its header (decoded, the rest drained).

Cycles: the sequencer's are the RTL's (dts_isa's calibrated model); the three new ops
are MODELLED until M2 builds them (CYC below), and the budget applies CYC_HEADROOM to
those alone.

Usage:
    tools/mp2_isa.py --run STREAM.mp2 [--frames N]     # vs mp2_ref.py, bit-exact
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dts_isa as D     # noqa: E402
import ac3_isa as A     # noqa: E402
import mp2_ref as R     # noqa: E402

REPO = os.path.dirname(HERE)
UASM = os.path.join(REPO, 'dvd', 'dts', 'mp2.uasm')

VOPS = {'xclr': 0, 'cnt': 8, 'mdq': 26, 'msyn': 27, 'rclr': 28, 'mfs': 29}
VOP_NAME = {v: k for k, v in VOPS.items()}
CYC_HEADROOM = 1.25
# MODELLED (M2 calibrates): MDQ = C read, two issues, the floor's landing, the SCF
# issue, its write; MSYN per channel = the matrix (2,048 MACs + its pipeline), the two
# window passes (2 x 512 + pipeline), then 32 pairs, 3 cycles each; RCLR one word a cycle
CYC = {'mdq': 8, 'msyn': 8, 'msyn_ch': 2048 + 4 + 1024 + 8, 'msyn_emit': 3 * 32,
       'rclr': 2048 + 64 + 4}

# the 17 quantisation classes, id 1..17 (0 = no samples): the ROM index of C
LEVELS = [3, 5, 7, 9, 15, 31, 63, 127, 255, 511, 1023, 2047, 4095, 8191, 16383, 32767, 65535]
CLS = {n: i + 1 for i, n in enumerate(LEVELS)}
NB = {n: ({3: 2, 5: 3, 9: 4}[n] if n in (3, 5, 9) else n.bit_length()) for n in LEVELS}
LISTS = [R._T2ab_0_2, R._T2ab_3_10, R._T2ab_11_22, R._T2ab_23_end, R._T2cd_0_1, R._T2cd_2_end]
S_MAX, V_MAX = 1 << 25, 1 << 30            # the proved bounds (module doc)


def build_const():
    """MP2's constants, placed after DTS's and AC-3's in the shared constant ROM."""
    words, base = [], {}

    def put(name, vals):
        base[name] = D.MEM_CONST + len(D.CONST) + len(A.CONST) + len(words)
        words.extend(v & 0xFFFF for v in vals)
    for i, lst in enumerate(LISTS):            # allocation code -> class
        put(f'L{i}', [0] + [CLS[n] for n in lst[1:]])
    # by class id: CB {16 - nb [11:8], the code's bits [4:0]}, CA {n^2 [15:8], n [7:0]}
    # for a grouped class (else 0), D16 = 2^(15 - dsh) (packed: the ROM is 1,024 words)
    put('MP2CB', [0] + [((16 - NB[n]) << 8) | R.sample_bits(n)[1] for n in LEVELS])
    put('MP2CA', [0] + [((n * n) << 8) | n if n in (3, 5, 9) else 0 for n in LEVELS])
    put('MP2D16', [0] + [1 << (15 - R.D_SHIFT[n]) for n in LEVELS])
    lens = []
    for sr in (44100, 48000, 32000):           # {fs index, bitrate index}
        lens += [0] + [144 * br * 1000 // sr for br in R.BITRATES_L2[1:]] + [0]
    put('MP2LEN', lens)
    return words, base


CONST, CBASE = build_const()


def vec_tables():
    """The vector engine's MP2 ROM words: C (Q1.16, 17 bits) by class, SCF (Q1.20, 22
    bits) by index; N (Q1.14) 64 x 32, D (Q2.16) 512."""
    c = [0] + [R.C_Q16[n] for n in LEVELS]
    return c, list(R.SCF_Q20), [v for row in R.N_Q14 for v in row], list(R.D_Q16)


def load_program(mutate=None):
    """-> (the whole ROM image: DTS's, AC-3's then MP2's program, MP2's labels)."""
    words, _ = A.load_program()
    mw, labels, _ = D.assemble(open(UASM).read(), mutate, vops=VOPS, cbase=CBASE,
                               org=len(words))
    return words + mw, labels


def s16(v):
    return D.s16(v)


class Machine(D.Machine):
    """The sequencer, MP2's ops and the engine state they keep:
      S[256]       X's sample region {k, ch, sb} (27 bits)
      V[2][1024]   the ring, voff[ch] (the engine's ring, 2,048 x 32)
      ref          an mp2_ref.MP2Decoder whose synth runs beside every MSYN"""

    def __init__(self, words, labels):
        super().__init__(words, labels)
        self.const = D.CONST + A.CONST + CONST
        self.op_err = None
        self.S = [0] * 256
        self.V = [[0] * 1024 for _ in range(2)]
        self.voff = [0, 0]
        self.b2 = [0] * 64                  # the window's carried sums {ch, j} (DTS's buffer)
        self.ref = R.MP2Decoder()
        self.fs = None
        self.vec_model = 0
        self.ndq = 0
        self.dq_class = set()
        self.N, self.Dw = vec_tables()[2], R.D_Q16
        self.C, self.SCF = vec_tables()[:2]

    def vop(self, op):
        a = [self.reg[i] for i in range(8, 16)]
        name = VOP_NAME.get(op)
        c0 = self.cycles
        if name == 'xclr':
            self.S = [0] * 256
            t = D.CYC['xclr']
        elif name == 'cnt':               # counter 0: a frame longer than its header
            self.counters[a[0]] = self.counters.get(a[0], 0) + 1
            t = D.CYC['cnt']
        elif name == 'rclr':              # the ring and the carried sums; the offsets stay
            self.V = [[0] * 1024 for _ in range(2)]
            self.b2 = [0] * 64
            self.ref = R.MP2Decoder()
            self.ref.voff = list(self.voff)
            t = CYC['rclr']
            self.vec_model += t
        elif name == 'mdq':
            self.S[a[0] & 0xFF] = self.mdq(a)
            t = CYC['mdq']
            self.vec_model += t
        elif name == 'msyn':
            t = self.msyn(a[0], a[1] != 0)
            self.vec_model += t
        elif name == 'mfs':
            self.fs = a[0] & 3
            t = D.CYC['cnt']
        else:
            raise D.EngineError(f'unknown op {op} at pc {self.pc}')
        self.cycles = c0 + D.CYC['vop'] - D.CYC['instr'] + t
        self.by_cat[name] = self.by_cat.get(name, 0) + self.cycles - c0
        if self.vop_hook is not None:
            self.vop_hook(self, op)

    def mdq(self, a):
        """MDQ, the engine's way, checked against the model's requantize and scale."""
        dest, x16, d16, cls, sidx = a[0], s16(a[1]), a[2] & 0xFFFF, a[3], a[4] & 63
        if not 1 <= cls <= 17 or sidx > 62:
            raise D.EngineError(f'MDQ class {cls} / scalefactor {sidx} at pc {self.pc}')
        c, scf = self.C[cls], self.SCF[sidx]
        q = (x16 * c + d16 * c) >> 7                  # one accumulator, floored by 7
        s = (q * scf) >> 20
        # the model: the code back from x16, then requantize and scale
        n = LEVELS[cls - 1]
        nb = NB[n]
        code = ((a[1] & 0xFFFF) ^ 0x8000) >> (16 - nb)
        want = R.sat((R.MP2Decoder.requantize(code, n) * scf) >> 20, 27)
        if s != want:
            raise D.EngineError(f'MDQ {a[:5]}: engine {s}, model {want}')
        if abs(s) > S_MAX:
            raise D.EngineError(f'MDQ |S| = {abs(s)} past the proved bound')
        self.ndq += 1
        self.dq_class.add(n)
        return s

    def msyn(self, k, mono):
        """MSYN, the engine's way, checked slot by slot against the model's synth."""
        nch = 1 if mono else 2
        out = []
        for ch in range(nch):
            S = [self.S[(k << 6) | (ch << 5) | sb] for sb in range(32)]
            V = self.V[ch]
            self.voff[ch] = off = (self.voff[ch] - 64) % 1024
            for i in range(64):                       # the matrix: floor by 14
                acc = sum(self.N[i * 32 + kk] * S[kk] for kk in range(32))
                v = acc >> 14
                if abs(v) > V_MAX:
                    raise D.EngineError(f'MSYN |V| = {abs(v)} past the proved bound')
                V[(off + i) & 1023] = v
            pcm = []
            for j in range(32):                       # the window: low halves, then high
                taps = []
                for t in range(16):
                    di = j + 64 * (t >> 1) + 32 * (t & 1)
                    vi = (off + j + 128 * (t >> 1) + 96 * (t & 1)) & 1023
                    taps.append((self.Dw[di], V[vi]))
                lo = sum(d * (v & 0xFFFF) for d, v in taps)
                b2 = lo >> 16                         # the carried sum
                self.b2[(ch << 5) | j] = b2
                hi = b2 + sum(d * (v >> 16) for d, v in taps)
                p = R.sat(hi >> 1, 24)
                pcm.append(R.sat((p + 128) >> 8, 16))
            want = self.ref.synth(ch, S)
            if pcm != want or V != self.ref.V[ch] or off != self.ref.voff[ch]:
                raise D.EngineError(f'MSYN slot {k} ch {ch}: the decomposition is not the model')
            out.append(pcm)
        if mono:
            out.append(out[0])
        self.pcm[0].extend(out[0])
        self.pcm[1].extend(out[1])
        return CYC['msyn'] + nch * CYC['msyn_ch'] + CYC['msyn_emit']


    def checksums(self):
        """The engine buffers as tools/dts_isa.py Machine.checksums, in the RTL's address
        order, at MP2's widths: X (1,280 words, 27 bits: the samples at {k, ch, sb}),
        the ADPCM history (untouched: 0), the ring {ch, i} (2,048 x 32), b2 {ch, j}
        (64 x 29). -> (x, hist, ring, buf2)"""
        def ck(vals, w):
            m = (1 << w) - 1
            return sum((i + 1) * (v & m) for i, v in enumerate(vals)) & 0xFFFFFFFF
        return (ck(self.S + [0] * (1280 - 256), 27), 0, ck(self.V[0] + self.V[1], 32),
                ck(self.b2, 29))


def emulate(path, nframes=0, mutate=None):
    """-> (machine, the model's PCM pairs, frames fed)."""
    words, labels = load_program(mutate)
    m = Machine(words, labels)
    data = open(path, 'rb').read()
    ref = R.MP2Decoder()
    want = []
    n = 0
    for fr, hdr in R.iter_frames(data, nframes or None):
        m.feed(fr)
        m.run()
        want.extend(ref.decode_frame(fr, hdr))
        n += 1
    return m, want, n


def compare(m, want):
    """-> (mismatched pairs, the first one's index or None)."""
    got = list(zip(m.pcm[0], m.pcm[1]))
    bad = sum(1 for x, y in zip(got, want) if x != y) + abs(len(got) - len(want))
    first = next((i for i, (x, y) in enumerate(zip(got, want)) if x != y), None)
    if first is None and len(got) != len(want):
        first = min(len(got), len(want))
    return bad, first


IC_C, IC_SCF = 128, 160            # MP2's words in dts_vec's icoef ROM (256 deep)


def icoef_words(icoef):
    """dts_vec's icoef ROM: the IMDCT's coefficients, then MP2's C by class at IC_C and
    SCF by index at IC_SCF (one M10K holds 256 x 27 as it held 113)."""
    c, scf, _, _ = vec_tables()
    assert len(icoef) <= IC_C and IC_C + len(c) <= IC_SCF and IC_SCF + len(scf) <= 256
    w = list(icoef) + [0] * (IC_C - len(icoef)) + c
    w += [0] * (IC_SCF - len(w)) + scf
    return w + [0] * (256 - len(w))


def vec_files():
    """MP2's vector-engine ROM images: N (2,048 x 16, {i, k}) and D (512 x 18)."""
    _, _, n, d = vec_tables()
    return {'dts_mp2n.mem': [f'{v & 0xFFFF:04x}' for v in n],
            'dts_mp2d.mem': [f'{v & 0x3FFFF:05x}' for v in d]}


def svh_lines(labels):
    """MP2's part of dvd/dts/dts_ucode.svh: its entry points, its icoef words and op numbers."""
    return [
        '// MP2 (docs/mp2_engine.md): the entry points, its words in icoef, the op numbers',
        f"localparam [10:0] UC_MP2_RESET = 11'd{labels['RESET']};",
        f"localparam [10:0] UC_MP2_FRAME = 11'd{labels['FRAME']};",
        f"localparam [7:0]  MP2_IC_C     = 8'd{IC_C};",
        f"localparam [7:0]  MP2_IC_SCF   = 8'd{IC_SCF};",
    ] + [f"localparam [5:0]  V_{k.upper():8s}= 6'd{v};" for k, v in sorted(VOPS.items(), key=lambda kv: kv[1])
         if k not in ('xclr', 'cnt')]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--run')
    ap.add_argument('--frames', type=int, default=0)
    a = ap.parse_args()
    if a.run:
        m, want, n = emulate(a.run, a.frames)
        bad, first = compare(m, want)
        fc = m.frame_cycles
        print(f'mp2_isa: {n} frames, {len(m.pcm[0])} pairs, {bad} mismatches vs mp2_ref'
              + (f' (first at pair {first})' if first is not None else '')
              + f'; errors {m.errors or "none"}; {m.ndq} MDQ')
        if fc:
            print(f'  cycles a frame: max {max(fc)}, mean {sum(fc) // len(fc)}; '
                  f'by op {dict(sorted(m.by_cat.items()))}')
        return 1 if bad or m.errors else 0
    ap.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
