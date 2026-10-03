# MP2 on the shared audio engine (the rest of scenario E)

**Status (2026-10-03): ✅ M0 done (the model is the RTL's contract on all 89 gate streams); 🔧 M1 next.** Branch `feature/mp2-engine` (from
`feature/ac3-engine`, `CORE_VERSION dev-mp2engine`, not pushed).

**Why.** The engine built for DTS already runs AC-3 (`docs/ac3_engine.md` W1) and DTS
(`docs/dts_decoder.md` P2/P3). `dvd/mp2/mp2_decode.sv` is the last hardwired decoder:
839 ALM, 37 M10K, 5 DSP in core (the DTS build's fit, 2026-10-03). Moving it onto the
engine is estimated at **−400 … −600 ALM, about −15 M10K, −5 DSP**. There is no fit
pressure (~1,060 ALM spare with DTS in), so this is headroom for later features, not a
need.

**The risk is VCD.** Video CD and SVCD audio is MP2, at 44.1 kHz too, so a regression
reaches every VCD. Hence the same discipline as AC-3: the new path must be
**bit-identical to `mp2_decode`**, scored A/B on the same streams, not "close to a
reference".

## The contract

`mp2_decode` is bit-exact against `tools/mp2_ref.py` (`bench/dvd/run_mp2.sh`), an
integer-only model whose header states the fixed-point contract. The engine reproduces
it exactly:

| Step | Arithmetic | On the engine |
|---|---|---|
| dequantise | `s = (x << (25 − nb)) + (1 << (24 − dsh))`, `(s × C_q16) >> 16` → Q2.24 | one multiply |
| scale | `sat27((s × SCF_q20) >> 20)` | one multiply; S needs **27 bits** (X is 25 today: widen, +1 M10K) |
| matrix | `V[i] = sat32((Σ_k N_q14[i][k] × S[k]) >> 14)` | 2,048 MACs per channel per 32 samples (N 16-bit × S 27-bit fits) |
| window | `pcm = sat16((Σ_16 D_q16 × V + 2^24) >> 25)` | **V is up to 32 bits** — over the 27×27 multiplier: each tap is TWO MACs, `D × V_lo` and `D × V_hi` with the product shifted 16 (a new datapath option) — 1,024 MACs |

- Grouped samples (3/5/9-level) come out of the sequencer's divider. The model takes the
  LOW digit first (`code % nlevels`, then `//=`), which is the divider's own order.
- Mono: `mp2_decode` synthesises channel 0 only and duplicates it (bit-identical to the
  model, which synthesises a duplicated channel 1). The engine does the same.
- Dual channel, joint (intensity) stereo: as the model.

**Cycles:** ~3,100 MACs per 32 output samples per channel. At 48 kHz stereo that is 36 ×
2 × 3,100 ≈ 221K per 24 ms frame of 648K cycles, about 34 % + the parse, ~40 %. Lower
at 44.1 and 32 kHz. The factorised transform (DTS's half IMDCT) would be ~3× cheaper
but rounds differently: rejected, because bit identity is the VCD safety net.

## New engine resources (estimate)

- **Datapath:** a product-shift-by-16 option and a 32-bit saturation (~+60 ALM).
- **V ring:** 2 channels × 1,024 × 32 bits. DTS's IMDCT ring (1,024 × 24) widens to
  2,048 × 32 and is shared (+4 M10K).
- **ROMs:** N (2,048 × 16, 4 M10K; symmetry could halve it), D (512 × 18), SCF (63),
  C / D-shift by class, the allocation tables (B.2a–d).
- **Microcode:** header, table select, allocation, scfsi, scalefactors, 12 granules.
  The program ROM holds 1,292 of 2,048 words today, so it may need 4K (+8 M10K).
- **Freed:** `mp2_decode` (839 ALM, 37 M10K, 5 DSP), minus its PCM FIFO, which STAYS:
  it carries half the DTS ADPCM codebook at power-up (D4), and it becomes the MP2 output
  FIFO.

## Plan

- **M0 — the model and the corpus.** `mp2_ref.py` is the model (already bit-exact
  against the RTL). Build the gate set, local and never committed (`~/mp2-streams/gate`,
  `$MP2_TEST_DIR`): real VCD windows, DVD MPEG-audio tracks from the library, and
  synthetic streams for every rate, mode and allocation table. Then a census of what
  the real material uses.
- **M1 — the program and its emulator.** `dvd/dts/mp2.uasm`, `tools/mp2_isa.py`,
  bit-exact against `mp2_ref.py` on every stream; the cycle budget.
- **M2 — the RTL.** The vector ops (dequantise and scale, matrix, window with the split
  MAC), the sequencer's parts, and a gate `run_mp2_seq.sh`. Then **an A/B bench against
  `mp2_decode`**, every PCM pair, as `run_ac3_ab.sh` did for AC-3.
- **M3 — wire in.** `mp2_decode` out of `dvd_audio_decode`. Its PCM FIFO stays as a plain
  FIFO module (MP2 output + the codebook host). The output rate (`fs_o` → the NCO) comes
  from the program. Then `run_mp2`, `run_vcd`, `run_wav` and the chain benches.
- **M4 — fit and HIL:** VCD rips (44.1 kHz), a DVD MP2 track, and the rig's VCD path.

## M0 result (2026-10-03)

**The corpus** (`$MP2_TEST_DIR`, default `~/mp2-streams/gate`; local, never committed):

- `tools/mp2_scan.py --extract DIR`: three windows of every MPEG-audio stream in the DVD
  library (`$DVD_ISO_DIR`, title sets declaring audio coding mode 2) and of every VCD
  rip's `*Track 2*.bin` (`$VCD_DIR`). 17 streams from 3 DVDs and 3 VCDs.
- `tools/gen_mp2_streams.sh`: 72 synthetic `twolame` streams in `synth/`, covering
  32 / 44.1 / 48 kHz × stereo / joint / dual / mono × 32–384 kbit/s, with CRC variants.

**The census** (what real material uses): VCD is 44.1 kHz 224k, stereo and joint
(mode_ext 1–3), table B.2b. The DVDs are 48 kHz, 256k stereo (two discs) and 384k with
CRC (one disc). No real stream uses 32 kHz, dual channel, mono, or tables B.2c/d. Those
come from the synthetic set alone, so it is what keeps the engine honest there.

**The gate:** `bench/dvd/run_mp2_model.sh` writes a `mp2_ref.py` fixture of 24 frames
for each stream and runs `mp2_decode_tb` on it. **89 of 89 bit-exact**, 27,648 samples
each. Corrupting one golden word fails the bench, so it can fail. This makes the model the
contract on the whole set, so M1's emulator is scored against `mp2_ref.py`, and M2's RTL is
scored both against the model and A/B against `mp2_decode`.
