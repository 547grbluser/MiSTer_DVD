#!/usr/bin/env python3
"""dts_fixed.py -- the DTS core decoder in the HARDWARE's order and arithmetic.

The bit-exact golden for the RTL (docs/dts_decoder.md sec 6, link 3). It differs
from tools/dts_ref.py (FFmpeg's order, bit-exact against the FFmpeg binary) in
exactly the two ways the design note decides, and each is measured here:

 1. STREAMING (sec 3). Everything is done per SUBSUBFRAME as it is parsed: the
    sample codes are dequantised, the high-frequency VQ vectors expanded,
    ADPCM predicted, joint intensity applied, and the result synthesised before
    the next subsubframe is read. FFmpeg instead finishes a whole subframe of
    sample codes before its ADPCM, VQ and joint passes. Those passes are causal
    within a band, so the subband samples must come out IDENTICAL:
    `verify` checks that bit for bit against dts_ref's front end.

 2. STEREO BY A SUBBAND-DOMAIN DOWNMIX (D3). The primary channels are mixed to
    L/R before synthesis, so two synthesis filters run instead of up to five.
    That is not bit-exact with mixing after synthesis; `verify` measures the
    difference against dts_ref's per-channel PCM mixed afterwards with the same
    coefficients.

The downmix rule (D3, decided by the maintainer 2026-10-02): the one this
core's AC-3 decoder uses (liba52 A52_STEREO | A52_ADJUST_LEVEL), at its default
levels -- see default_gains(). A stream's embedded coefficients are ignored
(and counted). LFE is left out.

Output: s16 stereo, rounded half up from the 24-bit synthesis domain and
saturated.

Block codes past levels^4 are decoded leniently -- the four low digits kept,
the overflow counted -- as libdca does and FFmpeg does not (D5, ⏳ proposed):
one library disc carries hundreds of them in otherwise valid frames, and
refusing them would silence it half the time. Both decoders in `verify` run
lenient, so they stay comparable.

Usage:
    tools/dts_fixed.py verify <file.dts> [--frames N]
    tools/dts_fixed.py decode <file.dts> out.wav [--frames N]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dts_ref as R                                              # noqa: E402
import dts_tables as T                                           # noqa: E402

Q = 15                                         # downmix coefficient fraction bits
# Mutation hooks for tools/test_dts_fixed.py's RED arms (empty in normal use).
MUT = set()
SQRT1_2 = 0.70710678118654752


def default_gains(amode):
    """-> {speaker: (gL, gR)} as Q15 ints: the AC-3 path's Lo/Ro rule (D3, decided).

    dvd/ac3/imdct_512.sv's effective levels, at the A/52 defaults (DTS has no
    mix-level fields): clev = slev = 0.7071, zeroed for a role the layout does
    not carry; a MONO surround is split to both outputs and pre-scaled by
    another 0.7071 (liba52 slev*LEVEL_3DB) -- and that pre-scaled value is the
    one the normalisation uses. Then k = 1 / (1 + clev_eff + slev_eff)
    (liba52 A52_ADJUST_LEVEL). Mono, stereo, sum/difference stereo and Lt/Rt
    pass through at unity; dual mono (AMODE 1) maps A to L and B to R, as FFmpeg
    does (AC-3 rejects its dual mono). LFE is not mixed.
    """
    spk = R.PRM_CH_TO_SPKR[amode]
    if amode == 0:                              # mono: C to both, unity
        return {R.SPK_C: (1 << Q, 1 << Q)}
    clev = SQRT1_2 if R.SPK_C in spk else 0.0
    if R.SPK_LS in spk:                         # a surround pair: slev as is
        slev = SQRT1_2
    elif R.SPK_CS in spk:                       # a mono surround: -3 dB more
        slev = SQRT1_2 * SQRT1_2
    else:
        slev = 0.0
    k = 1.0 / (1.0 + clev + slev)
    g = {R.SPK_L: (k, 0.0), R.SPK_R: (0.0, k)}
    if clev:
        g[R.SPK_C] = (k * clev, k * clev)
    if R.SPK_LS in spk:
        g[R.SPK_LS] = (k * slev, 0.0)
        g[R.SPK_RS] = (0.0, k * slev)
    if R.SPK_CS in spk:
        g[R.SPK_CS] = (k * slev, k * slev)
    return {sp: (round(a * (1 << Q)), round(b * (1 << Q))) for sp, (a, b) in g.items()}


def to_s16(v):
    v = (v + 128) >> 8
    return -32768 if v < -32768 else 32767 if v > 32767 else v


# --------------------------------------------------------------------------
# The hardwired loops, one function each. tools/dts_isa.py's vector ops call
# these SAME functions, so the instruction-level emulator (the RTL's golden) is
# anchored to this model rather than to itself (docs/dts_decoder.md sec 6).
# --------------------------------------------------------------------------
def step_size(abits, lossless):
    return T.LOSSLESS_QUANT[abits] if lossless else T.LOSSY_QUANT[abits]


def huff_scale(scale, adj):
    """A Huffman-coded band's scale, adjusted (adj: the Q22 SCALE_FACTOR_ADJ value)."""
    return R.clip23((adj * scale) >> 22)


def dequant_band(q, abits, lossless, scale):
    """8 quantised codes -> 8 subband samples."""
    return R.dequantize(q, step_size(abits, lossless), scale)


def vq_band(vec8, scale):
    """One subsubframe's 8 high-frequency VQ values (int8) x scale -> 8 samples."""
    return [R.clip23((v * scale + 8) >> 4) for v in vec8]


def adpcm_band(x8, hist, coeff):
    """coeff = the 4 ADPCM_VB coefficients, or None for an unpredicted band.
    -> (8 samples, the new 4-sample history)."""
    if coeff is None:
        if 'stale_history' in MUT:
            return list(x8), list(hist)
        return list(x8), (list(hist) + list(x8))[-R.ADPCM_COEFFS:]
    hb, out = list(hist), []
    for j in range(R.SUBBAND_SAMPLES):
        pred = sum(hb[-1 - i] * coeff[i] for i in range(R.ADPCM_COEFFS))
        v = R.clip23(x8[j] + R.clip23(R.norm(pred, 13)))
        out.append(v)
        hb = hb[1:] + [v]
    return out, hb


def joint_band(src8, jsc, hist):
    """A band past a channel's own count: its source's samples x the joint scale.
    -> (8 samples, the new history)."""
    x8 = [R.clip23(R.mul(v, jsc, 17)) for v in src8]
    return x8, (list(hist) + x8)[-R.ADPCM_COEFFS:]


def butterfly_band(a8, b8):
    """Sum/difference of one band of a channel pair -> (a + b, a - b)."""
    return [u + v for u, v in zip(a8, b8)], [u - v for u, v in zip(a8, b8)]


def mix_column(x, j, gains_side, nmix):
    """One output side's 32 synthesis inputs for sample j of the subsubframe:
    x[ch][band][j] mixed with Q15 gains, one rounding. gains_side[ch] is that
    channel's gain on this side; nmix[ch] bounds its bands (sec D3)."""
    inp = []
    for b in range(R.SUBBANDS):
        acc = 0
        for ch, g in enumerate(gains_side):
            if g and b < nmix[ch]:
                acc += x[ch][b][j] * g
        inp.append(R.clip23(R.norm(acc, Q) if 'mix_trunc' not in MUT else acc >> (Q - 2)))
    return inp


class StreamDecoder:
    """Frame bytes -> (L, R) s16, in the hardware's order."""

    def __init__(self):
        # per channel, per band: the last 4 reconstructed subband samples
        self.hist = [[[0] * R.ADPCM_COEFFS for _ in range(R.SUBBANDS)] for _ in range(7)]
        self.synth = [R.SynthFixed(), R.SynthFixed()]
        self.ops = {'mac': 0, 'bits': 0, 'frames': 0, 'synth_blocks': 0, 'pred_bands': 0,
                    'hist_from_plain': 0, 'bfly_uneven': 0, 'dmix_ignored': 0}
        self.plain = [[False] * R.SUBBANDS for _ in range(7)]

    def decode(self, data, trace=None):
        """`trace`, if a list, receives (ch, band, sample index, value) of every
        front-end subband sample -- the hook `verify` compares with dts_ref."""
        br = R.BitReader(data)
        h = R.parse_frame_header(br)
        c = R.parse_coding_header(br, h)
        nch, npb = c['nchannels'], h['npcmblocks']
        amode = h['audio_mode']
        spk = R.PRM_CH_TO_SPKR[amode]
        if not h['predictor_history']:
            for ch in range(7):
                for b in range(R.SUBBANDS):
                    self.hist[ch][b] = [0] * R.ADPCM_COEFFS
                    self.plain[ch][b] = False
        win = T.FIR_32BANDS_PERFECT_FIXED if h['filter_perfect'] else T.FIR_32BANDS_NONPERFECT_FIXED
        gains = default_gains(amode)
        L, Rr = [], []
        nact = []
        for ch in range(nch):
            n = c['nsubbands'][ch]
            if c['joint_index'][ch]:
                n = max(n, c['nsubbands'][c['joint_index'][ch] - 1])
            nact.append(n)
        t0 = 0                                     # subband sample index within the frame
        for sf in range(c['nsubframes']):
            s = R.parse_subframe_header(br, h, c)
            # head of the subframe's audio data: the VQ indices, then LFE (consumed;
            # LFE is not in the stereo mix, D3)
            vq = [{b: T.HIGH_FREQ_VQ[br.bits(10)] for b in range(c['vq_start'][ch], c['nsubbands'][ch])}
                  for ch in range(nch)]
            if h['lfe_present']:
                for _ in range(2 * h['lfe_present'] * s['nssf']):
                    br.sbits(8)
                br.bits(8)
            for ssf in range(s['nssf']):
                # x[ch][band] = this subsubframe's 8 samples
                x = [[[0] * R.SUBBAND_SAMPLES for _ in range(R.SUBBANDS)] for _ in range(nch)]
                for ch in range(nch):
                    for b in range(c['vq_start'][ch]):
                        a = s['abits'][ch][b]
                        q, huff = R.extract_audio(br, c, ch, a)
                        t = s['tmode'][ch][b]
                        scale = s['scales'][ch][b][0 if (t == 0 or ssf < t) else 1]
                        if huff:
                            scale = huff_scale(scale, c['scale_adj'][ch][a - 1])
                        x[ch][b] = dequant_band(q, a, h['bit_rate'] == 3, scale)
                        self.ops['mac'] += R.SUBBAND_SAMPLES
                    for b, vec in vq[ch].items():
                        sc = s['scales'][ch][b][0]
                        base = ssf * R.SUBBAND_SAMPLES
                        x[ch][b] = vq_band(vec[base:base + R.SUBBAND_SAMPLES], sc)
                        self.ops['mac'] += R.SUBBAND_SAMPLES
                if ssf == s['nssf'] - 1 or h['sync_ssf']:
                    if br.bits(16) != 0xFFFF:
                        raise R.DtsError('E_DSYNC', f'DSYNC failed (subframe {sf}, ssf {ssf})')
                # ADPCM, causal per band: uses (and updates) the running history
                for ch in range(nch):
                    for b in range(c['nsubbands'][ch]):
                        hb = self.hist[ch][b]
                        if s['pmode'][ch][b] and self.plain[ch][b] and any(hb):
                            self.ops['hist_from_plain'] += 1   # stale_history can bite here
                        self.plain[ch][b] = not s['pmode'][ch][b]
                        coeff = T.ADPCM_VB[s['pvq'][ch][b]] if s['pmode'][ch][b] else None
                        x[ch][b], self.hist[ch][b] = adpcm_band(x[ch][b], hb, coeff)
                        if coeff is not None:
                            self.ops['mac'] += 4 * R.SUBBAND_SAMPLES
                            self.ops['pred_bands'] += 1
                # joint intensity: bands past a channel's own count copy its source
                for ch in range(nch):
                    src = c['joint_index'][ch] - 1
                    if src >= 0:
                        for b in range(c['nsubbands'][ch], c['nsubbands'][src]):
                            x[ch][b], self.hist[ch][b] = joint_band(x[src][b], s['jscale'][ch][b],
                                                                    self.hist[ch][b])
                            self.ops['mac'] += R.SUBBAND_SAMPLES
                if trace is not None:
                    for ch in range(nch):
                        for b in range(R.SUBBANDS):
                            for j in range(R.SUBBAND_SAMPLES):
                                trace.append((ch, b, t0 + j, x[ch][b][j] if b < nact[ch] else 0))
                # sum/difference in the subband domain (linear, so it commutes
                # with synthesis; FFmpeg applies it to the PCM). ⚠ After it, BOTH
                # channels of the pair carry bands up to the larger of their two
                # active counts: the downmix loop below is bounded by `nmix`, and
                # bounding it by each channel's own count dropped R's upper bands
                # from L+R (written_amode3: 3,459 LSB).
                nmix = list(nact)

                def bfly(p, q):
                    if p in spk and q in spk:
                        ip, iq = spk.index(p), spk.index(q)
                        for b in range(R.SUBBANDS):
                            x[ip][b], x[iq][b] = butterfly_band(x[ip][b], x[iq][b])
                        if nact[ip] != nact[iq]:
                            self.ops['bfly_uneven'] += 1   # the pair bound matters here
                        if 'mix_own_bound' not in MUT:
                            nmix[ip] = nmix[iq] = max(nact[ip], nact[iq])
                if (h['sumdiff_front'] and amode > 0) or amode == R.AMODE_STEREO_SUMDIFF:
                    bfly(R.SPK_L, R.SPK_R)
                if h['sumdiff_surround'] and amode >= R.AMODE_2F2R:
                    bfly(R.SPK_LS, R.SPK_RS)
                # downmix to L/R in the subband domain, one rounding per output
                for j in range(R.SUBBAND_SAMPLES):
                    for side, out in ((0, L), (1, Rr)):
                        gs = [gains.get(spk[ch], (0, 0))[side] for ch in range(nch)]
                        inp = mix_column(x, j, gs, nmix)
                        self.ops['mac'] += sum(min(nmix[ch], R.SUBBANDS) for ch in range(nch) if gs[ch])
                        pcm = self.synth[side].run(inp, win)
                        self.ops['synth_blocks'] += 1
                        out.extend(to_s16(v) for v in pcm)
                t0 += R.SUBBAND_SAMPLES
        # bands past each channel's active count are cleared, history included
        for ch in range(nch):
            for b in range(nact[ch], R.SUBBANDS):
                self.hist[ch][b] = [0] * R.ADPCM_COEFFS
        o = R.parse_optional_info(br, h, c)
        if o['dmix_coeff'] is not None:
            # D3: the AC-3 rule is used; a stream's own coefficients are ignored,
            # and counted so it is never silent that they were
            self.ops['dmix_ignored'] += 1
        self.ops['bits'] += h['frame_size'] * 8
        self.ops['frames'] += 1
        return L, Rr, h, c


def mix_after(pcm, amode, gains):
    """dts_ref's per-speaker 24-bit PCM, mixed AFTER synthesis with the same
    Q15 gains (exact integer sum, one rounding) -> (L, R) s16."""
    spks = [sp for sp in R.PRM_CH_TO_SPKR[amode]]
    n = len(pcm[spks[0]])
    out = ([], [])
    for side in (0, 1):
        for t in range(n):
            acc = sum(pcm[sp][t] * gains.get(sp, (0, 0))[side] for sp in spks)
            out[side].append(to_s16(R.clip23(R.norm(acc, Q))))
    return out


def run_verify(path, nframes=0):
    R.OPT.add('lenient_block')                     # D5: the hardware's behaviour
    """-> {frames, fe_bad, worst, rms, hist, ops}: [1] the streaming front end
    against dts_ref's (bit-exact expected), [2] mix-before vs mix-after."""
    buf = open(path, 'rb').read()
    ref = R.Decoder()
    hw = StreamDecoder()
    nf, fe_bad, worst, sq, cnt, hist = 0, 0, 0, 0, 0, {}
    for _, fr in R.frames(buf):
        if nframes and nf >= nframes:
            break
        trace = []
        L, Rr, h, c = hw.decode(fr, trace)
        r = ref.decode(fr)
        for ch, b, t, v in trace:
            if r['sb'][ch][b][t] != v:
                fe_bad += 1
        gl, gr = mix_after(r['pcm'], h['audio_mode'], default_gains(h['audio_mode']))
        for a, b_ in zip(L + Rr, gl + gr):
            d = abs(a - b_)
            worst = max(worst, d)
            sq += d * d
            cnt += 1
            hist[d] = hist.get(d, 0) + 1
        nf += 1
    return {'frames': nf, 'fe_bad': fe_bad, 'worst': worst,
            'rms': (sq / cnt) ** 0.5 if cnt else 0.0, 'hist': dict(sorted(hist.items())),
            'ops': hw.ops, 'samples': cnt}


def cmd_verify(args):
    v = run_verify(args.file, args.frames)
    print(f'verify: {v["frames"]} frames')
    print(f'  [1] streaming front end vs FFmpeg order: {v["fe_bad"]} subband samples differ '
          f'-> {"PASS (bit-exact)" if not v["fe_bad"] else "FAIL"}')
    print(f'  [2] mix-before vs mix-after synthesis, s16: worst {v["worst"]} LSB, '
          f'rms {v["rms"]:.4f}, distribution {v["hist"]}')
    o = v['ops']
    if o['frames']:
        print(f'  ops a frame: {o["mac"] // o["frames"]} MACs (excl. synthesis), '
              f'{o["synth_blocks"] // o["frames"]} synthesis blocks, '
              f'{o["bits"] // o["frames"]} bits')
    return 1 if v['fe_bad'] else 0


def cmd_decode(args):
    import wave
    R.OPT.add('lenient_block')                     # D5
    buf = open(args.file, 'rb').read()
    hw = StreamDecoder()
    pcm = bytearray()
    nf = 0
    for _, fr in R.frames(buf):
        if args.frames and nf >= args.frames:
            break
        L, Rr, _, _ = hw.decode(fr)
        for a, b in zip(L, Rr):
            pcm += a.to_bytes(2, 'little', signed=True) + b.to_bytes(2, 'little', signed=True)
        nf += 1
    with wave.open(args.out, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(bytes(pcm))
    print(f'decode: {nf} frames -> {args.out}')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('verify')
    p.add_argument('file')
    p.add_argument('--frames', type=int, default=0)
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser('decode')
    p.add_argument('file')
    p.add_argument('out')
    p.add_argument('--frames', type=int, default=0)
    p.set_defaults(fn=cmd_decode)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
