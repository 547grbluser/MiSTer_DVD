#!/usr/bin/env python3
"""mp2_scan.py -- what do the library's MP2 streams use? And cut gate windows of them.

docs/mp2_engine.md M0. Two sources:
  - DVD images under $DVD_ISO_DIR whose title sets declare MPEG-1 audio (VTS audio
    coding mode 2): windows across the title VOBS, each stream's PES payload
    (stream_id 0xC0-0xC7) concatenated;
  - VCD / SVCD rips under $VCD_DIR (default ~/Videos/vcd), every "*Track 2*.bin"
    (MODE2/2352): deblocked (tools/cd_deblock_ref.py), then the audio elementary
    stream is taken with ffmpeg's MPEG-PS demuxer (-c:a copy).
Per stream, it tallies every frame's header through tools/mp2_ref.py: sample rate,
bitrate, mode (stereo / joint / dual / mono), mode_ext, the allocation table, and
protection -- what the gate set must cover. With --extract DIR it writes each window
as DIR/<name>.mp2 (whole frames from the first sync).
Usage: tools/mp2_scan.py [--extract DIR] [--window-mb N]
"""
import argparse
import glob
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mp2_ref as R                                    # noqa: E402
from cd_deblock_ref import deblock                     # noqa: E402

MODES = {0: 'stereo', 1: 'joint', 2: 'dual', 3: 'mono'}


def table_name(sr, br, mode):
    nch = 1 if mode == 3 else 2
    sblimit, _ = R.table_select(sr, br, nch)
    return {8: 'B.2c', 12: 'B.2d', 27: 'B.2a', 30: 'B.2b'}[sblimit]


def census(data):
    row = {}
    n = 0
    for _, h in R.iter_frames(data):
        n += 1
        key = (h['sample_rate'], h['bitrate'], MODES[h['mode']],
               h['mode_ext'] if h['mode'] == 1 else '-', table_name(h['sample_rate'], h['bitrate'], h['mode']),
               'crc' if h['protection'] == 0 else 'nocrc')
        row[key] = row.get(key, 0) + 1
    return n, row


def dvd_windows(iso, nwin, win_sectors):
    """-> [(name, bytes)] for each MPEG-audio stream of each title set."""
    from dvd_vm_ref import IsoNav
    from nav_extract import parse_vts_attr
    out = []
    nav = IsoNav(iso)
    try:
        for vn, ifo in sorted(nav.vts_ifo.items()):
            mat = nav.sec(ifo)
            audio = parse_vts_attr(mat)[2]
            if not any(a[0] in (2, 3) for a in audio):
                continue
            last_vts = struct.unpack('>I', mat[0x0C:0x10])[0]
            last_ifo = struct.unpack('>I', mat[0x1C:0x20])[0]
            tt = struct.unpack('>I', mat[0xC4:0xC8])[0]
            if not tt:
                continue
            start, end = ifo + tt, ifo + last_vts - (last_ifo + 1)
            span = end - start
            for i in range(nwin):
                pos = start + max(span - win_sectors, 0) * i // max(nwin - 1, 1)
                nav.f.seek(pos * 2048)
                blob = nav.f.read(win_sectors * 2048)
                per = {}
                for k in range(len(blob) // 2048):
                    sec = blob[k * 2048:(k + 1) * 2048]
                    if sec[:4] != b'\x00\x00\x01\xba':
                        continue
                    p = 14 + (sec[13] & 7)
                    while p + 9 < 2048 and sec[p:p + 3] == b'\x00\x00\x01':
                        sid = sec[p + 3]
                        plen = struct.unpack('>H', sec[p + 4:p + 6])[0]
                        body = p + 6
                        if 0xC0 <= sid <= 0xC7:
                            hdr = body + 3 + sec[body + 2]          # MPEG-2 PES header
                            per.setdefault(sid, bytearray()).extend(sec[hdr:min(2048, body + plen)])
                        p = body + plen
                stem = os.path.splitext(os.path.basename(iso))[0]
                for sid, b in per.items():
                    out.append((f'{stem}_vts{vn:02d}_{sid:02x}_w{i}', bytes(b)))
    finally:
        nav.f.close()
    return out


def vcd_windows(binpath, nwin, win_bytes):
    out = []
    size = os.path.getsize(binpath)
    stem = os.path.basename(os.path.dirname(binpath))
    for i in range(nwin):
        pos = (max(size - win_bytes, 0) * i // max(nwin - 1, 1)) // 2352 * 2352
        with open(binpath, 'rb') as f:
            f.seek(pos)
            raw = f.read(win_bytes // 2352 * 2352)
        ps = deblock(raw)
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, 'a.mpg'), os.path.join(d, 'a.mp2')
            open(src, 'wb').write(ps)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'mpeg', '-i', src, '-map', '0:a:0',
                            '-c:a', 'copy', '-f', 'mp2', dst], check=False)
            if os.path.exists(dst):
                out.append((f'vcd_{stem}_w{i}', open(dst, 'rb').read()))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--extract')
    ap.add_argument('--windows', type=int, default=3)
    ap.add_argument('--window-mb', type=float, default=2.0)
    a = ap.parse_args()
    streams = []
    root = os.environ.get('DVD_ISO_DIR')
    if root:
        for iso in sorted(glob.glob(os.path.join(root, '**', '*.[iI][sS][oO]'), recursive=True)):
            try:
                streams += dvd_windows(iso, a.windows, int(a.window_mb * 512))
            except Exception:
                pass
    vdir = os.path.expanduser(os.environ.get('VCD_DIR', '~/Videos/vcd'))
    for b in sorted(glob.glob(os.path.join(vdir, '**', '*Track 2*.bin'), recursive=True)):
        streams += vcd_windows(b, a.windows, int(a.window_mb * 1048576))
    tot = {}
    for name, data in streams:
        n, row = census(data)
        if not n:
            continue
        print(f'{name}: {n} frames, ' + '; '.join(f'{k[0]} Hz {k[1]}k {k[2]} ext {k[3]} {k[4]} {k[5]} x{v}'
                                                    for k, v in sorted(row.items())))
        for k, v in row.items():
            tot[k] = tot.get(k, 0) + v
        if a.extract:
            os.makedirs(a.extract, exist_ok=True)
            first = next(R.iter_frames(data), None)
            frames = b''.join(fr for fr, _ in R.iter_frames(data))
            if first:
                open(os.path.join(a.extract, name + '.mp2'), 'wb').write(frames)
    print('== across the library ==')
    for k, v in sorted(tot.items()):
        print(f'  {k[0]} Hz {k[1]:3d}k {k[2]:6s} ext {k[3]} {k[4]} {k[5]}: {v} frames')
    return 0


if __name__ == '__main__':
    sys.exit(main())
