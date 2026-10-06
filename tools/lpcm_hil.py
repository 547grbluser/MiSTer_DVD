#!/usr/bin/env python3
"""lpcm_hil.py -- the hardware check of docs/lpcm_full.md: play test VOBs on the rig,
capture the HDMI audio, score what came out.

Each VOB is copied to the board's /tmp under one fixed name (/media/fat is nearly full),
the capture starts BEFORE the launch (the walk begins 0.5 s into the file; capturing
the card touches nothing on the board, unlike a screenshot), and the capture is scored:

  t_<fs>_<nch>_<bits>.vob    tools/lpcm_vob.py's speaker walk: each channel where the
                             downmix puts it, and at 96 kHz no 30 kHz alias
  t_ac3_dualmono.vob         AC-3 1+1 (tools/ac3_dualmono.py --secs 8): 440 Hz on the
                             left only, 1 kHz on the right only

Run the CONTROL arm first (the current main build deployed): it must FAIL on the formats
this branch adds -- that proves the check can see the difference on this rig.

    tools/lpcm_hil.py .sim/lpcm_vob/t_*.vob [--link96] [--secs 22]
"""
import argparse
import math
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mister as M          # noqa: E402
import lpcm_vob as V        # noqa: E402

REMOTE = '/tmp/hil_lpcm.vob'


def score_dualmono(wav):
    import numpy as np
    import wave
    with wave.open(wav, 'rb') as w:
        rate = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(float) / 32768
    a = a.reshape(-1, 2)
    env = np.sqrt((a ** 2).mean(axis=1))
    on = next((i for i in range(0, len(env) - 2400, 2400) if env[i:i + 2400].mean() > 0.01), None)
    if on is None:
        print('FAIL: the capture is silent'); return False
    seg = a[on + rate // 2:on + 4 * rate]

    def db(x, hz):
        win = np.hanning(len(x))
        f = np.fft.rfft(x * win)
        k = int(round(hz * len(x) / rate))
        e = np.abs(f[k - 2:k + 3]).max() * 2 / win.sum()
        return 20 * math.log10(e) if e > 1e-9 else -180.0
    l440, l1k, r440, r1k = db(seg[:, 0], 440), db(seg[:, 0], 1000), db(seg[:, 1], 440), db(seg[:, 1], 1000)
    ok = l440 > -40 and r1k > -40 and l1k < l440 - 50 and r440 < r1k - 50
    print(f'{"ok  " if ok else "FAIL"} 1+1: left 440 Hz {l440:.1f} dB (1 kHz {l1k:.1f}), '
          f'right 1 kHz {r1k:.1f} dB (440 Hz {r440:.1f})')
    print('dualmono score: ' + ('PASS' if ok else 'FAIL'))
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('vobs', nargs='+')
    ap.add_argument('--link96', action='store_true', help='the rig runs hdmi_audio_96k=1')
    ap.add_argument('--secs', type=float, default=22.0, help='capture length (launch included)')
    ap.add_argument('--keep', help='directory to keep the captures in')
    a = ap.parse_args()
    _, adev, afmt = M.capture_devices()
    if not adev:
        sys.exit('lpcm_hil: no capture card found')
    out = a.keep or tempfile.mkdtemp(prefix='lpcm_hil_')
    os.makedirs(out, exist_ok=True)
    results = []
    for vob in a.vobs:
        name = os.path.basename(vob)
        print(f'== {name}')
        M.scp(vob, REMOTE)
        wav = os.path.join(out, name + '.wav')
        cap = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                                '-f', afmt, '-ac', '2', '-ar', '48000', '-i', adev,
                                '-t', str(a.secs), wav])
        time.sleep(1.5)                                  # the capture's own start-up
        M.cmd_launch(argparse.Namespace(image=REMOTE, opt=[], delay=2, timeout=90, no_wait=False))
        cap.wait(timeout=a.secs + 60)
        if not os.path.exists(wav):
            print('FAIL: no capture'); results.append((name, False)); continue
        m = re.match(r't_(\d+)_(\d)_(\d+)\.vob$', name)
        if m:
            ok = V.score(wav, int(m.group(1)), int(m.group(2)), a.link96)
        elif 'dualmono' in name:
            ok = score_dualmono(wav)
        else:
            print('skip: no scorer for this name'); ok = False
        results.append((name, ok))
    M.ssh(f'rm -f {REMOTE}\n', check=False)
    print('\n'.join(f'{"PASS" if ok else "FAIL"} {n}' for n, ok in results))
    print(f'captures: {out}')
    return 0 if all(ok for _, ok in results) else 1


if __name__ == '__main__':
    sys.exit(main())
