#!/usr/bin/env python3
"""check_decode_duty_wiring.py -- the dec_duty instrument's seams
(docs/decode_pacing.md).

WHY A SCRIPT: dec_duty_tb and dvd_telem_tb are each handed their inputs and are
correct for what they are given. What decides whether telemetry words 17..20
report the decoder -- the right net into the right class, the right counter
into the right word, reset by the pin and not by a flush -- is a chain of
connections no bench instantiates (emu.sv has no bench). A swapped pair here
would still pass both benches and would report "parked on the display" as
"starved", which is exactly the distinction the instrument exists to make.

WHAT IS PINNED
  motcomp.v      dbg_picbuf_busy is picbuf_busy itself (not busy, which also
                 carries flush_mvec_fifo -- decode work, not a display wait).
  mpeg2video.v   dec_duty: .rst(hard_rst) -- the PIN reset; sync_rst also fires on
                 every soft flush and would zero the counters mid-window.
                 .picbuf_busy <- motcomp.dbg_picbuf_busy; .getbits_valid /
                 .vld_en <- getbits_fifo's own outputs; .ref_stall <-
                 motcomp.dbg_ref_stall; outputs disp/starve/back/ref ->
                 dbg_prof0..3 in that order.
  emu.sv         dbg_prof0..3 -> core_duty_{disp,starve,back,ref} ->
                 dvd_telem .dec_{disp,starve,back,ref}.
  dvd_telem.sv   src[17..20] = dec_{disp,starve,back,ref}; word 16 = DUTY_MAGIC.
  DVD.qsf        names dvd/dec_duty.sv (a file it does not name is invisible to
                 Quartus and to tools/lint_undriven.sh).
  dvd_ctl.cpp    reads 21 words and trusts 17..20 only behind the marker.

strip_comments() first; every test is over tokens; a missing instance is a named
FAIL, never a skip.

Usage: check_decode_duty_wiring.py [--emu P] [--mpeg P] [--motcomp P] [--telem P]
Exit 0 = wired as designed, 1 = not.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from check_field_blend_wiring import strip_comments, connections  # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        fails += 1


def norm(e):
    return re.sub(r'\s+', '', e or '')


def pin(conns, inst, port, want):
    got = None if conns is None else conns.get(port)
    ok(conns is not None and norm(got) == want,
       f'{inst}.{port}({want})' + ('' if norm(got) == want else f'   got: {got!r}'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--emu', default=os.path.join(ROOT, 'dvd/emu.sv'))
    ap.add_argument('--mpeg', default=os.path.join(ROOT, 'rtl/mpeg2/mpeg2video.v'))
    ap.add_argument('--motcomp', default=os.path.join(ROOT, 'rtl/mpeg2/motcomp.v'))
    ap.add_argument('--telem', default=os.path.join(ROOT, 'dvd/dvd_telem.sv'))
    ap.add_argument('--qsf', default=os.path.join(ROOT, 'DVD.qsf'))
    ap.add_argument('--ctl', default=os.path.join(ROOT, 'main/support/dvd/dvd_ctl.cpp'))
    a = ap.parse_args()
    rd = lambda p: strip_comments(open(p).read())

    print('== motcomp.v ==')
    mc = rd(a.motcomp)
    ok(re.search(r'\bassign\s+dbg_picbuf_busy\s*=\s*picbuf_busy\s*;', mc) is not None,
       'assign dbg_picbuf_busy = picbuf_busy;  (not busy: that includes flush_mvec_fifo)')

    print('== mpeg2video.v ==')
    mv = rd(a.mpeg)
    dd = connections(mv, 'dec_duty')
    ok(dd is not None, 'dec_duty is instantiated')
    pin(dd, 'dec_duty', 'rst', 'hard_rst')
    pin(dd, 'dec_duty', 'getbits_valid', 'getbits_valid')
    pin(dd, 'dec_duty', 'vld_en', 'vld_en')
    pin(dd, 'dec_duty', 'ref_stall', 'recon_ref_stall')
    for port, net in (('disp_cnt', 'dbg_prof0'), ('starve_cnt', 'dbg_prof1'),
                      ('back_cnt', 'dbg_prof2'), ('ref_cnt', 'dbg_prof3')):
        pin(dd, 'dec_duty', port, net)
    mco = connections(mv, 'motcomp')
    pb = None if dd is None else norm(dd.get('picbuf_busy'))
    pin(mco, 'motcomp', 'dbg_picbuf_busy', pb or '<dec_duty.picbuf_busy>')
    pin(mco, 'motcomp', 'dbg_ref_stall', 'recon_ref_stall')
    gb = connections(mv, 'getbits_fifo')
    pin(gb, 'getbits_fifo', 'getbits_valid', 'getbits_valid')
    pin(gb, 'getbits_fifo', 'vld_en', 'vld_en')
    ok(re.search(r'\bassign\s+dbg_prof[0-3]\s*=', mv) is None,
       'no dbg_prof[0-3] is also tied off by an assign')

    print('== emu.sv ==')
    em = rd(a.emu)
    mp = connections(em, 'mpeg2video')
    tl = connections(em, 'dvd_telem')
    for i, cls in enumerate(('disp', 'starve', 'back', 'ref')):
        pin(mp, 'mpeg2video', f'dbg_prof{i}', f'core_duty_{cls}')
        pin(tl, 'dvd_telem', f'dec_{cls}', f'core_duty_{cls}')

    print('== dvd_telem.sv ==')
    te = rd(a.telem)
    for i, cls in zip(range(17, 21), ('disp', 'starve', 'back', 'ref')):
        ok(re.search(r'\bassign\s+src\[%d\]\s*=\s*dec_%s\s*;' % (i, cls), te) is not None,
           f'src[{i}] = dec_{cls}')
    ok(re.search(r"5'd16\s*:\s*dout_r\s*<=\s*DUTY_MAGIC\s*;", te) is not None,
       'word 16 = DUTY_MAGIC')

    print('== DVD.qsf ==')
    q = open(a.qsf).read()
    ok(re.search(r'^set_global_assignment\s+-name\s+SYSTEMVERILOG_FILE\s+dvd/dec_duty\.sv\s*$',
                 q, re.M) is not None, 'DVD.qsf names dvd/dec_duty.sv')

    print('== dvd_ctl.cpp ==')
    c = strip_comments(open(a.ctl).read())
    ok(re.search(r'uint16_t\s+w\[\s*21\s*\]', c) is not None, 'reads 21 words')
    ok(re.search(r'w\[16\]\s*==\s*DVD_TELEM_DUTY_MAGIC', c) is not None,
       'words 17..20 trusted only behind w[16] == DVD_TELEM_DUTY_MAGIC')

    print('RESULT:', 'PASS' if fails == 0 else f'FAIL ({fails})')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
