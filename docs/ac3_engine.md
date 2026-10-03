# The AC-3 parse on the shared audio engine (scenario E)

**Status (2026-10-02): A0 under way. The golden tap is built; the model
(`tools/ac3_model.py`) is bit-exact against the RTL on all 13 stock streams. The
emulator and any RTL are not built.** Branch `feature/ac3-engine` (from
`feature/dts-decode`, `CORE_VERSION dev-ac3engine`, not pushed).

**Why.** `docs/dts_decoder.md` §4 scenario E estimates that moving MP2 and the AC-3
parse onto the microcoded engine built for DTS (P1, `dvd/dts/`) saves **−1,000 …
−1,450 ALM** and ~40 M10K against today, with no DTS at all. The AC-3 row is the least
certain, so it is measured first, cheaply: a model and an emulator, before any RTL.

⚠ **This revisits a recorded decision.** `docs/ac3_decoder.md`'s durable decisions say
"strict full-fabric RTL, no microcoded sequencer". The maintainer asked for this
measurement on 2026-10-02, so that decision is being *revisited*, not overturned. It
stands until a fit says otherwise.

## The contract: what the parse hands the IMDCT

`imdct_512` stays hardwired (§4: a direct-form 5.1 transform needs 2.3–2.7× one
multiplier). The engine replaces everything upstream of it: `sync_crc`, `bsi_parse`,
`audblk_parse`, `exponent_decode`, `bit_allocation`, `mantissa_dequant` and both bit
readers. Per block it must produce exactly what `imdct_512` reads from `ac3_parse`:

- `coeff_mem`: signed Q1.23 for `{channel, bin}`, 256 bins for every full-bandwidth
  channel (high bins zero-filled), and the LFE slot (6, 7 bins). `imdct_512` never
  reads LFE, but it shares the dither LFSR and the bit position with everything after
  it, so it is scored.
- `blksw` (a bit per channel), `dynrng` (8 bits);
- per frame: `acmod`, `lfeon`, `cmixlev`, `surmixlev`.

**The golden is the RTL itself, not liba52.** The datapath is fixed-point (Q1.23,
truncating, its own 16-bit rounded level tables, its own dither scaling), so liba52
can only approximate it. A reclaim has to be trace-identical to today's decoder
(`logic_reclaim.md` method), so it is scored against the RTL's own output.

**The tap: `bench/ac3/golden_main.cpp`** (`make -f Makefile.golden`). It is a separate
Verilator build of the same RTL list as the cosim, with `--public-flat-rw` for the
internal `blksw` and `dynrng`; the RTL is not changed and no liba52 is linked. Run it
as `obj_golden/ac3_golden STREAM.ac3 > STREAM.gold`, with `AC3_MAX_FRAMES=N` to stop
early. Records:
- `F frame start len acmod lfeon cmixlev surmixlev`
- `B frame blk blksw dynrng`
- `C ch v0..` (24-bit hex)
- `E frame` (`err_unsupported` rose)

✅ **The capture point is checked.** It captures at `mant_done`, as `imdct_512` starts.
Capturing at `imdct_done` instead (`AC3_TAP=imdct`) dumps byte-identically on three
streams: coupled 5.1 with LFE, coupled stereo, and BBB's short blocks. So no late write
is missed.

## The cycle budget (measured on today's RTL, estimated on the engine)

A frame is 1,536 samples, 32 ms: **864K cycles at 27 MHz**. The tap's timing (stderr):

| Stream | Parse a block (exponents → mantissas, hardwired) | IMDCT a block |
|---|---|---|
| `tone_5p1_48k_192k` (coupled 5.1) | 4,822 mean | 13,479 |
| `noise_5p1_48k_640k` (uncoupled 5.1) | 12,470 mean, 15,108 max | 13,479 |
| `bbb_short_5p1` (448k, short blocks) | 10,092 mean | 13,474 |
| `sweep_192k` (coupled stereo) | 4,429 mean | 4,573 |

The parse and the IMDCT run **in series** (`ac3_parse`: `P_MANT → P_IMDCT → P_DRAIN`),
so today a 5.1 frame is about 160K cycles, 18 % of the budget.

**Bit allocation on the engine, from `bit_allocate.c`'s loops** (a full-bandwidth
channel, 253 bins):
- bins 0–19 take the per-bin mask path, about 35 instructions each;
- the banded loop integrates about 233 bins (a log-add of about 13–14 cycles each) and
  computes about 30 band masks;
- the bap pass costs about 9 cycles a bin.

That is about 7K cycles a channel fully interpreted. A 5.1 block with coupling and LFE
is about 38K, and a frame with every block reallocating about **230K (27 %)**. Two small
vector ops (the band's log-add integration; the bap lookup) bring that to about **75K
(9 %)**.

**Whole frame on the engine (estimate): about 240K (28 %)** with those two ops, a
mantissa op (XQ-like, about 3 cycles a coefficient) and an exponent op; **about 390K
(45 %)** with bit allocation fully interpreted. Both are under the 60 % bar the DTS
engine is held to. So bit allocation does **not** have to stay one hardwired op, and
its 591 ALMs are genuinely reclaimable: scenario E's row stands.

## Plan

1. **A0: the hardware-order model, `tools/ac3_model.py`.** A function library at
   vector-op granularity (as `tools/dts_fixed.py`), chained by a stream decoder:
   - the integer stages (syntax, exponents, bit allocation) from liba52 0.8.0's
     `parse.c` / `bit_allocate.c`, which the RTL transcribes literally;
   - the fixed-point stage from the RTL (`mantissa_dequant.sv`): the level tables and
     constants parsed out of `ac3_mant_tables.svh` / `ac3_tables.svh`, so they cannot
     drift.

   Gate: bit-exact against `ac3_golden` dumps on the whole gate set.
2. **A1: the engine program and the emulator.** AC-3 microcode and vector ops for
   `tools/dts_isa.py`'s machine, scored bit-exact against the model, with cycles from
   the RTL-calibrated model. This is scenario E's go/no-go on cycles.
3. Then RTL and a fit, as P1b did, if the maintainer wants it.

**State the model must carry across blocks and frames** (the RTL resets it only on
`rst`, and the tap starts cold):
- the dither LFSR, advanced once per dithered `bap 0` bin in coefficient order,
  including the coupled channels' per-channel advances;
- the grouped-quantizer cache, reset once per block, persisting across channels, the
  coupling read and the LFE read;
- exponent reuse;
- `chincpl`, the coupling geometry, `cplco` and `rematflg` reuse;
- `deltbae`.

**Reader facts for the ISA:**
- AC-3's grouped codes are **high digit first** (`q_1_0 = code / 9`, then `/ 3`),
  the reverse of DTS's block codes. The restoring divider still works if the
  digits are emitted reversed.
- The quantizer cache is **block-persistent across ops**: engine state, not an
  op's local.

## A0 result so far: the model

`tools/ac3_model.py` is the parse in the RTL's arithmetic, at vector-op granularity:
- `ungroup_exps`, `bit_allocate` (liba52's loops), `Mantissas.m16` (the grouped
  caches), `scale_coeff`, `dither_coeff`, `recombine`, `rematrix`;
- `Decoder` holds the state that persists across blocks and frames;
- the RTL's level, dither, bit-allocation and band tables are parsed out of
  `dvd/ac3/*.svh`.

**Gate `tools/test_ac3_model.py`:** it rebuilds the tap, regenerates every golden
from the current RTL, and requires bit-exact output on every stream, plus RED
mutations of the model.
- ✅ All 13 stock streams are **bit-exact** (12 frames each): acmod 1–7, coupled stereo
  and 5.1 with LFE, BBB's short blocks with ch0 uncoupled.
- Five RED arms bite: dither rounding, the recombine's shift, rematrix off, grouped
  digits low-first, the coupling-coordinate placement.
- ⏳ **Two arms bite on no stream**, because no stock stream has the feature: the
  recombine's **saturation** and the stereo **phase flags**. The library census
  (below) decides whether disc windows cover them. If not, derived fixtures can:
  rewriting fixed-width fields of real frames (zero coupling exponents give huge
  coordinates that saturate the recombine) keeps every frame's length.

⚠ **Finding: a latent RTL deviation from liba52 in delta bit allocation.**
`audblk_parse.sv` resets `deltbae` to NONE at the **start of every block**, and
`bit_allocation.sv` applies `deltba` only when the block says NEW. liba52 resets it
**once a frame** (`a52_frame`) and keeps NEW, and REUSE, across blocks. On a stream
that sends delta-BA in one block and not in a later block of the same frame (or says
REUSE), the RTL allocates without the delta where liba52 applies it. The bit
allocations differ, so the mantissa bit counts differ and the block desynchronises.
- No stock stream uses delta-BA, so the cosim's "bap bit-exact" could not see this.
- The model follows the RTL by default; `Decoder(liba52_deltba=True)` gives liba52's
  rule.
- `tools/ac3_scan.py` decodes every library window both ways and counts the blocks
  that differ. That measures how often a real disc hits this.
- Fixing it changes today's decoder, so it is the maintainer's call, not this branch's.

## Gate set

- `tools/streams/*.ac3` (`tools/gen_test_stream.sh`): acmod 3–6 uncoupled at 640k,
  coupled stereo, coupled 5.1 with LFE, uncoupled 5.1;
- the committed `bench/ac3/vectors/`: BBB 5.1 (448k, short blocks, ch0 uncoupled in
  coupling) and BBB mono;
- real-disc windows of about 20 frames, extracted by the PES
  `first_access_unit_pointer` (as `tools/acmod_scan.py`, never a `0x0B77` search):
  DRC-heavy 5.1 (*The Matrix*'s 0x82 sets `dynrng` on nearly every block) and delta-BA.

## Open decisions (⏳ the maintainer's)

- **The RTL's refusals.** It refuses acmod 0 (dual mono) and coupling-channel
  `deltbae == NEW`, and halts on any error. Trace identity says the engine reproduces
  that, and the model does by default. Microcode could implement coupling delta-BA
  cheaply instead.
- **The IMDCT in series.** The engine could parse block n+1 while `imdct_512`
  transforms block n, but only with a double-buffered `coeff_mem` (+M10K). Not needed
  at 28–45 %.
- **MP2 on the engine** (the other half of scenario E) comes after AC-3. Its synthesis
  on the factorised transform needs an LSB-bounded golden of its own.

## Local references

liba52 0.8.0's source (GPL) is a local reference, never committed. The tools take its
path from `LIBA52_SRC_DIR`.
