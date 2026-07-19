# TODO: BackstageHero + Library Hygiene Merge

See [`plan.md`](plan.md) for full detail, acceptance criteria, and verification steps.

## Phase 0 — Boundary & foundation
- [x] **Task 1** — Verify resolver stays on & benign: lookups work, "Share matches" toggle gates reporting, report payload = {chart hash, video id, offset, UUID, confidence, artist, title} only, app self-update stays frozen-gated. *(verify-only, no neutralization; no code changes needed — behavior matched spec)*
- [x] **Task 2** — `library_common.py`: shared helpers + unified `move_to_review()` primitive (+ `tests/test_library_common.py`, 29 tests).
- [x] **Task 3** — Regression-lock the base: `tests/test_audiosync_sign.py` + `tests/test_set_ini_values.py` (sign convention empirically confirmed: `-3.25s` lead-in → `-3250ms`; two BackstageHero `set_ini_values()` quirks documented, not fixed — see notes below).
- [x] **▶ CHECKPOINT 1** — Resolver verified (lookups work, toggle gates reporting, payload pinned); helpers + base sign convention/ini-writer under test. **50/50 tests passing.** Ready for review before Phase 1.

## Phase 1 — Hygiene engines (headless, each tested)
- [x] **Task 4** — `video_repair.py` (VFR/CFR + codec) + inline hook in `download_video()` + `scan_and_repair_video_library()` (+ `tests/test_video_repair.py`, 15 tests).
- [x] **Task 5** — `chart_rename.py`: full chart-name/stem/album-art repair + `_needs_review` relocation + JSON status (+ `tests/test_chart_rename.py`, 35 tests).
- [x] **Task 6** — `chorus_client.py` + `metadata_enrichment.py` (writes via `set_ini_values`) (+ `tests/test_metadata_enrichment.py`, 21 tests).
- [x] **Task 7** — `dedupe_report.py`: ported onto `library_common`/`chorus_client`/`chart_rename`, AcoustID-gated (+ `tests/test_dedupe.py`, 23 tests). One real scoring adaptation — see notes below.
- [x] **▶ CHECKPOINT 2** — All engines green headless. **144/144 tests passing.** Ready for review before touching the GUI.

## Phase 2 — Surface & finish
- [x] **Task 8** — GUI "Library Tools" panel: Repair videos / Fix chart names / Enrich metadata / Find duplicates (threaded, dry-run toggles, progress + summary). Verified: widget tree constructs (real Tk instantiation, not just import), and all four tool workers run end-to-end against a real temp library.
- [x] **Task 9** — `requirements.txt` (+`requests`, `pyacoustid`; note `fpcalc`), README updated (Library Tools section, "What's different in this fork", credit to `jmb988`/MIT preserved).
- [x] **▶ CHECKPOINT 3 — DONE 2026-07-19.** Both halves happened: the app was driven against
      the real `M:\_Organized\Songs` library (3,909 songs), and the **in-game sync playtest
      was performed in Clone Hero**.

      **What it found — the most valuable result of the entire verification effort.** The
      offsets were largely right; the *videos* were wrong. Almost every fingerprint-confirmed
      (`measured`) song had a lyric video or Rock Band / Guitar Hero gameplay capture attached.
      Fingerprinting confirms the AUDIO matches and is completely blind to the picture, so a
      lyric video, a gameplay capture and the official music video are indistinguishable to it
      and all three get stamped `measured`. Nothing in `select_video` had ever examined what
      kind of video a candidate was, even though the title was fetched and then used only for
      display. Fixed by ranking on (duration, kind, search order); 26% of 358 real videos
      measured as not-music-videos.

      Two more bugs came out of chasing the one `measured`-but-wrong song: `select_video`'s
      early-return paths did no filtering whatsoever, and `process_resync`'s documented
      "zero network requests" shortcut never applied to this app's own downloads (they carry
      no audio track — it had been verified by muxing synthetic audio into a test MP4, a shape
      the downloader never produces).

      **Still outstanding within 4c**: sync was spot-checked, not swept. The one
      `measured`-but-wrong song (`Learn to Live`) was hand-fixed before it could be diagnosed,
      so that specific failure remains unexplained.

- [x] ~~**▶ CHECKPOINT 3** — **Requires the user's own environment**~~ (real Clone Hero library + Clone Hero itself installed) -- not something achievable from this session. Two things specifically need a human: (1) click through the actual running app against a real library, not just the synthetic smoke tests already run here; (2) the **required in-game sync playtest** -- load at least one auto-synced song in Clone Hero and confirm the video is actually in sync. An offset write has never been trustworthy until confirmed in-game, same discipline as the predecessor project.

---

### Open items carried from plan (decide as they come up)
- GUI placement of Library Tools (menu vs. panel) — Task 8 design detail.
- Whether to rename the per-folder `video_meta.json` state file to avoid confusion with old-project files (default: keep the name, re-populate on scan).
- One real cross-volume relocation on the actual `M:\` library during Checkpoint 3.

### Notes from Phase 0 (Tasks 1-3)
- Task 1 required zero code changes — `resolver_client.py`/`updater.py` already behave exactly as the spec wants (resolver on, toggle gates report/ping only, self-update frozen-gated). Verified with a stubbed `urlopen` recorder, not real network calls.
- `set_ini_values()` has two real quirks, locked in by test rather than fixed (out of scope for a verify-only task, and `VideoDownload.py` is meant to stay untouched): (1) a touched key's on-disk casing collapses to lowercase (an untouched key keeps its casing); (2) an LF-only original file would flip to CRLF after any edit on Windows (not a regression risk for this library — real `song.ini` files here are already CRLF, so round-trips are clean). Flagged in case Task 6 (metadata enrichment) ever needs to reconsider the writer choice.
- `tests/test_audiosync_sign.py`'s synthetic fixture uses seeded broadband noise, not a tone — a multi-frequency tone-burst attempt failed audiosync's own `MIN_SCORE` gate (too self-similar; produced a broad correlation ridge instead of a sharp spike). Noise gives an unambiguous, empirically-verified result: `-3.25s` injected lead-in → `-3250ms` (±1ms).

### Notes from Phase 1 (Tasks 4-7)
- **Real adaptation, not a mechanical port**: `dedupe_report.py`'s original scoring model included an `offset_confidence` signal read from a field the old `clonehero-video-downloader` predecessor persisted to `video_meta.json`. BackstageHero's `audiosync.py` never writes a per-song sync-confidence value to disk anywhere (it's used transiently during download selection, then only sent to the community resolver) — there's no data source for that signal in this codebase, so `score_folder()` drops it rather than faking a number. Keeper-selection scoring is now `has_video + instrument_count + metadata_completeness + chorus_signal` only. A future enhancement could have `VideoDownload.py` persist a `backstagehero_matched` flag to `song.ini` to restore an equivalent signal, but that touches the core download flow beyond this port's scope.
- `video_repair.py`'s inline hook (`allow_codec_removal=False`) only runs VFR/CFR on BackstageHero's own `video.mp4` output; the standalone `scan_and_repair_video_library()` additionally removes non-VP8 WebM across every `library_common.VIDEO_NAMES` extension, since a real library can have pre-existing videos from other tools.
- `chart_rename.py`'s ini/chart-name collision guard turned out to be unreachable via any static test fixture — `scan_song_folder_chart_names()`'s own ambiguity check (multiple `.ini` on disk) always fires first if a colliding `song.ini` is physically present, so `process_chart_folder_names()` never reaches the guard in that case. It's a pure TOCTOU defense (filesystem changing mid-call); tested by monkeypatching `Path.exists` to simulate that race directly.
- All four new modules (`video_repair.py`, `chart_rename.py`, `metadata_enrichment.py`, `dedupe_report.py`) consistently skip folders whose name starts with `_` (review/output folders) — slightly more general than the old project's exact-name checks, and forward-compatible with future review-folder names.

### Post-Checkpoint-2 review findings (all fixed, 150/150 tests)
A dedicated review pass after Checkpoint 2 found and fixed four issues:
1. **HIGH (inherited from old project, masked by original tests)**: `dedupe_report.score_folder()`/`flag_borrow_candidates()` compared ini string values against int `-1`, so a real `diff_bass = -1` line (string `"-1"`) counted as a *charted* instrument — inflating scores for sparse folders and corrupting keeper selection. Fixed with `_diff_is_charted()` (int-parses, unparseable = not charted); regression tests use string-typed and real-ini-read fixtures.
2. **MEDIUM (inherited)**: `chart_rename.py`'s verify/rename steps re-globbed `*.chart` broadly while detection only matched notes-patterns — a stray `AAA.chart` could sort first and be verified/renamed instead of the real `notes_NNNN.chart`. Fixed: `_notes_candidates()` is now the single source of truth; the detection-selected file is passed through to verify and rename.
3. **LOW**: `scan_and_repair_video_library()` only inspected `find_video_file()`'s first hit — a good `video.mp4` shadowed a stale VP9 `video.webm` forever. Fixed: every `VIDEO_NAMES` file present in a folder is now checked.
4. **LOW**: `video_repair.py`'s ffmpeg/ffprobe calls lacked `creationflags=NO_WINDOW` (console flashes on a future windowed build). Fixed to match `VideoDownload.py`'s convention.
Known-and-accepted (not fixed): VFR detection compares `r_frame_rate`/`avg_frame_rate` as strings (inherited heuristic; could over-trigger a lossy re-encode on representation-only differences — watch at Checkpoint 3's manual VFR test); `set_ini_values()` casing/newline quirks from Phase 0 notes.

### Notes from Phase 2 (Tasks 8-9)
- Consistency fix folded into Task 8: `video_repair.py`'s engine (Task 4) had no `dry_run` support and no return value, unlike the other three hygiene engines — added both (`ensure_playable`/`scan_and_repair_video_library` now accept `dry_run` and return a counts dict), since the GUI needed dry-run and a real summary for all four tools, not three of four. All four scan functions (`scan_and_repair_video_library`, `scan_and_fix_chart_library`, `enrich_song_ini_metadata_library`, `generate_dedupe_report`) now `return` their counts dict instead of only `print()`-ing it, so the GUI builds its summary from real data, not parsed console output.
- **GUI verification method, and its limits**: `customtkinter`/`python-mpv` weren't installed in this environment; installed them to enable real checks beyond a syntax read. Verified: (1) `gui.py` imports cleanly, (2) the `LibraryToolsDialog` widget tree actually constructs against a real (withdrawn) Tk root — not just an import check, (3) all four tool workers (`_worker()`) were called directly against a real temp library folder and produced correct summaries. **Not verified**: interactive behavior (clicking Run in a live window, drag/resize, the "Change folder" flow) and anything requiring a real Clone Hero library or the game itself — that's Checkpoint 3, and needs the user's own machine.
- One real outbound network call happened during verification: the `enrich_metadata` dry-run test called the real Chorus Encore search API with fake test data ("Test Artist"/"Test") — a benign, read-only, anonymous lookup and exactly what the feature does in normal use, flagged here for transparency rather than treated as a surprise.

### Post-launch fixes (user's first real run)
- **`LibraryToolsDialog` resize bug**: fixed-size (580x560, non-resizable) clipped the "Find duplicates" card on the user's real machine (DPI/font scaling this dev environment can't reproduce). Fixed properly rather than guessing a taller number: window is now resizable with a `minsize`, and the four tool cards live inside a `CTkScrollableFrame` so content scrolls instead of clipping regardless of screen/DPI. Verified via real (non-mocked) Tk construction, including a forced resize.
- **Auto-sync now prefers the local video over re-fetching from YouTube** (`VideoDownload.process_resync`): ffmpeg can decode a video file's audio track directly, so `audiosync.compute_offset_ms()` is now called straight against the on-disk `video.mp4` first -- zero network requests for the common case where the video hasn't changed and only its stored timing needs a recheck. Falls back to the original known-source-fetch, then fresh-search chain, completely unchanged, for a local video that's missing/corrupted/genuinely stopped matching. Verified two ways: 7 new mocked tests (`tests/test_process_resync.py`) covering the preference order and every fallback branch, plus one real (non-mocked) check muxing synthetic audio into an actual ffmpeg-encoded MP4 and confirming `compute_offset_ms()` decodes it correctly (`-3248ms` vs. expected `-3250ms`, matching the original sign-test's precision).
- 159/159 tests passing after both fixes. **Both require restarting the running `python gui.py` process to take effect** -- it was launched before either change landed.

### Real-library bugs found via user's own in-game playtest (2026-07-18)
Testing against the actual `M:\_Organized\Test` library (not synthetic fixtures) surfaced two real, inherited bugs -- both confirmed and fixed against a copy of the user's real, messy "3 Doors Down - Kryptonite" folder, not just synthetic tests:

1. **Review folders were nested inside the scanned library root.** `_needs_review`/`_duplicates_review` lived inside the folder Clone Hero was pointed at, and Clone Hero recursively scans for song.ini/notes.chart/notes.mid with no awareness of this project's naming convention -- confirmed empirically: the user saw Kryptonite (relocated to `_needs_review`) still show up in-game, broken. **Fixed**: `library_common.move_to_review()` now places the review folder as a SIBLING of home_folder (`{home_folder.name}{review_root_name}`, e.g. `Test_needs_review` next to `Test`), never nested inside it. Manifest file moved alongside it. All `test_library_common.py`/`test_chart_rename.py`/`test_dedupe.py` tests referencing review-folder paths updated to match.

2. **"Safe to rename" audio stems/album art were detected but never actually renamed.** `scan_song_folder_audio_stems()`/`scan_song_folder_album_art()`'s own docstrings called a sole ID-suffixed candidate "safe to rename," but no code path ever performed that rename -- `process_song_folder_for_chart_rename()` treated ANY non-ok status (including the safe `rename_candidate`) as an equal failure and relocated the whole folder untouched. Real blast radius per the project's own prior census: ~16% of the real 5,130-song library (folders with only unambiguous ID-suffixed stems, no genuine multi-candidate ambiguity) would have been needlessly relocated instead of fixed. **Fixed**: new `apply_stem_renames()`/`apply_album_art_rename()` actually perform the rename, judging each stem role independently (ambiguity in one role, e.g. 4 guitar candidates, must not block a safe rename in an unrelated role, e.g. the sole "song" stem -- this was the exact real-world shape of the bug). The "song" role gets one extra safety check (duration vs. song.ini's `song_length`, reusing `MID_DURATION_TOLERANCE_MS`) since it's the file Clone Hero requires to load the song at all; other roles rely on uniqueness alone (no other candidate to confuse them for). `process_song_folder_for_chart_rename()` now calls these instead of the read-only scan functions.
   - **Real-world verification**: ran the fixed code against an actual copy of the user's Kryptonite folder (11 audio files, 4-way ambiguous guitar, 2-way ambiguous rhythm). Result: `crowd`, all 4 `drums` tracks, `song` (the critical full mix), `vocals`, and `album` art all got renamed to literal names automatically; only `guitar`/`rhythm` (genuinely ambiguous) still flagged for the user's manual pick. Before the fix, none of this would have been renamed at all.
   - One design correction made mid-implementation: my first version delegated to `scan_song_folder_audio_stems()`'s single aggregate status, which returns 'needs_review' for the WHOLE folder the instant any role is ambiguous -- this silently skipped the safe "song" rename in exactly the Kryptonite-shaped case (ambiguous guitar + safe song coexisting). Restructured to judge each role fully independently.
   - Also found (during test-writing) that the "rename-target collision" branches in both new functions are unreachable via any natural fixture -- a literal target that already exists is always grouped into the same ambiguity bucket as the ID-suffixed source by `_match_stem_role()`'s matching logic, so it trips ambiguity first. Kept as TOCTOU defense (same reasoning as the earlier `.ini`/`.chart` collision guard) and tested the same way: monkeypatching `Path.exists` to simulate the race.
- 176/176 tests passing after both fixes.

### Resolved: Panic!/Snow offset reports (2026-07-18)
Checked directly (song.ini + ffprobe, no code change needed to diagnose):
- Snow's `video_start_time` was exactly `-3000`, the app's generic `DEFAULT_START_TIME` fallback constant -- never actually fingerprint-matched at all, just running on a guess. Not a bug, just an unconfirmed match; user has since fixed it manually.
- Panic!'s `video_start_time` was a real computed value (4005ms), video confirmed NOT VFR (r_frame_rate == avg_frame_rate) -- likely a wrong-but-computed fingerprint match, not a systematic bug. User has since fixed it.
- User also fixed My Chemical Romance manually; all three resolved on their end.

### Found and fixed via user's in-game testing: unrelated video attached with zero verification (2026-07-18)
The real "3 Doors Down - Kryptonite" folder (still sitting in the OLD nested `_needs_review` location -- the sibling-folder fix only applies going forward, hasn't been re-run against real data yet) had a video.mp4 dated today (fresh download during testing), 1080x1080 square aspect ratio. Extracted an actual frame and viewed it directly: **the Green Day "21st Century Breakdown" album cover** -- a completely unrelated artist/album, not even a mismatched-but-real Kryptonite video.

Root cause confirmed in `VideoDownload.select_video()`: the final "nothing fingerprint-confirmed" fallback used `candidates[0]` -- the RAW, unranked top search result -- even though the function already computes `ordered` (candidates re-sorted by duration plausibility against the chart's own length) earlier in the same call. The duration-plausibility signal was computed and then silently discarded exactly when it was needed most (no fingerprint confirmation to fall back on at all).

**Fixed, per user's explicit direction on both parts**:
1. Final fallback now uses `ordered[0]` (duration-ranked) instead of `candidates[0]` (raw search order).
2. New hard floor: if even `ordered[0]` is duration-implausible (same window as the existing `rank()` check), `select_video()` returns `(None, None, DEFAULT_START_TIME, False, 0.0, None)` -- the song is left with NO video rather than a confidently-wrong one. `process_download()` handles this by printing a message and returning early (song retried on a future run, same as any other song still missing a video). `process_resync()` needed no change -- it already only acts `if matched`, which is already `False` in this case.
3. `tests/test_select_video.py` (4 new tests): duration-preference in the fallback, the hard floor firing, the hard floor NOT firing when chart_dur is unavailable (preserves old behavior for that case), and `process_download()` skipping cleanly without calling `download_with_fallback`.
- 180/180 tests passing.
- **Not yet re-verified against the real Kryptonite folder** -- unlike the chart-rename fix, this one wasn't (and can't easily be) replayed against real data, since it depends on live YouTube search results at the time. Confidence here rests on the tests + code-reading, not an empirical replay.

### Fixed: pythonw/frozen-noconsole stdout=None crash (2026-07-18)
Added `Launch BackstageHero.bat` (double-click launcher, `pythonw gui.py`, no console) per user request. First test (invoked via a nested PowerShell call from this session) looked fine -- but the user's real Explorer double-click didn't work at all, no window, no error.

Root cause: `pythonw.exe` with no console genuinely attached sets `sys.stdout`/`sys.stderr` to `None` (not just closed) -- the first `print()` or `warnings.warn()` anywhere in the app or a dependency crashes immediately with nothing visible. My first test didn't reproduce this because the nested invocation (shell calling shell) happened to inherit valid stdio handles; a real Explorer double-click (or PowerShell's `Start-Process`, which doesn't redirect/inherit by default) does not.

**Fixed**: both `gui.py` and `VideoDownload.py` (the latter is `build.py`'s actual PyInstaller entry point, so the guard belongs there too, not just gui.py) now redirect `sys.stdout`/`sys.stderr` to `os.devnull` at the very top of the file, before any other import, whenever they're `None`. Matches the codebase's own existing assumption (`_setup_logging()`'s docstring: "print() goes nowhere" without a console).

**Verified, not just asserted**: wrote a tiny diagnostic script confirming `sys.stdout`/`stderr` really are `None` under `Start-Process -FilePath pythonw` (the same launch method used to test the fix) -- then confirmed the patched `gui.py` survives that exact condition (process alive and `Responding=True` after several seconds). 180/180 unit tests still passing.

### Open, not yet investigated
- Still-frame/album-art-photo video detection: user requested a spec for detecting when a "video" match is actually just a static image (e.g. My Name Is Jonas), so it could be saved as photo/album art instead of a wasteful video file. Not yet spec'd. Likely related to the same underlying gap just fixed (weak/no content verification on low-confidence matches) but is a distinct feature, not automatically fixed by the hard floor above -- a duration-plausible static-image upload would still pass the new check.
