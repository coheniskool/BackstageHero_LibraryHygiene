# Plan: Performance & Simplification Pass

*Spec: [`SPEC-perf-simplification.md`](../SPEC-perf-simplification.md). Findings A-E (25 items) come from a three-way parallel code review, referenced by number below (A1-A13, B14-B15, C16, D17-D18, E19-E25).*

## Overview

Two independent foundation fixes (VideoDownload.py's ini-read helper, library_common.py's shared folder-listing helper) unlock most of the high-severity findings, since chart_rename.py and gui.py's redundant I/O is downstream of them. The algorithmic (dedupe), subprocess-dedup (static_art/video_repair), concurrency (enrichment/video-repair-scan), and resolver-server (db/app/client) work are each independent of the foundation and of each other — they're grouped into vertical, independently-testable tasks by file/module, not by finding-theme, so each task lands with its own green test run.

## Dependency graph

```
Task 1 (VideoDownload.py ini-read consolidation: A1, A13)  ─── foundation for Task 2
Task 3 (library_common.py shared folder listing: A5,6,7,8) ─── foundation for Task 5
        │                                                         │
Task 2 (gui.py consolidation: A2,3,4)  ←── needs Task 1            │
Task 5 (chart_rename.py consolidation: A9,10,11,12)  ←── needs Task 3
Task 6 (static_art.py: B14)             ─── independent
Task 7 (video_repair.py probe merge: B15) ─── independent, feeds Task 10
Task 8 (dedupe_report.py blocking key: C16) ─── independent, soft-benefits from Task 3
                                                                    CHECKPOINT 1 (all of the above green)
Task 9 (metadata_enrichment.py concurrency: D17)   ─── independent
Task 10 (video_repair.py scan concurrency: D18)    ←── needs Task 7
                                                                    CHECKPOINT 2
Task 11 (resolver/db.py query consolidation: E19-23) ─── independent
Task 12 (resolver/app.py purge threading: E24)       ─── independent
Task 13 (resolver_client.py client-id cache: E25)    ─── independent
                                                                    CHECKPOINT 3 (final — full suite + manual spot-check)
```

Tasks 6, 7, 8, 9, 11, 12, 13 have no dependency on each other or on Tasks 1-5 and can be built in any order once started. Task 10 should follow Task 7 (merging the two ffprobe calls first means the concurrency change parallelizes one call per video instead of two).

---

## Phase 0 — Foundation (shared I/O helpers)

### Task 1 — `VideoDownload.py` ini-read consolidation
- **Model**: Sonnet 5 — moderate consolidation touching a write path; needs care but not open-ended reasoning.
- **Do**: Add a helper (e.g. `_read_ini_section(folder)`) that parses `song.ini` once and returns a dict/section object. Keep `_read_ini_value(folder, key)` as a thin wrapper over it for existing single-key callers so no call site needs to change unless it's making 2+ calls for the same file (those get updated to call the new helper directly). Apply to finding A13: have `_probe_and_store_resolution` compute `backstagehero_res` *before* the main `set_ini_values()` call in the download path, folding it into the same `values` dict instead of a second read-modify-write.
- **Acceptance**: Any code path reading 2+ keys from the same `song.ini` does exactly one parse. Downloading a song writes `song.ini` exactly once (not twice) for the base fields + resolution.
- **Verify**: `tests/test_video_repair.py`/existing ini tests still pass; add a test asserting `_read_ini_section` is called once (not per-key) when multiple keys are requested from the same folder, and a test on the download path asserting a single `set_ini_values()` call covers both base fields and `backstagehero_res`.

### Task 3 — `library_common.py` shared folder-listing helper
- **Model**: Sonnet 5 — foundational but mechanical: consolidate existing logic into one helper, no new algorithm.
- **Do**: Add a helper (e.g. `list_song_folder_files(song_dir)`) that does one `iterdir()`/`scandir()` pass and returns the filenames, used by `find_song_audio` (A5), `find_video_file` (A6), and `iter_song_folders`'s `looks_like_song_folder` check (A7) instead of each doing its own independent listing. Rewrite `read_song_ini_fields` (A8) to parse the ini text once and build a dict of all present keys, then look up only the requested subset, instead of one `re.search` per key.
- **Acceptance**: `find_song_audio`/`find_video_file` each do one directory listing per call, not up to 5+4. `iter_song_folders`'s walk lists each container directory once, not twice. `read_song_ini_fields` does one regex/parse pass regardless of how many keys are requested.
- **Verify**: `tests/test_library_common.py` (existing 29+ tests) stay green; add a test asserting the listing helper is called once per folder across a combined `find_song_audio` + `find_video_file` call sequence, and a test confirming `read_song_ini_fields` with N keys still returns correct values in one pass (compare output to the old per-key behavior on the same fixture).

---

## Phase 1 — Downstream consolidation + independent fixes

### Task 2 — `gui.py` consolidation (needs Task 1)
- **Model**: Sonnet 5 — straightforward call-site rewiring onto the Task 1 helper, moderate GUI-code familiarity needed.
- **Do**: Fix CSV export (A2) to use the Task 1 `_read_ini_section` helper instead of 6-7 separate reads per song — read each song's ini once, derive all CSV columns (including the duplicate `_video_kind`/title read, A3) from that one read. Fix `SyncEditor._read_offset` (A4) to call the shared ini reader instead of its own hand-rolled line scan.
- **Acceptance**: CSV export does one ini parse per song, not 6-7. `_video_kind()` and the title column share one read. `SyncEditor` no longer has a divergent ini parser.
- **Verify**: Existing GUI/library-tools tests (per `tasks/todo.md` Task 8 verification method — real Tk construction, worker functions called directly) still pass; add a test exporting a small synthetic library's CSV and asserting the ini-read count (via a call-counting stub) drops from N×6-7 to N×1.

### Task 5 — `chart_rename.py` consolidation (needs Task 3)
- **Model**: Opus 4.8 — this file carries known TOCTOU collision guards and a history of subtle real-library bugs (per `tasks/todo.md`'s "unreachable via fixture" and stem-rename findings); the file-fragility signal flags this module, so the higher reasoning tier is worth it despite the change itself being mechanical.
- **Do**: Fix A9 by threading the already-resolved `Path` objects from `scan_song_folder_chart_names` through to `process_chart_folder_names`'s `id_suffixed` branch instead of re-globbing/re-deriving notes candidates. Fix A10 by having `process_song_folder_for_chart_rename` reuse the ini path already resolved during the scan instead of a third independent `find_song_ini` glob. Fix A11 by extracting the shared `by_role` matching loop out of `scan_song_folder_audio_stems`/`apply_stem_renames` into one helper both call. Fix A12 by folding the `is_sng_packaged` glob into the same per-folder listing pass from Task 3 where practical.
- **Acceptance**: A single chart-rename pass over one folder does one `*.ini` glob, one notes-candidate derivation, one `by_role` scan — not 2-3x each. Existing collision-guard/TOCTOU tests (per `tasks/todo.md` notes) still pass unchanged, since those are deliberately-unreachable-by-fixture defensive paths, not touched by this consolidation.
- **Verify**: `tests/test_chart_rename.py` (existing 35+ tests) green; add a call-counting test on the real Kryptonite-shaped multi-file fixture (per `tasks/todo.md`'s prior real-library bug fix) asserting glob/listing call counts drop.

### Task 6 — `static_art.py` duplicate ffprobe fix (B14)
- **Model**: Haiku 4.5 — small, mechanical, single-function fix (return/cache an already-computed value).
- **Do**: Have `probe_static_video` return (or cache) the duration it already computed via `_probe_duration_and_bitrate`, and have `convert_to_album_art` reuse that value instead of calling `_probe_duration_and_bitrate` a second time when no existing art is found.
- **Acceptance**: Converting one video to album art spawns one `ffprobe` duration probe, not two.
- **Verify**: `tests/test_static_art.py` green; add a test with a mocked/counted `subprocess` call asserting exactly one duration probe per conversion.

### Task 7 — `video_repair.py` probe merge (B15)
- **Model**: Haiku 4.5 — mechanical merge of two known `ffprobe` invocations into one; existing canned-JSON tests bound the change.
- **Do**: Merge the codec probe and frame-rate probe into one `ffprobe` invocation requesting `codec_name,r_frame_rate,avg_frame_rate` together, replacing the two separate subprocess calls in `ensure_playable`.
- **Acceptance**: `ensure_playable` spawns one `ffprobe` call per video instead of two; VFR-vs-CFR and codec detection both still work off the single call's output.
- **Verify**: `tests/test_video_repair.py` (existing 15+ tests, canned ffprobe JSON) updated to the merged-call shape; assert both VFR and codec detection still classify correctly from one canned response.

### Task 8 — `dedupe_report.py` blocking-key fix (C16)
- **Model**: Opus 4.8 — the plan's own risk note flags this as the one task that can silently regress *correctness* (a missed duplicate), not just speed; choosing and justifying the blocking key, plus designing the boundary test, needs the highest reasoning tier available.
- **Do**: Compute a conservative blocking key per folder (e.g. first 2 normalized chars of `title_norm`, or a length bucket — pick and document the exact key in code comments) once before the matching loop, then only run `SequenceMatcher.ratio()` between folders sharing a bucket, instead of the current full O(n²) pass.
- **Acceptance**: `group_candidates` no longer compares every pair; a real near-duplicate pair (e.g. differing only by a trailing `[dup1]` or minor typo within the same bucket) is still grouped correctly.
- **Verify**: `tests/test_dedupe.py` (existing 23+ tests) green; add the boundary test called for in the spec — a deliberately near-duplicate pair whose normalized titles land in adjacent-but-different buckets under the chosen key, asserting the implementation's bucketing choice doesn't silently drop it (tune the key width until this passes, per the spec's "conservative, not lossy" boundary).

> **CHECKPOINT 1** — Tasks 1, 2, 3, 5, 6, 7, 8 done. Run full `pytest tests/ -v`. All I/O-consolidation and the algorithmic dedupe fix are in and green before touching concurrency or the resolver server. Stop, review diff size/shape before Phase 2.

---

## Phase 2 — Concurrency (documented behavior shift: completion/progress ordering)

### Task 9 — `metadata_enrichment.py` concurrency (D17)
- **Model**: Sonnet 5 — a known thread-pool pattern already exists in `gui.py` (`_probe_resolutions`) to mirror; the reasoning load is moderate (preserve the abort-invariant, write a deterministic concurrency test), not open-ended.
- **Do**: Run `fill_song_ini_metadata`/Chorus lookups via a bounded `ThreadPoolExecutor` (5-10 workers) in `enrich_song_ini_metadata_library`, matching the existing `gui.py` `_probe_resolutions` thread-pool pattern. Preserve the "one song's failure doesn't abort the batch" invariant.
- **Acceptance**: All songs still get processed; final aggregate counts dict is correct regardless of completion order; a single Chorus lookup failure/exception doesn't stop the rest of the batch.
- **Verify**: `tests/test_metadata_enrichment.py` (existing 21+ tests) green; add a concurrency test with mocked Chorus calls (one slow, one erroring, several fast) asserting all complete, the error doesn't propagate/abort, and the final summary counts match a fully-serial run on the same input.

### Task 10 — `video_repair.py` scan concurrency (D18, needs Task 7)
- **Model**: Sonnet 5 — same pattern as Task 9, applied to the file Task 7 already touched; low novelty.
- **Do**: Thread-pool the per-video `ensure_playable` calls in `scan_and_repair_video_library`, same pattern as Task 9.
- **Acceptance**: All videos still get scanned/repaired; final counts dict correct regardless of order; one video's ffmpeg failure doesn't abort the scan.
- **Verify**: `tests/test_video_repair.py` concurrency test mirroring Task 9's shape (mocked subprocess calls, mixed success/failure/slow, assert aggregate correctness).

> **CHECKPOINT 2** — Tasks 9, 10 done. Full `pytest tests/ -v` green including new concurrency tests. Manual optional: time a real library-tools run before/after on a copied library subset to confirm the wall-clock win is real, not just theoretical.

---

## Phase 3 — Resolver server (independent module, own test surface)

### Task 11 — `resolver/db.py` query consolidation (E19-23)
- **Model**: Opus 4.8 — five SQL rewrites (including a schema migration and a `NOT IN`→`NOT EXISTS`/index tradeoff) against a module with an unconfirmed test-coverage gap; likely needs to design a new test harness from scratch, which is higher-ambiguity work than the other Phase 3 tasks.
- **Do**: E19 — collapse `client_stats()`'s 5 `COUNT(*)` queries into one with conditional `SUM(CASE WHEN ...)`. E20 — collapse `stats()`'s 3 scalar queries into one. E21 — add `CREATE INDEX idx_mappings_status ON mappings(status)` (schema migration, additive) or rewrite `list_pending()`'s `NOT IN` as `NOT EXISTS`. E22 — have `_refresh_mapping()` return the computed status string so `record_vote()` doesn't re-query it. E23 — push the distinct-voter count in `_refresh_mapping()` to `SELECT COUNT(DISTINCT ip_hash)` instead of pulling all rows into Python.
- **Acceptance**: Each of the five query sites does the documented single-query form; `list_pending()` uses the new index or the `NOT EXISTS` rewrite; all admin/stats/voting endpoints return identical values to before, just via fewer/cheaper queries.
- **Verify**: Add/extend a resolver DB test module (`tests/test_resolver_db.py` if none exists — check first, since only `test_resolver_client.py`/`test_resolver_integration.py` were found) with a seeded in-memory/temp SQLite DB, asserting each consolidated query returns the same values as the pre-change multi-query version on identical fixture data, and that the new index exists after migration.

### Task 12 — `resolver/app.py` purge threading (E24)
- **Model**: Sonnet 5 — a contained, well-understood change (move one call to a daemon thread, add logging); the main judgment call (is fire-and-forget acceptable here) is already resolved in the spec.
- **Do**: Move `_purge_resolve()`'s Cloudflare API call in `admin_set`/`admin_offset` onto a background daemon thread; the HTTP response no longer waits on purge completion. Add server-side logging so a purge failure is still visible in logs even though it's no longer in the response.
- **Acceptance**: `admin_set`/`admin_offset` return immediately without waiting on the Cloudflare round-trip; a failed purge is logged, not silently dropped; a successful purge still happens (just asynchronously).
- **Verify**: Test with a mocked/recording `_purge_resolve` asserting the endpoint response doesn't block on it (e.g. mock sleeps, assert response time is unaffected) and that the call still happens (assert the mock was invoked, possibly awaited via a short join in the test).

### Task 13 — `resolver_client.py` client-id caching (E25)
- **Model**: Haiku 4.5 — trivial: module-level memoization of a single value.
- **Do**: Cache the client id in a module-level variable after the first disk read in `_client_id()`, instead of re-reading the file on every `ping()`/`report()` call.
- **Acceptance**: Only the first `ping()`/`report()` call in a process reads the client-id file from disk; subsequent calls reuse the cached value; the value still matches what's on disk (no staleness within one process's lifetime, which matches current single-process usage).
- **Verify**: `tests/test_resolver_client.py` (existing) green; add a test asserting the file-read happens once across multiple `ping()`/`report()` calls (call-counting on the file-open).

> **CHECKPOINT 3 (final)** — Full `pytest tests/ -v` green across all 13 tasks. Manual spot-check: run the four Library Tools and CSV export against a real/copied library folder and confirm no regressions (per `SPEC-perf-simplification.md` Success Criteria). Diff review before merge — this pass touches 10 files; review as a whole for consistency (e.g. the new shared helpers in `library_common.py`/`VideoDownload.py` used the way each downstream task assumed).

---

## Risks & watch-items

- **Line numbers drift**: findings were captured from a point-in-time review (2026-07-20); re-verify exact line numbers at implementation time for each task, per the spec's own note.
- **Dedupe blocking-key correctness (Task 8)**: the one place this pass can silently regress correctness (a missed real duplicate) rather than just get slower/faster. Budget real time for the boundary test, not just the happy path.
- **Concurrency test flakiness (Tasks 9, 10)**: thread-pool tests with mocked I/O should avoid real `time.sleep`-based ordering assertions where possible — assert on final aggregate correctness, not exact interleaving, to keep tests deterministic.
- **Resolver test coverage gap (Task 11)**: no existing `test_resolver_db.py` was confirmed — this task may need to establish a fixture/harness (temp SQLite file or in-memory DB matching `resolver/db.py`'s schema) rather than extend existing coverage.
- **Task 12's async purge**: changes an observable guarantee (purge-before-response) into fire-and-forget — confirm this is acceptable for the resolver's actual admin workflow (per spec, already flagged as a documented behavior shift) before landing.
