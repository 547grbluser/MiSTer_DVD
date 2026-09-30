#!/usr/bin/env python3
"""check_rf_flush_wiring.py -- the audio reframers forget their in-flight frame on a
hard audio flush.

WHY THIS IS A SCRIPT AND NOT (ONLY) A BENCH (2026-09-30)
--------------------------------------------------------
After about one backward D-pad seek in six on Men in Black, the picture held ~1.2 s and
audio stayed silent ~2.5 s. ac3_reframer emits a frame start (stamped with the pending
PES PTS) on the 0x77 of its sync; dts_reframer holds that start in its 4-byte pipeline
until four more audio bytes arrive. The reframers reset on the core reset and a track
switch only, so a seek flush that lands while a start sits in that pipeline keeps it,
and the fresh ring commits it as its first frame, carrying the OLD position's PTS.
dvd_audio_decode latches it as play_pts and, after a backward seek, never releases
before the ~2.5 s fallback (docs/dvd_nav.md §2h "The stale audio PTS").

The fix is one term in emu.sv's reset expression:

    wire rf_rst_n = reset_n & ~aud_realign_q;                     // pre-fix
    wire rf_rst_n = reset_n & ~aud_realign_q & ~rf_flush_q;       // fixed
    (rf_flush_q <= aud_flush, registered)

The second half: with the reframers reset, ac3_reframer is unlocked and would take a
stray 0B77 in the landing's leading partial frame as the first frame (a click). emu
therefore also hands ps_demux ONE aud_realign cycle as it leaves the flush's reset
(dmx_rlgn_go: a latch set by aud_flush, cleared once pipe_rst_n releases), so the demux
starts the landing at its first_access_unit_pointer -- the track-switch path.

bench/dvd/seek_rf_pts_tb.sv proves the chain behaves with that wiring REBUILT in the
bench; it cannot see emu.sv, which has no bench at all. This reads the connection out
of dvd/emu.sv (the check_spdif_bs_hold_wiring.py pattern).

It refuses the tempting wrong versions too:
  - keyed to aud_resync / aud_rst_n: a track switch's gentle half and #141's in-band
    re-time keep a CONTINUOUS byte stream, where a mid-frame reset only drops bytes into
    a live ring; and a seek pulses aud_flush, not aud_resync (the bench's +RESYNC arm).
  - keyed to pipe_rst_n / load_flush: that also fires on a keep_vbuf menu hop, where the
    ring is preserved -- the "static pop" the old reset_n-only rule existed to prevent.
  - aud_flush used raw: it is a counter compare driving async resets; register it.
  - rf_rst_n defined ABOVE aud_flush's declaration: emu.sv has no `default_nettype
    none`, so a forward reference can become a 1-bit implicit net.

Traps written against: strip_comments() FIRST (the comment above rf_rst_n quotes the
pre-fix line); token sets, never substrings. A lookup that finds nothing, or two
drivers, is a NAMED FAIL, never a skip.

Exit 0 = wired as designed; 1 = a named failure. Optional argv[1] = a file to check
instead of dvd/emu.sv, so a runner can mutate a copy in $TMP and never touch the tree.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REFRAMERS = ('ac3_reframer', 'dts_reframer', 'mp2_reframer')
FORBIDDEN = {'pipe_rst_n', 'load_flush', 'aud_resync', 'aud_rst_n', 'aud_flush'}


def strip_comments(src):
    """Blank out // and /* */ comments, preserving offsets and newlines."""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '/' and i + 1 < n and src[i + 1] == '/':
            j = src.find('\n', i)
            j = n if j < 0 else j
            out.append(' ' * (j - i))
            i = j
        elif c == '/' and i + 1 < n and src[i + 1] == '*':
            j = src.find('*/', i + 2)
            j = n if j < 0 else j + 2
            out.append(''.join(ch if ch == '\n' else ' ' for ch in src[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def terms(expr):
    return set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', expr))


def negated(expr):
    """Identifiers that appear as ~name in expr."""
    return set(re.findall(r'~\s*([A-Za-z_][A-Za-z0-9_]*)', expr))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'dvd', 'emu.sv')
    src = strip_comments(open(path).read())
    fails = []

    # 1. exactly one driver of rf_rst_n
    defs = list(re.finditer(r'\bwire\s+rf_rst_n\s*=\s*([^;]*);', src))
    defs += list(re.finditer(r'\bassign\s+rf_rst_n\s*=\s*([^;]*);', src))
    if len(defs) != 1:
        fails.append(f'rf_rst_n: expected exactly one driver, found {len(defs)}')
        return report(fails, None)
    d = defs[0]
    expr = re.sub(r'\s+', ' ', d.group(1)).strip()
    t, neg = terms(expr), negated(expr)

    if 'reset_n' not in t:
        fails.append(f'rf_rst_n lost the core reset: `{expr}`')
    if 'aud_realign_q' not in neg:
        fails.append(f'rf_rst_n lost the track-switch reset (~aud_realign_q): `{expr}`')
    bad = t & FORBIDDEN
    if bad:
        fails.append(f'rf_rst_n uses {sorted(bad)} directly -- the hard flush must come '
                     f'through a REGISTERED copy of aud_flush, and never pipe_rst_n / '
                     f'load_flush (keep_vbuf hops) or aud_resync / aud_rst_n (continuous '
                     f'streams): `{expr}`')

    # 2. one of the negated terms is a register loaded from aud_flush and nothing else
    flush_regs = []
    for name in sorted(neg - {'aud_realign_q'}):
        loads = re.findall(r'\b' + re.escape(name) + r'\s*<=\s*([^;]*);', src)
        rhs = {re.sub(r'\s+', ' ', r).strip() for r in loads}
        rhs.discard("1'b0")
        if rhs == {'aud_flush'}:
            flush_regs.append(name)
        elif 'aud_flush' in {x for r in rhs for x in terms(r)}:
            fails.append(f'{name} is loaded from `{sorted(rhs)}` -- it must be a plain '
                         f'registered copy of aud_flush')
    if not flush_regs:
        fails.append('rf_rst_n has no ~<reg> term whose register is loaded from aud_flush '
                     '-- a seek flush leaves a frame start (and its OLD PTS) in the '
                     f'reframer pipeline (docs/dvd_nav.md §2h): `{expr}`')

    # 3. rf_rst_n is defined AFTER aud_flush is declared (no implicit-net forward ref)
    decl = re.search(r'\bwire\b[^;]*\baud_flush\b[^;]*;', src)
    if not decl:
        fails.append('aud_flush: no wire declaration found')
    elif decl.start() > d.start():
        fails.append('rf_rst_n is defined above the declaration of aud_flush -- emu.sv '
                     'has no `default_nettype none`, so the forward reference can become '
                     'an implicit 1-bit net')
    for r in flush_regs:
        rdecl = re.search(r'\breg\b[^;]*\b' + re.escape(r) + r'\b[^;]*;', src)
        if not rdecl or rdecl.start() > d.start():
            fails.append(f'{r} is not declared as a reg ahead of rf_rst_n')

    # 4. flush_ctl drives aud_flush
    fc = re.search(r'\bflush_ctl\s+\w+\s*\((.*?)\);', src, re.S)
    if not fc:
        fails.append('flush_ctl instance not found')
    elif not re.search(r'\.aud_flush\s*\(\s*aud_flush\s*\)', fc.group(1)):
        fails.append('flush_ctl .aud_flush is not connected to aud_flush')

    # 5. every reframer takes rf_rst_n
    for mod in REFRAMERS:
        inst = re.findall(r'\b' + mod + r'\s+\w+\s*\((.*?)\);', src, re.S)
        if len(inst) != 1:
            fails.append(f'{mod}: expected exactly one instance, found {len(inst)}')
            continue
        m = re.search(r'\.rst_n\s*\(\s*([^)]*?)\s*\)', inst[0])
        if not m or m.group(1) != 'rf_rst_n':
            fails.append(f'{mod}.rst_n is `{m.group(1) if m else "?"}`, not rf_rst_n -- '
                         f'its pipeline would carry a frame start across a seek')

    # 6. the demux starts the landing's audio on a real frame: ps_demux.aud_realign
    #    carries, beside aud_switch, a pulse G = <latch> [& pipe_rst_n], where the
    #    latch is SET by aud_flush and CLEARED once pipe_rst_n releases -- so the demux
    #    sees one realign cycle as it leaves the flush's reset (docs/dvd_nav.md §2h).
    dm = re.findall(r'\bps_demux\s+\w+\s*\((.*?)\);', src, re.S)
    if len(dm) != 1:
        fails.append(f'ps_demux: expected exactly one instance, found {len(dm)}')
    else:
        m = re.search(r'\.aud_realign\s*\(([^)]*)\)', dm[0])
        rl = re.sub(r'\s+', ' ', m.group(1)).strip() if m else ''
        rt = terms(rl)
        if 'aud_switch' not in rt:
            fails.append(f'ps_demux.aud_realign lost the track switch: `{rl}`')
        armed = False
        for g in sorted(rt - {'aud_switch'}):
            gd = re.findall(r'\bwire\s+' + re.escape(g) + r'\s*=\s*([^;]*);', src)
            if len(gd) != 1:
                continue
            for a in sorted(terms(gd[0]) - {'pipe_rst_n'}):
                sets = re.search(r'\bif\s*\(\s*aud_flush\s*\)\s*' + re.escape(a) +
                                 r"\s*<=\s*1'b1\s*;", src)
                clrs = re.search(r'\bif\s*\(\s*pipe_rst_n\s*\)\s*' + re.escape(a) +
                                 r"\s*<=\s*1'b0\s*;", src)
                if sets and clrs:
                    armed = True
                    gpos = src.find('wire ' + g)
                    if decl and gpos < decl.start():
                        fails.append(f'{g} is defined above the declaration of aud_flush')
        if not armed:
            fails.append('ps_demux.aud_realign is not armed by a hard flush (a latch set '
                         'by aud_flush and cleared when pipe_rst_n releases) -- the '
                         'unlocked reframer takes a stray 0B77 in the landing\'s partial '
                         f'frame as the first frame: `{rl}`')

    return report(fails, expr)


def report(fails, expr):
    if fails:
        for f in fails:
            print(f'FAIL: {f}')
        return 1
    print(f'OK: the reframers reset, and the demux realigns, on a hard audio flush '
          f'(rf_rst_n = {expr})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
