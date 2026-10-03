#!/usr/bin/env python3
"""test_mp2_isa.py -- gate for the MP2 engine program and emulator (docs/mp2_engine.md M1).

Claims:
  [1] the emulator, running dvd/dts/mp2.uasm, is bit-exact against tools/mp2_ref.py
      (itself bit-exact with mp2_decode: bench/dvd/run_mp2_model.sh) on every PCM pair
      of every gate stream, refuses nothing, and every MDQ / MSYN equals the model's own
      requantize / synth (mp2_isa.Machine raises otherwise: the RTL's decomposition is
      proved on every op);
  [2] every frame fits BUDGET of real time (1152 / fs at 27 MHz): the sequencer at the
      RTL's own cycle counts, the three new vector ops MODELLED, with CYC_HEADROOM;
  [3] the generated images match the programs (dts_isa.py --asm --check);
  [4] a frame shorter than its header's length is refused (E_LEN), and so are a bad
      sync, Layer I/III, free format and the reserved rate; one LONGER than its header
      (mp2_reframer appends what follows a frame until the next sync) is decoded, the
      rest drained and counted (CNT counter 0).
RED arms: microcode mutations (`;MUT name:` lines in mp2.uasm), each tied to the
stream feature it needs; one that bites on no stream with the feature is a FAIL, one
whose feature no stream has is a GAP.
Streams: every *.mp2 under $MP2_TEST_DIR (default ~/mp2-streams/gate; built by
tools/mp2_scan.py --extract and tools/gen_mp2_streams.sh, never committed).
Usage: python3 tools/test_mp2_isa.py [--frames N] [--jobs N]
"""
import argparse
import glob
import multiprocessing as mp
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dts_isa as D          # noqa: E402
import mp2_isa as P          # noqa: E402
import mp2_ref as R          # noqa: E402

CLK = 27_000_000
BUDGET = 0.60
ARMS = {'nocrc': 'crc', 'mono2': 'mono', 'b2b': 'b2b', 'b2d': 'b2d', 'nobound': 'joint',
        'scfsi3': 'scfsi3', 'noclr': 'alloc_drop', 'ownint': 'joint', 'hifirst': 'grouped'}


def streams():
    root = os.path.expanduser(os.environ.get('MP2_TEST_DIR', '~/mp2-streams/gate'))
    return sorted(glob.glob(os.path.join(root, '**', '*.mp2'), recursive=True))


def features(path, nframes):
    """What a stream exercises, from its own parse (mp2_ref's order up to the scalefactors)."""
    f = {}
    prev = None
    for fr, h in R.iter_frames(open(path, 'rb').read(), nframes):
        get, _ = R._bits(fr)
        get(32)
        if h['protection'] == 0:
            f['crc'] = 1
            get(16)
        nch = 1 if h['mode'] == 3 else 2
        f['mono'] = f.get('mono', 0) or nch == 1
        sbl, tab = R.table_select(h['sample_rate'], h['bitrate'], nch)
        f['b2b'] = f.get('b2b', 0) or sbl == 30
        f['b2d'] = f.get('b2d', 0) or sbl == 12
        bound = min((h['mode_ext'] + 1) * 4, sbl) if h['mode'] == 1 else sbl
        alloc = [[0] * 32 for _ in range(2)]
        for sb in range(sbl):
            if sb < bound:
                for ch in range(nch):
                    alloc[ch][sb] = get(R.nbal_of(tab[sb]))
            else:
                alloc[0][sb] = alloc[1][sb] = get(R.nbal_of(tab[sb]))
                f['joint'] = f.get('joint', 0) or alloc[0][sb] != 0
        for sb in range(sbl):
            for ch in range(nch):
                if alloc[ch][sb]:
                    s = get(2)
                    if s == 3:
                        f['scfsi3'] = 1
                    if R.sample_bits(tab[sb][alloc[ch][sb]])[0]:
                        f['grouped'] = 1
        live = {(ch, sb) for ch in range(nch) for sb in range(sbl) if alloc[ch][sb]}
        if prev is not None and prev - live:
            f['alloc_drop'] = 1
        prev = live
    return f


def run_one(args):
    path, nframes, mutate = args
    try:
        m, want, n = P.emulate(path, nframes, mutate=mutate)
    except D.EngineError as e:
        return path, mutate, None, str(e), 0.0, 0
    bad, first = P.compare(m, want)
    sr = R.MP2Decoder.parse_header(open(path, 'rb').read(4) or b'\0\0\0\0')
    fs = sr['sample_rate'] if sr else 48000
    frac = 0.0
    if m.frame_cycles:
        vec = m.vec_model / max(len(m.frame_cycles) + 1, 1)
        frac = (max(m.frame_cycles) + (P.CYC_HEADROOM - 1) * vec) / (CLK * 1152 / fs)
    err = sum(m.errors.values())
    return path, mutate, bad, (f'first at pair {first}' if first is not None else ''), frac, err


def refusals():
    """[4]: hand-made frames the program must refuse, each with its own code."""
    ok, words, labels = True, *P.load_program()
    base = None
    for p in streams():
        base = next(R.iter_frames(open(p, 'rb').read()), None)
        if base:
            break
    fr = bytearray(base[0])
    cases = {'E_LEN': (bytes(fr[:-1]), 5), 'E_SYNC': (bytes([0xFF, 0x0F]) + bytes(fr[2:]), 1),
             'E_ID': (bytes([fr[0], fr[1] ^ 0x02]) + bytes(fr[2:]), 2),
             'E_BRATE': (bytes(fr[:2]) + bytes([fr[2] | 0xF0]) + bytes(fr[3:]), 3),
             'E_SRATE': (bytes(fr[:2]) + bytes([fr[2] | 0x0C]) + bytes(fr[3:]), 4)}
    for name, (data, code) in cases.items():
        m = P.Machine(words, labels)
        m.feed(data)
        m.run()
        good = m.errors == {code: 1} and not m.pcm[0]
        ok &= good
        print(f'[4] {"PASS" if good else "FAIL"} {name}: errors {m.errors}, {len(m.pcm[0])} pairs')
    for mut in (None, 'lenexact'):            # a long frame: decoded, counted (and RED:
        w, lb = P.load_program(mut)           # an exact-length check refuses it)
        m = P.Machine(w, lb)
        m.feed(bytes(fr) + b'\x12\x34\xff\xf0\x00')
        m.feed(bytes(fr))
        m.run()
        good = not m.errors and len(m.pcm[0]) == 2304 and m.counters == {0: 1}
        if mut:
            ok &= not good
            print(f'    RED {mut:8s} ' + ('bites on the long frame' if not good else 'BLIND: FAIL'))
        else:
            ok &= good
            print(f'[4] {"PASS" if good else "FAIL"} a frame 5 bytes long: {len(m.pcm[0])} pairs, '
                  f'counters {m.counters}, errors {m.errors}')
    m = P.Machine(words, labels)              # and a good frame after a refused one decodes
    m.feed(bytes(fr[:-1]))
    m.feed(bytes(fr))
    m.run()
    good = m.errors == {5: 1} and len(m.pcm[0]) == 1152
    ok &= good
    print(f'[4] {"PASS" if good else "FAIL"} a good frame after a refused one: {len(m.pcm[0])} pairs')
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--frames', type=int, default=24)
    ap.add_argument('--jobs', type=int, default=os.cpu_count())
    a = ap.parse_args()
    fails = 0
    rc = D.write_mems(check=True)
    print(f'[3] {"PASS" if rc == 0 else "FAIL"} the generated images match the programs')
    fails += rc != 0
    paths = streams()
    if not paths:
        print('FAIL: no streams (set MP2_TEST_DIR)')
        return 1
    fails += not refusals()
    with mp.Pool(a.jobs) as pool:
        base = pool.map(run_one, [(p, a.frames, None) for p in paths])
        feat = {p: f for p, f in zip(paths, pool.starmap(features, [(p, a.frames) for p in paths]))}
        worst = (0.0, '')
        clean = set()
        for path, _, bad, why, frac, err in base:
            name = os.path.relpath(path, os.path.dirname(os.path.dirname(path)))
            ok1 = bad == 0 and err == 0
            ok2 = frac <= BUDGET
            fails += (not ok1) + (not ok2)
            if ok1:
                clean.add(path)
            if frac > worst[0]:
                worst = (frac, name)
            if not (ok1 and ok2):
                print(f'[1] {"PASS" if ok1 else "FAIL"} {name}: {bad} mismatches {why}, {err} refused; '
                      f'[2] {"PASS" if ok2 else "FAIL"} worst frame {frac * 100:.0f} %')
        print(f'[1] {len(clean)} of {len(paths)} streams bit-exact vs mp2_ref, every op proved; '
              f'[2] worst frame {worst[0] * 100:.1f} % of real time ({worst[1]})')
        jobs = []
        for arm, need in ARMS.items():
            have = [p for p in paths if feat[p].get(need) and p in clean]
            jobs += [(p, a.frames, arm) for p in have]
        res = pool.map(run_one, jobs)
    for arm, need in ARMS.items():
        mine = [r for r in res if r[1] == arm]
        if not mine:
            print(f'    RED {arm:8s} GAP -- no stream has {need}')
            continue
        bit = sum(1 for r in mine if r[2] is None or r[2] or r[5])
        print(f'    RED {arm:8s} ' + (f'bites on {bit} of {len(mine)} stream(s) with {need}' if bit
                                     else f'BLIND on {len(mine)} stream(s) with {need}: FAIL'))
        fails += not bit
    print(f'RESULT: {"PASS" if not fails else "FAIL"}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
