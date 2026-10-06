#!/usr/bin/env python3
"""Analog Dither A/B on the HIL rig: a static dark ramp, captured off the analog output.

The rig's analog RGB goes through a RetroTINK into the USB capture card, so this measures
what the I/O board's 6-bit DAC actually puts out (docs/single_raster_analog.md §8).

  dither_ab.py ramp [out.mpg] [--secs N]  make the test clip (720x480 MPEG-2 at qscale 1, 180 s):
                                       top half Y 16 -> 64 left to right (grey), bottom half
                                       the same with Cb = 152 (a dark blue ramp)
  dither_ab.py cap <label> [--secs 4]  capture the card -> .sim/dither_cap/<label>.npz
  dither_ab.py score [--ref L] L...    score captures (regions from --ref, default each own)
  dither_ab.py still <label>           one live frame of the grey half, contrast x5 -> PNG

Typical arm (the clip on the rig, Main = MiSTer_DVDcss for live `osd`):
  mister.py launch /media/fat/games/DVD/RAMP_DITHER.mpg --opt "Video Output=Interlaced"
  mister.py osd "Analog Dither=Off"; dither_ab.py cap ctl_480i_off   (then On, then Off again)

Scores (lower = smoother):
  stair  RMS residual of the frame-averaged column profile against a cubic fit: the staircase
         left once the smooth ramp is removed. The blue one also carries the colour matrix's
         and the RetroTINK's curvature, which a cubic does not remove.
  line   per frame, the row means over the ramp, detrended by a quadratic over rows; RMS,
         averaged over frames. The line texture of a dither whose lines do not each average
         to the value. Only resolvable at 480i: the RetroTINK halves 480p's height.

Capture: 1920x1080 uncompressed YUYV at 10 fps, full range (the card's MJPEG mode crushes the
blacks). Set the RetroTINK's deinterlacer to Weave for these static patterns: under Bob the
field shown alternates and moves the image a source line, and if the 10 fps grab catches both
fields the frame average blends two edge positions (seen once as a "softer moon" on one arm
only; every single frame was sharp, and under Weave On and Off were identical). Capture-card traps (resolve it by name; drop its flat no-signal frames) are
in .claude/skills/hil-testing. Check nothing (OBS) holds the video node first.
"""
import os, subprocess, sys, time
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(ROOT, '.sim', 'dither_cap')
W, H = 1920, 1080


def vdev():
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    import mister
    v, _, _ = mister.capture_devices()
    return v or '/dev/video0'


def grab(secs, warm=1.5, fps=None):
    """Live frames (the card's flat RGB-7 no-signal frames dropped), retrying on open."""
    # 10 fps by default. At 480p the dither inverts every frame, so a 60 Hz output sampled at
    # 10 fps (every 6th frame) always shows ONE phase; 20 fps (every 3rd) alternates them, so
    # the average shows what the eye blends. DITHER_FPS overrides.
    fps = fps or int(os.environ.get('DITHER_FPS', '10'))
    for _ in range(4):
        # Uncompressed YUYV, declared FULL range. The card's MJPEG mode crushes everything
        # below ~16/255 to black (measured on a levels pattern: steps 0..14 all read 0 in
        # MJPEG, all distinct in YUYV), and ffmpeg assumes TV range for YUYV, which would
        # crush the same codes again on conversion.
        raw = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'v4l2',
                              '-input_format', 'yuyv422', '-video_size', f'{W}x{H}',
                              '-framerate', str(fps), '-i', vdev(), '-ss', str(warm),
                              '-t', str(secs), '-vf',
                              'scale=in_range=full:out_range=full:in_color_matrix=bt709',
                              '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                             capture_output=True).stdout
        n = len(raw) // (W * H * 3)
        fr = np.frombuffer(raw[:n * W * H * 3], np.uint8).reshape(n, H, W, 3)
        good = [f for f in fr if f.std() >= 1.0]
        if len(good) >= 8:
            return np.stack(good).astype(np.float32), n
        print(f'  only {len(good)}/{n} live frames, retrying')
        time.sleep(1)
    sys.exit('dither_ab: no live signal from the capture card')


def make_ramp(out, secs=180):
    w, h = 720, 480
    yrow = np.round(16 + 48 * np.arange(w) / (w - 1)).astype(np.uint8)
    cb = np.full((h // 2, w // 2), 128, np.uint8)
    cb[h // 4:, :] = 128 + 24
    frame = (np.repeat(yrow[None, :], h, axis=0).tobytes() + cb.tobytes()
             + np.full((h // 2, w // 2), 128, np.uint8).tobytes())
    p = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                          '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-s', f'{w}x{h}',
                          '-r', '30000/1001', '-i', '-', '-c:v', 'mpeg2video',
                          '-qscale:v', '1', '-qmin', '1', '-qmax', '1', '-intra_vlc', '1',
                          '-g', '15', '-bf', '0', '-b:v', '8M', '-maxrate', '9M',
                          '-bufsize', '1835k', '-aspect', '4:3', '-color_primaries', 'smpte170m',
                          '-color_trc', 'smpte170m', '-colorspace', 'smpte170m',
                          '-an', '-f', 'vob', out], stdin=subprocess.PIPE)
    for _ in range(int(secs * 30000 / 1001)):
        p.stdin.write(frame)
    p.stdin.close()
    if p.wait():
        sys.exit('dither_ab: ffmpeg failed')
    print(out)


def cap(label, secs):
    os.makedirs(CAP, exist_ok=True)
    g, n = grab(secs)
    blocks = g.reshape(len(g), H, W // 16, 16, 3).mean(axis=3)    # per-frame, 16-col blocks
    np.savez_compressed(os.path.join(CAP, label + '.npz'), mean=g.mean(axis=0), blocks=blocks)
    print(f'  {label}: {len(g)}/{n} live frames saved')


def regions(mean):
    """Grey rows, blue rows and the ramp's column range, found from the picture itself."""
    mid = slice(int(W * 0.55), int(W * 0.75))
    rows = np.where(mean[:, mid].mean(axis=(1, 2)) > 12)[0]
    blue = (mean[:, mid, 2].mean(axis=1) - mean[:, mid, 0].mean(axis=1)) > 6

    def trim(a):
        a = np.sort(np.array(a))
        k = max(2, len(a) // 8)
        return a[k:-k]
    grey_rows = trim([y for y in rows if not blue[y]])
    blue_rows = trim([y for y in rows if blue[y]])
    prof = mean[grey_rows].mean(axis=(0, 2))
    x1 = np.where(prof > 12)[0].max() - 12              # inside the right edge
    x0 = np.where(prof > 3)[0].min() + 8                # past the black left end
    return grey_rows, blue_rows, x0, x1


def stair(profile):
    x = np.arange(len(profile))
    return float(np.sqrt(np.mean((profile - np.polyval(np.polyfit(x, profile, 3), x)) ** 2)))


def line(blocks, rows, b0, b1, ch):
    vals = []
    for f in blocks:
        sel = f[rows][:, b0:b1]
        rm = sel[..., ch].mean(axis=1) if ch >= 0 else sel.mean(axis=(1, 2))
        y = np.arange(len(rm))
        vals.append(np.sqrt(np.mean((rm - np.polyval(np.polyfit(y, rm, 2), y)) ** 2)))
    return float(np.mean(vals))


def load(label):
    return np.load(os.path.join(CAP, label + '.npz'))


def score(labels, ref):
    print(f'{"capture":30s} {"stair grey":>10s} {"stair blue":>10s} {"line grey":>10s} '
          f'{"line blue":>10s} {"mean grey":>10s}')
    for lab in labels:
        d = load(lab)
        mean, blocks = d['mean'], d['blocks']
        gr, br, x0, x1 = regions(load(ref)['mean'] if ref else mean)
        b0, b1 = x0 // 16 + 1, x1 // 16
        print(f'{lab:30s} {stair(mean[gr][:, x0:x1].mean(axis=(0, 2))):10.3f} '
              f'{stair(mean[br][:, x0:x1, 2].mean(axis=0)):10.3f} '
              f'{line(blocks, gr, b0, b1, -1):10.3f} {line(blocks, br, b0, b1, 2):10.3f} '
              f'{float(mean[gr][:, x0:x1].mean()):10.2f}')


def still(label):
    g, _ = grab(1.0)
    f = g[len(g) // 2]
    gr, _, x0, x1 = regions(g.mean(axis=0))
    crop = f[gr.min():gr.max(), x0:x1]
    out = np.clip((crop - crop.mean()) * 5 + 128, 0, 255).astype(np.uint8)
    os.makedirs(CAP, exist_ok=True)
    path = os.path.join(CAP, label + '.png')
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo',
                    '-pix_fmt', 'rgb24', '-s', f'{out.shape[1]}x{out.shape[0]}', '-i', '-', path],
                   input=out.tobytes(), check=True)
    print(path)


if __name__ == '__main__':
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == 'ramp':
        secs = float(a[a.index('--secs') + 1]) if '--secs' in a else 180
        pos = [x for x in a[1:] if x != '--secs' and x != a[a.index('--secs') + 1]] if '--secs' in a else a[1:]
        make_ramp(pos[0] if pos else os.path.join(ROOT, '.sim', 'RAMP_DITHER.mpg'), secs)
    elif a[0] == 'cap':
        cap(a[1], float(a[a.index('--secs') + 1]) if '--secs' in a else 4.0)
    elif a[0] == 'score':
        ref = a[a.index('--ref') + 1] if '--ref' in a else None
        score([x for x in a[1:] if x != '--ref' and x != ref], ref)
    elif a[0] == 'still':
        still(a[1])
    else:
        sys.exit(__doc__)
