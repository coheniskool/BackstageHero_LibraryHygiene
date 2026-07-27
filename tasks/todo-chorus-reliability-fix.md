# TODO: Chorus Lookup Reliability — Backoff and Concurrent-Run Protection

See [`plan-chorus-reliability-fix.md`](plan-chorus-reliability-fix.md) for full detail, acceptance criteria, and verification steps. Spec: [`../SPEC-chorus-reliability-fix.md`](../SPEC-chorus-reliability-fix.md).

## Task 1: `chorus_client.py` — bounded retry with backoff on 429/503/transient errors
- [x] Add `import time`, `from email.utils import parsedate_to_datetime`
- [x] Add constants `CHORUS_MAX_ATTEMPTS = 3`, `CHORUS_RETRY_BASE_SECONDS = 1.0`, `CHORUS_RETRYABLE_STATUS_CODES = {429, 503}`
- [x] Add `_retry_after_seconds(response)` helper (numeric seconds, then HTTP-date fallback via `parsedate_to_datetime`, else `None`)
- [x] Add `_backoff_seconds(attempt)` helper (`CHORUS_RETRY_BASE_SECONDS * (2 ** attempt)`)
- [x] Wrap `search_by_artist_title()`'s request+parse body in `for attempt in range(CHORUS_MAX_ATTEMPTS):`
- [x] Add `except requests.exceptions.HTTPError` branch: retry on 429/503 if attempts remain, using `Retry-After` or backoff; otherwise fall through to existing log+return None
- [x] Add `except (requests.exceptions.ConnectionError, requests.exceptions.Timeout)` branch: same retry-or-give-up shape, backoff only
- [x] Keep existing bottom `except Exception` unchanged, still last
- [x] Update docstring: note retry behavior, keep "never raises" statement
- [x] Create `tests/test_chorus_client.py` (new) with `_FakeResponse` supporting `status_code`/`headers`/a real-`HTTPError`-raising `raise_for_status()`
- [x] `test_429_with_retry_after_header_then_success`
- [x] `test_503_with_no_retry_after_uses_exponential_backoff`
- [x] `test_connection_error_is_retried_then_succeeds`
- [x] `test_timeout_is_retried_then_succeeds`
- [x] `test_retries_exhausted_returns_none_without_raising`
- [x] `test_non_retryable_http_status_returns_none_with_no_retry`
- [x] `test_retry_after_as_http_date_is_parsed`
- [x] `test_timeout_kwarg_is_identical_on_every_attempt`
- [x] `pytest tests/test_chorus_client.py tests/test_chorus_client_robustness.py -v` green (16/16)

## ▶ Checkpoint 1
- [x] `pytest tests/ -v` full suite green (579 passed, 1 skipped; 1 pre-existing unrelated failure in `test_cookie_settings_gui.py` — reads this machine's real `settings.json`, `use_browser_cookies: true` from actual prior usage, not touched by this diff)
- [x] Manual read-check: docstring still says "never raises"; every branch returns, never bare-raises

## Task 2: `gui.py` — re-entrancy guard on `_maybe_start_enrichment()` / `_run_enrichment()`
- [x] `App.__init__` — add `self._enrichment_lock = threading.Lock()` and `self._enrichment_running = False` near `self._tool_running` (gui.py:1445)
- [x] `_maybe_start_enrichment()` (gui.py:1788) — add lock-guarded check-and-set before spawning the thread; log at `info` and return if already running; extend docstring
- [x] `_run_enrichment()` (gui.py:1801) — clear the flag under the lock in a `finally`
- [x] `tests/test_gui_enrichment_integration.py` — update `_app_with()` to set `_enrichment_lock`/`_enrichment_running` so existing 5 tests keep passing
- [x] `test_maybe_start_enrichment_is_a_noop_when_already_running` (fake `Thread`, `caplog.at_level(logging.INFO, logger='backstagehero')` for the skip message)
- [x] `test_run_enrichment_clears_the_running_flag_on_success`
- [x] `test_run_enrichment_clears_the_running_flag_on_failure`
- [x] `test_second_maybe_start_enrichment_is_a_noop_while_first_is_running` (real `threading.Event`-blocking stub, real threads — following `tests/test_library_tools_dialog.py`'s concurrency-test pattern, not the fake-`Thread` pattern)
- [x] `pytest tests/test_gui_enrichment_integration.py -v` green (10 tests total)

## ▶ Checkpoint 2
- [x] `pytest tests/ -v` full suite green (583 passed, 1 skipped; same pre-existing unrelated `test_cookie_settings_gui.py` failure as Checkpoint 1 — `test_background_mode_gui_wiring.py`/`test_background_mode_controller.py` unaffected)
- [x] Manual read-check: skip-and-log only, flag cleared via `finally`

## ▶ Checkpoint (final)
- [x] `pytest tests/ -v` full suite green (modulo the pre-existing, unrelated `test_cookie_settings_gui.py` failure noted above)
- [x] Diff review: only `chorus_client.py`, `gui.py`, `tests/test_chorus_client.py` (new), `tests/test_gui_enrichment_integration.py` touched — `chorus_cache.py` untouched, no cross-process lock, no proactive throttle, no GUI status surfacing
- [x] Confirm both spec success criteria in the plan file's Final Checkpoint section

---

### Notes
- Line numbers verified live against current code at plan time (2026-07-27) — re-verify at `/build` time if this drifts.
- Tasks 1 and 2 are fully independent (disjoint files) — any order, or parallel.
