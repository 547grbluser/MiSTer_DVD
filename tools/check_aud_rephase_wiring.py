#!/usr/bin/env python3
"""check_aud_rephase_wiring.py -- the audio decoder's stale-latch request reaches aud_resync.

WHY THIS IS A SCRIPT AND NOT A BENCH (2026-09-29, docs/nonseamless_audio.md 4a step 6)
------------------------------------------------------------------------------------
The in-band re-time (dvd/dvd_audio_decode.sv) replaced the display-time audio reset
at every content discontinuity. It keeps ONE reset: when a re-time head is already
latched and has gone stale on the clock's own timeline, only aud_resync can drop what
is already in the decoder, so the decoder raises `resync_req` and emu.sv routes it to
flush_ctl's `aud_rephase_req`. That input used to be `disc_rephase`, the DISPLAY's
content-jump pulse (`aud_disc_rephase`), which still exists in emu.sv and is now inert.

The seam is two named port connections in emu.sv, which has no bench; aud_retime_tb
drives resync_req into a MODEL of the flush and cannot see a wrong wire. Two wrong
wirings both compile and both pass every module bench:
  - flush_ctl.aud_rephase_req <- aud_disc_rephase (the old display pulse): brings back
    the head-discard at every non-seamless join (~1.3 s lost per join).
  - flush_ctl.aud_rephase_req left unconnected / tied 0: the ULTIMATE_T2 stale-latch
    rescue silently never fires.

Checks, on comment-stripped source (comments here quote the old wiring):
  1. dvd_audio_decode's `.resync_req(NET)` and flush_ctl's `.aud_rephase_req(NET)` name
     the SAME net, and it is not aud_disc_rephase.
  2. Nothing else drives NET (no `assign NET =`, no second `.resync_req`).
  3. dvd/flush_ctl.sv ORs aud_rephase_req into the aud_resync trigger.
  4. THE SEAMLESS STAMP (step 7, HW round 2): audio_ring's `.aud_frame_seamless` reads
     the reader's `cell_seamless`, and the ring's `.frame_seamless` output and the
     decoder's `.frame_seamless` input name the same net with no other driver. Every
     chain bench ties the ring input 0, so a tie-0 or a wrong net in emu.sv passes the
     whole suite and silently brings back the Matrix white-rabbit gap + 0.2 s lag.
  5. THE ORPHAN TRIGGER (step 8, HW round 3): dvd_audio_decode's `.anchor_disc` is a net
     assigned from `rephase_req`, and rephase_req is the display's CONTENT-jump flag
     (`av_anchor_delta_valid && av_anchor_delta_w[34]`). Not a tie-off (T2's menu entry
     goes back to ~0.7 s late), and not the raw `av_anchor_pulse`: that also fires on the
     first anchor after a flush, which would orphan -- and release early -- a startup
     latch, putting a whole title's audio early.
A lookup that finds nothing, or two of something, is a NAMED FAIL, never a skip.

Exit 0 = wired as designed; 1 = a named failure. Optional argv[1]/argv[2] = emu.sv /
flush_ctl.sv to check instead, so a runner can mutate copies and never touch the tree.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def instance_body(src, module):
    """The port list of every `<module> [#(...)] <inst> ( ... );` instance."""
    bodies = []
    for m in re.finditer(r'\b' + re.escape(module) + r'\b\s*(#\s*\()?', src):
        i = m.end()
        if m.group(1):                       # skip a #( ... ) parameter list
            depth = 1
            while i < len(src) and depth:
                depth += {'(': 1, ')': -1}.get(src[i], 0)
                i += 1
        im = re.match(r'\s*([A-Za-z_]\w*)\s*\(', src[i:])
        if not im:
            continue
        j = i + im.end()
        depth = 1
        k = j
        while k < len(src) and depth:
            depth += {'(': 1, ')': -1}.get(src[k], 0)
            k += 1
        bodies.append(src[j:k - 1])
    return bodies


def port_net(body, port):
    return [re.sub(r'\s+', '', m.group(1))
            for m in re.finditer(r'\.' + re.escape(port) + r'\s*\(([^()]*)\)', body)]


def main():
    emu = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'dvd', 'emu.sv')
    fc = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, 'dvd', 'flush_ctl.sv')
    src = strip_comments(open(emu).read())
    fsrc = strip_comments(open(fc).read())
    fails = []

    dec = instance_body(src, 'dvd_audio_decode')
    flc = instance_body(src, 'flush_ctl')
    if len(dec) != 1:
        fails.append(f'expected ONE dvd_audio_decode instance in emu.sv, found {len(dec)}')
    if len(flc) != 1:
        fails.append(f'expected ONE flush_ctl instance in emu.sv, found {len(flc)}')
    if not fails:
        src_net = port_net(dec[0], 'resync_req')
        dst_net = port_net(flc[0], 'aud_rephase_req')
        if len(src_net) != 1 or not src_net[0]:
            fails.append(f'dvd_audio_decode .resync_req: expected one named net, found {src_net}')
        if len(dst_net) != 1 or not dst_net[0]:
            fails.append(f'flush_ctl .aud_rephase_req: expected one named net, found {dst_net}')
        if port_net(flc[0], 'disc_rephase'):
            fails.append('flush_ctl still has a .disc_rephase connection (the retired display pulse)')
        if not fails:
            net = src_net[0]
            if dst_net[0] != net:
                fails.append(f'flush_ctl.aud_rephase_req reads "{dst_net[0]}", '
                             f'but the decoder drives "{net}"')
            if dst_net[0] == 'aud_disc_rephase':
                fails.append('flush_ctl.aud_rephase_req is wired to the DISPLAY pulse '
                             '(aud_disc_rephase): the head-discard at every join is back')
            if re.search(r'\bassign\s+' + re.escape(net) + r'\b', src):
                fails.append(f'"{net}" has an assign driver besides the decoder port')
            if len(re.findall(r'\.resync_req\s*\(\s*' + re.escape(net) + r'\s*\)', src)) != 1:
                fails.append(f'"{net}" is driven by more than one .resync_req')

    rng = instance_body(src, 'audio_ring')
    if len(rng) != 1:
        fails.append(f'expected ONE audio_ring instance in emu.sv, found {len(rng)}')
    elif len(dec) == 1:
        s_in = port_net(rng[0], 'aud_frame_seamless')
        s_out = port_net(rng[0], 'frame_seamless')
        d_in = port_net(dec[0], 'frame_seamless')
        if s_in != ['cell_seamless']:
            fails.append(f'audio_ring.aud_frame_seamless reads {s_in}, not the reader\'s cell_seamless '
                         '(a tie-off here brings the Matrix seamless regression back)')
        if len(s_out) != 1 or not s_out[0] or len(d_in) != 1 or s_out != d_in:
            fails.append(f'audio_ring.frame_seamless {s_out} and dvd_audio_decode.frame_seamless '
                         f'{d_in} are not the same named net')
        elif re.search(r'\bassign\s+' + re.escape(s_out[0]) + r'\b', src):
            fails.append(f'"{s_out[0]}" has an assign driver besides audio_ring')

    if len(dec) == 1:
        ad = port_net(dec[0], 'anchor_disc')
        if len(ad) != 1 or not re.match(r'^[A-Za-z_]\w*$', ad[0] or ''):
            fails.append(f'dvd_audio_decode.anchor_disc: expected one named net, found {ad} '
                         '(a tie-off brings back the T2 menu-entry delay)')
        else:
            drv = re.findall(r'\bassign\s+' + re.escape(ad[0]) + r'\s*=\s*([^;]*);', src)
            if drv != ['rephase_req']:
                fails.append(f'"{ad[0]}" (dvd_audio_decode.anchor_disc) is driven by {drv}, not rephase_req '
                             '(the display CONTENT-jump flag; av_anchor_pulse would orphan startup latches)')
            rq = re.findall(r'\bwire\s+rephase_req\s*=\s*([^;]*);', src)
            if len(rq) != 1 or set(re.findall(r'[A-Za-z_]\w*', rq[0])) != {'av_anchor_delta_valid', 'av_anchor_delta_w'} \
                    or '[34]' not in rq[0].replace(' ', ''):
                fails.append(f'rephase_req is {rq}, not av_anchor_delta_valid && av_anchor_delta_w[34]')

    trig = re.search(r'else\s+if\s*\(([^;]*?)\)\s*aud_resync_cnt\s*<=', fsrc)
    if not trig:
        fails.append('flush_ctl.sv: no `else if (...) aud_resync_cnt <=` trigger found')
    elif 'aud_rephase_req' not in set(re.findall(r'[A-Za-z_]\w*', trig.group(1))):
        fails.append(f'flush_ctl.sv: aud_resync trigger "{trig.group(1).strip()}" '
                     'does not include aud_rephase_req')

    if fails:
        for f in fails:
            print('FAIL: ' + f)
        return 1
    print('PASS: check_aud_rephase_wiring (decoder resync_req -> flush_ctl.aud_rephase_req -> aud_resync; '
          'cell_seamless -> audio_ring stamp -> decoder; rephase_req -> anchor_disc)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
