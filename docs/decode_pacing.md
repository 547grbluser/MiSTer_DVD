# Decode pacing: content coding × output mode

**Status:** investigation ✅ MERGED (PR #137, 2026-09-28). The fix is being built one stage
per branch (§7). **F1** (no OSD display reads) ✅ MERGED (PR #138), bit-exact in sim and
HW-measured: Progressive lates ROGER 9.2→3.8/s, Office 8.2→1.0, Thayer 7.3→0.1. Next: F2. Measured on the rig with v0.8.0
(`DVD_20260928.rbf`), the v0.7.0 control (`DVD_20260924.rbf`) and an instrument build
(`dev-pacing`, which adds `dvd/dec_duty.sv`).

**The answer in one paragraph.** The lates on Thayer's Quest are neither a compute
ceiling nor a scheduler bug. They are **DDR3 contention from the Progressive raster's
display reads**. On Progressive the display re-reads the whole decoded frame from DDR3
at every refresh: twice the lines per second of Interlaced, and twice what Film 24p's
content-rate raster reads. Those reads have strict top priority on the port the decoder
also uses, so every picture's motion-comp waits longer for its reference pixels. The same
pictures cost **30–50 % more decode time on Progressive than on Interlaced**, nearly all
of it in reference-fetch wait. Even so, the decoder's parse front sits parked about half
the time on every raster. The lates are the pictures that no longer fit their
one-frame budget, because a one-deep picture handoff cannot bank the idle time of the
light pictures. It affects **every** busy interlaced or 25p source on Progressive:
frame-coded or field-coded, NTSC or PAL, title or menu domain, any Deinterlace setting.
It is not specific to field pictures, to Thayer, or to v0.8.0.

## 1. The question

The v0.8.0 smoke test showed Thayer's Quest (field-coded laserdisc FMV) on
**Video Output = Progressive** at ~7.4 lates/s, ~7.4 drops/s and 26.3 fps content (29.97
expected), with an audio rate reading of "~67 kHz". v0.7.0 was identical. The same disc on
**Interlaced** showed 0 lates, and MiB (film) and PAL BBB showed 0 on Progressive. A decode
ceiling would not move with the raster, so lates that depend on the output mode do not fit
CLAUDE.md's Known gap ("compute-bound stutter … the decoder ceiling, not pacing").

## 2. The instrument had to be fixed first

### 2a. The "67 kHz" was the instrument, not the audio

`tools/mister.py telem --watch` unwrapped every 16-bit counter modulo 65536. But the
counters live in **three reset domains**:

| Counters | Reset by |
|---|---|
| `refreshes` | `reset_n` only (never during play) |
| `pickups`, `lates`, `drops` | `sync_rst`: mount, `~keep_vbuf` jump, watchdog |
| `aud_play`, `aud_gate`, word 5 | `aud_rst_n = reset_n & ~aud_flush & ~aud_resync` (`flush_ctl.sv`): every seek, `~keep_vbuf` jump, mode switch (a live `osd "Video Output=…"` included), audio-track switch and non-seamless re-anchor |

A counter zeroed mid-window unwraps as a forward jump of up to 65535 counts. For
`aud_play` (÷16) that is up to ~1.05 M phantom samples. `aud_play` physically *cannot*
exceed 48 kHz: it counts the 48 kHz NCO ÷ 16, gated by `drain_en & ~pause`
(`dvd_audio_decode.sv`), and gating only removes ticks. The earlier "65,524 drain-gate
closures" in `docs/hil_harness.md` is the same signature (65536 − 12).

**Fix:**
- `telem_count()` treats a per-row delta above `2 × TELEM_MAX_RATE × dt + 2` as a
  reset.
- It excludes that interval from the count and from the span, and reports resets per
  counter.
- Gate: `tools/test_telem_unwrap.py`. Its RED arm shows the old unwrap reporting the
  phantom rate.

After the fix, every non-Thayer cell reads **48 000 Hz ± 1 with zero resets**.

### 2b. A separate, real audio observation: Thayer's title VTSes

Thayer's field-coded title VTS_08 shows **1–4 audio resets per 20 s window**, in both
modes and on v0.7.0 too. These are non-seamless cell re-anchors (`aud_resync`); the video
counters do not reset because the VBUF is kept. After excluding the reset intervals,
audio still plays only **41–46 kHz worth per wall-second**. So there are real gaps after
each re-anchor, about 5–13 % of the time silent.

This is *not* the pacing defect. **Investigation handed off to
[`docs/nonseamless_audio.md`](nonseamless_audio.md)** (2026-09-29): each restart is a
flush at a non-seamless cell join followed by ~1.5–2 s of silence while the clock walks
up to the new cell's audio. The boot FMV (menu
domain) shows 0–1 resets per window and 48 kHz. Also observed and not investigated:
`av_drift_ms` medians of about +1500 in two Thayer VTS_08 windows. That is inside the
±5825 ms range, so it is not an alias.

### 2c. Other fields that lie if read naively

- **`lates` during a still** (found 2026-09-29): while the VM holds a PGC still
  (`flags.still = 1`), `lates` rises by one per refresh, although the picture on screen
  is exactly the one intended. A boot capture of Thayer's First Play counts ~300 "lates"
  in 5 s this way. Exclude still rows before reading lates as decode lateness.

- **`vid_err`** (the JSON key) is word 5, `{skip[7:0], catch[3:0], rearm[3:0]}`: audio
  discard counters, not a video error. `mister.py` decodes it as `aud_disc`, and only on
  a window without audio resets, since it shares `aud_play`'s reset domain.
- **`disp_lag_ms` / `av_drift_ms`** are `[19:4]` slices: full scale ±5825 ms. The
  smoke test's `av_drift ≈ +5354` and `disp_lag ≈ −3463` sit at or near that edge and
  are **not measurements**. The summary flags |v| > 5000 as alias-suspect.
- **`sched_ps` is `progressive_sequence`,** not `picture_structure`. Telemetry cannot
  see field coding at all (§3).
- The samples-per-refresh "ideal" was hard-coded for 59.94 Hz; it now follows the
  measured raster.

### 2d. What a drop costs (so the rates reconcile)

A field-coded B pair is dropped atomically but acks **once per field**, at cost 1 each
(`vld.v` `drop_pair_arm`/`drop_pic_field`). A frame-coded drop acks once, at cost 2.

- **Field-coded:** lates ≈ drops, and fps = 29.97 − drops/2. Thayer: 29.97 − 7.4/2 =
  26.27, measured 26.3, so the smoke test's fps was real.
- **Frame-coded:** lates ≈ 2 × drops, and fps = 29.97 − drops. ROGER_WATERS: 29.97 −
  4.48 = 25.49, measured 25.52.

The count ratios also identify which late source is firing:
- An Interlaced pair miss is `late_raw + late_ext` = 2 lates, and funds one pair drop = 2
  acks, giving **2 : 2**.
- A field-parity insertion (`par_late_r`) is a single late (odd totals).
- A scheduler catch-up request (`catchup_late`) is 1 late per 2 acks.

Every Interlaced burst in this study was even and 1 : 1, and every frame-coded
Progressive window ran about 2 : 1 (one late per missed refresh, a frame drop acking once
at cost 2). So every late measured here is a real pair or frame miss, not a parity
insertion or a catch-up request.

## 3. Discs, chosen by bitstream

Measured with `tools/video_cadence_census.py` (`--per-window`, and `--field-order
--all-vts`) on the local library. PAL discs come only from its `pal/` subset.

| Class | Disc | Measured |
|---|---|---|
| Soft-telecine film | MEN_IN_BLACK (VTS21) | pf 100 %, rff 49.9 %, rff toggle 33 % |
| Frame-coded 29.97i | ROGER_WATERS_IN_THE_FLESH (VTS02) | pf 0 %, field pictures 0 % |
| Field-coded 29.97i | Thayer's Quest **VTS_08** | field pictures 100 %, 97.7 % TOP-first |
| Frame-coded 29.97i, same source | Thayer's Quest VTS_09 (Auto's pick) | pf 0 %, field pictures 0 % |
| Field-coded, another disc | Angel_And_The_Badman VTS01 | field pictures 100 % |
| Thayer menu domain (boot FMV) | `VIDEO_TS.VOB` | **not re-measured**: field-coded per `docs/status_log.md`. The census walks VTS_01–11, not the VMGM |
| 25p PAL | big-buck-bunny-PAL (VTS05) | pf 100 %, rff 0 |
| 25i PAL | THE_OFFICE_UK_DISC1_PAL (VTS04) | pf 0 % |
| MPEG-1 | a VCD `.bin` (SIF, MP2 audio at 44.1 kHz by design) | — |

**Thayer:**
- With Disc Menus = Off, Auto plays the largest VTS. That is **VTS_09, the only
  frame-coded one**: VTS_01–08 and VTS_10 are 100 % field-coded and VTS_11 is mixed.
- The field-coded cell therefore forces `Title VTS Units = 8`. VTS_08 (478 MB) is used
  because VTS_01, the one in the brief, is only 176 MB (~2.5 min).
- VTS_09 is kept as a same-source, frame-coded control.
- Since `sched_ps` is `progressive_sequence`, a cell's field coding rests on this census
  plus the forced VTS; telemetry cannot confirm it.

## 4. Method

`tools/pacing_matrix.py <image> --label L --opt …` works like this:
1. It launches the disc and waits for the board's own `video_live` with pickups
   advancing, not for a fixed sleep.
2. It walks the cells by **live** `osd` changes, so every cell sees the same scene region.
3. The Video Output cells are **interleaved** (Prog, Ilace, Auto, Prog, Ilace, Auto) so
   that scene content cannot pose as a mode effect.
4. After each change it waits for the settle (the switch itself re-aligns and resets the
   audio counters), re-checks liveness, then takes one reset-aware `telem --watch` window
   of 30 s (20–25 s on short content).
5. Raw rows and summaries go to `.sim/pacing/<build>/<disc>/`. The per-cell table is
   Appendix A.

Rates come from telemetry counters only, never from capture frames: a capture measures
offsets, not rates.

**Auto** resolves as `interlaced_eff = (mode == Interlaced) | (mode == Auto &
analog_want)`. On this rig the ini asks for analog, so **Auto = Interlaced**. On an
HDMI-only setup Auto resolves to **Progressive**, the losing column below. That makes
this a default-settings issue for HDMI users.

## 5. The matrix (v0.8.0; lates/s per window, with mean content fps)

| Disc | Prog (Weave) | Interlaced | Auto (= Ilace here) | Prog + Film Off | Prog + Film On | Prog + Bob | Prog + Blend |
|---|---|---|---|---|---|---|---|
| ROGER (frame-coded 29.97i) | 9.0 / 9.9 (25.3) | 0.2 / 0.1 (29.9) | 0.0 / 1.1 (29.7) | 8.8 (25.6) | 6.0 (24.0)† | 9.7 (25.1) | 9.8 (25.1) |
| Thayer VTS_08 (field-coded) | 7.1 / 6.7 (26.5) | 0.7 / 0.1 (29.7) | 0.4 / 0.4 (29.6) | 7.0 (26.3) | 5.9 (23.9)† | 6.9 (26.5) | 6.2 (26.9) |
| Thayer VTS_09 (frame-coded) | 7.3 / 7.3 (26.3) | 0.0 / 0.0 (30.0) | 0.0 / 0.0 (30.0) | 1.5 (29.2)‡ | 6.0 (24.0)† | 7.2 (26.4) | 7.3 (26.3) |
| Angel (field-coded film) | 6.6 / 8.8 (26.2) | 0.0 / 0.0 (30.0) | 0.0 / 0.0 (29.9) | 9.2 (25.4) | 6.0 (24.0)† | 9.2 (25.4) | 9.2 (25.4) |
| MiB (soft-telecine film) | 0.2 / 2.6 (23.3) | 0.0 / 0.0 (24.0) | 0.1 / 0.0 (24.0) | 3.3 (22.3) | 0.0 (24.0) | 3.5 (22.2) | 2.3 (22.8) |
| BBB PAL (25p) | 5.1 / 0.8 (23.5) | 0.0 / 0.0 (25.0) | 0.0 / 0.0 (25.0) | 4.8 (22.6) | **0.0 (25.0)** | 6.2 (21.9) | 7.0 (21.5) |
| Office PAL (25i) | 8.2 / 8.2 (20.9) | 0.1 / 0.1 (24.9) | 0.0 / 0.0 (25.0) | 8.4 (20.8) | **0.0 (25.0)** | 8.3 (20.9) | 8.3 (20.9) |
| VCD (MPEG-1 SIF) | 0.0 / 0.0 (25.0) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |

† Film 24p On over 29.97 content is structurally late: a 23.976 Hz raster cannot show
29.97 fps. It is not a defect.
‡ Film Off and Film Auto are identical on `pf = 0` content (Auto never engages). The
1.5 is scene variance, meaning a lighter stretch. Scene variance is also why MiB and BBB
swing between windows, and why the smoke test's "0 on MiB / BBB" was a quiet scene.

**v0.7.0 control:**
- ROGER: Prog 8.8 / 9.9, Ilace 0.2 / 0.1.
- Thayer VTS_08: Prog 6.8 / 6.7, Ilace 0.4 / 0.0.
- Identical to v0.8.0: not a regression.

**Menu vs title domain.** The Thayer boot FMV (menu = 1) gives Prog 5.9 / 7.5 and
Interlaced 0.15 / 0.40, the same Progressive/Interlaced split as its title VTSes. The
domain does not change the mechanism. It does carry one extra item, §6c.

**What does not matter:**
- field vs frame coding: Thayer VTS_08 and VTS_09 match, and Angel matches ROGER;
- Deinterlace: Weave, Bob and Blend are equal within scene variance, since none of them
  adds DDR3 reads. Bob really engaged: 60 of 60 rows read `flags.bob = 1` on the
  instrument build's Office Bob cell. The v0.8.0 run's Main predates that flag, so its
  `bob` column reads 0 regardless;
- the release (v0.7.0 = v0.8.0).

**What matters:** the display's frame re-read rate, and how busy the content is.

## 6. Root cause

### 6a. Separating decode cost from scheduling: `dec_duty`

No existing counter could say whether the decoder was busy, starved or waiting. The stage
profiler that once did was removed. The retired profiler's own comment claims it had
"confirmed the high-motion stutter is compute-bound", but that was measured on Matrix,
which `docs/motcomp_throughput.md` later discounted.

`dvd/dec_duty.sv` puts every `clk_dec` cycle into one exclusive VLD class, plus one
independent class:

| Class | Meaning |
|---|---|
| **parked** | `picbuf_busy`. The VLD reached the next picture's header and waits until the display picks up the picture ahead of it. The update marker has passed through the mvec FIFO by then, so motion-comp has consumed every macroblock of the previous picture; the parked time is idle apart from a bounded reconstruction tail |
| **starved** | no bitstream |
| **pipe-stalled** | parse held back by rld / mvec / motcomp |
| **active** | the rest |
| **ref-wait** | independent: `recon_ref_stall`, motion-comp waiting for reference pixels from DDR3 |

Telemetry words 16–20. Gates are in §8.

### 6b. The measurement

**THE_OFFICE_UK (PAL 25i).** The same pictures, three rasters, zero drops in two of
them (so the same picture mix):

| Raster | Display re-reads | Lates/s | Decode busy / picture | Ref-wait / picture | Parked |
|---|---|---|---|---|---|
| Interlaced, 50 fields/s | ½ frame × 50/s | 0.1 | 16.6–18.2 ms | 12.9–14.6 ms | 0.54–0.59 |
| **Progressive, 50 Hz** | **full frame × 50/s** | **8.2–8.4** | **23.3–24.6 ms** | **19.9–21.0 ms** | 0.49–0.51 |
| Progressive + Film 24p On, 25 Hz | full frame × 25/s | 0.0 | 13.1 ms | 9.8 ms | 0.67 |

**ROGER_WATERS, three interleaved rounds:**
- Interlaced: 15.4 ms busy / 12.4 ms ref-wait per picture.
- Progressive: 20.5 ms / 18.3 ms per picture.
- That is **+33 % busy, +48 % ref-wait**. The ref-wait rise (+5.9 ms) covers the whole
  busy rise (+5.1 ms).

**Thayer VTS_09:** +30 % busy, +40 % ref-wait. **Thayer VTS_08:** +28 % busy, +36 %
ref-wait. **MiB:** the same direction, smaller on its lighter windows.

**Caveats.**
- **Picture mix.** Progressive drops about 4.5 B-pictures/s, so the pictures left in its
  denominator lean towards I/P. A GOP estimate puts that effect at a few percent, not
  +48 %. The Office rows carry the headline because Interlaced and Film-On there have
  zero drops and an identical mix.
- **Film-On (13.1 ms) beats Interlaced (17.4 ms) at equal lines per second.** That is
  *consistent with* the Film 24p design rationale in `emu.sv`: the film raster's long
  vertical blank hands motion-comp contiguous DDR3 windows. It was not measured
  separately here.
- **The instrument does not perturb what it measures.** The `dev-pacing` build's lates
  match v0.8.0 cell for cell: ROGER Prog 8.9–9.9 vs 9.0–9.9, Office Prog 8.2 vs 8.2,
  Thayer VTS_09 Prog 7.2–7.4 vs 7.3.

**Conclusion: DDR3 arbitration occupancy.** What decides a picture's cost is how
often the display re-reads the frame on the decoder's port, not the picture's content
alone:
- The per-picture reference-fetch time follows the display's read rate.
- The display's reads have strict top priority (`framestore_request.v`) on the same
  f2sdram port as the reference reads.

This is `docs/hw_budget_and_lessons.md` §2's rule measured on the display path itself:
the scarce resource is arbitration occupancy, not bandwidth.

**Why lates while parked ~50 %?** `tools/pacing_model.py` models it:
- The model: the one-deep handoff (`motcomp_picbuf` parks the VLD until pickup), the PTS
  due test, when each raster runs it, and the drop ledger.
- At **equal** decode cost, it predicts Interlaced no better than Progressive. So the
  asymmetry has to come from the decode cost itself, and it does (above).
- A decoder that averages well inside the budget still misses on the pictures in its
  heavy tail, because idle time after light pictures cannot be banked ahead. Progressive
  pushes 30–50 % more of the tail over the line.

**The measured replacement for "compute-bound":**
- Average decode throughput is about 2× the content rate (parked about 50 %).
- Individual heavy pictures exceed a one-frame budget, and Progressive's display reads
  push many more of them over it.
- A per-picture maximum is not yet measured (§7, instrument).

### 6c. Interlaced lates in Thayer's boot FMV — ✅ resolved 2026-09-29, see the end of this section

**Answer (per-picture instrument, §7):** after F2 the FMV reads **0 lates on Interlaced**
for two minutes from boot, in two clean launches; **no picture** took longer than one
frame period (0 of ~3,300; the longest 21.1 ms of a 33.4 ms budget). The only lates left
are **counted during the disc's First Play still** (`flags.still = 1`, pickups flat, one
per refresh for ~5 s), which is bookkeeping, not a visible miss. The slow-picture
hypothesis below is refuted for the post-F2 build. The pre-F2 bursts were the same
display-read contention as §6b. The original analysis follows, unchanged.

On a clean launch straight into Interlaced (Disc Menus = On), the boot FMV reads
**3.4 lates/s, then 0.9 lates/s** in the next minute. v0.8.0 is identical (3.39 / 0.89):
the FMV is deterministic from boot, which makes this a free reproduction. The lates come
in bursts of 2–8 per 0.5 s, in even counts, with lates = drops (§2d: real pair misses).

What was ruled out:
- **Starvation or a reader/link stall.** `dec_starve` is flat at 0.35 % in burst and
  calm rows alike, and `vbuf_fill` does not dip.
- **A heavier scene.** In the sustained 40–45 s burst the per-decoded-picture busy time
  is **the same 16.4 ms** as in calm rows, and the VLD is parked 62–68 %.
- **A drop/late feedback loop.** With **Frame Drop = Off** on the same boot timeline the
  display delivers the same 28.4 fps (Off) vs 28.3 (On). It simply counts 9.76 lates/s
  instead of dropping. The drops respond to the misses and do not cause them.

A 0.5 s row cannot resolve individual pictures. A late and a parked VLD cannot coincide
at the same instant (a late needs `~output_frame_valid`, i.e. a picture buffer not
parked), so the row averages cannot say which picture missed. **Leading hypothesis:**
isolated slow pictures that the one-deep handoff cannot absorb, costing a whole pair each
on Interlaced; the clip-start I-pictures of the FMV's short cells are the prime suspect.
That is unproven, and it needs the per-picture instrument in §7.

## 7. Fix plan (proposed; one behavioural change per build)

Ranked by cost against payoff. Each stage is measured on the rig with
`tools/pacing_matrix.py`, on ROGER, Office and Thayer VTS_08, Progressive against
Interlaced, before the next stage starts.

**F1. Stop the dead OSD reads.** ✅ **Built and HW-measured 2026-09-29**
(PR #138, `releases/DVD_osdread_20260929_0343.rbf`, clk_dec 86.0 MHz at
both slow corners). `OSD_READS = 0`
in `resample.v`, one parameter for both `resample_addrgen` and `resample_dta`.
`bench/dvd/run_osd_read.sh` proves the pixels bit-identical against `OSD_READS = 1`
in four geometries (progressive, interlaced, 720-wide, bursty stall), and 8 → 6 read
words per macroblock-line. Three mutations are each caught.
`tools/check_osd_read_wiring.py` pins the seams and the premise
(`dot_osd_enable = 1'b0`).

**Hardware result.** Same script (`tools/pacing_matrix.py`, interleaved, `--no-variants`)
and same discs. "Before" is the `dev-pacing` build, which has the same `dec_duty`
instrument. A **control re-run of that build right after the F1 run** reproduced it: ROGER
Prog 9.22 lates/s (identical), Thayer VTS_09 7.30 (vs 7.32).

| Disc | Mode | Lates/s (before → F1) | fps | Decode ms/picture | Ref-wait ms/picture | Parked |
|---|---|---|---|---|---|---|
| ROGER | Prog | 9.22 → **3.76** | 25.37 → 28.09 | 20.9 → 17.2 | 18.4 → 14.7 | 0.47 → 0.52 |
| ROGER | Ilace | 0.50 → **0.00** | 29.72 → 29.97 | 15.4 → 14.1 | 12.4 → 11.1 | 0.54 → 0.58 |
| Office PAL | Prog | 8.21 → **1.01** | 20.92 → 24.49 | 23.4 → 19.3 | 20.0 → 16.1 | 0.51 → 0.52 |
| Office PAL | Ilace | 0.10 → **0.00** | 24.94 → 25.01 | 17.4 → 15.8 | 13.8 → 12.2 | 0.56 → 0.60 |
| Thayer VTS_09 | Prog | 7.32 → **0.08** | 26.32 → 29.94 | 17.7 → 15.1 | 13.5 → 11.3 | 0.53 → 0.54 |
| Thayer VTS_09 | Ilace | 0.00 → **0.00** | 29.96 → 29.98 | 13.4 → 12.5 | 9.2 → 8.2 | 0.60 → 0.62 |
| MiB | Prog | 1.44 → **0.00** | 23.27 → 24.00 | 16.1 → 13.0 | 13.3 → 10.2 | 0.62 → 0.69 |
| MiB | Ilace | 0.00 → **0.00** | 23.98 → 23.98 | 13.1 → 11.9 | 10.0 → 8.9 | 0.68 → 0.71 |

- **Mechanism.** Removing a quarter of the display reads cut per-picture reference-fetch
  wait by 2.2–3.9 ms on every disc, and on Interlaced too, which reads less but still
  read OSD words. That is the same mechanism §6b measured, run in reverse.
- **What is left.** Only ROGER, the busiest disc measured, still lates meaningfully on
  Progressive: 3.8/s, about 1.9 dropped frames/s, down from about 4.6. Its windows range
  0.85–5.95 by scene. Office is at 1.0/s (about 0.5 frames/s); Thayer VTS_09 and MiB
  are at about 0.
- **Next.** F2 (chroma-row reuse, a further 6 → 3 words) targets that residue. It took
  it to 0 (below).

- **What:** `resample_addrgen` issues 8 words per macroblock per line: OSD × 2, Y × 2,
  and U, V × 2 rows each. The upstream OSD layer is tied off in this fork
  (`dot_osd_enable = 1'b0`, `mpeg2video.v`), so the OSD words fetch data nothing uses:
  **25 % of every display read**, about 21 MB/s on Progressive.
- **Change:** skip `STATE_WR_OSD_MSB/LSB` and feed `resample_dta` the constant words it
  expects on its OSD FIFO.
- **Quality cost:** none. The OSD pixel is transparent today.
- **Gates:**
  - `resample_chain_tb` scores the pixel output **bit-exact** against the current RTL,
    with `!==`;
  - a new display-request counter asserts 6 words per macroblock-line;
  - a mutation that restores the OSD requests must fail exactly the counter arm;
  - `tools/check_osd_read_wiring.py` pins the `resample_dta` OSD feed.

**F2. Reuse chroma rows.** ✅ **Built, bit-exact in sim and HW-measured 2026-09-29**
(✅ MERGED PR #139, `releases/DVD_chromareuse_20260929_1223.rbf`).

**Hardware result: 0 lates on Progressive on every disc measured.** Same script
(`tools/pacing_matrix.py`, interleaved, `--no-variants`, `Disc Menus=Off`), same discs and
rounds as F1. The **control arm** re-ran the F1 build on ROGER in the same session and
reproduced it: Progressive 3.58 lates/s (F1's record: 3.76), 17.0 / 14.5 ms (17.2 / 14.7).
The other discs' "before" is F1's own table.

| Disc | Mode | Lates/s (F1 → F2) | fps | Decode ms/picture | Ref-wait ms/picture | Parked |
|---|---|---|---|---|---|---|
| ROGER | Prog | 3.58 → **0.00** (all 3 windows) | 28.18 → 29.97 | 17.0 → 14.7 | 14.5 → 12.3 | 0.52 → 0.56 |
| ROGER | Ilace | 0.00 → **0.00** | 29.98 → 29.96 | 14.1 → 13.4 | 11.1 → 10.4 | 0.58 → 0.60 |
| Office PAL | Prog | 1.01 → **0.00** | 24.49 → 24.99 | 19.3 → 16.7 | 16.1 → 13.5 | 0.52 → 0.58 |
| Office PAL | Ilace | 0.00 → **0.00** | 25.01 → 25.01 | 15.8 → 15.1 | 12.2 → 11.4 | 0.60 → 0.62 |
| Thayer VTS_09 | Prog | 0.08 → **0.00** | 29.94 → 29.97 | 15.1 → 13.2 | 11.3 → 9.3 | 0.54 → 0.60 |
| Thayer VTS_09 | Ilace | 0.00 → **0.00** | 29.98 → 29.94 | 12.5 → 12.0 | 8.2 → 7.6 | 0.62 → 0.64 |
| MiB | Prog | 0.00 → **0.00** | 24.00 → 24.00 | 13.1 → 11.4 | 10.2 → 8.5 | 0.68 → 0.73 |
| MiB | Ilace | 0.00 → **0.00** | 23.98 → 23.97 | 11.9 → 11.6 | 8.9 → 8.5 | 0.71 → 0.72 |

- **Mechanism, again in reverse:** per-picture ref-wait fell 1.7–2.6 ms on Progressive and
  0.4–0.8 ms on Interlaced (fewer words there too). Progressive now costs ROGER 14.7 ms
  of decode per picture, about what **Interlaced** cost before F1 (15.4).
- Across F1 + F2, ROGER Progressive went 9.2 → 0 lates/s and 20.9 → 14.7 ms per picture.
- The board shows a correct picture (ROGER, Progressive, screenshot checked). The sim
  proves bit-identity; the shot only rules out a gross wiring fault the bench cannot see.
- **The rest of the §3 census set, measured afterwards on the same F2 build** (same
  script, 2 interleaved rounds; "before" is the pre-F1 `dev-pacing` run where it exists,
  else v0.8.0 from §5):

  | Disc | Prog lates/s (before → F2) | Ilace | F2 decode / ref-wait ms per picture (Prog) |
  |---|---|---|---|
  | Thayer VTS_08 (field-coded) | 6.77 → **0.00** | 0.15 → 0.00 | 18.8 / 15.5 → 13.8 / 10.2 |
  | Angel (field-coded film) | 6.6 / 8.8 (v0.8.0) → **0.00** | 0 → 0 | 11.8 / 9.5 |
  | BBB PAL (25p) | 5.1 / 0.8 (v0.8.0) → **0.00** | 0 → 0 | 13.7 / 9.7 |
  | VCD (MPEG-1 SIF) | 0 → **0.00** | 0 → 0 | 3.7 / 2.8 |
  | Thayer boot FMV (menu domain) | 6.70 → **0.05** (one 0.10 window) | 0.27 → 0.00 | 13.9 / 10.5 |
  | ROGER, Film Off / Bob / Blend | 8.8 / 9.7 / 9.8 (v0.8.0) → **0.00 / 0.00 / 0.00** | — | — |
  | ROGER, Film 24p **On** | 6.0 → 5.99 | — | structural (†): a 23.976 Hz raster cannot show 29.97 fps |

  **Every Progressive cell of the census reads 0**, frame- and field-coded, NTSC and PAL,
  title and menu domain, Weave, Bob and Blend. Thayer VTS_08's low audio rate (40–46 kHz
  with 2–5 counter resets per window) is §2b's pre-existing cell re-anchor behaviour,
  identical before F1.
- **What was wrong:** bilinear chroma upsampling (`resample_addrgen.v`, "see
  bilinear.txt") fetched two chroma rows for each of U and V on **every** macroblock-line
  (4 of the 6 words left after F1), although each chroma row serves several lines.
- **Change:** `resample_dta` keeps the last two rows of each plane in a 256 × 64 RAM
  ({plane, slot, column}, 64 columns: DVD maxes out at 45 macroblocks). The address
  generator decides once per line, for the upper and the lower row: reuse a slot, or fetch
  into the slot this line does not need. It sends that decision to `resample_dta` with
  every position code (the resample fifo grows 3 → 8 bits: `{lcp, sl, fl, su, fu, pos}`),
  so the two halves cannot disagree about which words exist, however far ahead the address
  generator runs. `CHROMA_REUSE` in `resample.v` is one parameter for both modules; `0`
  rebuilds the F1 structure exactly (the bench baseline).
- **The slot key** is the row `memory_address` actually fetches, computed with its own
  arithmetic: `delta_y + ((mv + sign) >>> 1) >>> 1`, before the clip to the picture
  height. The address of a word is a function of (frame, component, column, key,
  `mb_width`, sizes), so an equal key under an equal signature is an equal word. Keys that
  clip to the same row are only a missed reuse.
- **Invalidation:** at every `STATE_NEXT_IMG`, and on any change of the signature {frame,
  `hcrop_en`, `mb_width`, `horizontal_size`, `vertical_size`}. A change is sticky until
  the next line start, and the rest of that line fetches both rows (the dta's column
  counter restarts at each line's `COL_0`, so a mid-line crop change would otherwise
  shift columns). A line wider than 64 macroblocks fetches everything, as before.
- **Timing.** The first build missed `clk_dec` at −40 °C (82.1 MHz against 86.0). All 60
  worst paths ran `mb_height` → the clamp → the key adders → the tag compare → the tag
  write in the one `FIRST_RQ` cycle. The key, `c_same` and `cr_ok` are now registered
  every cycle. `disp_y` holds for the whole line and `STATE_WAIT` always precedes
  `FIRST_RQ`, **except** straight after `STATE_NEXT_IMG`. That line, and any line after a
  signature change, *skips*: it fetches both rows and files neither, so a stale key is
  never stored. Cost: two extra row fetches per scan.
- **Measured in sim (words per macroblock-line, exact per scan; the bench's 256-line
  geometry, so the two skip fetches weigh double what they do at 480):**

  | Scan | F1 | F2 |
  |---|---|---|
  | Progressive raster, progressive or interlaced content (frame scan) | 6 | **3.016** (≈3.008 at 480 lines) |
  | Interlaced raster, interlaced content (field scan) | 6 | **4.016** (129 rows per 128 lines) |
  | Interlaced raster, progressive frame (field scan) | 6 | **4.016–4.031** (bottom / top field) |

  At 720×480 and 60 Hz: Progressive 62 → **31 MB/s** (83 before F1), below the pre-F1
  Interlaced 41 MB/s where lates were about 0. Interlaced 31 → 21 MB/s.
- **Quality cost:** none. The pixels are bit-identical.
- ✅ **Finding, preserved deliberately in F2 and FIXED in its follow-up (below):** the
  upstream "lower" chroma row was not the one `bilinear.txt` describes. `memory_address`
  halves `mv_y` for chroma a second time, so `mv ±2` (progressive upsampling) landed on
  rows **+0 / −1**: odd lines got no vertical chroma interpolation at all. `mv ±4`
  (interlaced upsampling) landed on **±1**, the *opposite field's* chroma row, where 4:2:0
  interlaced needs ±2. F2 kept it bit for bit, so F2 could be proven by identity.
  - **Pre-existing:** `mem_addr.v` and the upstream `rtl/mpeg2/resample_addrgen.v` are
    unchanged since the upstream import (`30c8a75`). Upstream's own clamps (`plus_2` on the
    last chroma row, `plus_4` on the last two) show that ±1 / ±2 rows was the intent.
  - **Size, measured 2026-09-29.** Six decoded frames each, through a model of
    `resample_bilinear`'s arithmetic, upstream rows against the intended rows. ROGER
    (interlaced path): mean |Δchroma| 0.49, 99th percentile 3, max 20 of 255, 0.7 % of
    pixels off by ≥ 4. MiB (progressive path): 0.14 / 1 / 10, 0.2 %. The worst 64×64
    blocks look the same side by side, enlarged 4×. The difference is faint horizontal
    streaks at colour edges; on interlaced content it grows with motion (25 % of the
    chroma is from the other field).
- **Gates:** `bench/dvd/run_chroma_reuse.sh`:
  - [1] Bit-exact across 14 arms, CHROMA_REUSE 0 vs 1, with memory words that hash
    their own address. Two checksums: displayed pixels, and every pixel `resample` emits
    per scan (independent of raster timing). The arms: progressive; **weave** (interlaced
    content on the progressive raster, the case F2 targets; new `+weave` in
    `resample_chain_tb`); fields with both upsamplings and both field orders; `vsz=300`
    progressive and field (the clip against the `mb_height` clamps); 720 wide; Crop; SIF
    2× repeat; bursty stalls; 30→60 pacing; a mid-line crop toggle at a *logical* scan
    point (`+croptog`).
  - [2] Words per scan, exactly as the key model derives them by hand: every distinct
    row once, plus the two skip fetches (the runner header lists each).
  - [3] `tools/check_chroma_reuse_wiring.py`: one knob; `OSD_READS = 0` beside it; the
    fifo width; the key arithmetic pinned against `mem_addr.v`; the flag layout.
  - Mutations M1–M5, each caught by its own arm. M2 (no geometry invalidation) fails
    **only** the croptog arm, which shows that arm reaches it. The `NEXT_IMG`
    invalidation is not gated and cannot be: a scan opens at the top rows while the slots
    hold the previous scan's bottom rows.
- **The other display benches:** `run_osd_read`, `run_field_blend`, `run_pause_still`,
  `run_field_phase` and `run_field_parity` pass on F2. `run_prefetch_chain` (bursty memory
  stalls) was compared against the F1 structure from a `main` worktree:
  - 62 % average bandwidth (12,000-cycle stalls): the deep buffer is **clean on F2 (0
    black frames) where F1 shows 9**, at every one of 5 stall phases. F2's buffer holds
    about twice the lines.
  - 30 % (45,000-cycle stalls, about 26 lines, longer than either buffer): F2 scores
    **more** "BLACK" frames (13–14 against 7–12, at every phase). That is the metric: a
    frame counts as BLACK for a gap *inside* the picture band, and one that shows no more
    than a short unbroken band is not counted. Over the same 19 frames F2 shows **50 %
    more video lines** (4,319 against 2,879), completes 9 scans against 6 and spends 28 %
    fewer cycles in underflow. Both are unwatchable in that regime; it is not a
    regression.
- **Cost** (against the F1 build, same seed 9): **+285 ALMs** (38,963, 93 %), +135
  registers, **+2 RAM blocks** (the 256 × 64 cache; the resample fifo's 3 → 8 bits fit
  its existing block). `clk_dec` **90.6 MHz @100 °C, 91.5 MHz @−40 °C** (F1: 92.3 / 90.5),
  `releases/DVD_chromareuse_20260929_1223.rbf`.

**F2 follow-up: the right chroma rows.** 🔧 **Built on `feature/chroma-rows`, sim-proven
2026-09-29; HW-measured: ⚠ one repeatable late on ROGER Progressive, decision open.** This
fixes the upstream finding above that F2 preserved.
- **What changes in the picture.** Every line now interpolates its chroma between the two
  rows `resample_bilinear`'s 0.75 / 0.25 weights are written for:
  - Progressive upsampling: the nearest row and the next one (+1 on odd lines, −1 on
    even). Before the fix, odd lines had no vertical chroma interpolation.
  - Interlaced upsampling: the nearest row of the line's own field and that field's
    neighbour, two frame rows away. Before the fix, 25 % of each pixel's chroma came from
    the other field.

  The size is the measurement above: a mean of about 0.5 of 255, and faint streaks at
  colour edges that grow with motion on interlaced content. Nothing else moves. Luma, the
  weights and the raster are untouched.
- **Change** (`dvd/resample_addrgen.v`, `DVD-FORK FIX (chroma rows)`):
  - The lower row's `mv_y` doubles (`±2 → ±4`, `±4 → ±8`). `mv_y` is in luma
    half-pixels and `memory_address` halves it once more for chroma, so one chroma row is
    `mv 4`.
  - The two bottom clamps now read `vertical_size / 2` (the rows `memory_address` clips to)
    instead of `mb_height`. The two differ when the height is not a multiple of 16. There,
    the interlaced neighbour below a field's last row would have been clipped onto the
    *other* field's last row. DVD heights (480, 576) are multiples of 16, so for DVD this
    is only consistency.
  - The upstream `rtl/mpeg2/resample_addrgen.v` is not built (the `.qsf` swaps in the `dvd/`
    copy) and keeps the old offsets.
- **Why the cache had to change with it.** Interlaced upsampling now reads only rows of the
  line's own field. A *frame* scan of it (weave: interlaced content on the Progressive
  raster, the case F2 was built for) alternates fields line by line, so consecutive lines
  share **no** row: `4m {2m, 2m−2}`, `4m+1 {2m+1, 2m−1}`, `4m+2 {2m, 2m+2}`, `4m+3 {2m+1,
  2m+3}`. With F2's two slots per plane that misses on every line: 6 words per macroblock-line,
  undoing F2 for ROGER. So the doubled offsets on their own would have been a pacing
  regression.
  - The slots are now **banked by row parity**: two per bank, four per plane. A key's bank
    is `key[0]`, so a compare against all four slots can only hit in the row's own bank.
  - When both of a line's rows fall in one bank, F2's two-slot rule applies inside it.
    When they fall in different banks (progressive upsampling: one even row, one odd),
    each bank decides alone.
  - Flags grow to `{lcp, sl[1:0], fl, su[1:0], fu}`, a slot id being `{bank, slot}`. The
    resample fifo grows 8 → 10 bits and the cache 256 × 64 → 512 × 64.
- **Words per macroblock-line (sim, exact per scan):** Progressive raster **3.016**, for
  progressive and weave alike, the same as F2. **Interlaced raster 4.016 → 3.03**
  (1552 words / 512 macroblock-lines): a field now reads only its own 64 rows, not all 128.
  So the fix is free on Progressive and cheaper on Interlaced.
- **Gates** (`bench/dvd/run_chroma_reuse.sh`):
  - [1] (identity, CHROMA_REUSE 0 vs 1) still proves the banked cache. It cannot see the
    fix, because both builds read the same rows.
  - **[4] is new:** `resample_chain_tb +rowref=1`. Every chroma word names its own row
    (the bench decodes component and row from the address with the `mem_codes.v` layout),
    and every pixel `resample` emits is scored exactly against the spec's rows for its
    line: `((6·code(up) + 2·code(lo) + 7) >>> 3) + 128`. Those rows are computed from
    `disp_y` alone, never from the address generator's own arithmetic. There are 8 arms
    (progressive, weave, both field upsamplings and both field orders, `vsz=300` frame and
    field, and the pause still).
  - On upstream's offsets, the prog arm scores **49,263 bad pixels out of 99,311**: the odd
    lines, exactly as the finding said.
  - Mutations M6 (no banks: pixels identical, the weave words rise, caught by [2] only) and
    M7 (upstream's offsets: caught by [4] only, with [2] unchanged) join M1–M5.
  - `tools/check_chroma_reuse_wiring.py` pins the four offsets, the clamps, the bank bit,
    the 10-bit flag layout and the 512-entry cache.
- **Build** (`releases/DVD_chromarows_20260930_0212.rbf`, seed 9): `clk_dec` **93.5 MHz
  @100 °C, 89.9 MHz @−40 °C** (target 86.0). RAM blocks 510 → 512. At synthesis the
  change costs **+102 LUTs, +52 registers** in `resample`: whole-design ALM estimate 40,543 →
  40,606, against a `quartus_map` of `main`. The *fitted* figure moved 39,050 → 40,786
  (97 %), but that is packing ("Difficulty packing design: High"), not logic.
- **HW, 2026-09-30, ROGER, same `pacing_matrix` script, control arm first:**

  | Build | Prog lates/s | Prog decode / ref ms | Ilace decode / ref ms |
  |---|---|---|---|
  | `main` (F2 + PR #141), control | 0.00 | 14.8 / 12.3 | 13.5 / 10.4 |
  | chroma rows, run 1 | 0.01 | 14.8 / 12.3 | 13.1 / 10.0 |
  | chroma rows, run 2 | 0.01 | 14.9 / 12.4 | 13.1 / 10.0 |

  Per-picture cost is unchanged on Progressive, and Interlaced is 0.4 ms cheaper (4 → 3
  words). **But the one late is repeatable.** It falls at the same place every time,
  about the 1,055th picture after launch. Every launch crosses that point in the first
  Progressive window:
  - the control builds (F2's own run, this session's control, 3 interleaved A/B launches)
    **0 of 5** late there;
  - this build **4 of 5**, one late each.

  ⚠ **It is not established that the heaviest picture is what goes late.** `pic_max`
  near that point is 449–451 on **both** builds, so no picture decoded slower. The late is
  counted in the 0.5 s sample *before* the one where `pic_max` reaches 450. That fits a
  late registered at the deadline while that picture is still decoding (`pic_max` records
  a picture when it completes). It fits slack eroding over the preceding pictures, or a
  display-side cause, just as well. A fix must be judged by the interleaved launch A/B
  (`.sim/chromarows/ab.sh`, 5 + 5 launches), not by window averages, which are identical.

  Unproven explanation: the weave word count is unchanged, but the timing moved. F2
  fetched a new chroma row on alternate lines (2, 4, 2, 4 words per macroblock); the
  correct rows arrive in pairs (2, 2, 4, 4). At a picture already near its deadline that
  costs one frame. Data: `.sim/chromarows/hil/` (gitignored).

F1 and F2 do not violate `hw_budget_and_lessons.md` §2's "do not reach for smaller data
first": they remove *redundant* reads and cost no quality. The port move below stays the
structural fix, not a fallback.

**F3. Put display reads on their own port.**
- **What:** move the display/resample read path onto the idle `ram2` f2sdram port
  (`hw_budget_and_lessons.md` §1–2: measured zero decoder cost for a same-size master
  once moved). This removes the display from the decoder's arbiter altogether, whatever
  its byte rate.
- **Design item:**
  - Today same-port ordering guarantees that the decoder's reconstruction writes are in
    DDR3 before the display reads the picture back.
  - Across two ports that is no longer implied. The decoder's posted writes on `ram1` (the
    reconstruction tail of the last macroblock rows) must be known complete before the
    pickup that lets `ram2` read them.
  - Design it as an explicit write-drain handshake at `output_frame_valid`, not an
    assumption.
- **Gates:** a `check_*_wiring.py` for the new port nets; `lint_undriven`; HIL against
  the previous stage.

**F4. A deeper output queue.**
- **What:** the VLD is parked about 50 % of the time on every raster, because
  `motcomp_picbuf` hands over one picture at a time. Two or more decoded pictures queued
  ahead of the display would let light pictures bank time for heavy ones.
- **Why last:** it should resolve both §6b's remaining tail and §6c. It is also the
  largest change (frame-store slot allocation and the picbuf FSM).
- **Gates:** `motcomp_picbuf_tb`, `run_field_order.sh`, `run_disp_sched.sh`, plus the
  full stc/pts suite.

**Instrument: the per-picture maximum.** ✅ **Built, sim-gated and HW-measured
2026-09-29** (✅ MERGED PR #140). The result, below: §6c resolved, F4 not justified.
- **What a picture is:** one `picbuf_busy`-low stretch. `picbuf_busy` falls when the
  picbuf lets the VLD start a picture and rises at the next picture's header
  (`update_picture_buffers`, once per frame: a field pair is one picture). Its decode
  time is the stretch's **non-starved** cycles (back + active, the average "decode ms"
  above, per picture).
- **Telemetry words 21–24** (`dvd/dec_duty.sv` → `dvd_telem.sv`), behind a second marker,
  so a Main that knows only word 16's `DD01` keeps reading 17–20:

  | Word | Field | Meaning |
  |---|---|---|
  | 21 | `PIC_MAGIC` | `0xDD02`: words 22–24 exist (older cores answer 0 past word 20) |
  | 22 | `pic_max` | longest single-picture decode in the last completed 2^26-cycle window (0.83 s, longer than the 250 ms poll), cycles/4096. A level, held a whole window |
  | 23 | `pic_n` | pictures decoded (wraps) |
  | 24 | `pic_over` | … whose decode took longer than **one frame period** of the content: `frame_rate_code`'s period as an exact 81 MHz cycle count (29.97 → 2,702,700) |

- **Host:** `dvd_ctl.cpp` reads 25 words and emits `pic_max`/`pic_n`/`pic_over`.
  `mister.py telem_summary` reduces `pic_max` as a level (the max over the window's rows)
  and the counters as reset-aware rates, into `s['pic']` = `{max_ms, n, over,
  over_frac, over_per_s}`. `pacing_matrix.py` prints `picmax` and `over/s` per cell.
- **A dropped B picture** fires no update, so its skipped parse folds into the previous
  stretch: the max can read long there, never short.
- **Gates** (`bench/dvd/run_telem.sh`, all green):
  - `dec_duty_tb` [6]–[9]: the threshold is exact to the cycle (2,702,700 is not over,
    2,702,701 is); starved cycles are excluded; the threshold follows
    `frame_rate_code`; `pic_max` reads 0 until its window closes, then the window's
    longest picture, then 0 after an empty window;
  - `dvd_telem_tb` [7]; `test_telem_unwrap` [4]; `check_decode_duty_wiring` (the new
    seams, `frame_rate_code` from the VLD, both markers in the Main);
  - mutations M5–M8, each caught by its own arm.
- **Trap hit on the way:** the threshold was first written as an `always @*` case. The
  bench holds `frame_rate_code` constant from time zero, iverilog never evaluated the
  block, and `thr` stayed X, so `pic_over` never counted. It is a continuous assign now.
- **HW result (2026-09-29, `releases/DVD_pictime_20260929_1432.rbf` = F2 + the
  instrument; `clk_dec` 89.3 / 87.2 MHz; `dec_duty` 290 ALUT / 243 regs).** Two clean
  launches of Thayer's boot FMV straight into Interlaced with Disc Menus = On, telemetry
  logged from before the core loads (`mister.py launch --telem-log`, 130 s):

  | Launch | 0–60 s lates/s | 60–120 s | pic_max | Pictures over one frame period |
  |---|---|---|---|---|
  | 1 | 4.99 | **0.00** | 21.1 / 20.6 ms | **0 / 1649**, 0 / 1786 |
  | 2 | 5.36 | **0.00** | 21.1 / 20.7 ms | **0 / 1501**, 0 / 1794 |

  - **All** of the first minute's lates fall in the 5 s before the FMV starts, while the
    VM holds the First Play still: `flags.menu = 1, still = 1`, pickups flat at 1, VBUF
    empty, decoder starved, and `lates` +1 per refresh. From the FMV's first picture to
    the end of the capture: **0**.
  - The earlier captures (§6c) started after launch and never saw the still. The
    sustained FMV lates they did see (up to 58 per 5 s at 40–45 s) are gone.
  - **Decision: F4 is not justified.** Its premise is single heavy pictures that the
    one-deep handoff cannot absorb. No picture on this content exceeds 64 % of its budget,
    and the VLD is parked 54–62 % of the time.
- **Caveat for every reader of `lates`:** the governor counts a late on every refresh
  while a PGC **still** holds the picture. Filter `flags.still` before reading a
  startup or menu window as decode lateness (added to §2c).

**Workarounds available today (manual):**
- Video Output = **Interlaced** removes these lates on every disc measured.
- On PAL discs, **Film 24p Out = On** (25 Hz raster) removes them on Progressive too:
  Office 8.3 → 0, BBB 5 → 0. It trades away Bob and the in-core Blend's 50 Hz motion.
- A 29.97 Hz content-rate raster for NTSC video would be the NTSC equivalent. It would
  extend the existing film-raster path, be HDMI-only, and disable Bob. It is recorded as
  an option, not recommended over F1–F3.

## 8. Tools and gates added

| File | What |
|---|---|
| `tools/mister.py` `telem_count` / `telem_summary` | reset-aware rates, what each cell measured, `--jsonl` / `--json` / `--from`, and the `dec_*` duty fractions |
| `tools/test_telem_unwrap.py` | gate: reset, genuine wrap, duplicate rows, and a RED arm |
| `tools/pacing_matrix.py` | the scripted interleaved matrix |
| `tools/pacing_model.py` | the pickup-discipline model (§6b) |
| `dvd/dec_duty.sv` | where the decoder's time goes (telemetry words 16–20); instrument only, `DVD-FORK DEBUG` |
| `bench/dvd/dec_duty_tb.sv`, `dvd_telem_tb` [6] | exclusive classes and exact scale; the words in order with every input tied off |
| `tools/check_decode_duty_wiring.py` | motcomp → mpeg2video → emu → telem → qsf → dvd_ctl, plus the per-picture words 21–24 |
| `bench/dvd/run_osd_read.sh`, `tools/check_osd_read_wiring.py` | F1: bit-exact without the OSD reads, 8 → 6 words |
| `bench/dvd/run_chroma_reuse.sh`, `tools/check_chroma_reuse_wiring.py` | F2: bit-exact chroma row reuse in 14 arms, exact words per scan; the chroma-row fix's reference arms [4] (`+rowref`); M1–M7 |
| `resample_chain_tb` `+addrhash` / `+weave` / `+croptog` | address-hashed memory, the PIXSUM / RSUM / RQS lines, interlaced content on the progressive raster, a logical-point mid-line crop toggle |
| `bench/dvd/run_telem.sh` | runs all of the above, plus mutations M1–M4, each caught |

`dec_duty` costs 116 ALUT / 112 registers. `clk_dec` closes at 86.0 MHz at both slow
corners (`releases/DVD_pacing_20260928_2211.rbf`). Keep it through F1–F4: it is the
measurement each stage is judged by.

## Appendix A. Every cell

Columns: `ps`/`pf`/`frc` are the scheduler's modal flags (`ps` = `progressive_sequence`);
`rst` is the counter resets excluded; `lag`/`drift` are medians in ms. The duty column is
the parked / starved / pipe-stalled / active fractions, plus ref-wait. Builds: `v080`,
`v070`, `duty` (= `dev-pacing`), and `menu_il_cells` (clean Interlaced boot-FMV runs;
`dropab` = Frame Drop Off vs On).

#### v080
| disc | cell | late/s | drop/s | fps | raster | audHz | rst | ps | pf | frc | menu | still | blend | lag_ms | drift_ms | duty disp/starve/back/active |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| angel | auto_1 | 0.00 | 0.00 | 29.946 | 59.925 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +195.2 | - |
| angel | auto_2 | 0.00 | 0.00 | 29.951 | 59.934 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | +152.2 | - |
| angel | ilace_1 | 0.00 | 0.00 | 29.967 | 59.966 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +197.5 | - |
| angel | ilace_2 | 0.00 | 0.00 | 29.959 | 59.952 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | +166.4 | - |
| angel | prog-blend | 9.16 | 9.16 | 25.420 | 59.964 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 1.00 | +4.8 | +211.6 | - |
| angel | prog-bob | 9.19 | 9.16 | 25.371 | 59.935 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +4.8 | +197.5 | - |
| angel | prog-film-off | 9.17 | 9.17 | 25.379 | 59.927 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -0.2 | +163.2 | - |
| angel | prog-film-on | 5.98 | 5.91 | 23.983 | 23.983 | 43834 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -1551.6 | +184.0 | - |
| angel | prog_1 | 6.63 | 6.56 | 26.696 | 59.953 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +7.5 | +150.8 | - |
| angel | prog_2 | 8.78 | 8.78 | 25.605 | 59.952 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -0.2 | +192.2 | - |
| bbbpal | auto_1 | 0.00 | 0.00 | 24.987 | 50.014 | 47999 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | -20.1 | +240.9 | - |
| bbbpal | auto_2 | 0.00 | 0.00 | 24.994 | 49.988 | 47999 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | +221.2 | - |
| bbbpal | ilace_1 | 0.00 | 0.00 | 24.995 | 49.990 | 47999 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | -20.1 | +237.3 | - |
| bbbpal | ilace_2 | 0.00 | 0.00 | 25.009 | 50.019 | 47999 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | +216.7 | - |
| bbbpal | prog-blend | 7.01 | 3.52 | 21.497 | 50.002 | 48000 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +4.8 | +261.2 | - |
| bbbpal | prog-bob | 6.19 | 3.10 | 21.900 | 49.990 | 48000 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +4.8 | +260.6 | - |
| bbbpal | prog-film-off | 4.78 | 2.39 | 22.591 | 49.998 | 47999 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | +264.5 | - |
| bbbpal | prog-film-on | 0.00 | 0.00 | 25.034 | 25.034 | 47999 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | -15.3 | +271.1 | - |
| bbbpal | prog_1 | 5.10 | 2.57 | 22.446 | 49.990 | 47999 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | -6.8 | +254.4 | - |
| bbbpal | prog_2 | 0.79 | 0.40 | 24.602 | 49.995 | 47999 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | +247.5 | - |
| mib | auto_1 | 0.13 | 0.06 | 23.918 | 59.958 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | +163.4 | - |
| mib | auto_2 | 0.00 | 0.00 | 23.987 | 59.951 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | +125.2 | - |
| mib | ilace_1 | 0.00 | 0.00 | 23.965 | 59.945 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | +158.0 | - |
| mib | ilace_2 | 0.00 | 0.00 | 23.981 | 59.920 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | +135.8 | - |
| mib | prog-blend | 2.27 | 1.14 | 22.843 | 59.919 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | +2.5 | +160.7 | - |
| mib | prog-bob | 3.47 | 1.74 | 22.231 | 59.916 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | +2.5 | +157.0 | - |
| mib | prog-film-off | 3.34 | 1.65 | 22.287 | 59.917 | 48000 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -0.2 | +148.6 | - |
| mib | prog-film-on | 0.00 | 0.00 | 23.975 | 23.975 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | +2.3 | +154.1 | - |
| mib | prog_1 | 0.20 | 0.10 | 23.901 | 59.917 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -4.1 | +171.4 | - |
| mib | prog_2 | 2.62 | 1.31 | 22.714 | 59.958 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -0.2 | +169.1 | - |
| office | auto_1 | 0.00 | 0.00 | 24.992 | 50.017 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | -20.1 | +158.4 | - |
| office | auto_2 | 0.00 | 0.00 | 24.988 | 50.008 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | +120.5 | - |
| office | ilace_1 | 0.10 | 0.10 | 24.944 | 49.986 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | -20.1 | +162.8 | - |
| office | ilace_2 | 0.06 | 0.03 | 24.950 | 49.997 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | +124.4 | - |
| office | prog-blend | 8.31 | 4.15 | 20.863 | 49.999 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 1.00 | +10.0 | +172.4 | - |
| office | prog-bob | 8.29 | 4.16 | 20.861 | 50.007 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | +10.0 | +174.6 | - |
| office | prog-film-off | 8.38 | 4.15 | 20.808 | 49.991 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | +168.9 | - |
| office | prog-film-on | 0.00 | 0.03 | 25.001 | 24.968 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | +6.0 | +178.0 | - |
| office | prog_1 | 8.25 | 4.12 | 20.840 | 49.991 | 48000 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | -9.8 | +168.5 | - |
| office | prog_2 | 8.25 | 4.09 | 20.912 | 50.008 | 48000 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | +171.6 | - |
| roger | auto_1 | 0.00 | 0.00 | 29.986 | 59.939 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +74.1 | - |
| roger | auto_2 | 1.10 | 0.55 | 29.398 | 59.933 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +85.0 | - |
| roger | ilace_1 | 0.19 | 0.10 | 29.892 | 59.947 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +78.0 | - |
| roger | ilace_2 | 0.07 | 0.03 | 29.933 | 59.932 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +85.0 | - |
| roger | prog-blend | 9.83 | 4.90 | 25.054 | 59.942 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 1.00 | +4.3 | +64.2 | - |
| roger | prog-bob | 9.74 | 4.87 | 25.087 | 59.943 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +4.3 | +60.8 | - |
| roger | prog-film-off | 8.80 | 4.45 | 25.557 | 59.947 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +62.8 | - |
| roger | prog-film-on | 5.98 | 5.98 | 23.972 | 23.972 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -48.5 | +62.4 | - |
| roger | prog_1 | 8.97 | 4.48 | 25.521 | 59.942 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -1.4 | +100.6 | - |
| roger | prog_2 | 9.90 | 4.93 | 24.992 | 59.915 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +57.1 | - |
| thay08 | auto_1 | 0.39 | 0.39 | 29.486 | 59.944 | 43096 | 4 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +523.2 | - |
| thay08 | auto_2 | 0.39 | 0.39 | 29.749 | 59.941 | 45369 | 1 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +172.3 | - |
| thay08 | ilace_1 | 0.69 | 0.69 | 29.537 | 59.961 | 43405 | 3 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +166.4 | - |
| thay08 | ilace_2 | 0.10 | 0.10 | 29.824 | 59.943 | 44299 | 2 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +183.8 | - |
| thay08 | prog-blend | 6.21 | 6.21 | 26.854 | 59.916 | 44750 | 2 | 0 | 0 | 4 | 0.00 | 0.00 | 1.00 | +0.0 | +190.6 | - |
| thay08 | prog-bob | 6.90 | 6.90 | 26.502 | 59.900 | 45042 | 3 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +1502.0 | - |
| thay08 | prog-film-off | 6.99 | 7.09 | 26.295 | 59.927 | 44125 | 2 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +162.3 | - |
| thay08 | prog-film-on | 5.86 | 5.81 | 23.941 | 23.941 | 45032 | 3 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -609.1 | +176.7 | - |
| thay08 | prog_1 | 7.10 | 7.19 | 26.363 | 59.920 | 44496 | 2 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +1517.0 | - |
| thay08 | prog_2 | 6.67 | 6.72 | 26.634 | 59.938 | 41574 | 4 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +689.8 | - |
| thay09 | auto_1 | 0.00 | 0.00 | 29.993 | 59.948 | 46199 | 1 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +174.9 | - |
| thay09 | auto_2 | 0.00 | 0.00 | 29.971 | 59.942 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +195.7 | - |
| thay09 | ilace_1 | 0.00 | 0.00 | 29.946 | 59.932 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | -41.1 | - |
| thay09 | ilace_2 | 0.00 | 0.00 | 29.963 | 59.926 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +165.9 | - |
| thay09 | prog-blend | 7.32 | 3.68 | 26.298 | 59.954 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 1.00 | -8.2 | +149.7 | - |
| thay09 | prog-bob | 7.16 | 3.56 | 26.413 | 59.948 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -8.2 | +157.0 | - |
| thay09 | prog-film-off | 1.50 | 0.75 | 29.214 | 59.930 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +154.3 | - |
| thay09 | prog-film-on | 5.99 | 5.95 | 23.966 | 23.966 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -53.5 | +154.7 | - |
| thay09 | prog_1 | 7.28 | 3.60 | 26.373 | 59.947 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +6.0 | +176.4 | - |
| thay09 | prog_2 | 7.31 | 3.67 | 26.312 | 59.934 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +129.8 | - |
| vcd | auto_1 | 0.00 | 0.00 | 25.006 | 50.090 | 44099 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | -0.4 | -290.0 | - |
| vcd | auto_2 | 0.00 | 0.00 | 25.027 | 50.093 | 44099 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | -0.5 | -276.1 | - |
| vcd | ilace_1 | 0.00 | 0.00 | 25.018 | 50.075 | 44099 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +0.7 | -294.2 | - |
| vcd | ilace_2 | 0.00 | 0.00 | 25.018 | 50.114 | 44099 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +0.5 | -279.1 | - |
| vcd | prog-blend | 0.00 | 0.00 | 25.010 | 50.020 | 44099 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +9.4 | -262.0 | - |
| vcd | prog-bob | 0.00 | 0.00 | 24.982 | 50.003 | 44100 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +9.4 | -259.6 | - |
| vcd | prog-film-off | 0.00 | 0.00 | 25.012 | 50.024 | 44099 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | -258.8 | - |
| vcd | prog-film-on | 0.00 | 0.00 | 25.018 | 25.018 | 44100 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | -11.2 | -260.6 | - |
| vcd | prog_1 | 0.00 | 0.00 | 25.026 | 49.972 | 44100 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | +206.2 | - |
| vcd | prog_2 | 0.00 | 0.00 | 25.022 | 49.964 | 44099 | 0 | 1 | 1 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | -258.8 | - |

#### v070
| disc | cell | late/s | drop/s | fps | raster | audHz | rst | ps | pf | frc | menu | still | blend | lag_ms | drift_ms | duty disp/starve/back/active |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| roger | auto_1 | 0.00 | 0.00 | 29.986 | 59.908 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +76.3 | - |
| roger | auto_2 | 1.17 | 0.58 | 29.361 | 59.923 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +86.6 | - |
| roger | ilace_1 | 0.20 | 0.10 | 29.871 | 59.937 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +78.2 | - |
| roger | ilace_2 | 0.13 | 0.07 | 29.904 | 59.939 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +83.4 | - |
| roger | prog_1 | 8.83 | 4.41 | 25.569 | 59.931 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +7.5 | +98.1 | - |
| roger | prog_2 | 9.89 | 4.94 | 25.046 | 59.946 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +58.5 | - |
| thay08 | auto_1 | 0.39 | 0.39 | 29.458 | 59.951 | 41384 | 3 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +811.6 | - |
| thay08 | auto_2 | 0.59 | 0.59 | 29.686 | 59.964 | 45073 | 1 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +186.5 | - |
| thay08 | ilace_1 | 0.39 | 0.39 | 29.697 | 59.934 | 40736 | 3 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +164.3 | - |
| thay08 | ilace_2 | 0.00 | 0.00 | 29.861 | 59.919 | 44663 | 2 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +204.1 | - |
| thay08 | prog_1 | 6.80 | 6.80 | 26.556 | 59.911 | 46746 | 3 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +1520.5 | - |
| thay08 | prog_2 | 6.69 | 6.79 | 26.621 | 59.934 | 41643 | 4 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +627.9 | - |

#### duty
| disc | cell | late/s | drop/s | fps | raster | audHz | rst | ps | pf | frc | menu | still | blend | lag_ms | drift_ms | duty disp/starve/back/active |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| mib | auto_1 | 0.13 | 0.06 | 23.938 | 59.959 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -0.2 | +167.1 | 0.68/0.00/0.30/0.02 ref 0.25 |
| mib | auto_2 | 0.00 | 0.00 | 23.951 | 59.942 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | -185.4 | 0.69/0.00/0.29/0.02 ref 0.23 |
| mib | ilace_1 | 0.00 | 0.00 | 23.980 | 59.933 | 48000 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -0.2 | +167.8 | 0.67/0.00/0.30/0.02 ref 0.25 |
| mib | ilace_2 | 0.00 | 0.00 | 23.983 | 59.940 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | -186.3 | 0.69/0.00/0.29/0.02 ref 0.23 |
| mib | prog_1 | 0.13 | 0.07 | 23.914 | 59.931 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | +5.3 | +165.2 | 0.67/0.00/0.31/0.02 ref 0.26 |
| mib | prog_2 | 2.75 | 1.37 | 22.620 | 59.937 | 47999 | 0 | 0 | 1 | 4 | 0.00 | 0.00 | 0.00 | -0.2 | +172.3 | 0.58/0.00/0.40/0.02 ref 0.36 |
| office | auto_1 | 0.00 | 0.00 | 24.994 | 49.987 | 48000 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | -20.1 | +169.1 | 0.55/0.00/0.42/0.03 ref 0.34 |
| office | auto_2 | 0.00 | 0.00 | 25.013 | 50.025 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | -20.1 | +165.5 | 0.57/0.00/0.41/0.03 ref 0.33 |
| office | ilace_1 | 0.13 | 0.06 | 24.916 | 49.995 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | -20.1 | +175.3 | 0.54/0.00/0.43/0.03 ref 0.36 |
| office | ilace_2 | 0.07 | 0.03 | 24.958 | 50.015 | 48000 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | -20.1 | +172.1 | 0.58/0.00/0.39/0.02 ref 0.32 |
| office | prog-blend | 8.35 | 4.16 | 20.815 | 49.975 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 1.00 | +1.2 | -118.0 | 0.49/0.00/0.48/0.02 ref 0.44 |
| office | prog-bob | 8.29 | 4.13 | 20.828 | 50.007 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | +1.2 | -118.9 | 0.50/0.00/0.48/0.02 ref 0.43 |
| office | prog-film-off | 8.31 | 4.19 | 20.806 | 49.987 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | +0.0 | -112.7 | 0.49/0.00/0.48/0.03 ref 0.44 |
| office | prog-film-on | 0.00 | 0.00 | 24.992 | 24.992 | 48000 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | -10.0 | -120.9 | 0.67/0.00/0.31/0.02 ref 0.25 |
| office | prog_1 | 8.18 | 4.12 | 20.939 | 49.993 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | -9.8 | +171.2 | 0.51/0.00/0.47/0.02 ref 0.42 |
| office | prog_2 | 8.25 | 4.09 | 20.910 | 50.002 | 47999 | 0 | 0 | 0 | 3 | 0.00 | 0.00 | 0.00 | -0.2 | +165.7 | 0.51/0.00/0.46/0.02 ref 0.42 |
| roger | auto_1 | 0.00 | 0.00 | 29.978 | 59.956 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | +76.3 | 0.69/0.00/0.28/0.02 ref 0.19 |
| roger | auto_2 | 1.31 | 0.65 | 29.302 | 59.947 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +73.1 | 0.53/0.00/0.45/0.02 ref 0.39 |
| roger | auto_3 | 0.29 | 0.13 | 29.793 | 59.946 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | +78.8 | 0.53/0.00/0.45/0.03 ref 0.39 |
| roger | ilace_1 | 0.20 | 0.10 | 29.861 | 59.952 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | +74.3 | 0.53/0.00/0.44/0.03 ref 0.38 |
| roger | ilace_2 | 0.07 | 0.03 | 29.949 | 59.964 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +69.7 | 0.54/0.00/0.44/0.02 ref 0.38 |
| roger | ilace_3 | 1.24 | 0.62 | 29.361 | 59.933 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -16.7 | +81.4 | 0.56/0.00/0.42/0.02 ref 0.35 |
| roger | prog_1 | 8.90 | 4.45 | 25.544 | 59.951 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -6.0 | +100.1 | 0.47/0.00/0.50/0.02 ref 0.46 |
| roger | prog_2 | 9.87 | 4.94 | 25.037 | 59.912 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +78.2 | 0.48/0.00/0.49/0.02 ref 0.45 |
| roger | prog_3 | 8.90 | 4.42 | 25.532 | 59.932 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +85.5 | 0.45/0.00/0.53/0.02 ref 0.48 |
| thay08 | auto_1 | 0.59 | 0.59 | 29.369 | 59.918 | 42844 | 4 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +704.0 | 0.56/0.00/0.40/0.03 ref 0.33 |
| thay08 | auto_2 | 0.49 | 0.49 | 29.726 | 59.944 | 45371 | 1 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +181.7 | 0.57/0.00/0.40/0.03 ref 0.34 |
| thay08 | ilace_1 | 0.30 | 0.30 | 29.708 | 59.957 | 41444 | 3 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +196.3 | 0.55/0.00/0.41/0.03 ref 0.35 |
| thay08 | ilace_2 | 0.00 | 0.00 | 29.871 | 59.941 | 44227 | 2 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +191.8 | 0.56/0.00/0.40/0.03 ref 0.34 |
| thay08 | prog_1 | 6.80 | 6.80 | 26.612 | 59.974 | 46755 | 3 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +1515.6 | 0.50/0.00/0.46/0.03 ref 0.41 |
| thay08 | prog_2 | 6.74 | 6.70 | 26.584 | 59.961 | 41459 | 4 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +434.8 | 0.49/0.00/0.47/0.03 ref 0.42 |
| thay09 | auto_1 | 0.00 | 0.00 | 29.990 | 59.940 | 45668 | 1 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +202.8 | 0.58/0.00/0.38/0.03 ref 0.29 |
| thay09 | auto_2 | 0.00 | 0.00 | 29.968 | 59.935 | 48000 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +204.4 | 0.60/0.00/0.37/0.03 ref 0.28 |
| thay09 | ilace_1 | 0.00 | 0.00 | 29.950 | 59.939 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +165.3 | 0.62/0.00/0.35/0.03 ref 0.25 |
| thay09 | ilace_2 | 0.00 | 0.00 | 29.975 | 59.949 | 47032 | 1 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +165.7 | 0.58/0.00/0.39/0.03 ref 0.30 |
| thay09 | prog_1 | 7.23 | 3.60 | 26.397 | 59.946 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | -1.1 | +165.3 | 0.54/0.00/0.43/0.03 ref 0.35 |
| thay09 | prog_2 | 7.40 | 3.68 | 26.234 | 59.907 | 47999 | 0 | 0 | 0 | 4 | 0.00 | 0.00 | 0.00 | +0.0 | +201.2 | 0.53/0.00/0.44/0.03 ref 0.36 |
| thaymenu | auto_1 | 4.93 | 4.93 | 27.506 | 59.942 | 47999 | 0 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | +0.0 | +179.2 | 0.59/0.00/0.37/0.03 ref 0.31 |
| thaymenu | auto_2 | 3.35 | 3.35 | 28.269 | 59.935 | 47999 | 0 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | +0.0 | +168.7 | 0.58/0.00/0.38/0.03 ref 0.32 |
| thaymenu | ilace_1 | 0.40 | 0.40 | 29.777 | 59.954 | 48000 | 0 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | +0.0 | +118.6 | 0.56/0.00/0.40/0.03 ref 0.34 |
| thaymenu | ilace_2 | 0.15 | 0.20 | 29.862 | 59.970 | 48000 | 0 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | -16.9 | +58.1 | 0.56/0.00/0.40/0.03 ref 0.34 |
| thaymenu | prog_1 | 5.91 | 5.91 | 27.005 | 59.923 | 48000 | 0 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | +0.0 | +186.5 | 0.52/0.00/0.44/0.03 ref 0.37 |
| thaymenu | prog_2 | 7.49 | 7.39 | 26.223 | 59.939 | 47999 | 0 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | +0.0 | +177.8 | 0.51/0.00/0.46/0.03 ref 0.40 |

#### menu_il_cells
| disc | cell | late/s | drop/s | fps | raster | audHz | rst | ps | pf | frc | menu | still | blend | lag_ms | drift_ms | duty disp/starve/back/active |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| dropab | Interlaced_fdOff | 9.76 | 0.00 | 28.380 | 59.944 | 46491 | 2 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | -1334.8 | +159.6 | 0.57/0.00/0.39/0.03 ref 0.33 |
| dropab | Interlaced_fdOn | 3.38 | 3.36 | 28.288 | 59.936 | 47999 | 1 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | +0.0 | +177.4 | 0.59/0.00/0.38/0.03 ref 0.31 |
| thaymenu_clean | dev-pacing_1 | 3.41 | 3.39 | 28.272 | 59.939 | 47965 | 1 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | +0.0 | +176.7 | 0.59/0.00/0.38/0.03 ref 0.31 |
| thaymenu_clean | dev-pacing_2 | 0.86 | 0.87 | 29.547 | 59.949 | 48000 | 1 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | +0.0 | +174.0 | 0.57/0.00/0.39/0.03 ref 0.33 |
| thaymenu_clean | v080_1 | 3.39 | 3.39 | 28.269 | 59.932 | 47999 | 1 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | +0.0 | +178.5 | - |
| thaymenu_clean | v080_2 | 0.89 | 0.87 | 29.535 | 59.942 | 47999 | 1 | 0 | 0 | 4 | 1.00 | 0.00 | 0.00 | +0.0 | +173.0 | - |

