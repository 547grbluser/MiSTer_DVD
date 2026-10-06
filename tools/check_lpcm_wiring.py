#!/usr/bin/env python3
"""Gate for full DVD-Video LPCM (feature/lpcm-full, docs/lpcm_full.md): check that
emu.sv carries the LPCM header from ps_demux to dvd_audio_decode, the HDMI link rate
from sys_top through a synchroniser, and the reserved-header flag to the popup.

WHY THIS EXISTS
---------------
`lpcm_full_tb` proves the arithmetic and `lpcm_dec_tb` proves dvd_audio_decode's
seams, but both are HANDED their inputs. emu.sv has no bench, so a wrong wire there
is invisible to them. The plausible ones:

  * a header field tied off or crossed (fs96 <-> bad): every 96 kHz track would
    play at half speed, or every one would be muted;
  * `.link96 (AUDIO_96K)` straight from sys_top's clock domain, unsynchronised;
  * `.link96 (1'b0)`, or the sys_top tap tied off: 96 kHz is always decimated
    (it sounds right, so nobody notices -- exactly why this is gated);
  * lpcm_unsup left out of aud_unsupported: a reserved track plays silent with no
    message, the failure this feature exists to end.

What is checked (in emu.sv, comments stripped):
  * ps_demux .aud_lpcm_{quant,nch_m1,fs96,bad} and dvd_audio_decode
    .lpcm_{quant,nch_m1,fs96,bad} are the same plain nets, declared at the right width;
  * dvd_audio_decode .link96 is the last stage of a >= 2-flop shift of AUDIO_96K,
    and AUDIO_96K is an input port;
  * dvd_audio_decode .lpcm_unsup is a plain net that appears in aud_unsupported;
and in sys/sys_top.v: the emu instance passes .AUDIO_96K(audio_96k), with
audio_96k = cfg[6] (the bit audio_out clocks from).

    python3 tools/check_lpcm_wiring.py            # exit 0 = wired right
    python3 tools/check_lpcm_wiring.py --red      # each mutation must FAIL

⚠ Walks to each instantiation's matching ')' and strips comments -- do NOT
"simplify" it to a grep: emu.sv carries commented-out history.
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


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
            out.append(re.sub(r'[^\n]', ' ', src[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def instantiation_body(src, module):
    m = re.search(r'(?m)^\s*' + re.escape(module) + r'\s+(#\s*\(.*?\)\s*)?\w+\s*\(', src, re.S)
    if not m:
        return None
    i = m.end() - 1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '(':
            depth += 1
        elif src[j] == ')':
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    return None


def port_net(body, port):
    m = re.search(r'\.' + re.escape(port) + r'\s*\(', body or '')
    if not m:
        return None
    i = m.end()
    depth = 1
    for j in range(i, len(body)):
        if body[j] == '(':
            depth += 1
        elif body[j] == ')':
            depth -= 1
            if depth == 0:
                return body[i:j].strip()
    return None


IDENT = re.compile(r'^[A-Za-z_]\w*$')


def check(emu_raw, top_raw):
    src = strip_comments(emu_raw)
    top = strip_comments(top_raw)
    bad = []
    dm = instantiation_body(src, 'ps_demux')
    dec = instantiation_body(src, 'dvd_audio_decode')
    if dm is None or dec is None:
        return ['no ps_demux / dvd_audio_decode instantiation found']

    def declared(net, width):
        if width == 1:
            pat = r'(?m)^\s*wire\s+([\w\s,]*,\s*)?%s\b' % re.escape(net)
        else:
            pat = r'(?m)^\s*wire\s*\[\s*%d\s*:\s*0\s*\]\s*([\w\s,]*,\s*)?%s\b' % (width - 1, re.escape(net))
        return re.search(pat, src) is not None

    # the header: demux -> decode, field for field
    for field, width in (('quant', 2), ('nch_m1', 3), ('fs96', 1), ('bad', 1)):
        a = port_net(dm, 'aud_lpcm_' + field)
        b = port_net(dec, 'lpcm_' + field)
        if not a or not IDENT.match(a):
            bad.append("ps_demux .aud_lpcm_%s is '%s' -- must be a plain net" % (field, a))
            continue
        if b != a:
            bad.append("dvd_audio_decode .lpcm_%s is '%s', not ps_demux's .aud_lpcm_%s net (%s)"
                       % (field, b, field, a))
        elif not declared(a, width):
            bad.append("net '%s' is not declared as a %d-bit wire" % (a, width))

    # the link rate: AUDIO_96K -> >= 2 flops -> .link96
    if not re.search(r'(?m)^\s*input\s+AUDIO_96K\b', src):
        bad.append('emu has no `input AUDIO_96K` port')
    ln = port_net(dec, 'link96')
    if not ln or not IDENT.match(ln):
        bad.append("dvd_audio_decode .link96 is '%s' -- must be the synchroniser's output net" % ln)
    else:
        m = re.search(r'\bwire\s+%s\s*=\s*(\w+)\s*\[\s*(\d+)\s*\]\s*;' % re.escape(ln), src)
        if not m:
            bad.append("'%s' is not a wire taken from a synchroniser stage (sync[n])" % ln)
        else:
            sreg, stage = m.group(1), int(m.group(2))
            sh = re.search(r'\b%s\s*<=\s*\{\s*%s\s*\[\s*%d\s*\]\s*,\s*AUDIO_96K\s*\}\s*;'
                           % (re.escape(sreg), re.escape(sreg), stage - 1), src)
            if stage < 1 or not sh:
                bad.append("'%s' is not the last stage of a >= 2-flop shift of AUDIO_96K" % ln)

    # the popup
    un = port_net(dec, 'lpcm_unsup')
    if not un or not IDENT.match(un):
        bad.append("dvd_audio_decode .lpcm_unsup is '%s' -- must be a net (never left open)" % un)
    else:
        m = re.search(r'\bwire\s+aud_unsupported\s*=\s*(.*?);', src, re.S)
        if not m:
            bad.append('no `wire aud_unsupported = ...` found')
        else:
            rhs = m.group(1)
            # the term must not be ANDed with a constant 0
            if not re.search(r'\b%s\b' % re.escape(un), rhs) or re.search(r"1'b0\s*&\s*%s\b" % re.escape(un), rhs) \
                    or re.search(r"\(\s*1'b0\s*&[^|]*\b%s\b" % re.escape(un), rhs):
                bad.append("aud_unsupported does not carry %s -- a reserved LPCM track would "
                           "play silent with no message" % un)

    # sys_top: the tap
    em = instantiation_body(top, 'emu')
    if em is None:
        bad.append('sys_top: no emu instantiation found')
    else:
        t = port_net(em, 'AUDIO_96K')
        if t != 'audio_96k':
            bad.append("sys_top: emu .AUDIO_96K is '%s' -- must be audio_96k" % t)
    if not re.search(r'\bwire\s+audio_96k\s*=\s*cfg\s*\[\s*6\s*\]\s*;', top):
        bad.append('sys_top: audio_96k is not cfg[6]')
    return bad


MUTATIONS = [  # (label, file, pattern, replacement)
    ('nch tied stereo', 'emu', r'\.lpcm_nch_m1\s*\(\s*ps_aud_lpcm_nch_m1\s*\)', ".lpcm_nch_m1 (3'd1)"),
    ('fs96 <- bad', 'emu', r'\.lpcm_fs96\s*\(\s*ps_aud_lpcm_fs96\s*\)', '.lpcm_fs96 (ps_aud_lpcm_bad)'),
    ('bad tied low', 'emu', r'\.lpcm_bad\s*\(\s*ps_aud_lpcm_bad\s*\)', ".lpcm_bad (1'b0)"),
    ('demux nch open', 'emu', r'\.aud_lpcm_nch_m1\s*\(\s*ps_aud_lpcm_nch_m1\s*\)', '.aud_lpcm_nch_m1 ()'),
    ('link raw', 'emu', r'\.link96\s*\(\s*aud_link96\s*\)', '.link96 (AUDIO_96K)'),
    ('link tied low', 'emu', r'\.link96\s*\(\s*aud_link96\s*\)', ".link96 (1'b0)"),
    ('one-flop sync', 'emu', r'wire\s+aud_link96\s*=\s*aud_link96_s\[1\];', 'wire aud_link96 = aud_link96_s[0];'),
    ('popup dropped', 'emu', r'\(aud_lpcm_unsup & aud_dec_en', "(1'b0 & aud_dec_en"),
    ('unsup open', 'emu', r'\.lpcm_unsup\s*\(\s*aud_lpcm_unsup\s*\)', '.lpcm_unsup ()'),
    ('sys_top tap tied', 'top', r'\.AUDIO_96K\(audio_96k\)', ".AUDIO_96K(1'b0)"),
]


def main():
    emu_p = os.path.join(ROOT, 'dvd', 'emu.sv')
    top_p = os.path.join(ROOT, 'sys', 'sys_top.v')
    emu, top = open(emu_p).read(), open(top_p).read()
    if '--red' in sys.argv:
        if check(emu, top):
            print('FAIL: the unmutated files do not pass:', check(emu, top))
            return 1
        rc = 0
        for label, which, pat, rep in MUTATIONS:
            src = emu if which == 'emu' else top
            mut, n = re.subn(pat, rep, src, count=1)
            if n == 0:
                print('FAIL %s: the mutation did not apply (anchor moved)' % label)
                rc = 1
                continue
            found = check(mut, top) if which == 'emu' else check(emu, mut)
            if found:
                print('ok   %s -> %s' % (label, found[0]))
            else:
                print('FAIL %s: the mutant PASSED' % label)
                rc = 1
        return rc
    bad = check(emu, top)
    if bad:
        print('\n'.join('FAIL: ' + b for b in bad))
        return 1
    print('OK: ps_demux LPCM header -> dvd_audio_decode (quant, nch_m1, fs96, bad); '
          'sys_top cfg[6] -> AUDIO_96K -> 2-flop sync -> .link96; lpcm_unsup -> aud_unsupported')
    return 0


if __name__ == '__main__':
    sys.exit(main())
