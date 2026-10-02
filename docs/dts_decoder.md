# In-fabric DTS core decoder (`dvd/dts/`, planned)

**Status: 📝 DESIGN — decisions recorded 2026-10-02, no RTL yet.** Branch
`feature/dts-decode` (`CORE_VERSION dev-dtsdecode`). Next concrete step: **P0** (§7).

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
- ⏳ **LFE: proposed to be left out of the stereo downmix** (the usual stereo-downmix
  practice). LFE is not subband-coded but decimated samples with their own interpolation
  FIR, so dropping it also removes that path. **Not yet decided by the maintainer.**
- ⏳ **Downmix coefficients:** the standard ones per AMODE unless the stream embeds its
  own. Confirm where the core carries embedded coefficients before P0's model is final.

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

## 3. The stream forces a streaming decode (design rule)

A frame at the spec maximum does not fit in a frame buffer. `npcmblocks` is a 7-bit field
plus 1, a multiple of 8, so a frame can carry up to 128 subband samples per band (4,096
PCM samples). DVD's usual value is 16. Buffering a whole frame of subband samples at the
maximum would take 32 bands × 128 × 6 channels ≈ 24K entries, about 58 M10K at 24 bits:
the codebook problem a second time.

The bitstream order avoids it (verified in FFmpeg `dca_core.c`, `parse_frame_data`). Each
subframe is `parse_subframe_header` (the side info for **all** channels, including every
ADPCM VQ index and every high-frequency VQ index) followed by `parse_subframe_audio`, which
loops over subsubframes (each 8 samples × bands × channels).

**Rule: decode per subsubframe as it is parsed.** Dequantise, predict, expand VQ, downmix
and synthesise each 8-sample subsubframe before parsing the next. Never hold a frame.

**Persistent state** is small and sized to the maximum:
- ADPCM history: 4 samples × 32 bands × channels.
- The parse record for one subframe.
- Two QMF histories (L/R, 512 taps each).

**Codebook prefetch.** Every codebook index is in the subframe header, ahead of any sample
code that needs it. So the microcode issues the DDR3 reads **as each index is parsed**, and
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

**Cycles (27 MHz, about 27M a second):**
- **Synthesis (stereo after the downmix):** about 80 multiply-accumulates per output
  sample per channel, about 7.7M a second.
- **ADPCM, dequantisation and downmix:** about 2M a second at 5 channels.
- **Parse:** about 240K sample codes a second at 1536 kbit/s. At about 15–20 cycles per
  code that is about 4–5M a second. **The risk:** an interpreter that costs 13–19 cycles
  per *bit*, not per code, would need 20M a second or more and blow the budget. If P1
  measures that, the sample-code loop becomes a vector op.
- **Total target:** under 60 % of the budget.

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

- **P0 — tools and measurement (next).**
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

- ⏳ LFE in the downmix, and embedded downmix coefficients (D3).
- ⏳ The DDR3 address for the codebooks, and `ram2` on `clk_sys` (D4).
- ⏳ AMODE 10–15 (user-defined, 6–8 channels): refuse or map (§5).
- ⏳ Dynamic range compression (`DYNF`): ignore, or apply under an OSD option (an option
  would need `playback/settings.md`, which the docs parity check enforces).
- ⏳ The core's CRC (`CPF`): check and refuse, or ignore as `mp2_decode` does.
