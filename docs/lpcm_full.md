# Full DVD-Video audio: every legal LPCM format, and AC-3 dual mono

**Status (2026-10-06): ✅ HW-CONFIRMED on the rig (§12), A/B against `main`; ⏳ PR. Decided (§10): dedicated RTL, the `sys_top` tap,
FFmpeg's channel order.** Branch `feature/lpcm-full`,
`CORE_VERSION "dev-lpcmfull"`.

This replaces audit item 4 of the 2026-10-01 *DVD Demystified* 3rd-edition audit
("Unsupported audio fails silently", unmerged branch `docs/demystified-3rd-audit`,
`docs/roadmap.md` "2026-10-01 spec-audit" and `docs/conformance.md` §4). The maintainer
decided to **support** these formats rather than announce them. `AUDIO UNSUPPORTED` stays
only for header values the format reserves (§2).

## 1. What is legal, and what the core does today

| | DVD-Video allows (3rd ed. Tables 9.27, 9.28) | today |
|---|---|---|
| LPCM rate | 48 or 96 kHz | 48 only. A 96 kHz track plays at half speed, with no message |
| LPCM word length | 16, 20, 24 bit | all three, truncated to 16 (✅ HW, PR fj#133) |
| LPCM channels | 1 to 8 | **2 only**. Mono plays at double speed (consecutive samples become L/R). 3–8 channels are mis-paired |
| LPCM bit rate | ≤ 6.144 Mbit/s | n/a |
| AC-3 acmod 0 (1+1 dual mono) | legal | refused: silence (`docs/ac3_decoder.md` "NOT supported") |

The 6.144 Mbit/s cap bounds what can exist: 96 kHz carries at most 2 channels at 24 bit,
3 at 20 bit and 4 at 16 bit; 48 kHz at most 5 / 6 / 8. Tables are still sized to 8
channels (the spec-maximum rule); the decimator never sees more than 4 input channels.
Worst-case input is 768 KB/s, about one byte per 35 `clk_sys` cycles (27 MHz).

Library exposure is nil: 0 of 1,430 discs carry 96 kHz or multichannel LPCM, and acmod 0
appears as 4 frames on 1 disc of 491. The real-world vehicles are audiophile "96/24"
DVD-Video discs and some concert discs.

## 2. The LPCM header and the packing (verified against two decoders)

The 6-byte sub-header after the substream ID (`ps_demux` `S_AUD_SUBHDR`, bytes_remaining
6→1): frame count, first-access-unit pointer (2), `emph|mute|res|frame#`,
**`quant[7:6] | freq[5:4] | res[3] | (channels−1)[2:0]`** (bytes_remaining == 2, the byte
`ps_demux` already reads for `quant`), dynamic range. Source: FFmpeg
`libavcodec/pcm-dvd.c` `pcm_dvd_parse_header` and VLC `modules/codec/lpcm.c` `VobHeader`
agree on every field.

- `freq`: 0 = 48 kHz, 1 = 96 kHz. **2 (44.1) and 3 (32) are not DVD-Video** (the book: "48 or
  96 kHz"; FFmpeg: "no traces … in any commercial software"). They raise
  `AUDIO UNSUPPORTED`.
- `quant` 3 is illegal (FFmpeg rejects it as 28-bit): `AUDIO UNSUPPORTED`.
- A stream over 6.144 Mbit/s is out of spec but decodable. It plays rather than being
  refused, because FFmpeg's own `pcm_dvd` encoder writes them (48 kHz 5.1 at 24 bit).

**20/24-bit packing (FFmpeg).** For 2 or more channels the stream is a flat sequence of
**4-sample groups in stream (channel-interleaved) order**: four high 16-bit words, then
2 low bytes (20-bit) or 4 (24-bit). That is exactly what `lpcm_unpack` already decodes. For
multichannel, only the channel each sample belongs to changes, so a channel counter
replaces the fixed L/R pairing. **Mono is the exception:** its groups are 2 samples (two
high words, then 1 or 2 low bytes). VLC's `VobExtract` uses 4-sample groups for every
channel count, mono included. FFmpeg is followed here, since it special-cases mono
deliberately.

**Channel order: the two references disagree** (no field on the disc says; only DVD-Audio
has a channel-assignment code, 3rd ed. Table 9.40). Both put L, R first.
- FFmpeg applies `av_channel_layout_default`: 3 = 2.1 (L R LFE), 4 = 4.0 (L R C Cs),
  5 = 5.0 (L R C Ls Rs), 6 = 5.1 (L R C LFE Ls Rs), 7 = 6.1, 8 = 7.1.
- VLC does no reordering for VOB, so 6 = L R Ls Rs C LFE (`VobHeader`'s mask in VLC's
  native order).

FFmpeg's reading is also what mpv and Kodi play, and what FFmpeg's `pcm_dvd` encoder writes
for our synthetic streams. **Decision pending, §10.**

## 3. Measured budget (today's fit: `output_files/DVD.fit.rpt`, SEED 7, 2026-10-05)

| | used | free |
|---|---|---|
| ALM | 38,699 / 41,910 (92 %) | **~3,200** |
| M10K | 519 / 553 | 34 |
| DSP | 87 / 112 | 25 |

⚠ `hw_budget_and_lessons.md` §3 still quotes 40,785 ALM (~1,125 free). That figure is from
2026-10-01, before the engine merges reclaimed ~2,000; the fit report supersedes it.

Per entity, for scale: `lpcm_unpack` 119 ALM, 16 M10K. `pcm_out` 127 ALM, 2 DSP.
`audio_engine` 4,637 ALM: `dts_seq` 1,169, `dts_vec` 1,411, `imdct_512` 2,013.

## 4. Option A (recommended): dedicated RTL beside `lpcm_unpack`

`lpcm_unpack`'s pair FIFO is not free to change: it is the engine's DTS/MP2 output FIFO
(the `ser_*` path) and holds half the DTS codebook at power-up (`CB_INIT`, D4). So every
new stage sits on the **producer side**, between the byte assembler and the FIFO write, and
the FIFO still receives stereo s16 pairs.

```
bytes → assembler (+channel counter, +mono groups) → downmix MAC (n ch → L/R)
      → [96 kHz on a 48 kHz link] half-band FIR, ÷2 → pair FIFO (unchanged) → aud_ce
```

- **Bypass:** `nch == 2 && fs == 48k` writes the assembler's pair straight to the FIFO, cycle
  for cycle as today, so the HW-confirmed path and CD-DA/WAV are bit-identical
  (`lpcm_unpack_tb`, `dvd_audio_decode_tb`, the VCD/MP2 suites unchanged).
- **Downmix law:** the same law as the AC-3 path (`imdct_512` S_DMX + `lvl_q`), so the
  manual states one rule. Centre and surrounds go in at −3 dB, LFE is dropped, and the sum
  is normalised by 1/(1 + Σ gains), as liba52 does, so it cannot clip. Mono goes to both
  sides. The coefficients come from a small table indexed by (channel count, channel).
- **Half-band FIR:** about 63–79 taps (pass band to 20 kHz, ≥ 80 dB from 28 kHz).
  Odd-index taps are zero and the filter is symmetric, so there are about 16–20 distinct
  coefficients and one multiply per pair of taps. It runs after the downmix, on 2 channels,
  never more. Load: about 2 × 20 MACs per 48 kHz output, i.e. ~40 of the 562 cycles
  available.

| part | ALM | M10K | DSP |
|---|---|---|---|
| assembler: channel counter, mono groups | 40–70 | 0 | 0 |
| downmix MAC + coefficient table + saturation | 120–180 | 0 | 1 |
| half-band FIR: delay line, control, coefficient ROM | 150–250 | 1 | 1 |
| `ps_demux` rate/channel capture, reserved-value flag | 15–30 | 0 | 0 |
| 96 kHz NCO mode, synchronised link bit | 15–25 | 0 | 0 |
| **total** | **~350–550** | **1** | **2** |

(The downmix and the FIR could share one DSP, since the load is far below one MAC a cycle.
That saves one DSP block for more control logic, and DSPs are the least scarce resource.)

## 5. Option B: a fourth program on the shared audio engine

The engine is idle while LPCM plays, and its output already lands in the same FIFO (the
DTS/MP2 `ser_*` serialiser). But:

- **MP2's window op (MSYN) cannot be reused as the FIR.** Its addressing is hard-wired to
  MP2's matrix/window shape (`dts_vec.sv` V_MM/V_MWIN). A FIR, and a downmix-accumulate,
  would be **new vector ops in `dts_vec`: RTL anyway**. B then saves only the reuse of
  the engine's multiplier, accumulator and RAMs.
- **The constant ROM has 27 words free.** Eight downmix layouts plus the FIR taps do not
  fit, so B needs a new coefficient ROM, or `li`/`st` at RESET (~256 program words).
- **Program ROM:** ~150–250 of the 483 free words. That is a third to a half of what
  `docs/logic_reclaim.md` §10 wants for future reclaim.
- **Cycle budget unproven:** at 96 kHz × 4 channels the scalar get/skip/issue loop has
  ~70 cycles per sample, so it would have to be proved with the `CYC` model.
- **Process:** house style for an engine program is a Python model, an emulator
  bit-exact against it, and an op-for-op RTL A/B (as MP2's M0–M3). That is several times
  A's verification work for a streaming filter with no bitstream parsing in it, which is
  the part the microcode exists for.

Estimate: **~200–400 ALM, 0–1 M10K, 0 DSP.** About 150 ALM and 2 DSP cheaper than A, with
~3,200 ALM free.

**Recommendation: A.** The saving is ~5 % of the remaining headroom. It costs program ROM
that has better uses, a new vector unit regardless, and the engine's heavier verification
route for code that does no parsing. A keeps the stereo 48 kHz path bit-identical by
construction.

## 6. How the core learns the HDMI link rate

`MiSTer.ini hdmi_audio_96k=1` → `sys_top.v:275` `audio_96k = cfg[6]` (`cfg` is a
`clk_sys` register written over the HPS bus) → `audio_out.sample_rate` only. `emu` never
sees it.

- **(a) Additive `sys_top` tap (recommended of the two asked about).** One new `emu`
  input and one line in the instantiation. It works on stock Main, and it *is* the bit
  `audio_out` clocks from, so the core cannot disagree with the link. Inside, a 2-FF sync
  (cheap insurance), latched where `nco_fs` is latched (only while `!draining`), and
  `nco_fs` code 3 = 96 kHz.
- **(b) Main passes it down.** Works only with the custom Main. On stock Main it fails
  safe (decimates), but it is more code and a second reader of the ini key.
- **(c) Always decimate, ignore the link.** No `sys/` edit and no 96 kHz NCO mode. On a
  96 kHz link the 48 kHz output is zero-order-held, as every other codec already is
  today. It loses only content above 24 kHz. The book notes that most players subsample 96
  to 48 anyway, and that the CSS licence caps protected-track digital output at 48 kHz.
  It also avoids the one real 96 kHz-native risk: the 4,096-pair FIFO is 85 ms of
  elasticity at 48 kHz but only 42 ms at 96 kHz.

## 7. AC-3 acmod 0 (dual mono)

- **Parse:** `bsi()` repeats `dialnorm2, compr2, langcod2, audprodi2 (mixlevel2,
  roomtyp2)` when acmod = 0 (liba52 `parse.c` `chaninfo = !acmod` loop). The audio block
  is the generic `nfchans = 2` path. Coupling is allowed (liba52 decodes it), but
  rematrixing and `dsurmod` are acmod 2 only. So the fix is in `dvd/dts/ac3.uasm` (lift
  the refusal and walk the second block, ~10–20 words), plus `tools/ac3_model.py` and
  the emulator.
- **Output:** `imdct_512` already treats 1+1 as no-downmix (`dmx_en = nfchans > 2`;
  `lvl_den` = 131072 → level 2.0, as 2/0), so channel 1 → L and channel 2 → R should need no
  RTL. This is to be confirmed in the bench, not assumed.
- **Vectors:** the 4 real frames (find the disc with `tools/acmod_scan.py`), plus synthetic
  frames made by rewriting a 2/0 stream that has no rematrixing and no `dsurmod`. The
  rewrite inserts the second bsi block and redoes the CRCs. Golden: a52dec
  (`dvd_repos/a52dec-0.8.0`). FFmpeg's AC-3 encoder does not emit acmod 0.
- Gates: `run_ac3_ab.sh`, `run_ac3.sh --red`, `run_ac3_seq.sh --red`,
  `tools/test_ac3_isa.py`, all green and unchanged on the existing streams.

## 8. Test vectors

FFmpeg's `dvd` muxer accepts `-c:a pcm_dvd` (measured 2026-10-05). It writes mono, stereo,
5.1(side) and 7.1, at 48 and 96 kHz, as s16 (quant 0) or s32 (quant 2 = 24-bit). The
headers it writes check out: e.g. `0x91` = 24-bit, 96 kHz, 2 channels.
It **cannot** write 3, 4, 5 or 7 channels, or 20-bit. Those fixtures need a small Python
packer following §2, which is checked against FFmpeg's decoder on the formats both can
produce.

## 9. Not in scope

Bit-perfect 24/96 over S/PDIF as linear PCM (`docs/iec61937.md`, `docs/fabric_audio.md`
"Hi-res LPCM") is a separate feature. The 16-bit `AUDIO_L/R` cap stays.

⏳ **To check in this feature:** AC-3/DTS passthrough with `hdmi_audio_96k=1`. IEC 61937
needs a 48 kHz link, and `docs/hdmi_bitstream.md` says reg 0x15 follows the ini in bitstream
mode too.

## 10. Decisions (the maintainer's, 2026-10-05)

1. **Dedicated RTL** for the downmix and decimator (option A, §4).
2. **The `sys_top` tap** for the link rate (§6 (a)): 96 kHz LPCM plays natively on a 96 kHz
   link and is decimated on a 48 kHz one.
3. **FFmpeg's channel order** (§2) for 3–8 channels.

## 11. What was built (2026-10-05)

| piece | where |
|---|---|
| header capture: channels, 96 kHz, `bad` (reserved rate / quant 3) | `dvd/ps_demux.sv` `aud_lpcm_nch_m1/fs96/bad` |
| the new path: mono groups, channel counter, downmix MAC | `dvd/lpcm_unpack.sv` (`npath`) |
| 71-tap half-band, 2:1, 128-pair ring in one M10K | `dvd/lpcm_hb.sv` |
| 96 kHz NCO (`nco_fs` 3), `dec`, reserved-header drain, `lpcm_unsup` | `dvd/dvd_audio_decode.sv` |
| the link rate: `sys_top` `.AUDIO_96K(audio_96k)` → 2-flop sync → `.link96` | `sys/sys_top.v`, `dvd/emu.sv` |
| popup: `aud_lpcm_unsup` in `aud_unsupported` | `dvd/emu.sv` |
| golden model, fixtures, FFmpeg cross-check | `tools/lpcm_model.py` |
| AC-3 1+1: dual BSI block and per-block `dynrng2e` | `dvd/dts/ac3.uasm`, `tools/ac3_model.py` |
| an open-content 1+1 stream, and its a52dec cross-check | `tools/ac3_dualmono.py` (run by `tools/gen_test_stream.sh` → `tools/streams/dualmono_440_1k_48k_192k.ac3`, generated, not committed) |
| LPCM test VOBs (FFmpeg video, our group-aligned LPCM) and the capture scorer | `tools/lpcm_vob.py` |

The original stereo path (stereo at the output rate, CD-DA/WAV, the engine's
serialised DTS/MP2 pairs) is unchanged code: the path is chosen at a sample-time
boundary, and `dvd_audio_decode` forces `nch_m1 = 1`, `dec = 0` for CD-DA and the
engine.

### Gates

- **`bench/dvd/run_lpcm_full.sh --red`.** It runs:
  - `tools/lpcm_model.py --selftest`: the tables are re-derived, and unpack is checked
    against FFmpeg's `pcm_dvd` decoder on the 13 formats its encoder writes;
  - `lpcm_full_tb`: 19 arms (A–T) pair for pair with `!==`. They cover mono at 16/20/24
    bit, 3–8 channels, 96 kHz through the half-band, 96 kHz on a 96 kHz link, a stereo
    control, and three pressure arms;
  - `lpcm_dec_tb` S1–S5: the NCO's rate measured, decimation, the reserved-header
    drain and popup, a 5.1 downmix, and CD-DA immune to a stale header;
  - `ps_demux_lpcm_tb` H1–H6;
  - `lpcm_unpack_tb`, unchanged;
  - `tools/check_lpcm_wiring.py --red`: 10 mutations of `emu.sv` and `sys_top`.

  19 RTL mutations each fail exactly their own arms.
- **AC-3:** `tools/test_ac3_model.py`, `tools/test_ac3_isa.py` (arms `nodual`, `nodyn2`
  tied to the `dualmono` feature), `bench/dvd/run_ac3.sh --red`, `run_ac3_seq.sh --red`,
  `run_ac3_ab.sh`, and `bench/ac3/run_imdct_xcheck.sh` (`imdct_512` = `imdct_model` on
  the 1+1 stream). `tools/ac3_dualmono.py --check`: FFmpeg (CRC-checked) and a52dec
  decode the stream as 1+1 with 440 Hz left and 1 kHz right, and the core's arithmetic
  (`ac3_model` → `imdct_model`) correlates **1.00000** with a52dec per channel and
  0.000 across.
- Unchanged and green: `run_mp2`, `run_dts_dec`, `run_cb_copy`, `run_aud_retime`,
  `run_aud_switch`, `run_css`, `run_auddrain`, `run_seek_rf_pts`, `run_subpic`,
  `run_menudrain`, `run_mgl`, `run_disp_sched`, `run_dts`, `run_dts_seq`, `run_mp2_eng`,
  `run_wav`, `run_vcd`, `run_stc_freerun` (its first run failed once: "dvd/lpcm_hb.sv: No
  such file" in a build that writes to a shared `/tmp/aud_sim`; the same build by hand,
  and a full rerun, are green), and all 13 `bench/ac3` suites. `run_front_cosim` now skips the 1+1 stream, which
  `ac3_front` refuses by design.
- `run_reader_regress.sh` is not applicable: `dvd_iso_reader` is untouched. Its runner
  only gained `lpcm_hb.sv` in the audio file list.

### Findings, so they are not rediscovered

- **Authoring packs LPCM PES payloads in whole groups; FFmpeg's muxer does not.**
  - Measured over the whole of both library LPCM discs: *Three Tenors* (16-bit) carries
    2,008-byte payloads (502 × 4), and all 1,094,212 PES of *Roger Waters* (20-bit) are
    2,010 bytes (201 × 10).
  - So the realign path, which starts at a payload's first byte (`ps_demux`), stays as
    it is.
  - FFmpeg's `dvd` muxer splits groups across PES (24-bit stereo: 2,010 % 12 = 6) and
    always writes first-access-unit = 4. An FFmpeg-muxed multichannel or 24-bit VOB
    therefore mis-pairs channels after a seek. That is a property of the file. It is
    also why the bench fixtures come from `tools/lpcm_model.py`'s own packer.
- **An 18-bit signed constant cannot hold unity in Q1.17.** `18'sd131072` is −131072,
  and Icarus says nothing. The first bench run negated every mono and 2.1 sample. The
  gains are 19 bits (Cyclone V's 18 × 19 mode).
- **The half-band must not start an output in the cycle its previous output is being
  written:** `out_room` cannot count that write yet. Pressure arm R lost 7 pairs to a
  full FIFO until `!out_v` joined the start condition (mutation H4).
- **`dec` comes from `link96`, not from the latched NCO rate.** The NCO's rate
  latches a cycle after the codec does, so the first byte of a 96 kHz stream would latch
  the decimating path for one sample-time.
- **AC-3 1+1 also carries `dynrng2e`/`dynrng2` in every audio block**, not only the
  second BSI block. FFmpeg's "new coupling strategy must be present in block 0" on
  the first rewritten stream found it. As in liba52 (`parse.c`), the last one sent
  applies to both channels.
- **`run_ac3_seq.sh`'s X8 had been surviving on `main`, unnoticed.** The local gate set
  gained a second zero-SNR window (*Anastasia*, 2026-09-15) that sorts ahead of the one
  X8 was written against (*DARK PASSENGERS*) and carries no block X8 can see. Measured:
  X8 survives on `main` and on this branch with *Anastasia*, and is caught with *DARK
  PASSENGERS*. `pick()` now tries that window first.
- **`ac3_ab_tb`'s `+refuse` required the engine to refuse too.** A 1+1 stream now runs
  with `+halt`: `ac3_front` must halt, and the engine must decode past it.
- **The only 1+1 in the library is silence.** Both frames of the *Casino Royale* window
  decode to zero in our arithmetic and in a52dec. The synthetic stream is the one that
  proves the decode.

### ⏳ Found, not fixed: bitstream passthrough on a 96 kHz HDMI link

`main/integration/apply_integration.py` writes ADV7513 reg `0x15` from `hdmi_audio_96k` in
bitstream mode too, and leaves N at 12288 (stock Main's 96 kHz value), while
`dvd/hdmi_bs_i2s.sv` always sends 48 kHz frames. With `hdmi_audio_96k=1` the HDMI channel
status therefore claims 96 kHz for an IEC 61937 burst that must be 48 kHz. This was read
from the code, not heard. The rig has no receiver to confirm it on, and it predates this
branch. **The fix is in the Main:** in bitstream mode write `0x15 = 0x20` and N = 6144
(`0x01–0x03 = 00 18 00`), and restore the ini's values when returning to PCM. Optical
S/PDIF is unaffected. The manual (`audio/passthrough.md`) tells users to leave the key at
0 for HDMI passthrough meanwhile.

### Known limitations

- Output is 16-bit stereo: truncation, not dither, for 20/24-bit; multichannel downmixed.
- The channel order is an assumption (§2): FFmpeg's. A disc authored to VLC's reading
  would put its surrounds into the centre's gain and lose one surround.
- 1+1's two DRC words: last one wins (liba52), not one per channel.
- A track whose format changes mid-stream without an audio reset switches path only at a
  sample-time boundary. In practice a track change resets the decoder.
- 96 kHz native output needs `hdmi_audio_96k=1` and holds 42 ms in the 4,096-pair FIFO
  (85 ms at 48 kHz).

### Fit (2026-10-06, `releases/DVD_lpcmfull_20261006_0207.rbf`, SEED 7)

| | before (`dev-stilloff`) | this build | delta | §4 estimate |
|---|---|---|---|---|
| ALM | 38,699 | 39,104 (93 %) | **+405** | 350–550 |
| M10K | 519 | 520 | +1 | 1 |
| DSP | 87 | 91 | +4 | 2 (four 18 × 19 multipliers, no sharing) |
| `clk_dec` slow 100 °C / −40 °C | 93.55 / 89.74 | 91.80 / 91.61 MHz | | gate 86.0 |
| `clk_mem` slow 100 °C / −40 °C | 95.22 / 96.38 | **90.14 / 90.75 MHz** | | runs at 90.0 |

⚠ `clk_mem` passes with 0.14 MHz to spare, down from 5. It is placement-sensitive
(PR #157), and this netlist moved it. It passes the gate, so the build is flashable,
but if the next change touches it, re-sweep the seed before trusting it.

## 12. Hardware (2026-10-06, rig .236, `DVD_lpcmfull_20261006_0207.rbf`)

Method: `tools/lpcm_vob.py` makes the VOBs (FFmpeg's video, our group-aligned LPCM, a
speaker walk), and `tools/lpcm_hil.py` plays each one while capturing the HDMI audio. It
scores where every channel lands, its level against the downmix gain (±1.5 dB), and the
30 kHz alias. **Control arm first:** `main`'s build (`DVD_stilloff_20261005_2238.rbf`).

| | control (`main`) | this build |
|---|---|---|
| 96 kHz stereo 24-bit | FAIL (no tones) | ✅ L/R exact; 30 kHz image ≤ −125 dB |
| 48 kHz 5.1 20-bit | FAIL (channels mis-paired) | ✅ every channel where the downmix puts it, LFE silent |
| 48 kHz 5.0 24-bit | | ✅ |
| 48 kHz mono 20-bit | FAIL | ✅ both sides −12.3 dB |
| 96 kHz 4.0 16-bit | | ✅; 30 kHz image −104 dB |
| 48 kHz 7.1 16-bit | | ✅ (first run's last channel read low: the file was shorter than its walk, now fixed in `lpcm_vob.py`) |
| AC-3 1+1 | FAIL (silent) | ✅ 440 Hz left, 1 kHz right, ≥ 87 dB separation |

Levels land 0.3 dB under the model's on every arm, the same offset on every channel: the
capture path's.

Unchanged paths, this build:
- `tools/audio_check.py`: *Three Tenors* (16-bit LPCM), *Roger Waters* (20-bit LPCM) and
  *Almost Famous* (AC-3 + DTS in `Decode PCM`): every track audible. DTS audible means the
  codebook init in `lpcm_unpack`'s FIFO survived the write-port change. The map report
  shows both halves still carry `cb_host_lpcm.mem`.
- `clk_mem` smoke (PR #157's recipe: THE_OFFICE, Disc Menus Off, Progressive, 120 s
  telemetry):

  | | lates | drops | longest picture | over a frame period |
  |---|---|---|---|---|
  | control | 1 | 0 | 20.8 ms | 0 of 2,978 |
  | this build | 1 | 0 | 20.9 ms | 0 of 2,971 |

  The 0.14 MHz margin costs nothing measurable here.

**96 kHz link** (`hdmi_audio_96k=1`, set by the maintainer; stock Main):
- Both 96 kHz VOBs play at correct pitch with every channel in place.
- The core's measured audio rate is **96,000.2 Hz** while the 96 kHz track plays, with 0
  drain-gate closures and 0 lates. So the NCO really runs native: the `sys_top` tap
  reached the core, and on stock Main.
- A 48 kHz 5.1 file and the AC-3 1+1 file also play correctly on the 96 kHz link.

⏳ **Not checked on hardware:** HDMI bitstream passthrough on a 96 kHz link (§11, no
receiver on the rig).

### Next

Open the PR when asked. Then by ear on the rig: synthetic VOBs from our packer (mono, 5.1/24,
7.1/16, 96/24 stereo, 96/16 4.0), with the current `main` build as the control.
`hdmi_audio_96k=1` needs an ini change and a reboot on the shared rig: ask first.
