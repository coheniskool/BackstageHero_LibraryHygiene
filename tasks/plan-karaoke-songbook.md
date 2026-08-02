# Plan: Karaoke Songbook Generator

See [`../SPEC-karaoke-songbook.md`](../SPEC-karaoke-songbook.md) for objective, tech stack, boundaries, and success criteria. See [`todo-karaoke-songbook.md`](todo-karaoke-songbook.md) for the checklist derived from this plan.

## Context

The spec ports a design-handoff HTML/JS prototype — a punk/grunge print-and-bind "karaoke songbook" generated from a Clone Hero library's Artist/Title data — into this app as a manual "Generate Songbook" button, reading directly from a completed library scan instead of requiring a separate CSV re-upload. Two research passes fully mapped both sides before this plan was written: the exact pagination algorithm in the reference prototype (`Clone Hero Songbook.dc.html`), and this codebase's exact conventions for settings persistence, dialog/thread patterns, module-dispatch patterns, and test style. Line numbers below were verified live at plan time (2026-08-02) — re-verify at build time if drifted.

## Key facts binding on implementation

**Reference algorithm** (`Clone Hero Songbook.dc.html` lines 120-196, in `C:\Users\aaron\Downloads\Karaoke Book Design System\design_handoff_karaoke_songbook\reference\`):
- Constants: `colCapacity=900`px/column, `gap=20`px, page width `816`px (8.5in@96dpi), non-binding padding `52.8`px (0.55in), `letterBannerHeight=78` (64+14), `artistLineHeight=17`/`artistMarginTop=9`, `songLineHeight=16`/`songPadLeft=25`, text-measure fudge `×1.08`.
- `contentWidth = 816 - bindingMargin*96 - 52.8`; `colWidth = floor((contentWidth - gap*(numCols-1)) / numCols)`.
- Flatten `{letters:[{letter,artists:[{name,songs[]}]}]}` into one linear stream of `{type:'letter'|'artist'|'song', ...}` items.
- `forceNewPage()` before every letter item (page break, not column break; no-op if already at a fresh page/col0). `advance()` = column break (rolls to next page after the last column).
- Orphan control: when placing an `artist` item, peek at the next flattened item; if it's that artist's first `song` and `artistHeight+songHeight <= colCapacity` but `> remaining`, force `advance()` before placing the artist (pair moves together). Skipped (no special handling) if the pair could never fit one full column.
- General rule: any item (letter/artist/song) whose own height `> remaining` triggers `advance()`.
- TOC page numbers are computed *during* pagination (`firstPage[artistName] = pageIdx`, then `+3` for display) — never sourced elsewhere; the port must reproduce the exact flatten/measure/advance sequence to get matching numbers.
- Data contract (`songbook-data.js`): `{letters:[{letter,artists:[{name,songs:[string]}]}], toc:[{name,count}], stats:{totalSongs,totalArtists,mean,stdev,threshold}}`. `toc[].page` and all `pages[]` output are synthesized by pagination, not present in source data.
- CSS tokens (colors, fonts, cover/tape/grain treatment, letter-banner clip-path zigzag, TOC 2-col layout, page footer) are catalogued in full below in Task 3 — port verbatim into Python-generated HTML/CSS.
- **Known inconsistency in the source file, resolved not carried over**: `bindingMargin` fallback is `0.85` in `_paginate()` but `0.9` in `renderVals()`/props schema; `accentColor`/`coverColor` fallbacks likewise differ between `_paginate()`'s schema defaults and `renderVals()`'s runtime defaults. **Decision: use `renderVals()`'s runtime fallbacks as canonical** (`bindingMargin=0.9`, `accentColor=#8C2727`, `coverColor=#6B8E23`) since that function is what actually executes at render time and most plausibly matches the reference screenshots — verify against `screenshots/01-cover.png` during Task 3 and adjust if colors don't match.

**Full CSS/design tokens to port (Task 3):**
- Palette: `#2E2E2E` (page bg wrapper / letter-banner bg / TOC header border+text / page-number text), `#232120` (cover bg / cover-card text), `#E9E1D4` (paper/cream bg, title fill, badge fg), `#8C2727` (accent default), `#B5A642` (accent/cover alt), `#3B5998` (accent/cover alt), `#6B8E23` (cover default, olive), `#9c9584`/`#7a7468` (tape stripes), `#5a5550` (TOC subhead text), `#b8b0a0` (TOC dashed divider), `#4a453e` (song-line text).
- Fonts: `'Courier New', monospace` everywhere. Cover: 11px/700/ls3px/uppercase (kicker), 58px/700/lh0.88/uppercase (title), 15px/700/ls1px/uppercase (tagline badge), 15px/700 (stats row), 10px/700/ls0.5px/op0.75 (sync date). TOC: 34px/700/uppercase/ls1px (H2), 12px/700 (tag), 11px (subhead), 13px/700/uppercase (entry name), 11px/700 (page#/count badge). Content: 32px/700 (letter banner), 12.5px/700/uppercase (artist — matches `measureLines` font string exactly), 10.5px (song — matches too), 11px/700 (page-number footer).
- Cover treatment: base `background:#232120; color:#E9E1D4; padding:0.55in 0.55in 0.55in {bindingMarginIn}`; grain layer (3×3px radial-dot, opacity 0.6); diagonal scuff layer (repeating-linear-gradient 115deg, multiply blend); vignette blob (320×320px circle, radial-gradient maroon glow, blur); poster card (92%×90%, `background:{coverColor}`, `rotate(-0.6deg)`, box-shadow); two crosshatch texture overlays (0deg + 90deg repeating gradients); 3 tape strips (diagonal-hatch gradient `#9c9584`/`#7a7468`, rotated -30/24/-8deg); title "Clone Hero" filled `#E9E1D4` with hard drop-shadow `3px 3px 0 #232120`, "Songbook" filled `#232120` with `-webkit-text-stroke:2px #E9E1D4`; tagline badge (dark chip, rotate -1deg); stats footer with `border-top:3px dashed #232120`.
- Letter-divider banner: `background:#2E2E2E; color:#E9E1D4; height:64px; margin-bottom:14px; clip-path: polygon(0% 0%, 100% 0%, 100% 78%, 92% 100%, 84% 78%, 76% 100%, 68% 78%, 60% 100%, 52% 78%, 44% 100%, 36% 78%, 28% 100%, 20% 78%, 12% 100%, 4% 78%, 0% 100%)` (zigzag/torn-ticket bottom edge, 8 teeth).
- TOC page: bg `#E9E1D4`; header row with 5px solid `#2E2E2E` border-bottom; `column-count:2; column-gap:36px` entry list; each entry `display:flex; justify-content:space-between; border-bottom:1px dashed #b8b0a0; break-inside:avoid`; count shown as a pill/badge `background:{accentColor}`.
- Song-line hang-indent: `padding-left:25px; text-indent:-13px; line-height:1.5` on a `— {title}` line (dash sits in the outdented space).
- Page padding: cover/TOC `0.55in 0.55in 0.55in {bindingMarginIn}`; content pages `0.55in 0.55in 0.4in {bindingMarginIn}` (smaller bottom margin, room for footer).
- Page-number footer: centered, `font-size:11px; font-weight:700; color:#2E2E2E`, `pageNumber = pageIdx + 3`. No footer on cover/TOC (pages 1-2).
- Page-box dimensions to reimplement directly (replacing `doc-page.js`'s harness, which is pure browser-print machinery, not Clone-Hero-specific): US Letter `816×1056`px @ 96dpi.

**BackstageHero codebase conventions** (`gui.py`, `VideoDownload.py`, `library_common.py`, `updater.py`, `tests/`):
- Artist/title parsing: `VideoDownload.read_metadata(folder) -> (artist, title)` (`VideoDownload.py:525-529`), not `library_common`. Folder enumeration for the standalone/CLI path: `library_common.iter_song_folders(home_folder)` (`library_common.py:290-304`).
- `Song` dataclass lives in `gui.py:234-244` (`filename, folder, label, key, has_video, res, checked, status, stag`) — the in-memory list `self._songs: list[Song]` (`gui.py:1422`, populated in `_on_library_scanned`, `gui.py:1937-1938`) is what the GUI button passes in directly; no re-scan needed.
- Settings persistence: `_SETTINGS_FILE = os.path.join(updater.data_dir(), 'settings.json')` (`gui.py:133` → `%LOCALAPPDATA%\BackstageHero\settings.json`), `_load_settings()`/`_save_settings()` (`gui.py:136-149`, plain JSON, no atomic-write — matches "rarely-written UI prefs" precedent), `self._settings = _load_settings()` at startup (`gui.py:1462`), `App._persist_setting(key, value)` (`gui.py:1761-1763`). Model: `_tool_dry_run_prefs()`/`_on_tool_dry_run_change()` (`gui.py:1765-1777`) — dialog never touches `self._settings` directly, only via values-in/callback-out from `App`.
- Module-level dispatch pattern to mirror: `_run_library_tool(songs_folder, key, dry_run)` + `_format_tool_summary(key, counts, dry_run)` (`gui.py:1015-1075`) — plain functions outside any class specifically so a dialog-less caller (or here, a CLI) can invoke the same logic the GUI does.
- Dialog/thread pattern to mirror (`LibraryToolsDialog`, `gui.py:1078-1401`) — **not** the App's `self._queue`/`_poll_queue` mechanism (that's for the long-running download workers only): CTkToplevel boilerplate (`grab_set()`, `protocol('WM_DELETE_WINDOW', self._close)`, icon via `_asset_path('icon.ico')` guarded by `os.path.exists`, `self.after(50, self._center)`), then `_run_tool`→disable buttons+status label→`threading.Thread(target=self._worker, ...)`→worker computes then `self.after(0, lambda: self._finish(...))`, with `_notify_run_state` called un-marshalled (bare flag, GIL-atomic) so the main window knows a background op is running even if the dialog closes first.
- Header wiring: `folder_row` grid at `gui.py:1517-1530` currently has "Library Tools" (col 1) and "Change folder" (col 2), no gap column — new "Generate Songbook" button needs an explicit grid-column plan (insert before "Library Tools" so it becomes col 1, shifting the other two to 2/3 — keeps primary/most-clicked actions rightmost; minor UI call, not architectural).
- Guard for "nothing scanned yet": `if not self._songs_folder or not self._songs: return` (`_export_library_csv`, `gui.py:1972`) — reuse verbatim; `_open_library_tools` (`gui.py:2245-2262`) shows the "check `self._running`/`self._tool_running`, `messagebox.showinfo` if busy" pattern to copy for `_open_songbook_dialog`.
- Test conventions: pure-function tests (`tests/test_chart_rename.py` style — plain import, no GUI, `tmp_path` real files, no mocks) for `songbook.py`'s data/pagination functions; GUI-dialog tests (`tests/test_library_tools_dialog.py` style — module-scoped withdrawn `ctk.CTk()` root fixture with `pytest.skip` on Tk-unavailable, per-test dialog fixture monkeypatching `_asset_path`, a `_pump(root, seconds, until)` helper to assert on background-thread state changes, and a final test calling the module-level dispatch function with no dialog instance at all) for `SongbookDialog`.
- `conftest.py` just does `sys.path.insert(...)` for repo-root imports — no shared fixtures to reuse beyond that.

## Task Breakdown

Vertically sliced, dependency-ordered. No GUI work starts until the headless core is proven correct against real data.

### Task 1 — Data pipeline: parse, dedupe, sort, bucket, TOC stats
— M — **Model: Sonnet 5** (new logic with real correctness edge cases — case-insensitive dedup, quote-stripped sort, mean/stdev threshold math — but fully specified by the README, not open-ended design)
New `songbook.py` (GUI-import-free) + new `tests/test_songbook.py`.
- `parse_entries(songs_or_folder)` — duck-typed in-memory `Song`-like list (`.folder` attr) OR raw folder path (`library_common.iter_song_folders` + `VideoDownload.read_metadata` for CLI). Returns `list[(artist, title)]`, skipping rows missing either.
- `dedupe_and_sort(entries)` — case-insensitive artist-casing merge (first-seen display casing kept) + case-insensitive (artist,title) pair dedup; alphabetical sort stripping leading quote chars (`"`, `'`, curly quotes) before sort-key comparison; songs sorted case-insensitively within each artist.
- `bucket_by_letter(sorted_entries)` — A-Z + `#` grouping (first alphanumeric char of sort key, uppercased; digits → `#`).
- `compute_stats_and_toc(buckets)` — `totalSongs`, `totalArtists`, `mean`/`stdev` of songs-per-artist, `threshold = mean + 1.5*stdev`, `toc` = artists over threshold, sorted alphabetically (not by count).
**Verify**: fixtures from real `reference/sample-library.csv` rows (quoted fields, embedded commas/quotes, case-variant artists) + synthetic `#`-bucket/threshold cases; assert exact `stats` numbers, not just "doesn't crash." Checkpoint: run against the full 7,687-row CSV once, sanity-check `totalArtists`/`totalSongs`.

### Task 2 — Pagination engine (needs Task 1)
— L — **Model: Opus 5** (highest-risk task in this plan — a precise, stateful port of a page/column state machine with a one-item-lookahead orphan rule; an off-by-one here doesn't crash, it silently produces a wrong-but-plausible PDF with mismatched TOC page numbers, the exact "detection/algorithm logic" failure class this codebase has been burned by before)
Add to `songbook.py` + `tests/test_songbook.py`.
- `_measure_lines(text, font_path, font_size, max_width_px)` via `PIL.ImageFont.truetype(...).getlength(text)` against real `cour.ttf`/`courbd.ttf`, `×1.08` fudge as starting point (recalibrate in Task 3).
- `paginate(buckets, toc, column_count=3, binding_margin=0.9)` — port `contentWidth`/`colWidth`, flatten step, `advance()`/`forceNewPage()` state machine, one-item-lookahead orphan control, `firstPage`/`+3` TOC numbering — exactly as documented above.
**Verify**: hand-built small letter buckets exercising (a) letter forcing new page mid-column, (b) artist+first-song pushed by orphan control, (c) artist name alone taller than a column (orphan skip path), (d) TOC page numbers matching pagination's own `firstPage`. Assert exact `pageNumber`/column contents.

### Task 3 — HTML rendering + PDF export (needs Task 2)
Two parts with different risk profiles — separate model calls per the plan's own convention:
- `render_html(...)` — S/M — **Model: Sonnet 5** (large surface area but mechanical: every color/font/dimension is already catalogued above, this is careful verbatim porting into string templates, not new design)
- Chrome/Edge discovery + `render_pdf(...)` — S — **Model: Sonnet 5** (new logic: subprocess invocation, path-discovery fallback chain, and the "fail loudly, name the cause" error-handling boundary specified in the spec's Boundaries section)
Add to `songbook.py` + `tests/test_songbook.py`.
- `render_html(paginated, accent_color='#8C2727', cover_color='#6B8E23', binding_margin=0.9)` — Python string template with the full CSS token set above, `@page`/print-size CSS reimplemented directly (816×1056px letter, insets) rather than depending on `doc-page.js`.
- Chrome/Edge discovery: `shutil.which('chrome')`/`shutil.which('msedge')`, then standard Program Files / Program Files (x86) paths for each. Raise a specific named exception if neither found.
- `render_pdf(html_str, out_pdf_path)` — writes sibling `.html` (kept as manual-print fallback), shells to `[browser, '--headless', '--disable-gpu', f'--print-to-pdf={out}', '--no-pdf-header-footer', html_uri]`, temp-file + `os.replace` for both outputs.
**Verify**: `render_html()` structural-marker assertions (no browser needed); `shutil.which` monkeypatched to `None` → specific error, no crash. **Manual one-time visual checkpoint**: generate a real PDF from `sample-library.csv`, compare side-by-side against `screenshots/01-cover.png`/`02-most-requested.png`/`03-song-list.png` — this is where the PIL fudge factor and default colors get confirmed or retuned.
**▶ Human checkpoint**: stop here and show the PDF/screenshot comparison before starting GUI work.

### Task 4 — Orchestrator + optional CLI entry (needs Task 3)
— S — **Model: Haiku 4.5** (thin glue/wrapper — chains already-built functions, `argparse` mirrors `dedupe_report.py`'s existing flag style almost exactly; low judgment, low correctness risk)
Add to `songbook.py`.
- `generate_songbook(songs_folder, songs=None, column_count=3, binding_margin=0.9, accent_color=..., cover_color=...)` — `songs=None` walks the folder itself (CLI path); otherwise uses the passed-in list (GUI path, no re-scan). Chains Tasks 1-3, writes `<songs_folder>/Clone Hero Songbook.pdf`+`.html`, returns `{pdf_path, html_path, page_count, stats}` or raises. Mirrors `_run_library_tool`/`_format_tool_summary`'s module-level-dispatch reasoning (`gui.py:1015-1075`).
- `argparse` CLI mirroring `dedupe_report.py`'s `--library-path`/`--dry-run` style: `--library-path`, `--columns`, `--binding-margin`, `--accent`, `--cover`, `--out`.
**Verify**: cold-shell CLI run against a `sample-library.csv`-built tmp folder, no GUI import; one pytest covering `generate_songbook()` end-to-end (skip real-PDF assertion if no Chrome/Edge present, assert HTML at minimum).

### Task 5 — GUI integration (needs Task 4)
— L — **Model: Sonnet 5** (new dialog class + threading + settings-persistence wiring, editing an already-large/production `gui.py` — closely modeled on `LibraryToolsDialog` so the pattern is known, but thread-marshalling mistakes here are the kind that only surface as an intermittent UI freeze or a silently-stale status label)
Edits to `gui.py` only.
- `SongbookDialog(ctk.CTkToplevel)` — same boilerplate as `LibraryToolsDialog.__init__`. Controls: column-count selector (2/3/4), binding-margin slider (0.6-1.3in), accent-color swatches (3), cover-color swatches (4), status label, Generate button. On Generate: disable controls, background thread → `songbook.generate_songbook(...)` → `self.after(0, ...)` finish (success: status + Open action; failure: red error naming the specific cause, e.g. missing Chrome).
- `App` additions: `songbook_options` in `self._settings` (new key), `_songbook_options()`/`_on_songbook_option_change()` mirroring `_tool_dry_run_prefs`/`_on_tool_dry_run_change` exactly, `_open_songbook_dialog()` mirroring `_open_library_tools`'s busy-check pattern, new "Generate Songbook" `CTkButton` in `folder_row` (grid plan above), disabled directly per the `_songs_folder`/`_songs` guard (lower friction than click-then-message, since this isn't a destructive hygiene scan).
**Verify**: manual — `python gui.py`, scan a real folder, click through the dialog, confirm PDF lands in the songs folder and opens correctly.

### Task 6 — GUI dialog tests (needs Task 5)
— S — **Model: Sonnet 5** (adapting a near-identical existing template is mostly mechanical, but background-thread test assertions — the `_pump()` mainloop-polling pattern — are a recurring source of flaky/wrong tests if the timing predicate is copied carelessly; worth the extra reasoning budget over Haiku)
New `tests/test_songbook_dialog.py`, mirroring `tests/test_library_tools_dialog.py` exactly (module-scoped withdrawn root fixture + Tk-unavailable skip, per-test dialog fixture monkeypatching `_asset_path`, `_pump()` helper, final no-dialog-instance test against the module-level `songbook.generate_songbook()`).
**Verify**: `pytest tests/test_songbook_dialog.py -v` — skips cleanly (not fails) on a headless/no-Tk environment.

## Verification Summary (end-to-end)
1. `pytest tests/ -v` full suite green, including new files.
2. `python songbook.py --library-path <sample folder>` — standalone, no GUI import.
3. `python gui.py` → scan → Generate Songbook → adjust settings → Generate → visual compare vs the three reference screenshots.
4. Re-run step 3 unchanged twice → identical page count and TOC page numbers (determinism check).
5. Hide Chrome and Edge → confirm a clear, specific GUI error, not a crash or silent no-op.

## Open items carried into implementation
- PIL's `×1.08` fudge factor is a starting guess; Task 3's manual checkpoint is where it gets tuned against real screenshots.
- `bindingMargin`/`accentColor`/`coverColor` default discrepancy resolved via `renderVals()`'s runtime fallbacks — double-check against `screenshots/01-cover.png`'s actual colors during Task 3.
