# Plan: Chorus Cache Write Amplification — Batched Flush, TTL Prune, Payload Trim

**Spec**: [`SPEC-chorus-cache-write-perf.md`](../SPEC-chorus-cache-write-perf.md) (amends `SPEC-library-enrichment.md`; revisits the file `SPEC-chorus-reliability-fix.md` deliberately left alone)

## Overview

Three vertically-sliced changes to `chorus_cache.py`, each one a complete, independently-verifiable path, ordered so the riskiest lands first:

1. **Batched writes end-to-end** — the dirty-counter/flush machinery *plus* the `enrich_library()` `finally` that makes it safe. These ship together because batching without a final flush is a data-loss regression, not a partial improvement.
2. **TTL prune on load** — drops the 69% of entries that are already unreachable.
3. **Payload trim** — stores only the six fields the cache's one consumer reads, which is 89% of the file.

Each task lands its own tests in the same task — no separate "write tests" task, matching `plan-chorus-reliability-fix.md`'s convention.

## Architecture Decisions

- **Batching over JSONL.** The spec's rejected-alternative section carries the full reasoning; the short version is that once Tasks 2-3 land, the file is ~0.5 MB and a full atomic rewrite costs single-digit milliseconds, so JSONL would trade away `os.replace` durability and force a real migration to optimize something already cheap.
- **Task 1 ships batching and the flush together.** Splitting them would leave an intermediate commit where a killed run silently loses its last partial batch. Vertical slice = the whole write path.
- **`_dirty` is an int counter of pending changes, not a bool.** `_maybe_flush()` needs a count for the N-threshold, and prune/trim reuse it by incrementing — one mechanism, three producers.
- **A failed save paces its own retry.** The spec says a transient `OSError` must "retry at the next threshold" rather than discard the pending window. Taken literally that is a write-storm: `_dirty >= N` stays true, so every subsequent lookup retries a failing write immediately. `_retry_not_before` (Task 1) is the minimal honest reading — keep the dirty window, but pace retries at the time threshold. This matters because the known real failure here is `WinError 32` (SPEC-chorus-reliability-fix.md), which is transient-but-repeated.
- **Prune/trim are eager, not lazy.** A load that changes anything marks the cache dirty, so even a zero-miss run rewrites the shrunken file once. That single write is the mechanism that actually reclaims the 126 MB — the spec's Open Question 3 settled this.

## Dependency Graph

```
Task 1: batching (_dirty/_maybe_flush/flush/_save bookkeeping) + enrich_library finally
   │        chorus_cache.py, library_enrichment.py, 2 test files
   │
   ├── Task 2: TTL prune on load          ── needs Task 1's `_dirty` to persist the shrunken form
   │        chorus_cache.py, 1 test file
   │
   └── Task 3: payload trim (insert + legacy-on-load)  ── needs Task 1's `_dirty` for the same reason
            chorus_cache.py, 1 test file
```

Tasks 2 and 3 are logically independent of each other but both edit `_load()`, so run them **sequentially in this order** to avoid overlapping edits to the same function. Neither can precede Task 1.

---

## Task 1: Batched flush — `chorus_cache.py` write machinery + `enrich_library()` final flush

**Description**: Stop calling `_save()` per insert. Introduce a dirty counter and two thresholds (count and elapsed time), a public `flush()`, and retry pacing for a failed save. Then make `enrich_library()` call `flush()` in a `finally` so a normal return, an exception, or a `KeyboardInterrupt` all persist the run's work. The atomic temp + `os.replace` write itself is untouched.

**Exact change** (`chorus_cache.py`, currently 77 lines — `__init__`:39-43, `_load`:45-53, `_save`:55-64, `search_by_artist_title`:66-76):

- Add module constants below `_SECONDS_PER_DAY` (:23), with a why-comment citing the 2026-08-10 measurement (126.42 MB / 7,743 entries / 0.75 s per `json.dumps`) and stating that the batch size bounds *worst-case entry loss on a kill*, not throughput:
  ```python
  FLUSH_EVERY_N_INSERTS = 25
  FLUSH_EVERY_SECONDS = 60
  ```
- `__init__` (:39-43): after `self._load()`, add `self._dirty = 0`, `self._last_flush = time.time()`, `self._retry_not_before = 0.0`. (After, not before — Task 2/3 have `_load()` set `_dirty`, so initializing it afterward would clobber that. Initialize `_dirty = 0` *before* `self._load()` and leave the other two after; call this out explicitly in the code so the ordering isn't "fixed" later by someone tidying.)
- `_save()` (:55-64): return `True` on success / `False` on the `OSError` path. On success set `self._dirty = 0` and `self._last_flush = time.time()`. On failure leave `_dirty` untouched and set `self._retry_not_before = time.time() + FLUSH_EVERY_SECONDS`. Keep the existing `log.warning` wording and the never-raise behavior exactly as-is.
- Add `_maybe_flush()`: return immediately if `not self._dirty` or `time.time() < self._retry_not_before`; otherwise call `_save()` when `self._dirty >= FLUSH_EVERY_N_INSERTS or (time.time() - self._last_flush) >= FLUSH_EVERY_SECONDS`.
- Add public `flush()`: unconditional last-chance persist — ignores `_retry_not_before` (a paced retry must not cause a run's final write to be skipped), calls `_save()` if `self._dirty`, and is a cheap no-op when nothing is pending. Docstring says why callers need it and that it is safe to call repeatedly.
- `search_by_artist_title()` (:73-76): replace `self._save()` with `self._dirty += 1` followed by `self._maybe_flush()`.
- Comment the atomic-write block to state that batching changed *when* we write, never *how*, citing `_save_background_state` (gui.py:184-196) for the same rationale — a run spanning days must never leave a half-written file a later run would trust.

**Exact change** (`library_enrichment.py`, `enrich_library()`:167-228):

- Wrap the per-song loop (:197-217) so `client.flush()` runs in a `finally`. `_save_sidecar` (:220) and the return dict (:222-228) stay outside/after, unchanged. No signature change, no return-value change. One comment line on why the `finally` exists (a GUI enrichment thread is a daemon thread — app close is a real path where the loop never finishes normally).

**Acceptance criteria:**
- 10 consecutive cache misses under a high `FLUSH_EVERY_N_INSERTS` produce **zero** disk writes; a subsequent `flush()` produces exactly one, containing all 10 entries.
- A write lands automatically once `FLUSH_EVERY_N_INSERTS` is reached, with no explicit `flush()` call.
- A slow trickle of lookups still checkpoints on the `FLUSH_EVERY_SECONDS` bound (verified with an injected clock, never a real sleep).
- `flush()` with nothing pending performs no write and does not raise.
- A failed `_save()` leaves `_dirty` intact (the window is retried, not discarded) **and** does not re-attempt the write on the very next lookup.
- `enrich_library()` leaves every entry from the run on disk after returning, with the flush threshold set above the song count so only the final flush can have written it.
- `enrich_library()` still persists earlier entries when a later song raises.
- `search_by_artist_title()` still never raises on cache trouble; the temp + `os.replace` write is byte-for-byte the same operation.

**Verification:**
- Update the three existing tests that encode save-per-lookup — this is the contract change made visible, and each keeps asserting exactly what it asserted before:
  - `test_disk_cache_persists_across_instances` (tests/test_chorus_cache.py:109) — add `first_client.flush()` before `assert cache_path.exists()`.
  - `test_disk_cache_written_as_valid_json` (:152) — add `client.flush()` before reading the file.
  - `test_disk_write_failure_does_not_raise` (:136) — add an explicit `client.flush()`. **Critical**: without it this test would still pass, but *vacuously* — batching means one lookup writes nothing, so the failing-`open` path would never be entered and the test would assert nothing.
- New tests in `tests/test_chorus_cache.py` (following the file's existing `_stub`/`monkeypatch`/injected-clock conventions, `monkeypatch.setattr(cc.time, 'time', ...)` as `test_entry_expires_after_ttl`:78 already does):
  - `test_writes_are_batched_not_per_lookup`
  - `test_flush_happens_after_n_inserts`
  - `test_flush_happens_after_time_threshold`
  - `test_flush_with_nothing_pending_is_a_noop`
  - `test_failed_save_keeps_dirty_window_for_retry`
  - `test_failed_save_does_not_retry_on_every_lookup`
- New tests in `tests/test_library_enrichment.py` (using the file's `_make_song`/`_stub_chorus` helpers, :20-32):
  - `test_enrichment_flushes_cache_at_end_of_run`
  - `test_enrichment_flushes_cache_even_on_error`
- `C:\Python314\python.exe -m pytest tests/test_chorus_cache.py tests/test_library_enrichment.py -v`

**Dependencies**: None.

**Files touched** (4 — Medium):
- `chorus_cache.py`
- `library_enrichment.py`
- `tests/test_chorus_cache.py`
- `tests/test_library_enrichment.py`

---

## ▶ CHECKPOINT 1

- [ ] `C:\Python314\python.exe -m pytest tests/test_chorus_cache.py tests/test_library_enrichment.py -v` — green.
- [ ] `C:\Python314\python.exe -m pytest tests/ -q` — full suite green against the 796 passed / 1 skipped baseline (`e490ca5`), now +8.
- [ ] Manual read-check: `_save()`'s temp-file + `os.replace` body is unchanged apart from the added return value and counter bookkeeping. Diff it line by line — this is the one thing Boundaries → Always Do is most explicit about.
- [ ] Manual read-check: `test_disk_write_failure_does_not_raise` genuinely enters the failing-`open` path now (confirm by temporarily making the stub count calls, or by reading the flow) — a vacuous pass here is worse than a red test.
- [ ] Confirm `_dirty = 0` is initialized **before** `self._load()`, so Tasks 2-3 can set it from inside `_load()`.

---

## Task 2: TTL prune on load — `chorus_cache.py`

**Description**: `_load()` (:45-53) currently reads whatever is on disk verbatim and keeps it forever. TTL is only checked on read (:70), so expired entries are unreachable but still re-serialized by every save. Drop them at load time and mark the cache dirty so the shrunken form reaches disk. Zero behavior change: an expired entry can never be returned to a caller today.

**Exact change** (`chorus_cache.py`):
- In `_load()`, after the successful `json.load`, filter `self._entries` to entries whose `cached_at` is within `self.ttl_seconds`. Treat a missing or non-numeric `cached_at` as expired rather than raising — today's read path (:70) would `KeyError`/`TypeError` on such an entry, and the prune is the natural place to make that defensive.
- If anything was dropped, `self._dirty += 1` so the run's first flush (or its final `flush()`) persists the shrunken file.
- Comment with the 2026-08-10 finding: 5,379 of 7,743 live entries (69%) were already past TTL and were being rewritten on every one of the 126 MB saves.

**Acceptance criteria:**
- A cache file containing one fresh and one 30-day-old entry loads with only the fresh entry, and after `flush()` the expired key is gone from disk.
- Fresh entries are never dropped (the guard against an over-eager prune).
- An entry with a missing or non-numeric `cached_at` is pruned, not raised on.
- A load that pruned something marks the cache dirty, so a run with zero cache misses still shrinks the file on its final flush.
- Pruning is driven by the existing `ttl_days`/`DEFAULT_TTL_DAYS`; no new TTL constant, no change to the 7-day default.

**Verification:**
- New tests in `tests/test_chorus_cache.py`:
  - `test_expired_entries_pruned_on_load`
  - `test_prune_on_load_does_not_lose_fresh_entries`
  - `test_malformed_cached_at_treated_as_expired`
  - `test_prune_on_load_persists_at_next_flush`
- Regression: `test_entry_expires_after_ttl` (:78) must pass unmodified — it exercises in-memory TTL expiry within one instance's life, which the prune does not touch.
- `C:\Python314\python.exe -m pytest tests/test_chorus_cache.py -v`

**Dependencies**: Task 1 (needs `_dirty`).

**Files touched** (2 — Small):
- `chorus_cache.py`
- `tests/test_chorus_cache.py`

---

## Task 3: Payload trim — `chorus_cache.py`

**Description**: Store only the six fields the cache's one consumer reads. `library_enrichment._enrich_one_song()` (library_enrichment.py:138-145) is the only reader of a cached result, and it reads `name`, `artist`, `album`, `genre`, `year`, `charter`. The stored `notesData` field alone is 112.8 MB of the 126.4 MB file. Applied at insert time and to legacy full-payload entries found on load, so the live file shrinks in place rather than being rebuilt.

**Exact change** (`chorus_cache.py`):
- Add `_CACHED_RESULT_FIELDS = ('name', 'artist', 'album', 'genre', 'year', 'charter')` as a module constant, with a comment stating this is an **allowlist by deliberate choice, not a `notesData` denylist** — a future fat field Chorus adds must not silently reintroduce the problem — and that widening it is an Ask First decision, the same rule `metadata_enrichment.CHORUS_FILLABLE_KEYS` (metadata_enrichment.py:50-52) already carries.
- Add `_trim(result)`: returns `{k: result[k] for k in _CACHED_RESULT_FIELDS if k in result}`; any non-dict (including `None`, a cached no-match) passes straight through rather than raising.

  **Corrected during build.** This bullet originally specified a `result.get(k)` projection filling absent fields with `None`, which contradicts this task's own "existing tests pass unmodified" claim two sections down — a `None`-filled six-key dict is not equal to the three-key `_RESULT_A`. Verified before implementing: the intersection form leaves the fixtures identical, the `None`-fill form does not. Intersection is also the better design on its own merits — the cache shouldn't invent keys the API never sent, and every consumer reads through `.get()` anyway.
- Apply `_trim` to the **returned** value as well as the stored one. A miss returning the raw response while a hit returns the projection would make a lookup's shape depend on cache state — a bug that only surfaces in production. Not in the original plan; added during build and covered by `test_trimmed_result_is_returned_on_both_miss_and_hit`.
- Apply in `search_by_artist_title()` at insert (:74) and in `_load()` for legacy entries; if any legacy entry was trimmed, `self._dirty += 1`.
- Update the `CachedChorusClient` class docstring (:32-37) to state the narrowed contract explicitly: this returns a six-field projection, **not** a drop-in for `chorus_client.search_by_artist_title()`. Cite the 2026-08-10 spec-review approval and the 112.8-of-126.4 MB number.

**Acceptance criteria:**
- A stubbed Chorus result containing `notesData` is stored without it, and with all six consumed fields intact.
- A legacy on-disk entry carrying a full payload comes back trimmed after load, six fields intact — the migration path for the live 126 MB file.
- A `None` result is still cached as `None` (existing `test_none_result_is_cached_too`:98 must pass unmodified).
- A result missing some of the six fields omits those keys rather than filling them with `None` (see the correction above).
- A cache miss and a subsequent cache hit for the same lookup return the same shape.
- `metadata_enrichment` and `dedupe_report` are untouched — they call the raw `chorus_client` and never see the cache.

**Verification:**
- New tests in `tests/test_chorus_cache.py`:
  - `test_only_consumed_fields_are_cached`
  - `test_legacy_full_payload_entry_is_trimmed_on_load`
- Regression: `test_none_result_is_cached_too` (:98) and every other existing test in the file pass unmodified — the fixtures `_RESULT_A`/`_RESULT_B` (:9-10) already contain only `name`/`artist`/`genre`, all inside the allowlist, so they survive trimming unchanged. **Verify this assumption before relying on it**; if either fixture is ever widened, these tests start asserting on trimmed-away keys.
- `C:\Python314\python.exe -m pytest tests/test_chorus_cache.py tests/test_library_enrichment.py tests/test_metadata_enrichment.py tests/test_dedupe.py -v`

**Dependencies**: Task 1 (needs `_dirty`). Run after Task 2 — both edit `_load()`.

**Files touched** (2 — Small):
- `chorus_cache.py`
- `tests/test_chorus_cache.py`

---

## ▶ CHECKPOINT 2

- [ ] `C:\Python314\python.exe -m pytest tests/ -q` — full suite green, now ~812 passed / 1 skipped (796 baseline + 16 new).
- [ ] Manual read-check: `_CACHED_RESULT_FIELDS` is an allowlist and the class docstring states the narrowed contract. A future reader must be able to find *why* the cache stopped returning raw Chorus responses.
- [ ] Manual read-check: `_load()` does prune-then-trim in one pass and increments `_dirty` for either, with the atomic-write path still untouched.

---

## ▶ CHECKPOINT 3 — real-data migration rehearsal (non-destructive)

The spec's success criterion #2 is a claim about the live 126.42 MB file. Verify it against real data **without touching the live file**:

- [ ] Copy `M:/_Organized/Songs/backstagehero_chorus_cache.json` to the scratchpad. Never open the live path for writing at any point in this rehearsal.
- [ ] Construct a `CachedChorusClient` against the **copy**, call `flush()`, and measure the resulting file.
- [ ] Assert the result is **< 5 MB** (predicted ~0.52 MB) and that the entry count matches the ~2,352 non-expired entries measured on 2026-08-10 (allowing for TTL drift since that date — more entries will have expired, so expect ≤ 2,352).
- [ ] Spot-check several surviving entries: all six consumed fields present, `notesData` absent, `cached_at` preserved.
- [ ] Confirm the copy is still valid JSON and loads cleanly into a second client with no further pruning.

This is the one verification that exercises the legacy-migration path against the data it was written for. Test fixtures cannot substitute — they contain nothing resembling a 1 MB `notesData` blob.

---

## Final Checkpoint (whole spec)

- [ ] `C:\Python314\python.exe -m pytest tests/ -q` — full suite green, ≥ 796 passed / 1 skipped plus the new tests.
- [ ] Diff review: exactly `chorus_cache.py`, `library_enrichment.py`, `tests/test_chorus_cache.py`, `tests/test_library_enrichment.py` touched. No new dependency. No `atexit`/`signal` hook, no background flush thread, no cross-process lock, no JSONL format change, no change to `chorus_client.py`, `metadata_enrichment.py`, `dedupe_report.py`, or `gui.py`.
- [ ] Spec success criteria, confirmed by test rather than by a live run: (1) O(run-length / 25) writes instead of one per lookup; (2) the cache file lands < 5 MB (Checkpoint 3); (3) a kill mid-run leaves a parseable file missing at most 25 entries or 60 s of lookups.
- [ ] Confirm by inspection, not just by green tests, that the two properties Boundaries singles out still hold: the atomic temp + `os.replace` write is intact, and `search_by_artist_title()` still never raises on cache trouble.
- [ ] **The 2026-08-10 stall investigation stays open.** This fix landing is not evidence about that stall — the spec's arithmetic (≈1.7% of one core) argues it was never the cause. Do not close it on the back of this merge.

---

## Out of Scope (explicitly, per spec)

- JSONL / append-only cache format — weighed and rejected in the spec's Rejected Alternative section, recorded so it reads as decided rather than missed.
- Changing `DEFAULT_TTL_DAYS` — the prune reuses the existing 7-day TTL; re-tuning it is a separate decision with separate API-load consequences (Ask First).
- Widening `_CACHED_RESULT_FIELDS` beyond the six approved fields (Ask First).
- Deleting or rebuilding the live 126 MB cache as a "migration" — it must shrink in place; discarding it means ~7,800 lookups against an API known to rate-limit (Never Do).
- A cross-process lock, a background flush thread, or `atexit`/`signal` hooks (Never Do).
- Any change to `chorus_client.search_by_artist_title()`'s request shape or response handling — already fenced off by SPEC-chorus-reliability-fix.md.
- Diagnosing the 2026-08-10 ~70% CPU stall. Separate investigation, separate evidence.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `test_disk_write_failure_does_not_raise` passes vacuously after batching, silently losing coverage of the never-raise contract | High — a real guarantee stops being tested while looking green | Called out in Task 1's verification and given its own Checkpoint 1 line item; the fix is an explicit `flush()` that forces the failing-`open` path |
| A failed save retries on every subsequent lookup, turning a transient `WinError 32` into a write-storm | Medium — the exact failure SPEC-chorus-reliability-fix.md documented | `_retry_not_before` paces retries at the time threshold; `test_failed_save_does_not_retry_on_every_lookup` proves it |
| `_dirty` initialized after `_load()`, silently discarding the prune/trim dirty mark so the live file never shrinks | Medium — the fix appears to work but reclaims nothing on disk | Ordering is specified in Task 1 with an in-code comment, and re-checked at Checkpoint 1 and Checkpoint 3 (where a non-shrinking file would be caught against real data) |
| `_RESULT_A`/`_RESULT_B` fixtures widened later to include a field outside the allowlist, breaking existing tests confusingly | Low | Task 3's verification states the assumption explicitly and requires checking it before relying on it |
| Line numbers cited here drift before implementation | Low | All were verified live during the spec pass on 2026-08-10; re-verify at `/build` time if anything else lands first |
| Losing up to 25 entries on a kill is judged unacceptable after the fact | Low | Documented as an accepted, explicit trade in the spec's Durability section — the loss is bounded, recomputable, and costs only re-lookups; if it ever matters, `FLUSH_EVERY_N_INSERTS` is a one-line tuning change |

## Open Questions

Both carried from the spec; neither blocks implementation.

1. Should the GUI's enrichment thread call `flush()` periodically, independent of `enrich_library()`'s `finally`? The 60-second time threshold already covers the daemon-thread-killed-at-app-close case, so the plan does **not** do this. Flagged because that is the one path where "the `finally` never ran" is real.
2. Should a prune-only run with zero cache misses still rewrite the file? Planned as **yes** (eager) — one 34 MB write once is a rounding error against what this removes, and bounding the file is the entire point.
