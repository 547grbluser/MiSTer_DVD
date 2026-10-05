#!/usr/bin/env python3
"""chap_edge_census.py -- how often a chapter skip at a title's edge has
somewhere authored to go (audit item 7, docs/dvd_nav.md "Chapter skip at the
title's edges").

Per title (one TT_SRPT entry; every VTS, not just the main feature):
  * the PGC holding its LAST chapter (PTT) -- Next from there runs that PGC's
    POST (libdvdnav vm_jump_next_pg), so: does it have POST commands, and what
    does the block do when run with default GPRMs?
  * the PGC holding its FIRST chapter -- Prev at its start follows prev_pgc_nr
    (vm_jump_prev_pg), so: is it set, and does it name this PGC itself?

Reuses the project's parsers rather than a new one: IsoNav + eval_block from
dvd_vm_ref.py, read_ptt_table from ptt_ref.py (IsoNav provides the sec()/rd()
methods ptt_ref's Disc does, so it is passed straight in).

⚠ "default GPRMs" is a STATIC reading: a real POST branches on GPRMs the menus
set ("Play All" flags), so the outcome column says what the block does from a
cold start, not on every playback. Civil War 2's title 1 reads CallSS here and
went to VTS 2 on the board with the menus' GPRMs.

    tools/chap_edge_census.py <iso|dir> [...]            # report
    tools/chap_edge_census.py --json out.json <dir> ...  # also dump per title
Directories are walked recursively (the library is subdivided).
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dvd_vm_ref import (IsoNav, DOM_TT, _VMCmd, jump_instruction,  # noqa: E402
                        link_instruction, link_subins, eval_block, Regs, Lfsr)
from ptt_ref import read_ptt_table                                # noqa: E402


def links_in(cmds):
    """Every link/jump a block COULD take (each command's condition forced true)."""
    out = []
    for b in cmds:
        c = _VMCmd(b)
        t = c.bits(63, 3)
        lk = None
        if t == 1:
            lk = jump_instruction(c, True) if c.bits(60, 1) else link_instruction(c, True)
        elif t in (2, 3) and c.bits(51, 4):
            lk = link_instruction(c, True)
        elif t in (4, 5, 6):
            lk = link_subins(c, True)
        if lk:
            out.append(lk[0])
    return out


def scan(path):
    nav = IsoNav(path)
    cache = {}

    def pgc(vts, pgcn):
        if (vts, pgcn) not in cache:
            pit = nav.pgcit(DOM_TT, vts)
            cache[(vts, pgcn)] = (nav.pgc(pit[pgcn - 1][1])
                                  if pit and 1 <= pgcn <= len(pit) else None)
        return cache[(vts, pgcn)]

    titles, seen = [], set()
    for gt, (vts, ttn) in sorted(nav.tt.items()):
        if (vts, ttn) in seen or vts not in nav.vts_ifo:
            continue
        seen.add((vts, ttn))
        n, ptts = read_ptt_table(nav, nav.vts_ifo[vts], ttn)
        if not ptts:
            continue
        (lp, lg), (fp, _) = ptts[-1], ptts[0]
        pl, pf = pgc(vts, lp), pgc(vts, fp)
        if pl is None or pf is None:
            continue
        try:
            out = eval_block(pl["post"], Regs(), Lfsr())
        except Exception:                      # malformed block: say so, go on
            out = ("ERR",)
        titles.append(dict(
            gt=gt, vts=vts, ttn=ttn, nptt=n, npgc=len(set(p for p, _ in ptts)),
            last_pgcn=lp, last_pgn=lg, post_n=len(pl["post"]),
            post_links=links_in(pl["post"]), post_eval=out[0] if out else None,
            next=pl["next"], first_pgcn=fp, prev=pf["prev"],
            prev_npg=(pgc(vts, pf["prev"]) or {}).get("nr_pgms") if pf["prev"] else None,
            dur=sum(c["pbtime"] for c in pl["cells"]) if lp == fp else None))
    return titles


def gather(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for dp, _, fs in os.walk(p):
                out += [os.path.join(dp, f) for f in fs if f.lower().endswith('.iso')]
        else:
            out.append(p)
    return sorted(out)


def report(res):
    ok = {k: v for k, v in res.items() if isinstance(v, list)}
    T = [t for v in ok.values() for t in v]
    print("images: %d scanned, %d parsed, %d failed the IFO parse"
          % (len(res), len(ok), len(res) - len(ok)))
    if not T:
        return
    post = [t for t in T if t['post_n']]
    print("titles: %d" % len(T))
    print("\nNEXT from the last chapter (POST):")
    print("  last chapter's PGC has POST: %d (%.1f%%)" % (len(post), 100.0 * len(post) / len(T)))
    for k, n in collections.Counter(t['post_eval'] for t in post).most_common():
        print("    default-GPRM outcome %-18s %d" % (k, n))
    print("  no POST but next_pgc_nr set: %d" % sum(1 for t in T if not t['post_n'] and t['next']))
    mf = [max(v, key=lambda t: t['dur'] or 0) for v in ok.values() if v]
    print("  main features (longest single-PGC title per image): %d, with POST %d"
          % (len(mf), sum(1 for t in mf if t['post_n'])))
    for k, n in collections.Counter(t['post_eval'] for t in mf if t['post_n']).most_common(6):
        print("    %-18s %d" % (k, n))
    pv = [t for t in T if t['prev']]
    self_ = [t for t in pv if t['prev'] == t['first_pgcn']]
    print("\nPREV at chapter 1 (prev_pgc_nr):")
    print("  set: %d titles on %d images (%d main features)"
          % (len(pv), sum(1 for v in ok.values() if any(t['prev'] for t in v)),
             sum(1 for t in mf if t['prev'])))
    print("  names its own PGC: %d (%d of them multi-program -- the core restarts these)"
          % (len(self_), sum(1 for t in self_ if (t['prev_npg'] or 0) > 1)))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--json')
    a = ap.parse_args()
    res = {}
    for p in gather(a.paths):
        try:
            res[os.path.relpath(p)] = scan(p)
        except Exception as e:                 # an unreadable IFO is a finding
            res[os.path.relpath(p)] = repr(e)
    report(res)
    if a.json:
        json.dump(res, open(a.json, 'w'), indent=1)


if __name__ == '__main__':
    main()
