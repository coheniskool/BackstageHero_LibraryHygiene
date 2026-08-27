# Plan: Chorus Cache Robustness — Self-Heal on Unrecoverable Load Failure

**Spec**: [`SPEC-chorus-cache-robustness.md`](../SPEC-chorus-cache-robustness.md) (closes SPEC-chorus-cache-write-perf.md's Open Questions 4-6)

## Overview

Three changes to `chorus_cache.py` and nothing else. Each is a complete, independently-verifiable path, ordered so the one carrying the actual defect lands first:

1. **Self-heal on the read path** — `_load()` survives anything short of `BaseException`, and splits reset behavior by cause so an unreadable file is not confused with a bad one. This is the spec's headline item and subsumes the `RecursionError` finding.
2. **Self-heal on the write path** — the same hole in `_save()`'s handler, found while writing the spec: `json.dump` raises `RecursionError` too.
3. **Prune floor** — `_compact()` stops deleting entries a default-TTL caller would still serve.

Each task lands its own tests, matching `plan-chorus-cache-write-perf.md`'s convention. No task modifies an existing test; if one goes red, that is a finding to stop on, not to fix by editing the test.

`library_enrichment.py` is deliberately not modified. Its guarded `finally` (library_enrichment.py:246-249) becomes belt-and-braces once Task 2 lands, which is the right relationship for a `finally` that logs.

## Architecture Decisions

- **Backstop, not a wider exception tuple.** Widening `_load()` to `(OSError, ValueError, RecursionError)` fixes the one input we happened to think of. The spec's Decision 1 records why the parent spec already reached this conclusion: the point is to stop enumerating.
- **Reset behavior splits by cause, and that split is the design.** Unreadable (`OSError`) → in-memory reset only. Unusable (everything else) → reset *and* mark dirty so the next flush replaces the garbage. `WinError 32` is transient-but-repeated in this codebase, so treating a read failure as proof the file is bad would let a momentary lock clobber a good 34 MB cache with `{}` — worse than the bug being fixed.
- **`except Exception`, never `except BaseException`.** Ctrl-C during the 0.80 s load of a 126 MB file must abort the run. Pinned by its own test in Task 1 so a later tidy-up cannot widen it.
- **The specific handlers stay alongside the backstop.** Specific = diagnosis (an actionable `log.txt` line), backstop = survival. Collapsing them loses the messages. Same reason `_compact()`'s per-entry handler stays: it drops *one* bad entry and keeps 7,742 good ones, which the backstop cannot do.
- **`_reset()` is a two-line private method, not an inlined pair of statements.** Three call sites, one rule, and the docstring is where "the dirty mark IS the self-heal" gets said once instead of three times.
- **Read TTL and prune TTL are different questions.** `self.ttl_seconds` answers "will I serve this?"; `self._prune_seconds = max(ttl_seconds, DEFAULT_TTL_DAYS * _SECONDS_PER_DAY)` answers "may I delete this for everyone?" Flooring only the second one fixes the destructive direction and leaves the other alone.
- **Nothing touches the atomic write.** Task 2 changes the `except` line *below* the `os.replace` block. Success Criterion 7 holds that block at zero diff, and it is checked by reading the diff, not by assuming.

## Dependency Graph

```
Task 1: self-heal read path (_load restructure + _reset)
   │        chorus_cache.py, tests/test_chorus_cache.py, tests/test_library_enrichment.py
   │
   ├── Task 2: self-heal write path (_save except-clause)     ── independent of Task 3
   │        chorus_cache.py, tests/test_chorus_cache.py
   │
   └── Task 3: prune floor (_prune_seconds)                   ── needs Task 1's _load in its final shape
            chorus_cache.py, tests/test_chorus_cache.py
```

Tasks 2 and 3 are logically independent of each other. Task 3 edits `_compact()` and `__init__`, which Task 1 also touches, so run it after Task 1 rather than alongside. Task 2 touches only `_save()` and can go either side of Task 3 — the order below is chosen so both self-heal halves land together and can be reviewed as one idea.

---

## Task 1: Self-heal on the read path — `_load()` restructure + `_reset()`

**Description**: Make `CachedChorusClient.__init__` survive any cache-file content. Restructure `_load()` around one `try` with three ordered handlers (`OSError` → in-memory reset; the wrong-shape branch and a catch-all `except Exception` → reset *and* mark dirty). Add a private `_reset()`. `KeyboardInterrupt` must still propagate.

**Exact change** (`chorus_cache.py`, `_load` at :121-137):

- Parse into a **local** `entries` and assign `self._entries` only after the `isinstance` check passes, so no failure path can leave the instance holding a half-processed structure.
- `except OSError as e` — keep the existing `'Could not read Chorus cache %s: %s'` wording. `self._entries = {}` and **no dirty mark**, with the comment explaining why: `WinError 32` (SPEC-chorus-reliability-fix.md) and a concurrent process mid-`os.replace` (gui.py:1975-1980) both look exactly like this, and overwriting a file we could not read *this once* would replace a good cache with `{}`.
- Wrong-shape branch — message changes from `'is not an object; ignoring'` to `'is not an object; starting empty'` (it no longer merely ignores; it replaces), and calls `self._reset()`.
- `except Exception as e` — the backstop. Logs `type(e).__name__` alongside the message, calls `self._reset()`. Comment carries: the 2026-08-10 verification that `json.load` raises `RecursionError` on Python 3.14.4 and that it is **not** a `ValueError`; what the old behavior cost (escaped `__init__` before anything was written, so the bad file was never pruned or replaced — enrichment dead every run until the user deleted the file by hand, and under the GUI dead on a daemon thread with the app still running); and why it is `Exception` and not `BaseException`.
- Handler order matters and is not incidental: `OSError` must precede the catch-all or the non-clobbering branch becomes unreachable. Say so in the code.

**New private method** (`chorus_cache.py`, beside `_compact`):

```python
    def _reset(self):
        """Empty the cache AND mark it for replacement on disk.

        The dirty mark is the self-heal: without it a file we couldn't make
        sense of survives every run that happens to have zero cache misses,
        and we pay the same failed parse forever. Deliberately NOT used for a
        read failure -- see _load()'s OSError branch.
        """
        self._entries = {}
        self._dirty += 1
```

**Verify**: `C:\Python314\python.exe -m pytest tests/test_chorus_cache.py tests/test_library_enrichment.py -v`

**Acceptance**:
- A ~200,000-deep nested JSON file at `cache_path` no longer raises out of `__init__` (fails on `main` with `RecursionError`).
- An unusable file is replaced with valid JSON at the first `flush()`; an unreadable one is left byte-identical.
- `KeyboardInterrupt` raised from `json.load` propagates.
- A raise from inside `_compact()` is survived too.
- All 34 existing `tests/test_chorus_cache.py` tests pass **unmodified**.

**Files**: `chorus_cache.py`, `tests/test_chorus_cache.py`, `tests/test_library_enrichment.py`

**Tests** (8 new):

| Test | Pins |
|---|---|
| `test_deeply_nested_cache_file_is_survivable` | the headline defect; fails on `main` |
| `test_unparseable_cache_is_replaced_at_next_flush` | the half of self-heal that isn't just "doesn't crash" |
| `test_wrong_shape_cache_is_replaced_at_next_flush` | same branch, second input |
| `test_unreadable_cache_is_not_replaced` | the guard against self-heal eating a good, momentarily-locked cache |
| `test_load_failure_does_not_swallow_keyboardinterrupt` | `Exception`, not `BaseException` |
| `test_compact_failure_is_survivable` | the backstop covers compaction, not only the parse |
| `test_lookup_still_works_after_unrecoverable_load_failure` | the promise as behavior, not internal state |
| `test_enrichment_survives_a_corrupt_chorus_cache` (`test_library_enrichment.py`) | the defect at the level the user experiences it |

`test_unreadable_cache_is_not_replaced` needs care: make `open` raise `OSError` during construction only, restore it, then `flush()` and compare the file's bytes to what was written. Asserting "no write happened" via the `os.replace` seam (`_count_saves`, tests/test_chorus_cache.py:182) is the cheaper form and either is acceptable — byte comparison is preferred because it asserts the outcome rather than the mechanism.

---

## ▶ Checkpoint 1

- `C:\Python314\python.exe -m pytest tests/ -q` green — 827 baseline + 8.
- Read-check: `OSError` branch does **not** call `_reset()`. This is the single line the spec's Never Do list is about.
- Read-check: the catch-all is `except Exception`, and the `KeyboardInterrupt` test genuinely fails if it is widened (flip it locally to `BaseException` and confirm red, then flip back).
- Read-check: no existing test was edited — `git diff tests/test_chorus_cache.py` shows additions only.
- Read-check: `_save()` untouched by this task.

---

## Task 2: Self-heal on the write path — `_save()`'s exception handler

**Description**: `_save()` catches `(OSError, TypeError, ValueError)` (chorus_cache.py:213) and promises in its own docstring that a run must never fail because it could not persist a cache entry. Verified 2026-08-10 on Python 3.14.4: `json.dump` raises `RecursionError` on deeply nested input, which that tuple misses — the same gap Task 1 just closed on the read side.

**Exact change** (`chorus_cache.py`, `_save` at :213-227):

- `except (OSError, TypeError, ValueError) as e` → `except Exception as e`.
- Extend the existing comment (:214-219) rather than replacing it: keep the `OSError`-is-expected and `TypeError`/`ValueError`-cover-non-JSON-native-values reasoning, add `RecursionError` with the date, and state the accepted cost — a catch-all can mask a bug introduced by a later refactor as a logged warning instead of a red test, which is the price of a never-raise contract and the same price this function already pays for `OSError`.
- Add `type(e).__name__` to the `log.warning` so an unanticipated cause is identifiable in `log.txt` rather than anonymous.
- **Nothing below the handler changes.** `_dirty` still survives, `_retry_not_before` is still set one `FLUSH_EVERY_SECONDS` out, and the success path still resets all three counters. Broadening the catch changes *which* failures are survived, never what a survived failure does to the bookkeeping.
- **Nothing above the handler changes.** The `with_name` / `open` / `json.dump` / `os.replace` block is at zero diff.

**Verify**: `C:\Python314\python.exe -m pytest tests/test_chorus_cache.py -v`

**Acceptance**:
- `flush()` does not raise when `json.dump` raises `RecursionError`.
- The pending window survives that failure and lands on the next successful flush — same as the `OSError` path.
- `git diff` shows zero changed lines between `tmp_path = ...` and `os.replace(...)`.

**Files**: `chorus_cache.py`, `tests/test_chorus_cache.py`

**Tests** (1 new):

- `test_save_does_not_raise_on_recursionerror` — monkeypatch `cc.json.dump` to raise `RecursionError`; `flush()` must not raise, and a subsequent flush with `json.dump` restored must write the entry that was pending. Reuses the flaky-`open` shape of `test_failed_save_keeps_dirty_window_for_retry` (tests/test_chorus_cache.py:268).

---

## Task 3: Prune floor — `ttl_days` stops being destructive

**Description**: `_compact()` prunes against `self.ttl_seconds` and writes the result back, so a shorter `ttl_days` permanently deletes entries a default-TTL caller would still have served. Floor the *prune* horizon at the default while leaving the *read* horizon per-instance.

**Exact change** (`chorus_cache.py`):

- `__init__` (:98-119) — add beside `self.ttl_seconds`, **before** `_load()`:
  ```python
  self._prune_seconds = max(self.ttl_seconds, DEFAULT_TTL_DAYS * _SECONDS_PER_DAY)
  ```
  The existing initialization-order comment (:102-114) gains one clause naming `_prune_seconds` as another thing `_load()` reads — not a rewrite.
- `_compact()` (:175) — `age >= self._prune_seconds` in place of `age >= self.ttl_seconds`. That is the only line of logic that moves.
- `_compact()` docstring (:139-157) — a paragraph on why the two horizons differ: reading asks "will I serve this?", pruning asks "may I delete this for everyone?", and only the second is destructive.
- Class docstring (:89-95) — rewrite the `ttl_days` paragraph from a warning into the guarantee. Keep the *reason* the distinction exists, so the floor is not later "simplified" away by someone who reads `max()` of a constant and a value as redundant.

**Verify**: `C:\Python314\python.exe -m pytest tests/test_chorus_cache.py -v`

**Acceptance**:
- A client at `ttl_days=1` deletes nothing a `ttl_days=7` client would serve, and still refuses to serve stale entries itself.
- A client at `ttl_days=30` prunes at 30 days, not 7 — the floor must not become a ceiling.
- `test_entry_expires_after_ttl` (:78) and `test_entry_at_exactly_ttl_is_pruned` (:780) pass unmodified; both run at the default, where `max(7d, 7d)` is `7d`.

**Files**: `chorus_cache.py`, `tests/test_chorus_cache.py`

**Tests** (3 new):

- `test_shorter_ttl_does_not_delete_what_a_default_caller_would_serve` — 5-day-old entry, `ttl_days=1`, `flush()`, entry still on disk. Fails on `main`.
- `test_shorter_ttl_still_refuses_to_serve_a_stale_entry` — the other half; that same client must go to the network.
- `test_longer_ttl_prunes_at_its_own_horizon` — `ttl_days=30`: 40-day entry pruned, 10-day entry kept.

---

## ▶ Checkpoint 2 (final)

- `C:\Python314\python.exe -m pytest tests/ -q` → **839 passed, 1 skipped** (827 + 12).
- `git diff --stat` — `chorus_cache.py` only; `library_enrichment.py` unchanged; `tests/` additions only.
- Read-check: zero changed lines inside `_save()`'s temp-file + `os.replace` block (Success Criterion 7).
- Read-check: every new comment carries its date and its why; no comment restates its code.
- Update SPEC-chorus-cache-write-perf.md's Open Questions 4-6 to point at this spec as resolved, per Success Criterion 8.

## Risks

| Risk | Mitigation |
|---|---|
| A catch-all hides a real bug introduced later | `type(e).__name__` in every backstop log line; the accepted cost is stated in the spec and in the code comment, not discovered later |
| Someone "simplifies" `except Exception` to `except BaseException` for symmetry | `test_load_failure_does_not_swallow_keyboardinterrupt` goes red |
| Someone deletes `_compact()`'s per-entry handler as redundant under the backstop | Spec Open Question 2 records why it is not: fine-grained drops one entry, backstop drops all 7,743 |
| Someone reads `max(self.ttl_seconds, DEFAULT_TTL_DAYS * _SECONDS_PER_DAY)` as redundant | Class docstring and `_compact()` docstring both state the guarantee it provides; `test_shorter_ttl_does_not_delete_what_a_default_caller_would_serve` goes red |
| The self-heal clobbers a good cache during the known GUI+CLI race | The `OSError` branch never marks dirty — the deliberate asymmetry, with its own test |
