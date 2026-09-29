# Audio gaps at non-seamless cell joins (Thayer's Quest VTS_08) — investigation

**Status:** ⏳ open. Offline analysis done 2026-09-29 (§2a); event-level rig capture next. Branch
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

## 2a. Findings, 2026-09-29 (no rig time yet)

Scripts are in `.sim/nsaudio/` (gitignored): `ifo_cells.py` (cell table),
`cell_pts.py` (per-cell PTS extents), `join_pics.py` (pictures across a join),
`rows.py` (one character per telemetry row), `census.py` / `census2.py` (library).

**The IFO confirms the joins are authored non-seamless.** VTS_08 PGC 1 has 63 cells in
two kinds that alternate: a ~5–22 s clip cell with `byte0 = 0x03` (`stc_discontinuity`
and `seamless_angle`, **no `seamless_play`**), then a ~1 s cell with `0x09`
(`seamless_play`, `still = 255`). A cell scan puts numbers on it. Every `0x03` cell restarts
video and audio at PTS ≈ 0.06–0.18 s. Every `0x09` cell continues the previous one
(for example c1 video 0.094–15.443, then c2 15.576–16.511). All the other VTSes measure the
same way (`census2.py`: 26–34 backward joins per PGC in VTS 2–8).

**Q3 is answered: the lead is the parse-front / VBUF depth, not a mux lead.** At every
`0x03` cell the first audio PTS in stream order equals the first video PTS (`firstA ≈
firstV`, within 30 ms). So the new cell's audio is not muxed ahead. After the flush the
ring has lost whatever the demux had already parsed past the join (the new cell's first
~1.1–1.4 s). The next frame to arrive is that far ahead of the clock, which has just
re-anchored to the displayed picture.

**There are two defects, not one.** One character per 0.5 s row (`rows.py`):
`p` = playing in phase, `s` = silent, `E` = playing with `av_drift` > +0.7 s, digit =
re-anchors in that row.

```
f2wide auto_1   EEEEEEEEEEEEEEEEEE11sspppppppppppppppp2EEEEEEEELL
f2wide ilace_1  ppppppppppp11ssspppppppL1spppppppppp2EEEEEEEEEEEE
f2wide prog_1   EEEEEEEEEEEEEEEEEEEEEEEEEEELLL1sspppppppppppppppp
```

1. **Gap (`1ss` then `p`).** The known shape: `aud_resync`, the ring refills and stays silent
   while `av_drift` walks down at wall-clock speed, then scheduled playback resumes in
   phase (`play_err` 0, ring 33–34 frames). Gap ≈ 0.5–1.5 s.
2. **Early audio (`E`), and it is the worse one.** After some joins, often those with two
   re-anchors in one row, audio restarts **immediately** and keeps playing. The ring is
   empty (`aud_frames` 0–3), `av_drift` is about +1.5 s, and `play_err` is frozen at
   **−1.31 … −1.41 s**. By that field's own definition the audio is running **~1.4 s ahead
   of the picture**. It stays that way for 10–15 s, until the next join's flush happens
   to re-time it. Nothing corrects early audio in mid-play, because `head_catchup` only
   discards LATE audio. The ring is empty because audio now plays at the parse front: the
   demux is paced by the full VBUF, so audio arrives at exactly real-time rate and never
   underruns. That makes the state stable.

**What the counters rule out:**
- **No `load_flush` / jump at these joins.** `reanchors` keeps counting across every event,
  and `disp_sched` zeroes it on `flush` (= `load_flush`). So the audio resets are
  `aud_resync` (disc_rephase), not `jump_ack`/`seek_ack`.
- **`anch_bwd` / `anch_fwd` say nothing.** They are 2-bit saturating counters, pinned at
  3/0 since before each window.
- **Word 5 (`vid_err` in the JSON, = `{skip, catch-up, re-arms}`).** At an `E` event there is
  one underrun re-arm after the reset, with no skip and no catch-up. At a `1ss` event there
  are 0–2 re-arms. So "release → underrun → re-arm → release" occurs inside the event.
  The 0.5 s rows cannot show which frame each release latched.
- **The failure is deterministic.** `duty/thay08` and `v070/thay08` are different runs
  (different `t`, different counter values), and they classify identically row for row.
  The same launch reproduces the same joins at the same moments, which makes a rig
  capture cheap to repeat.

**How the `E` state arises is NOT settled.** A scheduled release needs `stc >= play_pts`,
so `play_err` starts at ≥ 0. To reach −1.4 s, either the clock stepped back ~1.4 s after
the release with no audio reset, or `play_pts` latched a frame from a different timeline
than the clock. One candidate to test: **`cell_seamless` is a parse-front level, and it is
sampled at a display-time event.** The reader sets it when it *starts streaming* a cell.
`disc_rephase` fires when the *display* reaches the join, ~1.3 s later in content. With
Thayer's 1 s `0x09` cells between clips, the reader can already be inside a
`seamless_play` cell when the display crosses a `0x03` join. The flush is then withheld
for the wrong cell, or the reverse. The Matrix never exposed this because its seamless
cells are minutes long. The event-level capture (§4 step 3) decides it.

**Library census (flag only, `census.py`, 1,527 ISOs parsed):** 1,064 discs have at least
one in-title join flagged `stc_discontinuity && !seamless_play` (angle-block interiors
skipped), and 299 have `seamless_play && stc_discontinuity` (the Matrix class). Many of these
are play-all extras and episode joins, not the main feature. A flag is a claim, so
`census2.py` MEASURES every join from the NAV packs (`vobu_s_ptm` of the new cell minus
`vobu_e_ptm` of the previous one; backward = what trips `disc_jump_w`) and records each
PGC's length. That is what separates main features from extras. ⏳ Running.

**Design requirement (recorded before choosing a mechanism).** Withholding the flush alone
is not a fix. §12.2's seamless carve-out works because a seamless cell's audio really does
continue sample for sample. At a non-seamless join, playing on without a re-time is exactly
how the `E` state keeps audio 1.4 s early. Whatever the fix, it must (a) keep the new cell's
opening audio rather than discard it, (b) let the old cell's tail play out, and (c) re-time
audio to the new timeline **at the new cell's first audio frame**. It must also cope with
the clock not yet having re-anchored when that frame reaches the head of the queue.

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
