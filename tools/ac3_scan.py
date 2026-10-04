#!/usr/bin/env python3
"""ac3_scan.py -- what do the library's AC-3 tracks use, and where does the RTL
part from liba52?

docs/ac3_engine.md's census. For every title set declaring an AC-3 stream it
samples windows across the whole title VOBS (VTSI_MAT 0xC4 .. the title set's
end), rebuilds each AC-3 substream (private stream 1, 0x80-0x87) from the PES
first-access-unit pointer -- never a 0x0B77 search, which false-syncs inside
compressed payload (tools/acmod_scan.py) -- and decodes every frame with
tools/ac3_model.py TWICE: in the RTL's behaviour and with liba52's delta-bit-
allocation persistence. Per stream it records the coding features the blocks
used, the RTL's refusals, and the blocks whose coefficients differ between the
two (the DEVIATION in ac3_model.py, measured rather than argued).

Usage:
    tools/ac3_scan.py                       # every image under $DVD_ISO_DIR
    tools/ac3_scan.py <iso> [<iso> ...] [--jobs N] [--json out.jsonl]
    tools/ac3_scan.py --extract DIR         # save ~20-frame windows that cover
                                            # each feature, for the gate set
"""
import argparse
import glob
import json
import os
import struct
import sys
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac3_model as M                                           # noqa: E402
from dvd_vm_ref import IsoNav                                   # noqa: E402
from nav_extract import parse_vts_attr                          # noqa: E402

FMT_AC3 = 0                       # VTS audio attribute coding mode
WANT = ('deviation', 'deltba_new', 'deltba_reuse', 'deltbaie_off_after_new', 'dynrnge', 'short',
        'cpl', 'remat', 'dith', 'skip', 'phsflg', 'recomb_sat', 'remat_sat', 'cpl_ch0_uncoupled',
        'zero_snr')
# extracted by default: the rare ones, and any window where decoding stopped
RARE = ('deviation', 'deltba_new', 'deltba_reuse', 'deltbaie_off_after_new', 'short', 'phsflg',
        'recomb_sat', 'remat_sat', 'cpl_ch0_uncoupled', 'error')


def ac3_streams(f, start, n):
    """-> {substream_id: bytes} for sectors start..start+n: each substream's
    payload from the first access unit of its first PES onwards (so the bytes
    begin on a sync frame), or none if no PES in the window points at one."""
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
            end = min(2048, body + plen)
            if sid == 0xBD and body + 3 < 2048:
                sub = body + 3 + sec[body + 2]
                if sub + 4 <= end and 0x80 <= sec[sub] <= 0x87:
                    ssid, nfr = sec[sub], sec[sub + 1]
                    fap = struct.unpack('>H', sec[sub + 2:sub + 4])[0]
                    pay = sec[sub + 4:end]
                    if ssid in out:
                        out[ssid].extend(pay)
                    elif nfr and fap and fap - 1 < len(pay):
                        out[ssid] = bytearray(pay[fap - 1:])   # from the first access unit
            p = body + plen
    return out


def scan_stream(windows):
    """Decode each window's frames in both modes -> the stream's row."""
    row = dict(frames=0, acmods={}, lfe=0, rates={}, errors={}, deviation=0,
               best_window={}, stats=dict.fromkeys(M.STATS, 0))
    best = {}
    for wi, data in enumerate(windows):
        rtl, lib = M.Decoder(), M.Decoder(liba52_deltba=True)
        w_stats = dict.fromkeys(WANT, 0)
        w_first = {}                         # feature -> the first frame that used it
        prev = {}
        for fk, (_, fr) in enumerate(M.frames(bytes(data))):
            try:
                hdr, blocks = rtl.frame(fr)
            except M.Ac3Error as e:
                row['errors'][e.code] = row['errors'].get(e.code, 0) + 1
                row.setdefault('error_detail', str(e))
                row.setdefault('error_window', wi)
                row.setdefault('error_frame', fk)
                break                                   # the RTL halts at a refusal
            except Exception as e:                      # a model defect: record, go on
                row['errors']['CRASH'] = row['errors'].get('CRASH', 0) + 1
                row.setdefault('error_detail', repr(e))
                row.setdefault('error_window', wi)
                row.setdefault('error_frame', fk)
                break
            try:
                _, lblocks = lib.frame(fr)
            except M.Ac3Error:
                lblocks = None
            for k in RARE:
                if k in rtl.stats and rtl.stats[k] > prev.get(k, 0) and k not in w_first:
                    w_first[k] = fk
            prev = dict(rtl.stats)
            row['frames'] += 1
            row['acmods'][hdr['acmod']] = row['acmods'].get(hdr['acmod'], 0) + 1
            row['lfe'] |= hdr['lfeon']
            r = hdr['frame_bytes'] * 8 * 48000 // 1536 // 1000
            row['rates'][r] = row['rates'].get(r, 0) + 1
            if lblocks is not None:
                d = sum(1 for a, b in zip(blocks, lblocks) if a['coeff'] != b['coeff'])
                row['deviation'] += d
                w_stats['deviation'] += d
        for k, v in rtl.stats.items():
            row['stats'][k] += v
            if k in w_stats:
                w_stats[k] += v
        for k, v in w_stats.items():
            if v and v > best.get(k, (0, -1))[0]:
                best[k] = (v, wi)
                if k in w_first:
                    row.setdefault('first_frame', {})[k] = w_first[k]
    row['best_window'] = {k: wi for k, (_, wi) in best.items()}
    if 'error_window' in row:
        row['best_window']['error'] = row['error_window']
    return row


def scan_iso(args_path):
    path, args = args_path
    try:
        nav = IsoNav(path)
    except Exception as e:                              # not an ISO9660 DVD-Video image
        return [], f'{os.path.basename(path)}: {e}'
    rows = []
    try:
        for vn, ifo_lba in sorted(nav.vts_ifo.items()):
            mat = nav.sec(ifo_lba)
            audio = parse_vts_attr(mat)[2]
            if not any(a[0] == FMT_AC3 for a in audio):
                continue
            last_vts = struct.unpack('>I', mat[0x0C:0x10])[0]
            last_ifo = struct.unpack('>I', mat[0x1C:0x20])[0]
            tt = struct.unpack('>I', mat[0xC4:0xC8])[0]
            if not tt:
                continue
            start = ifo_lba + tt
            end = ifo_lba + last_vts - (last_ifo + 1)
            span = end - start
            if span < args.window:
                continue
            k = args.windows
            per_sub = {}
            for i in range(k):
                pos = start + (span - args.window) * i // max(k - 1, 1)
                for ssid, data in ac3_streams(nav.f, pos, args.window).items():
                    per_sub.setdefault(ssid, [b''] * k)[i] = data
            for ssid, wins in sorted(per_sub.items()):
                s = scan_stream(wins)
                s.update(image=os.path.basename(path), vts=vn, substream=ssid)
                rows.append(s)
                if args.extract:
                    stem = os.path.splitext(os.path.basename(path))[0]
                    for feat, wi in s['best_window'].items():
                        if feat not in args.want:
                            continue
                        out = os.path.join(args.extract, f'{stem}_vts{vn:02d}_{ssid:02x}_{feat}.ac3')
                        if not os.path.exists(out):
                            os.makedirs(args.extract, exist_ok=True)
                            fr = list(M.frames(bytes(wins[wi])))
                            if feat == 'error':             # the frames AROUND the stop
                                ef = s['error_frame']
                                fr = fr[max(0, ef - 3):ef + 2]
                            elif feat in s.get('first_frame', {}):   # around a rare event
                                ef = s['first_frame'][feat]
                                stop = s.get('error_frame') if s.get('error_window') == wi else None
                                fr = fr[max(0, ef - 3):(stop if stop is not None else len(fr))]
                            fr = fr[:args.extract_frames]
                            with open(out, 'wb') as fo:
                                fo.write(b''.join(f for _, f in fr))
    finally:
        nav.f.close()
    return rows, None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('images', nargs='*')
    ap.add_argument('--windows', type=int, default=6, help='windows a title set (default 6)')
    ap.add_argument('--window', type=int, default=200, help='sectors a window (default 200)')
    ap.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument('--json')
    ap.add_argument('--extract')
    ap.add_argument('--extract-frames', type=int, default=20)
    ap.add_argument('--want', default=','.join(RARE),
                    help='features to extract windows for (default: the rare ones + errors)')
    a = ap.parse_args()
    a.want = set(a.want.split(','))
    images = a.images
    if not images:
        root = os.environ.get('DVD_ISO_DIR')
        if not root:
            ap.error('no images: pass them or set DVD_ISO_DIR')
        images = sorted(glob.glob(os.path.join(root, '**', '*.[iI][sS][oO]'), recursive=True))
    rows, skipped = [], []
    with Pool(a.jobs) as pool:
        for r, err in pool.imap_unordered(scan_iso, [(p, a) for p in images]):
            rows.extend(r)
            if err:
                skipped.append(err)
    if a.json:
        with open(a.json, 'w') as f:
            for r in rows:
                f.write(json.dumps(r) + '\n')
    tot = dict.fromkeys(M.STATS, 0)
    frames = blocks_dev = 0
    with_feat = dict.fromkeys(M.STATS, 0)
    errors = {}
    acmods = {}
    for r in rows:
        frames += r['frames']
        blocks_dev += r['deviation']
        for k, v in r['stats'].items():
            tot[k] += v
            with_feat[k] += bool(v)
        for k, v in r['errors'].items():
            errors[k] = errors.get(k, 0) + v
        for k, v in r['acmods'].items():
            acmods[int(k)] = acmods.get(int(k), 0) + 1
    print(f'ac3_scan: {len(images)} images ({len(skipped)} unreadable), {len(rows)} AC-3 streams, '
          f'{frames} frames, {tot["blocks"]} blocks')
    print(f'  acmod (streams): {dict(sorted(acmods.items()))}')
    for k in M.STATS[1:]:
        print(f'  {k:24s} {with_feat[k]:5d} streams, {tot[k]:7d} blocks')
    dev_streams = sum(1 for r in rows if r['deviation'])
    print(f'  DEVIATION (RTL vs liba52 delta-BA): {dev_streams} streams, {blocks_dev} blocks')
    print(f'  refusals: {errors or "none"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
