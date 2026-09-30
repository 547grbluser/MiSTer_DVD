#!/usr/bin/env python3
"""check_chroma_reuse_wiring.py -- F2's seams: the display reuses chroma rows
(docs/decode_pacing.md §7 F2).

WHY A SCRIPT: bench/dvd/run_chroma_reuse.sh proves the pixels bit-identical in the display
chain it builds, but these live outside anything a bench instantiates, or are cheaper to
pin as text than to discover in a board round:
  * ONE knob. resample.v passes the same CHROMA_REUSE to resample_addrgen (which decides
    which chroma words are requested) and resample_dta (which pops exactly those). Two
    knobs could disagree, and a disagreement shifts every following display word.
  * the COMBINATION. The reuse path of resample_dta has no OSD read, so CHROMA_REUSE = 1
    is only valid with OSD_READS = 0 -- the defaults in resample.v must be exactly that,
    and mpeg2video must not override either.
  * the FLAGS reach resample_dta. The resample fifo carries the reuse flags only when it
    is 10 bits wide: its width must follow the parameter, and both of its data ports must
    use the same slice.
  * the KEY mirrors memory_address. A slot is tagged with the row memory_address fetches;
    if mem_addr.v's chroma motion-vector arithmetic ever changes, the key must change with
    it or reuse serves the wrong row. The two arithmetic forms are pinned side by side.
  * the flag layout {lcp, sl[1:0], fl, su[1:0], fu} agrees between the writer and the
    reader, and a slot id is {bank, slot} with the bank the row's parity (key[0]).
  * the chroma-row fix: the lower row's offsets are one chroma row (mv 4, progressive) and
    the same field's neighbour (mv 8, interlaced), and the bottom clamps read vertical_size.
    bench/dvd/run_chroma_reuse.sh [4] proves the rows; this pins the constants so an
    "obvious" revert to upstream's 2 / 4 is a named failure here too.

strip_comments() first (the comments quote old forms); a missing match is a named FAIL,
never a skip.

Usage: check_chroma_reuse_wiring.py [--mpeg P] [--resample P] [--addrgen P] [--dta P] [--memaddr P]
Exit 0 = wired as designed, 1 = not.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from check_field_blend_wiring import strip_comments  # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        fails += 1


def has(src, pat):
    return re.search(pat, src) is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mpeg', default=os.path.join(ROOT, 'rtl/mpeg2/mpeg2video.v'))
    ap.add_argument('--resample', default=os.path.join(ROOT, 'rtl/mpeg2/resample.v'))
    ap.add_argument('--addrgen', default=os.path.join(ROOT, 'dvd/resample_addrgen.v'))
    ap.add_argument('--dta', default=os.path.join(ROOT, 'rtl/mpeg2/resample_dta.v'))
    ap.add_argument('--memaddr', default=os.path.join(ROOT, 'rtl/mpeg2/mem_addr.v'))
    a = ap.parse_args()
    rd = lambda p: strip_comments(open(p).read())

    print('== no override (mpeg2video.v) ==')
    mv = rd(a.mpeg)
    ok(not has(mv, r"\bresample\s*#\s*\("),
       'mpeg2video does not override resample parameters (CHROMA_REUSE / OSD_READS stay at their defaults)')

    print('== one knob, a valid combination (resample.v) ==')
    rs = rd(a.resample)
    ok(has(rs, r"\bparameter\s+CHROMA_REUSE\s*=\s*1\s*;"), 'resample: parameter CHROMA_REUSE = 1')
    ok(has(rs, r"\bparameter\s+OSD_READS\s*=\s*0\s*;"), 'resample: parameter OSD_READS = 0 (required by reuse)')
    for inst in ('resample_addrgen', 'resample_dta'):
        m = re.search(r"\b" + inst + r"\s*#\s*\((.*?)\)\s*" + inst + r"\s*\(", rs, re.S)
        plist = m.group(1) if m else ''
        ok(has(plist, r"\.CHROMA_REUSE\s*\(\s*CHROMA_REUSE\s*\)"), f'{inst} #(.CHROMA_REUSE(CHROMA_REUSE))')
        ok(has(plist, r"\.OSD_READS\s*\(\s*OSD_READS\s*\)"), f'{inst} #(.OSD_READS(OSD_READS))')

    print('== the flags reach resample_dta (resample.v fifo) ==')
    ok(has(rs, r"localparam\s*\[8:0\]\s*RESAMPLE_WIDTH\s*=\s*CHROMA_REUSE\s*\?\s*9'd10\s*:\s*9'd3\s*;"),
       "RESAMPLE_WIDTH = CHROMA_REUSE ? 9'd10 : 9'd3")
    ok(has(rs, r"\.dta_width\s*\(\s*RESAMPLE_WIDTH\s*\)"), 'resample_fifo dta_width(RESAMPLE_WIDTH)')
    ok(has(rs, r"\.din\s*\(\s*resample_wr_dta\s*\[\s*RESAMPLE_WIDTH\s*-\s*1\s*:\s*0\s*\]\s*\)"),
       'resample_fifo din(resample_wr_dta[RESAMPLE_WIDTH-1:0])')
    ok(has(rs, r"\.dout\s*\(\s*resample_rd_dta\s*\[\s*RESAMPLE_WIDTH\s*-\s*1\s*:\s*0\s*\]\s*\)"),
       'resample_fifo dout(resample_rd_dta[RESAMPLE_WIDTH-1:0])')
    ok(has(rs, r"wire\s*\[9:0\]\s*resample_wr_dta\s*;") and has(rs, r"wire\s*\[9:0\]\s*resample_rd_dta\s*;"),
       'resample_wr_dta / resample_rd_dta are 10 bits')

    print('== the key mirrors memory_address (mem_addr.v vs resample_addrgen) ==')
    ma = rd(a.memaddr)
    ok(has(ma, r"mv_y_corr_0\s*<=\s*\{\s*11'b0\s*,\s*mv_y\[12\]\s*\}"),
       'mem_addr stage 0: mv_y_corr_0 = sign bit of mv_y')
    ok(has(ma, r"mv_y_1\s*<=\s*\(\s*mv_y_0\s*\+\s*mv_y_corr_0\s*\)\s*>>>\s*1"),
       'mem_addr stage 1 (chroma): (mv + sign) >>> 1')
    ok(has(ma, r"mv_y_2\s*<=\s*\{\s*mv_y_1\[12\]\s*,\s*mv_y_1\[12:1\]\s*\}"),
       'mem_addr stage 2: integer part = arithmetic >> 1')
    ok(has(ma, r"frame_picture_2\s*&&\s*~field_in_frame_2\)\s*begin\s*delta_y_3\s*<=\s*delta_y_2\s*;\s*mv_y_3\s*<=\s*mv_y_2\s*;"),
       'mem_addr stage 3: frame picture passes delta_y and mv_y unscaled')
    ag = rd(a.addrgen)
    ok(has(ag, r"ck_mv_sgn\s*=\s*\{\s*12'b0\s*,\s*ck_mv\[12\]\s*\}"), 'addrgen key: sign bit')
    ok(has(ag, r"ck_mv_c\s*=\s*\(\s*ck_mv\s*\+\s*ck_mv_sgn\s*\)\s*>>>\s*1"), 'addrgen key: (mv + sign) >>> 1')
    ok(has(ag, r"ck_mv_p\s*=\s*ck_mv_c\s*>>>\s*1"), 'addrgen key: integer part >>> 1')
    ok(has(ag, r"ck_lo\s*=\s*ck_up\s*\+\s*ck_mv_p"), 'addrgen key: lower = upper + integer part')
    ok(has(ag, r"\.frame_picture\s*\(\s*1'b1\s*\)") and has(ag, r"\.field_in_frame\s*\(\s*1'b0\s*\)"),
       "the display's memory_address is a frame picture, not field-in-frame (stage 3 passes the key)")
    # the key's two inputs must be the very expressions the request states use
    ok(has(ag, r"ck_up\s*=\s*progressive_upscaling\s*\?\s*\{2'b0,\s*disp_y\[11:1\]\}\s*:\s*\{2'b0,\s*disp_y\[11:2\],\s*disp_y\[0\]\}"),
       'addrgen key: upper = the chroma delta_y of STATE_WR_U/V_*')
    ok(has(ag, r"if\s*\(progressive_upscaling\)\s*disp_delta_y\s*<=\s*\{2'b0,\s*disp_y\[11:1\]\}\s*;\s*else\s*disp_delta_y\s*<=\s*\{2'b0,\s*disp_y\[11:2\],\s*disp_y\[0\]\}"),
       'addrgen: the chroma delta_y the key copies is unchanged')
    ok(has(ag, r"ck_mv\s*=\s*progressive_upscaling\s*\?\s*\(disp_y\[0\]\s*\?\s*disp_mv_y_plus_4\s*:\s*disp_mv_y_minus_4\)\s*:\s*\(disp_y\[1\]\s*\?\s*disp_mv_y_plus_8\s*:\s*disp_mv_y_minus_8\)"),
       'addrgen key: lower mv = the mv_y of STATE_WR_U/V_LOWER')
    ok(has(ag, r"if\s*\(progressive_upscaling\)\s*disp_mv_y\s*<=\s*disp_y\[0\]\s*\?\s*disp_mv_y_plus_4\s*:\s*disp_mv_y_minus_4\s*;\s*else\s*disp_mv_y\s*<=\s*disp_y\[1\]\s*\?\s*disp_mv_y_plus_8\s*:\s*disp_mv_y_minus_8\s*;"),
       'addrgen: the lower-row mv_y the key copies is unchanged')

    print('== the chroma-row fix: one chroma row is mv 4 (resample_addrgen) ==')
    for name, val in (('minus_4', r"-13'sd4"), ('plus_4', r"13'sd4"), ('minus_8', r"-13'sd8"), ('plus_8', r"13'sd8")):
        ok(has(ag, r"disp_mv_y_" + name + r"\s*=[^;]*\?\s*13'sd0\s*:\s*" + val + r"\s*;"),
           f'disp_mv_y_{name} is {val.replace(chr(92), "")} (or 0 at the edge)')
    ok(has(ag, r"disp_c_rows\s*=\s*\{\s*1'b0\s*,\s*vertical_size\[13:1\]\s*\}"),
       'bottom clamps: chroma rows = vertical_size / 2 (what memory_address clips to)')
    ok(has(ag, r"disp_mv_y_plus_4\s*=\s*\(\(disp_c_up_pr\s*\+\s*13'd1\)\s*>=\s*disp_c_rows\)")
       and has(ag, r"disp_mv_y_plus_8\s*=\s*\(\(disp_c_up_il\s*\+\s*13'd2\)\s*>=\s*disp_c_rows\)"),
       'bottom clamps fire when the neighbour row does not exist')

    print('== flag layout {lcp, sl[1:0], fl, su[1:0], fu} (writer and reader) ==')
    ok(has(ag, r"output\s+reg\s*\[9:0\]\s*resample_wr_dta\s*;"), 'addrgen resample_wr_dta is 10 bits')
    ok(has(ag, r"cr_fu\s*=\s*resample_wr_dta\[3\]") and has(ag, r"cr_fl\s*=\s*resample_wr_dta\[6\]"),
       'addrgen next-state reads fu = [3], fl = [6]')
    ok(has(ag, r"\{c_lcp,\s*bl,\s*c_sl,\s*c_fl,\s*bu,\s*c_su,\s*c_fu\}"),
       'addrgen writes {lcp, {bank, slot} lower, fl, {bank, slot} upper, fu}')
    ok(has(ag, r"\bbu\s*=\s*ck_up_q\[0\]") and has(ag, r"\bbl\s*=\s*ck_lo_q\[0\]"),
       "addrgen: a row's bank is its key's parity")
    ok(len(re.findall(r"resample_wr_dta\s*<=\s*\{\s*cr_flags\s*,\s*ROW_", ag)) == 5,
       'all five position arms carry cr_flags in [9:3]')
    dt = rd(a.dta)
    ok(has(dt, r"input\s*\[9:0\]\s*resample_rd_dta\s*;"), 'dta resample_rd_dta is 10 bits')
    ok(has(dt, r"flg\s*<=\s*p_dout\[9:3\]"), 'dta captures the flags from [9:3]')
    ok(has(dt, r"q_fetch\s*=\s*q_low\s*\?\s*flg\[3\]\s*:\s*flg\[0\]"), 'dta: fetch = fl (lower) / fu (upper)')
    ok(has(dt, r"q_slot\s*=\s*q_low\s*\?\s*flg\[5:4\]\s*:\s*flg\[2:1\]"), 'dta: slot = sl (lower) / su (upper)')
    ok(has(dt, r"q_copy\s*=\s*q_low\s*&\s*flg\[6\]"), 'dta: copy = lcp, lower words only')
    ok(has(dt, r"c_addr\s*=\s*\{\s*q\[1\]\s*,\s*q_slot\s*,\s*col\s*\}") and has(dt, r"cram\s*\[\s*0\s*:\s*511\s*\]"),
       'dta: cache = 2 planes x 4 slots x 64 columns')
    ok(has(dt, r"parameter\s+CHROMA_REUSE\s*=\s*1\s*;") and has(ag, r"parameter\s+CHROMA_REUSE\s*=\s*1\s*;"),
       'both modules default CHROMA_REUSE = 1 (a lone instantiation cannot disagree either)')

    print('RESULT:', 'PASS' if fails == 0 else f'FAIL ({fails})')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
