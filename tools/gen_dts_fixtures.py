#!/usr/bin/env python3
"""gen_dts_fixtures.py -- synthetic DTS core streams for the DTS gates.

The library's discs (tools/dts_scan.py) never exercise some of what the format
allows -- Huffman-coded sample codes, for one -- but the decoder must still be
right on them (CLAUDE.md "Design to the DVD spec maximum"). FFmpeg's DTS
encoder does produce some of those features, from generated signals, so these
fixtures are free of any disc content: unlike the library rips they could be
committed, but they are regenerated instead (they depend on the encoder).

Each fixture is named for what it is for. tools/test_dts_ref.py and
tools/test_dts_fixed.py read them from DTS_TEST_DIR alongside any rips.

Usage: tools/gen_dts_fixtures.py [OUT_DIR]     (default: $DTS_TEST_DIR or ~/dts-streams)
"""
import os
import subprocess
import sys

# (name, channel layout, bit rate, ADPCM on): a spread of channel modes and
# rates; the low rates push the encoder to Huffman-code its samples.
FIXTURES = [
    ('synth_stereo_320k', 'stereo', '320k', 0),
    ('synth_stereo_768k', 'stereo', '768k', 0),
    ('synth_mono_192k', 'mono', '192k', 0),
    ('synth_quad_640k_adpcm', 'quad(side)', '640k', 1),
    ('synth_50side_768k_adpcm', '5.0(side)', '768k', 1),
    ('synth_51side_768k_adpcm', '5.1(side)', '768k', 1),
    ('synth_51side_1536k', '5.1(side)', '1536k', 0),
]
# (the encoder's floors, measured on n9.0.2: mono 192k, stereo 320k, quad 640k,
# 5.x 768k)
SECONDS = 4
CHANNELS = {'mono': 1, 'stereo': 2, 'quad(side)': 4, '5.0(side)': 5, '5.1(side)': 6}


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        'DTS_TEST_DIR', os.path.expanduser('~/dts-streams'))
    os.makedirs(out, exist_ok=True)
    for name, layout, rate, adpcm in FIXTURES:
        path = os.path.join(out, name + '.dts')
        # one expression per channel, each different (a channel identical to
        # another, or silent, would hide channel-mapping and sum/diff faults):
        # noise + a per-channel tone + a gated burst (transients); LFE a 60 Hz tone
        nch = CHANNELS[layout]
        exprs = []
        for k in range(nch):
            if layout.startswith('5.1') and k == 3:
                exprs.append('0.5*sin(2*PI*60*t)')
                continue
            exprs.append(f'0.15*(2*random({k})-1)+0.25*sin(2*PI*{220 + 110 * k}*t)'
                         f'+0.5*sin(2*PI*1500*t)*lt(mod(t+{0.07 * k:.2f}\\,0.5)\\,0.03)')
        src = f"aevalsrc=exprs={'|'.join(exprs)}:c={layout}:s=48000:d={SECONDS}"
        cmd = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', src,
               '-c:a', 'dca', '-strict', '-2', '-dca_adpcm', str(adpcm), '-b:a', rate,
               '-f', 'dts', path]
        subprocess.run(cmd, check=True)
        print(f'gen_dts_fixtures: {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
