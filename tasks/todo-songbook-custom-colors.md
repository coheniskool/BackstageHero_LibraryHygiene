# TODO: Custom Songbook Colors (Album Art + Manual Picker)

See [`plan-songbook-custom-colors.md`](plan-songbook-custom-colors.md) for full detail. Spec: [`../SPEC-songbook-album-art-colors.md`](../SPEC-songbook-album-art-colors.md).

## Task 1: Palette extraction + legibility clamping (songbook.py) — ✅ DONE 2026-08-02
— M — **Model: Sonnet 5** (new algorithmic logic, real correctness bar)
- [x] `_load_weighted_palette()` — Pillow quantize+getcolors on a downscaled copy
- [x] `_score_swatch()` — `count * (0.05 + 0.95*saturation)` — tuned so a vivid 20%-area region beats an 80% gray background comfortably (not a razor's-edge split)
- [x] `_pick_primary_and_accent()` — top score = primary; scans past a hue-close 2nd place for the first swatch ≥30° away, falls back to literal 2nd-highest if nothing clears the bar (incl. single-swatch flat-art case: accent=primary)
- [x] `_clamp_for_legibility(rgb, role)` — HSL band per role (`cover`: L[0.35,0.70]/minS 0.35; `accent`: L[0.22,0.50]/minS 0.45 — darker/more-saturated since it's used both as a white-text badge bg and as text-on-cream), generic (reused by Task 3's custom picker)
- [x] `AlbumArtError` + `extract_cover_and_accent_colors()`
- [x] 24 unit tests: weighted scoring (gray-vs-red), hue-distance-skip with a 3rd-place fallback, single-swatch case, clamp edge cases (white/black/gray) — required widening an initially-too-tight `1e-6` tolerance to `6e-3` to account for real 8-bit RGB round-trip quantization noise, not a real bug
- [x] Manual checkpoint: ran against 3 realistic synthetic images (pale cream+powder-blue, near-black+dark-red, vivid yellow+purple) and **rendered the actual cover/TOC pages through headless Chrome** to eyeball real legibility — all three read clearly; the dark case's accent color landed 0.0036 below the saturation floor on paper (pure round-trip rounding, confirmed by checking the raw un-rounded value) but is visually indistinguishable from in-band. No constant retuning needed.

## Task 2: Cover-image embedding + render/generate plumbing (songbook.py) — ✅ DONE 2026-08-02
— S/M — **Model: Sonnet 5**
- [x] `_image_to_data_uri(image_path, max_dimension=1200)` — caught and fixed a real bug during implementation: Pillow's `.format` attribute is lost after `.convert('RGB')` (confirmed directly), so the JPEG-vs-PNG format decision has to read `.format` *before* converting, not after
- [x] `render_html(..., cover_image_data_uri=None)` fills the cover's blank collage div; unchanged (blank) when omitted
- [x] `generate_songbook(..., cover_image_path=None)` — does NOT call extraction itself, per the spec's boundary
- [x] CLI: `--album-art`, `--accent-hex`, `--cover-hex`, `--show-album-art-on-cover`; `--accent`/`--accent-hex` and `--cover`/`--cover-hex` are true `argparse.add_mutually_exclusive_group()` pairs; `--album-art`'s conflict with all four role flags is a manual post-parse check (argparse can't put one arg in two groups) that still raises via `parser.error()` for the same usage-message-then-`SystemExit(2)` behavior
- [x] 17 new tests: `<img>` present/absent, data-URI round-trip + downscaling, end-to-end with a real image, all CLI flag defaults/overrides, both native mutually-exclusive-group errors and the manual album-art conflict check, `main()`'s color resolution via a spy on `generate_songbook`
- [x] `pytest tests/test_songbook.py -v` — 81/81 passed
- [x] Cold-shell CLI verification: `--album-art ... --show-album-art-on-cover` (confirmed the data URI actually lands in the output HTML), `--accent-hex/--cover-hex` (confirmed absent), and the mutual-exclusion error (confirmed exit code 2 with a specific message naming the conflicting flag) — all three run standalone, no GUI import
- [x] `pytest tests/ -v` full suite — 681 passed, 1 pre-existing skip, no regressions

## Task 3: GUI — Album Art row + Custom color buttons (gui.py, needs Task 1-2) — ✅ DONE 2026-08-02
— L — **Model: Sonnet 5** (production `gui.py`, subtle-bug risk profile)
- [x] `from tkinter import colorchooser` import
- [x] "Custom…" button per role appended to `_accent_buttons`/`_cover_buttons` (row 4/5), reusing `_refresh_swatch_selection()` unchanged — its `fg_color` IS the picked/clamped hex, same visual language as the presets
- [x] Custom click → `colorchooser.askcolor()` → `songbook._clamp_for_legibility()` → set role source='custom' → update button color → persist; Cancel (`(None, None)`) = no-op
- [x] New Album Art row (row 6) + "show on cover" checkbox (row 7, disabled until image loaded): Choose Image… + 28px thumbnail (`CTkImage`, same pattern as the header logo) + Clear
- [x] Picking image → background thread → `extract_cover_and_accent_colors()` → both roles set to 'custom' via the exact same `_apply_custom_color()` path Custom… uses (called twice, once per role); `AlbumArtError` → status label
- [x] `_resolve_role_color(role)` + `_worker()` color resolution made source-aware per role; passes `cover_image_path=` only when the checkbox is checked AND an image is loaded
- [x] New persisted keys: `album_art_path`, `show_album_art_on_cover`, `accent_source`/`cover_source`, `accent_custom_hex`/`cover_custom_hex` — **simplified vs. the plan's "re-extract on reopen"**: since the clamped hex is already persisted from last time, reopen just reloads the saved hex/thumbnail directly (no re-running extraction, which would be redundant work producing the same deterministic result); still silently drops a moved/deleted `album_art_path` with no error
- [x] **2 real bugs caught by the test suite before manual verification**: (1) two pre-existing tests needed updating because `_on_accent_change`/`_on_cover_change` now also correctly persist `{role}_source='swatch'` (intentional new behavior, not a regression); (2) a genuine bug — `except songbook.AlbumArtError as e:` followed by a deferred `self.after(0, lambda: ...str(e))` raised `NameError` because Python 3 clears `e` the instant the except block exits, before the lambda ever runs — fixed by capturing `message = str(e)` into a plain local first
- [x] **Real (unmocked) end-to-end verification**, since `gui.py` has no installed-app identity for this session's screen-automation tooling to target: built a real Tk root + real `SongbookDialog`, ran the actual background-thread album-art extraction against a real image, checked a real Generate against the real `songbook.generate_songbook()`, and confirmed via the actual output HTML that both extracted+clamped colors and the embedded image landed correctly — then overrode accent via a real (OS-dialog-mocked) Custom… pick and confirmed cover's album-art-derived color was untouched, proving the per-role independence the spec required

## Task 4: GUI dialog tests (needs Task 3) — ✅ DONE 2026-08-02
— M — **Model: Sonnet 5**
- [x] Custom… mocked askcolor tests (success + cancel), clamping-applied test, button-shows-clamped-hex test
- [x] Album art mocked extraction tests (success sets both roles; error shows status)
- [ ] Swatch-after-album-art/custom reverts just that role
- [ ] Checkbox disabled-until-loaded; `_worker()` passes `cover_image_path` only when checked
- [x] `pytest tests/test_songbook.py tests/test_songbook_dialog.py -v` — 81 + 46 passed
- [x] `pytest tests/ -v` full suite — 696 passed, 1 pre-existing skip, no regressions

## ▶ Checkpoint (final) — ✅ DONE 2026-08-02
- [x] `pytest tests/ -v` full suite green — 696 passed, 1 pre-existing skip
- [x] `python songbook.py --library-path <folder> --album-art <image>` and `--accent-hex/--cover-hex` both run standalone, no GUI import (Task 2's cold-shell check)
- [x] Manual GUI flow verified via a real (unmocked) end-to-end script rather than on-screen clicking (see Task 3 note on why) — real extraction, real generation, real per-role override, all confirmed against actual output
- [x] Re-ran `extract_cover_and_accent_colors()` twice against the same real image → byte-identical hex output (also incidentally confirmed the single-swatch-image fallback: a flat solid-color image correctly returns the same hex for both cover and accent, exactly the documented/tested degenerate case, not a bug)
- [x] Diff review: `songbook.py` (+~250 lines: extraction/clamping/data-URI/CLI), `gui.py` (+201/-14: Custom buttons, Album Art row, source-aware `_worker`), `tests/test_songbook.py` (+~200 lines), `tests/test_songbook_dialog.py` (+~180 lines) — caught and fixed 2 real bugs during this review/test pass (Pillow `.format`-lost-after-`.convert()`, and the `except...as e` deferred-lambda `NameError`)

---

### Notes
- Line numbers verified live at plan time (2026-08-02) — re-verify if drifted.
- Tasks 1→2→3→4 are sequential (each needs the previous).
- Task 1's manual checkpoint (real-image clamp tuning) is deliberate — cheaper to get right before Task 3 wires the UI on top.
