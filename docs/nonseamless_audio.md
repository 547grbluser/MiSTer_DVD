# Audio gaps at non-seamless cell joins (Thayer's Quest VTS_08) — investigation

**Status:** 🔧 fixed in sim, ⏳ HW round pending (2026-09-29). Root cause 1: `pts_assoc` second-field PTS (§2b). Root cause 3: in-band audio re-time (§4a). §4b is the next step. Branch
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

**How the `E` state arises: SUPERSEDED by §2b** (a PTS mis-tag plus the re-phase cooldown; the `cell_seamless` hypothesis below was not the cause). The text as first written: A scheduled release needs `stc >= play_pts`,
so `play_err` starts at ≥ 0. To reach −1.4 s, either the clock stepped back ~1.4 s after
the release with no audio reset, or `play_pts` latched a frame from a different timeline
than the clock. One candidate to test: **`cell_seamless` is a parse-front level, and it is
sampled at a display-time event.** The reader sets it when it *starts streaming* a cell.
`disc_rephase` fires when the *display* reaches the join, ~1.3 s later in content. With
Thayer's 1 s `0x09` cells between clips, the reader can already be inside a
`seamless_play` cell when the display crosses a `0x03` join. The flush is then withheld
for the wrong cell, or the reverse. The Matrix never exposed this because its seamless
cells are minutes long. The event-level capture (§4 step 3) decides it.

### 2b. SETTLED by the 20 ms capture (2026-09-29): the `E` state is a PTS mis-tag plus the re-phase cooldown

Capture: `DVD_pictime` core (= `main` RTL), the custom Main from `97bead5` (the
`dvd_telem_fast` knob), `mister.py launch ... --telem-fast-ms 20 --telem-seconds 120`,
`Disc Menus=Off, Title VTS Units=8`. 5,967 samples, `.sim/nsaudio/fast_auto_1.jsonl`,
read with `.sim/nsaudio/events.py`. Events line up with the IFO cell durations to within
~0.3 s: **PGC 1 plays its cells in order and the `still = 255` holds are not applied**
in this mode.

**Two anchors at most joins, not one.** 0.12–0.8 s *before* the real timeline change,
the display re-anchors on a picture only **−66.8 ms** (2 frames) off its extrapolated
timeline. Because it is backward, it counts as a content discontinuity: `aud_resync`,
ring flushed. Then the real join arrives. `disp_lag` reads −4.9…+5.2 s there, which is
the 16–6 s step aliased by the ±5.8 s field.

**Root cause 1: `pts_assoc` tags a SECOND field.** In this field-coded video, a PES
carrying the next I-frame's PTS starts between the two field start codes of the preceding
B picture. The positional rule (`pts_assoc.sv`, and `tools/pts_map.py`'s golden) gives the
mark to the first *picture start code* at or after the payload. That is the B's **second
field**, and `disp_sched` then subtracts one field. The value proves the tag wrong. c2's mark
is 16.377 s, **exactly the next I's display time**, while the B it lands on displays at
16.310. So that frame is stamped ~50 ms LATE (16.377 − 1 field = 16.360 against 16.310), the
timeline extrapolates from it, and the next correct tag falls more than a frame behind. `disc_jump_w` fires. The measured value is the
argument: the author's PTS is the I's. (A second field does not begin a new frame, so a
reference decoder does not give it a frame's timestamp either. Verify the exact clause or
decoder behaviour before citing it.)
`.sim/nsaudio/second_tags.py` lists every tag that lands on a second field in PGC 1. All 20
are on B pictures, and the capture matches their predicted positions:

| cell | second-field tag, s before cell end | spurious anchor, s before the real join |
|---|---|---|
| c2 | 0.13 | 0.125 |
| c8 | 0.80 | 0.79 |
| c11 | 0.27 | 0.33 |
| c13 (1.07 s cell) | 0.80 | 0.35 s *after* c13 starts (the cooldown suppressed its reset) |
| c15 | 0.53, 0.40 | 0.48 |
| c17 | 0.13 | 0.14 |
| c4, c6, c9, c12 | none | none |

**Root cause 2: the ~0.62 s re-phase cooldown (`emu.sv` `rephase_cool`) eats the real join.**
The spurious anchor starts the cooldown, so the real join's re-phase inside it is dropped.
The full sequence at c12 (t = 81.74–82.09 s):
1. The dispatcher has already crossed into the new cell's audio (`av_drift` shows a −6.1 s step).
2. The spurious anchor flushes the ring. The clock is still on the OLD timeline (~6.3 s).
3. The refilled ring's first frame is new-cell audio (PTS ~1.3). Against the old clock it looks
   5 s LATE, so the gate releases at once (`play_err` +4985).
4. The real join steps the clock back to ~0.1 s, with no audio reset (cooldown).
5. The audio is now 1.41 s EARLY (`play_err` −1412), and stays that way for the whole clip.

With the gap ≥ 0.62 s (c9: 0.79 s) the join's reset fires, and only a normal gap results.

**Root cause 3 (the original report): each real join's `aud_resync` discards the new
cell's opening**, ~1.3 s of audio the demux had already parsed. The gap is that long.
Joins with no spurious anchor (c5, c10, c13, c14) show it alone: reset, quiet 1.3–1.6 s,
then in phase.

**Fix order.** (1) `pts_assoc` + golden: a second-field header neither claims a mark nor
advances the drop horizon. That removes the spurious anchors, their extra gaps and the
`E` state on this disc. (2) The cooldown silently drops a real discontinuity. That is a
latent `E` for any two joins within 0.62 s, so revisit it after (1). (3) The head-discard
gap at every non-seamless join (§4 requirement).

**Library census (flag only, `census.py`, 1,527 ISOs parsed):** 1,064 discs have at least
one in-title join flagged `stc_discontinuity && !seamless_play` (angle-block interiors
skipped), and 299 have `seamless_play && stc_discontinuity` (the Matrix class).

**Measured census (`census2.py`, 1,525 discs with title PGCs, 2026-09-29).** Every
in-title join was measured from the NAV packs as `vobu_s_ptm` of the new cell minus
`vobu_e_ptm` of the previous one. A backward step is what trips `disc_jump_w`.
- **1,036 discs** have at least one measured backward join inside a title PGC. Of
  80,793 backward joins, 74,853 are flagged `D` only (non-seamless), 5,930 `SD`
  (seamless, already carved out), and 10 `S` only.
- **Main feature** (proxy: each disc's longest PGC): 895 discs have at least one
  non-seamless backward join, 2,336 joins in all. **606 of them have exactly one, and 399
  of those are at the LAST cell**, a short end card after the credits, where losing
  ~1.3 s is barely audible. 180 are mid-PGC: episode boundaries in play-all PGCs (for
  example 7th Heaven) and Disney "fast play" chains (WALL-E 64, Finding Nemo 50).
- **The heaviest users are FMV games:** Last Bounty Hunter 96, Space Pirates 85, Drug
  Wars 76, Mad Dog 67 / Mad Dog 2 64, Crime Patrol 40, Who Shot Johnny Rock 27, all in
  PGCs of 3–7 minutes. That is one join every few seconds, and each one costs ~1.3 s of
  audio today.
- Field-coded video (root cause 1) is rare in a 230-disc sample: Thayer's Quest and the
  Mad Dog discs so far (`field_scan.py`).

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
   write the rationale here before touching `flush_ctl`. → §4a (proposal, not yet
   approved).

## 4a. Design for root cause 3 (the head-discard gap): IMPLEMENTED, sim-gated (approved by the maintainer 2026-09-29)

**Why the flush cannot simply be kept, moved or narrowed.** It fires when the *display*
crosses the join, but it resets the *ring*. The ring is a parse-front structure that by
then holds the new cell's first ~1.1–1.4 s. Any display-time reset of it throws that
opening away, whatever qualifier gates it. VLC's `ES_OUT_RESET_PCR` flush is harmless
only because VLC flushes at the demux, before any new-cell data enters its buffers.
Withholding the flush (the §12.2 seamless carve-out, generalised) keeps the audio but
loses the re-time. That is exactly how the `E` state kept audio 1.4 s early.

**Proposal: re-time in band, at the audio frame that carries the discontinuity.**
In `dvd_audio_decode`'s dispatcher:
1. **Detect.** A PTS-tagged frame at the ring head whose PTS steps off the dispatched
   timeline (backward by more than ~1 frame, or forward by more than an authored-gap
   bound) is an audio discontinuity. It is carried by the frame itself, so nothing
   depends on which cell the reader is in (the `cell_seamless` parse-front vs display
   hazard in §2a) or on the display's anchor timing.
2. **Let the old timeline finish.** Stop dispatching at that frame (do not pop it). The
   decode/PCM FIFOs drain at the normal rate: the old cell's tail plays out, nothing is
   discarded.
3. **Re-arm with empty FIFOs.** When they are empty, clear `draining` / `play_pts_valid`
   and dispatch the held frame, so it latches `play_pts`. This is the re-arm the v5.3
   comment warns about, but taken with the FIFOs EMPTY, which is the one condition
   under which it cannot deadlock.
4. **Release only on the right timeline.** Release when `0 <= stc - play_pts < WIN`
   (WIN ≈ 0.5 s), not merely `>= 0`. While the clock is still on the old timeline a
   backward-stepped frame reads grossly "late", which is precisely the case that
   released new-cell audio against the old clock in the `E` sequence (§2b step 3).
   Holding it until the display's own re-anchor brings the clock within WIN of it makes
   the ordering irrelevant. `arm_timer`'s ~2.5 s fallback remains the liveness bound.
5. **Retire the display-time `aud_resync` on `disc_rephase`** (keep it for `aud_switch`),
   and with it the `rephase_cool` cooldown's audio role. The menu problem it was added
   for (#63: old-timeline audio carried across a discontinuity) is handled by 1–4 at
   the frame where it actually happens.

**Expected cost at a join:** the hold lasts from the old audio's end to the display's
re-anchor. That is about the dispatch lead, ~0.2 s at most, instead of ~1.3 s. The ring
backpressures the demux for that time, well inside the ~1 s VBUF cushion.

**Risks to measure before shipping:**
- **Matrix seamless branches** (audio continuous across a PTS restart) would take a short
  hold where today they take none. Measure it on the rig. If it is audible, a seamless
  frame would need a flag carried through the ring rather than a cell-level level.
- **Forward audio gaps** that are authored (audio pauses while video runs) would now be
  PTS-scheduled instead of played early. That is more correct, but new.
- **Menus:** every keep_vbuf hop and looping cell goes through the new path instead of
  the reset. Menu sets (T2, Scooby-Doo 2, Harry Potter) are the regression gate.
- **Bench first:** a dispatcher bench with a synthetic ring carrying old-tail +
  new-head frames, a clock that re-anchors before, at, and after the head reaches the
  dispatcher, and a RED arm for each of steps 2–4.

### As built (2026-09-29)

- **`dvd/dvd_audio_decode.sv` "IN-BAND TIMELINE RE-TIME".** The detector compares a
  tagged head's PTS against the last tagged frame dispatched to play (`last_pts`).
  Backward by more than 50 ms or forward by more than 1 s trips it. It runs only with
  `sched_en`, and `last_pts` clears on reset and whenever `sched_en` is low. The hold
  (`disc_hold`) keeps S_IDLE from popping while `draining || play_pts_valid`. The
  existing underrun re-arm closes the gate when the old tail runs out. The head then
  dispatches with `cur_disc`, which sets `retime`. A `retime` arm releases only when
  `0 <= stc - play_pts < 0.5 s`.
- **Overlap trim (step 5, added while writing the bench).** If the old audio runs past
  its video, the display re-anchors before the tail ends. The new head then reads
  50–500 ms late on its own timeline. Playing it would leave the whole clip that late,
  because the mid-play catch-up acts only past 300 ms. So a discontinuity head that is late
  by more than `STALE` and less than the window is discarded (`head_retime_stale`). The
  new timeline resumes within 50 ms. A head late by 0.5 s or more means the clock is
  still on the other timeline, so it waits.
- **⚠ Step 4's premise was CORRECTED after HW round 1.** The premise was that a head late
  by `RETIME_WIN` or more means the clock is on the other timeline. It is not always true
  (ULTIMATE_T2, below). The discriminator is now the ARRIVALS. `arr_agree` is a two-sided
  test: the demux's newest audio sits within (−3 s, +50 ms] of the clock. The −3 s side
  covers the parse front's normal ~1.1–1.6 s lead. The old `arr_current` is one-sided and
  would read "current" for arrivals any distance ahead of the clock.
  - If the arrivals agree, a late head is stale on the clock's own timeline and is
    discarded at any lateness.
  - If they disagree, the clock is elsewhere and the head waits.
- **Step 6, a STALE LATCH.** A head latched while the clock was elsewhere can turn out
  `RETIME_WIN` or more late once the clock arrives, with the arrivals agreeing. Frames
  already dispatched cannot be dropped except by a reset. So the decoder raises
  `resync_req`. `emu.sv` routes it to `flush_ctl.aud_rephase_req` (the port was renamed
  from `disc_rephase`), which fires `aud_resync`. That is the old display-time reset, now
  only on the decoder's own evidence. `tools/check_aud_rephase_wiring.py` gates the seam.
- **Liveness.** `HOLD_W` (~0.62 s) force-re-arms a hold that never sees its underrun.
  A `retime` arm that never reaches its window takes the ordinary `arm_timer` fallback
  (2.5 s).
- **`dvd/flush_ctl.sv`:** `aud_resync` fires only on `aud_switch`. `disc_rephase` and
  `cell_seamless` stay as ports and are inert. `emu.sv`'s re-phase pulse and its 0.62 s
  cooldown are still wired, and now drive nothing.
- **New port `dbg_retime_cnt`:** scored by the bench, not in telemetry (word 5 is full).
  On the rig a re-time reads as one gate closure and one underrun re-arm, with **no**
  `aud_play` reset, and `play_err` lands at ~0.

**Gates.**
- `bench/dvd/run_aud_retime.sh --red`. `aud_retime_tb` S1–S6 score every unique LPCM
  sample that leaves the module, and the STC when it left:

  | scenario | result |
  |---|---|
  | S1 crossing at the old end | all kept, 112-clk gap |
  | S2 audio ends 150 ms before the crossing | silence, then the head exactly at its PTS |
  | S3 70 ms overlap | 5 frames trimmed, resumes 45 ms late |
  | S4 2 s forward gap | head held to its PTS |
  | S5 continuous control | no re-time, no re-arm, no gap |
  | S6 no re-anchor | fallback plays it |

  | S7 T2 shape: clock already new, head 0.6 s late, current audio behind it | head discarded, lands 45 ms late, no fallback, no reset |
  | S8 stale latch: latched on the old clock, 0.7 s late when the clock arrives | exactly one `resync_req`, fresh audio in phase |

  S1–S7 must raise no `resync_req`. The bench models the demux arrival front (frames are
  published only after `arr_pts` moves, as in the core) and the reset `resync_req` causes.
  RED arms: detector off, hold off, window off, overlap trim off, an over-triggering
  detector, agree-off (fails S7: the T2 regression), and resync-off (fails S8). Each fails
  its own scenario. The wiring check has two RED mutations: the display pulse wired back
  in, and the request dropped from the trigger.
- The same runner reruns `dvd_audio_decode_tb` and `flush_ctl_tb`.
- `run_seamless_audio.sh --red`: its RED arm now restores the display-time re-phase,
  and `flush_ctl_tb` [10b]–[10d] catch it.
- **Two old stimuli changed:** `dvd_audio_decode_tb` C6 and C9 fed a PTS *backward* from
  the frame just played. That is a discontinuity now, which is a different claim. They
  are made monotonic, as a real late backlog is.

**Known limitations.**
- **A display crossing more than 2.5 s after its audio ends** releases early through the
  `arm_timer` fallback. The old flush path had the same bound.
- **An overlap resumes up to 50 ms late** (`STALE_TICKS`), inside ordinary lip-sync
  tolerance.
- **The Matrix white-rabbit cells now take a hold** where the carve-out gave none. The
  expected gap is ~0: the soundtrack is continuous there, so the old audio ends as the
  picture crosses. HW must confirm it.
- **Forward steps under 1 s are not re-timed.** The old display re-phase was backward-only
  too, so nothing regresses there.

### HW round 1 (2026-09-29, build `DVD_nsaudio_20260929_1726`, SEED 9, 90.48/88.25 MHz)

**Thayer VTS_08 (20 ms capture `.sim/nsaudio/fast_fix_1.jsonl`, `join_loss.py`):**

| | before | after |
|---|---|---|
| audio lost per join, ±1.5 s | 0.8–2.3 s | **0.0 ms** at 9 of 10 joins, 29.7 ms at the first |
| spurious −66.8 ms anchors | 6 | 0 |
| `play_err` after a join | 0, or −1.3…−1.4 s | 0–3 ms |
| ring at a join | flushed | 28–34 frames throughout |

(The "AUD_RESET" rows every 21.8 s in `events.py` output are the 16-bit `aud_play`
counter wrapping, not resets.)

**⚠ REGRESSION FOUND: ULTIMATE_T2, boot → menu (control arm = `DVD_pictime`, same launch).**
The first menu segment follows a 4.5 s still (`flags.still`). Old build: audio released at
21.94 s against the old clock (`play_err` +4574), then the display's backward re-anchor at
21.985 s fired `aud_resync`, and the fresh audio played **in sync from 22.15 s**. New build:
the display re-anchored at 21.96 s. The first new-segment frame to reach the head arrived
**0.53 s LATE against that already-new clock** (`av_drift` −530 at 22.56 s), with ~0.8 s of
newer, current audio queued behind it. It was treated as "late by ≥ 0.5 s, so the clock must
be on the other timeline", held, and released by the `arm_timer` fallback at 24.42 s. It then
played **2.5 s late** until the next still. (The 50 s of silence afterwards is on both builds:
a silent still menu.)

**Root cause:** step 4's premise, "late by ≥ RETIME_WIN ⇒ the clock is on the other
timeline", is false when the audio is simply behind on the same timeline. The old flush
masked that case by discarding the stale head. **Fix:** use the discriminator the decoder
already has for the mid-play catch-up, `arr_current` (the demux's newest arrival is at or
ahead of the clock):
- If the arrivals are current, a late head is stale on the current timeline: discard it at
  dispatch, and if one is already latched, release it so the existing catch-up discards the
  backlog.
- If the arrivals are also far behind the clock, the clock is on the other timeline: hold.
  That covers Thayer, where the arrivals are new-timeline and the clock is still old.

Needs a bench scenario (S7: the clock already new, the head late > WIN, arrivals current).

### HW round 2 (2026-09-29, second rig: SuperStation One; build `DVD_nsaudio_20260929_1855`, 93.57/89.69 MHz)

Control arm = `DVD_pictime` on the same box. Captures `.sim/nsaudio/ss_*.jsonl`.

| case | old build | round-2 build |
|---|---|---|
| Thayer VTS_08, 10 joins | 0.8–2.3 s lost per join | **0.0 ms** lost at every join, no resets |
| ULTIMATE_T2 boot → menu | in sync from 22.13 s | in sync from 22.82 s: the step-6 rescue fired at 22.57 s (it waited for the latch to be 0.5 s late) |
| The Matrix white-rabbit c4 / c5 / c7 | **0 ms lost**, `av_drift` +95…105 ms | **~190 ms gap, then ~0.2 s LATE** at c4 and c7; c5 fine |

**The Matrix regression** was the flagged risk. A `seamless_play` cell restarts its PTS
while the soundtrack runs on sample for sample. At c4 the new audio restarts ~1.1 s ahead
of the old clock, so it was held. The display then re-anchored forward to a picture
0.21 s past the head, so it released 0.21 s late. The author's audio-versus-video offset
at a seamless boundary is not a phase to honour.

**Fixes (round 3, sim-gated):**
- **Step 7: no re-time at an authored-seamless join.** The reader's `cell_seamless` is
  stamped per frame into `audio_ring`'s descriptor at the write side (53 bits now:
  `frame_seamless`), where it names the right cell. The old `flush_ctl` carve-out sampled
  it at display time, ~1 s later in content. A discontinuity head stamped seamless is
  just dispatched, sample-continuous.
- **The hold waits for the clock.** `disc_hold` now also holds while `!arr_agree`. A head
  dispatched while the clock was still elsewhere got latched, and a latch can only be
  undone by a reset. Waiting lets the dispatch-side trim see the true lateness. So T2 is
  trimmed at the crossing instead of rescued 0.6 s later, and audio that leads its first
  picture lands in phase (S10). Step 6 (`resync_req`) is kept only as a safety net for a
  second re-anchor after a correct latch (S8 reshaped to that). `HOLD_W` is a parameter
  now so the bench can shrink it.
- **Rejected on the way:** lowering step 6's threshold from 0.5 s to STALE. It would have
  sent every join whose audio leads its picture by 50–500 ms to the ~1 s reset, which is
  the original problem again.

## 4b. HW round (next)

1. `USE_DOCKER=1 ./build_release.sh --compile`. Ask the UMD and H264 sessions for a
   slot, and run `mister.py state` before deploying.
2. Thayer VTS_08 with the same launch and `--telem-fast-ms 20`, both fixes in. At every
   join expect:
   - one anchor, with no `-66.8` spurious anchor before it;
   - no `aud_play` reset;
   - a gap of tens of ms, not 1.3 s;
   - `play_err` ~0 after each join, never −1.3 s.
3. Regression:
   - The Matrix PGC 1 across a white-rabbit cell (listen for a click or gap);
   - T2 / MiB looping menus and Scooby-Doo 2's whac-a-mole clips (lip-sync and
     "good job");
   - a play-all TV disc across an episode join.
4. Capture-card audio (`audio_check.py`, or a plain capture) across one Thayer join,
   to confirm by ear what the telemetry says.

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
