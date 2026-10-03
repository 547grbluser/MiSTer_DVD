# The AC-3 parse on the shared audio engine (scenario E)

**Status (2026-10-02): ✅ A0, ✅ A1, ✅ A2a, ✅ A2b done; next A2c.** The model is
bit-exact against the RTL on 30 streams. The whole AC-3 parse runs as an engine
program (`dvd/dts/ac3.uasm`, emulated by `tools/ac3_isa.py`) and is bit-exact against
the model on every block of those 30 streams. The sequencer's AC-3 units are built
(A2b) and trace-identical to the emulator on all of them, with the emulator charging
their exact RTL cycle counts. The worst frame needs **37 % of real time** with the
IMDCT in series. **Next: A2c**, the vector side of AC-3's ops in `dts_vec.sv`.
✅ **Decided (maintainer, 2026-10-02): next is the AC-3 engine's RTL and a standalone
fit (A2), before MP2.** ALMs are the binding resource, and the AC-3 engine's ALM cost
is scenario E's least certain number; the same fit prices the hardwired
bit-allocation op. The 2K ROM is accepted until then. Branch
`feature/ac3-engine` (from `feature/dts-decode`, `CORE_VERSION dev-ac3engine`, not
pushed).

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

✅ **Decided (maintainer, 2026-10-02): an engine port refuses a frame with an invalid
code, and counts it** (`E_GROUP` for a grouped code past its range, `E_EXP` for an
exponent outside 0–24, `E_CHBW` for a bandwidth code above 60). That is the house
fail-loud rule, and `ac3.uasm` already does it. Decoding leniently and counting (DTS's
D5) was the alternative. Either makes the behaviour defined where today's RTL reads out
of range; 68 library windows carry such codes, 55 of them on one damaged disc.

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
- ✅ **Decided (maintainer, 2026-10-02): leave today's decoder as it is.** It changes
  no block on the library, and touching the shipping decoder carries risk for no
  audible gain. An engine port reproduces the RTL's rule (trace identity), and could
  take liba52's later at almost no cost.

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

**Gate `tools/test_ac3_isa.py`:**
- [1] bit-exact against the model on every block of every stream, refusals at the
  same frames;
- [2] every frame within 60 % of real time, counting the op charges with their headroom
  and `imdct_512` in series (13.5K cycles a block measured for 3+ channels, 4.6K for
  stereo). **Worst: 35.8 %**, `noise_5p1_48k_640k`;
- [3] the generated images are current;
- five microcode RED arms (`;MUT` lines), each tied to its feature: the phase-3
  lowcomp seed, the coupling exponent seed, the phase flags, the frame-start
  `dynrng` reset, the mask's knee. All bite.

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
is **711 words**, and together with DTS's 500 that is **1,211 words in one ROM**:
- over 1,024, so the ROM is 2K deep, **8 M10K**. DTS alone was 2.
- Options, not yet measured: hardwire phases 1–4 of bit allocation as one op (about
  120 words out, some ALMs in); a 256-entry ÷3 table for the exponent group count (out
  of a microcode loop); a shared subroutine for the five "read a field into a
  per-channel array" loops.
- MP2 on the engine adds a third program. The ROM depth is a scenario-E cost to
  measure, not assume.

## A1b result: the mantissas on the engine; A1 complete (2026-10-02)

The stand-in is gone. The mantissa stage is microcode (`MANT`, `CPLPASS`) around five
ops, each calling the model's own function:

| Op | Does | Charge (derived; no RTL) |
|---|---|---|
| `QRST` | empties the grouped-quantizer caches at the block's mantissa stage | 2 |
| `AQ` | one channel's bins: zero, dither or a dequantised mantissa (`one_coeff`) | 1 a bin + its code bits + a fresh grouped code's divisions (high digit first, so 2×5 / 2×7 / 1×7 extra) + 3 |
| `AQC` | one coupling band: each bin read once, scattered into every coupled channel (`cpl_bins`) | as `AQ` + 1 a coupled channel a bin + 2 + nf coordinate reads |
| `CZERO` | a channel's zero tail | 1 a bin + 2 |
| `REMAT` | 2/0 rematrix (`rematrix_coeffs`) | 4 a bin + 3 |
| `IMDCT` | the block's coefficients are ready (the handshake that starts `imdct_512`) | 2 |

**Where the engine state lives (decided):**
- **coefficients** in the vector engine's X buffer: 7 slots × 256 × 24 bits fit its 2K
  words, and `imdct_512` reads them there. It needs a read port: a wiring item, as
  DTS's P3;
- **the grouped-quantizer caches** in the sequencer's mantissa unit, emptied by `QRST`
  (the RTL resets them per block too);
- **the dither LFSR** in a vector-engine register, reset only at power-up.

AC-3's ops are numbered from 16, so **the vop field the RTL decodes widens from 4 to
6 bits**. The instruction word already carries 6.

**Result:**
- ✅ `tools/test_ac3_isa.py` passes: **bit-exact against the model on every block of all
  30 streams**, refusals at the same frames, eight microcode RED arms all biting (the
  five of A1a, plus rematrix off, coupling bands unmerged, and a coupled channel's zero
  tail from the wrong bin).
- The model gate gained a `cplmerge` counter for the band-merge arm.

**Cycles a frame (emulator):**

| Stream | Frame | Sequencer microcode | Ops |
|---|---|---|---|
| `noise_5p1_48k_640k` (heaviest) | 218K | 159K | 58K (`AQ` 33K) |
| *Matrix Reloaded* 5.1 448k | 200K | 150K | 48K |
| `tone_5p1_48k_192k` (coupled 5.1) | 135K | 116K | 19K |
| *Battlefield Earth* 2/0 192k | 81K | 62K | 18K |

The gate's worst frame, with ×1.25 headroom on the op charges and `imdct_512` in
series, is **36.3 % of real time**.
⚠ **What that figure is made of:**
- **Sequencer cycles** (about 75 % of the frame) use DTS's RTL-calibrated instruction
  costs. That calibration has not been checked on *this* program's instruction mix,
  which is load-heavy loops, calls six deep, and `min` in hot loops.
- **Op cycles** are structure-derived estimates × 1.25.
- **`imdct_512`** is measured on the RTL.

So the figure is less measured than DTS's 39.8 %, and an RTL bench would replace it.

**`tools/test_ac3_isa.py` [1r] scores the engine against the RTL directly:** its
coefficients, LFE, `blksw` and `dynrng` are compared with the `.gold` dumps, not only
with the model. It is bit-exact on every stream, and a mutated program fails it (no
rematrix: 153 values; the wrong zero tail: 288), so the check can fail.

**Where the cycles go (profile, *Matrix Reloaded*):** bit allocation's microcode is
about **70 % of the frame**, about 140K cycles (16 % of real time). It is the per-bin
and per-band helpers: mask 27K, leak update 26K, bap write 14K, lowcomp 11K, and the
channel loop.

**Where the words go (779):**

| Part | Words |
|---|---|
| side information (`BLOCK`) | 283 |
| bit allocation (`BA_CHAN` 141, `ALLOC` 102, helpers about 90) | about 333 |
| the header (`FRAME`) | 62 |
| mantissas (`MANT` + `CPLPASS`) | 68 |
| delta-BA segments | 33 |

⏳ **Decision: the microcode's size.** DTS 500 + AC-3 779 = **1,279 words**, so the ROM is
2K deep and takes **8 M10K**. DTS alone takes 2; ≤ 1,024 words would take 4.
- Scenario E frees about 40 M10K, so +6 is affordable, and M10K is not the binding
  resource.
- To fit 1,024, AC-3 must shed about 255 words. A hardwired per-band
  bit-allocation op (leak update + mask + bap fill) would remove about 200 words *and*
  most of the 140K cycles, but it costs ALMs, the binding resource, so only a fit can
  price it.
- **The no-ALM cuts were measured, and they are small.** A shared per-channel field
  reader for `BLOCK`'s five loops saves about 8 words. `floor(y/3) = (y·171) >> 9`
  (exact for y < 256) replaces the exponent group-count loop, but it is a cycle cut,
  not a word cut (+4 words, −330 cycles a channel-block). **So 1,024 words genuinely
  needs the hardwired per-band op**, which makes the size question an ALM question.
- `dvd/dts/ac3_ucode.mem` and `ac3_const.mem` are **emulator images**. No RTL reads
  them yet, and nothing in `DVD.qsf` names them.
- MP2 would be a third program in the same ROM.
- ✅ **Decided (maintainer, 2026-10-02): accept the 2K-deep ROM (8 M10K) for now.**
  The hardwired per-band bit-allocation op is decided at the fit, when its ALM price is
  known.

## A2a: one engine, two programs (2026-10-02)

- **One microcode ROM for both programs:** `dvd/dts/engine_ucode.mem`, 2K × 40, with DTS's
  program at 0 (500 words) and AC-3's after it (779 words, origin 500). It is written
  by `tools/dts_isa.py --asm`, which assembles both; `tools/ac3_isa.py` assembles at
  the origin, so the emulator's pcs are the RTL's. One constant ROM likewise:
  `engine_const.mem`, DTS's 92 words then AC-3's 436, 528 in all.
- **The error vectors moved to the ROM's top 32 words (2016–2047)**, so they clear
  both programs. The assembler refuses any word that reaches them. The pc is 11 bits.
- **`dts_seq.sv` gained a `codec` input** (0 DTS, 1 AC-3; change it only in reset).
  It picks the entry point and the restart point after a refusal (`UC_DTS_*` /
  `UC_AC3_*` in `dts_ucode.svh`). The vop field is 6 bits through `dts_vec` and
  `dts_top`.
- **Gates:** the DTS benches pass unchanged after the widening (`run_dts_seq.sh --red`
  37 arms and 19 mutations; `run_dts.sh --red` 41 and 18), and so do `test_dts_isa.py`
  and `test_ac3_isa.py`.
- **Fit leg (a): the DTS engine with the shared ROM, no AC-3 unit yet** (`tools/fit_unit.sh`,
  same settings as P1b). It isolates the ROM's own cost from the AC-3 units' cost:

  | | ALM | M10K | Fmax −40 °C / 100 °C |
  |---|---|---|---|
  | P1b: DTS engine, 512-word ROM | 2,227 | 31 | 35.8 / 37.7 MHz |
  | **A2a: DTS engine + the shared 2K ROM** | **2,233** | **39** | 35.5 / 36.8 MHz |

  The microcode ROM goes from 2 M10K to 8 (1,279 × 40). The constant ROM, now 528
  words, moves out of logic into 2 M10K. The 11-bit pc and the codec select cost
  about 6 ALM. **Leg (b), the AC-3 units, is measured against 2,233 / 39.**
- **The sequencer → vector interface for AC-3 (decided):** XQ's code port gains an
  11-bit address. The mantissa unit hands the vector engine:

  | Item | Address | Value (24 bits) |
  |---|---|---|
  | a bin (`AQ`, `AQC`) | `{slot, bin}` | m16 [16:0], exp [21:17], bap 0 [22], dither this bin [23] (AQ) |
  | the band's channel set (`AQC`) | `0x7F0` | dither mask [4:0], `chincpl` [9:5], nf [12:10] |
  | a coordinate (`AQC`) | `0x700 + ch` | Q5.18, the phase applied |

  The vector engine does the scale (a shift), the dither and the recombine, and
  aborts the op on a refusal's `err_valid` (A2b). The
  emulator traces these items as kind 2 and every op's record write as kind 3, in the
  units' issue order: that is what the sequencer bench will score. The
  bit-allocation units read `baptab` and `latab` through the constant ROM's port
  (`latab` joined it: 784 words, still one 1K-deep ROM).

## A2b: the sequencer's AC-3 units (2026-10-02)

`dvd/dts/dts_seq.sv` gained AC-3's sequencer-side units. They use the existing bit
reader, XQ's restoring divider and the record and constant ROM ports. `tools/ac3_isa.py`'s
`Machine.run_op` is each unit's definition:

- **EXPD** splits each 7-bit group code with two in-place divisions by 5 on XQ's
  divider (`code/25` is the final quotient). It writes each digit's exponent `rep`
  times. An exponent outside 0..24 refuses (E_EXP) *before* it is written.
- **BAPSD** does liba52's log-add, 2 cycles a bin. It reads `latab` through the
  constant ROM's port, with the index clamped at 255. **BAPFILL** reads `baptab` the
  same way, at `clamp(156 + mask + 4 exp, 0, 304)`, pipelined at 1 a bin. **BAPZERO**
  takes 1 a bin. Both pipelined units drain before the next instruction, which reads
  those words. **QRST** finishes at the vop's dispatch.
  - The clamps copy `bit_allocation.sv`'s `la_addr` / `bl_addr`. `tools/ac3_model.py`
    now copies them too, because Python's indexing would have raised, or worse wrapped
    a negative index. No gate stream reaches either clamp, so that is a GAP, not a
    pass.
- **The mantissa unit (AQ, AQC).** For each bin it reads the bin's record word, then
  its code bits. A grouped code is split by the divider (3-level ÷3 twice, 5-level ÷5
  twice, 11-level ÷11 once). The high digit is used first and the rest are cached. A
  quotient ≥ levels is an invalid code and refuses (E_GROUP).
  - The caches persist across every AQ and AQC of a block's mantissa stage. **Only
    QRST clears them**, and mutation X9 (cleared at each AQ) proves the bench sees it.
  - Levels come from a new 43-word **MLEV** table in the constant ROM (827 words now).
    The unit sends one item a bin over XQ's port, now with an 11-bit `xq_addr`.
  - AQC first reads nf, `chincpl`, the band's phase flag and nf dither flags. It sends
    the channel set, then each coupled channel's coordinate: a serial shift by its
    exponent, with ch1's phase applied.
- **The record RAM has one write port:** the program's stores and the units' writes.
  The units' writes are traced as kind 3.
- **⚠ The abort contract (for A2c):** a unit's refusal pulses `err_valid` mid-op, and
  **the vector engine must abort an in-flight AQ / AQC on it**. The sequencer drains
  the frame and restarts at FRAME. It also drops a pending item (`xq_valid`).
- **Gate `bench/dvd/run_ac3_seq.sh`** (shares `dts_seq_tb.sv` with DTS, `+codec=1`;
  goldens from `tools/ac3_golden.py`): **34 arms GREEN**, every event identical:
  - every gate stream at 4 frames, and each refusal window at 5, so the restart after
    the refused frame is scored;
  - noise 5.1 with input stalls and item back-pressure;
  - the invalid grouped-code window (E_GROUP mid-AQ) under stalls;
  - E_EXP through `--badexp`, which rewrites a frame's first exponent codes to 124
    (no disc window has one);
  - truncation (1,309 overrun bits).
  - **[cycles]:** on every stall-free, refusal-free arm, each op's cycles in its own
    states equal the emulator's charge.
  - `--red`: **20 mutations, each caught by its own arm**. X20 is timing-only (one
    extra divider step), so only [cycles] catches it.
- **⚠ GAP: BAPZERO's value.** The gate's one zero-SNR window (*Dark Passengers*, 114 of
  120 blocks) codes every such block with exponent 0 and fresh exponents. All its
  BAPZERO writes are 0, so "the exponent lost" and "the old bap kept" both survive,
  even over 20 frames. A zero-SNR block with real exponents would show them. X8
  scores BAPZERO's range instead.
- **The cycle model is now the RTL's.** `ac3_isa.bin_cycles` and the per-op charges
  count the units' states one for one. The vector side (A2c) is the only modelled
  part, and `test_ac3_isa.py` [2] puts the ×1.25 headroom on it alone. The worst frame
  is **37.4 %** of real time (noise 5.1), against 36.3 % on the derived charges: EXPD
  and the mantissa unit cost more than the sketch (two divisions a group, about
  2 + bits + 1 a bin).
- DTS's gates stay green: `run_dts_seq.sh` (37 arms) and `run_dts.sh` (41).
- **Fit leg (b), the sequencer half** (`tools/fit_unit.sh dts_top`, same settings as
  leg (a); the vector side's AC-3 ops are not in it yet):

  | | ALM | sequencer | vector engine | M10K | DSP | Fmax −40 °C / 100 °C |
  |---|---|---|---|---|---|---|
  | A2a: DTS engine + the shared 2K ROM | 2,233 | ~860 | ~1,120 | 39 | 1 | 35.5 / 36.8 MHz |
  | **A2b: + AC-3's sequencer units** | **2,679** | **1,297** | 1,124 | **39** | 1 | **37.5 / 39.1 MHz** |

  - **AC-3's sequencer units cost about +440 ALM** and no M10K. MLEV fits in the
    constant ROM, which is still 1K deep, and the record RAM still infers as an M10K
    (map report checked).
  - Scenario E (`dts_decoder.md` §4) budgeted AC-3's ops, bit allocation included, in
    its vector-engine row: +1,200 … +1,500 together with MP2's synthesis, bit
    allocation alone +300 … +600. The whole sequencer half lands inside that, with A2c's
    vector ops still to add.
  - Not yet priced: the hardwired per-band bit-allocation op (the 2K-ROM decision), so
    the comparison waits for leg (b) complete.
- **Next (A2c):** AC-3's ops in `dts_vec.sv`:
  - the bin items: the scale shift, the dither LFSR × 23170 and the recombine, all on
    the DSP;
  - CZERO, REMAT, and the IMDCT handshake;
  - X-buffer writes;
  - the abort above;
  - an X-buffer read port and the side-info outputs on `dts_top` for `imdct_512`.

  Its gate is a whole-engine bench against `.gold` coefficient dumps. Then **A2d**,
  fit leg (b) against 2,233 ALM / 39 M10K.

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
