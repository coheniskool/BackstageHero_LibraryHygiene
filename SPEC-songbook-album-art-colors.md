# Spec: Custom Songbook Colors (Album Art + Manual Picker)

## Objective

Extend the "Generate Songbook" feature (`songbook.py`, `SongbookDialog` in `gui.py` — see `SPEC-karaoke-songbook.md`, fully shipped) with two new ways to choose accent/cover colors beyond the four fixed named swatches: (1) derive them from a picked image file's own dominant colors, and (2) pick an arbitrary color directly via a native color-picker dialog (slider/wheel + hex-code entry, both built into the OS dialog).

**User**: same solo hobbyist. Wants the songbook's look to match a specific piece of art he has in mind — a favorite album cover, a band's logo colors — or, failing that, to just dial in an exact color rather than being limited to the four presets.

**Success looks like**: in the Generate Songbook dialog, either (a) click "Choose Image…", pick a photo/album-art file, and see the accent/cover swatches replaced by two new colors pulled straight from that image — legible, not muddy or unreadably light/dark — or (b) click "Custom…" next to either color row and pick any color from the OS's native picker (which already provides both a slider/wheel and a hex-code field, so no bespoke color-picker widget needs to be built). Both new inputs sit alongside the existing swatch rows, which stay fully clickable. A checkbox lets the user also place the picked album-art image in the cover's currently-empty collage area (see Boundaries/Open Questions — this fills a real, known gap: the reference design's own cover art asset was never delivered, see `tasks/todo-karaoke-songbook.md` Task 3).

## Decisions locked in during /spec (with the user)

- **Per-role color source, not one joint mode.** Accent and cover each independently track their own source — `accent_source` / `cover_source`, each `'swatch' | 'custom'` — because a custom hex pick for accent shouldn't force cover back to a preset or vice versa; that mirrors how the two swatch rows already work today (fully independent of each other). Clicking a swatch in a row sets that row's source to `'swatch'` and records which named preset; clicking that row's "Custom…" button and choosing a color sets that row's source to `'custom'` and records the picked hex directly. Whichever was touched most recently for that role wins — no separate "confirm" step.
- **Album art is a single joint override, layered on top of the per-role sources, not a third per-role value.** Loading an image computes *both* colors at once (that's what makes it different from swatch/custom, which are always per-role) and immediately drives both rows' displayed color — but it does not erase the per-role `swatch`/`custom` selections underneath. Clicking a swatch or "Custom…" in either row afterward overrides *that role's* displayed color back to a per-role choice; there is no separate "turn off album art" toggle to hunt for, because touching a row's own controls is what turns it off for that role. (Practical effect: album art is not a mode you're "in" or "out of" — it's just the most recent action, exactly like swatch/custom already are, and it can partially apply to only one role if the user overrides the other with a swatch/custom pick afterward.)
- **Custom color input mechanism**: `tkinter.colorchooser.askcolor()` — the native OS color dialog. This single dialog already provides a slider/wheel picker AND a hex-code entry field, satisfying "slider or hex/color code input" with zero new dependency and no bespoke widget to design, test, or maintain.
- **Cover artwork placement**: a separate checkbox, independent of color extraction — "Also show this image on the cover" — controls whether the picked album-art image is placed into the cover's collage area (currently blank; the reference's own `collage-stickers.png` was never delivered in the handoff bundle). Unchecked by default: color extraction and image-on-cover are two different opt-ins, not one bundled behavior. (Not applicable to the custom-color picker, which never involves an image.)
- **Legibility**: extracted *and* custom-picked colors are auto-adjusted (HSL lightness/saturation clamped into a safe band) rather than used raw. The design's fixed text colors (dark `#232120` text on the cover poster, white `#E9E1D4` text on accent-color badges, `{accentColor}` used as literal text color on the cream TOC page) assume the cover/accent colors sit in roughly the same vividness/darkness range the four presets already do — neither a pale album cover nor a user-picked near-white/near-black hex should be allowed to make text unreadable.

## Tech Stack

- **No new pip dependency.** Color extraction uses Pillow only (already a dependency, `pillow>=9.0`): `Image.quantize(colors=N, method=Image.Quantize.MEDIANCUT)` + `getcolors()` to get a small weighted palette, no numpy/scikit-learn k-means. `Image.Quantize.FASTOCTREE` (better results on photos) requires Pillow ≥9.1 — **ask-first**: worth a version-floor bump if MEDIANCUT's results look muddy in testing, but starting with MEDIANCUT keeps today's `>=9.0` floor untouched.
- Cover-image embedding uses a base64 data URI (stdlib `base64` + Pillow's own format re-encode) — no new asset-copying/relative-path machinery, keeps the existing single-HTML-file-is-self-contained property intact for the manual-print fallback.
- File picker: `tkinter.filedialog.askopenfilename`, already imported in `gui.py` (`from tkinter import filedialog, messagebox, ttk`).
- Custom color picker: `tkinter.colorchooser.askcolor()` — stdlib, ships with Python, no import currently present in `gui.py` so this adds one `from tkinter import colorchooser` line, not a dependency.

## Commands

```
Run (GUI):   python gui.py → Generate Songbook → either:
               "Choose Image…" → (both colors update) → optionally check
               "Also show this image on the cover", or
               "Custom…" next to Accent or Cover → pick a color in the native dialog
             → Generate
Run (CLI):   python songbook.py --library-path <path> --album-art <image path>
             [--show-album-art-on-cover] [--columns 3] [--binding-margin 0.9] [--out <path>]
             python songbook.py --library-path <path> --accent-hex "#3B5998"
             --cover-hex "#8C2727" [--columns 3] [--out <path>]
             (--album-art, --accent/--accent-hex, and --cover/--cover-hex are each
             mutually exclusive within their own role; --album-art sets both roles
             at once and cannot be combined with either role's flag)
Test:        pytest tests/test_songbook.py tests/test_songbook_dialog.py -v
```

## Project Structure

New functions added to the existing `songbook.py` (not a new module — this logic only serves the songbook feature, same reasoning that kept `render_html`/`paginate` together in one file):

```
songbook.py  (additions)
  extract_cover_and_accent_colors(image_path)
      -> (cover_hex, accent_hex). Pure, GUI-free, independently testable/CLI-callable.
      Raises AlbumArtError on an unreadable/corrupt/unsupported file.
      Internals (private helpers):
        _load_weighted_palette(image_path, num_colors=8)
            -> [(pixel_count, (r,g,b)), ...] via Image.quantize()+getcolors() on a
               downscaled (e.g. 200px-max-dimension) copy, for speed.
        _score_swatch(count, rgb)
            -> count * saturation-ish weight, so a large flat gray background loses
               to a smaller but vivid logo color (mirrors Android Palette API /
               Vibrant.js-style dominant-color scoring, not a literal port of either).
        _pick_primary_and_accent(scored_swatches)
            -> top-scored swatch = primary (cover); the next swatch whose hue is at
               least ~30 degrees from primary's = accent, falling back to the 2nd-
               highest-scored swatch outright if nothing clears that hue-distance bar
               (a monochrome image still needs *some* accent).
        _clamp_for_legibility(rgb, role)
            -> HSL lightness/saturation clamped into a role-specific safe band
               ('cover' vs 'accent' have different constraints -- cover just needs to
               not fight the fixed dark poster text; accent needs to work BOTH as a
               white-on-accent badge background AND as accent-colored text on the
               cream TOC page). Exact band numbers are an implementation-time-tuned
               constant, calibrated against a handful of real test images the way
               Task 2/3 of the original build calibrated PIL's text metrics and the
               accent/cover defaults against real screenshots -- not guessed once and
               left unverified.
  _image_to_data_uri(image_path, max_dimension=1200)
      -> "data:image/<fmt>;base64,..." string, re-encoded through Pillow (so an
         input format Chrome can't render natively still works) and downscaled to
         cap how much the self-contained HTML file grows.
  render_html(..., cover_image_data_uri=None)
      -> when given, fills the cover's currently-empty collage <div> with an <img
         style="object-fit:contain; object-position:right center"> using it,
         mirroring the reference's own (never-delivered) collage-image markup.
  generate_songbook(..., cover_image_path=None)
      -> when given, builds the data URI and passes it to render_html. Does NOT
         itself call extract_cover_and_accent_colors -- callers (GUI dialog, CLI
         main()) resolve accent_color/cover_color BEFORE calling generate_songbook,
         same as they already resolve a swatch name to a hex via
         ACCENT_COLOR_CHOICES/COVER_COLOR_CHOICES today. Keeps generate_songbook
         decoupled from *how* a color was chosen.
  AlbumArtError(RuntimeError) -- unreadable file, corrupt image, zero-byte file, etc.
  _clamp_for_legibility(rgb, role) is reused as-is for a custom-picked color -- it
      does not care whether the rgb it was given came from palette extraction or
      the OS color dialog, so no separate "clamp a custom color" function is needed.

gui.py  (SongbookDialog additions)
  - "Choose Image…" button + small thumbnail preview (CTkImage) + "Clear" button,
    placed as a new row alongside the existing Accent/Cover swatch rows.
  - A "Custom…" button appended to the END of each of the existing accent_row and
    cover_row swatch rows (not a new row -- it lives right next to that role's own
    presets, reinforcing that it's a per-role choice, not a global mode). Opens
    tkinter.colorchooser.askcolor(initialcolor=<that role's current hex>, parent=self);
    on a result, runs it through songbook._clamp_for_legibility(rgb, role), sets
    that role's source to 'custom', stores the clamped hex, and re-purposes that
    same button to show the picked color as its own fg_color (so "Custom…" becomes,
    visually, just one more swatch once a color is set) -- reuses the existing
    _refresh_swatch_selection() bordering mechanism for the "currently selected"
    highlight, extended to treat the Custom button as one more entry in that row's
    button dict rather than a special case.
  - Picking an image runs extract_cover_and_accent_colors() on a background thread
    (never block the GUI thread, same rule as Generate itself) and, on success,
    updates BOTH rows at once: each row's displayed/selected color becomes the
    extracted+clamped hex, but neither row's `_source` is set to a new third value --
    there are still only 'swatch'/'custom' per role (see Decisions above). The
    simplest correct implementation: loading album art calls the exact same
    per-role "set a custom hex" path the Custom… button uses, once per role, with
    the two extracted colors -- so album art is really just "custom, but both
    roles at once, computed instead of picked." On AlbumArtError, shows the error
    in the existing status label -- no new error surface needed.
  - Clicking a swatch OR "Custom…" in a row afterward overrides that row's color;
    the other row's color (from album art or its own prior choice) is untouched.
  - New checkbox "Also show this image on the cover", enabled only while an image
    is loaded; disabled/uncheckable otherwise. Independent of both rows' color
    sources -- can be checked whether the current colors came from that image,
    a different since-cleared image, a swatch, or a custom pick.
  - New persisted keys in songbook_options: album_art_path (str, '' if none),
    show_album_art_on_cover (bool), accent_source/cover_source ('swatch'|'custom'),
    accent_custom_hex/cover_custom_hex (str, only meaningful when that role's
    source is 'custom'). On dialog reopen: if album_art_path is set and the file
    still exists, silently re-run extraction and repopulate the preview and BOTH
    rows' colors (this is the one case where reopening can override a saved
    per-role source, since loading the persisted image is itself the album-art
    action); if the file is gone (moved/deleted), skip that step entirely and
    just restore each row from its own saved source/hex/swatch-name, no error
    shown for a routine missing-file case.

tests/test_songbook.py           -> extract_cover_and_accent_colors()/_clamp_for_legibility()
                                     unit tests against small Pillow-generated synthetic
                                     images (known solid-color halves, a near-white image,
                                     a near-black image) -- deterministic, no real photos
                                     needed for correctness tests. _clamp_for_legibility()
                                     is tested once, generically -- it has no idea whether
                                     its caller is extraction or the custom-color path.
tests/test_songbook_dialog.py    -> file-picker wiring, per-role source switching
                                     (swatch -> custom -> swatch, and album art setting
                                     both rows' custom hex at once), checkbox enable/
                                     disable, persistence -- extraction and
                                     colorchooser.askcolor() are both mocked here, same
                                     pattern Task 6 used for songbook.generate_songbook.
```

## Code Style

Matches `SPEC-karaoke-songbook.md`'s existing conventions, carried forward: 4-space indentation, plain functions (no new classes beyond the dialog's own additions), comments explain *why* (e.g. why MEDIANCUT over FASTOCTREE, why data-URI over copying an asset file), f-strings throughout.

## Testing Strategy

- **Unit-tested** (pure functions, no GUI/Chrome needed): `_load_weighted_palette()`/`_score_swatch()`/`_pick_primary_and_accent()` against small synthetic Pillow images with known, deliberately-constructed color compositions (e.g. a 90%-gray/10%-red image should score red as primary despite gray's larger area, once saturation weighting is applied — assert the *reasoning*, not just "returns two hex strings"). `_clamp_for_legibility()` against edge cases: pure white in, pure black in, already-in-band in (no-op), fully desaturated gray in.
- **Not unit-tested**: whether an arbitrary *real* album cover photo "looks good" — validated manually against a handful of real test images during implementation, same "manual validation for real external/subjective input" precedent the original build used for visual fidelity (Task 3) and for `reencode_to_cfr()`/real fingerprint matching.
- **GUI dialog tests**: mock `songbook.extract_cover_and_accent_colors()` (mirrors how Task 6 mocked `generate_songbook()`) and `tkinter.colorchooser.askcolor()` to test the dialog's own wiring — file picked → thread → both rows' source/hex update → swatches deselect → checkbox enables; Custom… clicked → askcolor() result → that row's source/hex updates → the OTHER row is untouched; askcolor() cancelled (returns `(None, None)`) → no change, no error; error path (`AlbumArtError`) → status label shows it, both rows' sources stay unchanged.
- No CI — local script, run on-demand, matching the rest of this repo.

## Boundaries

- **Always**:
  - Keep `extract_cover_and_accent_colors()` GUI-import-free and independently callable, matching every other `songbook.py` function.
  - Auto-clamp both extracted AND custom-picked colors for legibility — never hand an unreadable raw color straight to `render_html()`, regardless of which input method produced it (confirmed with the user: consistency across all color-entry paths beats letting a custom pick bypass the safety net).
  - Run extraction (and the eventual full generation) on a background thread — never block the GUI. The native `colorchooser.askcolor()` dialog is itself modal/blocking by OS design, which is fine (expected UI behavior for a picker) — the "never block" rule is about avoiding OUR OWN long-running work (image decode/quantize, PDF generation) on the main thread, not about the OS dialog's inherent modality.
  - Fail with a specific, named error (`AlbumArtError`) for a bad image file, shown in the dialog's existing status label — never a silent no-op or an unhandled traceback.
  - Regenerate fully on every Generate click, same as today — no caching of a stale extracted-color result across different images.
  - Treat accent and cover as fully independent settings everywhere in this feature (persistence, UI state, album-art application) — never assume "if one changed, so did the other" outside of the one documented exception (loading an image sets both, because that's what extraction inherently produces).

- **Ask first**:
  - Bumping the Pillow version floor (`>=9.0` → `>=9.1`) to use `Image.Quantize.FASTOCTREE` instead of `MEDIANCUT`, if MEDIANCUT's real-photo results look poor during implementation.
  - Any change to the exact HSL legibility-clamp band numbers once they're chosen and shipped (they're a tuned constant, not meant to be casually adjusted later without re-checking against real images).
  - Changing the default `show_album_art_on_cover` away from unchecked/off.
  - Revisiting the "clamp custom colors too" decision — it was asked and answered explicitly during /spec; don't quietly relax it later because a clamped custom pick looks slightly different from what the user typed in.

- **Never**:
  - Never send the picked image file anywhere over the network (no resolver/API involvement — this is 100% local Pillow processing).
  - Never let a missing/moved album-art file on dialog reopen surface as an error — that's an expected, routine case (fall back to each row's own saved swatch/custom source silently).
  - Never make `generate_songbook()` itself responsible for resolving swatch names, running color extraction, or opening the color-chooser dialog — it only ever takes already-resolved hex strings for accent/cover, keeping that boundary consistent with how it works today.
  - Never let a cancelled `colorchooser.askcolor()` call (user clicked Cancel) change anything — `askcolor()` returns `(None, None)` on cancel, and that must be a no-op, not a fallback-to-black or similar surprise.

## Success Criteria

- Picking an image in the dialog updates both the accent and cover color state to two new hex values within a couple seconds, without freezing the UI.
- Clicking "Custom…" next to a color row opens the native OS color picker (with both a slider/wheel and a hex field) and, on a selection, updates only that row's color — the other row is untouched.
- All three color-entry paths (swatch, album art, custom picker) produce colors that are visually legible when rendered (dark cover text stays readable, white-on-accent badges stay readable, accent-as-text-on-cream stays readable) — verified across a small manual test set spanning a very light cover, a very dark cover, a vividly colored cover, and a deliberately near-white/near-black custom hex pick.
- Clicking a swatch in a row after using album art or a custom pick for that row reverts just that row to the swatch's color, without affecting the other row.
- Checking "Also show this image on the cover" and generating produces a PDF with that image filling the previously-blank collage area; leaving it unchecked leaves that area blank exactly as today.
- `pytest tests/test_songbook.py tests/test_songbook_dialog.py -v` passes, covering the extraction/scoring/clamping logic and the dialog wiring for all three color-entry paths.
- Settings (album art path, show-on-cover checkbox, each row's source and hex/swatch-name) persist independently across app restarts via `self._settings`, same mechanism as every other Generate Songbook setting.

## Open Questions

- Exact HSL clamp band numbers for 'cover' vs 'accent' aren't chosen yet — first implementation pass should pick reasonable starting bands, then verify against a handful of real album-art images AND a few deliberately extreme custom hex picks (pure white, pure black, a very desaturated gray) and adjust, the same "verify against real input before trusting the constant" discipline the original build used for the PIL measurement fudge factor and the accent/cover defaults.
- Whether MEDIANCUT's palette quality is good enough on real photographic album art, or whether the Pillow version floor should be bumped for FASTOCTREE — deferred to implementation, flagged as ask-first above.
- Hue-distance threshold for "primary and accent are different enough" (spec'd as "~30 degrees" above) is a starting guess, not empirically verified yet.
- Whether a clamped custom color should be shown back to the user somehow (e.g. the Custom… button's swatch reflects the *clamped* result, not the exact value they picked in the OS dialog) — leaning yes, for the same reason the button shows the actual color used for generation rather than a value that's now technically wrong, but worth confirming during /plan since it's a slightly different UX moment than album art's "you didn't pick an exact color anyway."
