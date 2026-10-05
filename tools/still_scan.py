#!/usr/bin/env python3
"""still_scan.py -- find menu-domain STILLS WITH NO BUTTONS: the cells a user Still off
(Play/Pause or Select, docs/dvd_nav.md "Still off") acts on.

For the VMGM and every VTSM (language unit 0), lists each cell that carries a still time
(1..254 s, or 255 = indefinite) and no cell command, and whose NAV pack at the cell's last
VOBU has no buttons (hli_ss == 0 or btn_ns == 0). Those are the vehicles for a HIL run of
the feature: a warning card, a dead-end screen, a narrated still.

    tools/still_scan.py <iso>...

One line per hit:
    <iso> <VMGM|VTSMn> pgc=N eid=0xNN cell=N still=S last=0|1 btn_ns=N ss=N

Limits: menu domains only (title stills are not scanned); the libdvdnav playback-time
HEURISTIC stills (still_time 0) are not listed, because their hold is decided at play time;
whether a cell is ever REACHED is not checked -- run tools/bin/trace_nav on the disc for
that (it prints "[skip Ns still]" for each timed still on its boot path).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nav_extract as ne   # noqa: E402

be16, be32 = ne.be16, ne.be32


def pgcs_of_ut(f, ifo_lba, ut_ptr_off):
    """(pgcn, entry_id, absolute byte of the PGC) for language unit 0 of a PGCI_UT."""
    f.seek(ifo_lba * 2048)
    mat = f.read(2048)
    ut = be32(mat, ut_ptr_off)
    if not ut:
        return []
    ut_abs = (ifo_lba + ut) * 2048
    f.seek(ut_abs)
    if be16(f.read(8), 0) == 0:
        return []
    f.seek(ut_abs + 8)
    start = be32(f.read(8), 4)
    f.seek(ut_abs + start)
    pit = f.read(8)
    out = []
    for j in range(min(be16(pit, 0), 999)):
        f.seek(ut_abs + start + 8 + j * 8)
        srp = f.read(8)
        out.append((j + 1, srp[0], ut_abs + start + be32(srp, 4)))
    return out


def cells(f, pgc_abs):
    """[(index, still_time, cell_cmd_nr, first_sector, last_vobu_start)] of one PGC."""
    f.seek(pgc_abs)
    p = f.read(236)
    nc, cpo = p[3], be16(p, 232)
    if not nc or not cpo:
        return []
    f.seek(pgc_abs + cpo)
    cb = f.read(nc * 24)
    return [(i, cb[i * 24 + 2], cb[i * 24 + 3], be32(cb, i * 24 + 8), be32(cb, i * 24 + 16))
            for i in range(nc) if len(cb) >= (i + 1) * 24]


def nav_btns(f, vob_lba, rbn):
    """(hli_ss, btn_ns) of the NAV pack at a menu-VOB RBN, or None if it is not one."""
    f.seek((vob_lba + rbn) * 2048)
    s = f.read(2048)
    if not ne.is_nav_pack(s):
        return None
    pci = s[0x2D:]
    return be16(pci, 0x60) & 3, pci[0x71] & 0x3F


def scan(iso):
    with open(iso, 'rb') as f:
        doms = [(0, ne.find_vmg_ifo(f), 200)]
        for v in range(1, 100):          # spec max: 99 title sets
            try:
                doms.append((v, ne.find_vts_ifo(f, v), 208))
            except SystemExit:
                break
        for v, ifo, off in doms:
            try:
                vob, _ = ne.find_menu_vob(f, v)
            except SystemExit:
                continue
            for pgcn, eid, pa in pgcs_of_ut(f, ifo, off):
                cl = cells(f, pa)
                for i, st, cmd, _first, lvs in cl:
                    if st == 0 or cmd != 0:
                        continue
                    nb = nav_btns(f, vob, lvs)
                    if nb is None:
                        continue
                    ss, ns = nb
                    if ss == 0 or ns == 0:
                        print('%s %s pgc=%d eid=0x%02x cell=%d still=%d last=%d btn_ns=%d ss=%d'
                              % (os.path.basename(iso), 'VMGM' if v == 0 else 'VTSM%d' % v,
                                 pgcn, eid, i + 1, st, i + 1 == len(cl), ns, ss), flush=True)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    rc = 0
    for iso in sys.argv[1:]:
        try:
            scan(iso)
        except Exception as e:           # a damaged image must not stop a sweep
            print('%s ERR %s' % (os.path.basename(iso), e), file=sys.stderr)
            rc = 1
    return rc


if __name__ == '__main__':
    sys.exit(main())
