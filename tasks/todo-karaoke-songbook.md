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

## Task 2: Pagination engine (needs Task 1)
— L — **Model: Opus 5** (highest-risk task — stateful port with a one-item-lookahead orphan rule; bugs here fail silently as a plausible-but-wrong PDF, not a crash)
- [ ] `_measure_lines()` via `PIL.ImageFont.truetype` against real `cour.ttf`/`courbd.ttf`, `×1.08` fudge as starting point
- [ ] `paginate()` — contentWidth/colWidth formula, flatten step, `advance()`/`forceNewPage()` state machine, one-item-lookahead orphan control, `firstPage`/`+3` TOC numbering
- [ ] Unit tests: letter forcing new page mid-column, artist+first-song orphan-pushed, artist-alone-taller-than-column skip path, TOC page numbers match `firstPage`
- [ ] Assert exact `pageNumber`/column contents, not just page count

## Task 3: HTML rendering + PDF export (needs Task 2)
— M — **Model: Sonnet 5** for `render_html()` (mechanical verbatim CSS/HTML port, tokens already catalogued) — **Model: Sonnet 5** for Chrome/Edge discovery + `render_pdf()` (new subprocess/error-handling logic)
- [ ] `render_html()` — full CSS token set (palette, fonts, cover grain/tape/vignette/poster-tilt, letter-banner zigzag clip-path, TOC 2-col `break-inside:avoid`, hang-indent song lines, page footer) per plan's catalogued tokens
- [ ] `@page`/print-size CSS reimplemented directly (816×1056px letter, insets) — no dependency on `doc-page.js`
- [ ] Chrome/Edge discovery (`shutil.which` + standard install paths) with a specific named exception if neither found
- [ ] `render_pdf()` — sibling `.html` kept, shell to headless print-to-pdf, temp-file + `os.replace`
- [ ] Unit tests: `render_html()` structural markers, `shutil.which`-mocked-`None` error path
- [ ] **Manual visual checkpoint**: real PDF from `sample-library.csv` vs `screenshots/01-cover.png`/`02-most-requested.png`/`03-song-list.png` — retune PIL fudge factor / confirm default colors here
- [ ] ▶ **Human checkpoint** — stop and show PDF/screenshot comparison before starting Task 5

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
