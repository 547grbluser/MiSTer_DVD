#!/usr/bin/env python3
"""Gate for the title-edge chapter skip (audit item 7): check that emu.sv hands
the reader's chap_edge pulse to the VM.

WHY THIS EXISTS
---------------
The feature lives at a SEAM. `dvd_iso_reader` decides that a chapter burst
has nowhere left to go in the title and pulses `chap_edge` / `chap_edge_dir`
instead of seeking; `dvd_vm` acts on `key_chedge` / `key_chedge_dir` (Next runs
the PGC's POST, Prev follows prev_pgcn). Each module's bench drives its own
side with the other side's value (`iso_reader_chapedge_tb`, `dvd_vm_tb` S27),
so a missing, swapped or constant-tied connection in emu.sv is invisible to
both, and emu.sv has no bench. This reads the connection out of the file.

What is checked:
  * the `dvd_iso_reader` instantiation connects `.chap_edge` and
    `.chap_edge_dir` to two DIFFERENT plain nets;
  * the `dvd_vm` instantiation connects `.key_chedge` / `.key_chedge_dir` to
    those SAME nets, in the same order (a swap turns every Next into a Prev);
  * both nets are declared 1-bit `wire`s and nothing else assigns them;
  * the reader's `.vm_mode` and the VM's `.enable` are the same net, so the
    reader only emits when the VM that consumes the pulse is running.

    python3 tools/check_chap_edge_wiring.py [emu.sv]   # exit 0 = wired right
    python3 tools/check_chap_edge_wiring.py --red      # self-test: each
                                                       # miswiring must FAIL

Walks to each instantiation's matching ')' and strips comments first -- do
NOT "simplify" it to a grep: emu.sv carries commented-out history, and the
comments beside these ports quote the port names.
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


IDENT = re.compile(r'^[A-Za-z_]\w*$')


def check(raw):
    src = strip_comments(raw)
    bad = []
    rd = instantiation_body(src, 'dvd_iso_reader')
    vm = instantiation_body(src, 'dvd_vm')
    if rd is None:
        bad.append('no dvd_iso_reader instantiation found')
    if vm is None:
        bad.append('no dvd_vm instantiation found')
    if bad:
        return bad

    pairs = (('chap_edge', 'key_chedge'), ('chap_edge_dir', 'key_chedge_dir'))
    nets = []
    for rport, vport in pairs:
        rn, vn = port_net(rd, rport), port_net(vm, vport)
        if rn is None:
            bad.append('dvd_iso_reader has no .%s connection' % rport)
        elif not IDENT.match(rn):
            bad.append("dvd_iso_reader .%s is '%s' -- must be a plain net" % (rport, rn))
        if vn is None:
            bad.append('dvd_vm has no .%s connection (the edge key never reaches the VM)' % vport)
        elif not IDENT.match(vn):
            bad.append("dvd_vm .%s is '%s' -- must be a plain net, not a constant" % (vport, vn))
        if rn and vn and IDENT.match(rn) and IDENT.match(vn) and rn != vn:
            bad.append('dvd_iso_reader .%s (%s) and dvd_vm .%s (%s) are different nets'
                       % (rport, rn, vport, vn))
        nets.append(rn)
    if nets[0] and nets[1] and nets[0] == nets[1]:
        bad.append('.chap_edge and .chap_edge_dir share one net (%s)' % nets[0])

    for net in nets:
        if not net or not IDENT.match(net):
            continue
        if not re.search(r'(?m)^\s*wire\s+' + re.escape(net) + r'\s*;', src):
            bad.append("net '%s' is not declared as a 1-bit wire" % net)
        if re.search(r'(?m)^\s*assign\s+' + re.escape(net) + r'\b', src):
            bad.append("net '%s' is also driven by an assign" % net)

    vm_mode, enable = port_net(rd, 'vm_mode'), port_net(vm, 'enable')
    if vm_mode is None or enable is None:
        bad.append('reader .vm_mode or VM .enable is unconnected')
    elif vm_mode != enable:
        bad.append("reader .vm_mode (%s) and VM .enable (%s) differ: the reader would emit "
                   "edges a disabled VM drops, or keep clamping while the VM runs" % (vm_mode, enable))
    return bad


def red(raw):
    """Each plausible miswiring must fail the check."""
    cases = [
        ('VM key tied to 0', r'\.key_chedge\s*\(\s*chap_edge_w\s*\)', ".key_chedge    (1'b0)"),
        ('VM key port dropped', r'\.key_chedge\s*\(\s*chap_edge_w\s*\)\s*,', ''),
        ('dir swapped with the pulse',
         r'\.key_chedge_dir\s*\(\s*chap_edge_dir_w\s*\)', '.key_chedge_dir(chap_edge_w)'),
        ('reader port dropped', r'\.chap_edge\s*\(\s*chap_edge_w\s*\)\s*,', ''),
        ('VM enable not the reader vm_mode', r'\.enable\s*\(\s*menus_on\s*\)', ".enable        (1'b1)"),
    ]
    ok = True
    for name, pat, sub in cases:
        mut, n = re.subn(pat, sub, raw, count=1)
        if n == 0:
            print('RED STALE: %s (pattern matched nothing)' % name)
            ok = False
        elif not check(mut):
            print('RED MISS: %s passed the check' % name)
            ok = False
        else:
            print('red ok: %s -> FAIL' % name)
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
    print('OK: dvd_iso_reader .chap_edge/.chap_edge_dir -> dvd_vm .key_chedge/.key_chedge_dir '
          '(1-bit wires, reader vm_mode == VM enable)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
