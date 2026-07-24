# Spec: Performance & Simplification Pass

## Description

A repo-wide pass that removes redundant file/directory I/O, subprocess spawns, and network round-trips found during a code-simplification review (see review notes below), and fixes one algorithmic hot spot (O(n²) dedupe matching). The goal is fewer disk reads, fewer subprocess spawns, fewer serial network calls, and fewer SQL queries per operation — with correctness preserved and any resulting behavior shift explicitly documented, not accidental.

This is not a rewrite: every change is a targeted fix to a specific finding below, in the module it lives in. No new abstractions beyond what's needed to eliminate the duplication (e.g. a shared "read this folder's files/ini once" helper where 3+ call sites currently each do their own I/O).

## Objective

**User**: solo hobbyist running this against a real library of ~5,000+ songs (`M:\_Organized\Songs`). The GUI, resolver, and hygiene tools (chart-rename, dedupe, metadata enrichment, video repair) are all in daily/occasional use per `SPEC.md`.

**Success looks like**: the four Library Tools (repair videos, fix chart names, enrich metadata, find duplicates) and the GUI's CSV export/song-selection paths run measurably faster against the real library — fewer redundant `.ini` re-parses, fewer duplicate `glob()`/`ffprobe` calls, no full O(n²) fuzzy-match pass in dedupe, and independent per-song network/subprocess work (Chorus lookups, video-repair probes) runs concurrently instead of serially — without breaking any existing pytest coverage or the resolver's payload/behavior guarantees from `SPEC.md`.

**In scope**: all findings from the review (see "Findings in scope" below) — high, medium, and low impact — across `gui.py`, `VideoDownload.py`, `library_common.py`, `chart_rename.py`, `static_art.py`, `dedupe_report.py`, `video_repair.py`, `metadata_enrichment.py`, `resolver/db.py`, `resolver/app.py`, `resolver_client.py`. `build.py`/`make_brand.py` (build-time only, not a runtime hot path) are noted but deprioritized.

**Out of scope**: no new features, no UI redesign, no change to the resolver's reported payload fields or the community-resolver on-by-default behavior locked in `SPEC.md`. No dependency additions beyond stdlib `concurrent.futures` (already available).

## Tech Stack

No changes. Same as `SPEC.md`: Python 3.x, `customtkinter`, `yt-dlp`, `ffmpeg`/`ffprobe`, `numpy`, `requests`, `pyacoustid`, stdlib `sqlite3` (resolver), stdlib `concurrent.futures` for the new thread-pool work (no new dependency).

## Commands

```
Run:    python gui.py
Test:   pytest tests/ -v          (must stay green throughout — see Testing Strategy)
Dedupe: python dedupe_report.py --library-path <path> [--dry-run]
```

## Findings in scope

Grouped by theme; each is a distinct, independently-testable change.

### A. Redundant per-folder file/ini I/O (root-cause consolidation)
1. `VideoDownload.py:951` `_read_ini_value` — opens+parses the whole ini file per single key lookup. Add a "parse once, return dict/section" helper; keep `_read_ini_value` as a thin wrapper for single-key callers that don't need to change.
2. `gui.py:1591-1633` CSV export — re-parses each song's `.ini` 6-7× (once per field), on the UI thread, including via a single "Dump video" right-click. Fix via (1).
3. `gui.py:1626-1627` — `_video_kind()` and a direct `_read_song_value()` call both read `backstagehero_video_title` from the same file back-to-back. Read once, derive both.
4. `gui.py:497-510` `SyncEditor._read_offset` — hand-rolled line scan for `video_start_time` duplicating `_read_ini_value`. Reuse the shared reader.
5. `library_common.py:296-310` `find_song_audio` — up to 5 separate `glob()` calls per song. Consolidate to one directory listing, matched in memory.
6. `library_common.py:328-335` `find_video_file` — 4 separate `.exists()` stats. Fold into the same shared directory-listing pass as (5).
7. `library_common.py:267-293` `iter_song_folders` — each container directory listed twice (once by the walk, once by `looks_like_song_folder`). Share one listing.
8. `library_common.py:338-355` `read_song_ini_fields` — one `re.search` per requested key (19 keys × 19 scans in dedupe's `_build_score_inputs`). Single-pass parse into a dict, then look up keys.
9. `chart_rename.py:490-505` `process_chart_folder_names` — re-globs `*.ini` and re-derives notes candidates already computed moments earlier. Thread the resolved `Path` objects through instead of re-deriving.
10. `chart_rename.py:608` `process_song_folder_for_chart_rename` — independent third `*.ini` glob via `library_common.find_song_ini`, for a folder already scanned in (9). Reuse the already-resolved path.
11. `chart_rename.py:200-208` / `296-303` — `scan_song_folder_audio_stems` and `apply_stem_renames` each independently `iterdir()` and build an identical `by_role` dict. Extract to one shared helper.
12. `chart_rename.py:463-471` `is_sng_packaged` — one more independent glob on top of (9)/(11). Fold into the shared per-folder listing where practical.
13. `VideoDownload.py:838-840` + `1075-1099` — resolution probe (`_probe_and_store_resolution`) does a second independent read-modify-write of `song.ini` right after the main `set_ini_values()` write. Probe resolution before the first write and fold `backstagehero_res` into the same `values` dict.

### B. Duplicate subprocess spawns
14. `static_art.py:415,439` `convert_to_album_art` — `probe_static_video` computes duration via `_probe_duration_and_bitrate`, then a second independent call to the same function re-derives it when no existing art is found. Return/cache duration from the first call.
15. `video_repair.py:171-173` — `ensure_playable` spawns two separate `ffprobe` calls (codec probe + frame-rate probe) per video. Merge into one `ffprobe` call requesting both `codec_name` and `r_frame_rate`/`avg_frame_rate` together.

### C. Algorithmic hot spot
16. `dedupe_report.py:95-112` `group_candidates` — full O(n²) pairwise `SequenceMatcher` comparison over the whole library (~13M comparisons at real-library scale). Add a cheap blocking key (e.g. first-letter/length bucket of `title_norm`) computed once per folder, and only fuzzy-compare within a bucket.
   - **Documented behavior shift**: candidates whose normalized titles differ enough to land in different buckets will no longer be fuzzy-compared, even if `SequenceMatcher.ratio()` would have scored them as similar (e.g. a leading-character typo). The blocking key must be chosen conservatively (e.g. first 1-2 normalized chars, not an exact prefix) to keep this a rare, acceptable edge case — call out the chosen key and its miss rate in the implementation.

### D. Serial network/subprocess work → concurrent (per user decision: in scope)
17. `metadata_enrichment.py:138-140` `enrich_song_ini_metadata_library` — one blocking Chorus HTTPS call per song, fully serial. Run via a bounded `ThreadPoolExecutor` (5-10 workers).
18. `video_repair.py` library scan (`scan_and_repair_video_library`) — per-video `ensure_playable` (after fix #15, one `ffprobe` call) run serially across the library. Thread-pool the per-video work.
   - **Documented behavior shift**: progress/summary output for both (17) and (18) will no longer print in strict folder-scan order — songs complete as their thread finishes, not left-to-right. The final aggregate counts/summary dict (already returned per `tasks/todo.md` Task 8/9 notes) must remain order-independent and correct; only interleaved *progress* messages during the run may reorder.

### E. Resolver query/latency consolidation
19. `resolver/db.py:239-259` `client_stats()` — 5 separate `COUNT(*)` queries. Consolidate into one query with conditional `SUM(CASE WHEN ...)`.
20. `resolver/db.py:262-273` `stats()` — 3 separate scalar queries against `mappings`. Consolidate into one `SELECT COUNT(*), SUM(status='approved'), SUM(status='pending')`.
21. `resolver/db.py:214-222` `list_pending()` — unindexed `status` column behind `NOT IN`. Add `CREATE INDEX idx_mappings_status ON mappings(status)` (or rewrite as `NOT EXISTS`).
22. `resolver/db.py:155-161` `record_vote()` — re-queries `status` immediately after `_refresh_mapping()` computed and wrote it. Have `_refresh_mapping()` return the status string directly.
23. `resolver/db.py:92-97` `_refresh_mapping()` — pulls every vote row into Python to dedupe by `ip_hash` on every insert (O(n) per vote). Push the distinct-count to SQLite (`SELECT COUNT(DISTINCT ip_hash)`).
24. `resolver/app.py:200-201,216-217` `admin_set`/`admin_offset` — `_purge_resolve()` blocks the request handler on a Cloudflare API round-trip (up to 5s). Move to a background thread (`threading.Thread(..., daemon=True).start()`); purge success isn't required for the HTTP response.
   - **Documented behavior shift**: a purge failure will no longer be visible in the admin response — it becomes fire-and-forget. Must still be logged server-side so failures aren't silently lost.
25. `resolver_client.py:71-85` `_client_id()` — reads the client-id file from disk on every `ping()`/`report()` call. Cache in a module-level variable after first read.

### Deprioritized (build-time only, not a runtime hot path)
26. `make_brand.py` `_vgrad()` — per-row Python `putpixel` loop; only matters if brand assets are regenerated frequently (they aren't). Not required for this pass; may be picked up opportunistically.

## Project Structure

No new files except possibly a small shared helper inside `library_common.py` for the "list this folder's files once" pattern used by findings 5-13. No new top-level modules.

## Code Style

Match existing conventions already locked in `SPEC.md`: 4-space indentation, f-strings, atomic ini writes (temp file + `os.replace`) preserved wherever a write path is touched (finding 13). Thread-pool code (findings 17, 18, 24) follows the existing pattern already in `gui.py`'s `_probe_resolutions` (`ThreadPoolExecutor`), not a new concurrency style.

## Testing Strategy

- **Bar**: existing `pytest tests/` suite (180+ tests per `tasks/todo.md`) must stay green throughout. Behavior may shift where explicitly documented above (findings 16, 18, 17, 24) — those shifts get a regression test asserting the *new* documented behavior, not the old one.
- Each finding with no existing coverage over the touched function gets a small regression test (before/after-equivalent output) added alongside the fix, in the same style as the existing `tests/test_*.py` files for that module.
- Concurrency changes (17, 18, 24) get a test asserting: (a) all items still get processed (no dropped work), (b) the final aggregate result is correct regardless of completion order, (c) a single item's failure doesn't abort the batch (already a stated invariant for enrichment in `SPEC.md`/`tasks/todo.md`).
- The dedupe blocking-key change (16) gets a test with a deliberately near-duplicate pair that spans two adjacent buckets, asserting the chosen bucketing strategy doesn't silently drop a real duplicate at the boundary the implementation picks.
- No CI — local, run on-demand, same as `SPEC.md`.

## Boundaries

- **Always**:
  - Keep the resolver's on-by-default behavior and exact reported payload unchanged (per `SPEC.md` Boundaries) — findings 19-25 touch resolver *server* internals (DB queries, purge timing), never the client payload contents.
  - Preserve `song.ini` atomic-write discipline (temp file + `os.replace`) on every touched write path (finding 13).
  - Keep every existing pytest test passing, or replace it with an updated test that reflects a documented behavior shift from this spec — never leave a shift undocumented or a test silently deleted.
  - Preserve the "one failed item never aborts the batch" invariant when adding concurrency (17, 18).
- **Ask first**:
  - Any additional behavior shift discovered mid-implementation that isn't already listed above.
  - Changing the thread-pool worker count defaults if profiling suggests the initial guess (5-10) is wrong.
- **Never**:
  - Never widen the resolver's reported payload or change its on-by-default posture.
  - Never remove or weaken the atomic-write guarantee on `song.ini`.
  - Never silently drop a real duplicate/match to gain speed (finding 16's blocking key must be conservative, not lossy in the common case).

## Success Criteria

- [ ] All 25 in-scope findings (A-E) addressed, each as an independently reviewable change
- [ ] `pytest tests/ -v` green, including new/updated regression tests for every touched function
- [ ] Documented behavior shifts (dedupe bucketing, concurrent-scan ordering, fire-and-forget purge) each have an explicit test proving the new behavior is correct and intentional
- [ ] No change to resolver payload fields or on-by-default behavior
- [ ] Manual spot-check: CSV export and Library Tools (repair videos, fix chart names, enrich metadata, find duplicates) run against a real/copied library folder without regressions

## Resolved Decisions

- **Scope**: all findings (high + medium + low), not just high-impact — user chose the full pass.
- **Concurrency**: in scope — thread-pool the independent per-song network/subprocess calls (Chorus lookups, video-repair probes), accepting the ordering/fire-and-forget shifts that come with it.
- **Correctness bar**: behavior may shift from this pass where the shift is documented in the spec and covered by a test — not a strict zero-behavior-change bar.

## Notes

- This spec's findings came from a three-way parallel code review (gui.py+VideoDownload.py; library_common.py+chart_rename.py+static_art.py+dedupe_report.py; resolver+updater+sync utils), each agent reading the relevant files in full and reporting concrete file:line findings. Line numbers are accurate as of the review pass on 2026-07-20 and should be re-verified at implementation time since prior findings in this same task list (e.g. the chart-rename `.ini`/`.chart` collision fix) have shown line numbers drift as fixes land.
