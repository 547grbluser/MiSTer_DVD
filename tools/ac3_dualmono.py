#!/usr/bin/env python3
"""ac3_dualmono.py -- make an open-content AC-3 1+1 (acmod 0, dual mono) test stream.

No encoder at hand writes acmod 0 (FFmpeg's does not), and the one library window
that carries it is from a commercial disc, so it cannot be committed. This rewrites
an FFmpeg 2/0 stream, encoded with coupling and rematrixing off, frame by frame into
1+1 (docs/lpcm_full.md §7):

  * acmod 2 -> 0, and 2/0's dsurmod (2 bits) removed;
  * Ch2's copy of dialnorm / compre / langcode / audprodie inserted after audprodie
    (dialnorm2 = dialnorm, the three flags 0);
  * every block's rematstr (and block 0's rematrix flags, all 0) removed: 2/0 only;
  * every block's dynrng2e (0) inserted after dynrng: 1+1 only;
  * the frame keeps its length: the bits saved go to the aux field (auxdatae 0);
  * crc1 and crc2 recomputed (crc1 solved over GF(2): it sits inside what it covers).

The audio blocks are otherwise untouched, so Ch1 is the original left (a 440 Hz
tone) and Ch2 the original right (1 kHz). The field positions come from
tools/ac3_model.py's own parse (a logging BitReader), so the rewrite cannot disagree
with the model about where a field is.

  python3 tools/ac3_dualmono.py [out.ac3] [--secs S]   # default tools/streams/dualmono_440_1k_48k_192k.ac3, 0.5 s
  python3 tools/ac3_dualmono.py --check FILE  # a52dec and FFmpeg (CRC-checked) decode it as
                                              # 1+1 with 440 Hz left and 1 kHz right
"""
import inspect
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ac3_model as M  # noqa: E402

SRC_LINES = inspect.getsource(M).splitlines()
MODEL_FILE = os.path.abspath(M.__file__)


class LogReader(M.BitReader):
    """A BitReader that records (start bit, width, value, model source line) per read."""
    last = None

    def __init__(self, data):
        super().__init__(data)
        self.log = []
        LogReader.last = self

    def bits(self, n):
        p = self.pos
        v = super().bits(n)
        f = sys._getframe(1)
        while f and (f.f_code.co_name == 'sbits' or os.path.abspath(f.f_code.co_filename) != MODEL_FILE):
            f = f.f_back
        line = SRC_LINES[f.f_lineno - 1] if f else ''
        self.log.append((p, n, v, line))
        return v


def crc16(data, crc=0):
    """A/52's CRC: x^16 + x^15 + x^2 + 1, MSB first."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def solve_crc1(fr, f58):
    """crc1 (bytes 2-3) such that the CRC over bytes [2, f58) is 0."""
    def f(x):
        b = bytearray(fr)
        b[2], b[3] = x >> 8, x & 0xFF
        return crc16(b[2:f58])
    f0 = f(0)
    cols = [f(1 << i) ^ f0 for i in range(16)]
    # Gaussian elimination: sum_i x_i cols[i] = f0
    rows = []      # (mask over x bits, rhs) per output bit
    for bit in range(16):
        m = sum(((cols[i] >> bit) & 1) << i for i in range(16))
        rows.append([m, (f0 >> bit) & 1])
    x, piv = 0, []
    r = 0
    for c in range(16):
        k = next((j for j in range(r, 16) if (rows[j][0] >> c) & 1), None)
        if k is None:
            continue
        rows[r], rows[k] = rows[k], rows[r]
        for j in range(16):
            if j != r and (rows[j][0] >> c) & 1:
                rows[j][0] ^= rows[r][0]
                rows[j][1] ^= rows[r][1]
        piv.append(c)
        r += 1
    for j, c in enumerate(piv):
        if rows[j][1]:
            x |= 1 << c
    assert f(x) == 0, 'crc1 has no solution'
    return x


def to_bits(data):
    return [(data[i >> 3] >> (7 - (i & 7))) & 1 for i in range(8 * len(data))]


def from_bits(bits):
    out = bytearray(len(bits) // 8)
    for i, b in enumerate(bits):
        out[i >> 3] |= b << (7 - (i & 7))
    return bytes(out)


def rewrite(fr):
    """One 2/0 frame (coupling and rematrixing off) -> the same audio as 1+1."""
    saved = M.BitReader
    M.BitReader = LogReader
    try:
        hdr, _ = M.Decoder().frame(fr)
    finally:
        M.BitReader = saved
    rd = LogReader.last
    assert hdr['acmod'] == 2, 'the source must be 2/0'
    end_blocks = rd.pos
    drop, acmod_at, ins_at, dialnorm = set(), None, None, None
    cplstre = set()                 # each block's cplstre: dynrng2e (0) goes before it
    for p, n, v, line in rd.log:
        if '# cplstre' in line:
            cplstre.add(p)
        if 'acmod = br.bits(3)' in line:
            acmod_at = p
        elif '# dsurmod' in line or '# rematstr' in line or 'self.rematflg |= br.bits(1)' in line:
            drop.update(range(p, p + n))
        elif '# dialnorm' in line and dialnorm is None:
            dialnorm = v
        elif '# copyrightb, origbs' in line:
            ins_at = p
        elif 'phsflginu' in line or 'cplinu' in line and v:
            raise SystemExit('the source uses coupling: encode with -channel_coupling 0')
    assert None not in (acmod_at, ins_at, dialnorm)
    bits = to_bits(fr)
    dual = [(dialnorm >> (4 - i)) & 1 for i in range(5)] + [0, 0, 0]   # dialnorm2; compr2e,
    out = []                                                           # langcod2e, audprodi2e 0
    for i in range(end_blocks):
        if i == ins_at:
            out += dual
        if i in cplstre:
            out.append(0)                                              # dynrng2e
        if acmod_at <= i < acmod_at + 3:
            out.append(0)
        elif i not in drop:
            out.append(bits[i])
    total = 8 * len(fr)
    if len(out) > total - 18:
        raise SystemExit('the frame has no room for the dual block')
    out += [0] * (total - 18 - len(out)) + [0, 0] + [0] * 16    # aux, auxdatae, crcrsv, crc2
    b = bytearray(from_bits(out))
    f58 = ((len(fr) >> 2) + (len(fr) >> 4)) << 1
    c1 = solve_crc1(b, f58)
    b[2], b[3] = c1 >> 8, c1 & 0xFF
    c2 = crc16(b[f58:-2])
    b[-2], b[-1] = c2 >> 8, c2 & 0xFF
    assert crc16(b[2:f58]) == 0 and crc16(b[f58:]) == 0
    return bytes(b)


def make(out_path, secs='0.5'):
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, 'src.ac3')
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=48000:duration={secs}',
                        '-f', 'lavfi', '-i', f'sine=frequency=1000:sample_rate=48000:duration={secs}',
                        '-filter_complex', '[0][1]amerge=inputs=2[a]', '-map', '[a]',
                        '-c:a', 'ac3', '-b:a', '192k', '-channel_coupling', '0',
                        '-stereo_rematrixing', '0', '-f', 'ac3', src], check=True)
        data = open(src, 'rb').read()
    frs = [rewrite(fr) for _, fr in M.frames(data)]
    open(out_path, 'wb').write(b''.join(frs))
    print(f'{out_path}: {len(frs)} frames of 1+1')


def tone_hz(pcm, rate=48000):
    """The frequency of the strongest FFT bin of a mono int list (needs numpy)."""
    import numpy as np
    x = np.asarray(pcm, dtype=float)
    x = x * np.hanning(len(x))
    k = int(np.argmax(np.abs(np.fft.rfft(x))[1:])) + 1
    return k * rate / len(x)


def check(path):
    ok = True
    # the model decodes every frame as 1+1
    d = M.Decoder()
    n = 0
    for _, fr in M.frames(open(path, 'rb').read()):
        hdr, _ = d.frame(fr)
        ok &= hdr['acmod'] == 0
        n += 1
    print(f'model: {n} frames, {d.stats["dualmono"]} of them 1+1')
    ok &= d.stats['dualmono'] == n > 0
    with tempfile.TemporaryDirectory() as td:
        for name, cmd in (
                ('ffmpeg', ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-err_detect', 'crccheck+explode',
                            '-i', path, '-f', 's16le', '-ac', '2', os.path.join(td, 'ff.raw')]),
                ('a52dec', ['sh', '-c', f'a52dec -o wav "{path}" > {os.path.join(td, "a52.wav")}'])):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(f'FAIL {name}: {r.stderr.strip()[:200]}'); ok = False; continue
            raw = open(os.path.join(td, 'ff.raw' if name == 'ffmpeg' else 'a52.wav'), 'rb').read()
            if name == 'a52dec':
                raw = raw[44:]
            s = struct.unpack('<%dh' % (len(raw) // 2), raw[:len(raw) // 4 * 4])
            l, rr = list(s[0::2])[4800:], list(s[1::2])[4800:]
            fl, fr_ = tone_hz(l), tone_hz(rr)
            good = abs(fl - 440) < 15 and abs(fr_ - 1000) < 30
            print(f'{"ok  " if good else "FAIL"} {name}: left {fl:.0f} Hz, right {fr_:.0f} Hz')
            ok &= good
    # the core's own arithmetic (ac3_model -> imdct_model, imdct_512 bit for bit)
    # against a52dec, sample for sample: Ch1 must be the left, Ch2 the right
    try:
        import numpy as np
        import imdct_model as I
        im = I.Imdct512()
        mine = [[], []]
        for hdr, blocks in I.decode_blocks(path, 0):
            for b in blocks:
                pcm = im.block(b['coeff'], I.nfchans(hdr['acmod']), b['blksw'], b['dynrng'],
                               hdr['acmod'], hdr['cmixlev'], hdr['surmixlev'])
                mine[0] += [I.w32(v) for v in pcm[0]]
                mine[1] += [I.w32(v) for v in pcm[1]]
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(['sh', '-c', f'a52dec -o wav "{path}" > {td}/a.wav 2>/dev/null'], check=True)
            raw = open(f'{td}/a.wav', 'rb').read()[44:]
        ref = np.frombuffer(raw[:len(raw) // 4 * 4], dtype='<i2').reshape(-1, 2).T.astype(float)
        n = min(len(mine[0]), ref.shape[1])
        corr = [[float(np.corrcoef(np.asarray(mine[a][:n], float), ref[b][:n])[0, 1])
                 for b in (0, 1)] for a in (0, 1)]
        good = corr[0][0] > 0.999 and corr[1][1] > 0.999 and abs(corr[0][1]) < 0.1
        print(f'{"ok  " if good else "FAIL"} the core\'s arithmetic vs a52dec: Ch1~L {corr[0][0]:.5f}, '
              f'Ch2~R {corr[1][1]:.5f}, Ch1~R {corr[0][1]:+.3f}')
        ok &= good
    except ImportError:
        print('skip the arithmetic cross-check (no numpy)')
    print('ac3_dualmono check: ' + ('PASS' if ok else 'FAIL'))
    return ok


if __name__ == '__main__':
    if '--check' in sys.argv:
        sys.exit(0 if check(sys.argv[sys.argv.index('--check') + 1]) else 1)
    secs = '0.5'
    if '--secs' in sys.argv:                    # e.g. 8 for a hardware-check VOB
        k = sys.argv.index('--secs')
        secs = sys.argv[k + 1]
        del sys.argv[k:k + 2]
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'streams', 'dualmono_440_1k_48k_192k.ac3')
    make(out, secs)
    sys.exit(0 if check(out) else 1)
