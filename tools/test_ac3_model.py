#!/usr/bin/env python3
"""test_ac3_model.py -- gate: tools/ac3_model.py is bit-exact with dvd/ac3/'s RTL.

Claims:
  [1] for every stream, the model's per-block output -- coeff_mem (Q1.23, every
      channel and bin, the LFE slot), blksw, dynrng, and the frame's acmod / lfeon
      / mix levels -- is IDENTICAL to the RTL's (bench/ac3/golden_main.cpp, the
      Verilated front end; regenerated here from the current RTL each run);
  [2] the model refuses exactly the frames the RTL refuses.
RED arms: mutations of the model, each of which must make [1] fail on at least
one stream. An arm no stream catches is a FAIL: either the stream set lacks the
feature or the comparison cannot see it.

Streams: tools/streams/*.ac3 (tools/gen_test_stream.sh), bench/ac3/vectors/*.ac3,
and every *.ac3 under $AC3_TEST_DIR (disc windows from tools/ac3_scan.py
--extract: local, never committed).
Usage: python3 tools/test_ac3_model.py [--frames N]      (exit 0 = PASS)
"""
import argparse
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import ac3_model as M  # noqa: E402

GOLD_BIN = os.path.join(REPO, 'bench', 'ac3', 'obj_golden', 'ac3_golden')
GOLD_DIR = os.path.join(REPO, '.sim', 'ac3', 'gold')


def streams():
    s = sorted(glob.glob(os.path.join(REPO, 'tools', 'streams', '*.ac3')))
    s += sorted(glob.glob(os.path.join(REPO, 'bench', 'ac3', 'vectors', '*.ac3')))
    d = os.environ.get('AC3_TEST_DIR')
    if d:
        s += sorted(glob.glob(os.path.join(os.path.expanduser(d), '**', '*.ac3'), recursive=True))
    return s


def goldens(paths, nframes):
    subprocess.run(['make', '-s', '-f', 'Makefile.golden'], cwd=os.path.join(REPO, 'bench', 'ac3'),
                   check=True, stdout=subprocess.DEVNULL)
    os.makedirs(GOLD_DIR, exist_ok=True)
    env = dict(os.environ, AC3_MAX_FRAMES=str(nframes))
    procs, out = [], {}
    for p in paths:
        g = os.path.join(GOLD_DIR, os.path.basename(p) + '.gold')
        out[p] = g
        procs.append(subprocess.Popen([GOLD_BIN, p], stdout=open(g, 'w'),
                                      stderr=subprocess.DEVNULL, env=env))
    for pr in procs:
        pr.wait()
    return out


# RED arms: name -> (patch function, restore function)
def _arms():
    o = dict(dither=M.dither_coeff, recomb=M.recombine, remat=M.Decoder.rematrix,
             m16=M.Mantissas.m16, cplco=M.cplco_q518, ungroup=M.ungroup_exps,
             cplpass=M.Decoder.coupling, alloc=M.bit_allocate)

    def m16_cache_order(self, bap):            # grouped digits low first (DTS's order)
        if bap == -1 and not self.q1:
            c = self.br.bits(5)
            self.q1 = [M.Q1LEV[c // 9], M.Q1LEV[(c // 3) % 3]]
            return M.Q1LEV[c % 3]
        return o['m16'](self, bap)

    def no_phs(self, mq, nf, bap, dith, coeff):  # the phase flags ignored
        saved = list(self.phsneg)
        self.phsneg = [0] * len(saved)
        try:
            o['cplpass'](self, mq, nf, bap, dith, coeff)
        finally:
            self.phsneg = saved

    def alloc_no_lowcomp(g, bai, deltba, bndstart, start, end, fl, sl, exp):
        return o['alloc'](g, bai, deltba, bndstart, start, end, fl, sl,
                          exp if start else [e for e in exp])
    return {
        'dither_round':   (lambda: setattr(M, 'dither_coeff', lambda l, e: M.s24(
                            (M.s16(l) * 23170) >> (7 + e)) if 7 + e <= 30 else 0),
                           lambda: setattr(M, 'dither_coeff', o['dither'])),
        'recombine_nosat': (lambda: setattr(M, 'recombine', lambda cc, co: M.s24((cc * co) >> 18)),
                            lambda: setattr(M, 'recombine', o['recomb'])),
        'recombine_shift': (lambda: setattr(M, 'recombine', lambda cc, co: M.sat24((cc * co) >> 17)),
                            lambda: setattr(M, 'recombine', o['recomb'])),
        'remat_off':      (lambda: setattr(M.Decoder, 'rematrix', lambda self, c: None),
                           lambda: setattr(M.Decoder, 'rematrix', o['remat'])),
        'grouped_lowfirst': (lambda: setattr(M.Mantissas, 'm16', m16_cache_order),
                             lambda: setattr(M.Mantissas, 'm16', o['m16'])),
        'cplco_rounding': (lambda: setattr(M, 'cplco_q518', lambda e, m, s: o['cplco'](e, m, s) | 1),
                           lambda: setattr(M, 'cplco_q518', o['cplco'])),
        'phsflg_ignored': (lambda: setattr(M.Decoder, 'coupling', no_phs),
                           lambda: setattr(M.Decoder, 'coupling', o['cplpass'])),
    }


def run(paths, gold, nframes):
    res = {}
    for p in paths:
        try:
            res[p] = M.compare(p, gold[p], nframes)
        except M.Ac3Error as e:
            res[p] = (0, 0, 1, f'model error {e}')
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--frames', type=int, default=12)
    a = ap.parse_args()
    paths = streams()
    if not paths:
        print('test_ac3_model: no streams (tools/gen_test_stream.sh, $AC3_TEST_DIR)')
        print('RESULT: FAIL')
        return 1
    gold = goldens(paths, a.frames)
    fails = 0
    base = run(paths, gold, a.frames)
    for p, (nf, nb, bad, first) in base.items():
        ok = bad == 0 and nb > 0
        fails += not ok
        print(f'[1] {"PASS" if ok else "FAIL"} {os.path.basename(p)}: {nf} frames, {nb} blocks, '
              f'{bad} values differ' + (f' (first: {first})' if first else ''))
    for name, (patch, restore) in _arms().items():
        patch()
        try:
            r = run(paths, gold, a.frames)
        finally:
            restore()
        bit = [os.path.basename(p) for p, v in r.items() if v[2] and not base[p][2]]
        print(f'    RED {name:18s} ' + (f'bites on {len(bit)} stream(s)' if bit else 'NEVER: FAIL'))
        fails += not bit
    print(f'RESULT: {"PASS" if not fails else "FAIL"}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
