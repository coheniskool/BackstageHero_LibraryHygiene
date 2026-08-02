# TODO: Karaoke Songbook Generator

See [`plan-karaoke-songbook.md`](plan-karaoke-songbook.md) for full detail, exact algorithm/constants, and CSS tokens. Spec: [`../SPEC-karaoke-songbook.md`](../SPEC-karaoke-songbook.md).

## Task 1: Data pipeline (parse, dedupe, sort, bucket, TOC stats) — ✅ DONE 2026-08-02
— M — **Model: Sonnet 5** (new dedup/sort/stats logic, real correctness edge cases, but fully spec'd)
- [x] New `songbook.py`: `parse_entries(songs_or_folder)` — duck-typed in-memory Song list OR raw folder path via `library_common.iter_song_folders` + `VideoDownload.read_metadata`
- [x] `dedupe_and_sort(entries)` — case-insensitive artist-casing merge + pair dedup, quote-stripped alphabetical sort
- [x] `bucket_by_letter(sorted_entries)` — A-Z + `#` grouping (bucket letter = first *alphanumeric* char, so e.g. `!I!` buckets under I, not `#`; `#` is reserved for a leading digit or no alphanumeric char at all, and always renders last regardless of raw sort position)
- [x] `compute_stats_and_toc(buckets)` — totalSongs/totalArtists/mean/stdev/threshold (population stdev), alphabetical TOC
- [x] New `tests/test_songbook.py` — 15 tests: real-fixture `song.ini` files via `tmp_path` (matches `test_chart_rename.py`'s style) + synthetic `#`/threshold cases; asserts exact numbers, not just "doesn't crash"
- [x] `pytest tests/test_songbook.py -v` — 15/15 passed
- [x] `pytest tests/ -v` full suite — 599 passed, 1 pre-existing skip, no regressions
- [x] Checkpoint: ran against the full 7,687-row `sample-library.csv` — **7685 rows parsed (2 skipped, missing artist/title) → 1839 unique artists, 7562 total songs, mean=4.112, stdev=13.053, threshold=23.691, 54 "Most Requested" artists.** These numbers match `songbook-data.js`'s reference stats (`totalArtists:1839, mean:4.11, stdev:13.05, threshold:23.69`) almost exactly — confirms the population-stdev choice (an open question in the plan) was the right one, and validates dedupe/bucket/stats end-to-end against real data before Task 2 builds on top.

## Task 2: Pagination engine (needs Task 1) — ✅ DONE 2026-08-02
— L — **Model: Opus 5** (highest-risk task — stateful port with a one-item-lookahead orphan rule; bugs here fail silently as a plausible-but-wrong PDF, not a crash)
- [x] `_measure_lines()`/`_measure_width()` via `PIL.ImageFont.truetype` against real `cour.ttf`/`courbd.ttf` — **naive PIL measurement was found to be ~11% high** (FreeType rounds each glyph advance to a whole pixel; Courier New's true canvas advance is 0.6em/char, e.g. 6.3px at 10.5px, but PIL alone gives 7.0px). Fixed by measuring at 100x the target size and dividing back down (`_MEASURE_SCALE=100`): at that scale both fonts used here (10.5px/12.5px) land on whole-pixel advances internally, so FreeType's rounding vanishes — verified at **zero error across 3000 real titles** from `sample-library.csv`. The reference's `×1.08` fudge factor is therefore kept unchanged (`_MEASURE_FUDGE`), since our measurement now equals canvas's.
- [x] `paginate()` — contentWidth/colWidth formula, flatten step, `advance()`/`forceNewPage()` state machine, one-item-lookahead orphan control, `firstPage`/`+3` TOC numbering — ported with an injectable `measure` callable so the state machine is tested independent of font availability
- [x] Reconciled the `bindingMargin` default discrepancy flagged in the plan: `_paginate()`'s own `0.85` fallback would measure against a column ~1px narrower than what `renderVals()` actually renders at `0.9` — used `0.9` as canonical (`DEFAULT_BINDING_MARGIN`)
- [x] 14 new tests (29 total in the file): column-width math, letter-forces-new-page (not just column), first-letter-never-emits-a-blank-leading-page, orphan-control-moves-the-pair, orphan-control-skipped-when-pair-cannot-fit-any-column, advance-rolls-to-a-new-page, TOC-page-numbers-computed-during-pagination, TOC-entry-with-no-matching-artist, empty-library, determinism (same input twice → identical output), plus real-font measurement tests (skip cleanly if Courier New isn't installed)
- [x] `pytest tests/test_songbook.py -v` — 29/29 passed; `pytest tests/ -v` full suite — 613 passed, 1 pre-existing skip, no regressions
- [x] **Verified against the reference's own oracle**, not just internal tests: transcribed all 54 artist/page/count rows from `screenshots/02-most-requested.png` (the actual rendered output of the original JS on this exact sample library) and diffed the port's output against them. Threshold matched exactly (23.69). **49/54 TOC page numbers matched exactly**; the other 5 (Led Zeppelin, My Chemical Romance, Thin Lizzy, Weezer, "Weird Al" Yankovic) were off by ±1. Traced this to the bundled `sample-library.csv` being a **different snapshot of the library than whatever produced the screenshot** — spot-checked raw CSV rows directly (e.g. My Chemical Romance: 31 unique titles in this CSV vs 51 shown in the screenshot; Weezer 28 vs 26; Led Zeppelin 25 vs 25, matches) — real per-artist count drift between two exports, not a pagination bug. Aggregate stats coincidentally landing almost identically (Task 1's checkpoint) masked this until checked at the individual-artist level.

## Task 3: HTML rendering + PDF export (needs Task 2) — ✅ DONE 2026-08-02
— M — **Model: Sonnet 5** for `render_html()` (mechanical verbatim CSS/HTML port, tokens already catalogued) — **Model: Sonnet 5** for Chrome/Edge discovery + `render_pdf()` (new subprocess/error-handling logic)
- [x] `render_html()` — full CSS token set (palette, fonts, cover grain/tape/vignette/poster-tilt, letter-banner zigzag clip-path, TOC 2-col `break-inside:avoid`, hang-indent song lines, page footer) per plan's catalogued tokens. All dynamic text (artist/title/letter/TOC name) is `html.escape()`d — real library data contains literal `<i>`/etc in titles, and pagination measured those as plain characters, so rendering must treat them as literal text too or the visible wrap point would desync from the computed one.
- [x] `@page`/print-size CSS reimplemented directly (816×1056px US Letter, `break-after:page` + legacy `page-break-after`) — no dependency on `doc-page.js`
- [x] Chrome/Edge discovery (`shutil.which` + standard install paths) with `BrowserNotFoundError` naming the specific cause if neither found
- [x] `render_pdf()` — sibling `.html` kept (manual-print fallback), shells to headless `--print-to-pdf`, temp-file + `os.replace` for both outputs
- [x] 11 new tests (40 total in the file): `render_html()` structural markers, color substitution, HTML-escaping, cover/TOC/content page presence; browser discovery (which-first, Edge fallback, specific error when neither found); `render_pdf()` with a mocked `subprocess.run`
- [x] `pytest tests/test_songbook.py -v` — 40/40 passed; `pytest tests/ -v` full suite — 624 passed, 1 pre-existing skip, no regressions
- [x] **Manual visual checkpoint, completed**: generated a real 84-page PDF from the full `sample-library.csv` via installed Chrome, plus standalone cover/TOC/content-page screenshots for a direct side-by-side against `screenshots/01-cover.png`/`02-most-requested.png`/`03-song-list.png`. Typography, layout, letter-banner zigzag, hang-indent wrap, and TOC styling all matched closely.
  - **Real bug found and fixed**: the reference source's own defaults are self-inconsistent (`renderVals()`'s `??` fallback says accent=`#8C2727`/cover=`#6B8E23`, but the DC props schema declares accent=`#3B5998`/cover=`#8C2727`). Task 2 had picked `renderVals()`'s fallback as canonical — **wrong**. The actual screenshots are red-cover/blue-accent, matching the **props schema** defaults, not `renderVals()`'s. Fixed: `DEFAULT_ACCENT_COLOR='#3B5998'`, `DEFAULT_COVER_COLOR='#8C2727'`. This is exactly the class of bug the checkpoint exists to catch — it wouldn't have been caught by any unit test, only by the visual diff against a real reference render.
  - **Known, undeliverable gap, not a bug**: the cover markup references `./assets/collage-stickers.png` (visible as a punk-sticker collage in `screenshots/01-cover.png`'s middle third) — no `assets/` folder exists anywhere in the handoff bundle, so this asset was never delivered. Left blank rather than fabricated; flagged to Aaron directly, since closing this gap needs either the original asset or a fresh piece of art, not an implementation decision.
  - PIL measurement fidelity (Task 2's concern) and `bindingMargin=0.9` both held up under the real-PDF render — no retuning needed.

## Task 4: Orchestrator + optional CLI entry (needs Task 3)
— S — **Model: Haiku 4.5** (thin wrapper chaining already-built functions, CLI mirrors an existing pattern)
- [ ] `generate_songbook(songs_folder, songs=None, ...)` — GUI path (passed-in list) vs CLI path (`songs=None` walks folder); writes `Clone Hero Songbook.pdf`+`.html`, returns result dict or raises
- [ ] `argparse` CLI (`--library-path`, `--columns`, `--binding-margin`, `--accent`, `--cover`, `--out`) mirroring `dedupe_report.py`'s flag style
- [ ] Cold-shell CLI run against a tmp folder built from `sample-library.csv`, no GUI import
- [ ] pytest covering `generate_songbook()` end-to-end (skip real-PDF assertion if no Chrome/Edge present)

## Task 5: GUI integration (needs Task 4)
— L — **Model: Sonnet 5** (new dialog + threading + settings wiring, modeled closely on `LibraryToolsDialog` but editing production `gui.py`; thread-marshalling mistakes surface subtly — an intermittent freeze or a stale status label, not a crash)
- [ ] `SongbookDialog(ctk.CTkToplevel)` in `gui.py` — same boilerplate as `LibraryToolsDialog` (grab_set, WM_DELETE_WINDOW, icon, `_center`); column-count/margin/accent/cover controls + status label + Generate button
- [ ] Generate button: disable controls → background thread → `songbook.generate_songbook()` → `self.after(0, ...)` finish (success: Open action; failure: specific red error e.g. missing Chrome)
- [ ] `App`: `songbook_options` in `self._settings`; `_songbook_options()`/`_on_songbook_option_change()` mirroring `_tool_dry_run_prefs`/`_on_tool_dry_run_change`
- [ ] `_open_songbook_dialog()` mirroring `_open_library_tools`'s busy-check pattern
- [ ] New "Generate Songbook" `CTkButton` in `folder_row` (insert before "Library Tools", shift grid columns), disabled per the `_songs_folder`/`_songs` guard
- [ ] Manual verify: `python gui.py`, scan real folder, full dialog flow, PDF lands and opens correctly

## Task 6: GUI dialog tests (needs Task 5)
— S — **Model: Sonnet 5** (mostly mechanical adaptation of an existing template, but the `_pump()` mainloop-polling assertion pattern is a known flakiness risk if the predicate is copied carelessly)
- [ ] New `tests/test_songbook_dialog.py` mirroring `tests/test_library_tools_dialog.py` (module-scoped withdrawn root fixture + Tk-unavailable skip, per-test dialog fixture monkeypatching `_asset_path`, `_pump()` helper, final no-dialog-instance test against `songbook.generate_songbook()`)
- [ ] `pytest tests/test_songbook_dialog.py -v` skips cleanly (not fails) on headless/no-Tk

## ▶ Checkpoint (final)
- [ ] `pytest tests/ -v` full suite green
- [ ] `python songbook.py --library-path <sample folder>` runs standalone, no GUI import
- [ ] `python gui.py` end-to-end: scan → Generate Songbook → settings → Generate → visual compare vs all 3 reference screenshots
- [ ] Re-run unchanged twice → identical page count and TOC page numbers (determinism)
- [ ] Hide Chrome and Edge → clear specific GUI error, not a crash or silent no-op
- [ ] Diff review: `songbook.py`, `gui.py`, `tests/test_songbook.py`, `tests/test_songbook_dialog.py`

---

### Notes
- Line numbers verified live against current code at plan time (2026-08-02) — re-verify at `/build` time if drifted.
- Tasks 1→2→3→4→5→6 are strictly sequential (each needs the previous) — this is a single linear port, not parallelizable work.
- Task 3's human checkpoint is deliberate: pagination/visual fidelity is far cheaper to fix before the GUI is built on top of it.
