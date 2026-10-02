# In-fabric DTS core decoder (`dvd/dts/`, planned)

**Status: ✅ P0 BUILT; D3 and D5 decided. The reference is bit-exact against
FFmpeg on 32 streams, spec maxima and joint intensity included; no RTL yet
(2026-10-02).** Branch
`feature/dts-decode` (`CORE_VERSION dev-dtsdecode`). Next concrete step: **P1** (§7), the
engine standalone, with `tools/dts_fixed.py` as its golden and `tools/dts_writer.py`'s
spec-maximum streams among its tests.

Today a DTS track is silent in `Decode PCM` mode: `dvd_audio_decode.sv` routes
`frame_type == 1` to a discard (`// DTS: discard`), and DTS is audible only through IEC 61937
passthrough to a receiver (`iec61937.md`, `hdmi_bitstream.md`). This note is the plan to
decode the DTS **core** to stereo PCM in the fabric, so a plain TV plays it.

## 1. Scope

- **In:** the DTS Coherent Acoustics **core** as DVD-Video carries it: 48 kHz, 16-bit
  big-endian frames (`dts_reframer.sv` already frames them), 768 / 1536 kbit/s, up to 5.1.
  Out: s16 stereo at 48 kHz on `AUDIO_L/R`, through the existing gate, PTS scheduling and
  re-time machinery.
- **Ignored (decoded as core only):** every extension — XCh (DTS-ES 6.1), X96, XXCH, XBR,
  and the EXSS substream. The core is backward compatible by design, so ignoring them
  is correct, not a fallback. A frame that carries one is **counted** (telemetry), never
  silently treated differently.
- **Unchanged:** `Audio Out = Passthru` still bitstreams DTS to a receiver.
- **Stock Main:** works. No file, no HPS process (D1, D4).

## 2. Decisions

### D1 — In fabric, not on the HPS (2026-10-02, maintainer)

An HPS route was costed first: the fabric sends DTS frames to a Main thread through a DDR3
ring, the thread decodes them with libdca or FFmpeg, and the PCM returns through a second
ring into a fifth codec arm. **Rejected.** It breaks the roadmap's first guiding principle
("self-contained `.rbf` — no HPS-side daemon", `roadmap.md`). It would work only with the
custom Main. And it adds a round trip that the dispatcher's in-band re-time (PR #141) and
hard-flush realign (PR #143) would have to cover with per-record epochs, which is exactly
where the stale-PTS class of bug lives. Kept here so the comparison is not re-derived.

### D2 — A microcoded sequencer plus hardwired vector ops on one multiplier

- **Sequencer:** a small register file and ALU, `GET n` from a bit-serial reader, a
  canonical-Huffman `VLC table` instruction, loads and stores to a parse record, constant
  ROM reads, branches, call and return, and `ERR code`. The whole parse is a ROM program.
- **Vector ops:** the heavy inner loops are hardwired and share one 27×27 DSP multiplier,
  one wide accumulator and one rounding, saturating shifter. They cover dequantisation,
  ADPCM prediction, VQ expansion, joint intensity, the subband-domain downmix and the
  32-band QMF synthesis.
- **Clock:** `clk_sys`, 27 MHz, the same domain as `audio_ring`, the 48 kHz NCO and the
  gate, so there is no CDC.

**Why microcode.** The DTS side info has many conditional fields: per-channel subband
counts, VQ start, joint intensity, transient modes, scale-factor and bit-allocation
codebook selectors, adjustment indices and per-subband prediction. As a hardwired FSM that
becomes a large mux tree (compare the AC-3 parse: `audblk_parse` + `bit_allocation` +
`mantissa_dequant` + `exponent_decode` ≈ 2,300 ALM). As microcode it is ROM words that can
be debugged in Python before any RTL runs. The cost is interpretive cycles (§4).

**Rejected: an FSM per stage**, the house style of `dvd/ac3/` and `dvd/mp2/`. The reason is
area (§4).

**Huffman unit: a binary-tree walk, not canonical decoding (measured 2026-10-02).** The
DTS core has **2,709 symbols** in 62 codebooks (FFmpeg `dcahuff.c`, excluding the LBR
tables), and the longest code is 16 bits. `tools/gen_dts_tables.py` assigns the codes
exactly as FFmpeg does and tests each book. **53 of the 62 are not canonical**: 50 have no
canonical structure in either bit sense and do not keep each length's codes contiguous
(`quant_index_9_0` alone has 49 runs of equal length in code order). So the cheap walk
(`code - first < count[len]`) cannot decode them. Instead, one node table serves every
book. Each internal node holds two entries {leaf, symbol or child index}, so there are
2,709 − 62 = 2,647 nodes of about 26 bits, about 7–9 M10K (it was estimated at 4). The
walk takes one code bit per step, two cycles with a synchronous M10K read, still
consuming bits as it goes. A code that matches no codebook can
only occur in a corrupt stream. It refuses the frame (silence, counted) rather than
un-reading bits.

### D3 — Stereo via a subband-domain downmix before synthesis

The 32-band synthesis filter is linear and identical for every channel within a frame
(`FILTS` is a per-frame flag). So `Σ g_c · QMF(x_c) = QMF(Σ g_c · x_c)`: downmix the
dequantised subband samples to L/R first, then run **two** synthesis filters, not up to
five. That turns the synthesis cost into the cost of MP2 stereo. Verified in FFmpeg:
`filter_perfect` is a frame-header field (`ff_dca_parse_core_frame_header`), and
`filter_frame_fixed` picks one prototype for every channel. The subband count is **per
channel** (`nsubbands[ch]`), so the downmix sums each band only over the channels that
code it; a band above a channel's count is zero for that channel.

**Caveats, accepted and recorded:**
- **Gain changes leave a transient.** The identity holds only while the gains are
  constant. If AMODE or the downmix coefficients change between frames, the QMF history
  was filtered under the old gains. The result is a bounded transient, a known deviation
  from mixing after synthesis.
- **Two-tier validation.** The RTL is bit-exact against our own fixed-point model. That
  model is within an LSB bound of FFmpeg's `BITEXACT` core path (`filter_frame_fixed`)
  mixed *after* synthesis (§6).
- ✅ **Measured (2026-10-02, `tools/dts_fixed.py verify`, `tools/test_dts_fixed.py`).**
  Mixing before synthesis is at most **1 LSB** at s16 from mixing after, with the same Q15
  gains. Terminator 2 (300 frames): RMS 0.11 LSB, 98.8 % of samples identical. A 1536 kbit/s
  5.1 window from *A.I.*: RMS 0.11. The seven synthetic fixtures: RMS ≤ 0.19. Stereo and
  mono are exact, because their gain is 1. The gate's bound is 1 LSB.
- ⚠ **RTL rule: after the sum/difference butterfly, both channels of the pair are mixed up
  to the LARGER of their two active band counts.** The downmix loop is bounded per
  channel to save cycles. But `L+R` carries R's content in bands above L's own count, so
  bounding L by its own count drops them. `tools/dts_fixed.py` did this at first and
  lost 3,459 LSB on `written_amode3`. FFmpeg's encoder always codes 32 bands on every
  channel, so the encoder-made sum/difference fixtures could not show it. The
  writer's uneven counts did. RED arm: `mix_own_bound`.
- ✅ **LFE is left out of the stereo downmix (decided by the maintainer, 2026-10-02).**
  This is the usual stereo-downmix practice, and the AC-3 path does the same. LFE is
  not subband-coded but decimated samples with their own interpolation FIR. The decoder
  still parses the LFE samples to advance through the frame, but never interpolates
  them, so the LFE FIR and its 256-tap table are not built.
- ✅ **The mix rule follows the AC-3 path exactly (decided by the maintainer, 2026-10-02).**
  These are `dvd/ac3/imdct_512.sv`'s effective levels at the A/52 defaults, because DTS has
  no mix-level fields:
  - clev = slev = 0.7071, zeroed for a role the layout does not carry.
  - A **mono** surround (2/1, 3/1) goes to both outputs, pre-scaled by another 0.7071.
    That pre-scaled value is also the one the normalisation uses.
  - `Lo = k(L + clev·C + slev·Ls)` with `k = 1/(1 + clev_eff + slev_eff)` (liba52
    `A52_ADJUST_LEVEL`). It cannot clip, and DTS and AC-3 play at the same level:
    3/2 gives 0.414, 3/1 gives 0.453, 2/1 gives 0.667, and 3/0 and 2/2 give 0.586.
  - Mono, stereo, sum/difference stereo and Lt/Rt pass through at unity. Dual mono
    (AMODE 1) maps A to L and B to R, as FFmpeg does (AC-3 rejects its dual mono).
  - ⚠ The first model got the mono surround wrong: it normalised by the full slev, so
    2/1 and 3/1 came out quieter than AC-3. Fixed in `default_gains()`.
  - **A stream's embedded coefficients are ignored and counted.** They sit in the core
    frame's aux data (`AUX` sync `0x9A1105A0`, 9-bit codes into `DMIXTABLE`), and FFmpeg
    itself uses them only when a stereo layout is requested. *Cinderella III* is the one
    library stream that carries them, in every frame; `tools/dts_fixed.py` counts them
    (`dmix_ignored`), and the RTL needs the same counter in telemetry.

### D4 — Codebooks: shipped in the bitstream as the starting contents of write-first RAMs, copied once to DDR3 (2026-10-02, maintainer)

Two trained codebooks, measured from FFmpeg `dcadata.c`. Real discs use both: the
Terminator 2 DTS sample predicts 36,377 bands with ADPCM and codes 168,750 bands with
high-frequency VQ over 5,625 frames (`tools/dts_ref.py info`).

| Table | Shape | Range | Bits | Size |
|---|---|---|---|---|
| `ff_dca_adpcm_vb` (ADPCM predictor VQ) | 4096 × 4 | −21806 … 21657 | 16 | 256 Kbit |
| `ff_dca_high_freq_vq` (high-frequency VQ) | 1024 × 32 | −87 … 89 | 8 | 256 Kbit |

All 4,096 ADPCM vectors are distinct, and there is no negation or half-table symmetry in
either table. No structure lets them be generated or shrunk. At about 52 M10K they exceed
the 41 free (§4).

**Alternatives rejected:**
- **A `boot1.rom` file pushed by stock Main** at ioctl index `0x40`. It would work, but it
  adds an install step, and a missing file silently disables DTS.
- **Paying the ~52 M10K.** Even after the PCM-FIFO merge, that leaves the device about 10
  short with nothing spare.

**Chosen.** An M10K can be given initial contents in the bitstream and still be an
ordinary RAM afterwards. FIFOs never read a word before writing it, so their power-up
contents are unused today. Three of them match the tables exactly:

| Host RAM | Size | Carries |
|---|---|---|
| `audio_ring` byte memory (`BYTE_DEPTH(32768)`) | 32,768 B | `high_freq_vq`, 32,768 int8 |
| `lpcm_unpack` PCM FIFO (`FIFO_AW(12)`, 4096 × 32) | 16 KB | `adpcm_vb`, first half |
| `mp2_decode` PCM FIFO (`PCM_AW(12)`, 4096 × 32) | 16 KB | `adpcm_vb`, second half |

After configuration, a copier (estimated 50–100 ALM) streams the 64 KB into a reserved
DDR3 region. That takes about a millisecond, and the audio path is held in reset until it
finishes. From then on the hosts are ordinary FIFOs, and the DTS engine fetches vectors
from DDR3.

**Rules the RTL must honour:**
1. **Copy once per configuration, never per reset.** The "copied" flag has **no reset
   term**, so only configuration initialises it. After the first audio the hosts hold
   PCM, so a second copy would write garbage over the tables. Quartus 17 can mangle such a
   register silently (CLAUDE.md, cross-cutting lessons), so **confirm its power-up value in
   the netlist**, not just in sim.
2. **Never silent.** Keep a sticky `dts_tables_ok` bit and a checksum of what was copied,
   readable from telemetry. A bad copy must show as a flag, not as plausible garbage
   audio. While the flag is low, DTS is refused with a counted error.
3. **Bench:** the copy is checked byte for byte against the `.mem` sources, with a
   mutation (for example, one host's init file swapped) that must fail exactly that arm.
4. **Inference check:** after any edit near a host's write sites, grep `DVD.map.rpt` for
   that RAM's "Inferred altsyncram" line *and* its init file. A host that silently falls
   back to registers or loses its init is the failure to catch.
5. **Reserved region:** no other master may write it, neither the decoder's framestore
   nor VBUF, nor anything Main touches. ⏳ Choose the address against the existing DDR3
   map before P2.

**Conflict to resolve before P2: merging the PCM FIFOs.** Merging the three per-codec PCM
FIFOs into one (a planned reclaim, about 20 M10K) would remove a host. If the merge lands
first, the second half of `adpcm_vb` needs a different write-first RAM: one of the AC-3 or
MP2 working buffers (`imdct_512` holds 23 M10K). **Order: wire the copy path first (P2),
then do the merge (P4) and move the half-table in the same change.**

**Port:** `ram2`, idle by construction (`hw_budget_and_lessons.md` §2), which needs the
`sysmem_lite` / `sys_top.v` plumbing that note lists, a `sys/` edit. The fetch traffic is
about 0.5 MB/s, so clocking `ram2` on `clk_sys` avoids a CDC; the `clk_mem` suggestion
there was for a line pump. ⏳ Confirm that `sysmem_lite` accepts a `clk_sys` `ram2_clk`.
`ram1` is the decoder's port and is not used: occupancy, not bandwidth, is what hurt it
before.

### D5 — Overflowed block codes are decoded leniently and counted (✅ decided by the maintainer, 2026-10-02)

A block code packs four samples as base-`levels` digits. A code of `levels⁴` or more is out
of range. **FFmpeg refuses the whole frame**; **libdca keeps the four low digits** and
prints an error. One library disc, *Shadoan* (`SHADOAN_1_2.iso`), carries 428 such codes
in 240 of 482 frames of one window, across six title sets. FFmpeg refuses exactly those
240 frames. Decoded leniently, every frame parses cleanly through its DSYNC words, so the
frames are valid and only those codes overflow (an encoder quirk). Refusing them would
silence that disc about half the time.

**Is the audio viable? Measured 2026-10-02 on the *Shadoan* window (482 frames):**
- **The frames are real audio, and the bad codes are rare encoder slips.** Overflow
  rates per allocation level are 0.02–0.79 %. If those fields were garbage, the rate
  would be (2ⁿ − L⁴)/2ⁿ, 13–41 %. The valid codes' samples have the small-value statistics
  of the Terminator 2 control (mean |sample| 1.93 at L = 17, against 1.71 on T2 and 4.24
  if uniform).
- **The intended value cannot be recovered.** A one-step carry past the quantiser's edge
  would leave the top digit at 0. The top digits are spread across the range instead,
  and the overflow digit is always 1. So no decoder can know what the encoder meant;
  what matters is that the error is bounded.
- **It is bounded, and inaudible by these measures.** A lenient decode keeps all four
  samples inside the quantiser's legal range, so the error is confined to four subband
  samples of one band per bad code. Decoding leniently and zeroing those four samples
  differ by **1.0 LSB RMS against a 750 LSB RMS signal (−58 dB)**. Frames with a bad
  code are indistinguishable from clean ones in click measure (second-difference p95: 894
  against 1,925 on clean frames) and in level continuity against their neighbours (p95:
  2.26 dB against 2.03 dB).
- **FFmpeg's refusal is the audible option.** It silences 240 of the 482 frames: 10.7 ms
  dropouts about every other frame, level jumps of about 60 dB, a stutter.

**Decision (maintainer, 2026-10-02): decode leniently**, as libdca does: keep the four low
digits and count every overflowed code in telemetry (never silent, CLAUDE.md). A hardware decoder that derives
the digits by divide-and-remainder without a range check behaves identically; the
counter is the only addition. `tools/dts_fixed.py`, the
hardware's model, does this. `tools/dts_ref.py` matches FFmpeg by default and takes
`OPT={'lenient_block'}`. Bit-exact comparison against FFmpeg is impossible on such frames
(FFmpeg outputs none), so `tools/test_dts_ref.py` reports the stream as REFUSED, and
`tools/test_dts_fixed.py` gates the lenient decode (both of its decoders run lenient).

## 3. The stream forces a streaming decode (design rule)

A frame at the spec maximum does not fit in a frame buffer. `npcmblocks` is a 7-bit field
plus 1, a multiple of 8, so a frame can carry up to 128 subband samples per band (4,096
PCM samples). DVD's usual value is 16. Buffering a whole frame of subband samples at the
maximum would take 32 bands × 128 × 6 channels ≈ 24K entries, about 58 M10K at 24 bits:
the codebook problem a second time.

The bitstream order avoids it (verified in FFmpeg `dca_core.c`, `parse_frame_data`). Each
subframe is `parse_subframe_header` (the side info for **all** channels, including every
ADPCM VQ index), then `parse_subframe_audio`. The audio data opens with every
high-frequency VQ index and the LFE samples, then loops over subsubframes (each 8 samples
× bands × channels).

**Rule: decode per subsubframe as it is parsed.** Dequantise, predict, expand VQ, downmix
and synthesise each 8-sample subsubframe before parsing the next. Never hold a frame.

✅ **Proven equivalent (2026-10-02).** FFmpeg runs its inverse ADPCM, VQ expansion and
joint intensity once per *subframe*, after all of that subframe's sample codes. Each is
causal within a band, so doing them per subsubframe must give the same samples.
`tools/dts_fixed.py` decodes in the streaming order, and its subband samples are
**bit-identical** to `tools/dts_ref.py`'s FFmpeg-order ones. This holds on Terminator 2,
on a 1536 kbit/s *A.I.* window with transients, and on all seven synthetic fixtures
(`tools/test_dts_fixed.py` [1]; its `stale_history` RED arm proves the check can fail).

**Persistent state** is small and sized to the maximum:
- ADPCM history: 4 samples × 32 bands × channels.
- The parse record for one subframe.
- Two QMF histories (L/R, 512 taps each).

**Codebook prefetch.** Every codebook index comes before any sample code that needs it:
the ADPCM indices are in the subframe header, and the VQ indices open the subframe's
audio data. So the microcode issues the DDR3 reads **as each index is parsed**, and
the vectors are local before dequantisation needs them. At the maximum that is about 200
fetches per subframe; serialised after the header, that would be about 1 ms of a 10.7 ms
frame, which is why issue-as-parsed matters. A small local vector buffer (one subframe's
worth) holds them.

## 4. Budget

**Fabric (last fit, the menu-panscan build, 2026-10-01):** 40,785 / 41,910 ALM (97 %,
**~1,125 spare**), 512 / 553 M10K (41 spare), 95 / 112 DSP (17 spare).

**Today's audio path, for reference:**

| Module | ALM | M10K | DSP |
|---|---|---|---|
| `dvd_audio_decode` total | 6,371 | 107 | 20 |
| `ac3_front` | 4,754 | 47 | 13 |
| `imdct_512` (inside `ac3_front`) | 2,062 | 23 | 9 |
| `mp2_decode` | 878 | 37 | 5 |
| `audio_ring` | 229 | 34 | 0 |
| `lpcm_unpack` | 107 | 16 | 0 |

**DTS estimate:** about **1,500–2,500 ALM**, about **18–25 M10K** without the codebooks
(microcode 5–10, Huffman tree 7–9, QMF prototypes 2–3, persistent state), and 1 DSP. It is an
estimate until P1's standalone fit replaces it. It exceeds the ALM spare, so **a reclaim
must land before or with the wiring** (P4 lists them; the net-by-scenario table below
shows which).

**Net ALM by scenario (2026-10-02; estimates except where the fit report measured a
module).** Each scenario builds on the one above it. Module figures move ±5–10 % with
packing.

| Change | ALM | Basis |
|---|---|---|
| DTS engine: sequencer ~800 + vector ops ~700–1,000 + codebook fetch and vector buffer ~100–150 | +1,600 … +2,000 | estimate |
| Codebook copier | +50 … +100 | estimate |
| `ram2` live: our master + the port's terminator (17 → ~52, as `ram1`'s) | +100 … +200 | estimate; terminators measured |
| `T_DTS` arm, telemetry words | +50 … +150 | estimate |
| **A. DTS alone** | **+1,800 … +2,450** | |
| One bit reader (MP2's `bit_reader` + `bit_fifo` = 189, measured) | −150 | net of the source mux |
| One PCM FIFO (`pcm_out` 117, `lpcm_unpack` 107, MP2's FIFO control) | −100 … −200 | the DTS output shares it too |
| **B. DTS + the cheap merges** | **+1,450 … +2,200** (central ~+1,800) | |
| MP2 onto the engine (`mp2_decode` 878, measured, minus its bit reader already counted, plus a microcode program and a grouping op) | −550 … −650 | estimate |
| **C. B + MP2 on the engine** | **+800 … +1,650** (central ~+1,200) | |
| AC-3 parse onto the engine (`ac3_parse` 4,483 − `imdct_512` 2,062 = ~2,420 measured; keeps the IMDCT; adds bit-allocation vector ops ~300–600 for the cycle budget) | −1,300 … −2,000 | estimate, the least certain row |
| **D. C + the AC-3 parse on the engine** | **−1,200 … +350** (central ~−450) | |

Against ~1,125 spare at 97 %, only **D** fits with margin. **C** lands at about 100 % and
would not be expected to fit or close timing. **A** and **B** do not fit. So the AC-3 parse
migration is on the critical path, not optional, unless P1's standalone fit comes in near
the bottom of its range. P1's fit replaces the engine rows. A migration is counted only
when its own fit measures it.

**Cycles (27 MHz, about 27M a second). Measured by `tools/dts_fixed.py`'s operation
counters (2026-10-02) on the heaviest stream type found, a 1536 kbit/s 5.1 window from
*A.I.*:**
- **Synthesis:** FFmpeg's factorised half IMDCT costs 288 multiplies per 32-sample block,
  plus 512 window taps: about 800 MACs a block, against about 2,560 for MP2-style direct
  matrixing. Stereo after the downmix is 32 blocks a frame, about **25.6K MACs a frame,
  about 2.4M a second**. ⏳ **P1 decision:** keep the factorised IMDCT as the synthesis
  vector op. It is also what makes the reference bit-exact. A direct matrix would cost
  about three times the cycles and need its own LSB-bounded golden.
- **Front end (dequantisation, ADPCM, VQ, joint, downmix):** **5,246 MACs a frame, about
  0.5M a second.**
- **Parse:** 16,104 bits a frame at 1536 kbit/s, about 1.5M bits a second, in about
  2,240 subband samples a frame (210K a second). Most DVD streams code them as block
  codes (2 codes per 8 samples) or raw 8–23-bit fields, not Huffman. **The risk** is an
  interpreter that pays per *bit*: at 13–19 cycles a bit that is 20M+ a second. If P1
  measures that, the sample-extraction loop becomes a vector op.
- **Total target:** under 60 % of the budget. The arithmetic is about 3M MACs a second;
  the parse is the cost to watch.

## 5. Spec maxima (CLAUDE.md "design to the DVD spec maximum")

The field widths are from FFmpeg's parser. ⏳ **Confirm each against ETSI TS 102 114
before sizing.**

| Field | Width | Max the field allows | What FFmpeg accepts | Size to |
|---|---|---|---|---|
| `nsubframes` | 4 + 1 | 16 | 16 | 16 |
| `nsubsubframes` | 2 + 1 | 4 | 4 | 4 |
| `npcmblocks` | 7 + 1, multiple of 8 | 128 | 128 | 128 (streaming, §3) |
| `frame_size` | 14 + 1 | 16,384 B | ≥ 96 | 16,384: streaming parse, the byte counter sized to it |
| primary channels | 3 + 1 | 8 | must equal the AMODE table: ≤ 5 (AMODE < 10) | ⏳ 5 + LFE if TS 102 114 confirms AMODE 10–15 are user-defined; else refuse, counted |
| `nsubbands` | 5 + 2 | 33 | ≤ 32 | 32; above that, refuse, counted |
| `subband_vq_start` | 5 + 1 | 32 | — | 32 |
| codebook indices | 12 / 10 | 4,096 / 1,024 | — | full tables (D4) |

A hard limit that is hit must be **visible**: a refusal counter and the last error code
in telemetry, never wrong audio.

## 6. Verification chain

1. **Tables** come from a pinned FFmpeg source via `tools/gen_dts_tables.py`, with a
   `--check` that regenerates and compares. Format facts are transcribed with credit:
   FFmpeg is LGPL-2.1-or-later, which may be carried under GPL. Add NOTICE and
   `site/content/about/acknowledgements.md` entries when this ships.
2. **A float reference model** (`tools/dts_ref.py`, written from the spec and FFmpeg's
   logic, not copied) is checked against the FFmpeg binary's output across the library's
   DTS tracks.
3. **A fixed-point model** (`tools/dts_fixed.py`) is the bit-exact golden for the RTL.
   It is within an LSB bound of FFmpeg's `BITEXACT` core path, with FFmpeg mixed after
   synthesis (D3). Round half up at every shift; every stage boundary saturates and counts.
   On a legal stream nothing but the PCM output may saturate.
4. **An assembler plus instruction-level emulator** (`tools/dts_isa.py`): its parse record
   is scored field by field against the reference model, and its PCM bit-exact against the
   fixed model.
5. **RTL:**
   - The sequencer is scored against the emulator by trace: every register write and
     every store, in program order.
   - The whole decoder is scored after every vector op by checksums of the engine
     buffers, and on every PCM pair. The first engine that differs is named.
   - Every check has a RED mutation (`--red`) that must fail exactly its own arm. Score
     with `!==`.

## 7. Phases

- **P0 — tools and measurement (✅ done; D3 and D5 decided 2026-10-02).**
  - The pinned table generator.
  - The reference, bit-exact against FFmpeg.
  - The hardware-order model: front end bit-exact, downmix within 1 LSB.
  - The fixtures: encoded, derived and written. The bitstream writer
    (`tools/dts_writer.py`) also supplies P1's spec-maximum test streams.
  - The two gates, and the census (§9).
  - `gen_dts_tables.py`.
  - **A library sweep, `tools/dts_scan.py`**, modelled on `tools/acmod_scan.py` (PES
    first-access-unit pointer, not a sync-word search). For every DTS track in
    `$DVD_ISO_DIR` it records: AMODE, LFE, bitrate, `npcmblocks`, `nsubframes`, the
    extensions present, and the fraction of frames using ADPCM prediction (PMODE) and
    high-frequency VQ.
    ⚠ Unlike acmod, PMODE and VQ change **per frame**, so the sweep must not stop at the
    first frames.
    The sweep sets priorities and test fixtures. It does not set limits: §5 does.
  - `dts_ref.py`, `dts_fixed.py`, and the LFE and downmix decisions (D3 ⏳).
- **P1 — the engine, standalone.** ISA, assembler, emulator, microcode, RTL, benches, then
  a standalone fit with every port a virtual pin. **This is the go/no-go on ALM and
  cycles.** A previously built microcoded decoder of this kind is the template for the
  ISA and the verification method; ⚠ port it under new file names, and strip header
  comments that reference anything outside this repository.
- **P2 — codebook residency.** Initialised hosts, the copier, the `ram2` plumbing
  (`sys/`), the reserved region, `dts_tables_ok` plus checksum, the bench and its
  mutation, and the map-report inference check.
- **P3 — wiring.** The `T_DTS` arm in `dvd_audio_decode` drives the engine instead of
  discarding. Then a `tools/check_dts_wiring.py`, telemetry words, HIL on DTS discs, and
  the manual: `audio/formats.md`, `audio/passthrough.md` (three statements that DTS has
  no fallback), `reference/compatibility.md`, possibly a README headline limitation.
  A minor version bump.
- **P4 — reclaim.** One bit reader, one PCM FIFO (moving the `adpcm_vb` half per D4), and
  moving MP2 and then the AC-3 parse onto the engine. §4's scenario table shows the AC-3
  parse move is needed for DTS to fit, so it is not optional. AC-3's IMDCT stays
  hardwired: a direct-form transform for 5.1 needs 61–74M multiply-accumulates a second,
  2.3–2.7× one multiplier at 27 MHz. Each move is gated trace-identical
  (`logic_reclaim.md` method) and the AC-3 path bit-exact against `bench/ac3`.

## 8. Open questions

- ✅ D3, decided: LFE is out, the mix follows the AC-3 path, and embedded coefficients
  are ignored but counted.
- ⏳ The DDR3 address for the codebooks, and `ram2` on `clk_sys` (D4).
- ⏳ AMODE 10–15 (user-defined, 6–8 channels): refuse or map (§5).
- ⏳ Dynamic range compression (`DYNF`): ignore, or apply under an OSD option (an option
  would need `playback/settings.md`, which the docs parity check enforces).
- ⏳ The core's CRC (`CPF`): check and refuse, or ignore as `mp2_decode` does. No library
  stream sets it (§9).
- ✅ D5: overflowed block codes are decoded leniently and counted (decided).
- ⏳ The default downmix rule (D3): the AC-3 path's liba52 Lo/Ro is proposed. *Cinderella
  III* is the one library stream with embedded coefficients, so it is the test case for
  honouring them.
- ✅ A joint-intensity test stream: `tools/dts_writer.py` (§9).

## 9. Library census (2026-10-02, `tools/dts_scan.py`)

**Method.** Every image under the library root was opened. Each title set declaring a DTS
stream had 8 windows of 500 sectors sampled evenly across its title VOBS (`0xC4` to
the title set's end), and every frame in them was parsed with `tools/dts_ref.py`'s front
end. That covered **147 streams on 90 discs, 145,786 frames**. 23 images could not be
read (not ISO9660, or not DVD-Video: no `VIDEO_TS`). Raw rows are in
`.sim/dts/scan.jsonl` (gitignored).

| Feature | Streams | Frames |
|---|---|---|
| ADPCM prediction | **147 (all)** | 85 % |
| High-frequency VQ | **118** | 77 % |
| Transients (two scale factors) | 66 | 30 % |
| Huffman sample codes | 0 | 0 % |
| Joint intensity | 0 | 0 % |
| Front / surround sum/difference | 0 / 0 | 0 % |
| Embedded downmix (aux data) | 1 (*Cinderella III*, every frame) | 0.5 % |
| Dynamic range, header CRC | 0 | 0 % |
| XCh extension (DTS-ES discrete 6.1) | 8 | — |
| `es_format` (ES matrixed) | 15 | — |

**Uniform across the library:**
- AMODE 9 (3/2) with LFE flag 2 (64× interpolation).
- `npcmblocks` 16, one subframe per frame.
- The non-perfect filter.
- Frames of 1,006 or 2,013 bytes (768 kbit/s on 105 streams, 1536 kbit/s on 42).
- Subband counts up to 32.
- Predictor history on.

**Errors:** only the *Shadoan* overflowed block codes (D5).

**What this means.** Both trained codebooks are needed on real discs: ADPCM in every
stream, VQ in four of five (confirming D4). Huffman sample codes, joint intensity and
sum/difference never appear on these discs. They are in the format, so they are built
and tested anyway (§5): the tests come from synthetic and derived fixtures, below. A
sweep says what is common, not what is possible.

### Test coverage of the reference (`tools/test_dts_ref.py`)

The gate set (`~/dts-streams/gate`, local, never committed), 34 streams:
- **Twelve from `tools/gen_dts_fixtures.py`**, made from generated signals only:
  - Eight encoded by FFmpeg's DTS encoder: mono to 5.1, 192k–1536k. These supply the
    **Huffman sample codes**, and one near-full-scale stream drives the half IMDCT's
    **pre-shift**.
  - Four derived by rewriting fixed-width fields the encoder never sets:
    `filter_perfect`, `sumdiff_front` and `sumdiff_surround`, and the coding header's
    2-bit **Huffman scale-adjustment** indices (set to ×1.4375).
  - The sum/difference streams are encoded in sum/difference form, `(a+b)/2` and
    `(a−b)/2`, so the decoder's butterflies rebuild in-range signals as a real stream's
    would. ⚠ The first version flagged ordinary L/R content instead. Its `L+R` passed full
    scale, FFmpeg clipped each speaker, and the downmix model (which scales before it
    could clip) differed by 3,583 LSB. That was an artefact of the fixture, not a decoder
    fault.
- **Fifteen from `tools/dts_writer.py`**, our own syntax-level DTS core encoder. It writes
  frames field by field from a description, fills that description with a seeded
  generator of legal, moderate-level values, and parses every frame back to compare.
  It makes what nothing else does:
  - **joint intensity** (three shapes at once);
  - the **frame-shape spec maxima**: `npcmblocks` 128 (4,096 PCM samples a frame), as
    16 subframes of 1 and as 4 subframes of 4 subsubframes with `sync_ssf`. Measured on
    the streams: 32 subbands, frames of 9.0–14.6 KB;
  - **front and surround sum/difference with uneven band counts per pair**
    (`sumdiff51`, and `amode3` for stereo): the pair-bound rule's two test streams;
  - **all ten channel arrangements** (AMODE 0–9; FFmpeg's encoder covers four);
  - every bit-allocation, scale-factor, transient and quantiser-index codebook
    selector, and bit allocations up to 26;
  - header CRC words, DRC, time code, aux data with downmix coefficients and a valid
    CRC, predictor history off, and the lossless step table.

  ⚠ The comparison passes `-f dts`: FFmpeg's raw-DTS probe rejects some legal streams,
  such as a small mono frame whose bit-rate field disagreed with it. The writer now also
  sets the bit-rate field from the real frame size.
- **Seven disc windows:** Terminator 2, plus windows for XCh, embedded downmix, 1536k
  with transients, 768k with VQ and transients, ES, and the *Shadoan* overflow.
  ⚠ The comparison runs FFmpeg with `-core_only 1`. Without it FFmpeg decodes XCh, and a
  DTS-ES disc returns 7 channels to our 6.

**Every stage now has a stream that exercises it** and a RED arm that bites on it,
joint intensity included: `tools/test_dts_ref.py` reports no coverage gaps. The writer's
streams are bit-exact through `tools/dts_ref.py` against FFmpeg, spec-maximum frames
included. Every stream passes `tools/test_dts_fixed.py`: the streaming front end is
bit-exact and the downmix is within 1 LSB.

## 10. The engine: ISA, memory map and datapath (P1a, 2026-10-02)

**Machine.** A microcoded sequencer with 40-bit instruction words and sixteen 16-bit
registers (`r0` reads 0; `r8`–`r15` carry vector-op arguments). Its instructions: ALU
operations (with immediate forms), loads and stores, `get n` (up to 16 bits, MSB first),
`vlc` (a Huffman symbol), compare-and-branch, call and return, `err`, and `vop` (run a
hardwired loop, with the sequencer stalled until it finishes). `tools/dts_isa.py` is
the executable definition: the assembler, plus an instruction-level emulator that the
RTL must match instruction by instruction. The program is `dvd/dts/dts.uasm`.

**ISA decisions, each with the alternative rejected:**
1. **Sample codes wider than 16 bits** (raw fields of up to 23 bits) **are never read
   by the sequencer.** One vector op, `XQ`, reads a band's 8 codes itself (Huffman,
   block or raw), dequantises them, and writes the subband buffer. The sequencer
   passes `XQ` only indices and selectors (abits, quantiser selector, scale index,
   adjustment index), which all fit 16 bits. `XQ` and the sequencer share one bit
   reader; that is safe because a vector op runs with the sequencer stalled.
   *Rejected:* 24-bit registers. That widens every ALU path, the register file and the
   record RAM for a few fields, and leaves the per-sample loop interpreted, which is the
   §4 cycle risk.
2. **Block-code division** happens inside `XQ`: dividing a code of up to 19 bits into
   four digits by one of seven levels (3–25). The emulator charges ~6 cycles a digit
   (multiply by a reciprocal, one correction); restoring division would be ~19. P1b
   picks the method against the measured cycle budget, below.
3. **The codebook port** is a request/response memory with a latency parameter. In P1
   the emulator reads the tables directly and charges the latency; `ram2` arrives in P2.
   **Both codebooks have 64-bit rows:** an ADPCM vector is 4 × int16, and one
   subsubframe's slice of a VQ vector is 8 × int8. So one DDR3 beat serves one band,
   and the local buffers hold one slice per band. An ADPCM vector is fetched when its
   index is parsed (subframe side info); a VQ slice at the start of each subsubframe,
   while `XQ` is still reading that subsubframe's sample codes.
   *Rejected:* buffering whole 32-byte VQ vectors per subframe: 4–5 M10K instead of 1–2.
4. **Datapath: one 27×27 multiplier and a 56-bit accumulator.** Measured operand
   widths, all signed:
   - subband samples, scale factors, steps and window coefficients: 24 bits;
   - IMDCT constants: 24–27 bits. The single exception, `mod_a`'s −85,479,984, needs
     28 bits; it is even, so it is stored halved with the rounding shift cut from 23
     to 22, which is exact.

   Largest sums: the window's eight taps plus the carried partial sum, 2^48.8; `dct_a`,
   2^49.0; `step × scale`, 2^45.7. So 50 bits plus sign suffice, and 56 leaves margin.

**Vector ops**, one hardwired loop each. Each calls the matching `tools/dts_fixed.py`
function, so the emulator is anchored to the model and not to itself:

| op | what | model function |
|---|---|---|
| `XCLR` | zero the subsubframe's subband buffer | — |
| `XQ` | read 8 codes (Huffman / block / raw, lenient per D5), dequantise one band | `R.extract_audio`, `dequant_band`, `huff_scale` |
| `XVQ` | 8 VQ values × scale, one band | `vq_band` |
| `ADPCM` | 4-tap prediction over 8 samples, or the plain history update | `adpcm_band` |
| `JOINT` | a joint-intensity band from its source | `joint_band` |
| `BFLY` | sum/difference of a channel pair, every band | `butterfly_band` |
| `MIXSYN` | sample j: mix both sides (AC-3 gains by AMODE, pair-bounded), half IMDCT, window, 32 PCM pairs out | `mix_column`, `SynthFixed.run`, `to_s16` |
| `HCLR` | clear one channel's ADPCM history from a band up | — |
| `CNT` | count an event in telemetry (`dmix_ignored`) | — |

**Memory map and M10K estimate** (spec maximum: 5 primary channels, 32 bands; codebooks
off-chip per D4):

| Memory | Size | M10K |
|---|---|---|
| Subband buffer, one subsubframe | 5 × 32 × 8 × 24 bit | 5 |
| ADPCM history | 5 × 32 × 4 × 24 bit | 3 |
| Record RAM (side info: abits/tmode/pmode, two scale indices, VQ index, joint scale; coding header) | ~1K × 16 | 2 |
| Codebook buffers: an ADPCM vector per band, a VQ slice per band | 2 × 160 × 64 bit | 2–4 |
| Synthesis rings and carried partial sums, ×2 sides | 2 × (512 + 32) × 24 bit | 3 |
| Window prototypes, perfect and non-perfect | 2 × 512 × 24 bit | 3 |
| IMDCT constants, scale / step / joint / adjustment tables, AC-3 gains | ~600 words | 1–2 |
| Huffman tree, every core book | 2,647 nodes × ~26 bit | 9 |
| Microcode | ~1K × 40 bit (estimate) | 4–6 |
| **Total** | | **~32–37** |

⚠ **This is above §4's 18–25 estimate.** The Huffman tree (9, not 4) and the
per-subsubframe buffers were under-counted there. It still fits the 41 free M10K with
the codebooks off-chip, but not with much to spare. The planned PCM-FIFO merge (~20,
P4) is what restores margin, and P1b's standalone fit replaces these numbers.
