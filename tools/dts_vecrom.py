#!/usr/bin/env python3
"""dts_vecrom.py -- the DTS vector engine's ROM images (docs/dts_decoder.md sec 10).

Written by tools/dts_isa.py --asm (and checked by --check) beside the microcode:

  dts_vconst.mem  512 x 24  the engine's constants, at the VK_* bases below
  dts_win.mem    1024 x 24  the two 512-tap window prototypes: {perfect, tap}
  dts_iprog.mem   N x 20    the half IMDCT as a program of multiply-accumulate terms
  dts_icoef.mem   M x 27    the program's coefficients
  dts_vec.svh               the bases and sizes, for dvd/dts/dts_vec.sv

THE IMDCT PROGRAM. FFmpeg's factorised half IMDCT (tools/dts_ref.py imdct_half_32)
is seven stages of 32 outputs: the pre-shift, two sum stages, the DCT stage, two
modulation stages and the final butterfly. Each output is a short sum of
coefficient x scratch terms, rounded once. Hardwiring the addressing of seven
stages costs ALMs; this program costs ROM words instead (ALMs are the scarcer
resource, docs/hw_budget_and_lessons.md). The RTL executes it term by term on
the shared multiplier; `run_prog` below is that executor, in Python, and `check`
proves it equal to imdct_half_32 on random, extreme and real inputs.

  scratch  64 words: two regions of 32. Stage s (0..6) reads region s & 1 and
           writes region !(s & 1) (stage 6 writes the IMDCT ring instead), so the
           destination is implicit: output n goes to stage n >> 5, index n & 31.
  term     {shl, neg, rsh[1:0], add, last, coef[7:0], src[5:0]}, 20 bits:
             add = 0  acc (+)= ICOEF[coef] * scratch[src]   (an output's first MAC
                      term starts the sum)
             add = 1  addend = scratch[src]                 (no product)
           on the last term of an output:
             r = round_half_up(acc >> RSH), RSH = (0, the pre-shift, 22, 23)[rsh]
             v = (neg ? -r : r) + addend;  if shl: v <<= the pre-shift;  clip23(v)
  Exactness: mulc(c, a + b, 23) = norm(c a + c b, 23) (one rounding of an exact
  integer sum), and x + norm(c y, 23) = norm(2^23 x + c y, 23) (adding a multiple of
  2^23 before the shift). But x - norm(c y, 23) is NOT norm(2^23 x - c y, 23) (half-
  up rounding is not odd-symmetric), hence the addend and neg fields. mod_a's
  -85,479,984 needs 28 bits; it is even, so it is stored halved with RSH 22.
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dts_fixed as F   # noqa: E402
import dts_ref as R     # noqa: E402
import dts_tables as T  # noqa: E402

# ---------------------------------------------------------------- the constant ROM
VK_WORDS = 512
VK = {'SCALE6': 0, 'SCALE7': 64, 'STEP': 192, 'JOINT': 256, 'ADJ': 385, 'GAIN': 389}
NCH_MAX = 5


def gain(amode, ch, side):
    """The Q15 downmix gain of primary channel ch on output side (0 L, 1 R)."""
    spk = R.PRM_CH_TO_SPKR[amode]
    if ch >= len(spk):
        return 0
    return F.default_gains(amode).get(spk[ch], (0, 0))[side]


def vconst_words():
    w = [0] * VK_WORDS

    def put(base, vals):
        for i, v in enumerate(vals):
            assert 0 <= v < (1 << 23), v
            w[base + i] = v
    put(VK['SCALE6'], T.SCALE_FACTOR_QUANT6)
    put(VK['SCALE7'], T.SCALE_FACTOR_QUANT7)
    put(VK['STEP'], T.LOSSY_QUANT)
    put(VK['STEP'] + 32, T.LOSSLESS_QUANT)
    put(VK['JOINT'], T.JOINT_SCALE_FACTORS)
    put(VK['ADJ'], T.SCALE_FACTOR_ADJ)
    put(VK['GAIN'], [gain(a, ch, s) for a in range(10) for ch in range(NCH_MAX) for s in (0, 1)])
    assert VK['GAIN'] + 100 <= VK_WORDS
    return w


def window_words():
    return [v & 0xFFFFFF for v in T.FIR_32BANDS_NONPERFECT_FIXED + T.FIR_32BANDS_PERFECT_FIXED]


# ---------------------------------------------------------------- the IMDCT program
RSH_ZERO, RSH_DYN, RSH_22, RSH_23 = range(4)


def build_prog():
    """-> (terms, coefs). A term is a dict: add, src, coef (an index), last, rsh,
    neg, shl."""
    coefs, terms = [], []

    def ci(v):
        assert -(1 << 26) <= v < (1 << 26), v
        if v not in coefs:
            coefs.append(v)
        return coefs.index(v)

    def out(parts, rsh=RSH_ZERO, neg=0, shl=0):
        """parts: [('mac', src, coefficient value) | ('add', src)]"""
        parts = [p for p in parts if p[0] == 'add' or p[2] != 0]   # a zero term adds 0
        assert parts and parts[-1][0] == 'mac'
        for k, p in enumerate(parts):
            last = k == len(parts) - 1
            t = {'add': int(p[0] == 'add'), 'src': p[1], 'coef': 0 if p[0] == 'add' else ci(p[2]),
                 'last': int(last), 'rsh': rsh if last else 0, 'neg': neg if last else 0,
                 'shl': shl if last else 0}
            terms.append(t)

    def rd(stage):
        base = 32 * (stage & 1)
        return lambda i: base + i
    one, big = 1, 1 << 23
    # stage 0: the pre-shift, (v + rnd) >> shift
    x = rd(0)
    for k in range(32):
        out([('mac', x(k), one)], RSH_DYN)
    # stage 1: sum_a(a, 16) + sum_b(a, 16)
    x = rd(1)
    for i in range(16):
        out([('mac', x(2 * i), one), ('mac', x(2 * i + 1), one)])
    out([('mac', x(0), one)])
    for i in range(1, 16):
        out([('mac', x(2 * i), one), ('mac', x(2 * i - 1), one)])
    # stage 2: sum_a(b[0:16], 8) + sum_b(b[0:16], 8) + sum_c(b[16:], 8) + sum_d(b[16:], 8)
    x = rd(2)
    for i in range(8):
        out([('mac', x(2 * i), one), ('mac', x(2 * i + 1), one)])
    out([('mac', x(0), one)])
    for i in range(1, 8):
        out([('mac', x(2 * i), one), ('mac', x(2 * i - 1), one)])
    for i in range(8):
        out([('mac', x(16 + 2 * i), one)])
    out([('mac', x(17), one)])
    for i in range(1, 8):
        out([('mac', x(16 + 2 * i - 1), one), ('mac', x(16 + 2 * i + 1), one)])
    # stage 3: dct_a(a[0:8]) + dct_b(a[8:16]) + dct_b(a[16:24]) + dct_b(a[24:32])
    x = rd(3)
    for i in range(8):
        out([('mac', x(j), T.DCT_A_COS[i][j]) for j in range(8)], RSH_23)
    for base in (8, 16, 24):
        for i in range(8):
            out([('mac', x(base), big)] +
                [('mac', x(base + 1 + j), T.DCT_B_COS[i][j]) for j in range(7)], RSH_23)
    # stage 4: mod_a(b[0:16]) + mod_b(b[16:32])
    x = rd(4)
    c = T.MOD_A_COS
    for i in range(8):
        out([('mac', x(i), c[i]), ('mac', x(8 + i), c[i])], RSH_23)
    for n in range(8):
        cv, rs = c[8 + n], RSH_23
        if not -(1 << 26) <= cv < (1 << 26):           # -85,479,984: halved, shift 22
            assert cv % 2 == 0
            cv, rs = cv // 2, RSH_22
        out([('mac', x(7 - n), cv), ('mac', x(15 - n), -cv)], rs)
    c = T.MOD_B_COS
    for i in range(8):
        out([('mac', x(16 + i), big), ('mac', x(16 + 8 + i), c[i])], RSH_23)
    for n in range(8):
        out([('add', x(16 + 7 - n)), ('mac', x(16 + 15 - n), c[7 - n])], RSH_23, neg=1)
    # stage 5: mod_c(a), then clip23(v << shift)
    x = rd(5)
    c = T.MOD_C_COS
    for i in range(16):
        out([('mac', x(i), c[i]), ('mac', x(16 + i), c[i])], RSH_23, shl=1)
    for n in range(16):
        out([('mac', x(15 - n), c[16 + n]), ('mac', x(31 - n), -c[16 + n])], RSH_23, shl=1)
    # stage 6: the butterfly into the ring
    x = rd(6)
    for i in range(16):
        out([('mac', x(i), one), ('mac', x(31 - i), -1)])
    for i in range(16):
        out([('mac', x(i), one), ('mac', x(31 - i), one)])
    assert sum(t['last'] for t in terms) == 7 * 32
    assert len(coefs) <= 256
    return terms, coefs


PROG, ICOEF = build_prog()


def prog_words():
    out = []
    for t in PROG:
        out.append(t['src'] | (t['coef'] << 6) | (t['last'] << 14) | (t['add'] << 15) |
                   (t['rsh'] << 16) | (t['neg'] << 18) | (t['shl'] << 19))
    return out


def run_prog(inp, terms=None, coefs=None):
    """The RTL's IMDCT executor: 32 mixed subband samples -> the 32 ring words."""
    terms = PROG if terms is None else terms
    coefs = ICOEF if coefs is None else coefs
    mag = sum(abs(v) for v in inp)
    shift = 2 if mag > 0x400000 else 0
    sc = list(inp) + [0] * 32
    ring = [0] * 32
    outn, acc, addend, fresh = 0, 0, 0, True
    for t in terms:
        v = sc[t['src']]
        if t['add']:
            addend = v
        else:
            p = coefs[t['coef']] * v
            acc = p if fresh else acc + p
            fresh = False
        if t['last']:
            r = R.norm(acc, (0, shift, 22, 23)[t['rsh']])
            r = (-r if t['neg'] else r) + addend
            if t['shl']:
                r <<= shift
            r = R.clip23(r)
            st, idx = outn >> 5, outn & 31
            if st == 6:
                ring[idx] = r
            else:
                sc[32 * (0 if st & 1 else 1) + idx] = r
            outn += 1
            acc, addend, fresh = 0, 0, True
    return ring


def check(real_streams=(), ncol=4000, seed=1):
    """run_prog == imdct_half_32 on random vectors (both pre-shift cases, the
    extremes) and on real synthesis inputs from streams. -> (columns, mismatches)"""
    rnd = random.Random(seed)
    cols = []
    lim = (1 << 23) - 1
    for _ in range(ncol):
        k = rnd.choice((8, 12, 16, 20, 23))
        cols.append([rnd.randint(-(1 << k), (1 << k) - 1) for _ in range(32)])
    cols.append([lim] * 32)
    cols.append([-lim - 1] * 32)
    cols.append([lim if i & 1 else -lim - 1 for i in range(32)])
    cols.append([0] * 32)
    # real columns: every synthesis input of a few frames of each stream
    orig = R.SynthFixed.run

    def run(self, inp, win):
        cols.append(list(inp))
        return orig(self, inp, win)
    R.SynthFixed.run = run
    try:
        R.OPT.add('lenient_block')
        for p in real_streams:
            hw = F.StreamDecoder()
            for n, (_, fr) in enumerate(R.frames(open(p, 'rb').read())):
                if n >= 2:
                    break
                hw.decode(fr)
    finally:
        R.SynthFixed.run = orig
    bad = sum(1 for c in cols if run_prog(c) != R.imdct_half_32(c))
    shifted = sum(1 for c in cols if sum(abs(v) for v in c) > 0x400000)
    return len(cols), bad, shifted


def vec_svh():
    return [
        '// dvd/dts/dts_vec.svh -- GENERATED by tools/dts_isa.py --asm; never edit.',
        '// The vector engine\'s ROM sizes and the constant ROM\'s bases (tools/dts_vecrom.py).',
        f'localparam int VK_WORDS    = {VK_WORDS};',
    ] + [f"localparam [8:0] VK_{k:<7}= 9'd{v};" for k, v in VK.items()] + [
        f'localparam int IPROG_WORDS = {len(PROG)};',
        f'localparam int ICOEF_WORDS = {len(ICOEF)};',
        '// primary channels by AMODE (packed: element i at [3*i +: 3])',
        f"localparam [29:0] VK_NCH = 30'h{sum(T.CHANNELS[a] << (3 * a) for a in range(10)):08x};",
    ]


if __name__ == '__main__':
    import glob
    gate = os.environ.get('DTS_TEST_DIR', os.path.expanduser('~/dts-streams/gate'))
    n, bad, sh = check(sorted(glob.glob(os.path.join(gate, '*.dts'))))
    print(f'dts_vecrom: {len(PROG)} IMDCT terms, {len(ICOEF)} coefficients; '
          f'{n} columns ({sh} pre-shifted): {bad} differ from imdct_half_32')
    sys.exit(1 if bad else 0)
