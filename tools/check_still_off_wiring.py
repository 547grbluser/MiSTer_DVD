#!/usr/bin/env python3
"""Gate for the user STILL OFF (UOP18, audit item 5): check how emu.sv decides
that a Play/Pause or Select press ends a still, and hands it to the reader.

WHY THIS EXISTS
---------------
The reader's half is benched (`iso_reader_stilloff_tb`): handed a `still_off`
pulse, it runs the still's deferred action, or ignores it on a dead-end hold.
Everything that decides WHEN that pulse exists lives in emu.sv, which has no
bench:
  * which keys raise it (Play/Pause and Select, by user decision 2026-10-05);
  * that it fires only on a PARKED still (`still_active`) with NO button armed
    and NONE pending (`hl_btns_armed`, `hl_btns_pend`). Without the pending term
    a press in the window between a menu still's park and its HLI promotion
    would skip the menu the buttons belong to;
  * that it preempts the pause toggle and clears pause. Left to toggle as well,
    a STILL_NEXT exit (no jump_ack) would land the next cell PAUSED.
A module bench is handed the value and cannot see a wrong wire, so this reads
the connections out of the file.

What is checked:
  * `still_off_ok` = an AND of exactly menus_on, still_active, !hl_btns_armed,
    !hl_btns_pend, !stopped_w;
  * `still_off_go` = still_off_ok && (pause_edge || sel_edge);
  * the reader's `.still_off` is a plain reg driven only from still_off_go
    (and its reset 0), and `.still_active` is the net still_off_ok reads;
  * nav_pci's `.btns_pend` / `.btns_armed` drive the nets still_off_ok reads;
  * the pause chain reads `if (start_streaming) ... else if (still_off_go)
    pause_q <= 1'b0; else if (pause_edge ...)`, i.e. the arm sits ABOVE the toggle.

    python3 tools/check_still_off_wiring.py [emu.sv]   # exit 0 = wired right
    python3 tools/check_still_off_wiring.py --red      # self-test: each
                                                       # miswiring must FAIL

strip_comments() runs first and every match is over code, never a grep: the
comments beside this logic quote the terms it checks.
"""
import os
import re
import sys


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
    """Text between the '(' after `module <inst_name>` and its matching ')'."""
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
    m = re.search(r'\.' + re.escape(port) + r'\s*\(\s*([^()]*?)\s*\)', body)
    return None if not m else m.group(1).strip()


def wire_expr(src, name):
    """RHS of `wire <name> = <expr>;`, whitespace-collapsed, or None."""
    m = re.search(r'\bwire\s+' + re.escape(name) + r'\s*=\s*([^;]+);', src)
    return None if not m else ''.join(m.group(1).split())


IDENT = re.compile(r'^[A-Za-z_]\w*$')

OK_TERMS = {'menus_on': False, 'still_active': False,
            'hl_btns_armed': True, 'hl_btns_pend': True, 'stopped_w': True}


def check(raw):
    src = strip_comments(raw)
    bad = []

    # ---- still_off_ok: the parked-still, no-buttons gate --------------------
    ok = wire_expr(src, 'still_off_ok')
    if ok is None:
        bad.append('still_off_ok: no `wire still_off_ok = ...;` declaration')
    else:
        if '||' in ok or '|' in ok.replace('||', ''):
            bad.append('still_off_ok: must be a pure AND, got `%s`' % ok)
        factors = ok.split('&&')
        seen = {}
        for f in factors:
            f = f.strip('()')
            neg = f.startswith('!') or f.startswith('~')
            name = f.lstrip('!~').strip('()')
            seen[name] = neg
        for name, want_neg in OK_TERMS.items():
            if name not in seen:
                bad.append('still_off_ok: missing the %s term (`%s`)' % (name, ok))
            elif seen[name] != want_neg:
                bad.append('still_off_ok: %s must be %s (`%s`)'
                           % (name, 'negated' if want_neg else 'un-negated', ok))
        extra = sorted(set(seen) - set(OK_TERMS))
        if extra:
            bad.append('still_off_ok: unexpected term(s) %s' % ', '.join(extra))

    # ---- still_off_go: which keys ------------------------------------------
    go = wire_expr(src, 'still_off_go')
    allowed = {'still_off_ok&&(pause_edge||sel_edge)', 'still_off_ok&&(sel_edge||pause_edge)',
               '(pause_edge||sel_edge)&&still_off_ok', '(sel_edge||pause_edge)&&still_off_ok'}
    if go is None:
        bad.append('still_off_go: no `wire still_off_go = ...;` declaration')
    elif go not in allowed:
        bad.append('still_off_go: expected still_off_ok && (pause_edge || sel_edge), got `%s`' % go)

    # ---- the reader seam ---------------------------------------------------
    rd = instantiation_body(src, 'dvd_iso_reader')
    if rd is None:
        bad.append('no dvd_iso_reader instantiation found')
    else:
        net = port_net(rd, 'still_off')
        if net is None:
            bad.append('dvd_iso_reader has no .still_off connection')
        elif not IDENT.match(net):
            bad.append("dvd_iso_reader .still_off is '%s' -- must be a plain net" % net)
        else:
            rhs = [''.join(r.split()) for r in
                   re.findall(r'\b' + re.escape(net) + r'\s*<=\s*([^;]+);', src)]
            if not rhs:
                bad.append("'%s' is never assigned" % net)
            elif 'still_off_go' not in rhs:
                bad.append("'%s' is not driven from still_off_go (got %s)" % (net, rhs))
            stray = [r for r in rhs if r not in ('still_off_go', "1'b0")]
            if stray:
                bad.append("'%s' has another driver: %s" % (net, stray))
            if re.search(r'(?m)^\s*assign\s+' + re.escape(net) + r'\b', src):
                bad.append("'%s' is also driven by an assign" % net)
        sa = port_net(rd, 'still_active')
        if sa != 'still_active':
            bad.append("dvd_iso_reader .still_active is '%s', not the still_active "
                       "that still_off_ok reads" % sa)

    # ---- nav_pci: the no-buttons facts --------------------------------------
    nav = instantiation_body(src, 'nav_pci')
    if nav is None:
        bad.append('no nav_pci instantiation found')
    else:
        for port, want in (('btns_armed', 'hl_btns_armed'), ('btns_pend', 'hl_btns_pend')):
            got = port_net(nav, port)
            if got != want:
                bad.append("nav_pci .%s is '%s', expected %s" % (port, got, want))
        if not re.search(r'(?m)^\s*wire\s+hl_btns_pend\s*;', src):
            bad.append('hl_btns_pend is not declared as a 1-bit wire')

    # ---- the pause chain: the arm sits above the toggle ----------------------
    chain = re.search(
        r"if\s*\(\s*start_streaming\s*\)\s*pause_q\s*<=\s*1'b0\s*;"
        r"\s*else\s+if\s*\(\s*still_off_go\s*\)\s*pause_q\s*<=\s*1'b0\s*;"
        r"\s*else\s+if\s*\(\s*pause_edge\b", src)
    if not chain:
        bad.append('pause chain: `else if (still_off_go) pause_q <= 1\'b0;` is not the arm '
                   'between start_streaming and the pause_edge toggle')
    return bad


def red(raw):
    """Each plausible miswiring must fail the check."""
    cases = [
        ('reader port tied to 0',
         r'\.still_off\s*\(\s*still_off_p\s*\)', ".still_off      (1'b0)"),
        ('reader port dropped',
         r'\.still_off\s*\(\s*still_off_p\s*\)\s*,', ''),
        ('Select no longer a key',
         r'\(pause_edge\s*\|\|\s*sel_edge\)', '(pause_edge)'),
        ('pending HLI not excluded',
         r'!hl_btns_pend\s*&&', ''),
        # Anchored on the declaration: the comment above it quotes the same terms.
        ('armed buttons not excluded',
         r'(still_off_ok\s*=\s*menus_on\s*&&\s*still_active\s*&&\s*)!hl_btns_armed\s*&&\s*', r'\1'),
        ('not limited to a parked still',
         r'(still_off_ok\s*=\s*menus_on\s*&&\s*)still_active\s*&&\s*', r'\1'),
        ('allowed while stopped',
         r'&&\s*\n\s*!stopped_w', ''),
        ('pulse driven from the raw key',
         r'still_off_p\s*<=\s*still_off_go\s*;', 'still_off_p  <= pause_edge;'),
        ('pause arm removed',
         r"else if \(still_off_go\)\s*pause_q <= 1'b0;", ''),
        ('nav_pci pending port dropped',
         r'\.btns_pend\s*\(\s*hl_btns_pend\s*\)\s*,', ''),
    ]
    ok = True
    for name, pat, sub in cases:
        mut, n = re.subn(pat, sub, raw, count=1)
        if n == 0:
            print('RED STALE: %s (pattern matched nothing)' % name)
            ok = False
        else:
            why = check(mut)
            if not why:
                print('RED MISS: %s passed the check' % name)
                ok = False
            else:
                print('red ok: %s -> FAIL: %s' % (name, why[0]))
    return ok


def main():
    args = [a for a in sys.argv[1:] if a != '--red']
    path = args[0] if args else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'dvd', 'emu.sv')
    raw = open(path).read()
    if '--red' in sys.argv[1:]:
        if not red(raw):
            return 1
    bad = check(raw)
    if bad:
        print('\n'.join('FAIL: ' + b for b in bad))
        return 1
    print('OK: still_off_ok (parked, no buttons armed/pending, menus on, not stopped) && '
          '(Play/Pause | Select) -> dvd_iso_reader .still_off; pause arm above the toggle')
    return 0


if __name__ == '__main__':
    sys.exit(main())
