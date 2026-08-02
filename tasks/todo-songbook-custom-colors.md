# TODO: Custom Songbook Colors (Album Art + Manual Picker)

See [`plan-songbook-custom-colors.md`](plan-songbook-custom-colors.md) for full detail. Spec: [`../SPEC-songbook-album-art-colors.md`](../SPEC-songbook-album-art-colors.md).

## Task 1: Palette extraction + legibility clamping (songbook.py)
— M — **Model: Sonnet 5** (new algorithmic logic, real correctness bar)
- [ ] `_load_weighted_palette()` — Pillow quantize+getcolors on a downscaled copy
- [ ] `_score_swatch()` — count * saturation-weighted score
- [ ] `_pick_primary_and_accent()` — top score = primary; ≥30° hue-distant next = accent, else 2nd-highest
- [ ] `_clamp_for_legibility(rgb, role)` — HSL band per role, generic (reused by Task 3's custom picker)
- [ ] `AlbumArtError` + `extract_cover_and_accent_colors()`
- [ ] Unit tests: weighted scoring (gray-vs-red), two-solid-halves, clamp edge cases (white/black/gray) with exact bound assertions
- [ ] Manual checkpoint: run against 3-4 real images (pale/dark/vivid), verify HSL numbers land in intended bands, tune constants here (not deferred)

## Task 2: Cover-image embedding + render/generate plumbing (songbook.py)
— S/M — **Model: Sonnet 5**
- [ ] `_image_to_data_uri(image_path, max_dimension=1200)`
- [ ] `render_html(..., cover_image_data_uri=None)` fills the cover's blank collage div
- [ ] `generate_songbook(..., cover_image_path=None)` — does NOT call extraction itself
- [ ] CLI: `--album-art`, `--accent-hex`, `--cover-hex`, `--show-album-art-on-cover`, mutually-exclusive groups per role
- [ ] Tests: `<img>` present/absent, end-to-end with real image, CLI flag defaults + mutual-exclusion `SystemExit`

## Task 3: GUI — Album Art row + Custom color buttons (gui.py, needs Task 1-2)
— L — **Model: Sonnet 5** (production `gui.py`, subtle-bug risk profile)
- [ ] `from tkinter import colorchooser` import
- [ ] "Custom…" button per role appended to `_accent_buttons`/`_cover_buttons`, reusing `_refresh_swatch_selection()` unchanged
- [ ] Custom click → `colorchooser.askcolor()` → clamp → set role source='custom' → update button color → persist; Cancel = no-op
- [ ] New Album Art row: Choose Image… + thumbnail + Clear + "show on cover" checkbox (disabled until image loaded)
- [ ] Picking image → background thread → `extract_cover_and_accent_colors()` → both roles set to 'custom' via the same per-role path Custom… uses; `AlbumArtError` → status label
- [ ] `_worker()` color resolution made source-aware per role; passes `cover_image_path=` when checked+loaded
- [ ] New persisted keys: `album_art_path`, `show_album_art_on_cover`, `accent_source`/`cover_source`, `accent_custom_hex`/`cover_custom_hex`; reopen logic (re-extract if file exists, silent fallback if not)
- [ ] Manual verify: real album art → legible colors; Custom… overrides one role only; show-on-cover checkbox reflected in generated PDF

## Task 4: GUI dialog tests (needs Task 3)
— M — **Model: Sonnet 5**
- [ ] Custom… mocked askcolor tests (success + cancel)
- [ ] Album art mocked extraction tests (success sets both roles; error shows status)
- [ ] Swatch-after-album-art/custom reverts just that role
- [ ] Checkbox disabled-until-loaded; `_worker()` passes `cover_image_path` only when checked
- [ ] `pytest tests/test_songbook.py tests/test_songbook_dialog.py -v` green
- [ ] `pytest tests/ -v` full suite, no regressions

## ▶ Checkpoint (final)
- [ ] `pytest tests/ -v` full suite green
- [ ] `python songbook.py --library-path <folder> --album-art <image>` and `--accent-hex/--cover-hex` both run standalone
- [ ] `python gui.py` full manual flow verified
- [ ] Re-run generation twice with same album art → identical colors (determinism)
- [ ] Diff review: `songbook.py`, `gui.py`, `tests/test_songbook.py`, `tests/test_songbook_dialog.py`

---

### Notes
- Line numbers verified live at plan time (2026-08-02) — re-verify if drifted.
- Tasks 1→2→3→4 are sequential (each needs the previous).
- Task 1's manual checkpoint (real-image clamp tuning) is deliberate — cheaper to get right before Task 3 wires the UI on top.
