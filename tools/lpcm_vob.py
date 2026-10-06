#!/usr/bin/env python3
"""lpcm_vob.py -- LPCM test VOBs for the hardware check of docs/lpcm_full.md, and the
scorer for what the core plays back.

FFmpeg's `dvd` muxer cannot make these: it writes no 20-bit, no 3/4/5/7-channel LPCM,
and it splits 20/24-bit groups across PES packets (docs/lpcm_full.md §11), which no
authoring tool does. So this keeps FFmpeg's VIDEO packs verbatim and writes the LPCM
packs itself: payloads in whole groups and whole sample-times, as authored discs carry
them, a PTS per PES, the first-access-unit pointer at the next 1/600 s frame.

The signal is a speaker walk, so a capture shows where every channel landed:
  segment k (k = 0 .. nch-1): channel k alone, a tone of 300 + 120 k Hz, 0.8 s, then
  0.2 s of silence. Expected at the stereo output: the downmix gains of channel k
  (tools/lpcm_model.py DMX_Q): FL left only, FR right only, a centre both equal, LFE
  nothing, a surround its own side.
  At 96 kHz one more segment: a 30 kHz tone on every channel at -6 dBFS, which the
  half-band must remove (below -80 dB) on a 48 kHz link; without a filter it would
  alias to 18 kHz.
  Then a 1 kHz tone on every channel, to the end (the level reference).

  python3 tools/lpcm_vob.py make OUT.vob --fs 96000 --nch 2 --bits 24 [--secs 12]
  python3 tools/lpcm_vob.py score CAPTURE.wav --fs 96000 --nch 2 [--link96]
"""
import argparse
import math
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lpcm_model as L  # noqa: E402

SEG_TONE, SEG_GAP = 0.8, 0.2
HF_HZ = 30000


def walk_hz(c):
    return 300 + 120 * c


def segments(fs, nch):
    """[(start s, end s, what)] of the speaker walk."""
    out, t = [], 0.5                              # 0.5 s of silence first
    for c in range(nch):
        out.append((t, t + SEG_TONE, ('ch', c)))
        t += SEG_TONE + SEG_GAP
    if fs == 96000:
        out.append((t, t + SEG_TONE, ('hf', None)))
        t += SEG_TONE + SEG_GAP
    out.append((t, None, ('ref', None)))
    return out


def signal(fs, nch, bits, secs):
    """The walk as a flat stream-order sample list at `bits`."""
    top = (1 << (bits - 1)) - 1
    amp = 0.25 * top                               # -12 dBFS: a downmix cannot clip
    segs = segments(fs, nch)
    n = int(secs * fs)
    out = [0] * (n * nch)
    for a, b, (kind, c) in segs:
        i0, i1 = int(a * fs), (int(b * fs) if b else n)
        for i in range(i0, min(i1, n)):
            t = (i - i0) / fs
            if kind == 'ch':
                out[i * nch + c] = int(amp * math.sin(2 * math.pi * walk_hz(c) * t))
            else:
                hz = HF_HZ if kind == 'hf' else 1000
                v = int((2 * amp if kind == 'hf' else amp) * math.sin(2 * math.pi * hz * t))
                for k in range(nch):
                    out[i * nch + k] = v
    return out


def ps_pack_header(scr90):
    """An MPEG-2 pack header (14 bytes), SCR in 90 kHz units, mux rate 10.08 Mbit/s."""
    b = bytearray(14)
    b[0:4] = b'\x00\x00\x01\xba'
    base, ext = scr90, 0
    b[4] = 0x44 | ((base >> 27) & 0x38) | ((base >> 28) & 0x03)
    b[5] = (base >> 20) & 0xFF
    b[6] = 0x04 | ((base >> 12) & 0xF8) | ((base >> 13) & 0x03)
    b[7] = (base >> 5) & 0xFF
    b[8] = 0x04 | ((base << 3) & 0xF8) | ((ext >> 7) & 0x03)
    b[9] = ((ext << 1) & 0xFE) | 0x01
    mux = 25200                                    # x 50 bytes/s
    b[10], b[11], b[12] = (mux >> 14) & 0xFF, (mux >> 6) & 0xFF, ((mux << 2) & 0xFC) | 0x03
    b[13] = 0xF8
    return bytes(b)


def pts_bytes(pts):
    return bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, 0x01 | ((pts >> 14) & 0xFE),
                  (pts >> 7) & 0xFF, 0x01 | ((pts << 1) & 0xFE)])


def scr_of(pack):
    b = pack
    return (((b[4] >> 3) & 7) << 30 | (b[4] & 3) << 28 | b[5] << 20 | ((b[6] >> 3) & 0x1F) << 15
            | (b[6] & 3) << 13 | b[7] << 5 | (b[8] >> 3))


def lpcm_packs(data, quant, nch, fs, pts0):
    """The payload stream -> 2048-byte packs: whole blocks, a PTS each (90 kHz)."""
    bits = 16 + 4 * quant
    st_bytes = nch * bits // 8                     # bytes a sample-time (whole groups below)
    g = 1 if quant == 0 else (2 if nch == 1 else 4)
    # the block: the smallest whole number of groups AND whole sample-times
    blk_samples = g
    while blk_samples % nch:
        blk_samples += g
    blk = blk_samples * bits // 8
    room = 2048 - 14 - 6 - 3 - 5 - 7
    pay = room // blk * blk
    au = fs // 600 * st_bytes                      # a 1/600 s access unit, in bytes
    b5 = (quant << 6) | ((1 if fs == 96000 else 0) << 4) | (nch - 1)
    out, off, k = [], 0, 0
    while off < len(data):
        chunk = data[off:off + pay]
        pts = pts0 + (off // st_bytes) * 90000 // fs
        nxt = (-off) % au                          # bytes to the next AU start
        fap = nxt + 4 if nxt < len(chunk) else 0
        nfr = (len(chunk) - nxt + au - 1) // au if nxt < len(chunk) else 0
        body = bytes([0xA0, nfr & 0xFF, fap >> 8, fap & 0xFF, k & 0x1F, b5, 0x80]) + chunk
        pad = 2048 - 14 - (6 + 3 + 5 + len(body))
        stuff = pad if 0 < pad < 6 else 0          # too short for a padding packet: PES
        pes = b'\x00\x00\x01\xbd' + struct.pack('>H', 3 + 5 + stuff + len(body)) \
            + bytes([0x81, 0x80, 5 + stuff]) + pts_bytes(pts) + b'\xff' * stuff + body
        if pad >= 6:                                # header stuffing instead
            pes += b'\x00\x00\x01\xbe' + struct.pack('>H', pad - 6) + b'\xff' * (pad - 6)
        assert len(pes) == 2048 - 14
        out.append((pts, pes))
        off += len(chunk)
        k += 1
    return out


def make(path, fs, nch, bits, secs):
    quant = {16: 0, 20: 1, 24: 2}[bits]
    if not L.legal(quant, fs, nch):
        print(f'note: {nch} ch x {fs} Hz x {bits} bit is over DVD-Video\'s 6.144 Mbit/s')
    s = signal(fs, nch, bits, secs)
    g = 1 if quant == 0 else L.group_samples(nch)
    s = s[:len(s) - len(s) % (g * nch)]
    data = L.pack_nch(s, quant, nch)
    with tempfile.TemporaryDirectory() as td:
        v = os.path.join(td, 'v.vob')
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                        '-i', f'testsrc=size=720x480:rate=30000/1001:duration={secs}',
                        '-c:v', 'mpeg2video', '-b:v', '3M', '-pix_fmt', 'yuv420p', '-an',
                        '-f', 'dvd', v], check=True)
        vid = open(v, 'rb').read()
    vpacks = [vid[i:i + 2048] for i in range(0, len(vid) - 2047, 2048)]
    # the first video PTS: audio starts with it
    p = vid.find(b'\x00\x00\x01\xe0')
    vpts = ((vid[p + 9] >> 1) & 7) << 30 | vid[p + 10] << 22 | (vid[p + 11] >> 1) << 15 \
        | vid[p + 12] << 7 | vid[p + 13] >> 1
    apacks = lpcm_packs(data, quant, nch, fs, vpts)
    lead = 27000                                   # an audio pack goes out 0.3 s ahead
    out, ai, last_scr = bytearray(), 0, 0
    for vp in vpacks:
        scr = scr_of(vp) if vp[:4] == b'\x00\x00\x01\xba' else last_scr
        while ai < len(apacks) and apacks[ai][0] - lead <= scr:
            out += ps_pack_header(last_scr) + apacks[ai][1]
            ai += 1
        out += vp
        last_scr = scr
    for pts, pes in apacks[ai:]:
        last_scr = max(last_scr, pts - lead)
        out += ps_pack_header(last_scr) + pes
    out += b'\x00\x00\x01\xb9'
    open(path, 'wb').write(out)
    print(f'{path}: {fs} Hz {nch} ch {bits}-bit, {secs} s, {len(apacks)} LPCM packs, '
          f'{len(vpacks)} video packs, {len(out) / 1e6:.1f} MB')


def score(wav, fs, nch, link96, t0=None):
    """A capture (48 kHz stereo WAV) of the walk -> PASS/FAIL per segment. t0: where the
    walk starts in the capture; found from the 1 kHz reference's onset if absent."""
    import numpy as np
    import wave
    with wave.open(wav, 'rb') as w:
        rate, nchan = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(float) / 32768
    a = a.reshape(-1, nchan)[:, :2]

    def tone_db(x, hz):
        win = np.hanning(len(x))
        f = np.fft.rfft(x * win)
        k = int(round(hz * len(x) / rate))
        e = np.abs(f[max(k - 2, 1):k + 3]).max() * 2 / win.sum()
        return 20 * math.log10(e) if e > 1e-9 else -180.0

    segs = segments(fs, nch)
    if t0 is None:
        # the first walk tone's onset: the first 50 ms window louder than -40 dBFS
        env = np.sqrt((a ** 2).mean(axis=1))
        hop = rate // 20
        on = next((i for i in range(0, len(env) - hop, hop) if env[i:i + hop].mean() > 0.01), None)
        if on is None:
            print('FAIL: the capture is silent'); return False
        t0 = on / rate - segs[0][0]
    g = L.DMX_Q[nch]
    ok = True
    print(f'walk found at {t0 + segs[0][0]:.2f} s in the capture')
    for a0, b0, (kind, c) in segs:
        if kind == 'ref':
            continue
        i0 = int((t0 + a0 + 0.15) * rate)
        i1 = int((t0 + (b0 if b0 else a0 + 1)) * rate) - int(0.1 * rate)
        seg = a[i0:i1]
        if kind == 'ch':
            hz = walk_hz(c)
            dl, dr = tone_db(seg[:, 0], hz), tone_db(seg[:, 1], hz)
            want = [g[c][0] / 131072, g[c][1] / 131072]
            # -12 dBFS on the disc x the gain; -999 where the gain is 0
            exp = [(-12.04 + 20 * math.log10(v)) if v else None for v in want]
            res = []
            for side, d, e in (('L', dl, exp[0]), ('R', dr, exp[1])):
                good = (d < -60) if e is None else (abs(d - e) < 1.5)
                res.append(good)
                ok &= good
            print(f'{"ok  " if all(res) else "FAIL"} ch {c} ({hz} Hz): L {dl:6.1f} dB R {dr:6.1f} dB, '
                  f'want L {"silent" if exp[0] is None else f"{exp[0]:.1f}"} '
                  f'R {"silent" if exp[1] is None else f"{exp[1]:.1f}"}')
        elif link96:
            # no half-band on a 96 kHz link, and the 48 kHz capture's own resampler
            # removes 30 kHz: this check would pass vacuously, so it is not scored
            print('skip 30 kHz segment: on a 96 kHz link the capture cannot see it')
        else:
            worst = -180.0
            for img in (HF_HZ, 48000 - HF_HZ):         # the tone, or its alias at 18 kHz
                if img < rate / 2:
                    worst = max(worst, tone_db(seg[:, 0], img), tone_db(seg[:, 1], img))
            good = worst < -70
            ok &= good
            print(f'{"ok  " if good else "FAIL"} 30 kHz at -6 dBFS: worst image in band '
                  f'{worst:.1f} dB (want < -70: the half-band removed it)')
    print('lpcm_vob score: ' + ('PASS' if ok else 'FAIL'))
    return ok


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest='cmd', required=True)
    m = sp.add_parser('make')
    m.add_argument('out')
    s = sp.add_parser('score')
    s.add_argument('wav')
    s.add_argument('--link96', action='store_true')
    s.add_argument('--t0', type=float)
    for p in (m, s):
        p.add_argument('--fs', type=int, default=48000, choices=(48000, 96000))
        p.add_argument('--nch', type=int, default=2, choices=range(1, 9))
    m.add_argument('--bits', type=int, default=16, choices=(16, 20, 24))
    m.add_argument('--secs', type=float, default=12)
    a = ap.parse_args()
    if a.cmd == 'make':
        make(a.out, a.fs, a.nch, a.bits, a.secs)
    else:
        sys.exit(0 if score(a.wav, a.fs, a.nch, a.link96, a.t0) else 1)
