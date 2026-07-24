# TODO: Performance & Simplification Pass

See [`perf-simplification-plan.md`](perf-simplification-plan.md) for full detail, acceptance criteria, and verification steps. Spec: [`SPEC-perf-simplification.md`](../SPEC-perf-simplification.md).

## Phase 0 — Foundation
- [x] **Task 1** *(Model: Sonnet 5)* — `VideoDownload.py`: shared ini-read helper (`_read_ini_section`); fold resolution write into the main `set_ini_values()` call (findings A1, A13). *(364/364 tests passing, incl. 4 new regression tests for the single-parse/single-write behavior.)*
- [x] **Task 3** *(Model: Sonnet 5)* — `library_common.py`: shared folder-listing helper for `find_song_audio`/`find_video_file`/`iter_song_folders`; single-pass `read_song_ini_fields` (findings A5-A8). *(2 new regression tests confirming no directory is scanned twice.)*

## Phase 1 — Downstream consolidation + independent fixes
- [x] **Task 2** *(Model: Sonnet 5)* — `gui.py`: CSV export + `_video_kind`/title dedup + `SyncEditor._read_offset` via shared reader (needs Task 1) (findings A2-A4). *(4 new regression tests, incl. a call-counting test for the CSV export.)*
- [x] **Task 5** *(Model: Opus 4.8 — fragile file, TOCTOU guards, real-bug history)* — `chart_rename.py`: thread resolved paths through, dedupe `by_role` scan, fold `is_sng_packaged` glob (needs Task 3) (findings A9-A12). *(Delegated to an Opus agent, diff reviewed and verified. 3 new regression tests; TOCTOU guards and rename/relocation decisions confirmed byte-identical.)*
- [x] **Task 6** *(Model: Haiku 4.5)* — `static_art.py`: reuse duration from first `ffprobe` probe instead of a second call (finding B14). *(Required updating 10 existing tests to a new `_probe_static_video_verdict` seam; 1 new call-counting regression test.)*
- [x] **Task 7** *(Model: Haiku 4.5)* — `video_repair.py`: merge codec + frame-rate probes into one `ffprobe` call (finding B15). *(Required updating 8 existing `ensure_playable` tests to a new `_probe_video_info` seam; 1 new call-counting regression test.)*
- [x] **Task 8** *(Model: Opus 4.8 — the one correctness-regression risk, not just speed)* — `dedupe_report.py`: blocking-key bucketing to kill the O(n²) fuzzy-match pass, with boundary test (finding C16). *(Delegated to an Opus agent, diff reviewed and verified: length-bucket key with ±1 adjacency, mathematically guaranteed to catch any pair within the bucket width. Boundary test proves a real near-duplicate pair spanning disjoint naive buckets still groups correctly.)*
- [x] **▶ CHECKPOINT 1** — Full `pytest tests/ -v` green: **374/374 passing.**

## Phase 2 — Concurrency (documented ordering shift)
- [x] **Task 9** *(Model: Sonnet 5)* — `metadata_enrichment.py`: thread-pool Chorus lookups (8 workers), preserve one-failure-doesn't-abort invariant (finding D17). *(3 new regression tests: failure survival, concurrent-vs-serial aggregate equality.)*
- [x] **Task 10** *(Model: Sonnet 5)* — `video_repair.py`: thread-pool per-video scan (needs Task 7, done) (finding D18). *(1 new regression test: failure survival, all videos still accounted for.)*
- [x] **▶ CHECKPOINT 2** — Full `pytest tests/ -v` green: **377/377 passing.**

## Phase 3 — Resolver server
- [x] **Task 11** *(Model: Opus 4.8 — 5 SQL rewrites + a new test harness from scratch)* — `resolver/db.py`: consolidated `client_stats()`/`stats()` queries (6→1, 5→3+1), added `status` index AND rewrote `list_pending()` as `NOT EXISTS`, `_refresh_mapping()` now returns status directly (findings E19-E22). *(E23 judged not a real win — documented in-code why: the median calc already forces a full row-fetch, so a separate COUNT(DISTINCT) would add a query, not remove one. New `tests/test_resolver_db.py`, 12 tests, built from scratch since no coverage existed.)*
- [x] **Task 12** *(Model: Sonnet 5)* — `resolver/app.py`: `_purge_resolve()` now fires the Cloudflare call on a background daemon thread; added logging (the server had none at all before) so a purge failure is visible instead of silently discarded (finding E24). *(New `tests/test_resolver_app.py`, gated by `pytest.importorskip('fastapi')` since that dependency isn't installed in this dev environment — skips cleanly here, runs for real wherever the resolver's own deps are present.)*
- [x] **Task 13** *(Model: Haiku 4.5)* — `resolver_client.py`: cache client id in memory after first read (finding E25). *(First delegation attempt's report didn't match what was on disk — no changes had actually landed despite a confident summary; re-verified via diff and re-implemented directly. 3 new regression tests.)*
- [x] **▶ CHECKPOINT 3 (final)** — Full `pytest tests/ -v` green: **392 passed, 1 skipped.**

---

### Notes
- Findings/line numbers captured from a point-in-time review (2026-07-20) — re-verify at implementation time per each task.
- Task 8 (dedupe blocking key) is the one place this pass can regress *correctness* (a missed duplicate), not just speed — budget real time for its boundary test.
- Task 11's resolver DB test coverage gap: confirm whether `tests/test_resolver_integration.py` already exercises `resolver/db.py`'s queries before assuming a new test file is needed.
