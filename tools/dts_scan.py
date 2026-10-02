#!/usr/bin/env python3
"""dts_scan.py -- what do the library's DTS tracks ACTUALLY use?

P0 of docs/dts_decoder.md. For every title set that declares a DTS stream, it
samples windows spread across the WHOLE title VOBS (VTSI_MAT 0xC4 .. the
title set's end, 0x0C -- not 0xC0, which is the menu VOBS), pulls every DTS
substream (private stream 1, 0x88-0x8F) out of them, and parses every frame
with tools/dts_ref.py's front end. Per stream it records the header fields and
the coded features each frame used.

⚠ Unlike AC-3's acmod (tools/acmod_scan.py), ADPCM prediction, high-frequency
VQ, transients and joint intensity change FRAME BY FRAME, so this samples many
windows across the title set rather than stopping at the first frames.

What it is for: setting priorities and finding test streams. It is NOT where
limits come from -- a sweep says what is common, not what is possible
(CLAUDE.md "Design to the DVD spec maximum"; docs/dts_decoder.md sec 5).

Usage:
    tools/dts_scan.py                       # every image under $DVD_ISO_DIR
    tools/dts_scan.py <iso> [<iso> ...]
    tools/dts_scan.py --json out.jsonl      # one row per stream
    tools/dts_scan.py --extract DIR         # also save windows that cover the
                                            # reference gate's coverage gaps
"""
import argparse
import glob
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dts_ref                                                  # noqa: E402
from dvd_vm_ref import IsoNav                                   # noqa: E402
from nav_extract import parse_vts_attr                          # noqa: E402

FMT_DTS = 6                       # VTS audio attribute coding mode
FEATURES = ('pmode_bands', 'vq_bands', 'huff', 'block', 'raw', 'transient', 'joint_bands')


def dts_payloads(f, start, n):
    """-> {substream_id: bytes} for the DTS substreams in sectors start..start+n."""
    out = {}
    f.seek(start * 2048)
    blob = f.read(n * 2048)
    for k in range(len(blob) // 2048):
        sec = blob[k * 2048:(k + 1) * 2048]
        if sec[:4] != b'\x00\x00\x01\xba':
            continue
        p = 14 + (sec[13] & 7)
        while p + 6 < 2048 and sec[p:p + 3] == b'\x00\x00\x01':
            sid = sec[p + 3]
            plen = struct.unpack('>H', sec[p + 4:p + 6])[0]
            body = p + 6
            if sid == 0xBD and body + 3 < 2048:
                sub = body + 3 + sec[body + 2]
                if sub + 4 <= min(2048, body + plen) and 0x88 <= sec[sub] <= 0x8F:
                    # 4-byte substream header: id, frame count, first-AU pointer
                    out.setdefault(sec[sub], bytearray()).extend(sec[sub + 4:body + plen])
            p = body + plen
    return out


def scan_stream(windows):
    """windows: [bytes] for one substream. -> summary dict."""
    s = {'frames': 0, 'errors': {}, 'amode': set(), 'lfe': set(), 'bit_rate': set(),
         'npcmblocks': set(), 'nsubframes': set(), 'frame_size': set(),
         'ext': set(), 'filter_perfect': set(), 'predictor_history': set(),
         'sumdiff_front': 0, 'sumdiff_surround': 0, 'crc_present': 0, 'sync_ssf': 0,
         'drc': 0, 'aux': 0, 'aux_dmix': 0, 'es_format': 0,
         'max_nsubbands': 0, 'frames_pmode': 0, 'frames_vq': 0,
         'frames_transient': 0, 'frames_joint': 0, 'frames_huff': 0}
    for f in FEATURES:
        s[f] = 0
    best = {}                     # feature -> (count, window index)
    for wi, data in enumerate(windows):
        dec = dts_ref.CoreDecoder()
        wfeat = dict.fromkeys(FEATURES + ('sumdiff_front',), 0)
        for _, fr in dts_ref.frames(bytes(data)):
            try:
                r = dec.decode_frame(fr)
            except dts_ref.DtsError as e:
                s['errors'][e.code] = s['errors'].get(e.code, 0) + 1
                dec = dts_ref.CoreDecoder()
                continue
            h, c, st = r['h'], r['c'], r['stats']
            s['frames'] += 1
            s['amode'].add(h['audio_mode'])
            s['lfe'].add(h['lfe_present'])
            s['bit_rate'].add(h['bit_rate'])
            s['npcmblocks'].add(h['npcmblocks'])
            s['nsubframes'].add(c['nsubframes'])
            s['frame_size'].add(h['frame_size'])
            if h['ext_audio_present']:
                s['ext'].add(h['ext_audio_type'])
            s['filter_perfect'].add(h['filter_perfect'])
            s['predictor_history'].add(h['predictor_history'])
            sf = bool(h['sumdiff_front'] and h['audio_mode'] > 0)
            s['sumdiff_front'] += sf
            wfeat['sumdiff_front'] += sf
            s['sumdiff_surround'] += bool(h['sumdiff_surround'] and h['audio_mode'] >= 8)
            s['crc_present'] += h['crc_present']
            s['sync_ssf'] += h['sync_ssf']
            s['drc'] += h['drc_present']
            s['es_format'] += h['es_format']
            s['aux'] += h['aux_present']
            s['aux_dmix'] += r['opt']['dmix_coeff'] is not None
            s['max_nsubbands'] = max(s['max_nsubbands'], max(c['nsubbands']))
            for k in FEATURES:
                s[k] += st[k]
                wfeat[k] += st[k]
            s['frames_pmode'] += st['pmode_bands'] > 0
            s['frames_vq'] += st['vq_bands'] > 0
            s['frames_transient'] += st['transient'] > 0
            s['frames_joint'] += st['joint_bands'] > 0
            s['frames_huff'] += st['huff'] > 0
        for k, v in wfeat.items():
            if v and v > best.get(k, (0, -1))[0]:
                best[k] = (v, wi)
    for k in ('amode', 'lfe', 'bit_rate', 'npcmblocks', 'nsubframes', 'frame_size', 'ext',
              'filter_perfect', 'predictor_history'):
        s[k] = sorted(s[k])
    s['best_window'] = {k: v[1] for k, v in best.items()}
    return s


def scan_iso(path, args):
    nav = IsoNav(path)
    rows = []
    for vn, ifo_lba in sorted(nav.vts_ifo.items()):
        mat = nav.sec(ifo_lba)
        audio = parse_vts_attr(mat)[2]
        if not any(a[0] == FMT_DTS for a in audio):
            continue
        last_vts = struct.unpack('>I', mat[0x0C:0x10])[0]
        last_ifo = struct.unpack('>I', mat[0x1C:0x20])[0]
        tt = struct.unpack('>I', mat[0xC4:0xC8])[0]
        if not tt:
            continue
        start = ifo_lba + tt
        end = ifo_lba + last_vts - (last_ifo + 1)     # the backup IFO sits at the end
        span = end - start
        if span < args.window:
            continue
        k = args.windows
        per_sub = {}
        for i in range(k):
            pos = start + (span - args.window) * i // max(k - 1, 1)
            for ssid, data in dts_payloads(nav.f, pos, args.window).items():
                per_sub.setdefault(ssid, [None] * k)[i] = data
        for ssid, wins in sorted(per_sub.items()):
            wins = [w or b'' for w in wins]
            s = scan_stream(wins)
            s.update({'image': os.path.basename(path), 'vts': vn, 'substream': ssid,
                      'declared': [a[1] for a in audio if a[0] == FMT_DTS]})
            rows.append(s)
            if args.extract:
                for feat, wi in s['best_window'].items():
                    name = f'{os.path.splitext(os.path.basename(path))[0]}_vts{vn:02d}_{ssid:02x}_{feat}.dts'
                    out = os.path.join(args.extract, name)
                    if feat in args.want and not os.path.exists(out):
                        os.makedirs(args.extract, exist_ok=True)
                        with open(out, 'wb') as fo:
                            fo.write(bytes(wins[wi]))
    nav.f.close()
    return rows


def pct(a, b):
    return f'{a * 100 // b:3d}%' if b else '   -'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('images', nargs='*')
    ap.add_argument('--windows', type=int, default=8,
                    help='windows sampled across each title set (default 8)')
    ap.add_argument('--window', type=int, default=500,
                    help='sectors per window (default 500, ~1 MB)')
    ap.add_argument('--json', help='write one JSON row per stream here')
    ap.add_argument('--extract', help='save covering windows as .dts files here')
    ap.add_argument('--want', default='huff,transient,joint_bands,sumdiff_front',
                    help='features to extract windows for (default: the gate\'s gaps)')
    args = ap.parse_args()
    args.want = set(args.want.split(','))
    images = args.images
    if not images:
        root = os.environ.get('DVD_ISO_DIR', os.path.expanduser('~/dvd-isos'))
        images = sorted(glob.glob(os.path.join(root, '**', '*.iso'), recursive=True) +
                        glob.glob(os.path.join(root, '**', '*.ISO'), recursive=True))
        print(f'dts_scan: {len(images)} images under {root}')
    out = open(args.json, 'w') if args.json else None
    allrows, nerr = [], 0
    for path in images:
        try:
            rows = scan_iso(path, args)
        except Exception as e:                       # a bad rip is not a crash
            print(f'ERR  {os.path.basename(path)}: {e}')
            nerr += 1
            continue
        for s in rows:
            allrows.append(s)
            fr = s['frames']
            print(f'{s["image"][:40]:40} VTS{s["vts"]:02d} 0x{s["substream"]:02x} '
                  f'{fr:5d} fr  amode {s["amode"]} lfe {s["lfe"]} '
                  f'{"/".join(str(b // 1000) for b in s["bit_rate"])}k '
                  f'npb {s["npcmblocks"]} ext {s["ext"] or "-"}  '
                  f'pred {pct(s["frames_pmode"], fr)} vq {pct(s["frames_vq"], fr)} '
                  f'huff {pct(s["frames_huff"], fr)} trans {pct(s["frames_transient"], fr)} '
                  f'joint {pct(s["frames_joint"], fr)} sumdiff {pct(s["sumdiff_front"], fr)} '
                  f'perfect {s["filter_perfect"]} dmix {s["aux_dmix"]}'
                  + (f'  ERR {s["errors"]}' if s['errors'] else ''))
            if out:
                out.write(json.dumps(s) + '\n')
                out.flush()
    if out:
        out.close()
    # library summary
    n = len(allrows)
    tot = sum(r['frames'] for r in allrows)

    def streams_with(key):
        return sum(1 for r in allrows if r[key])
    print(f'\n=== {n} DTS streams, {tot} frames parsed, {nerr} unreadable images ===')
    for key, label in (('frames_pmode', 'ADPCM prediction'), ('frames_vq', 'high-frequency VQ'),
                       ('frames_huff', 'Huffman sample codes'), ('frames_transient', 'transients'),
                       ('frames_joint', 'joint intensity'), ('sumdiff_front', 'front sum/diff'),
                       ('sumdiff_surround', 'surround sum/diff'), ('aux_dmix', 'embedded downmix'),
                       ('drc', 'dynamic range'), ('crc_present', 'header CRC')):
        print(f'  {label:22} {streams_with(key):4} streams, '
              f'{pct(sum(r[key] for r in allrows), tot)} of frames')
    for key in ('amode', 'lfe', 'bit_rate', 'npcmblocks', 'nsubframes', 'ext', 'filter_perfect'):
        vals = {}
        for r in allrows:
            for v in r[key]:
                vals[v] = vals.get(v, 0) + 1
        print(f'  {key:22} ' + ', '.join(f'{v}: {c}' for v, c in sorted(vals.items())))
    errs = {}
    for r in allrows:
        for k, v in r['errors'].items():
            errs[k] = errs.get(k, 0) + v
    print(f'  errors                 {errs or "none"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
