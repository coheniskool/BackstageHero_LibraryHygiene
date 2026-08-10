# Spec: Chorus Cache Write Amplification — Batched Flush, TTL Prune, Payload Trim

**Parent spec**: SPEC-library-enrichment.md (this document only amends `chorus_cache.py`'s persistence behavior and `library_enrichment.py`'s use of it; sidecar format, chart/score parsing, Chorus request shape and everything else there is unchanged and not restated here).

**Sibling spec**: SPEC-chorus-reliability-fix.md explicitly decided *not* to touch `chorus_cache.py` ("Leaving `_save()`'s per-lookup atomic-write pattern as-is (it's correct for the single-writer case)"). That was the right call for the concurrency bug it was fixing — correctness, not cost. This spec revisits the same code for a different reason: the pattern is correct and *unaffordable*. Nothing here weakens the single-writer/atomic-replace guarantees that spec relies on.

## Objective

`CachedChorusClient.search_by_artist_title()` calls `self._save()` after **every** cache miss, and `_save()` serializes the entire `_entries` dict and rewrites the whole file (chorus_cache.py:55-64, chorus_cache.py:75). That is an O(n) disk write per insert — O(n²) bytes across a run.

Measured against the live cache on **2026-08-10** (`M:/_Organized/Songs/backstagehero_chorus_cache.json`, drive M: a local fixed disk, 358 GB free):

| Metric | Value |
|---|---|
| Cache file size | **126.42 MB** (132,564,870 bytes) |
| Entries | 7,743 |
| Mean / median / max entry | 17,080 B / 5,187 B / **1,088,395 B** |
| `json.dumps()` of the whole dict | **0.75 s** |
| `json.load()` of the whole dict | 0.80 s |
| Entries already past the 7-day TTL | **5,379 of 7,743 (69%)** |
| Bytes attributable to the `notesData` field | **112.8 MB of 126.4 MB (89%)** |
| Library size | ~7,873 song folders |

A `.tmp` sibling was present at 31,153,162 bytes with an mtime 5 s *after* the main file's — a partial atomic write caught in flight, i.e. a live writer at that moment.

So a full cold enrichment pass over ~7,800 songs currently costs roughly **0.75 s of CPU serialization plus a 126 MB file rewrite per song**: on the order of **2 hours of pure `json.dumps` time and ~1 TB of writes** for a job whose actual product is a few hundred KB of metadata. Every one of those bytes is rewritten to persist a single new key.

**User**: same solo hobbyist as the parent spec, running the GUI (`gui.py`) against a real ~7,800-song library on Windows, typically with "Enrich after scan" enabled and often over a multi-day unattended background run.

**Success looks like**: an enrichment pass over the full library performs a bounded, small number of cache writes against a file measured in hundreds of KB rather than one 126 MB rewrite per song, without giving up the crash-safety the atomic-write path exists for.

## Scope note — this is a confirmed defect, NOT a confirmed root cause

This defect was found while investigating a stall with sustained ~70% CPU on 2026-08-10. **It is not established as that stall's cause, and this spec does not claim it is.** The arithmetic argues against it:

- The rewrite was observed happening roughly **once a minute**, not back-to-back. One save is ~0.75 s of serialization plus ~0.25 s of write at a conservative local-disk rate — call it ~1 s of work per minute, ≈ **1.7% of a single core**. That cannot produce sustained ~70% CPU.
- No running enrichment pass was found in `log.txt` at the time.
- A once-a-minute save cadence implies roughly one *cache miss* per minute, which is what a rate-limited Chorus API (see SPEC-chorus-reliability-fix.md) would produce — the write amplification is being masked, not exercised, in that trace.

The stall investigation stays open and independent. This spec fixes a real, measured, self-evident cost that will bite hardest exactly when the Chorus API *is* healthy and lookups come back fast — the case where saves go back-to-back instead of once a minute. Do not close the stall investigation on the strength of this fix landing; verify the stall separately.

## Root Cause 1 — Unbatched whole-file writes

chorus_cache.py:73-76:

```python
result = chorus_client.search_by_artist_title(artist, title)
self._entries[key] = {'result': result, 'cached_at': time.time()}
self._save()          # json.dump of the full dict + os.replace
return result
```

`_save()` has no notion of "what changed" — it can only re-serialize everything. The per-lookup call site turns an O(1) logical insert into an O(n) physical one. There is no flush point, no dirty counter, and no way for a caller to say "I'm about to do 7,800 of these."

## Root Cause 2 — The file is never bounded, so O(n) grows without limit

`_load()` (chorus_cache.py:45-53) reads whatever is on disk verbatim. `search_by_artist_title` checks TTL on *read* (chorus_cache.py:70) and simply falls through to a fresh lookup when an entry is stale — but the stale entry is never removed. It stays in `_entries`, gets re-serialized by every subsequent `_save()`, and gets written back to disk forever.

**69% of the live cache (5,379 of 7,743 entries) is already past its 7-day TTL and can never be returned to a caller.** Every one of those entries is pure write-amplification ballast. Pruning them on load is a strict improvement with **zero behavior change** — a TTL-expired entry is unreachable by definition (chorus_cache.py:70 already refuses to return it).

Pruning alone: **126.42 MB → 33.94 MB**, 7,743 → 2,352 entries.

## Root Cause 3 — 89% of the file is a field nobody reads

The cache stores `chorus_client.search_by_artist_title()`'s **entire** response dict. That response carries a `notesData` field which accounts for **112.8 MB of the 126.4 MB file** — and no consumer of the cached result ever touches it.

Every consumer that goes through `CachedChorusClient`:

- `library_enrichment._enrich_one_song()` (library_enrichment.py:138-145) reads exactly six fields: `name`, `artist`, `album`, `genre`, `year`, `charter`.

That is the complete list — `library_enrichment.enrich_library()` (library_enrichment.py:190) is the **only** construction site of `CachedChorusClient` in the codebase. The other two Chorus consumers call the raw client and never see the cache at all:

- `metadata_enrichment.fill_song_ini_metadata()` (metadata_enrichment.py:96) — raw `chorus_client`, uses `name`/`artist` + `CHORUS_FILLABLE_KEYS` (`year`, `genre`, `charter`, `album`).
- `dedupe_report` (dedupe_report.py:386) — raw `chorus_client`, passes the result to `score_folder`.

Storing only the fields the cache's own consumer reads: **126.42 MB → 1.68 MB**. Combined with the TTL prune: **126.42 MB → 0.52 MB** (2,352 entries). That is a **243× reduction**, and it makes the whole-file-rewrite question close to academic — a 0.5 MB atomic replace is sub-10 ms.

This one is a genuine contract narrowing: `CachedChorusClient.search_by_artist_title()` stops being a drop-in for `chorus_client.search_by_artist_title()`, returning a projection rather than the raw response. Given it has exactly one caller reading exactly six fields, that is a cheap price. **Approved at spec review on 2026-08-10** — a six-field allowlist, deliberately not a `notesData` denylist, so a future fat field Chorus adds cannot silently reintroduce the problem. Widening `_CACHED_RESULT_FIELDS` is an Ask First decision, matching the convention `metadata_enrichment.CHORUS_FILLABLE_KEYS` (metadata_enrichment.py:50-52) already sets for the same kind of list.

## Behavior Change

1. **Batched flush.** `_save()` is no longer called per insert. Writes happen when a dirty-entry threshold or an elapsed-time threshold is crossed, plus a mandatory final flush when the owning run finishes.
2. **New public `flush()`.** Callers get an explicit "persist now" verb. `library_enrichment.enrich_library()` calls it in a `finally` so a normal return, an exception, or a `KeyboardInterrupt` all persist the run's work.
3. **TTL prune on load.** Entries already past `ttl_seconds` are dropped during `_load()` and never re-serialized.
4. **Payload trim.** Only the six consumed fields of a Chorus result are stored — applied both at insert time and to legacy full-payload entries encountered on load.

Unchanged: the cache key, TTL semantics, `force=`, `None`-result caching, the corrupt-file-is-ignored behavior, the never-raise-on-write-failure behavior, and the temp-file + `os.replace` atomic write itself.

## Implementation

### chorus_cache.py

- Add instance state: `self._dirty = 0` and `self._last_flush = time.time()`.
- Add module constants with the usual why-comment:
  - `FLUSH_EVERY_N_INSERTS = 25`
  - `FLUSH_EVERY_SECONDS = 60`
  These are deliberately conservative. Once the file is ~0.5 MB a flush is single-digit milliseconds, so the batch size exists to bound *worst-case entry loss on a kill*, not to chase throughput — 25 lost lookups is a few seconds of re-work on the next run, and the time bound guarantees a slow, rate-limited run still checkpoints roughly once a minute (which is, not coincidentally, exactly the cadence observed on 2026-08-10).
- `search_by_artist_title()`: after the insert, increment `_dirty` and call a new `_maybe_flush()` instead of `_save()`.
- `_maybe_flush()`: call `_save()` only when `_dirty >= FLUSH_EVERY_N_INSERTS` or `time.time() - _last_flush >= FLUSH_EVERY_SECONDS`.
- `flush()` (public): unconditionally `_save()` if `_dirty`, then reset the counters. Safe and cheap to call when nothing is pending.
- `_save()`: on success reset `_dirty = 0` and `_last_flush = time.time()`. On the existing `OSError` path, **leave `_dirty` as-is** so a transient failure retries at the next threshold rather than silently discarding the pending window — this matters for the `WinError 32` case SPEC-chorus-reliability-fix.md documented.
- `_load()`: after parsing, drop entries whose `cached_at` is older than `ttl_seconds`; tolerate entries missing/holding a non-numeric `cached_at` by treating them as expired (today's read path would `KeyError` on a malformed entry — pruning is the natural place to make that defensive). If anything was pruned (or legacy-trimmed), set `_dirty` so the shrunken form is written by the run's first flush. That costs at most one extra write per run and is the mechanism that actually reclaims the 126 MB on disk.
- Payload trim: a module-level `_CACHED_RESULT_FIELDS = ('name', 'artist', 'album', 'genre', 'year', 'charter')` and a small `_trim(result)` applied on insert and on load. `None` results stay `None`.
- The atomic temp + `os.replace` write is untouched. Comment it to say so explicitly, citing the same rationale as `_save_background_state` in gui.py:184-196 — a run that may span days must never be able to leave a half-written cache a later run would trust.

### library_enrichment.py

- Wrap the per-song loop in `enrich_library()` (library_enrichment.py:197-220) so `client.flush()` runs in a `finally`. Without this, the last partial batch of a run is lost, and — worse for a GUI run — the enrichment thread is a daemon thread, so app close can drop it silently.
- No signature change, no return-value change.

A context-manager (`__enter__`/`__exit__`) on `CachedChorusClient` was considered and rejected for now: there is exactly one construction site, and `try/finally` around a loop is more legible here than restructuring it into a `with`. Worth revisiting only if a second caller ever appears.

### Rejected alternative — JSONL append-only

An append-only JSONL cache makes an insert genuinely O(1) and is the textbook fix. Rejected because:

- It abandons the temp + `os.replace` atomicity in favor of "a torn tail line is skippable on load" — defensible, but strictly weaker than what exists, and this codebase has a stated, incident-backed preference for atomic replace on files a multi-day run depends on.
- It changes the on-disk format, so the live 126 MB file needs a real migration path (silently rebuilding it means ~7,800 lookups against an API already known to rate-limit — see SPEC-chorus-reliability-fix.md).
- It needs its own compaction story, or the append log grows unboundedly with duplicate keys — reintroducing the size problem this spec is trying to close.
- Most decisively: once the TTL prune and payload trim land, the file is ~0.5 MB and a full rewrite costs single-digit milliseconds. Batching turns an already-cheap write into a rare one. Paying for JSONL's format churn and weaker durability to optimize a 5 ms operation is the wrong trade.

Recorded here so a future reader knows it was weighed rather than missed.

## Durability

**The cache is a cache. Losing recent entries is survivable and explicitly accepted.** Stating it plainly rather than leaving it implied:

- If the process is killed between flushes, at most `FLUSH_EVERY_N_INSERTS` (25) entries — or one `FLUSH_EVERY_SECONDS` (60 s) window's worth, whichever came first — are lost.
- The cost of that loss is bounded and non-destructive: the next run re-issues those Chorus lookups. No library file, no sidecar, and no user data is affected. The cache holds nothing that cannot be recomputed from the network.
- What is **not** acceptable, and does not change here, is a *corrupt* cache. The temp + `os.replace` discipline stays exactly as it is: any crash — mid-write, mid-run, machine power loss — must leave either the previous complete file or the new complete file, never a hybrid. A multi-day unattended background run is precisely the crash-mid-write scenario that discipline exists for.

The trade being made is explicit: **up to 25 recomputable lookups** in exchange for **removing ~1 TB of writes and ~2 hours of CPU from a full pass.**

## Commands

```
Tests:     C:\Python314\python.exe -m pytest tests/ -q
This file: C:\Python314\python.exe -m pytest tests/test_chorus_cache.py tests/test_library_enrichment.py -v
```

Baseline to hold: **796 passed, 1 skipped** (as of merge `e490ca5`). Python 3.14, stdlib only, no new dependencies.

## Testing Strategy

Existing coverage in `tests/test_chorus_cache.py` is a 12-test suite that must stay green — but **three of those tests encode the save-per-lookup behavior this spec is removing** and will need updating. Calling that out here so it reads as a deliberate contract change at review time, not as a surprise mid-build:

| Test | Why it breaks | Fix |
|---|---|---|
| `test_disk_cache_persists_across_instances` (:109) | Asserts `cache_path.exists()` after one lookup | Insert `first_client.flush()` before the assertion |
| `test_disk_cache_written_as_valid_json` (:152) | Reads the file after one lookup | Add `client.flush()` before reading |
| `test_disk_write_failure_does_not_raise` (:136) | Would pass *vacuously* — with batching, one lookup writes nothing, so the failing-`open` path is never exercised | Add an explicit `client.flush()` so the OSError path is genuinely hit and still must not raise |

New tests in `tests/test_chorus_cache.py`:

- `test_writes_are_batched_not_per_lookup` — count `_save` invocations (or `open` calls) across e.g. 10 lookups under a high `FLUSH_EVERY_N_INSERTS`; assert 0 writes, then assert `flush()` produces exactly 1 and all 10 entries are present on disk.
- `test_flush_happens_after_n_inserts` — with the threshold set low, assert a write lands automatically at the boundary without an explicit `flush()`.
- `test_flush_happens_after_time_threshold` — inject the clock the way `test_entry_expires_after_ttl` (:78) already does; assert a slow trickle of lookups still checkpoints on the time bound.
- `test_expired_entries_pruned_on_load` — write a cache file with one fresh and one 30-day-old entry, construct a client, `flush()`, and assert the expired key is gone from disk.
- `test_prune_on_load_does_not_lose_fresh_entries` — the guard against an over-eager prune.
- `test_malformed_cached_at_treated_as_expired` — an entry with a missing or non-numeric `cached_at` is pruned rather than raising.
- `test_flush_with_nothing_pending_is_a_noop` — no write, no raise.
- `test_failed_save_retries_at_next_threshold` — an OSError on flush must not clear the dirty counter.
- `test_only_consumed_fields_are_cached` — stub a result containing `notesData`; assert the on-disk entry omits it and retains all six consumed fields.
- `test_legacy_full_payload_entry_is_trimmed_on_load` — the migration path for the live 126 MB file: a hand-written cache entry carrying `notesData` must come back trimmed, with the six consumed fields intact.

In `tests/test_library_enrichment.py`:

- `test_enrichment_flushes_cache_at_end_of_run` — assert the cache file contains the run's entries after `enrich_library()` returns, with the flush threshold set above the number of songs so only the final flush can have written it.
- `test_enrichment_flushes_cache_even_on_error` — make a later song raise; assert earlier entries still persisted.

Regression: `tests/test_library_enricher_integration.py`, `tests/test_metadata_enrichment.py`, `tests/test_dedupe.py` and `tests/test_chorus_client*.py` should be untouched — none of them construct a `CachedChorusClient`, and the raw `chorus_client` contract does not change.

Explicitly **not** in scope as a test: a live run against the real 126 MB cache. The size reductions in this spec are arithmetic on measured data, verifiable by re-measuring the file after the first real run.

## Boundaries

### Always Do

- Keep the temp-file + `os.replace` atomic write. Batching changes *when* we write, never *how*.
- Keep `search_by_artist_title()`'s never-raise-on-cache-trouble contract — a convenience cache must never cost the user the ability to run enrichment (the philosophy `test_disk_write_failure_does_not_raise` already states).
- Keep flush thresholds as named module constants with a why-comment, not literals — the same reason `_PACE_DEFAULT` and friends are named in gui.py:210-214.
- Cite the 2026-08-10 measurement in the code comments, matching this codebase's convention of explaining *why* against a dated incident.
- Run the full suite; the merge baseline is 796 passed / 1 skipped.

### Ask First

- **Widening `_CACHED_RESULT_FIELDS`** beyond the six approved fields. The six-field allowlist was an explicit spec-review decision (2026-08-10); adding a seventh is another one, not a convenience. Same rule `CHORUS_FILLABLE_KEYS` already carries.
- Changing the 7-day `DEFAULT_TTL_DAYS`. Pruning uses the existing TTL; re-tuning it is a separate decision with separate consequences for API load.
- Any change to `chorus_client.search_by_artist_title()`'s response handling or request shape — out of scope, and SPEC-chorus-reliability-fix.md already fenced it off.

### Never Do

- Do not delete or rebuild the live 126 MB cache as a "migration." The prune/trim path must read it and shrink it in place; discarding it means ~7,800 fresh lookups against an API known to rate-limit.
- Do not add a cross-process lock, a background flush thread, or a `signal`/`atexit` hook. Flush cadence plus an explicit `finally` is sufficient for a single-process, single-writer cache; SPEC-chorus-reliability-fix.md already established the per-process guard that keeps that assumption true.
- Do not add a dependency. Python 3.14, stdlib only.
- Do not weaken or remove any existing test to make batching pass — the three tests above get *updated to call `flush()`*, which preserves what each was actually asserting.

## Success Criteria

1. A full enrichment pass over ~7,800 songs performs O(run-length / 25) cache writes, not one per lookup — proven by test, not by inspection.
2. `M:/_Organized/Songs/backstagehero_chorus_cache.json` measures **< 5 MB** after the first post-fix run (predicted ~0.52 MB: trim + prune).
3. Killing the process mid-run leaves a **parseable** cache file every time; at most 25 entries or 60 seconds of lookups are missing from it.
4. `C:\Python314\python.exe -m pytest tests/ -q` ≥ 796 passed, 1 skipped, with the new tests added.
5. The stall investigation from 2026-08-10 remains open and is resolved on its own evidence, not on this fix landing.

## Open Questions

1. ~~**Is the payload trim approved?**~~ **Resolved 2026-08-10: yes, six-field allowlist.** Kept here rather than deleted because it is the reason the cache lands at 0.52 MB instead of 33.94 MB, and a future reader wondering why the cache stopped returning raw Chorus responses should find the decision, not just its consequence.
2. **Should `flush()` also be called periodically by the GUI's enrichment thread**, independent of `enrich_library()`'s `finally`? The 60-second time threshold already covers this, so probably not — flagged only because a daemon thread killed at app close is the one path where "the finally never ran" is real, and the time bound is what caps the damage there.
3. **Should a prune-only-no-writes run still shrink the file?** As specced, yes: a load that prunes marks the cache dirty, so even a run with zero cache misses rewrites the shrunken file once. Alternative is to only shrink lazily when something else is already dirty. Chose eager because the whole point is bounding the file, and one 34 MB write once is a rounding error against what this spec removes.

---

**Next phase**: `/plan` → `tasks/plan-chorus-cache-write-perf.md` (chorus_cache batching + prune + trim, then the library_enrichment flush, then the three test updates), then `/build`.
