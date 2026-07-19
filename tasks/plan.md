# Plan: BackstageHero + Library Hygiene Merge

*Spec: [`SPEC.md`](../SPEC.md). Comparison basis: `Projects/clonehero-video-downloader/COMPARISON-BackstageHero.md`. Base: `coheniskool/BackstageHero_LibraryHygiene` @ `5b69ae5` (clean fork, no divergent commits). Source to port: `Projects/clonehero-video-downloader/{clonehero_video_offset.py, dedupe_report.py, chorus_client.py, CH-VideoScript.py}`.*

## Overview

BackstageHero is the base; we add four library-hygiene engines plus a test suite on top, surface them in the GUI, and close BackstageHero's one correctness gap (no VFR handling) inline in the download path. The offset/matching engine (`audiosync.py`) and download flow (`VideoDownload.py`) are **kept as-is** — we hook into them, we don't rewrite them.

## Key integration decisions (grounded in the actual code — read before starting)

These three findings came out of reading the real source and change the work materially (a fourth candidate — "yt-dlp auto-update is a no-op from source" — was considered and dropped from this list: it doesn't change any task's scope or acceptance criteria, since `maybe_update_ytdlp()` was staying in the code regardless of whether it does anything. It's noted inline in Task 1 as a footnote, not called out here):

1. **The resolver is on by default and stays that way (user opted in).** In `resolver_client.py`, `RESOLVER_BASE` defaults to the author's hosted instance (`https://backstage.jimmyproton.co.uk`), and `resolve()`/`report()`/`ping()` run from source (not `_frozen()`-gated). The user has **decided to keep this on** — lookups skip the search for known charts, and confident matches feed the shared pool. Payload is benign (chart hash / video id / offset / anonymous UUID / confidence / artist+title — no paths, no library listing, no personal data), and the GUI "Share matches" toggle still gates the outbound half. **No neutralization needed — this is now a verify-only task** (confirm it works, confirm the toggle still controls reporting, confirm nothing broader than the current payload is ever sent). Note: the *app self-update* channel (`updater.check_app_update`, GitHub exe) is separately `_frozen()`-gated and never fires from source — left as-is. (Same `_frozen()` gating covers `maybe_update_ytdlp()`, the yt-dlp PyPI auto-updater — also a no-op from source, kept in place per the spec, does nothing this phase.)

2. **Reuse BackstageHero's `set_ini_values()` — do NOT port our `patch_song_ini_keys()`.** Both are byte-preserving + atomic (`tmp` + `os.replace`). `VideoDownload.set_ini_values()` already does exactly what metadata enrichment needs. One edge: it returns `False` if there's no `[song]` section — fine, enrichment only targets loadable folders that have one. This avoids shipping two competing ini-writers.

3. **`video.mp4`-only convention simplifies codec handling — but the *existing-library* scan still needs the WebM path.** BackstageHero always writes remuxed AVC `video.mp4`, so *new* downloads are codec-safe and only need **VFR/CFR** handling inline. But the standalone "repair existing library" scan must still detect/remove pre-existing VP9/AV1 WebM (the user's current library has ~455 such files from other tools). So: inline hook = VFR/CFR only; standalone scan = VFR/CFR **and** codec removal. The ported code sheds the `.avi/.ogv/.mkv`/double-extension repair paths (BackstageHero never produces them).

Two more supporting decisions:

4. **Consolidate shared helpers into `library_common.py`.** `find_song_audio`, `find_song_ini`, `find_video_file`, `read_song_ini_fields`, `read_chart_song_fields`, `probe_audio_duration_ms`, `_normalize_for_match`, `parse_folder_name`, `normalize_lookup_value`, and the **`move_to_review()` relocation primitive** (the old code copy-pasted the same-volume/cross-volume/manifest logic into *both* `clonehero_video_offset.py` and `dedupe_report.py` — unify it, parameterized by review-root name).

5. **Hygiene state stays in a per-folder JSON file.** `chart_rename_status` persistence is still needed for resumability *and* for dedupe's keeper-eligibility gate. Keep the `video_meta.json` mechanism (read/merge/write only the `chart_rename_*` keys). Old files from `clonehero-video-downloader` are simply merged into / re-populated on next scan — no migration, per spec.

## Dependency graph

```
Task 1 (verify resolver behavior + payload) ─ independent, light (verify-only)
Task 2 (library_common.py helpers) ─── foundation for 4,5,6,7
Task 3 (BSH regression tests) ─────── locks the base  ── CHECKPOINT 1
        │
        ├─ Task 4 (video_repair.py + inline VFR hook + standalone scan)  ← needs 2
        ├─ Task 5 (chart_rename.py)                                       ← needs 2
        ├─ Task 6 (chorus_client.py + metadata enrichment)               ← needs 2, reuses set_ini_values
        └─ Task 7 (dedupe_report.py)                                     ← needs 2, 6 (chorus), 5 (chart_rename_status)
                                                                            CHECKPOINT 2 (all engines green headless)
Task 8 (GUI "Library Tools" panel + wire all four) ── needs 4,5,6,7
Task 9 (requirements.txt + README + credit)         ── needs all
                                                        CHECKPOINT 3 (manual GUI run + in-game playtest)
```

Tasks 4–7 are independent of each other except 7's soft dependencies (it reads `chart_rename_status` written by 5, and calls `chorus_client` from 6) and can be built in any order after Checkpoint 1.

## Why GUI wiring is one consolidated task (Task 8), not split per feature

The spec makes the GUI the primary surface. Wiring four features into BackstageHero's existing threaded model (`self._queue` / `_poll_queue` / `_dl_thread` / `run_song_with_backoff`) shares one scaffold: a background-thread runner that streams progress messages onto the UI queue and shows a summary. Building that scaffold once and adding four buttons against it avoids four half-integrated threading paths. Task 8 is internally incremental (scaffold + one button first, then the other three), so it stays checkpoint-able.

---

## Phase 0 — Boundary & foundation

### Task 1 — Verify resolver behavior + payload (keep it on)
- **Do**: No code change to the resolver defaults — it stays on. Confirm `resolver_client.enabled()` is `True` by default, that `resolve()` lookups short-circuit the YouTube search for a pool-known chart, and that the GUI "Share matches" toggle actually gates `report()`/`ping()`. Read `report()`/`ping()` once more and confirm the outbound payload is exactly {chart hash, video id, start_ms, anonymous UUID, confidence, artist, title} — nothing broader. Confirm `updater.check_app_update` (GitHub exe channel) stays `_frozen()`-gated so it never fires from source; leave `maybe_update_ytdlp()` in place (no-op from source, same `_frozen()` gating — see finding 1 above).
- **Acceptance**: A default `python gui.py` run performs community lookups and (with Share on) reports confident matches; turning "Share matches" off stops all outbound `report()`/`ping()` while lookups still work; no payload field beyond the documented set is ever sent; no app-self-update request fires from source.
- **Verify**: Point the app at a small test library with `report`/`ping`/`resolve` wrapped by a recording stub; assert lookups happen, assert the toggle gates reporting, assert the recorded report body matches the documented field set exactly, assert no `api.github.com` release call.

### Task 2 — `library_common.py` shared helpers
- **Do**: Create `library_common.py` housing the shared helpers (finding 4), ported from `clonehero_video_offset.py`/`CH-VideoScript.py` and reformatted to 4-space/f-string style. Unify the duplicated relocation logic into one `move_to_review(song_dir, home_folder, review_root_name, reason, extra_manifest_fields=None, dry_run=False)` with the same-volume `shutil.move` / cross-volume copy-verify-rmtree + JSONL manifest behavior from both old modules.
- **Acceptance**: All helpers importable with no dependency on `CH-VideoScript.py` or the old repo; `move_to_review` reproduces both old behaviors (needs_review and duplicates_review) via its `review_root_name`/`extra_manifest_fields` params.
- **Verify**: `tests/test_library_common.py` — file-discovery against both plain and ID-suffixed fixtures (incl. the no-audio → `None` case), `read_song_ini_fields`/`read_chart_song_fields` parsing, and a same-volume + a simulated cross-volume `move_to_review` with manifest assertions.

### Task 3 — Lock the BackstageHero base with regression tests
- **Do**: Add `tests/test_audiosync_sign.py` (synthetic reference clip + a copy delayed by a known `ffmpeg -itsoffset`, assert `compute_offset_ms` sign/magnitude — the same method our old `test_compute_offset_sign.py` used) and `tests/test_set_ini_values.py` (byte-preservation: only the target key changes, CRLF/LF preserved, comments/order intact, missing-key insertion, no `[song]` → `False`).
- **Acceptance**: Both pass against BackstageHero's unmodified code, empirically confirming the sign convention matches ours (both negate, both default `-3000`) before any hygiene code depends on it.
- **Verify**: `pytest tests/test_audiosync_sign.py tests/test_set_ini_values.py -v` green.

> **CHECKPOINT 1** — Resolver behavior confirmed (lookups work, toggle gates reporting, payload is the documented benign set); shared helpers + the base engine's sign convention and ini-writer are under test. Nothing hygiene-specific written yet. Stop, review, confirm the base is trustworthy before building on it.

---

## Phase 1 — Hygiene engines (headless, each independently tested)

### Task 4 — `video_repair.py` + inline VFR hook + standalone scan
- **Do**: Port `probe_frame_rate()`, `reencode_to_cfr()`, `probe_video_codec()` into `video_repair.py`. Add `ensure_playable(video_path, *, allow_codec_removal)`: always VFR-detect → CFR re-encode in place (atomic tmp + `os.replace`); when `allow_codec_removal` (standalone scan only), also remove non-VP8 WebM. Hook the **inline** path into `VideoDownload.download_video()` right after `video.mp4` is finalized (VFR/CFR only — AVC is already codec-safe). Add `scan_and_repair_video_library(home_folder)` for the standalone pass (VFR/CFR + codec removal), following BackstageHero's aggregate/summary print style.
- **Acceptance**: A VFR download is transparently CFR-re-encoded before it's considered done; a pre-existing VP9/AV1 WebM in the library is removed by the standalone scan (and the song then re-downloads on the next run, since it now has no video). New AVC downloads are never needlessly re-encoded when already CFR.
- **Verify**: `tests/test_video_repair.py` — `probe_frame_rate`/`probe_video_codec` against canned `ffprobe` JSON (VFR vs CFR, vp8 vs vp9); `reencode_to_cfr` via mocked `subprocess` (atomic-replace happens, tmp cleaned on failure). Manual: run one real VFR clip through `ensure_playable` and confirm CFR output.

### Task 5 — `chart_rename.py`
- **Do**: Port the chart-name repair suite from `clonehero_video_offset.py`: `scan_song_folder_chart_names`, `read_chart_song_fields`/`verify_chart_content_match`, `scan_song_folder_audio_stems`, `scan_song_folder_album_art`, `is_sng_packaged`, `process_chart_folder_names`, `process_song_folder_for_chart_rename`, `scan_and_fix_chart_library`, plus `load/save_chart_rename_status` (writing to the per-folder JSON, finding 5). Use `library_common.move_to_review(..., '_needs_review', ...)`. Reformat to 4-space/f-string.
- **Acceptance**: ID-suffixed `song_NNNN.ini`/`notes_NNNN.chart`/stems/album-art are renamed only after content verification (fuzzy Name+Artist for `.chart`, duration-vs-`song_length` for `.mid`); unverifiable/ambiguous/collision cases relocate intact to `_needs_review/` with a manifest; `.sng` folders are skipped; `confirmed_ok` folders are skipped on rerun.
- **Verify**: `tests/test_chart_rename.py` — synthetic fixtures for each status (`ok`/`id_suffixed`/`ambiguous`/`no_ini`/`no_chart_file`/`needs_review`/`skipped_sng`/`skipped_settled`), the collision guard, and dry-run touching zero files.

### Task 6 — `chorus_client.py` + metadata enrichment
- **Do**: Port `chorus_client.py` as-is (`requests`, POST `/search/advanced`). Port `sanitize_chorus_field`, `_chorus_match_confidence`, `fill_song_ini_metadata`, `enrich_song_ini_metadata_library` — but **write via `VideoDownload.set_ini_values()`** (finding 2), not a ported `patch_song_ini_keys`. Preserve the blank-only fill rule, the `CHORUS_MATCH_CONFIDENCE_THRESHOLD = 70` gate, and `sanitize_chorus_field`'s reject-unsafe-chars behavior.
- **Acceptance**: Blank `year/genre/charter/album` fields fill from a confident (≥70) Chorus match; populated fields are never overwritten; unsafe values are rejected, not partially cleaned; a single lookup failure never aborts the library run; `--dry-run`/dry mode writes nothing.
- **Verify**: `tests/test_metadata_enrichment.py` — mocked `chorus_client.search_by_artist_title` for filled/no_change/no_match/error paths; `sanitize_chorus_field` rejects `[`,`]`,`;`,`#`,newlines; confirm `set_ini_values` is the writer and only blank fields change.

### Task 7 — `dedupe_report.py`
- **Do**: Port `dedupe_report.py`, repointing its imports at `library_common` and `chorus_client` (dropping the `importlib` load of `CH-VideoScript.py`). Keep AcoustID guarded-import, `group_candidates` (version-tag-aware), `confirm_group` (fingerprint), `score_folder`/`select_keeper`/`is_keeper_eligible`, `flag_borrow_candidates`, and `move_to_duplicates_review` → `library_common.move_to_review(..., '_duplicates_review', extra_manifest_fields={'score': ...})`. Adapt `_build_score_inputs` to BackstageHero's `video.mp4`-only presence check and its song.ini keys.
- **Acceptance**: Fuzzy candidate groups are fingerprint-confirmed before any move; only `chart_rename_status == 'confirmed_ok'` folders are keeper-eligible (unscanned == needs_review); losers relocate to `_duplicates_review/` with score + borrow-candidate flags; nothing is ever deleted; without `fpcalc`/`pyacoustid` it reports candidates but confirms/moves nothing.
- **Verify**: `tests/test_dedupe.py` — `group_candidates` version-tag separation + `[dupN]` suffix stripping, `score_folder` weighting, `select_keeper` eligibility precondition (returns `None` when all ineligible), `flag_borrow_candidates`; `confirm_group` with mocked `acoustid`.

> **CHECKPOINT 2** — All four hygiene engines built and green headless (callable directly, no GUI). Run the full `pytest tests/ -v` suite. Stop, review coverage and real-fixture realism before touching the GUI.

---

## Phase 2 — Surface & finish

### Task 8 — GUI "Library Tools" panel (wire all four features)
- **Do**: Add a "Library Tools" surface to `gui.py` (menu or panel) with actions: **Repair videos** (VFR/CFR + codec scan), **Fix chart names**, **Enrich metadata**, **Find duplicates** — each with a dry-run toggle where destructive (chart-rename, dedupe), running off the UI thread via the existing `self._queue`/`_poll_queue` pattern, streaming progress and a final summary + counts. Build the shared threaded-runner scaffold with the first button, then add the other three. Reuse the existing "Large batch" confirm pattern for destructive scans over big libraries.
- **Acceptance**: Each tool runs from the GUI without freezing the window, shows live progress and a summary, respects dry-run, and never blocks a download run (mutually exclusive with `self._running` where appropriate).
- **Verify**: Manual — run each of the four tools from the GUI against a copied test library; confirm dry-run changes nothing, real runs produce the expected relocations/fills/removals, and the window stays responsive with a Stop that works.

### Task 9 — `requirements.txt`, README, attribution
- **Do**: Add `requests` and `pyacoustid` to `requirements.txt` (note `fpcalc` as a manual PATH binary, like ffmpeg). Update README to describe the merged tool, document the four Library Tools, note the community resolver is on by default (with what it shares) and how to use the "Share matches" toggle, and **preserve BackstageHero's MIT license + credit to `jmb988`**.
- **Acceptance**: A clean `pip install -r requirements.txt` + ffmpeg/fpcalc on PATH runs everything; README accurately reflects the merged feature set and attribution.
- **Verify**: Fresh venv install; `python gui.py` launches and every documented feature is reachable.

> **CHECKPOINT 3** — Full manual GUI run end-to-end: download+auto-sync a missing video, then run each hygiene tool. **Required in-game playtest**: load one auto-synced song in Clone Hero and confirm the video is in sync (an offset write is never trusted until confirmed in-game — same discipline as the old project).

---

## Risks & watch-items

- **Resolver payload scope**: the resolver is intentionally on (user opted in, payload is benign). The residual risk is *scope creep* — a future change widening what's reported. Task 1 pins the exact field set, and the Boundaries "Never" rule forbids widening it without a new decision.
- **audiosync sign mismatch**: low risk (documented as matching), but Task 3 empirically locks it before any auto-sync write path is trusted.
- **`set_ini_values` vs suffixed .ini**: it targets literal `song.ini`; enrichment assumes loadable folders (post-chart-rename). If enrichment is ever run on an un-repaired library, a suffixed-only folder is skipped (returns `False`), not corrupted — acceptable, note in README ordering guidance (repair names → enrich).
- **Cross-volume moves on the user's real library**: the copy-verify-before-delete path is preserved from the old code and unit-tested, but the real library may span volumes (`M:\`); Checkpoint 3 should include one real relocation on the actual setup.
- **fpcalc absence**: dedupe degrades to report-only, never errors — preserved from the old design.
