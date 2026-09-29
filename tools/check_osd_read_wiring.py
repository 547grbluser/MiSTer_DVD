#!/usr/bin/env python3
"""check_osd_read_wiring.py -- F1's seams: the display does not read the OSD words
(docs/decode_pacing.md §7).

WHY A SCRIPT: bench/dvd/run_osd_read.sh proves the pixels bit-identical in the display
chain it builds, but three things live outside anything a bench instantiates:
  * the PREMISE. F1 is only correct while the upstream OSD layer is tied off
    (mpeg2video.v `assign dot_osd_enable = 1'b0;`). If someone re-enables the OSD, the
    OSD words are needed again -- this check fails and names the decision to revisit.
  * mpeg2video must not override resample's OSD_READS back to 1 (or to a different
    value per half).
  * ONE knob: resample.v passes the same parameter to resample_addrgen and resample_dta.
    Two knobs could disagree, and a disagreement shifts every display word by one.
And inside the two modules, the duties that were keyed on the OSD state must follow
FIRST_RQ (scan_begin, the position code), and both FSMs must enter through
FIRST_RQ / FIRST_RD.

strip_comments() first (the comments quote the old forms); a missing match is a named
FAIL, never a skip.

Usage: check_osd_read_wiring.py [--mpeg P] [--resample P] [--addrgen P] [--dta P]
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
    a = ap.parse_args()
    rd = lambda p: strip_comments(open(p).read())

    print('== the premise (mpeg2video.v) ==')
    mv = rd(a.mpeg)
    ok(has(mv, r"\bassign\s+dot_osd_enable\s*=\s*1'b0\s*;"),
       "dot_osd_enable is tied 1'b0 (if the OSD is ever re-enabled, F1 must be revisited)")
    ok(not has(mv, r"\bresample\s*#\s*\("),
       'mpeg2video does not override resample parameters (OSD_READS stays at its default)')

    print('== one knob (resample.v) ==')
    rs = rd(a.resample)
    ok(has(rs, r"\bparameter\s+OSD_READS\s*=\s*0\s*;"), 'resample: parameter OSD_READS = 0')
    # (F2 appended .CHROMA_REUSE(...) to both parameter lists; checked by check_chroma_reuse_wiring.py)
    ok(has(rs, r"\bresample_addrgen\s*#\s*\(\s*\.OSD_READS\s*\(\s*OSD_READS\s*\)\s*[,)]"),
       'resample_addrgen #(.OSD_READS(OSD_READS) ...)')
    ok(has(rs, r"\bresample_dta\s*#\s*\(\s*\.OSD_READS\s*\(\s*OSD_READS\s*\)\s*[,)]"),
       'resample_dta #(.OSD_READS(OSD_READS) ...)')

    print('== resample_addrgen ==')
    ag = rd(a.addrgen)
    ok(has(ag, r"localparam\s*\[3:0\]\s*FIRST_RQ\s*=\s*OSD_READS\s*\?\s*STATE_WR_OSD_MSB\s*:\s*STATE_WR_Y_MSB\s*;"),
       'FIRST_RQ = OSD_READS ? STATE_WR_OSD_MSB : STATE_WR_Y_MSB')
    ok(has(ag, r"scan_begin\s*=\s*\(state\s*==\s*STATE_NEXT_IMG\)\s*&&\s*\(next\s*==\s*FIRST_RQ\)"),
       'scan_begin fires on entry to FIRST_RQ')
    ok(has(ag, r"resample_wr_en\s*<=\s*\(state\s*==\s*FIRST_RQ\)"),
       'one position code per macroblock, written in FIRST_RQ')
    ok(len(re.findall(r"\(state\s*==\s*FIRST_RQ\)[^;]*resample_wr_dta\s*<=", ag)) == 5,
       'all five resample_wr_dta position arms keyed on FIRST_RQ')
    ok(len(re.findall(r"next\s*=\s*FIRST_RQ\s*;", ag)) == 2,
       'both entries (STATE_NEXT_IMG, STATE_WAIT) go to FIRST_RQ')
    ok(not has(ag, r"next\s*=\s*STATE_WR_OSD_MSB\s*;") and not has(ag, r"state\s*==\s*STATE_WR_OSD_MSB"),
       'nothing else still enters or keys on STATE_WR_OSD_MSB')

    print('== resample_dta ==')
    dt = rd(a.dta)
    ok(has(dt, r"localparam\s*\[3:0\]\s*FIRST_RD\s*=\s*OSD_READS\s*\?\s*STATE_RD_OSD\s*:\s*STATE_RD_Y\s*;"),
       'FIRST_RD = OSD_READS ? STATE_RD_OSD : STATE_RD_Y')
    ok(len(re.findall(r"next\s*=\s*FIRST_RD\s*;", dt)) == 2,
       'both entries (STATE_INIT, STATE_WAIT) go to FIRST_RD')
    ok(len(re.findall(r"next\s*=\s*STATE_RD_OSD\s*;", dt)) == 1,
       'nothing else enters STATE_RD_OSD (only its own wait-for-data self-loop names it)')

    print('RESULT:', 'PASS' if fails == 0 else f'FAIL ({fails})')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
