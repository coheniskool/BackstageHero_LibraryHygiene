# TODO: Chorus Cache Robustness — Self-Heal on Unrecoverable Load Failure

See [`plan-chorus-cache-robustness.md`](plan-chorus-cache-robustness.md) for full detail, acceptance criteria, and verification steps. Spec: [`../SPEC-chorus-cache-robustness.md`](../SPEC-chorus-cache-robustness.md).

Baseline before any of this: **827 passed, 1 skipped** (verified at merge `2043079`). Target: **839 passed, 1 skipped**, with **no existing test modified**.

## Task 1: Self-heal on the read path — `_load()` restructure + `_reset()`
- [x] `_load()` (:121-137) — parse into a local `entries`; assign `self._entries` only after the `isinstance` check passes
- [x] `except OSError as e` first — keep the `'Could not read Chorus cache %s: %s'` wording, `self._entries = {}`, **no dirty mark**, with the WinError 32 / concurrent-`os.replace` reasoning in the comment
- [x] Wrong-shape branch — message to `'is not an object; starting empty'`, call `self._reset()`
- [x] `except Exception as e` backstop — log `type(e).__name__`, call `self._reset()`; comment carries the 2026-08-10 `RecursionError` verification, what the old behavior cost (dead enrichment every run, silent on a GUI daemon thread), and why `Exception` not `BaseException`
- [x] In-code note that handler ORDER is load-bearing: `OSError` before the catch-all, or the non-clobbering branch is unreachable
- [x] Add private `_reset()` — `self._entries = {}` + `self._dirty += 1`, docstring saying the dirty mark IS the self-heal and why the `OSError` path deliberately skips it
- [x] `test_deeply_nested_cache_file_is_survivable` — confirm it fails on `main` before it passes here
- [x] `test_unparseable_cache_is_replaced_at_next_flush`
- [x] `test_wrong_shape_cache_is_replaced_at_next_flush`
- [x] `test_unreadable_cache_is_not_replaced` — byte-compare the file after construct + `flush()`
- [x] `test_load_failure_does_not_swallow_keyboardinterrupt`
- [x] `test_compact_failure_is_survivable`
- [x] `test_unrecoverable_load_failure_costs_one_relookup_not_a_dead_feature` (planned as `test_lookup_still_works_after_...`; renamed to state the promise rather than the absence of a crash)
- [x] `test_enrichment_survives_a_corrupt_chorus_cache` (tests/test_library_enrichment.py)
- [x] `pytest tests/test_chorus_cache.py tests/test_library_enrichment.py -v` green

## ▶ Checkpoint 1
- [x] `pytest tests/ -q` full suite green (827 baseline + 8)
- [x] Read-check: the `OSError` branch does **not** call `_reset()` — the one line the spec's Never Do list is about
- [x] Read-check: flip the catch-all to `BaseException` locally, confirm `test_load_failure_does_not_swallow_keyboardinterrupt` goes red, flip it back
- [x] Read-check: `git diff tests/test_chorus_cache.py` shows additions only
- [x] Read-check: `_save()` untouched by this task

## Task 2: Self-heal on the write path — `_save()`'s exception handler
- [x] `_save()` (:213) — `except (OSError, TypeError, ValueError)` → `except Exception as e`
- [x] Extend the existing comment (:214-219), don't replace it: keep the OSError/TypeError/ValueError reasoning, add `RecursionError` + the 2026-08-10 `json.dump` finding, and state the accepted cost of a catch-all masking a later refactor's bug
- [x] Add `type(e).__name__` to the `log.warning`
- [x] Confirm nothing below the handler moved — `_dirty` survives, `_retry_not_before` still set one `FLUSH_EVERY_SECONDS` out, success path still resets all three
- [x] `test_save_does_not_raise_on_recursionerror` — and the pending entry lands on the next good flush
- [x] `pytest tests/test_chorus_cache.py -v` green

## Task 3: Prune floor — `ttl_days` stops being destructive
- [x] `__init__` (:98-119) — `self._prune_seconds = max(self.ttl_seconds, DEFAULT_TTL_DAYS * _SECONDS_PER_DAY)`, **before** `_load()`
- [x] Existing initialization-order comment (:102-114) gains one clause naming `_prune_seconds` — not a rewrite
- [x] `_compact()` (:175) — `age >= self._prune_seconds`; the only line of logic that moves
- [x] `_compact()` docstring — paragraph on why reading and pruning use different horizons (only pruning is destructive)
- [x] Class docstring (:89-95) — rewrite the `ttl_days` paragraph from warning to guarantee, keeping the reason so the `max()` isn't later "simplified" away
- [x] `test_shorter_ttl_does_not_delete_what_a_default_caller_would_serve` — confirm it fails on `main` first
- [x] `test_shorter_ttl_still_refuses_to_serve_a_stale_entry`
- [x] `test_longer_ttl_prunes_at_its_own_horizon`
- [x] Regression: `test_entry_expires_after_ttl` (:78) and `test_entry_at_exactly_ttl_is_pruned` (:780) pass unmodified
- [x] `pytest tests/test_chorus_cache.py -v` green

## ▶ Checkpoint 2 (final)
- [x] `pytest tests/ -q` → **839 passed, 1 skipped**
- [x] `git diff --stat` — `chorus_cache.py` only; `library_enrichment.py` unchanged; `tests/` additions only
- [x] Read-check: **zero** changed lines inside `_save()`'s temp-file + `os.replace` block (Success Criterion 7)
- [x] Read-check: every new comment carries its date and its why; no comment restates its code
- [x] Mark SPEC-chorus-cache-write-perf.md's Open Questions **5 and 6** resolved against this spec; **4 stays open** (cross-process concurrency — this change declines to make it worse, which is not a fix). Success Criterion 8 corrected to match

## Not in scope — decided, not deferred again
- [x] **`RecursionError` handler** — subsumed by Task 1's backstop; gets a regression test, not a handler (Spec Decision 1)
- [x] **`fsync`** — no. The self-heal is the cheaper answer to the same risk; a partial fsync would claim durability Windows can't deliver (Spec Decision 2). Reopens only if the cache ever holds something not recomputable
- [x] **`mkstemp` for the `.tmp` sibling** — no. `M:` measured 2026-08-10 as a local fixed disk, no mapped drives, admin-only default shares, one enabled user; the stated trigger isn't met and the change would put lines on the protected write block (Spec Decision 3). Reopens on a UNC path, mapped drive, explicit share, or a second interactive user
- [x] **The 2026-08-10 stall investigation** — separate thread, separate evidence, untouched by any of this
