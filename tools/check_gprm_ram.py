#!/usr/bin/env python3
"""Gate: dvd_vm's GPRMs stay a RAM (2026-09-26, docs/logic_reclaim.md §9).

The 16 GPRMs are a single-port M10K. That only holds while EVERY access to
the `gprm` array sits in its port block:

  * a READ anywhere else (a continuous assign, an always @*, a debug tap) makes
    Quartus build the array out of LUTs -- the parse_buf LUT-RAM explosion --
    and puts the ~350 ALMs this move reclaimed straight back;
  * a WRITE anywhere else, or an async reset of the array, stops the RAM being
    inferred at all, and Quartus says NOTHING (the ext_mem trap,
    docs/logic_reclaim.md).

Neither shows up in simulation; both show up as a fit that no longer closes.
So this reads dvd/dvd_vm.sv (comments stripped) and requires that every
`gprm[` outside a comment is one of the two port-block lines, and that the
array keeps its M10K ramstyle.

    python3 tools/check_gprm_ram.py [dvd_vm.sv]
    python3 tools/check_gprm_ram.py --red     # must FAIL each re-regression
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from check_hl_btnn_wiring import strip_comments  # noqa: E402

ALLOWED = {
    'if (g_we) gprm[g_aa] <= g_wd;',
    'g_qa <= gprm[g_aa];',
}


def check(path):
    src = strip_comments(open(path).read())
    bad = []
    if not re.search(r'\(\*\s*ramstyle\s*=\s*"M10K[^"]*"\s*\*\)\s*reg\s*\[15:0\]\s*gprm\s*\[0:15\]', src):
        bad.append('gprm is not declared (* ramstyle = "M10K..." *) reg [15:0] gprm [0:15]')
    for ln in src.splitlines():
        if 'gprm[' in ln:
            t = ' '.join(ln.split())
            if t not in ALLOWED:
                bad.append("gprm[] touched outside its RAM port block: '%s' -- a read "
                           "here rebuilds the array out of LUTs, a write stops the RAM "
                           "inferring" % t)
    return bad


def red():
    path = os.path.join(HERE, '..', 'dvd', 'dvd_vm.sv')
    if check(path):
        print('  GREEN FAILS on the working tree -- fix that first')
        return 1
    txt = open(path).read()
    muts = [
        ('R1 a debug tap reads the array', 'assign dbg_g3    = 16\'d0;', 'assign dbg_g3    = gprm[3];'),
        ('R2 a combinational operand read', ': opA;', ': gprm[cmpa_rsel[3:0]];'),
        ('R3 the array reset in the FSM', "        gprm_mode <= 16'd0;\n        sprm1 <= 16'd15;",
         "        for (gi = 0; gi < 16; gi = gi + 1) gprm[gi] <= 16'd0;\n        gprm_mode <= 16'd0;\n        sprm1 <= 16'd15;"),
        ('R4 ramstyle dropped', '(* ramstyle = "M10K, no_rw_check" *) reg [15:0] gprm [0:15];',
         'reg [15:0] gprm [0:15];'),
    ]
    rc = 0
    for label, a, b in muts:
        if txt.count(a) < 1:
            print('  BROKEN %s: anchor not found' % label)
            rc = 1
            continue
        with tempfile.NamedTemporaryFile('w', suffix='.sv', delete=False) as f:
            f.write(txt.replace(a, b, 1))
            tmp = f.name
        got = check(tmp)
        os.unlink(tmp)
        print(('  ok     %s -> %s' % (label, got[0])) if got else ('  MISSED %s' % label))
        rc |= 0 if got else 1
    return rc


def main():
    if sys.argv[1:2] == ['--red']:
        return red()
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, '..', 'dvd', 'dvd_vm.sv')
    bad = check(path)
    if bad:
        print('\n'.join('FAIL: ' + b for b in bad))
        return 1
    print('OK: the GPRM array is touched only by its RAM port block')
    return 0


if __name__ == '__main__':
    sys.exit(main())
