# Spec: Chorus Cache Robustness — Self-Heal on Unrecoverable Load Failure

**Parent spec**: SPEC-chorus-cache-write-perf.md. That spec's Open Questions 4-6 recorded five findings raised at its `/ship` review on 2026-08-10, judged real, and deliberately deferred rather than bolted onto a change whose discipline was "batching changes *when* we write, never *how*." This spec closes all five: two by building, three by deciding.

**Sibling spec**: SPEC-chorus-reliability-fix.md owns the cross-process story (GUI + CLI `library_enricher.py` on one library) and is not reopened here.

**Explicitly not in scope**: the 2026-08-10 stall investigation (sustained ~70% CPU). That thread stays open on its own evidence, exactly as the parent spec instructed. Nothing here should be read as bearing on it.

## Objective

`CachedChorusClient.__init__` can still die on a sufficiently broken cache file, and when it does, the damage is permanent rather than transient.

The failure shape is what makes it worth fixing. `_load()` (chorus_cache.py:121-137) catches `(OSError, ValueError)`; `_compact()` (chorus_cache.py:139-187) catches `(KeyError, TypeError, OverflowError)` per entry. Anything outside those tuples propagates out of `__init__` — and because **nothing has been written at that point**, the bad file is never pruned, never trimmed, never replaced. The next run reads the same file and dies the same way. Under the GUI, `enrich_library()` runs on a daemon enrichment thread, so the app survives and the feature does not: enrichment is silently dead until the user finds and deletes a JSON file by hand, in a library folder they have no reason to look in.

**A convenience cache should cost a re-lookup when it is corrupt, never a permanently dead feature.** That principle is already stated twice in this module — `search_by_artist_title()`'s never-raise contract, and `_save()`'s "a run must never fail because it couldn't persist one" (chorus_cache.py:198-200) — but it is only delivered on the *write* path. The read path still has an escape hatch.

**User**: same solo hobbyist as the parent spec, GUI (`gui.py`) against ~7,800 songs on Windows, "Enrich after scan" on, often over a multi-day unattended background run — the run least likely to have anyone watching when enrichment quietly stops working.

**Success looks like**: no reachable input to `CachedChorusClient(cache_path=...)` can prevent enrichment from running. A file we cannot make sense of costs the run its cached lookups and nothing else, and the file heals itself on the next flush.

## Evidence gathered before writing this spec (2026-08-10)

Two of the five deferred items turn on facts nobody had established. Both were measured, not assumed.

| Question | Finding | Consequence |
|---|---|---|
| Does `json.load` really raise `RecursionError`, not `ValueError`? | **Yes.** Python 3.14.4: `json.loads('['*200000 + ']'*200000)` → `RecursionError`, `isinstance(e, ValueError)` is `False`. | Confirms the escape from `_load()`'s handler. |
| Does `json.dump` have the same hole? | **Yes, newly found.** Dumping a 100,000-deep dict raises `RecursionError`, which `_save()`'s `(OSError, TypeError, ValueError)` equally misses. | The never-raise contract has the *same* gap on the write side. Fix both or fix neither. |
| Is it reachable from the Chorus API? | **No.** `chorus_client.py:157` catches bare `except Exception`, which swallows `RecursionError` before it can reach the cache, on top of the 1 MiB response cap at chorus_client.py:120-123. | Confirms the parent spec's "hand-crafted or corrupted disk file only." Pre-existing, not a regression. |
| Is `M:` ever shared or multi-user storage? | **No.** `Win32_LogicalDisk` reports `M:` as **DriveType 3 — local fixed disk** (`SilverDrive`, 1 TB). `net use` is empty: no mapped network drives. The only SMB shares touching it are the built-in administrative shares (`M$`, `C$`, `F$`), which require admin credentials. One enabled local user (`aaron`); `Guest` and `Administrator` disabled. | Settles the `.tmp` item — see Decision 3. |

## What gets built

### Change 1 — Self-heal backstop on the read path (chorus_cache.py `_load`)

Restructure `_load()` around three outcomes instead of two, and **split reset behavior by cause** rather than treating every failure the same:

| Cause | Examples | In-memory | On disk |
|---|---|---|---|
| **Unreadable** | `OSError` — file locked (`WinError 32`), permission denied, a read error mid-`json.load` | reset to `{}` | **untouched** |
| **Unusable** | `ValueError` (unparseable), wrong shape, `RecursionError`, anything else | reset to `{}` | **marked dirty → replaced at the next flush** |
| Good | — | loaded, then `_compact()`ed | as today |

The split is the whole point, and it is not symmetry for its own sake:

- **Unreadable is not evidence the file is bad.** `WinError 32` is a *known, transient, repeated* failure in this codebase (SPEC-chorus-reliability-fix.md), and a concurrent process mid-`os.replace` is exactly the case gui.py:1975-1980 documents. Overwriting a file we merely could not read *this once* would replace a perfectly good cache with `{}` — turning a transient read failure into permanent data loss, which is worse than the bug being fixed.
- **Unusable is** evidence the file is bad. Leaving it there means every future run pays the same failed parse. Marking it dirty is what makes "self-heal" literal rather than aspirational: the run's first flush replaces the garbage with a valid file.

Mechanically this is what the module already does everywhere else — `_compact()` marks dirty when it drops entries so the shrunken form reaches disk (chorus_cache.py:185-187). The unusable path is the same mechanism with a different trigger.

**`except Exception`, never `except BaseException`.** `KeyboardInterrupt` and `SystemExit` must propagate. A Ctrl-C during the 0.80 s `json.load` of a 126 MB file has to abort the run — swallowing it would make the app feel hung at precisely the moment the user is trying to stop it. This gets its own test so a later "tidy-up" cannot widen it.

The backstop wraps `_compact()` as well as the parse, not just `json.load`. `_compact()`'s per-entry handler is good but enumerated, and the next person to add a line to that loop should not have to re-derive which exceptions kill `__init__`.

**Kept, not replaced**: the existing specific handlers. `except OSError` keeps its current `'Could not read Chorus cache %s: %s'` wording, and the wrong-shape branch keeps its own message. Specific handlers are for *diagnosis* — they say what went wrong in terms a reader of `log.txt` can act on. The backstop is for *survival*, and logs the exception type name so an unanticipated cause is still identifiable rather than anonymous.

### Change 2 — Self-heal backstop on the write path (chorus_cache.py `_save`)

Broaden `_save()`'s `except (OSError, TypeError, ValueError)` (chorus_cache.py:213) to `except Exception`, keeping the existing tuple enumerated in the comment as the *expected* causes and adding `RecursionError` to that enumeration with the 2026-08-10 finding.

This is the same defect as Change 1 in the same module, and its blast radius is arguably worse: `flush()` is called from a `finally` in `enrich_library()` (library_enrichment.py:246-249), where a raise would replace the run's real exception with the cache's. That `finally` already carries its own `except Exception` guard for exactly this reason — but a contract that says "deliberately returns nothing... a run must never fail because it couldn't persist one" should be delivered by the function that promises it, not by its caller's belt-and-braces.

**Accepted cost, stated plainly**: a catch-all can mask a genuine bug introduced by a later refactor (an `AttributeError`, say) as a logged warning instead of a red test. That is the price of a never-raise contract, and it is the price this function already chose to pay for `OSError`. The mitigation is the log line carrying `type(e).__name__` and the test suite pinning the behavior — not a narrower catch.

### Change 3 — Pruning stops being destructive (chorus_cache.py `__init__` / `_compact`)

`_compact()` prunes against `self.ttl_seconds` and the shrunken result is written back, so a client constructed with a **shorter** `ttl_days` against a shared cache file permanently deletes entries a default-TTL caller would still have served. The class docstring warns about it (chorus_cache.py:89-95); the code does nothing about it.

Split the two TTLs, because they are not the same question:

- **Read TTL** (`self.ttl_seconds`) — "will *I* serve this entry?" Per-instance, and correctly so.
- **Prune TTL** (`self._prune_seconds = max(self.ttl_seconds, DEFAULT_TTL_DAYS * _SECONDS_PER_DAY)`) — "may I delete this entry *for everyone*?" Floored at the default, so no instance can ever delete an entry a default-TTL caller could still have used.

A shorter-TTL caller keeps its stricter read semantics and deletes nothing extra. A longer-TTL caller prunes at its own longer horizon, which is not data loss — it only keeps more. The direction that was destructive is the only direction that changes.

**Zero behavior change at the only real construction site.** `enrich_library()` (library_enrichment.py:198) takes the default, where `max(7d, 7d)` is `7d`. No existing test passes a non-default `ttl_days` (`test_entry_expires_after_ttl`, tests/test_chorus_cache.py:84, passes `ttl_days=7` — the default spelled out). The docstring paragraph at chorus_cache.py:89-95 changes from a warning into a statement of the guarantee.

Rejected alternative: **disable prune-on-load entirely for any non-default `ttl_days`.** Safest against deletion, but a long-TTL caller would then never shrink the file at all, reintroducing exactly the unbounded growth the parent spec closed. The floor gets the same safety without that.

## Decisions — closed, not built

These three are recorded here so they read as decided rather than missed. Each states the condition that would reopen it.

### Decision 1 — `RecursionError` needs no dedicated patch

**Subsumed by Change 1.** The parent spec already reached this conclusion ("the better fix for both this and the `OverflowError` case is a self-heal path... that subsumes the individual exception-tuple patches"), and the evidence table above confirms every premise: real on 3.14.4, not a `ValueError`, unreachable from the API, pre-existing on `main`. It gets a regression test, not a handler of its own. Widening the tuple to `(OSError, ValueError, RecursionError)` would fix the one input we happen to have thought of and leave the next one — that is the failure mode the backstop exists to end.

### Decision 2 — No `fsync`. Closed.

**The self-heal is the cheaper answer to the same risk.** The parent spec's Durability section was corrected at ship review to say `os.replace` without a preceding `f.flush()` + `os.fsync()` does not order the data write against the rename, so machine power loss can in principle leave the renamed file holding unwritten contents. True — and the consequence is an *unparseable cache file*, which after Change 1 costs one reset to `{}` and a re-lookup. That is the same price this codebase already accepts for a kill between flushes.

Three reasons not to pay for `fsync`:

1. **Nothing in the cache is irrecoverable.** Every entry is a network lookup away. The parent spec's explicit trade — up to 25 recomputable lookups for ~1 TB of writes and ~2 hours of CPU — already priced this exact loss.
2. **A partial `fsync` would be a false promise.** Full correctness needs an fsync on the temp file *and* on the containing directory. Windows cannot fsync a directory handle. Adding only the first would let the code *claim* power-loss durability it still does not deliver — strictly worse than an accurate weaker claim, because the next reader would trust it.
3. **The cost lands exactly where the parent spec was cutting.** On Windows `os.fsync` is `FlushFileBuffers`; a device-level flush per write is the category of cost that change existed to remove.

**Reopens if**: the cache ever holds something not recomputable from the network. It does not today, and `_CACHED_RESULT_FIELDS` is an Ask First list precisely so that cannot change quietly.

### Decision 3 — Keep the fixed `<cache>.tmp` name. Closed on measurement.

The parent spec flagged `_save()`'s fixed `.tmp` sibling (chorus_cache.py:209) plus `open()`'s symlink-following as a write-redirect primitive — **"a non-issue while the library folder is local and single-user,"** conditioned on a fact nobody had checked. Checked now: `M:` is a local fixed disk, there are no mapped network drives, the only shares are the built-in admin shares (which require credentials that can already write the cache directly), and there is one enabled local user. **The stated trigger condition is not met.**

Two further reasons this is the right call rather than merely an acceptable one:

- `mkstemp` would put `+`/`-` lines on the temp-file + `os.replace` block, which this project holds at **zero diff against its pre-merge form** by deliberate policy. Spending that budget on a threat model that requires an attacker who already has write access to the same directory is a bad trade.
- Batching already made the TOCTOU window **rarer**, not worse — from one window per lookup to one per batch.

**Reopens if** the library folder moves to a UNC path, a mapped network drive, an explicitly shared folder, or a machine with a second enabled interactive user. Any of those makes it a real primitive and worth the diff.

## Behavior Change

1. A `CachedChorusClient` constructed against **any** file content survives construction. Unreadable and unusable both yield an empty in-memory cache; neither raises.
2. An **unusable** cache file is replaced with a valid one at the next flush. An **unreadable** one is left alone.
3. `flush()` / `_save()` cannot raise for any reason short of `BaseException`.
4. A non-default `ttl_days` no longer deletes entries below the 7-day default horizon.

Unchanged: the cache key, read-TTL semantics, `force=`, `None`-result caching, `_CACHED_RESULT_FIELDS`, the batching thresholds and retry pacing, the monotonic-clock flush cadence, `enrich_library()`'s signature and return value, and — emphatically — the temp-file + `os.replace` write itself.

## Implementation

### chorus_cache.py

**`__init__` (:98-119)** — add `self._prune_seconds = max(self.ttl_seconds, DEFAULT_TTL_DAYS * _SECONDS_PER_DAY)` beside `self.ttl_seconds`, before `_load()`. The existing initialization-order comment (:102-114) already explains why everything `_load()` touches must precede it; `_prune_seconds` joins that list and the comment gets one clause, not a rewrite.

**`_load()` (:121-137)** — one `try` with three ordered handlers. Parse into a **local** first and assign to `self._entries` only once the shape check passes, so no failure path can leave the instance holding a half-processed structure:

```python
    try:
        with open(self.cache_path, encoding='utf-8') as f:
            entries = json.load(f)
        if not isinstance(entries, dict):
            # Valid JSON of the wrong shape. Unusable, not unreadable --
            # reset AND replace, same as unparseable.
            log.warning('Chorus cache %s is not an object; starting empty', self.cache_path)
            self._reset()
            return
        self._entries = entries
        self._compact()
    except OSError as e:
        # UNREADABLE, which is not evidence the file is BAD. WinError 32 is
        # transient-but-repeated here (SPEC-chorus-reliability-fix.md), and a
        # concurrent process mid-os.replace looks identical. Overwriting a
        # file we merely couldn't read this once would replace a good cache
        # with {} -- worse than the failure being handled. In memory only.
        log.warning('Could not read Chorus cache %s: %s', self.cache_path, e)
        self._entries = {}
    except Exception as e:
        # The backstop. Anything that reaches here means the file could not
        # be turned into a usable cache by any path we know -- RecursionError
        # from deeply nested JSON is the verified one (2026-08-10, Python
        # 3.14.4: json.load raises it and it is NOT a ValueError), but the
        # point is to stop enumerating. Before this, such a failure escaped
        # __init__ before anything had been written, so the bad file was
        # never pruned or replaced: enrichment stayed dead every run until
        # the user deleted the cache by hand, and under the GUI it died on a
        # daemon thread with the app still running.
        #
        # Exception, NOT BaseException: Ctrl-C during the 0.80s json.load of
        # a 126 MB file must still abort the run.
        log.warning('Chorus cache %s is unusable (%s: %s); starting empty',
                    self.cache_path, type(e).__name__, e)
        self._reset()
```

**`_reset()` (new, private)** — `self._entries = {}` then `self._dirty += 1`. Three call sites, one rule, and a docstring saying the dirty mark *is* the self-heal: without it the garbage on disk survives every run that happens to have zero cache misses.

**`_compact()` (:139-187)** — compare `age >= self._prune_seconds` instead of `self.ttl_seconds`. The existing docstring gains a paragraph on why pruning and reading use different horizons. Everything else in the method is untouched.

**`_save()` (:189-232)** — `except Exception as e` in place of the tuple at :213. Extend the existing comment (:214-219) to enumerate `RecursionError` alongside `TypeError`/`ValueError`, citing the 2026-08-10 finding that `json.dump` raises it and the old tuple missed it. The `_dirty`-survives / `_retry_not_before` pacing behavior below it is unchanged: a broadened catch changes *which* failures are survived, never what a survived failure does to the counters.

**Class docstring (:89-95)** — the `ttl_days` paragraph is rewritten from "this is destructive, be careful" to the guarantee the code now provides, keeping the reason the distinction exists so the floor is not later "simplified" away.

### library_enrichment.py

No change. `enrich_library()`'s guarded `finally` (library_enrichment.py:246-249) stays exactly as it is — Change 2 makes it belt-and-braces rather than the only thing standing between a cache write failure and a misattributed run failure, which is the right relationship for a `finally` that logs.

## Commands

```
Tests:     C:\Python314\python.exe -m pytest tests/ -q
This file: C:\Python314\python.exe -m pytest tests/test_chorus_cache.py tests/test_library_enrichment.py -v
```

Baseline to hold: **827 passed, 1 skipped** — verified by running the suite at merge `2043079` before writing this spec, not quoted from the parent. Python 3.14, stdlib only, no new dependencies.

## Project Structure

Unchanged; this spec adds no files to the application.

```
chorus_cache.py                  → the only module this spec changes
library_enrichment.py            → sole construction site; read, not modified
tests/test_chorus_cache.py       → 11 of the 12 new tests
tests/test_library_enrichment.py → the end-to-end survival test
SPEC-chorus-cache-robustness.md  → this file
tasks/plan-chorus-cache-robustness.md, tasks/todo-chorus-cache-robustness.md
```

## Code Style

House convention: comments explain **why**, cite the dated incident that motivated them, and name the alternative that was rejected. The existing `_compact()` docstring (chorus_cache.py:139-157) is the model. A new comment that only restates the code is a review failure, and so is a hardening comment with no date on it.

Tests carry a docstring stating what breaks in production if the assertion ever goes green for the wrong reason — the convention `test_disk_write_failure_does_not_raise` (tests/test_chorus_cache.py:137-144) already sets, where the docstring is what stops someone deleting the load-bearing `flush()`.

## Testing Strategy

pytest under `tests/`, `monkeypatch` for the clock and for `chorus_cache.chorus_client.search_by_artist_title` via the existing `_stub` helper. No new fixtures, no new dependencies. **12 new tests, no existing test modified** — every current assertion in `tests/test_chorus_cache.py` stays green unedited, which is itself the regression check that this change is additive.

New section in `tests/test_chorus_cache.py` — **self-heal**:

- `test_deeply_nested_cache_file_is_survivable` — the pinning test for the headline defect. A hand-written file of ~200,000 nested arrays; construction must not raise and a lookup must still work. Fails on `main` with `RecursionError` out of `__init__`.
- `test_unparseable_cache_is_replaced_at_next_flush` — corrupt file in, `flush()`, assert the file on disk is now valid JSON. The half of "self-heal" that is not just "doesn't crash."
- `test_wrong_shape_cache_is_replaced_at_next_flush` — `[1, 2, 3]` gets the same treatment, since it takes the same branch.
- `test_unreadable_cache_is_not_replaced` — `open` raises `OSError` during construction only; after restoring it, `flush()` must leave the original file byte-identical. The guard against the self-heal eating a good cache it merely could not read.
- `test_load_failure_does_not_swallow_keyboardinterrupt` — `json.load` raises `KeyboardInterrupt`; it must propagate. Pins `except Exception`, not `BaseException`, against a future tidy-up.
- `test_compact_failure_is_survivable` — force an exotic raise from inside `_compact()`'s loop; construction still survives. Pins that the backstop covers compaction, not only the parse.
- `test_lookup_still_works_after_unrecoverable_load_failure` — the user-visible promise, asserted as behavior rather than as internal state.
- `test_save_does_not_raise_on_recursionerror` — the write-path half of Change 2: force `json.dump` to raise `RecursionError`; `flush()` must not raise, and `_dirty` must survive for the retry exactly as it does for `OSError`.

New section in `tests/test_chorus_cache.py` — **prune floor**:

- `test_shorter_ttl_does_not_delete_what_a_default_caller_would_serve` — a 5-day-old entry, client at `ttl_days=1`, `flush()`; the entry must still be on disk. Fails on `main`.
- `test_shorter_ttl_still_refuses_to_serve_a_stale_entry` — the other half: that same client must not *return* it. Read TTL and prune TTL are different questions and this pins both answers.
- `test_longer_ttl_prunes_at_its_own_horizon` — `ttl_days=30`: a 40-day entry goes, a 10-day entry stays. The floor must not become a ceiling.

New in `tests/test_library_enrichment.py`:

- `test_enrichment_survives_a_corrupt_chorus_cache` — garbage at the cache path, `enrich_library()` runs to completion and returns its summary. This is the one that would have caught the original defect at the level the user actually experiences it.

Regression: `tests/test_chorus_client*.py`, `tests/test_metadata_enrichment.py`, `tests/test_dedupe.py` and `tests/test_library_enricher_integration.py` must be untouched — none construct a `CachedChorusClient` and no contract they depend on moves.

Explicitly **not** tested: a live run against the real cache file. Decisions 2 and 3 are closures, not code, and have nothing to assert.

## Boundaries

### Always Do

- Keep the temp-file + `os.replace` write at **zero diff** against its pre-merge form. Change 2 touches the `except` line below it; the write block itself is not to be reformatted, re-commented, or "improved" in passing.
- `except Exception`, never `except BaseException`. Both new catch-alls, forever.
- Log `type(e).__name__` on every backstop path. A catch-all that logs an anonymous failure is how a real bug hides for months.
- Keep the specific handlers alongside the backstop. Specific = diagnosis, backstop = survival; collapsing them into one catch-all loses the log messages that make `log.txt` actionable.
- Cite the 2026-08-10 evidence in the code comments, per house convention.
- Run the full suite. Baseline 827 passed, 1 skipped.

### Ask First

- Widening `_CACHED_RESULT_FIELDS` — unchanged Ask First from the parent spec, restated because Decision 2 leans on it: `fsync` stays closed only while everything in the cache is recomputable.
- Changing `DEFAULT_TTL_DAYS`. It is now load-bearing in a second place — it is the prune floor — so re-tuning it has a new consequence the parent spec did not have.
- Adding an `atexit`/`signal` hook, a background flush thread, or a cross-process lock. Still a Never in the parent spec; re-listed here because "make the daemon-thread case safer" is the obvious wrong turn to take from this spec's Objective.

### Never Do

- Never mark the cache dirty on an `OSError` read failure. That single line is the difference between self-heal and clobbering a good cache that was momentarily locked.
- Never back up the bad file to `<cache>.corrupt` or similar as part of the reset. Considered and rejected: it adds a second write path inside a failure handler, accumulates files nobody prunes, and buys forensics on data that is recomputable by definition. The log line carrying the exception type is the diagnosis budget.
- Never delete or rebuild the live cache as a migration. Unchanged from the parent spec: ~7,800 lookups against an API known to rate-limit.
- Never weaken or edit an existing test to make this pass. This change is additive; if a current test goes red, that is a finding, not an obstacle.
- No new dependencies. Python 3.14, stdlib only.

## Success Criteria

1. No file content at `cache_path` — unparseable, wrong-shape, deeply nested, oversized `cached_at`, infinite `cached_at`, or unreadable — causes `CachedChorusClient(cache_path=...)` to raise. Proven by test, including a deep-nesting case that fails on `main`.
2. After construction against an **unusable** file, the first `flush()` leaves a valid JSON cache on disk. After construction against an **unreadable** one, the file is byte-identical.
3. `flush()` does not raise when `json.dump` itself fails, and the pending window survives for retry.
4. A client at `ttl_days=1` deletes nothing a `ttl_days=7` client would have served, while still refusing to serve stale entries itself.
5. `KeyboardInterrupt` during load propagates.
6. `C:\Python314\python.exe -m pytest tests/ -q` ≥ **839 passed, 1 skipped** (827 + 12), with no existing test modified.
7. `git diff` shows zero changed lines inside `_save()`'s temp-file + `os.replace` block.
8. Decisions 1-3 are recorded in this document with their reopening conditions, and the parent spec's Open Questions 4-6 are marked resolved against it.

## Open Questions

1. **A stale `.tmp` can survive a failed `json.dump`.** If serialization raises mid-write, the `with` closes a partial `<cache>.tmp` and `os.replace` never runs — the real cache is correctly intact, but the temp file lingers. Not fixed here for two reasons: it is self-clearing by construction (the next `_save()` reopens the same fixed path with `'w'`, truncating it, then replaces it away), and a cleanup `finally` would put lines on the write block Success Criterion 7 holds at zero. Recorded so a future reader knows it was seen. Reopens together with Decision 3 — `mkstemp` and temp cleanup are one change, not two.

2. **The backstop makes `_compact()`'s per-entry handler partly redundant.** `(KeyError, TypeError, OverflowError)` now has a net beneath it. Keeping both is deliberate: the per-entry handler drops *one bad entry* and keeps the rest, while the backstop discards the whole cache. Losing 7,742 good entries because one is malformed would be a real regression, so the fine-grained handler is doing work the backstop cannot. Flagged only because "the try/except above covers this" is a plausible and wrong simplification at a later `/code-simplify`.

3. **Cross-process concurrency** remains open, and this spec does not close it either — see the parent's Open Question 4 and SPEC-chorus-reliability-fix.md's Open Question 1. Change 1's OSError branch is chosen partly *because* of that race, but choosing not to make it worse is not the same as fixing it. Still belongs in one place covering both specs.

---

**Next phase**: `/plan` → `tasks/plan-chorus-cache-robustness.md` (self-heal read path first, since it carries the defect; then the write-path backstop; then the prune floor), then `/build`.
