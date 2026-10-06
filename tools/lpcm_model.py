#!/usr/bin/env python3
"""lpcm_model.py -- the golden model of the core's DVD-Video LPCM path (docs/lpcm_full.md).

Every LPCM format DVD-Video allows: 48 or 96 kHz, 16/20/24-bit, 1-8 channels. What the
RTL (dvd/lpcm_unpack.sv + dvd/lpcm_hb.sv) must produce, bit for bit:

  bytes -> unpack (FFmpeg pcm-dvd.c's grouping, top 16 bits of each sample)
        -> downmix to stereo (FFmpeg's channel order, the AC-3 path's law)
        -> [96 kHz on a 48 kHz link] the 71-tap half-band, 2:1
        -> s16 pairs

Stereo at the output rate (2 channels, no decimation) is the legacy path and is
bit-identical to the input samples: the downmix is the identity there.

  python3 tools/lpcm_model.py --selftest        # the model against FFmpeg + the filter spec
  python3 tools/lpcm_model.py --fixture DIR     # the bench fixtures (bench/dvd/lpcm_full_tb.sv)
  python3 tools/lpcm_model.py --sv              # the RTL's coefficient tables, printed

The constants here are the contract. tools/lpcm_model.py --selftest re-derives both
tables from their definitions and fails if the committed integers have drifted.
"""
import math
import os
import random
import struct
import subprocess
import sys
import tempfile

# --------------------------------------------------------------------------- header
FS_CODES = {0: 48000, 1: 96000}           # 2 (44.1 k) and 3 (32 k) are not DVD-Video


def parse_hdr(b5):
    """The sub-header byte +5 (quant[7:6] freq[5:4] res[3] channels-1[2:0]).
    Returns (quant, fs, nch, bad): bad = a value the format reserves."""
    quant, freq, nch = b5 >> 6, (b5 >> 4) & 3, (b5 & 7) + 1
    bad = quant == 3 or freq not in FS_CODES
    return quant, FS_CODES.get(freq), nch, bad


# --------------------------------------------------------------------------- packing
def group_samples(nch):
    """Samples per packing group at 20/24 bit: mono 2, everything else 4 (pcm-dvd.c)."""
    return 2 if nch == 1 else 4


def pack_nch(samples, quant, nch):
    out = bytearray()
    if quant == 0:
        for s in samples:
            out += struct.pack('>h', s)
        return bytes(out)
    g = group_samples(nch)
    assert len(samples) % g == 0, "a whole number of groups"
    for i in range(0, len(samples), g):
        grp = samples[i:i + g]
        if quant == 1:                       # 20-bit: hi16, then a nibble each
            for s in grp:
                out += struct.pack('>H', (s >> 4) & 0xFFFF)
            for j in range(0, g, 2):
                out.append(((grp[j] & 0xF) << 4) | (grp[j + 1] & 0xF))
        else:                                # 24-bit: hi16, then a byte each
            for s in grp:
                out += struct.pack('>H', (s >> 8) & 0xFFFF)
            for s in grp:
                out.append(s & 0xFF)
    return bytes(out)


def unpack(data, quant, nch):
    """The payload bytes -> s16 samples in stream order (the top 16 bits of each).
    Trailing bytes short of a whole sample/group are ignored (as the RTL holds them)."""
    out = []
    if quant == 0:
        for i in range(0, len(data) - 1, 2):
            out.append(struct.unpack('>h', data[i:i + 2])[0])
        return out
    g = group_samples(nch)
    glen = g * 2 + (g // 2 if quant == 1 else g)
    for i in range(0, len(data) - glen + 1, glen):
        for j in range(g):
            out.append(struct.unpack('>h', data[i + 2 * j:i + 2 * j + 2])[0])
    return out


# --------------------------------------------------------------------------- downmix
# FFmpeg's default layout for each channel count (av_channel_layout_default), the
# maintainer's choice of order (docs/lpcm_full.md §10). Each channel's (left, right)
# weight before normalisation, by the AC-3 path's law (dvd/ac3/imdct_512.sv S_DMX): a
# centre at -3 dB to both, a surround at -3 dB to its side, a MONO surround (back
# centre) at -3 dB x -3 dB = 0.5 to both, LFE dropped. Then every gain is divided by
# the sum of one side's weights -- liba52's 1/(1 + clev + slev) -- so no sum can clip.
C3 = 92682 / 131072        # imdct_512's LEVEL_3DB, exactly (Q1.17)
B_MONO = 65536 / 131072    # imdct_512's mono-surround slev at surmixlev 0
LAYOUT = {
    1: ['FC'],
    2: ['FL', 'FR'],
    3: ['FL', 'FR', 'LFE'],                                   # 2.1
    4: ['FL', 'FR', 'FC', 'BC'],                              # 4.0
    5: ['FL', 'FR', 'FC', 'BL', 'BR'],                        # 5.0
    6: ['FL', 'FR', 'FC', 'LFE', 'BL', 'BR'],                 # 5.1
    7: ['FL', 'FR', 'FC', 'LFE', 'BC', 'SL', 'SR'],           # 6.1
    8: ['FL', 'FR', 'FC', 'LFE', 'BL', 'BR', 'SL', 'SR'],     # 7.1
}
WEIGHT = {
    'FL': (1.0, 0.0), 'FR': (0.0, 1.0), 'FC': (C3, C3), 'LFE': (0.0, 0.0),
    'BC': (B_MONO, B_MONO), 'BL': (C3, 0.0), 'BR': (0.0, C3),
    'SL': (C3, 0.0), 'SR': (0.0, C3),
}
# Mono is the one layout with no side: its centre goes to both at unity.
WEIGHT_MONO = (1.0, 1.0)


def dmx_gains_float(nch):
    if nch == 1:
        return [WEIGHT_MONO]
    w = [WEIGHT[r] for r in LAYOUT[nch]]
    sl = sum(a for a, _ in w)
    sr = sum(b for _, b in w)
    assert abs(sl - sr) < 1e-12, "every layout is left/right symmetric"
    return [(a / sl, b / sl) for a, b in w]


def dmx_gains_q():
    """{nch: [(gl, gr), ...]} in Q1.17 (131072 = 1.0), round half up."""
    return {n: [(int(math.floor(a * 131072 + 0.5)), int(math.floor(b * 131072 + 0.5)))
                for a, b in dmx_gains_float(n)] for n in range(1, 9)}


# The committed table (the RTL's dmx_g case is printed from this by --sv).
DMX_Q = {
    1: [(131072, 131072)],
    2: [(131072, 0), (0, 131072)],
    3: [(131072, 0), (0, 131072), (0, 0)],
    4: [(59386, 0), (0, 59386), (41993, 41993), (29693, 29693)],
    5: [(54292, 0), (0, 54292), (38390, 38390), (38390, 0), (0, 38390)],
    6: [(54292, 0), (0, 54292), (38390, 38390), (0, 0), (38390, 0), (0, 38390)],
    7: [(44977, 0), (0, 44977), (31803, 31803), (0, 0), (22488, 22488), (31803, 0),
        (0, 31803)],
    8: [(41992, 0), (0, 41992), (29693, 29693), (0, 0), (29693, 0), (0, 29693),
        (29693, 0), (0, 29693)],
}


def sat16(v):
    return -32768 if v < -32768 else 32767 if v > 32767 else v


def rnd17(acc):
    """(acc + 2^16) >> 17, arithmetic: round half up, as the RTL."""
    return (acc + (1 << 16)) >> 17


def downmix(samples, nch):
    """s16 samples in stream order -> stereo s16 pairs, one per whole sample-time."""
    g = DMX_Q[nch]
    out = []
    for t in range(0, len(samples) - nch + 1, nch):
        al = ar = 0
        for c in range(nch):
            al += samples[t + c] * g[c][0]
            ar += samples[t + c] * g[c][1]
        out.append((sat16(rnd17(al)), sat16(rnd17(ar))))
    return out


# --------------------------------------------------------------------------- half-band
# 71 taps (4 x 18 - 1), a Kaiser (beta 9) windowed half-band sinc, Q1.17, the centre
# trimmed so the taps sum to exactly 1.0. Every even offset from the centre is zero, so
# 37 taps are non-zero: k = 0, 2, ..., 70 and the centre, 35. Measured with the
# quantised taps: pass band 0-20 kHz flat to 0.0006 dB, stop band 28-48 kHz below
# -88 dB (at 96 kHz in). Re-derived by hb_design() in --selftest.
HB_N = 71
HB_BETA = 9.0
HB_TAPS_NZ = [  # h[0], h[2], ..., h[34] (h[70 - k] = h[k]); then h[35]
    -1, 6, -16, 37, -74, 135, -229, 369, -569, 847, -1230, 1752, -2469, 3486,
    -5022, 7649, -13480, 41577]
HB_CENTRE = 65536


def hb_taps():
    h = [0] * HB_N
    for j, v in enumerate(HB_TAPS_NZ):
        h[2 * j] = v
        h[HB_N - 1 - 2 * j] = v
    h[(HB_N - 1) // 2] = HB_CENTRE
    return h


def hb_design():
    """The derivation of HB_TAPS_NZ (needs numpy)."""
    import numpy as np
    c = (HB_N - 1) // 2
    n = np.arange(HB_N) - c
    h = 0.5 * np.sinc(n / 2.0) * np.kaiser(HB_N, HB_BETA)
    h /= h.sum()
    q = [int(math.floor(x * 131072 + 0.5)) for x in h]
    q[c] += 131072 - sum(q)
    return q


def halfband(pairs):
    """Stereo pairs at 96 kHz -> 48 kHz: output m is computed when input 2m + 1 has
    arrived, y[m] = sum_k h[k] x[2m + 1 - k], inputs before the first taken as 0."""
    h = hb_taps()
    out = []
    for n in range(1, len(pairs), 2):
        al = ar = 0
        for k in range(HB_N):
            if h[k] and n - k >= 0:
                al += h[k] * pairs[n - k][0]
                ar += h[k] * pairs[n - k][1]
        out.append((sat16(rnd17(al)), sat16(rnd17(ar))))
    return out


# --------------------------------------------------------------------------- the path
def decode(data, quant, nch, fs, link96):
    """The whole path: what reaches the pair FIFO. Returns (pairs, out_rate)."""
    s = unpack(data, quant, nch)
    if nch == 2:
        pairs = [(s[i], s[i + 1]) for i in range(0, len(s) - 1, 2)]
    else:
        pairs = downmix(s, nch)
    if fs == 96000 and not link96:
        return halfband(pairs), 48000
    return pairs, fs


def legal(quant, fs, nch):
    """The 6.144 Mbit/s cap (3rd ed. Table 9.28)."""
    return nch * fs * (16 + 4 * quant) <= 6144000


# --------------------------------------------------------------------------- self-test
def _ffmpeg_vob(fs, nch, fmt, secs, path):
    layout = {1: 'mono', 2: 'stereo', 6: '5.1(side)', 8: '7.1'}[nch]
    # a different tone per channel so a mis-paired channel cannot pass
    srcs = ''.join('sine=frequency=%d:sample_rate=%d:duration=%s[a%d];'
                   % (300 + 170 * c, fs, secs, c) for c in range(nch))
    ins = ''.join('[a%d]' % c for c in range(nch))
    fc = srcs + '%samerge=inputs=%d,channelmap=channel_layout=%s[o]' % (
        ins, nch, layout) if nch > 1 else 'sine=frequency=300:sample_rate=%d:duration=%s[o]' % (fs, secs)
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'quiet', '-y', '-filter_complex', fc,
           '-map', '[o]', '-c:a', 'pcm_dvd', '-sample_fmt', fmt, '-f', 'dvd', path]
    subprocess.run(cmd, check=True)


def lpcm_payload(path):
    """Concatenate the LPCM PES payloads of a PS file; return (bytes, the last b5)."""
    d = open(path, 'rb').read()
    out = bytearray()
    b5 = None
    p = 0
    while True:
        p = d.find(b'\x00\x00\x01\xbd', p)
        if p < 0:
            break
        plen = (d[p + 4] << 8) | d[p + 5]
        hl = d[p + 8]
        q = p + 9 + hl
        if 0xA0 <= d[q] <= 0xA7:
            b5 = d[q + 5]
            out += d[q + 7:p + 6 + plen]
        p += 6 + plen
    return bytes(out), b5


def _ffmpeg_decode_s16(path):
    r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', path,
                        '-f', 's32le', '-'], check=True, capture_output=True)
    v = struct.unpack('<%di' % (len(r.stdout) // 4), r.stdout)
    return [x >> 16 for x in v]


def selftest():
    fails = 0
    # 1. the committed tables equal their definitions
    if dmx_gains_q() != DMX_Q:
        print('FAIL DMX_Q differs from dmx_gains_q():', dmx_gains_q()); fails += 1
    try:
        d = hb_design()
        if d != hb_taps():
            print('FAIL HB taps differ from hb_design():', d[:36:2], d[35]); fails += 1
    except ImportError:
        print('skip hb_design (no numpy)')
    if sum(hb_taps()) != 131072:
        print('FAIL HB taps do not sum to 1.0'); fails += 1
    # 2. the filter meets its spec (quantised taps)
    try:
        import numpy as np
        h = np.array(hb_taps()) / 131072
        def mag(f):
            w = 2 * np.pi * np.asarray(f) / 96000
            return np.abs(np.exp(-1j * np.outer(w, np.arange(HB_N))) @ h)
        pb = mag(np.linspace(0, 20000, 800)); sb = mag(np.linspace(28000, 48000, 800))
        rip = 20 * np.log10(pb.max() / pb.min()); stop = 20 * np.log10(sb.max())
        ok = rip < 0.001 and stop < -87.5
        print('%s half-band: pass-band ripple %.4f dB, stop band %.1f dB'
              % ('ok  ' if ok else 'FAIL', rip, stop))
        fails += not ok
    except ImportError:
        print('skip filter response (no numpy)')
    # 3. pack/unpack round trip, every channel count and word length
    rnd = random.Random(1)
    for quant in (0, 1, 2):
        bits = 16 + 4 * quant
        for nch in range(1, 9):
            n = 4 * nch * 3
            s = [rnd.randrange(-(1 << (bits - 1)), 1 << (bits - 1)) for _ in range(n)]
            got = unpack(pack_nch(s, quant, nch), quant, nch)
            want = [x >> (bits - 16) for x in s]
            if got != want:
                print('FAIL round trip quant %d nch %d' % (quant, nch)); fails += 1
    print('ok   pack/unpack round trip: 16/20/24-bit x 1-8 channels')
    # 4. unpack against FFmpeg's decoder, every layout its encoder writes
    have_ff = subprocess.run(['which', 'ffmpeg'], capture_output=True).returncode == 0
    if not have_ff:
        print('skip FFmpeg cross-check (no ffmpeg)')
    else:
        checked = 0
        with tempfile.TemporaryDirectory() as td:
            for fs in (48000, 96000):
                for nch in (1, 2, 6, 8):
                    for fmt, quant in (('s16', 0), ('s32', 2)):
                        vob = os.path.join(td, 'x.vob')
                        try:
                            _ffmpeg_vob(fs, nch, fmt, '0.3', vob)
                        except subprocess.CalledProcessError:
                            if legal(quant, fs, nch):
                                print('FAIL FFmpeg refused a legal format: %d Hz %d ch %s'
                                      % (fs, nch, fmt)); fails += 1
                            continue        # over its cap: FFmpeg's encoder refuses
                        checked += 1
                        data, b5 = lpcm_payload(vob)
                        q, f, n, bad = parse_hdr(b5)
                        if (q, f, n, bad) != (quant, fs, nch, False):
                            print('FAIL header %02x -> %s' % (b5, (q, f, n, bad))); fails += 1
                            continue
                        ours = unpack(data, quant, nch)
                        ff = _ffmpeg_decode_s16(vob)
                        m = min(len(ours), len(ff))
                        if m == 0 or ours[:m] != ff[:m] or abs(len(ours) - len(ff)) > 4 * nch:
                            print('FAIL unpack vs FFmpeg: %d Hz %d ch %s (%d vs %d samples)'
                                  % (fs, nch, fmt, len(ours), len(ff))); fails += 1
            print('ok   unpack == FFmpeg pcm_dvd decode on %d formats (48/96 kHz x 1/2/6/8 ch '
                  'x 16/24-bit, those its encoder accepts)' % checked)
    print('lpcm_model selftest: %s' % ('PASS' if fails == 0 else 'FAIL'))
    return fails == 0


# --------------------------------------------------------------------------- fixtures
# bench/dvd/lpcm_full_tb.sv's cases. The letter is the arm: run_lpcm_full.sh's RED
# mutations each name the arms they must fail. Pressure arms feed a byte every cycle
# into a 64-pair FIFO drained slowly, so `full` and lpcm_hb's `hold` are exercised;
# every other arm feeds a byte per 8 cycles and drains fast, so neither ever rises.
#       arm  quant nch  fs     link96 pressure sample-times
CASES = [
    ('A', 0, 1, 48000, 0, 0, 300),    # mono 16
    ('B', 2, 1, 48000, 0, 0, 300),    # mono 24 (2-sample groups)
    ('C', 1, 1, 48000, 0, 0, 300),    # mono 20
    ('D', 2, 3, 48000, 0, 0, 300),    # 2.1, 24
    ('E', 1, 4, 48000, 0, 0, 300),    # 4.0, 20
    ('F', 2, 5, 48000, 0, 0, 300),    # 5.0, 24
    ('G', 1, 6, 48000, 0, 0, 300),    # 5.1, 20
    ('H', 0, 7, 48000, 0, 0, 300),    # 6.1, 16
    ('I', 0, 8, 48000, 0, 0, 300),    # 7.1, 16
    ('J', 2, 2, 96000, 0, 0, 400),    # 96 k stereo 24 -> half-band
    ('K', 0, 4, 96000, 0, 0, 400),    # 96 k 4.0 16 -> half-band
    ('L', 1, 3, 96000, 0, 0, 400),    # 96 k 2.1 20 -> half-band
    ('M', 2, 1, 96000, 0, 0, 400),    # 96 k mono 24 -> half-band
    ('N', 0, 2, 96000, 1, 0, 300),    # 96 k stereo on a 96 k link: the original path
    ('O', 0, 4, 96000, 1, 0, 300),    # 96 k 4.0 on a 96 k link: downmix only
    ('P', 1, 2, 48000, 0, 0, 300),    # 48 k stereo 20: the original path (control)
    ('Q', 2, 6, 48000, 0, 1, 600),    # pressure: 5.1 24 into a full FIFO
    ('R', 2, 2, 96000, 0, 1, 900),    # pressure: 96 k stereo 24, the half-band backed up
    ('T', 0, 1, 48000, 0, 1, 900),    # pressure: mono 16, a pair per 2 bytes in flight
]


def test_signal(nch, n, bits, seed):
    """n sample-times of nch channels, stream order. Each channel its own tone plus
    noise, with full-scale runs (both rails) so rounding and saturation are hit."""
    rnd = random.Random(seed)
    top = (1 << (bits - 1)) - 1
    out = []
    for t in range(n):
        for c in range(nch):
            if 40 <= t < 48:
                v = top if c % 2 == 0 else -top - 1            # the rails
            elif 120 <= t < 124:
                v = -top - 1                                    # all at the negative rail
            else:
                v = int(0.6 * top * math.sin(2 * math.pi * t * (0.011 + 0.017 * c)))
                v += rnd.randrange(-top // 8, top // 8)
            out.append(max(-top - 1, min(top, v)))
    return out


def write_fixtures(d):
    os.makedirs(d, exist_ok=True)
    bytes_all, pairs_all, table = bytearray(), [], []
    for arm, quant, nch, fs, link96, press, n in CASES:
        bits = 16 + 4 * quant
        g = 1 if quant == 0 else group_samples(nch)
        n -= n % (4 if g == 4 else 2)               # whole groups at any count
        s = test_signal(nch, n, bits, ord(arm))
        s = s[:len(s) - len(s) % g]
        data = pack_nch(s, quant, nch)
        pairs, rate = decode(data, quant, nch, fs, link96)
        dec = int(fs == 96000 and not link96)
        table.append((ord(arm), quant, nch - 1, dec, press, len(bytes_all), len(data),
                      len(pairs_all), len(pairs)))
        bytes_all += data
        pairs_all += pairs
    with open(os.path.join(d, 'lpcm_full_bytes.hex'), 'w') as f:
        f.write('\n'.join('%02x' % b for b in bytes_all) + '\n')
    with open(os.path.join(d, 'lpcm_full_pairs.hex'), 'w') as f:
        f.write('\n'.join('%04x%04x' % (l & 0xFFFF, r & 0xFFFF) for l, r in pairs_all) + '\n')
    with open(os.path.join(d, 'lpcm_full_cases.hex'), 'w') as f:
        for arm, q, nm1, dec, press, bo, bn, po, pn in table:
            f.write('%02x%01x%01x%01x%01x%06x%06x%06x%06x\n' % (arm, q, nm1, dec, press, bo, bn, po, pn))
    with open(os.path.join(d, 'lpcm_full_sizes.svh'), 'w') as f:
        f.write('// GENERATED by tools/lpcm_model.py --fixture; never edit.\n')
        f.write('localparam int NCASE  = %d;\n' % len(table))
        f.write('localparam int NBYTE  = %d;\n' % len(bytes_all))
        f.write('localparam int NPAIR  = %d;\n' % len(pairs_all))
    print('fixtures: %d cases, %d bytes, %d pairs -> %s'
          % (len(table), len(bytes_all), len(pairs_all), d))


# --------------------------------------------------------------------------- tables
def print_sv():
    print('// dmx gains, Q1.17: {nch_m1, ch} -> {gl, gr} (tools/lpcm_model.py DMX_Q)')
    for n in range(1, 9):
        for c, (a, b) in enumerate(DMX_Q[n]):
            print("            6'o%d%d: begin gl = 18'sd%d; gr = 18'sd%d; end"
                  % (n - 1, c, a, b))
    print('// half-band taps, Q1.17: j -> h[2j] (j < 18), j = 18 -> the centre')
    for j, v in enumerate(HB_TAPS_NZ + [HB_CENTRE]):
        print("            5'd%d: hc = %s18'sd%d;" % (j, '-' if v < 0 else '', abs(v)))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.exit(0 if selftest() else 1)
    if '--sv' in sys.argv:
        print_sv(); sys.exit(0)
    if '--fixture' in sys.argv:
        write_fixtures(sys.argv[sys.argv.index('--fixture') + 1]); sys.exit(0)
    print(__doc__)
