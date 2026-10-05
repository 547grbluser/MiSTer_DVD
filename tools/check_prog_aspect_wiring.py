#!/usr/bin/env python3
"""check_prog_aspect_wiring.py -- an EXPLICIT Analog Aspect Letterbox/Crop takes effect on
the Progressive raster; Auto, Fit and the whole interlaced raster do not change
(feature/progressive-aspect, docs/crt_anamorphic.md §13).

WHY THIS IS A SCRIPT AND NOT A BENCH
------------------------------------
dvd/emu.sv has no bench, and a module bench is HANDED the enables -- it cannot see a wrong
gate term in emu's resolve. So, like tools/check_menu_panscan_wiring.py (whose evaluator
this imports rather than copies), this reads the resolve out of dvd/emu.sv and EVALUATES
it over the whole input space.

The contract, each a named check:
  [1] INTERLACED IS BIT-IDENTICAL. With interlaced_eff = 1, (analog_letterbox, analog_crop)
      equals the resolve as it stood on main @ 4da0adc (PR #154), menu permitted_df swap
      included -- frozen below as `pre_branch()`, never re-derived from the file.
  [2] AUTO AND FIT DO NOT CORRECT ON PROGRESSIVE (user decision 2026-10-05). With
      interlaced_eff = 0 and Analog Aspect Auto (0) or Fit (1), both enables are 0 for
      every other input -- whatever the stream or menu aspect.
  [3] AN EXPLICIT LETTERBOX/CROP ON PROGRESSIVE resolves exactly as the interlaced raster
      does, the 16:9 menu swap included (user decision 2026-10-04).
  [4] SIF-HEIGHT CONTENT IS NEVER CORRECTED on either raster (crt_ov_map's bar literals
      assume a full-height picture).
  [5] aa_live -- the one "Analog Aspect is in force" net -- has exactly one definition,
      is declared before its first use (emu.sv has no `default_nettype none`, so a use
      ahead of the declaration is an implicit 1-bit net), evaluates to
      interlaced_eff | sel==2 | sel==3, and is what player_regs_inst .aa_live reads, so
      SPRM14 (the TV shape a disc reads) follows the same gate as the picture. The SPRM14
      value MAPPING belongs to bench/dvd/player_regs_tb.sv (its Progressive cases); this
      proves the input it is handed.
  [6] THE B15 ASPECT BUTTON IS UNCHANGED (user decision 2026-10-04): aspect_ctl_inst
      .analog_live is interlaced_eff, so on Progressive the button keeps cycling Aspect
      Ratio, which is what an HDMI-only rig needs.
  [7] VIDEO_ARX/ARY still read both enables, so HDMI geometry follows the corrected raster.

Exit 0 = wired as designed; 1 = a named failure. Optional argv[1] = a file to check
instead of dvd/emu.sv (bench/dvd/run_prog_aspect.sh mutates a copy in $TMP).
"""
import itertools
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import check_menu_panscan_wiring as mp  # noqa: E402  (Expr, Resolver, FREE, strip_comments)


def pre_branch(e):
    """main @ 4da0adc, verbatim: interlaced_eff & ~p240_eff & (menu-swapped want_lb/crop),
    p240_eff = interlaced_eff & sif_det_s2. Frozen -- do not edit to make a check pass."""
    menu_ctx = e['menus_on'] and e['menu_active']
    wide_eff = e['menu_ar_wide_w'] if menu_ctx else e['ar_wide_auto']
    sel = e['aa_osd_sel']
    want_lb = sel == 2 or (sel == 0 and wide_eff)
    want_crop = sel == 3
    menu169 = menu_ctx and e['menu_ar_wide_w']
    lb_to_crop = menu169 and e['menu_ar_df_w'] == 1
    crop_to_lb = menu169 and e['menu_ar_df_w'] == 2
    p240 = e['interlaced_eff'] and e['sif_det_s2']
    gate = e['interlaced_eff'] and not p240
    lb = (want_lb and not lb_to_crop) or (want_crop and crop_to_lb)
    crop = (want_crop and not crop_to_lb) or (want_lb and lb_to_crop)
    return int(gate and lb), int(gate and crop)


def port_net(src, inst, port):
    """The text inside `.port( ... )` of instance `inst` (None if absent)."""
    m = re.search(r'\b' + re.escape(inst) + r'\s*\(', src)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(src) and depth:
        depth += {'(': 1, ')': -1}.get(src[i], 0)
        i += 1
    body = src[m.end():i - 1]
    pm = re.search(r'\.' + re.escape(port) + r'\s*\(\s*([^()]*?)\s*\)', body)
    return pm.group(1).strip() if pm else None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'dvd', 'emu.sv')
    src = mp.strip_comments(open(path).read())
    fails = []
    r = mp.Resolver(src)
    names = sorted(mp.FREE)
    space = [range(1 << mp.FREE[n]) for n in names]

    def enables(e):
        return (r.ev(('id', 'analog_letterbox'), e)[0], r.ev(('id', 'analog_crop'), e)[0])

    # [1]..[4] the resolve, over the whole input space
    b1, b2, b3, b4, points = [], [], [], [], 0
    try:
        for vals in itertools.product(*space):
            e = dict(zip(names, vals))
            got = enables(e)
            points += 1
            il, sel, sif = e['interlaced_eff'], e['aa_osd_sel'], e['sif_det_s2']
            if il:
                if got != pre_branch(e):
                    b1.append((e, got, pre_branch(e)))
            elif sel in (0, 1):
                if got != (0, 0):
                    b2.append((e, got))
            elif not sif:
                want = pre_branch(dict(e, interlaced_eff=1))
                if got != want:
                    b3.append((e, got, want))
            if sif and got != (0, 0):
                b4.append((e, got))
    except (LookupError, ValueError, KeyError) as ex:
        fails.append(f'cannot evaluate the resolve: {ex}')
    else:
        if b1:
            e, got, want = b1[0]
            fails.append(f'[1] INTERLACED resolve changed at {len(b1)} points; first: {e} -> '
                         f'(lb,crop)={got}, main gave {want}')
        if b2:
            e, got = b2[0]
            fails.append(f'[2] Auto/Fit now correct on Progressive at {len(b2)} points (user '
                         f'decision: they must not); first: {e} -> (lb,crop)={got}')
        if b3:
            e, got, want = b3[0]
            if all(g == (0, 0) for _, g, _ in b3):
                fails.append(f'[3] Letterbox/Crop do nothing on Progressive ({len(b3)} points); '
                             f'first: {e} -> {got}, want {want}')
            else:
                fails.append(f'[3] explicit Letterbox/Crop on Progressive differs from the '
                             f'interlaced resolve at {len(b3)} points; first: {e} -> {got}, want {want}')
        if b4:
            e, got = b4[0]
            fails.append(f'[4] SIF-height content corrected at {len(b4)} points (the overlay bar '
                         f'literals assume a full-height picture); first: {e} -> {got}')

    # [5] aa_live: one definition, declared before use, the right function, read by player_regs
    d = mp.definitions(src, 'aa_live')
    if len(d) != 1:
        fails.append(f'[5] aa_live: expected exactly one definition, found {len(d)}')
    else:
        first_use = re.search(r'\baa_live\b', src)
        decl = re.search(r'\bwire\s*(?:\[[^\]]*\]\s*)?aa_live\s*=', src)
        if first_use and decl and first_use.start() < decl.start():
            line = src.count('\n', 0, first_use.start()) + 1
            fails.append(f'[5] aa_live is used (line {line}) before its declaration -- an implicit '
                         f'1-bit net there')
        try:
            badf = []
            for il in (0, 1):
                for sel in range(4):
                    e = {n: 0 for n in names}
                    e.update(interlaced_eff=il, aa_osd_sel=sel)
                    got = r.ev(('id', 'aa_live'), e)[0]
                    want = int(il or sel in (2, 3))
                    if got != want:
                        badf.append(f'il={il} sel={sel} -> {got} (want {want})')
            if badf:
                fails.append(f'[5] aa_live is not interlaced_eff | sel==2 | sel==3: {badf}')
        except (LookupError, ValueError, KeyError) as ex:
            fails.append(f'[5] cannot evaluate aa_live: {ex}')
    net = port_net(src, 'player_regs_inst', 'aa_live')
    if net != 'aa_live':
        fails.append(f'[5] player_regs_inst .aa_live must be the aa_live net, found `{net}` -- '
                     f'SPRM14 would describe a different gate from the picture')
    for en in ('analog_letterbox', 'analog_crop'):
        dd = mp.definitions(src, en)
        if len(dd) == 1 and 'aa_live' not in re.findall(r'\w+', dd[0]):
            fails.append(f'[5] {en} does not read aa_live -- picture and SPRM14 can drift apart: `{dd[0]}`')

    # [6] the B15 Aspect button stays on the interlaced raster
    net = port_net(src, 'aspect_ctl_inst', 'analog_live')
    if net != 'interlaced_eff':
        fails.append(f'[6] aspect_ctl_inst .analog_live must stay interlaced_eff (B15 unchanged on '
                     f'Progressive, user decision 2026-10-04); found `{net}`')

    # [7] HDMI geometry follows the corrected raster
    for name in ('VIDEO_ARX', 'VIDEO_ARY'):
        dd = mp.definitions(src, name)
        if len(dd) != 1 or not {'analog_letterbox', 'analog_crop'} <= set(re.findall(r'\w+', dd[0])):
            fails.append(f'[7] {name} must read both analog_letterbox and analog_crop; found {dd}')

    if fails:
        for f in fails:
            print(f'FAIL: {f}')
        return 1
    print(f'OK: interlaced bit-identical, Auto/Fit untouched on Progressive, explicit Letterbox/'
          f'Crop resolve there (menu swap included), SIF never corrected -- {points} points; '
          f'aa_live feeds player_regs; B15 unchanged; ARX/ARY follow')
    return 0


if __name__ == '__main__':
    sys.exit(main())
