# The AC-3 parse on the shared audio engine (scenario E)

**Status (2026-10-02): ✅ A0 done; ✅ A1a done.** The model is bit-exact against the
RTL on 30 streams. The engine program (side information, exponents, bit allocation;
mantissas by a stand-in op) is bit-exact against the model on all 30. ⏳ **Next: A1b,
the mantissa, coupling and rematrix ops.** Branch `feature/ac3-engine` (from
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
mutations of the model, each tied to the stream feature it needs.
- ✅ **30 streams bit-exact** (24 frames each):
  - the 13 stock streams: acmod 1–7, coupled stereo and 5.1 with LFE, BBB's short
    blocks with ch0 uncoupled;
  - 13 disc windows under `$AC3_TEST_DIR` (default `~/ac3-streams/gate`, local): both
    phase-flag streams, zero-SNR blocks, acmod 1, 3, 5 and 6, *Matrix Reloaded*'s 5.1
    (short blocks, ch0 uncoupled, DRC), a DRC-heavy 5.1, short blocks in stereo;
  - 4 refusal windows (acmod 0, coupling delta-BA, delta-BA overflow, reserved
    `deltbae`), where the model refuses the frame the RTL refuses (claim [2]).
- Six RED arms bite: dither rounding, the recombine's shift, rematrix off, grouped
  digits low-first, the coupling-coordinate placement, phase flags ignored.
- ⚠ **One GAP: the recombine's saturation.** The largest coupled coefficient in about
  4,500 disc windows is 7.8 % of full scale, and a coordinate is at most 7.75, so no
  real stream reaches saturation. No fixed-width field rewrite of one does either:
  zeroing the coupling exponents tops out near 0.6. A constructed stream with a
  coupling-band exponent of 2 or less would. The model saturates as the RTL does
  (read from `mantissa_dequant.sv`), but no stream proves it.

**Library census (`tools/ac3_scan.py`, 2026-10-02):** 1,554 images (24 unreadable),
**12,855 AC-3 streams, 2.46M frames, 14.8M blocks**, each decoded twice (the RTL's
rule, liba52's delta-BA rule).

| Feature | Streams | Blocks |
|---|---|---|
| acmod 2 / 7 / 1 / 5 / 6 / 3 | 8,768 / 3,798 / 115 / 5 / 4 / 1 | |
| coupling | 12,268 | 14.4M (97 %) |
| dither | 12,686 | 14.8M |
| rematrix (2/0) | 7,789 | 7.4M |
| `dynrng` sent | 11,358 | 2.4M |
| skip field | 12,584 | 2.9M |
| short blocks | 4,575 | 9,704 |
| ch0 uncoupled while coupling | 386 | 1,067 |
| phase flags in use / set | 73 / **2** | 15,856 / 340 bands |
| zero SNR offsets | 15 | 1,752 |
| **delta bit allocation** | **0** (the one NEW block is inside a corrupt frame) | 0 |
| recombine / rematrix saturation | 0 / 0 | 0 |

Every refusal the RTL makes occurs, and the model agrees with the RTL on all of them.
377 windows stopped decoding; the RTL tap and the model ran on each, with the frames
around the stop:
- **309: identical up to the refusal, and both refuse the same frame**: acmod 0, the
  coupling-band check, delta-BA overflow, reserved `deltbae`, coupling delta-BA.
- **68: the model stops on genuinely invalid input** that the RTL decodes on: a grouped
  code past its range (27–31, 125–127, 121–127), an exponent outside 0–24, a bandwidth
  code of 63, or a read past the frame. 55 of them are one disc (*Fairytopia*). There
  the RTL reads past its level tables, or clamps, which is implementation-defined, so
  matching it exactly is not meaningful.
- **0 windows where the model and the RTL disagree before a stop.**

⏳ **Decision for an engine port: invalid codes.** Refuse the frame and count it, or
decode leniently and count it (DTS's D5 precedent). Either way the behaviour becomes
defined, where today's RTL reads out of range.

⚠ **Finding: a latent RTL deviation from liba52 in delta bit allocation.**
`audblk_parse.sv` resets `deltbae` to NONE at the **start of every block**, and
`bit_allocation.sv` applies `deltba` only when the block says NEW. liba52 resets it
**once a frame** (`a52_frame`) and keeps NEW, and REUSE, across blocks. On a stream
that sends delta-BA in one block and not in a later block of the same frame (or says
REUSE), the RTL allocates without the delta where liba52 applies it. The bit
allocations differ, so the mantissa bit counts differ and the block desynchronises.
- No stock stream uses delta-BA, so the cosim's "bap bit-exact" could not see this.
- ✅ **Measured: no library stream uses delta-BA at all** (12,855 streams, above), so
  decoding with liba52's rule changes zero blocks. The deviation is real and harmless
  on this library. Delta-BA's coverage stays the RTL's own unit bench
  (`bench/ac3/run_balloc.sh`, liba52 on a constructed DELTA_BIT_NEW case); enabling
  delta-BA in a real frame adds bits, so no field rewrite can derive a fixture.
- The model follows the RTL by default; `Decoder(liba52_deltba=True)` gives liba52's
  rule.
- `tools/ac3_scan.py` decodes every library window both ways and counts the blocks
  that differ. That measures how often a real disc hits this.
- Fixing it changes today's decoder, so it is the maintainer's call, not this branch's.

## A1a result: side information, exponents and bit allocation on the engine (2026-10-02)

`dvd/dts/ac3.uasm` (the program) and `tools/ac3_isa.py` (the emulator: `tools/dts_isa.py`'s
sequencer with AC-3's constant ROM, its record map and its ops). Each op calls the
model's own function:

| Op | Does | Charge (derived from the intended structure; no RTL yet) |
|---|---|---|
| `EXPD` | 7-bit exponent groups to the bins' exponents | max(7, 3·rep) + 1 a group, + 4 |
| `BAPSD` | a band's integrated PSD (`ba_band_psd`) | 1 a bin + 4 |
| `BAPFILL` | the band's baps (`ba_bap_fill`) | 1 a bin + 4 |
| `BAPZERO` | baps 0 (zero SNR offsets) | 1 a bin + 3 |
| `MANTMODEL` | **A1a's stand-in**: the mantissas, coupling and rematrix, decoded by the model's code from the program's own exponents and baps | estimated: 1 a code bit + 2 a coefficient |

The rest of the parse is microcode: the frame header, the BSI, the side
information, the coupling coordinates, all five phases of liba52's bit
allocation, and the band masks.

**Record map** (2K × 16, as DTS's):
- 0x000–0x0FF holds the side information;
- `{bap[13:8], exp[4:0]}` sit one word a bin from 0x100: slots 0–4 at 0x100 + 256·ch,
  the coupling channel at 0x5DB (bins 37–252), LFE at 0x6D8;
- the 5 × 50 delta-BA bands sit at 0x6E0.

So the ops take the base address of a slot's bin 0, and every slot is addressed the
same way. A coupling coordinate is stored packed (`{m, e == 15, e + mstr}`), because
its Q5.18 value needs 22 bits and the registers have 16; the coupling op will expand
it.

**Result:**
- ✅ **Bit-exact against the model on every block of all 30 streams**: exponents,
  baps, coefficients, `blksw`, `dynrng`.
- Each refusal window stops at the same frame with the matching code (acmod 0,
  coupling delta-BA, reserved `deltbae`, delta-BA overflow, an invalid group code).
- One bug found on the way: the allocation loop's channel counter lived in a register
  that `BA_CHAN` clobbers, so channel 1 got no bit allocation.

**Cycles a frame (the emulator; the stand-in's share is an estimate):**

| Stream | Total | Mantissa stand-in | % of 864K |
|---|---|---|---|
| `noise_5p1_48k_640k` (the heaviest) | 211K | 30K | 24 % |
| *Matrix Reloaded* 5.1 448k | 192K | 24K | 22 % |

- The sequencer's own instructions are about 75 % of it: the side information and the
  bit allocation's per-bin (phases 1–4) and per-band logic.
- With `imdct_512` in series (81K a 5.1 frame), the frame is about 34 %, under the 60 %
  bar. The op charges carry a ×1.25 headroom factor (`CYC_HEADROOM`), about +13K.

⚠ **The binding constraint is the microcode's size, not its speed.** The AC-3 program
is **709 words**, and together with DTS's 500 that is **1,209 words in one ROM**:
- over 1,024, so the ROM is 2K deep, **8 M10K**. DTS alone was 2.
- Options, not yet measured: hardwire phases 1–4 of bit allocation as one op (about
  120 words out, some ALMs in); a 256-entry ÷3 table for the exponent group count (out
  of a microcode loop); a shared subroutine for the five "read a field into a
  per-channel array" loops.
- MP2 on the engine adds a third program. The ROM depth is a scenario-E cost to
  measure, not assume.

## Gate set

- `tools/streams/*.ac3` (`tools/gen_test_stream.sh`): acmod 3–6 uncoupled at 640k,
  coupled stereo, coupled 5.1 with LFE, uncoupled 5.1;
- the committed `bench/ac3/vectors/`: BBB 5.1 (448k, short blocks, ch0 uncoupled in
  coupling) and BBB mono;
- disc windows (`~/ac3-streams/gate`, local, never committed), cut by
  `tools/ac3_scan.py IMAGE --extract DIR --want FEATURE`, from the PES
  `first_access_unit_pointer` (never a `0x0B77` search). A single-event feature is cut
  around its first frame, and an error window around its stop. The images and features
  are in the gate table above; the refusal windows are copied from the error windows
  that both sides refused identically.

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
