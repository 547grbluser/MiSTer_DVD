#!/usr/bin/env python3
"""check_dac_dither_wiring.py -- the analog DAC's dither is wired where it acts.

WHY THIS IS A SCRIPT AND NOT A BENCH (2026-10-06)
-------------------------------------------------
dvd/dac_dither.sv is gated by bench/dvd/run_dac_dither.sh, but that bench is handed
the word and cannot see which word it is handed. Every seam that decides whether the
OSD's Analog Dither does anything is a connection in dvd/emu.sv (no bench) or in
sys/sys_top.v (no bench):

  emu      assign VGA_DITHER = status[8];   and the CONF_STR row "O[8],Analog Dither"
  sys_top  .VGA_DITHER(vga_dither_en) on the emu instance
           dac_dither on clk_vid, din = vga_o, dout = vga_od, the VGA word's OWN syncs/DE
           ALL SIX DAC assigns (VGA_R/G/B, the 6 pins; vga_r/g/b, the low bits on SDIO)
           read vga_od -- one left on vga_o is a channel that never dithers, which no
           module bench and no HDMI screenshot can see

It also refuses the OSD bit being shared: status[8] must belong to exactly one
CONF_STR row, or a saved setting of some other option would switch the dither.

Traps written against: strip_comments() FIRST (the comments here quote the stock
`vga_o[23:18]` lines); identifier token sets, never substrings (vga_o vs vga_od vs
vga_o_t). A lookup that finds nothing or two of something is a NAMED FAIL.

Usage: check_dac_dither_wiring.py [--emu FILE] [--top FILE] [--qsf FILE]
(a runner mutates copies and passes them here; the tree is never touched).
Exit 0 = wired as designed; 1 = a named failure.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from check_hl_btnn_wiring import strip_comments, instantiation_body, port_net  # noqa: E402

BIT = 8


def tokens(expr):
    return set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', expr))


def letter_bit(c):
    return int(c) if c.isdigit() else ord(c) - ord('A') + 10


def conf_rows_bits(src):
    """(row text, set of status bits) for every O option in a CONF_STR string."""
    out = []
    for s in re.findall(r'"([^"\n]*)"', src):
        m = re.match(r'^(?:[PHhDd]\d+)*OX?(?:\[(\d+)(?::(\d+))?\]|([0-9A-V])([0-9A-V])?),', s)
        if not m:
            continue
        if m.group(1) is not None:
            hi = int(m.group(1))
            lo = int(m.group(2)) if m.group(2) is not None else hi
        else:
            lo = letter_bit(m.group(3))
            hi = letter_bit(m.group(4)) if m.group(4) else lo
        out.append((s, set(range(min(lo, hi), max(lo, hi) + 1))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--emu', default=os.path.join(ROOT, 'dvd', 'emu.sv'))
    ap.add_argument('--top', default=os.path.join(ROOT, 'sys', 'sys_top.v'))
    ap.add_argument('--qsf', default=os.path.join(ROOT, 'DVD.qsf'))
    a = ap.parse_args()
    fails = []

    # ---- emu.sv
    emu = strip_comments(open(a.emu).read())
    rhs = [re.sub(r'\s+', '', m.group(1))
           for m in re.finditer(r'\bassign\s+VGA_DITHER\s*=\s*([^;]*);', emu)]
    if len(rhs) != 1:
        fails.append(f'emu: expected one `assign VGA_DITHER`, found {len(rhs)}')
    elif rhs[0] != f'status[{BIT}]':
        fails.append(f'emu: VGA_DITHER = {rhs[0]}, want status[{BIT}] (the OSD row)')
    if not re.search(r'\boutput\s+VGA_DITHER\b', emu):
        fails.append('emu: no `output VGA_DITHER` port')

    rows = [(s, b) for s, b in conf_rows_bits(emu) if BIT in b]
    named = [s for s, _ in rows if s.rstrip(';').endswith(',Analog Dither,Off,On')]
    if len(named) != 1:
        fails.append(f'emu: expected one CONF_STR row "O[{BIT}],Analog Dither,Off,On", found {len(named)}')
    if len(rows) != 1:
        fails.append(f'emu: status[{BIT}] is claimed by {len(rows)} CONF_STR rows {[s for s, _ in rows]} '
                     f'-- another option\'s saved value would switch the dither')

    # ---- sys_top.v
    top = strip_comments(open(a.top).read())
    body = instantiation_body(top, 'emu')
    if body is None:
        fails.append('sys_top: emu instance not found')
    elif port_net(body, 'VGA_DITHER') != 'vga_dither_en':
        fails.append(f'sys_top: emu .VGA_DITHER -> {port_net(body, "VGA_DITHER")}, want vga_dither_en')

    want = {'clk': 'clk_vid', 'en': 'vga_dither_en', 'hs': 'vga_hs', 'vs': 'vga_vs',
            'de': 'vga_de', 'din': 'vga_o', 'dout': 'vga_od'}
    n_inst = len(re.findall(r'(?m)^\s*dac_dither\s+\w+\s*\(', top))
    db = instantiation_body(top, 'dac_dither')
    if n_inst != 1 or db is None:
        fails.append(f'sys_top: expected one dac_dither instance, found {n_inst}')
    else:
        for port, net in want.items():
            got = port_net(db, port)
            if got != net:
                fails.append(f'sys_top: dac_dither .{port}({got}), want .{port}({net})')

    pins = {
        'VGA_R': r'\bassign\s+VGA_R\s*=\s*([^;]*);',
        'VGA_G': r'\bassign\s+VGA_G\s*=\s*([^;]*);',
        'VGA_B': r'\bassign\s+VGA_B\s*=\s*([^;]*);',
        'vga_r': r'\bwire\s*\[1:0\]\s*vga_r\s*=\s*([^;]*);',
        'vga_g': r'\bwire\s*\[1:0\]\s*vga_g\s*=\s*([^;]*);',
        'vga_b': r'\bwire\s*\[1:0\]\s*vga_b\s*=\s*([^;]*);',
    }
    for name, pat in pins.items():
        found = re.findall(pat, top)
        if len(found) != 1:
            fails.append(f'sys_top: expected one driver of {name}, found {len(found)}')
            continue
        t = tokens(found[0])
        if 'vga_o' in t or 'vga_od' not in t:
            fails.append(f'sys_top: {name} reads the undithered word (`{" ".join(found[0].split())}`) '
                         f'-- that channel never dithers')

    if len(re.findall(r'\bassign\s+vga_od\b', top)) != 0:
        fails.append('sys_top: vga_od has a second driver (an assign)')
    if len(re.findall(r'\bassign\s+vga_dither_en\b', top)) != 0:
        fails.append('sys_top: vga_dither_en is assigned in sys_top, not driven by emu')

    # ---- DVD.qsf
    qsf = open(a.qsf).read()
    if not re.search(r'(?m)^set_global_assignment\s+-name\s+SYSTEMVERILOG_FILE\s+dvd/dac_dither\.sv\s*$', qsf):
        fails.append('DVD.qsf does not name dvd/dac_dither.sv -- Quartus never sees the module')

    if fails:
        for f in fails:
            print(f'FAIL: {f}')
        return 1
    print(f'OK: Analog Dither wired (status[{BIT}] -> VGA_DITHER -> dac_dither on vga_o; '
          f'all six DAC drivers read vga_od)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
