#!/usr/bin/env python3
"""colour_scan.py -- which colour matrix does each disc's video signal?

ISO 13818-2 carries the YCbCr->RGB matrix in sequence_display_extension's
colour description (matrix_coefficients, par. 6.3.6). DVD-Video permits only
5 (BT.470 B/G) and 6 (SMPTE 170M), both the BT.601 matrix (DVD Demystified 3rd
ed., Table 9.18). A stream may also say nothing: no display extension at all,
or one with colour_description=0. What the decoder does with "nothing" is the
decision this census exists to size: rtl/mpeg2/yuv2rgb.v decoded it as BT.709
(the ISO default) until 2026-10, and now decodes it as BT.601
(docs/status_log.md "BT.601 default colour matrix").

Each sequence header is classified by what follows it before the next GOP /
picture / sequence start code:

    none        no sequence_display_extension
    cd=0        a display extension with colour_description=0
    cpP/tcT/mcM an explicit colour description

MEASURED 2026-10-05 over the local library (1,530 images; 26 unreadable):
  * 1,131 discs have untagged ("none") title sequences, 116 more "cd=0".
    By sequence-header count "none" is the largest bucket (15,023 title headers
    vs 8,269 tagged 6).
  * --feature on 39 random discs: 24 main features untagged, 14 tagged 6, 1
    tagged 5 -- i.e. ~60% of features played with the wrong matrix.
  * Tags seen: 6, 5 and 4 (FCC, ~36 discs; within 1% of 601). NO disc is tagged
    1 (BT.709) or 7 (SMPTE 240M), which is why honouring an explicit tag costs
    nothing on DVD.
  * ~8 discs (e.g. Finding Nemo, Signs, AVIATOR disc 2) carry a plausible
    colour_primaries but OUT-OF-RANGE transfer/matrix bytes (17..219) in some
    sequences. Unexplained: their PES scrambling bits are clear, and the bytes
    are present in the stream, not an artefact of this scanner's splicing.
    Treat them as data to investigate, not as a measurement. yuv2rgb folds
    8..255 to 0, so they decode as 601 either way.

Scope, stated honestly (as tools/qmatrix_scan.py does): by default it samples
the FIRST --sectors of every menu VOB and of the first part of every title VOB.
A title VOB's head is often a logo or warning card rather than the feature, so
--feature instead samples --windows windows of --win sectors spread across the
main VTS (IsoNav.best_vts), skipping the first and last 3% the way
tools/video_cadence_census.py does.

Usage:
    tools/colour_scan.py                       # the whole library ($DVD_ISO_DIR)
    tools/colour_scan.py <iso> [<iso> ...]
    tools/colour_scan.py --feature             # sample across each main feature
    tools/colour_scan.py --first-hit --mc 6    # print one disc whose feature says mc 6
    tools/colour_scan.py --json out.json
"""
import argparse
import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dvd_vm_ref import IsoNav                                   # noqa: E402
from video_cadence_census import video_payload                  # noqa: E402

SEC = 2048


def classify(es):
    """One (width, height, is_mpeg2, description) per sequence header in es."""
    out = []
    n = len(es)
    i = 0
    while True:
        i = es.find(b'\x00\x00\x01\xb3', i)
        if i < 0 or i + 12 > n:
            break
        w = (es[i + 4] << 4) | (es[i + 5] >> 4)
        h = ((es[i + 5] & 0xf) << 8) | es[i + 6]
        mpeg2 = False
        desc = 'none'
        j = i + 4
        while True:
            j = es.find(b'\x00\x00\x01', j)
            if j < 0 or j + 8 > n:
                break
            sc = es[j + 3]
            if sc == 0xb5:
                eid = es[j + 4] >> 4
                if eid == 1:
                    mpeg2 = True
                elif eid == 2:
                    if es[j + 4] & 1:
                        desc = 'cp%d/tc%d/mc%d' % (es[j + 5], es[j + 6], es[j + 7])
                    else:
                        desc = 'cd=0'
            elif sc in (0xb8, 0x00, 0xb3, 0xb7):   # GOP, picture, next sequence, end
                break
            j += 4
        out.append((w, h, mpeg2, desc))
        i += 4
    return out


def read_es(nav, lba, nsec):
    return b''.join(video_payload(nav.sec(lba + k)) for k in range(nsec))


def scan_heads(nav, nsec):
    vobs = [('menu', e, d) for _, (e, d) in sorted(nav.menu_vob.items())]
    for _, parts in sorted(nav.groups.items()):
        if parts:
            vobs.append(('title', parts[0][0], parts[0][1]))
    res = collections.Counter()
    for kind, ext, dl in vobs:
        for (w, h, m2, desc) in classify(read_es(nav, ext, min(nsec, max(1, dl // SEC)))):
            res[(kind, '%dx%d' % (w, h), 'mpeg2' if m2 else 'mpeg1', desc)] += 1
    return res


def scan_feature(nav, windows, win):
    vts = nav.best_vts
    if vts is None or vts not in nav.groups:
        raise ValueError('no title VOBs')
    runs = [(e, d // SEC) for e, d in nav.groups[vts]]
    total = sum(n for _, n in runs)

    def lba_at(idx):
        for e, n in runs:
            if idx < n:
                return e + idx
            idx -= n
        return None

    lo, hi = int(total * 0.03), int(total * 0.97)
    res = collections.Counter()
    for k in range(windows):
        start = lo + (hi - lo) * k // windows
        lbas = [lba_at(start + s) for s in range(win)]
        es = b''.join(video_payload(nav.sec(l)) for l in lbas if l is not None)
        for (w, h, m2, desc) in classify(es):
            res[('feature', '%dx%d' % (w, h), 'mpeg2' if m2 else 'mpeg1', desc)] += 1
    return res


def scan_iso(path, args):
    nav = IsoNav(path)
    try:
        if args.feature:
            return scan_feature(nav, args.windows, args.win)
        return scan_heads(nav, args.sectors)
    finally:
        nav.f.close()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('images', nargs='*')
    ap.add_argument('--sectors', type=int, default=64,
                    help='sectors sampled at the head of each VOB (default 64)')
    ap.add_argument('--feature', action='store_true',
                    help='sample windows across the main VTS instead of VOB heads')
    ap.add_argument('--windows', type=int, default=8, help='--feature windows (default 8)')
    ap.add_argument('--win', type=int, default=120, help='--feature sectors per window (default 120)')
    ap.add_argument('--first-hit', action='store_true',
                    help='print the first disc whose sampled video carries --mc and stop')
    ap.add_argument('--mc', default=None,
                    help="--first-hit: a matrix_coefficients value, or 'none' / 'cd=0'")
    ap.add_argument('--json', metavar='FILE')
    args = ap.parse_args()

    images = args.images
    if not images:
        root = os.environ.get('DVD_ISO_DIR', os.path.expanduser('~/dvd-isos'))
        images = sorted(glob.glob(os.path.join(root, '**', '*.iso'), recursive=True) +
                        glob.glob(os.path.join(root, '**', '*.ISO'), recursive=True))
        if not args.first_hit:
            print('colour_scan: %d images under %s' % (len(images), root))

    want = None
    if args.mc is not None:
        want = args.mc if args.mc in ('none', 'cd=0') else '/mc%d' % int(args.mc)

    per_disc = collections.Counter()
    totals = collections.Counter()
    out = {}
    n_err = 0
    for path in images:
        name = os.path.basename(path)
        try:
            res = scan_iso(path, args)
        except Exception as e:                          # noqa: BLE001
            n_err += 1
            if not args.first_hit:
                print('  ERR  %s: %s' % (name, e))
            continue
        doms = collections.defaultdict(set)
        for (kind, _, _, desc) in res:
            doms[kind].add(desc)
        if args.first_hit:
            vid = doms.get('feature', set()) | doms.get('title', set())
            if want and any(d == want or d.endswith(want) for d in vid):
                print(path)
                return 0
            continue
        for kind, descs in doms.items():
            for d in descs:
                per_disc[(kind, d)] += 1
        totals.update(res)
        out[name] = {k: sorted(v) for k, v in doms.items()}
        print('  %-52s %s' % (name, '  '.join('%s=%s' % (k, ','.join(sorted(v)))
                                                for k, v in sorted(doms.items()))))

    if args.first_hit:
        print('colour_scan: no disc carries %s' % args.mc, file=sys.stderr)
        return 1

    print('\ncolour_scan: discs carrying each description, per domain (%d unreadable)' % n_err)
    for (kind, d), v in per_disc.most_common():
        print('  %5d  %-8s %s' % (v, kind, d))
    print('\ncolour_scan: sequence headers by (domain, size, standard, description)')
    for k, v in totals.most_common(30):
        print('  %6d  %s' % (v, ' '.join(k)))
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(out, f, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
