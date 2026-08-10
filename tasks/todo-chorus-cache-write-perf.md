# TODO: Chorus Cache Write Amplification — Batched Flush, TTL Prune, Payload Trim

See [`plan-chorus-cache-write-perf.md`](plan-chorus-cache-write-perf.md) for full detail, acceptance criteria, and verification steps. Spec: [`../SPEC-chorus-cache-write-perf.md`](../SPEC-chorus-cache-write-perf.md).

## Task 1: Batched flush — `chorus_cache.py` write machinery + `enrich_library()` final flush
- [ ] Add constants `FLUSH_EVERY_N_INSERTS = 25`, `FLUSH_EVERY_SECONDS = 60` with a why-comment citing the 2026-08-10 measurement (126.42 MB / 7,743 entries / 0.75 s per `json.dumps`)
- [ ] `__init__` (:39-43) — `self._dirty = 0` **before** `self._load()`; `self._last_flush = time.time()` and `self._retry_not_before = 0.0` after it, with an in-code note on why the ordering matters
- [ ] `_save()` (:55-64) — return `True`/`False`; on success reset `_dirty = 0` and `_last_flush`; on `OSError` leave `_dirty` intact and set `_retry_not_before = time.time() + FLUSH_EVERY_SECONDS`
- [ ] Add `_maybe_flush()` — no-op if `not _dirty` or still inside `_retry_not_before`; else save when `_dirty >= FLUSH_EVERY_N_INSERTS` or `FLUSH_EVERY_SECONDS` elapsed
- [ ] Add public `flush()` — unconditional last-chance persist, ignores `_retry_not_before`, cheap no-op when clean, docstring explains why callers need it
- [ ] `search_by_artist_title()` (:73-76) — replace `self._save()` with `self._dirty += 1` + `self._maybe_flush()`
- [ ] Comment the atomic-write block: batching changed *when* we write, never *how* (cite `_save_background_state`, gui.py:184-196)
- [ ] `library_enrichment.enrich_library()` (:197-217) — `client.flush()` in a `finally` around the per-song loop, with a comment on the daemon-thread/app-close path
- [ ] Update `test_disk_cache_persists_across_instances` (tests/test_chorus_cache.py:109) — add `first_client.flush()`
- [ ] Update `test_disk_cache_written_as_valid_json` (:152) — add `client.flush()`
- [ ] Update `test_disk_write_failure_does_not_raise` (:136) — add `client.flush()` so the failing-`open` path is genuinely hit, not passed vacuously
- [ ] `test_writes_are_batched_not_per_lookup`
- [ ] `test_flush_happens_after_n_inserts`
- [ ] `test_flush_happens_after_time_threshold` (injected clock, no real sleep)
- [ ] `test_flush_with_nothing_pending_is_a_noop`
- [ ] `test_failed_save_keeps_dirty_window_for_retry`
- [ ] `test_failed_save_does_not_retry_on_every_lookup`
- [ ] `test_enrichment_flushes_cache_at_end_of_run` (tests/test_library_enrichment.py)
- [ ] `test_enrichment_flushes_cache_even_on_error` (tests/test_library_enrichment.py)
- [ ] `pytest tests/test_chorus_cache.py tests/test_library_enrichment.py -v` green

## ▶ Checkpoint 1
- [ ] `pytest tests/ -q` full suite green (796 baseline + 8)
- [ ] Read-check: `_save()`'s temp + `os.replace` body unchanged apart from the return value and counter bookkeeping — diff it line by line
- [ ] Read-check: `test_disk_write_failure_does_not_raise` genuinely enters the failing-`open` path
- [ ] Read-check: `_dirty = 0` initialized **before** `self._load()`

## Task 2: TTL prune on load — `chorus_cache.py`
- [ ] `_load()` (:45-53) — drop entries past `self.ttl_seconds` after the successful `json.load`
- [ ] Treat missing/non-numeric `cached_at` as expired rather than raising
- [ ] `self._dirty += 1` if anything was pruned
- [ ] Comment with the 2026-08-10 finding: 5,379 of 7,743 entries (69%) already past TTL, rewritten on every 126 MB save
- [ ] `test_expired_entries_pruned_on_load`
- [ ] `test_prune_on_load_does_not_lose_fresh_entries`
- [ ] `test_malformed_cached_at_treated_as_expired`
- [ ] `test_prune_on_load_persists_at_next_flush`
- [ ] Regression: `test_entry_expires_after_ttl` (:78) passes unmodified
- [ ] `pytest tests/test_chorus_cache.py -v` green

## Task 3: Payload trim — `chorus_cache.py`
- [ ] Add `_CACHED_RESULT_FIELDS = ('name', 'artist', 'album', 'genre', 'year', 'charter')` — comment that it is an allowlist by deliberate choice, not a `notesData` denylist, and that widening it is Ask First (same rule as `metadata_enrichment.CHORUS_FILLABLE_KEYS`, metadata_enrichment.py:50-52)
- [ ] Add `_trim(result)` — `None` passes through; non-dict passes through; else six-field projection
- [ ] Apply at insert (`search_by_artist_title`:74)
- [ ] Apply to legacy entries in `_load()`, `self._dirty += 1` if any were trimmed
- [ ] Update `CachedChorusClient` docstring (:32-37) — narrowed contract, NOT a drop-in for `chorus_client.search_by_artist_title()`; cite the 2026-08-10 approval and the 112.8-of-126.4 MB number
- [ ] Verify `_RESULT_A`/`_RESULT_B` (:9-10) contain only allowlisted fields before relying on the existing tests surviving unmodified
- [ ] `test_only_consumed_fields_are_cached`
- [ ] `test_legacy_full_payload_entry_is_trimmed_on_load`
- [ ] Regression: `test_none_result_is_cached_too` (:98) passes unmodified
- [ ] `pytest tests/test_chorus_cache.py tests/test_library_enrichment.py tests/test_metadata_enrichment.py tests/test_dedupe.py -v` green

## ▶ Checkpoint 2
- [ ] `pytest tests/ -q` full suite green (~812 passed / 1 skipped)
- [ ] Read-check: `_CACHED_RESULT_FIELDS` is an allowlist; class docstring states the narrowed contract
- [ ] Read-check: `_load()` prunes then trims in one pass, increments `_dirty` for either, atomic-write path untouched

## ▶ Checkpoint 3 — real-data migration rehearsal (non-destructive)
- [ ] Copy `M:/_Organized/Songs/backstagehero_chorus_cache.json` to the scratchpad — **never open the live path for writing**
- [ ] Construct a `CachedChorusClient` against the copy, `flush()`, measure
- [ ] Assert < 5 MB (predicted ~0.52 MB) and entry count ≤ 2,352 (TTL drift since 2026-08-10 means more will have expired)
- [ ] Spot-check surviving entries: six fields present, `notesData` absent, `cached_at` preserved
- [ ] Confirm the copy reloads cleanly into a second client with no further pruning

## ▶ Checkpoint (final)
- [ ] `pytest tests/ -q` full suite green
- [ ] Diff review: only `chorus_cache.py`, `library_enrichment.py`, `tests/test_chorus_cache.py`, `tests/test_library_enrichment.py` touched — no new dependency, no `atexit`/`signal` hook, no background flush thread, no cross-process lock, no JSONL, no changes to `chorus_client.py`/`metadata_enrichment.py`/`dedupe_report.py`/`gui.py`
- [ ] Confirm the three spec success criteria in the plan file's Final Checkpoint section
- [ ] Read-check (not just green tests): atomic temp + `os.replace` intact; `search_by_artist_title()` still never raises on cache trouble
- [ ] **Leave the 2026-08-10 stall investigation open** — this fix is not evidence about it

---

### Notes
- Line numbers verified live against current code at spec/plan time (2026-08-10) — re-verify at `/build` time if anything else lands first.
- Task order is mandatory: Tasks 2 and 3 both need Task 1's `_dirty`, and both edit `_load()`, so run 1 → 2 → 3 sequentially. No parallelization available here.
- Task 1 ships batching *and* the `enrich_library()` flush together on purpose — batching alone would be a data-loss regression, not a partial improvement.
