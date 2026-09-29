# Audio gaps at non-seamless cell joins (Thayer's Quest VTS_08) — investigation

**Status:** ⏳ open, not started (hand-off written 2026-09-29). Branch
`feature/nonseamless-audio` (`CORE_VERSION dev-nsaudio`, set in its first commit).
Symptom first recorded in `docs/decode_pacing.md` §2b.

## 1. Symptom

- **Where:** Thayer's Quest's field-coded title set VTS_08 (`Title VTS Units=8`,
  `Disc Menus=Off`). Its other field-coded VTSes (01–08, 10) are likely the same.
- **What:** 1–4 audio restarts per 20 s window, then **silence while the video keeps
  playing**. Over a whole window only 41–46 kHz worth of audio plays per wall-second, so
  about 5–13 % of the time is silent.
- **Age:** identical on v0.7.0, v0.8.0, the pre-F1 build and after F2. It is not a
  regression, and it has nothing to do with the decode-pacing work (video lates are 0
  there).
- **Also seen:** `av_drift_ms` medians of about +1.5 s in some VTS_08 windows (within
  the ±5.8 s range, so not an alias).

## 2. What the existing telemetry already shows (no rig time needed)

Raw 0.25–0.5 s rows (gitignored, on the workstation) for this exact cell exist from four
builds:
`.sim/pacing/{v070,v080,duty,f2wide}/thay08/**/*.jsonl` (one file per matrix cell;
`f2wide` is the current build).

A first look at `f2wide/thay08/f2_thay08/{auto,ilace}_*.jsonl` (2026-09-29) shows the
same shape at every restart:

| Row | `reanchors` | `aud_play` | `aud_frames` | `av_drift_ms` |
|---|---|---|---|---|
| the join | +1 (sometimes +2) | **reset** | 0 | jumps to **+1.1 … +2.3 s** |
| +0.5 s | 0 | +0 (silent) | refilling 6–27 | falling by ~500 per 0.5 s |
| +1.0 s | 0 | +0 (silent) | ~34 (ring full) | ~+300 |
| +1.5 s | 0 | resumes (partial row) | ~34 | ~+200 |

**Reading:**
- The re-anchor flushes the audio ring and decoder (`flush_ctl` `aud_resync`).
- The next cell's audio then arrives with PTSs **1–2 s ahead** of the re-anchored clock.
- The drain gate holds it until the clock walks up to it: `av_drift` falls at exactly
  wall-clock speed while the ring sits full and silent.
- So **each gap is about the audio's lead over the re-anchored clock, ~1.5–2 s**, and the
  video plays through it.
- Two re-anchors ~0.5 s apart (`+2`, or back-to-back rows) appear too.

This is the mechanism `docs/stc_freerun.md` §12.2 describes ("flushing costs ~1 s of
audio twice over: the queued ring frames are discarded, and the drain gate then cannot
re-open until the display clock walks up to a `play_pts`"). §12.2 fixed it only for cells
the author marked **seamless** (`cell_playback_t` byte 0 bit 3, exported as
`cell_seamless`; `flush_ctl` now withholds the audio flush there). Thayer's joins are
presumably genuinely **non-seamless**, so they still take the flush. **Confirm that from
the IFO first** (§4 step 1).

## 3. Questions to answer

1. **Breadth.** Is this one game disc or a class? Which discs have non-seamless cell
   joins *inside a title* (not menus), and how many per hour of playback?
2. **Is the flush needed at a non-seamless join?** What does a real player (libdvdnav)
   do with audio there? A non-seamless join may legitimately restart the STC, but it
   doesn't have to drop the tail of the old cell's audio or wait out the new cell's full
   mux lead.
3. **Why is the new cell's audio ~1–2 s ahead of the re-anchored clock?** Is that the
   disc's mux lead (audio packs muxed ahead of video), or does the clock re-anchor to a
   video PTS that is behind? The `anch_fwd` / `anch_bwd` and `disp_lag_ms` fields in the
   same rows may separate the two.
4. **The double re-anchor** (`reanchors +2` in one row). Is it one join counted twice, or
   two joins?

## 4. Plan

1. **IFO census (no rig).** For every title PGC in the library (`$DVD_ISO_DIR`; see the
   memory note on the library location), list the cell category byte (`seamless_play`,
   `stc_discontinuity`, interleaved) and count the non-seamless joins inside titles.
   Start with Thayer VTS_08 to confirm its joins are non-seamless.
   - `tools/dvd_census.py` already walks every ISO with our own validated parsers
     (`IsoNav` from `dvd_vm_ref.py`) and is the natural place to add a column.
   - Sweep **every VTS and PGC**, not just the default one (CLAUDE.md lesson).
2. **Measure each gap from the existing rows (no rig).** Script the §2 analysis over all
   four builds' `thay08` rows: gap length per re-anchor (from `aud_play` flat to resumed),
   and the `av_drift` at the join.
3. **Only then the rig:** measure one Thayer join with the capture card (`audio_check.py`
   or a plain capture) to confirm the silence is audible and matches the telemetry, and
   try one other disc from the census with in-title non-seamless joins.
4. **Design:** decide from 2–3 what the right behaviour at a non-seamless join is, and
   write the rationale here before touching `flush_ctl`.

## 5. Where to look

| What | Where |
|---|---|
| Audio flush on re-anchor, and the seamless carve-out | `dvd/flush_ctl.sv` (`aud_resync`, `cell_seamless`); `docs/stc_freerun.md` §3.7, §12.2 |
| Soft vs hard audio reset, ring behaviour after `aud_resync` | `docs/fabric_audio.md` (around the "aud_resync & ~aud_flush" note) |
| Drain gate, `play_pts`, A/V scheduling | `docs/av_sync.md`, `docs/stc_freerun.md` |
| Cell category byte in the reader | `dvd/dvd_iso_reader.sv` (`cell_cat_mem`, `cell_seamless`) |
| Existing gate for the seamless case | `bench/dvd/run_seamless_audio.sh` (`--red`) |
| Telemetry fields | `main/support/dvd/dvd_ctl.cpp` (names), `tools/mister.py telem_summary` |

## 6. Traps already known

- **`aud_play` resets are excluded from rates** by `telem_summary` (reset-aware), so a
  window's `audio_hz` understates the silence only by the reset intervals. Use the raw
  rows for gap lengths.
- **The rig is shared** with the maintainer and other Claude sessions: run
  `tools/mister.py state` before every deploy, even after being told "go", and
  `restore` afterwards (`.claude/skills/hil-testing/`).
- **Thayer VTS_08 needs `--opt "Title VTS Units=8"`**: with `Disc Menus=Off`, Auto plays
  VTS_09, which is frame-coded and a different structure (`decode_pacing.md` §3).
- **`lates` count once per refresh during a PGC still** (`decode_pacing.md` §2c). Irrelevant
  to audio, but it will show up in any boot capture.
