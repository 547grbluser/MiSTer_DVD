#!/usr/bin/env python3
"""check_dts_wiring.py -- the DTS seams in dvd/emu.sv and sys/sys_top.v, read out of the
files (emu.sv has no bench: a module bench is handed the value and cannot see a wrong
wire). docs/dts_decoder.md D4 (P2) and P3.

Claims:
  [ram2]   sys_top's ram2 is emu's second master (DDRAM2_*), on the core's clock; ddr_svc
           no longer drives it (its outputs go to svc_* nets, its inputs read idle)
  [copy]   emu instantiates dts_cb_mem on clk_sys, its DDR3 port on DDRAM2_*, reading the
           ring's out_byte; the three hosts carry the generated codebook images (the
           ring's CB_INIT, dvd_audio_decode's LPCM_INIT / MP2_INIT, which reach
           lpcm_unpack's and cb_host_ram's CB_INIT: mp2_decode's old FIFO, kept when
           MP2 moved onto the engine); the copy advances only on
           aud_rst_n (hosts_ready)
  [hold]   while it copies the audio path is idle: no ring writes, the decoder parked,
           and it reads the hosts in copy mode; once after it the hosts are reset
           (the ring's and the decoder's rst_n take ~cb_host_rst)
  [dts]    the decoder's codebook port is dts_cb_mem's and DTS is gated by its tables_ok
  [telem]  the Main reads 31 words and trusts 26..30 only behind word 25 == the
           marker dvd_telem sends (D4 rule 2: the copy's verdict is never silent)
Usage: python3 tools/check_dts_wiring.py      (exit 0 = PASS)
"""
import os
import re
import sys

REPO = os.environ.get('CHECK_ROOT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = 0


def ok(cond, what):
    global fails
    print(f'  {"ok  " if cond else "FAIL"} {what}')
    fails += not cond


def strip_comments(src):
    """Blank out // and /* */ comments, preserving offsets and newlines."""
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith('//', i):
            j = src.find('\n', i)
            j = n if j < 0 else j
            out.append(' ' * (j - i)); i = j
        elif src.startswith('/*', i):
            j = src.find('*/', i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r'[^\n]', ' ', src[i:j])); i = j
        else:
            out.append(src[i]); i += 1
    return ''.join(out)


def read(p):
    return strip_comments(open(os.path.join(REPO, p)).read())


def norm(s):
    return re.sub(r'\s+', '', s)


def instance(src, module, name):
    """The text of `module #(...) name ( ... );`, or ''."""
    m = re.search(r'\b' + module + r'\b\s*(#\s*\((?:[^()]|\([^()]*\))*\))?\s*' + name + r'\s*\(', src)
    if not m:
        return ''
    depth, i = 1, m.end()
    while depth and i < len(src):
        depth += {'(': 1, ')': -1}.get(src[i], 0)
        i += 1
    return norm(src[m.start():i])


def conn(inst, port):
    """The expression connected to .port(...) in a normalised instance, or None."""
    m = re.search(r'\.' + port + r'\(', inst)
    if not m:
        return None
    depth, i = 1, m.end()
    while depth and i < len(inst):
        depth += {'(': 1, ')': -1}.get(inst[i], 0)
        i += 1
    return inst[m.end():i - 1]


def main():
    st = read('sys/sys_top.v')
    emu = read('dvd/emu.sv')
    dec = read('dvd/dvd_audio_decode.sv')
    ctl = read('main/support/dvd/dvd_ctl.cpp')
    tel = read('dvd/dvd_telem.sv')

    print('[ram2]')
    sm = instance(st, 'sysmem_lite', 'sysmem')
    ok(conn(sm, 'ram2_clk') == 'ram2_clk', "sysmem_lite.ram2_clk is ram2_clk (emu's DDRAM2_CLK)")
    em = instance(st, 'emu', 'emu')
    for p, n in [('DDRAM2_CLK', 'ram2_clk'), ('DDRAM2_ADDR', 'ram2_address'),
                 ('DDRAM2_BURSTCNT', 'ram2_burstcount'), ('DDRAM2_BUSY', 'ram2_waitrequest'),
                 ('DDRAM2_DOUT', 'ram2_readdata'), ('DDRAM2_DOUT_READY', 'ram2_readdatavalid'),
                 ('DDRAM2_RD', 'ram2_read'), ('DDRAM2_DIN', 'ram2_writedata'),
                 ('DDRAM2_BE', 'ram2_byteenable'), ('DDRAM2_WE', 'ram2_write')]:
        ok(conn(em, p) == n, f'emu.{p} is {n}')
        ok(conn(sm, n.replace('ram2_', 'ram2_')) is not None or p == 'DDRAM2_CLK', f'sysmem_lite has {n}')
    ds = instance(st, 'ddr_svc', 'ddr_svc')
    ok(ds and not any(re.search(r'\(ram2_(address|burstcount|read|write|writedata|byteenable)\)', ds)
                      for _ in [0]),
       'ddr_svc drives none of the ram2 nets')
    ok(conn(ds, 'ram_waitrequest') == "1'b1", 'ddr_svc reads an idle port (waitrequest tied high)')

    print('[copy]')
    cb = instance(emu, 'dts_cb_mem', 'dts_cb_mem_inst')
    ok(bool(cb), 'emu instantiates dts_cb_mem')
    ok(conn(cb, 'clk') == 'clk_sys', 'on clk_sys')
    ok(norm('assign DDRAM2_CLK = clk_sys;') in norm(emu), 'DDRAM2_CLK = clk_sys')
    for p, n in [('ddr_addr', 'DDRAM2_ADDR'), ('ddr_burstcnt', 'DDRAM2_BURSTCNT'),
                 ('ddr_read', 'DDRAM2_RD'), ('ddr_write', 'DDRAM2_WE'), ('ddr_wdata', 'DDRAM2_DIN'),
                 ('ddr_be', 'DDRAM2_BE'), ('ddr_busy', 'DDRAM2_BUSY'), ('ddr_rdata', 'DDRAM2_DOUT'),
                 ('ddr_rvalid', 'DDRAM2_DOUT_READY')]:
        ok(conn(cb, p) == n, f'dts_cb_mem.{p} is {n}')
    ok(conn(cb, 'hosts_ready') == 'aud_rst_n', 'the copy advances on aud_rst_n')
    ring = instance(emu, 'audio_ring', 'audio_ring_inst')
    ok(conn(cb, 'cp_ring_q') == conn(ring, 'out_byte'), "the copier reads the ring's out_byte")
    ok('.CB_INIT("dvd/dts/cb_host_ring.mem")' in ring, 'the ring carries cb_host_ring.mem')
    ok(conn(ring, 'cp_step') == conn(cb, 'cp_ring_step'), "the ring's cp_step is the copier's")
    ad = instance(emu, 'dvd_audio_decode', 'dvd_audio_decode_inst')
    ok('.LPCM_INIT("dvd/dts/cb_host_lpcm.mem")' in ad and '.MP2_INIT("dvd/dts/cb_host_mp2.mem")' in ad,
       'the decoder carries cb_host_lpcm.mem / cb_host_mp2.mem')
    lp = instance(dec, 'lpcm_unpack', 'lpcm_unpack_inst')
    mp = instance(dec, 'cb_host_ram', 'cb_host_mp2_inst')
    ok('.CB_INIT(LPCM_INIT)' in lp and '.CB_INIT(MP2_INIT)' in mp,
       '...which reach lpcm_unpack and cb_host_ram')
    for p in ('cb_lpcm_step', 'cb_mp2_step', 'cb_lpcm_q', 'cb_mp2_q'):
        ok(conn(ad, p) is not None and conn(ad, p) == conn(cb, p.replace('cb_', 'cp_')),
           f"the decoder's {p} is the copier's")
    ok(conn(lp, 'cp_step') == 'cb_lpcm_step' and conn(lp, 'cp_mode') == 'cb_cp_mode', 'lpcm_unpack in copy mode')
    ok(conn(mp, 'cp_step') == 'cb_mp2_step' and conn(mp, 'cp_q') == 'cb_mp2_q', 'cb_host_ram in copy mode')
    for f in ('cb_host_ring.mem', 'cb_host_lpcm.mem', 'cb_host_mp2.mem', 'dts_cb.svh'):
        ok(os.path.exists(os.path.join(REPO, 'dvd', 'dts', f)), f'dvd/dts/{f} exists')

    print('[hold]')
    busy = conn(cb, 'busy')
    ok(conn(ring, 'aud_valid') == f'rf_aud_valid&~{busy}', 'no ring writes while it copies')
    ok(conn(ad, 'enable') == f'aud_dec_en&~{busy}', 'the decoder parked while it copies')
    ok(conn(ad, 'cb_cp_mode') == busy, "the decoder's FIFOs in copy mode while it copies")
    hr = conn(cb, 'host_rst')
    ok(conn(ring, 'rst_n') == f'aud_rst_n&~{hr}', "the ring is reset once after the copy")
    ok(conn(ad, 'rst_n') == f'aud_rst_n&~{hr}', 'the decoder is reset once after the copy')

    print('[dts]')
    for p in ('cb_req', 'cb_sel', 'cb_addr', 'cb_valid', 'cb_data'):
        ok(conn(ad, p) is not None and conn(ad, p) == conn(cb, p), f"the decoder's {p} is dts_cb_mem's")
    ok(conn(ad, 'dts_tables_ok') == conn(cb, 'tables_ok'), 'DTS is gated by the copy\'s tables_ok')

    print('[telem]')
    mw = re.search(r'uint16_t\s+w\[\s*(\d+)\s*\]', ctl)
    ok(mw is not None and int(mw.group(1)) >= 31, 'the Main reads 31 words')
    mm = re.search(r"AUD_MAGIC\s*=\s*16'h([0-9A-Fa-f]+)", tel)
    mc = re.search(r'#define\s+DVD_TELEM_AUD_MAGIC\s+0x([0-9A-Fa-f]+)', ctl)
    ok(mm and mc and int(mm.group(1), 16) == int(mc.group(1), 16), 'the word-25 marker agrees')
    ok(re.search(r'w\[25\]\s*==\s*DVD_TELEM_AUD_MAGIC', ctl) is not None,
       'words 26..30 are trusted only behind it')
    tl = instance(emu, 'dvd_telem', 'dvd_telem_inst')
    ok(conn(tl, 'cb_sum') == conn(cb, 'sum_seen'), "word 27/28 is the copy's checksum")
    df = conn(tl, 'dts_flags') or ''
    ok(df.endswith(f"{conn(cb, 'tables_ok')},~{busy}}}"), 'word 26 carries tables_ok and the copied flag')

    print(f'RESULT: {"PASS" if not fails else f"FAIL ({fails})"}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
