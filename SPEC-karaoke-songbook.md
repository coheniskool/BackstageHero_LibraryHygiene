# Spec: Karaoke Songbook Generator

## Objective

Add a "Generate Songbook" feature to BackstageHero_LibraryHygiene: a manual, on-demand tool that turns the Artist/Title data BackstageHero already extracts from a scanned library into a punk/grunge-styled, print-and-bind-ready PDF songbook — an alphabetical Artist/Title listing with letter-divider sections and a "Most Requested" table of contents for outlier-prolific artists.

This ports the design in the handoff bundle at `C:\Users\aaron\Downloads\Karaoke Book Design System\design_handoff_karaoke_songbook\` (`README.md`, `reference/Clone Hero Songbook.dc.html` — the pixel-reference prototype and its pagination logic, `reference/sample-library.csv` — 7,687-row test fixture, `screenshots/`) into this codebase, rather than building it as the handoff's originally-suggested standalone Node CLI. It runs inside the existing Python/customtkinter GUI so it can read directly from a completed scan instead of requiring a separate CSV re-upload step.

**User**: same solo hobbyist as the rest of this app. Wants a physical, printable catalog of his charted songs, regenerated on demand whenever his library changes meaningfully (not on every scan — scans are frequent and quick; songbook generation is a deliberate, occasional action).

**Success looks like**: after scanning a library folder, click a "Generate Songbook" button, optionally tweak column count / binding margin / accent & cover color in a small settings dialog, click Generate, and get a paginated PDF (plus its intermediate HTML) written next to the library's existing `backstagehero_library.csv`, matching the reference design pixel-close — same fonts, colors, torn-poster cover, letter-banner dividers, orphan-free column flow, and a "Most Requested" TOC with correct page numbers.

## Tech Stack

- **No new Python dependency for text/layout.** Pagination is computed in Python, not ported JS-in-a-webview: text width measurement uses `PIL.ImageFont` (pillow is already a dependency, used today for the logo/splash) against the real `Courier New` / `Courier New Bold` TTFs (`C:\Windows\Fonts\cour.ttf` / `courbd.ttf`) in place of the reference's `<canvas> 2D context measureText()`.
- **PDF rendering: shell out to installed Chrome or Edge in headless print-to-PDF mode** (`--headless --print-to-pdf=<out> --no-pdf-header-footer <html-file>`). No Puppeteer, no Node, no new pip package. Discovery order: `shutil.which('chrome')` / `shutil.which('msedge')`, then the standard install paths (`Program Files\Google\Chrome\Application\chrome.exe`, `Program Files (x86)\Google\Chrome\Application\chrome.exe`, `Program Files\Microsoft\Edge\Application\msedge.exe`, `Program Files (x86)\Microsoft\Edge\Application\msedge.exe`). If neither is found, generation fails with a clear GUI error naming the missing dependency — never silently falls back to something else.
- **HTML/CSS**: plain Python string templates reusing the reference's CSS almost verbatim (colors, gradients, clip-paths for the cover's tape/grain texture — no raster image assets, matching the handoff's "Assets" section).
- No new runtime dependency added to `requirements.txt`.

## Commands

```
Run (GUI):   python gui.py  → "Generate Songbook" button in the header row → settings
             dialog → Generate
Run (CLI, optional, mirrors dedupe_report.py's dual-mode pattern):
             python songbook.py --library-path <path> [--columns 3] [--binding-margin 0.85]
             [--accent red|olive|denim] [--cover olive|denim|red|yellow] [--out <path.pdf>]
Test:        pytest tests/ -v
```

## Project Structure

New module, following this repo's existing "one new `.py` file per hygiene tool" pattern (`chart_rename.py`, `dedupe_report.py`, `library_enrichment.py`):

```
songbook.py            -> new module. Pure functions, no GUI imports (importable/testable headless,
                           same separation dedupe_report.py and chart_rename.py already have from gui.py):
  parse_entries(songs_or_csv_path)      -> list of (artist, title) from either the in-memory Song
                                           list gui.py already holds post-scan, OR a standalone CSV
                                           path (for the optional CLI path) -- read_metadata() is
                                           reused from library_common.py either way, never re-parsed
                                           from scratch.
  dedupe_and_sort(entries)              -> case-insensitive de-dup + alphabetical sort, per the
                                           handoff README's exact rules (strip leading quote chars
                                           before sorting; keep first-seen casing as display name).
  bucket_by_letter(entries)             -> A-Z + '#' grouping.
  compute_toc(buckets)                  -> mean/stdev threshold, "Most Requested" artist list.
  paginate(buckets, toc, columnCount,
           bindingMargin)               -> the ported _paginate() logic: PIL-measured capacity-based
                                           column flow, forced page break per letter section, orphan
                                           control, hang-indent wrap -- see reference file's component
                                           class for the algorithm to port line-by-line.
  render_html(pages, toc, stats,
              accentColor, coverColor)  -> builds the final HTML string from the ported CSS.
  render_pdf(html, out_path)            -> writes HTML next to out_path, shells to Chrome/Edge
                                           headless print-to-pdf, returns the PDF path or raises.
  generate(songs, songs_folder, **opts) -> orchestrates the above; this is what gui.py calls.

gui.py                 -> adds:
  - "Generate Songbook" CTkButton in the header `folder_row` (next to "Library Tools"), enabled
    only when self._songs is non-empty -- same guard _export_library_csv() already uses.
  - SongbookDialog(ctk.CTkToplevel) class, same shape as LibraryToolsDialog: column-count selector
    (2/3/4), binding-margin slider (0.6-1.3in), accent-color swatches (red/olive-gold/denim),
    cover-color swatches (olive/denim/red/yellow), a "Generate" button that runs songbook.generate()
    in a background thread (mirrors _run_tool's disable-buttons/status-label/threading.Thread
    pattern), and an "Open" action once the PDF is written.
  - Settings persisted via the existing self._settings / _persist_setting() mechanism (same
    pattern as _tool_dry_run_prefs / _on_tool_dry_run_change), under a new 'songbook_options' key.

tests/test_songbook.py -> new. Fixtures built from reference/sample-library.csv's real edge cases
                           (quoted fields, embedded commas/quotes, case-variant artist names).
```

Output files (regenerated fresh each run, like `backstagehero_library.csv`):
```
<songs_folder>/Clone Hero Songbook.pdf   -> final output
<songs_folder>/Clone Hero Songbook.html  -> kept alongside as a manual-print fallback and for
                                             debugging pagination issues without re-running Chrome
```

## Code Style

Match this repo's established conventions (already documented in `SPEC.md`'s Code Style section, carried forward here):

- 4-space indentation, no tabs.
- Plain functions, one per discrete step (no new classes for the pagination/generation logic itself — only `SongbookDialog` is a class, matching `LibraryToolsDialog`'s existing precedent for GUI panels).
- Comments explain *why*, not what — e.g. why PIL substitutes for canvas measureText, why Chrome/Edge headless instead of a Python PDF library.
- f-strings throughout.
- Atomic writes for the output PDF/HTML (temp file + `os.replace`), consistent with every other file-writing path in this codebase.

## Testing Strategy

- **Unit-tested** (pure functions, no Chrome/Edge or GUI needed): `parse_entries()`, `dedupe_and_sort()` (case-insensitive merge + quote-stripping sort, using `reference/sample-library.csv`'s real edge cases as fixtures), `bucket_by_letter()`, `compute_toc()` (threshold math), and `paginate()`'s orphan-control and page-break-per-letter rules against small synthetic letter buckets sized to force each edge case deliberately.
- **Not unit-tested**: the actual Chrome/Edge subprocess invocation and visual pixel-fidelity — validated by manually opening the generated PDF and comparing against `screenshots/01-cover.png`, `02-most-requested.png`, `03-song-list.png`, same "manual validation for real external tools" precedent this repo already follows for `reencode_to_cfr()` and real fingerprint matching.
- No CI — local script, run on-demand, matching the rest of this repo.

## Boundaries

- **Always**:
  - Recreate the reference design pixel-close (colors, fonts, layout proportions, copy) — it is final, not a starting point.
  - Keep `songbook.py` GUI-import-free so it stays independently testable and CLI-callable, matching `dedupe_report.py`/`chart_rename.py`'s existing separation from `gui.py`.
  - Regenerate the PDF/HTML fully on every Generate click (no incremental/partial update) — same "never quietly goes stale" principle as `_export_library_csv()`.
  - Fail loudly in the GUI (status label + message) if Chrome/Edge isn't found or the subprocess errors — never silently produce an empty or missing PDF.

- **Ask first**:
  - Adding any new pip dependency (e.g. if PIL-based measurement or headless print-to-PDF proves insufficient and something like `weasyprint`/`reportlab` starts to look necessary).
  - Changing the default output location or filename away from `<songs_folder>/Clone Hero Songbook.pdf`.
  - Wiring songbook generation to run automatically after a scan (explicitly deferred — manual button only, per this spec).

- **Never**:
  - Never write generated songbook output into a song's own chart folder, or touch any `song.ini`/chart/audio file — this feature only reads Artist/Title data and writes to the songs-folder root, same boundary `_export_library_csv()` already respects.
  - Never block the GUI thread during generation — must run on a background thread like every other Library Tool.
  - Never guess at Chrome/Edge's location beyond `PATH` + the documented standard install paths above.

## Success Criteria

- Clicking "Generate Songbook" after a scan with songs present produces `Clone Hero Songbook.pdf` in the songs folder, matching the three reference screenshots in layout/color/typography.
- Running the same library twice with unchanged settings produces byte-stable pagination (same page count, same "Most Requested" page numbers) — no nondeterminism in the layout algorithm.
- `pytest tests/test_songbook.py -v` passes, covering dedup/sort/bucket/TOC/pagination edge cases from `reference/sample-library.csv`.
- Settings (column count, margin, colors) persist across app restarts via `self._settings`.
- Missing Chrome/Edge produces a clear, specific GUI error rather than a crash or silent no-op.

## Open Questions

- Exact PIL font-metric fidelity vs. the original canvas `measureText()` numbers hasn't been empirically verified yet — first implementation pass should include a manual pixel-comparison check against the three reference screenshots before trusting the ported capacity constants (~900px/column) as-is; they may need retuning for PIL's metrics.
- Whether "Most Requested" TOC and letter-bucket edge cases (e.g., artists whose name is entirely non-alphanumeric) need any special-casing beyond the `#` bucket the README already specifies — deferred to implementation/testing against the real 7,687-row fixture.
