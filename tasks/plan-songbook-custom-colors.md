# Plan: Custom Songbook Colors (Album Art + Manual Picker)

See [`../SPEC-songbook-album-art-colors.md`](../SPEC-songbook-album-art-colors.md) for objective, decisions locked in during /spec, and boundaries. See [`todo-songbook-custom-colors.md`](todo-songbook-custom-colors.md) for the checklist derived from this plan.

## Context

The spec adds two new ways to choose the songbook's accent/cover colors on top of the four fixed named swatches shipped in the original songbook feature: (1) derive both colors from a picked image's dominant colors, with an optional checkbox to also place that image in the cover's currently-blank collage area, and (2) pick either color directly via the native OS color dialog (slider/wheel + hex field in one). Two explicit decisions from /spec: legibility auto-clamping applies to custom-picked colors too, not just extracted ones; and the color-source model is **per-role** (`accent_source`/`cover_source`, each `'swatch'|'custom'`) rather than one joint mode — album art is the one action that sets both roles' custom hex at once, but each role can still be independently overridden afterward.

Builds directly on the already-shipped songbook feature (commits `cb06eb4`..`e9a49f7`). Line numbers below were verified live against current code at plan time (2026-08-02).

## Key facts verified against current code

- `songbook.py`: `DEFAULT_ACCENT_COLOR='#3B5998'`/`DEFAULT_COVER_COLOR='#8C2727'` (line ~402), `ACCENT_COLOR_CHOICES`/`COVER_COLOR_CHOICES` dicts (line ~408), `render_html(paginated, stats, accent_color=..., cover_color=..., binding_margin=..., synced_label='')` (line 528), `generate_songbook(songs_folder, songs=None, column_count=..., binding_margin=..., accent_color=..., cover_color=..., stdev_multiplier=..., synced_label='', out_path=None)` (line 636), `parse_args()` (line 677). The cover's blank collage placeholder is one exact line: `_render_cover_page()`'s `<div style="position: relative; flex: 1; margin: 10px -32px 10px 0;"></div>` (line 463) — this is where a picked image's `<img>` tag goes.
- `gui.py`: `SongbookDialog._build()` (line 1446) lays out rows 0-9: title(0), description(1), columns(2), binding margin(3), accent swatches(4), cover swatches(5), stdev multiplier(6), status(7), Open/Generate(8), Close(9). `_accent_buttons`/`_cover_buttons` dicts (populated ~1497-1519) are keyed by preset name and consumed by `_refresh_swatch_selection(buttons, selected_key)` (line 1573), which just borders whichever key matches — adding a `'custom'` key to these same dicts, with its `fg_color` set dynamically instead of fixed, reuses this exact mechanism unchanged. `_on_accent_change`/`_on_cover_change` (line 1593/1598) and `_worker()`'s color resolution (line 1626-1636) are the two places needing a source-aware branch instead of a flat swatch-name lookup. `_asset_path`'s logo-thumbnail pattern (`ctk.CTkImage(_PILImage.open(path), size=(w,h))`, line 1784-1792) is the thumbnail-preview precedent to reuse. `tkinter.colorchooser` is **not yet imported** (only `filedialog, messagebox, ttk` at line 39). `App._SONGBOOK_OPTION_DEFAULTS`/`_songbook_options()`/`_on_songbook_option_change()` (line 2096-2113) are a plain dict-merge-over-defaults + generic key/value persist — extending needs no structural change, just more dict entries.
- `requirements.txt` pins `pillow>=9.0`; dev env has 12.3.0. Start with `Image.Quantize.MEDIANCUT` (ancient-Pillow-safe) to keep the floor untouched, per the spec's ask-first boundary on bumping it.
- `colorsys` (stdlib) has `rgb_to_hls`/`hls_to_rgb` — exactly what `_clamp_for_legibility()` needs, no new dependency.

## Task Breakdown

Vertically sliced, dependency-ordered — pure extraction/clamping logic first, then render/generate plumbing, then GUI wiring on top.

### Task 1 — Palette extraction + legibility clamping (songbook.py, pure functions)
— M — **Model: Sonnet 5** (new algorithmic logic with a real correctness bar — scoring must prefer vivid-but-small regions over large-but-dull ones; clamping must provably fix the edge cases it exists for)
- `_load_weighted_palette(image_path, num_colors=8)` — downscale (max dim ~200px), `.quantize(colors=N, method=Image.Quantize.MEDIANCUT).convert('RGB').getcolors()` → `[(count, (r,g,b)), ...]`.
- `_score_swatch(count, rgb)` — `count * (0.15 + 0.85*saturation)` via `colorsys.rgb_to_hls` — floor term so a large moderately-saturated area still beats a tiny fully-saturated speck, pure grays heavily discounted.
- `_pick_primary_and_accent(scored)` — top = primary; next entry ≥30° hue away = accent, else 2nd-highest overall (documented fallback for near-monochrome images).
- `_clamp_for_legibility(rgb, role)` — HSL band per role (`'cover'`: not so dark it fights fixed dark poster text, not so light it washes out; `'accent'`: narrower/darker-leaning band — must work as white-on-accent badge bg AND accent-as-text on cream). Generic (rgb+role in, hex out) — Task 3's custom-picker path calls it directly with zero new code.
- `AlbumArtError(RuntimeError)` — unreadable/corrupt/zero-byte file.
- `extract_cover_and_accent_colors(image_path)` — orchestrates the above, returns `(cover_hex, accent_hex)`, both clamped.
**Verify**: synthetic-image unit tests (90%-gray/10%-red → red scores primary; two-solid-color-halves → both picked correctly; pure-white/black/desaturated-gray → each lands in its role's band, exact bounds asserted). Manual checkpoint: run against 3-4 real images (pale, dark, vivid) and eyeball resulting HSL numbers against the intended bands before calling constants final — done here, not deferred.

### Task 2 — Cover-image embedding + render/generate plumbing (songbook.py)
— S/M — **Model: Sonnet 5** (mechanical plumbing through existing functions, but data-URI/downscale path is new I/O-adjacent logic worth care)
- `_image_to_data_uri(image_path, max_dimension=1200)` — Pillow downscale if needed, re-save to in-memory buffer (PNG, or JPEG if source was JPEG), base64-encode, `data:image/<fmt>;base64,...`.
- `render_html(..., cover_image_data_uri=None)` — fills the line-463 empty div with an `<img>` when given, unchanged otherwise.
- `generate_songbook(..., cover_image_path=None)` — builds the data URI, threads to `render_html()`. Still does NOT call `extract_cover_and_accent_colors()` itself — callers resolve `accent_color`/`cover_color` first, same as swatch-name resolution today.
- CLI: `--album-art <path>`, `--accent-hex <hex>`, `--cover-hex <hex>`, `--show-album-art-on-cover`. `--album-art`/`--accent-hex`/`--cover-hex` mutually exclusive with `--accent`/`--cover` via `argparse.add_mutually_exclusive_group()` per role; `--album-art` excluded from both groups (sets both roles at once).
**Verify**: `render_html()` `<img>`-present-vs-absent test; `generate_songbook()` end-to-end with a real small image, data URI present in output HTML; CLI `parse_args()` tests for all 4 new flags, defaults, and mutual-exclusion `SystemExit`.

### Task 3 — GUI: Album Art row + Custom color buttons (gui.py, needs Tasks 1-2)
— L — **Model: Sonnet 5** (new UI wiring in production `gui.py`; mistakes surface as subtle stale-state bugs, not crashes — same risk profile as the original SongbookDialog build)
- Add `from tkinter import colorchooser` to the existing import line.
- **Custom buttons**: one more `ctk.CTkButton` appended to each of `accent_row`/`cover_row`'s loops, keyed `'custom'` in `_accent_buttons`/`_cover_buttons` — `_refresh_swatch_selection()` unchanged. Click opens `colorchooser.askcolor(initialcolor=..., parent=self)`; `(None, None)` (Cancel) is a no-op; otherwise run through `songbook._clamp_for_legibility(rgb, role)`, set that role's `_source='custom'`, update the button's own `fg_color` to the clamped hex, persist.
- **Album Art row** (new row between cover_row(5) and stdev-multiplier — rows 6→10 shift down by one): "Choose Image…" (`filedialog.askopenfilename`, image filter) + thumbnail (`CTkImage`, logo pattern) + "Clear" (disabled until loaded) + "Also show this image on the cover" checkbox (disabled until loaded).
- Picking a file runs `extract_cover_and_accent_colors()` on a background thread (never block the GUI); on success sets BOTH roles' `_source='custom'` via the same per-role path Custom… uses, updates both buttons + thumbnail. `AlbumArtError` → status label, no state change.
- `_worker()`'s color resolution becomes per-role source-aware (custom hex directly vs. swatch-name lookup as today); passes `cover_image_path=` when checked and loaded.
- New `_SONGBOOK_OPTION_DEFAULTS` keys: `album_art_path`('') , `show_album_art_on_cover`(False), `accent_source`/`cover_source`('swatch'), `accent_custom_hex`/`cover_custom_hex`(''). Reopen: if `album_art_path` set and file exists, silently re-extract and repopulate both rows + thumbnail; if gone, skip silently, restore each row from its own saved source/hex/swatch-name.
**Verify**: manual — `python gui.py`, real album art → both colors update, legible; Custom… on one role → only that role changes; check "show on cover" → Generate → image appears on cover PDF; uncheck → blank again.

### Task 4 — GUI dialog tests (tests/test_songbook_dialog.py, needs Task 3)
— M — **Model: Sonnet 5** (mirrors existing dialog-test patterns closely, but the per-role source-switching matrix has enough cases to get subtly wrong)
- Extend the existing file with: Custom… → mocked `askcolor` → only that role updates; Cancel → no-op; picking an image (mocked extraction) → both roles' custom hex update, checkbox enables; `AlbumArtError` → status shows it, no state change; swatch-after-album-art/custom → reverts just that role; checkbox disabled with no image; `_worker()` passes `cover_image_path` only when checked.
**Verify**: `pytest tests/test_songbook.py tests/test_songbook_dialog.py -v` green; `pytest tests/ -v` full suite, no regressions.

## Verification Summary (end-to-end)
1. `pytest tests/ -v` full suite green.
2. `python songbook.py --library-path <folder> --album-art <image>` and `--accent-hex "#3B5998" --cover-hex "#8C2727"` both run standalone, no GUI import.
3. `python gui.py` full flow: album art → legible colors → Custom… override → show-on-cover checkbox → Generate → PDF confirms.
4. Re-run generation twice with the same album-art image → identical extracted colors (determinism).

## Open items resolved during /plan
- Custom… button shows the post-clamp color (the color actually used for generation), not the pre-clamp OS-dialog value — consistent with how album art's extracted-then-clamped color is what's shown.
- Exact HSL clamp band numbers are tuned against real images as part of Task 1's own verification, not deferred further.
