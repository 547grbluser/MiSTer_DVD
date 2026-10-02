#!/usr/bin/env python3
"""dts_isa.py -- the DTS engine's sequencer: its instruction set, the assembler,
the ROM images, and the instruction-level emulator (docs/dts_decoder.md sec 10).

The fabric DTS decoder is a microcoded sequencer that runs the bitstream parse
and drives hardwired vector loops on one multiplier. This file is the executable
definition of that machine. The RTL must match the emulator instruction by
instruction (register writes, stores, PC) and word by word (PCM). The emulator's
vector ops call tools/dts_fixed.py's own functions, so the emulator is checked
against an independent model rather than against itself.

Instruction word, 40 bits:
    op[39:34] rd[33:30] rs[29:26] rt[25:22] imm[21:6] aux[5:0]
Registers r0..r15 are 16 bits, two's complement; r0 reads 0. r8..r15 carry the
vector ops' arguments.

    alu   rd, rs, rt        rd = rs FN rt                (FN in aux)
    alui  rd, rs, imm       rd = rs FN sext16(imm)
    ld    rd, imm[rs+rt]    rd = MEM[(rs + rt + imm) & 0xFFFF]
    st    rd, imm[rs+rt]    MEM[...] = rd
    get   rd, n             the next n bits (1..16), MSB first
    getr  rd, rt            the next rt bits (0..16)
    vlc   rd, imm[rs]       the Huffman symbol of book (rs + imm), signed
    b<c>  rs, rt, label     branch if rs <c> rt, c in eq ne lt ge (signed)
    b<c>i rs, k, label      ... against a signed 10-bit constant k
    jmp / call label, ret   (call stack depth 8)
    err   code              count the error code, drop the rest of the frame,
                            restart at the label FRAME
    vop   OP                run a vector op (args in r8..r15); the sequencer waits
    frame rd                wait for the next frame; rd = its length in bytes
    fend                    discard the rest of the frame
    bpos  rd                rd = bits consumed in this frame (low 16 bits)

Memory, 16-bit words: 0x0000-0x07FF the record RAM (2K x 16; the map ends at
REC_END); 0x1000-0x1FFF the constant ROM (read-only). Everything else belongs to
the vector ops.

The trace the RTL is scored against (Machine.trace, a list): one tuple
(kind, pc, addr, value) per event, in program order --
    kind 0  a register write      addr = the register, value 16 bits
    kind 1  a store               addr = the record address, value 16 bits
    kind 2  an XQ code extracted  addr = its index 0..7, value 24 bits (two's
                                  complement): the sequencer's code reader is
                                  scored here, before any dequantisation

Usage:
    tools/dts_isa.py --asm [--check]           # assemble dvd/dts/dts.uasm -> .mem files
    tools/dts_isa.py --run <file.dts> [--frames N]   # emulate vs tools/dts_fixed.py
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dts_ref as R      # noqa: E402
import dts_fixed as F    # noqa: E402
import dts_tables as T   # noqa: E402

REPO = os.path.dirname(HERE)
UASM = os.path.join(REPO, 'dvd', 'dts', 'dts.uasm')
UMEM = os.path.join(REPO, 'dvd', 'dts', 'dts_ucode.mem')
CMEM = os.path.join(REPO, 'dvd', 'dts', 'dts_const.mem')
HMEM = os.path.join(REPO, 'dvd', 'dts', 'dts_huff.mem')
RMEM = os.path.join(REPO, 'dvd', 'dts', 'dts_hroot.mem')
USVH = os.path.join(REPO, 'dvd', 'dts', 'dts_ucode.svh')

OPS = ['nop', 'alu', 'alui', 'ld', 'st', 'get', 'getr', 'vlc', 'br', 'bri', 'jmp', 'call',
       'ret', 'err', 'vop', 'frame', 'fend', 'bpos']
OP = {n: i for i, n in enumerate(OPS)}
FNS = ['add', 'sub', 'and', 'or', 'xor', 'shl', 'sar', 'shr', 'slt', 'sltu', 'seq', 'sne',
       'min', 'max']
FN = {n: i for i, n in enumerate(FNS)}
CONDS = ['eq', 'ne', 'lt', 'ge']
VOPS = ['xclr', 'xq', 'xvq', 'adpcm', 'joint', 'bfly', 'mixsyn', 'hclr', 'cnt']
VOP = {n: i for i, n in enumerate(VOPS)}
MEM_REC, MEM_CONST = 0x0000, 0x1000
REC_WORDS = 0x0800
STACK_DEPTH = 8
NCH_MAX = 5                       # primary channels (AMODE < 10)

# Counters the CNT op and the engine keep (telemetry ids)
CNT_DMIX_IGNORED = 0


def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def encode(op, rd=0, rs=0, rt=0, imm=0, aux=0):
    return (OP[op] << 34) | ((rd & 15) << 30) | ((rs & 15) << 26) | ((rt & 15) << 22) | \
        ((imm & 0xFFFF) << 6) | (aux & 63)


def decode(w):
    return (OPS[(w >> 34) & 63], (w >> 30) & 15, (w >> 26) & 15, (w >> 22) & 15,
            (w >> 6) & 0xFFFF, w & 63)


# ------------------------------------------------------------------ Huffman ROM
# Book ids: every core codebook in tools/dts_tables.py order. The bit-allocation,
# transient and scale books are the sequencer's (`vlc`); the quantiser-index books
# are XQ's, found through QBOOK[abits-1] + selector.
BOOKS = list(T.VLC)
BOOK_ID = {n: i for i, n in enumerate(BOOKS)}
QBOOK = [BOOK_ID[f'quant_index_{a}_0'] for a in range(R.CODE_BOOKS)]


def build_huff_tree():
    """-> (nodes, roots). nodes[i] = (left, right); an entry is (leaf, value):
    leaf=1 -> value is the symbol; leaf=0 -> value is the child node index. The
    RTL walks it one code bit per step (53 of the 62 books are not canonical)."""
    nodes, roots = [], []
    for name in BOOKS:
        root = len(nodes)
        nodes.append([None, None])
        for code, ln, sym in T.VLC[name]['codes']:
            n = root
            for k in range(ln - 1, -1, -1):
                bit = (code >> k) & 1
                if k == 0:
                    assert nodes[n][bit] is None
                    nodes[n][bit] = (1, sym)
                else:
                    if nodes[n][bit] is None:
                        nodes.append([None, None])
                        nodes[n][bit] = (0, len(nodes) - 1)
                    n = nodes[n][bit][1]
        roots.append(root)
    assert all(e is not None for nd in nodes for e in nd), 'a codebook is incomplete'
    return nodes, roots


HUFF_NODES, HUFF_ROOTS = build_huff_tree()


def huff_mem_words():
    """26-bit node words: {right(13), left(13)}, an entry = {leaf, value[11:0]}."""
    out = []
    for left, right in HUFF_NODES:
        def ent(e):
            return (e[0] << 12) | (e[1] & 0xFFF)
        out.append((ent(right) << 13) | ent(left))
    return out


# ------------------------------------------------------------------ constant ROM
def build_const():
    words, base = [], {}

    def put(name, vals):
        base[name] = MEM_CONST + len(words)
        words.extend(v & 0xFFFF for v in vals)
    put('SEL_NBITS', T.QUANT_INDEX_SEL_NBITS)
    put('GROUP_SIZE', T.QUANT_INDEX_GROUP_SIZE)
    put('CHANNELS', T.CHANNELS)
    put('BPS', T.BITS_PER_SAMPLE)
    put('DMIX_NCH', T.DMIX_PRIMARY_NCH)
    # the primary-channel index of L, R, Ls, Rs in each AMODE (-1 if absent)
    for name, spk in (('IDX_L', R.SPK_L), ('IDX_R', R.SPK_R),
                      ('IDX_LS', R.SPK_LS), ('IDX_RS', R.SPK_RS)):
        put(name, [R.PRM_CH_TO_SPKR[a].index(spk) if spk in R.PRM_CH_TO_SPKR[a] else -1
                   for a in range(10)])
    return words, base


CONST, CBASE = build_const()


# ------------------------------------------------------------------ assembler
class AsmError(Exception):
    pass


def _reg(tok, aliases):
    tok = aliases.get(tok.strip().lower(), tok.strip().lower())
    m = re.fullmatch(r'r(\d+)', tok)
    if not m or int(m.group(1)) > 15:
        raise AsmError(f'not a register: {tok!r}')
    return int(m.group(1))


def _is_reg(tok, aliases):
    tok = aliases.get(tok.strip().lower(), tok.strip().lower())
    return re.fullmatch(r'r\d+', tok) is not None


def assemble(text, mutate=None):
    """-> (words, labels, line numbers). `mutate`: a name; a line carrying
    `;MUT <name>: <instruction>` assembles that instruction instead (the RED arms
    of tools/test_dts_isa.py live beside the code they break)."""
    equ = {'C_' + k: v for k, v in CBASE.items()}
    equ.update({'B_' + n.upper(): i for n, i in BOOK_ID.items()})
    equ.update({'V_' + n.upper(): i for n, i in VOP.items()})
    equ['CNT_DMIX_IGNORED'] = CNT_DMIX_IGNORED
    aliases, labels, stmts = {}, {}, []
    mutated = False
    for ln, raw in enumerate(text.splitlines(), 1):
        mm = re.search(r';\s*MUT\s+(\w+)\s*:\s*(.*)$', raw)
        line = raw
        if mm and mutate == mm.group(1):
            line = re.sub(r'^(\s*(?:[A-Za-z_][\w.]*:\s*)*).*$', lambda m_: m_.group(1) + mm.group(2), raw)
            mutated = True
        line = re.sub(r';.*', '', line).strip()
        if not line:
            continue
        while True:
            m = re.match(r'([A-Za-z_][\w.]*):\s*(.*)$', line)
            if not m:
                break
            if m.group(1) in labels:
                raise AsmError(f'line {ln}: label {m.group(1)} twice')
            labels[m.group(1)] = len(stmts)
            line = m.group(2).strip()
        if not line:
            continue
        if line.startswith('.equ'):
            _, name, val = line.split(None, 2)
            equ[name] = int(eval(val, {'__builtins__': {}}, dict(equ)))
            continue
        if line.startswith('.reg'):
            _, name, reg = line.split(None, 2)
            aliases[name.lower()] = reg.strip().lower()
            continue
        stmts.append((ln, line, dict(aliases)))
    if mutate and not mutated:
        raise AsmError(f'no MUT {mutate} in the source')

    def ev(expr, ln):
        env = dict(equ)
        env.update(labels)
        try:
            return int(eval(expr, {'__builtins__': {}}, env))
        except Exception as e:
            raise AsmError(f'line {ln}: cannot evaluate {expr!r} ({e})')

    words = []
    for ln, line, al in stmts:
        m = re.match(r'(\w+)\s*(.*)$', line)
        mn, rest = m.group(1).lower(), m.group(2)
        args = [a.strip() for a in re.split(r',(?![^\[]*\])', rest)] if rest else []

        def reg(i):
            return _reg(args[i], al)

        def memref(s):
            m2 = re.fullmatch(r'(.*?)\s*\[([^\]]*)\]', s)
            if not m2:
                return ev(s, ln), 0, 0
            imm = ev(m2.group(1), ln) if m2.group(1).strip() else 0
            rr = [r.strip() for r in m2.group(2).split('+')]
            ra = _reg(rr[0], al) if rr and rr[0] else 0
            rb = _reg(rr[1], al) if len(rr) > 1 else 0
            return imm, ra, rb
        try:
            if mn in FN:
                if _is_reg(args[2], al):
                    words.append(encode('alu', reg(0), reg(1), reg(2), 0, FN[mn]))
                else:
                    words.append(encode('alui', reg(0), reg(1), 0, ev(args[2], ln), FN[mn]))
            elif mn.endswith('i') and mn[:-1] in FN:
                words.append(encode('alui', reg(0), reg(1), 0, ev(args[2], ln), FN[mn[:-1]]))
            elif mn == 'li':
                words.append(encode('alui', reg(0), 0, 0, ev(args[1], ln), FN['add']))
            elif mn == 'mov':
                words.append(encode('alu', reg(0), reg(1), 0, 0, FN['add']))
            elif mn in ('ld', 'st'):
                imm, ra, rb = memref(args[1])
                words.append(encode(mn, reg(0), ra, rb, imm))
            elif mn == 'get':
                n = ev(args[1], ln)
                if not 1 <= n <= 16:
                    raise AsmError(f'line {ln}: get {n} bits')
                words.append(encode('get', reg(0), 0, 0, n))
            elif mn == 'getr':
                words.append(encode('getr', reg(0), 0, reg(1)))
            elif mn == 'vlc':
                imm, ra, _ = memref(args[1])
                words.append(encode('vlc', reg(0), ra, 0, imm))
            elif mn[:1] == 'b' and mn[1:3] in CONDS and len(mn) == 3:
                words.append(encode('br', CONDS.index(mn[1:3]), reg(0), reg(1), ev(args[2], ln)))
            elif mn[:1] == 'b' and mn[1:3] in CONDS and mn[3:] == 'i':
                c = ev(args[1], ln)
                if not -512 <= c <= 511:
                    raise AsmError(f'line {ln}: branch constant {c} outside -512..511')
                c &= 0x3FF
                words.append(encode('bri', CONDS.index(mn[1:3]), reg(0), c >> 6,
                                    ev(args[2], ln), c & 63))
            elif mn in ('jmp', 'call'):
                words.append(encode(mn, 0, 0, 0, ev(args[0], ln)))
            elif mn in ('ret', 'fend', 'nop'):
                words.append(encode(mn))
            elif mn == 'err':
                words.append(encode('err', 0, 0, 0, ev(args[0], ln)))
            elif mn == 'vop':
                words.append(encode('vop', 0, 0, 0, 0, VOP[args[0].lower()]))   # op in aux
            elif mn in ('frame', 'bpos'):
                words.append(encode(mn, reg(0)))
            else:
                raise AsmError(f'line {ln}: unknown instruction {mn!r}')
        except (IndexError, KeyError) as e:
            raise AsmError(f'line {ln}: bad operands in {line!r} ({e})')
    return words, labels, [ln for ln, _, _ in stmts]


# ------------------------------------------------------------------ emulator
class NeedFrame(Exception):
    pass


class EngineError(Exception):
    pass


# Cycle model (P1a's estimate; P1b's RTL replaces it with measured cycles).
CYC = {
    'instr': 1, 'ld': 2, 'get_bit': 1, 'vlc_bit': 2, 'vlc_fixed': 2,
    'vop_overhead': 3, 'mac': 1, 'block_digit': 6, 'fetch': 8,
    'imdct': 300, 'window': 520, 'bfly_band': 8,
}


class Machine:
    """The sequencer and its vector ops. feed(frame) then run(): it runs until the
    program waits for the next frame. PCM pairs land in self.pcm (L, R lists)."""

    def __init__(self, words, labels):
        self.prog = words
        self.labels = labels
        self.reg = [0] * 16
        self.pc = labels.get('RESET', 0)
        self.stack = []
        self.rec = [0] * REC_WORDS
        self.frames = []          # queued frames
        self.cur = None
        self.bitpos = 0
        self.overrun = 0
        # vector state
        self.X = [[[0] * 8 for _ in range(32)] for _ in range(NCH_MAX)]
        self.hist = [[[0] * 4 for _ in range(32)] for _ in range(NCH_MAX)]
        self.synth = [R.SynthFixed(), R.SynthFixed()]
        self.pcm = ([], [])
        self.counters = {}
        self.errors = {}
        self.cycles = 0
        self.frame_cycles = []
        self.lenient = 0
        self.by_cat = {}          # cycles by vector op; the rest is the sequencer
        self.trace = None        # a list to receive (kind, pc, addr, value): module doc
        self.vop_hook = None     # called as vop_hook(machine, op) after every vector op

    # -- input ------------------------------------------------------------
    def feed(self, data):
        self.frames.append(bytes(data))

    def bits(self, n):
        if n == 0:
            return 0
        v = 0
        for _ in range(n):
            byte = self.bitpos >> 3
            if byte < len(self.cur):
                b = (self.cur[byte] >> (7 - (self.bitpos & 7))) & 1
            else:
                b = 0
                self.overrun += 1
            v = (v << 1) | b
            self.bitpos += 1
        self.cycles += n * CYC['get_bit']
        return v

    def sbits(self, n):
        v = self.bits(n)
        return v - (1 << n) if n and v >> (n - 1) else v

    def vlc(self, book):
        """The tree walk the RTL does: one code bit per step."""
        n = HUFF_ROOTS[book]
        while True:
            leaf, val = HUFF_NODES[n][self.bits(1)]
            self.cycles += CYC['vlc_bit'] - CYC['get_bit']
            if leaf:
                self.cycles += CYC['vlc_fixed']
                return val
            n = val

    # -- memory --------------------------------------------------------------
    def load(self, a):
        a &= 0xFFFF
        if a < REC_WORDS:
            return s16(self.rec[a])
        if MEM_CONST <= a < MEM_CONST + len(CONST):
            return s16(CONST[a - MEM_CONST])
        raise EngineError(f'load from 0x{a:04x} at pc {self.pc}')

    def store(self, a, v):
        a &= 0xFFFF
        if a >= REC_WORDS:
            raise EngineError(f'store to 0x{a:04x} at pc {self.pc}')
        self.rec[a] = v & 0xFFFF
        if self.trace is not None:
            self.trace.append((1, self.pc, a, v & 0xFFFF))

    @staticmethod
    def alu(fn, a, b):
        f = FNS[fn]
        return {
            'add': lambda: a + b, 'sub': lambda: a - b, 'and': lambda: a & b,
            'or': lambda: a | b, 'xor': lambda: a ^ b, 'shl': lambda: a << (b & 15),
            'sar': lambda: a >> (b & 15), 'shr': lambda: (a & 0xFFFF) >> (b & 15),
            'slt': lambda: int(a < b), 'sltu': lambda: int((a & 0xFFFF) < (b & 0xFFFF)),
            'seq': lambda: int(a == b), 'sne': lambda: int(a != b),
            'min': lambda: min(a, b), 'max': lambda: max(a, b),
        }[f]()

    def setr(self, rd, v):
        if rd:
            self.reg[rd] = s16(v)
            if self.trace is not None:
                self.trace.append((0, self.pc, rd, self.reg[rd] & 0xFFFF))

    # -- vector ops ----------------------------------------------------------
    def vop(self, op):
        a = [self.reg[i] for i in range(8, 16)]
        name = VOPS[op]
        c0 = self.cycles
        self.cycles += CYC['vop_overhead']
        if name == 'xclr':
            for ch in range(NCH_MAX):
                for b in range(32):
                    self.X[ch][b] = [0] * 8
            self.cycles += 40
        elif name == 'xq':
            ch, b, ab, qsel, sidx, adj, lossless = a[0], a[1], a[2], a[3], a[4] & 0xFFFF, a[5], a[6]
            q, huff = self.extract(ab, qsel)
            if self.trace is not None:
                self.trace.extend((2, self.pc, i, v & 0xFFFFFF) for i, v in enumerate(q))
            scale = self.scale_value(sidx)
            if huff:
                scale = F.huff_scale(scale, T.SCALE_FACTOR_ADJ[adj & 3])
            self.X[ch][b] = F.dequant_band(q, ab, bool(lossless), scale)
            self.cycles += 8 * CYC['mac'] + 2
        elif name == 'xvq':
            ch, b, vqi, ssf, sidx = a[0], a[1], a[2] & 1023, a[3], a[4] & 0xFFFF
            vec = T.HIGH_FREQ_VQ[vqi][ssf * 8:ssf * 8 + 8]
            self.X[ch][b] = F.vq_band(vec, self.scale_value(sidx))
            self.cycles += CYC['fetch'] + 8 * CYC['mac']
        elif name == 'adpcm':
            ch, b, pvq, pmode = a[0], a[1], a[2] & 0xFFF, a[3]
            coeff = T.ADPCM_VB[pvq] if pmode else None
            self.X[ch][b], self.hist[ch][b] = F.adpcm_band(self.X[ch][b], self.hist[ch][b], coeff)
            self.cycles += (32 + 16) * CYC['mac'] if pmode else 4
        elif name == 'joint':
            ch, b, src, jidx = a[0], a[1], a[2], a[3]
            jsc = T.JOINT_SCALE_FACTORS[jidx]
            self.X[ch][b], self.hist[ch][b] = F.joint_band(self.X[src][b], jsc, self.hist[ch][b])
            self.cycles += 8 * CYC['mac'] + 4
        elif name == 'bfly':
            p, q = a[0], a[1]
            for b in range(32):
                self.X[p][b], self.X[q][b] = F.butterfly_band(self.X[p][b], self.X[q][b])
            self.cycles += 32 * CYC['bfly_band']
        elif name == 'mixsyn':
            j, amode, nmix, perfect = a[0], a[1], a[2:7], a[7]
            spk = R.PRM_CH_TO_SPKR[amode]
            gains = F.default_gains(amode)
            win = T.FIR_32BANDS_PERFECT_FIXED if perfect else T.FIR_32BANDS_NONPERFECT_FIXED
            x = self.X[:len(spk)]
            for side in (0, 1):
                gs = [gains.get(spk[ch], (0, 0))[side] for ch in range(len(spk))]
                inp = F.mix_column(x, j, gs, nmix[:len(spk)])
                pcm = self.synth[side].run(inp, win)
                self.pcm[side].extend(F.to_s16(v) for v in pcm)
                self.cycles += sum(min(nmix[c], 32) for c in range(len(spk)) if gs[c]) * CYC['mac']
                self.cycles += CYC['imdct'] + CYC['window']
        elif name == 'hclr':
            ch, frm = a[0], a[1]
            for b in range(max(frm, 0), 32):
                self.hist[ch][b] = [0] * 4
            self.cycles += 32
        elif name == 'cnt':
            self.counters[a[0]] = self.counters.get(a[0], 0) + 1
        self.by_cat[name] = self.by_cat.get(name, 0) + self.cycles - c0
        if self.vop_hook is not None:
            self.vop_hook(self, op)

    def checksums(self):
        """The engine buffers, each as sum((i + 1) * (v mod 2^w)) mod 2^32 over the
        RTL's own address order, so the bench can read the RAMs hierarchically:
          X     {ch, band, j}  (5 x 32 x 8)    w 25 (a butterflied band is 25 bits)
          hist  {ch, band, k}  (5 x 32 x 4)    w 24 (k = 0 the oldest)
          ring  {side, i}      (2 x 512)       w 24 (the IMDCT output ring, physical)
          buf2  {side, i}      (2 x 32)        w 29 (the window's carried partial sums)
        -> (x, hist, ring, buf2)"""
        def ck(vals, w):
            m = (1 << w) - 1
            return sum((i + 1) * (v & m) for i, v in enumerate(vals)) & 0xFFFFFFFF
        x = [self.X[ch][b][j] for ch in range(NCH_MAX) for b in range(32) for j in range(8)]
        h = [self.hist[ch][b][k] for ch in range(NCH_MAX) for b in range(32) for k in range(4)]
        ring = self.synth[0].buf + self.synth[1].buf
        b2 = self.synth[0].buf2 + self.synth[1].buf2
        return ck(x, 25), ck(h, 24), ck(ring, 24), ck(b2, 29)

    def scale_value(self, sidx):
        """sidx: bit 8 selects the 7-bit table (selector 6), else the 6-bit one."""
        if sidx & 0x100:
            return T.SCALE_FACTOR_QUANT7[sidx & 0x7F]
        return T.SCALE_FACTOR_QUANT6[sidx & 0x3F]

    def extract(self, ab, qsel):
        """XQ's code reader: R.extract_audio's semantics on the engine's bit reader
        and tree walker (lenient block codes, D5)."""
        if ab <= R.CODE_BOOKS and qsel < T.QUANT_INDEX_GROUP_SIZE[ab - 1]:
            return [s16(self.vlc(QBOOK[ab - 1] + qsel)) for _ in range(8)], True
        if ab <= 7 and not (ab <= R.CODE_BOOKS and qsel < T.QUANT_INDEX_GROUP_SIZE[ab - 1]):
            nb, levels = T.BLOCK_CODE_NBITS[ab - 1], T.QUANT_LEVELS[ab]
            off = (levels - 1) // 2
            out = []
            for _ in range(2):
                code = self.bits(nb)
                for _ in range(4):
                    code, r = divmod(code, levels)
                    out.append(r - off)
                    self.cycles += CYC['block_digit']
                if code:
                    self.lenient += 1
            return out, False
        return [self.sbits(ab - 3) for _ in range(8)], False

    # -- the sequencer -------------------------------------------------------
    def run(self, max_steps=50_000_000):
        steps = 0
        while steps < max_steps:
            steps += 1
            w = self.prog[self.pc]
            op, rd, rs, rt, imm, aux = decode(w)
            npc = self.pc + 1
            self.cycles += CYC['instr']
            if op == 'nop':
                pass
            elif op == 'alu':
                self.setr(rd, self.alu(aux, self.reg[rs], self.reg[rt]))
            elif op == 'alui':
                self.setr(rd, self.alu(aux, self.reg[rs], s16(imm)))
            elif op == 'ld':
                self.cycles += CYC['ld'] - CYC['instr']
                self.setr(rd, self.load(self.reg[rs] + self.reg[rt] + s16(imm)))
            elif op == 'st':
                self.store(self.reg[rs] + self.reg[rt] + s16(imm), self.reg[rd])
            elif op == 'get':
                self.setr(rd, self.bits(imm))
            elif op == 'getr':
                n = self.reg[rt]
                if not 0 <= n <= 16:
                    raise EngineError(f'getr {n} bits at pc {self.pc}')
                self.setr(rd, self.bits(n))
            elif op == 'vlc':
                self.setr(rd, self.vlc(self.reg[rs] + s16(imm)))
            elif op in ('br', 'bri'):
                a = self.reg[rs]
                b = self.reg[rt] if op == 'br' else (((rt << 6) | aux) ^ 0x200) - 0x200
                c = CONDS[rd]
                if (c == 'eq' and a == b) or (c == 'ne' and a != b) or \
                        (c == 'lt' and a < b) or (c == 'ge' and a >= b):
                    npc = imm
            elif op == 'jmp':
                npc = imm
            elif op == 'call':
                if len(self.stack) >= STACK_DEPTH:
                    raise EngineError(f'call stack overflow at pc {self.pc}')
                self.stack.append(npc)
                npc = imm
            elif op == 'ret':
                npc = self.stack.pop()
            elif op == 'err':
                self.errors[imm] = self.errors.get(imm, 0) + 1
                self.cur = None
                self.stack = []
                npc = self.labels['FRAME']
            elif op == 'vop':
                self.vop(aux)
            elif op == 'frame':
                if self.cur is not None:
                    self.frame_cycles.append(self.cycles - self.frame_start)
                if not self.frames:
                    self.cur = None
                    return                                 # waits here (pc unchanged)
                self.cur = self.frames.pop(0)
                self.bitpos = 0
                self.frame_start = self.cycles
                self.setr(rd, len(self.cur))
            elif op == 'fend':
                self.bitpos = len(self.cur) * 8 if self.cur is not None else 0
            elif op == 'bpos':
                self.setr(rd, self.bitpos & 0xFFFF)
            self.pc = npc
        raise EngineError('step limit')


def load_program(mutate=None):
    words, labels, _ = assemble(open(UASM).read(), mutate)
    return words, labels


def emulate(path, nframes=0, mutate=None, budget=False):
    """Emulate a raw .dts stream; -> (L, R, machine)."""
    words, labels = load_program(mutate)
    m = Machine(words, labels)
    n = 0
    for _, fr in R.frames(open(path, 'rb').read()):
        if nframes and n >= nframes:
            break
        m.feed(fr)
        m.run()
        n += 1
    return m.pcm[0], m.pcm[1], m


def _packed(vals, w):
    """A packed localparam: element i at [w*i +: w] (no unpacked localparam arrays:
    the RTL keeps to what Quartus 17 is known to elaborate)."""
    v = 0
    for i, x in enumerate(vals):
        assert 0 <= x < (1 << w), (x, w)
        v |= x << (w * i)
    return f"{w * len(vals)}'h{v:0{(w * len(vals) + 3) // 4}x}"


def ucode_svh(words, labels):
    """dvd/dts/dts_ucode.svh: the sizes, entry points and XQ code-reader tables
    the sequencer RTL needs, all derived here so --check covers them."""
    ab = range(1, R.CODE_BOOKS + 1)
    blk = range(1, 8)
    lines = [
        '// dvd/dts/dts_ucode.svh -- GENERATED by tools/dts_isa.py --asm; never edit.',
        '// The sequencer\'s sizes and entry points, and XQ\'s code-reader tables',
        '// (packed: element i at [w*i +: w]; index abits - 1).',
        f'localparam int UC_WORDS    = {len(words)};',
        f'localparam int CONST_WORDS = {len(CONST)};',
        f'localparam int HUFF_NODES  = {len(HUFF_NODES)};',
        f'localparam int HUFF_BOOKS  = {len(HUFF_ROOTS)};',
        f"localparam [9:0] UC_RESET  = 10'd{labels.get('RESET', 0)};",
        f"localparam [9:0] UC_FRAME  = 10'd{labels['FRAME']};",
        '// the first quantiser-index book of abits (book = QBOOK + selector)',
        f'localparam [{6 * R.CODE_BOOKS - 1}:0] XQ_QBOOK  = {_packed([QBOOK[a - 1] for a in ab], 6)};',
        '// selectors below this are Huffman books',
        f'localparam [{4 * R.CODE_BOOKS - 1}:0] XQ_GSIZE  = {_packed([T.QUANT_INDEX_GROUP_SIZE[a - 1] for a in ab], 4)};',
        '// block codes (abits 1..7): bits a code, and the levels (the divisor)',
        f'localparam [34:0] XQ_BNBITS = {_packed([T.BLOCK_CODE_NBITS[a - 1] for a in blk], 5)};',
        f'localparam [34:0] XQ_LEVELS = {_packed([T.QUANT_LEVELS[a] for a in blk], 5)};',
    ]
    return lines


def write_mems(check=False):
    words, labels, _ = assemble(open(UASM).read())
    files = {UMEM: [f'{w:010x}' for w in words],
             CMEM: [f'{w:04x}' for w in CONST],
             HMEM: [f'{w:07x}' for w in huff_mem_words()],
             RMEM: [f'{r:03x}' for r in HUFF_ROOTS],
             USVH: ucode_svh(words, labels)}
    bad = []
    for path, lines in files.items():
        text = '\n'.join(lines) + '\n'
        if check:
            if not os.path.exists(path) or open(path).read() != text:
                bad.append(os.path.relpath(path, REPO))
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, 'w').write(text)
    print(f'dts_isa: {len(words)} microcode words, {len(CONST)} constant words, '
          f'{len(HUFF_NODES)} Huffman nodes, {len(HUFF_ROOTS)} book roots')
    if check:
        print('dts_isa: ' + ('FAIL -- stale: ' + ', '.join(bad) if bad else 'PASS -- generated files match'))
        return 1 if bad else 0
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--asm', action='store_true')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--run')
    ap.add_argument('--frames', type=int, default=0)
    args = ap.parse_args()
    if args.asm:
        return write_mems(args.check)
    if args.run:
        R.OPT.add('lenient_block')
        L, Rr, m = emulate(args.run, args.frames)
        hw = F.StreamDecoder()
        GL, GR = [], []
        n = 0
        for _, fr in R.frames(open(args.run, 'rb').read()):
            if args.frames and n >= args.frames:
                break
            a, b, _, _ = hw.decode(fr)
            GL += a
            GR += b
            n += 1
        bad = sum(1 for x, y in zip(L + Rr, GL + GR) if x != y) + abs(len(L) - len(GL))
        fc = m.frame_cycles
        print(f'dts_isa: {n} frames, {len(L)} samples, {bad} mismatches vs dts_fixed; '
              f'errors {m.errors or "none"}; cycles a frame max {max(fc) if fc else 0}, '
              f'mean {sum(fc) // max(len(fc), 1)}')
        return 1 if bad or m.errors else 0
    ap.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
