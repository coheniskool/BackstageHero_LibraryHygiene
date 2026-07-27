# Plan: Chorus Lookup Reliability — Backoff and Concurrent-Run Protection

**Spec**: [`SPEC-chorus-reliability-fix.md`](../SPEC-chorus-reliability-fix.md) (amends `SPEC-library-enrichment.md`)

## Overview

Two independent, vertically-sliced fixes for tonight's real-run failures: (1) `chorus_client.search_by_artist_title()` gains bounded retry-with-backoff on 429/503/transient connection errors, honoring `Retry-After`; (2) `gui.py` gains a lock-guarded re-entrancy flag so a second `_maybe_start_enrichment()` call while one is already running is a no-op. Each task lands its own tests in the same task — no separate "write tests" task.

## Dependency Graph

```
Task 1: chorus_client.py retry/backoff + tests/test_chorus_client.py (new)   ── independent
Task 2: gui.py re-entrancy guard + tests/test_gui_enrichment_integration.py  ── independent
```

Tasks 1 and 2 touch disjoint files and have no code dependency on each other — either order, or parallel, is fine. Both are required before the final checkpoint (the spec's success criteria need both fixes).

---

## Task 1: `chorus_client.py` — bounded retry with backoff on 429/503/transient errors

**Description**: Restructure `search_by_artist_title()` (chorus_client.py:30-104) so the existing single `requests.post()` call runs inside a bounded retry loop (fixed cap, e.g. `CHORUS_MAX_ATTEMPTS = 3`). Retry only on: HTTP 429/503 (via `requests.exceptions.HTTPError` from `raise_for_status()`), and `requests.exceptions.ConnectionError`/`Timeout`. On 429/503, prefer the response's `Retry-After` header (seconds or HTTP-date) when present; otherwise use exponential backoff with a low base (e.g. 1s, 2s, 4s). All other exceptions (bad JSON, oversized body, wrong shape, non-retryable HTTP status) return `None` immediately on the first attempt, exactly as today — no behavior change for those paths. After exhausting retries, fall through to today's "log and return `None`" — the function must never raise, matching its own docstring's "never raises" contract, and `CHORUS_REQUEST_TIMEOUT_SECONDS` stays a per-attempt timeout, not a cumulative budget.

**Exact change** (chorus_client.py):
- Add `import time` and a stdlib import for HTTP-date parsing (`from email.utils import parsedate_to_datetime`).
- Add module constants: `CHORUS_MAX_ATTEMPTS = 3`, `CHORUS_RETRY_BASE_SECONDS = 1.0`, `CHORUS_RETRYABLE_STATUS_CODES = {429, 503}`.
- Add a helper `_retry_after_seconds(response)` that reads `response.headers.get('Retry-After')`, parses it as a plain integer/float number of seconds first, falls back to HTTP-date parsing via `parsedate_to_datetime`, and returns `None` if absent or unparseable (caller falls back to exponential backoff in that case).
- Add a helper `_backoff_seconds(attempt)` returning `CHORUS_RETRY_BASE_SECONDS * (2 ** attempt)`.
- Wrap the existing request+parse body in `for attempt in range(CHORUS_MAX_ATTEMPTS):`. Every success path still `return`s directly (dict or `None`) exactly as today. Add two new `except` clauses ahead of the existing bottom `except Exception`:
  - `except requests.exceptions.HTTPError as e:` — inspect `e.response.status_code`; if it's in `CHORUS_RETRYABLE_STATUS_CODES` and `attempt < CHORUS_MAX_ATTEMPTS - 1`, log at `warning`, `time.sleep(_retry_after_seconds(e.response) or _backoff_seconds(attempt))`, `continue`; otherwise fall through to the existing `log.error(...); return None`.
  - `except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:` — same retry-or-give-up shape, always exponential backoff (no `Retry-After` on a connection failure).
  - Existing `except Exception as e:` stays last, unchanged in wording/behavior, and still catches everything not handled above (JSON errors, oversized-response `return None`s are unaffected since they're `return`s inside the `try`, not exceptions).
- Docstring gets one added sentence describing the retry behavior; the "Never raises" sentence stays and must remain true.

**Acceptance criteria:**
- `search_by_artist_title()` still returns a dict or `None` in every case; no test can make it raise.
- A 429 with a `Retry-After: 2` header followed by a 200 returns the successful result, and the (mocked) sleep duration reflects `Retry-After`, not the exponential fallback.
- A 503 with no `Retry-After` header retries using exponential backoff (1s, then 2s, ...).
- A `ConnectionError`/`Timeout` is retried the same way (exponential backoff, no `Retry-After` lookup attempted).
- Retries exhausted (all `CHORUS_MAX_ATTEMPTS` attempts fail) → returns `None`, no exception, and at most `CHORUS_MAX_ATTEMPTS - 1` sleeps occurred.
- A non-retryable HTTP error (e.g. 404/500-not-in-set) or a malformed-response case (existing robustness tests) returns `None` on the *first* attempt with **zero** sleeps — no regression, no new latency for cases that were never the problem.
- `CHORUS_REQUEST_TIMEOUT_SECONDS` is passed identically on every attempt (not multiplied/accumulated).
- No real `time.sleep` executes during tests — sleep is monkeypatched to a recording stub.

**Verification:**
- New `tests/test_chorus_client.py` (mirrors `tests/test_chorus_client_robustness.py`'s `_FakeRaw`/`_FakeResponse` pattern, extended with `status_code`/`headers`/a `raise_for_status()` that raises a real `requests.exceptions.HTTPError` with `.response` set when `status_code >= 400`, matching real `requests` behavior):
  - `test_429_with_retry_after_header_then_success` — queue `[429 w/ Retry-After: '2', 200 w/ valid body]`, monkeypatch `chorus_client.time.sleep`, assert the result matches the 200 payload and the recorded sleep duration is `2.0`.
  - `test_503_with_no_retry_after_uses_exponential_backoff` — queue `[503, 503, 200]`, assert recorded sleeps are `[1.0, 2.0]` (base 1s, doubling) and the final result matches the 200 payload.
  - `test_connection_error_is_retried_then_succeeds` and `test_timeout_is_retried_then_succeeds` — first `requests.post` call raises `requests.exceptions.ConnectionError`/`requests.exceptions.Timeout`, second returns 200; assert one exponential-backoff sleep and a correct final result.
  - `test_retries_exhausted_returns_none_without_raising` — queue all `CHORUS_MAX_ATTEMPTS` responses as 429/503; assert `search_by_artist_title(...)` returns `None`, no exception propagates, and exactly `CHORUS_MAX_ATTEMPTS - 1` sleeps were recorded.
  - `test_non_retryable_http_status_returns_none_with_no_retry` — a single 404/400 response; assert `None` and **zero** sleeps (proves non-retryable errors aren't slowed down).
  - `test_retry_after_as_http_date_is_parsed` — `Retry-After` header set to an HTTP-date ~2s in the future; assert the recorded sleep is close to 2s (tolerance, e.g. `pytest.approx(2.0, abs=0.5)`), not the exponential fallback.
  - `test_timeout_kwarg_is_identical_on_every_attempt` — capture `requests.post` call kwargs across attempts (e.g. via a fake that appends `k['timeout']` to a list each call) and assert every attempt used `CHORUS_REQUEST_TIMEOUT_SECONDS` unchanged.
- Regression: `pytest tests/test_chorus_client_robustness.py -v` — unchanged, all still green (these fakes' `raise_for_status()` never raises, so the new retry branches are never entered for them).

**Dependencies**: None.

**Files touched:**
- `chorus_client.py`
- `tests/test_chorus_client.py` (new)

---

## ▶ CHECKPOINT 1

- [ ] `pytest tests/test_chorus_client.py tests/test_chorus_client_robustness.py -v` — all green.
- [ ] `pytest tests/ -v` — full suite green, no regression outside these two files (in particular `tests/test_chorus_cache.py` and `tests/test_metadata_enrichment.py`, both of which call into `chorus_client`/monkeypatch around it, must be unaffected since they never exercise a real HTTP error path).
- [ ] Manual read-check: `search_by_artist_title()`'s docstring still states "never raises," and every new `except` branch ends in `return` (dict or `None`), never a bare `raise`.

---

## Task 2: `gui.py` — re-entrancy guard on `_maybe_start_enrichment()` / `_run_enrichment()`

**Description**: Add a lock-guarded instance flag so a second `_maybe_start_enrichment()` call (from either of its two call sites: gui.py:1939 and gui.py:2962) while a prior `_run_enrichment()` thread is still in flight is a no-op — logged at `info`, no second thread spawned. The flag is set (under the lock) before the thread starts and cleared (under the lock) in a `finally` inside `_run_enrichment()`, so it clears even if `library_enrichment.enrich_library()` raises.

**Exact change** (gui.py):
- In `App.__init__`, alongside the existing `self._tool_running: bool = False` (gui.py:1445), add:
  ```python
  self._enrichment_lock = threading.Lock()
  self._enrichment_running = False
  ```
- `_maybe_start_enrichment()` (gui.py:1788-1799): after the existing `if not self._enrich_var.get() or not self._songs_folder: return` early-out, add the guard before spawning the thread:
  ```python
  with self._enrichment_lock:
      if self._enrichment_running:
          log.info('Library enrichment already running; skipping')
          return
      self._enrichment_running = True
  threading.Thread(target=self._run_enrichment, daemon=True).start()
  ```
  Extend the existing docstring with one sentence noting the no-op-while-running behavior.
- `_run_enrichment()` (gui.py:1801-1805): wrap the existing `try/except` body with a `finally` that clears the flag under the lock:
  ```python
  def _run_enrichment(self):
      try:
          library_enrichment.enrich_library(self._songs_folder)
      except Exception as e:
          log.warning('Library enrichment failed: %s', e)
      finally:
          with self._enrichment_lock:
              self._enrichment_running = False
  ```
- No change to the two call sites (gui.py:1939, gui.py:2962) — they keep calling `self._maybe_start_enrichment()` exactly as today; the guard is entirely internal to the two methods.

**Acceptance criteria:**
- Calling `_maybe_start_enrichment()` while `self._enrichment_running` is already `True` does not construct a `threading.Thread` and does not call `library_enrichment.enrich_library`.
- The skip path logs at `info` level on the `backstagehero` logger with a message identifying the skip (so "why didn't enrichment re-run after I rescanned" is answerable from `log.txt`, per the spec's Boundaries → Always Do).
- `_run_enrichment()` clears `self._enrichment_running` back to `False` in both the success case and the case where `library_enrichment.enrich_library()` raises (guard must never get "stuck" `True` after a crash).
- Two back-to-back `_maybe_start_enrichment()` calls while the first `enrich_library()` call is still blocked (real concurrent execution, not just two synchronous calls) result in `enrich_library` being invoked exactly once.
- All five existing tests in `tests/test_gui_enrichment_integration.py` still pass unmodified in their assertions (only the shared `_app_with()` fixture helper gains the two new attributes needed for the guard code path to run without an `AttributeError`).
- The re-entrancy guard is a plain skip-and-log, never a queue/block/retry of the second call (per spec's Never Do).

**Verification:**
- Update `tests/test_gui_enrichment_integration.py`'s `_app_with()` helper to also set `app._enrichment_lock = threading.Lock()` and `app._enrichment_running = False`, so the existing 5 tests keep working against the new guarded code path.
- Add `test_maybe_start_enrichment_is_a_noop_when_already_running`: set `app._enrichment_running = True` up front, monkeypatch `threading.Thread` with the existing fake recorder pattern, call `_maybe_start_enrichment()`, assert no `Thread` was constructed, and (using `caplog.at_level(logging.INFO, logger='backstagehero')`, matching `tests/test_background_mode_controller.py:297`'s convention) assert a log record mentioning the skip was emitted.
- Add `test_run_enrichment_clears_the_running_flag_on_success` and `test_run_enrichment_clears_the_running_flag_on_failure`: set `app._enrichment_running = True` beforehand (simulating a flag `_maybe_start_enrichment` already set), monkeypatch `library_enrichment.enrich_library` to a no-op / a raiser respectively, call `app._run_enrichment()` directly (no thread needed, matches the file's existing direct-call convention), and assert `app._enrichment_running is False` afterward in both cases.
- Add `test_second_maybe_start_enrichment_is_a_noop_while_first_is_running` — the one test in this task that needs genuine concurrency, following `tests/test_library_tools_dialog.py`'s real-`threading.Event()`-blocking-stub pattern (that file is the closest existing precedent for testing an in-flight-run guard) rather than the fake-`Thread` pattern used elsewhere in this file, since a fake `Thread.start()` never executes its target and so can't produce genuine "still running" state:
  - Real `threading.Lock()`/`False` on the app fixture (do **not** monkeypatch `threading.Thread` away for this test).
  - Monkeypatch `gui.library_enrichment.enrich_library` with a stub that appends to a `calls` list, sets an `entered = threading.Event()`, then blocks on `release = threading.Event()` until signaled.
  - Call `app._maybe_start_enrichment()` once (spawns a real background thread), `entered.wait(timeout)` to confirm it actually started, call `app._maybe_start_enrichment()` a second time (must be a no-op), `release.set()`, then poll/wait (bounded timeout) for `app._enrichment_running` to become `False` again (confirms the first thread's `finally` ran).
  - Assert `calls == [str(tmp_path)]` — `enrich_library` was invoked exactly once despite two `_maybe_start_enrichment()` calls.
- `pytest tests/test_gui_enrichment_integration.py -v` — full file green (existing 5 tests + 4 new tests = 9).

**Dependencies**: None.

**Files touched:**
- `gui.py`
- `tests/test_gui_enrichment_integration.py`

---

## ▶ CHECKPOINT 2

- [ ] `pytest tests/test_gui_enrichment_integration.py -v` — all 9 tests green.
- [ ] `pytest tests/ -v` — full suite green, no regression (in particular `tests/test_background_mode_gui_wiring.py` and `tests/test_background_mode_controller.py`, which also instantiate `gui.App` via `object.__new__` and could be affected if `_enrichment_lock`/`_enrichment_running` are read anywhere outside the two touched methods — confirm they are not).
- [ ] Manual read-check: the guard is skip-and-log only (no queueing/blocking of the second call), and the flag is cleared via `finally` (not just at the end of the `try`), so a raised exception can't leave it stuck `True`.

---

## Final Checkpoint (whole spec)

- [ ] `pytest tests/ -v` — full suite green (both tasks' tests plus zero regressions elsewhere).
- [ ] Diff review: exactly `chorus_client.py`, `gui.py`, `tests/test_chorus_client.py` (new), `tests/test_gui_enrichment_integration.py` touched — no changes to `chorus_cache.py` (per spec: "no structural change required" once the GUI guard prevents concurrent `CachedChorusClient` instances), no changes to the Chorus API request body/shape (chorus_client.py:45-69, explicitly Ask First), no cross-process lock file, no proactive rate-limiting, no GUI status-bar surfacing of enrichment state.
- [ ] Success criteria from the spec's Objective, both confirmed by the tests above rather than a live run: (1) a rate-limited Chorus API now degrades gracefully per song (bounded retries, then a clean `None`) instead of the entire rest of a run failing every single lookup; (2) two enrichment passes against the same library can never run concurrently in-process, closing the `CachedChorusClient`/`WinError 32`/silent-cache-loss race described in Root Cause 2.
- [ ] Confirm the "never raises" contract and the "skip-and-log, never queue" contract are both still true by inspection, not just by the tests passing (these are the two properties Boundaries → Never Do singles out).

---

## Out of Scope (explicitly, per spec)

- Cross-process locking (GUI + CLI `library_enricher.py` running simultaneously against the same library) — Open Question #1, a future spec.
- Proactive rate-limiting/throttling of Chorus requests (a fixed minimum delay between requests) — Open Question #2, reactive retry-after-429 only.
- Surfacing enrichment running/failed/skipped state in the GUI status bar — stays log-only, per Open Question #3 and `_maybe_start_enrichment`'s own "nothing the user needs to watch live" docstring philosophy.
- Any change to the Chorus API request body/shape (chorus_client.py:45-69).
- Any structural change to `chorus_cache.py` — its atomic single-writer `_save()` pattern is correct once Task 2 prevents concurrent writers; not touched.

## Risks

| Risk | Mitigation |
|------|-----------|
| Line numbers cited here drift before implementation | Re-verify exact line numbers at `/build` time (both files were read fresh for this plan on 2026-07-27, but any concurrent edit invalidates them) |
| A test relies on real `time.sleep` and slows the suite or flakes under load | Every new sleep-related assertion in Task 1 monkeypatches `chorus_client.time.sleep`; Task 2's one real-concurrency test uses `threading.Event` (deterministic signaling), not sleep-based polling, for synchronization — only the final "wait for flag to clear" step should poll with a short bounded timeout |
| Task 2's guard-attribute addition to `_app_with()` is missed, leaving existing tests red | Checkpoint 2 explicitly re-runs the full existing file, not just the new tests, to catch this |
