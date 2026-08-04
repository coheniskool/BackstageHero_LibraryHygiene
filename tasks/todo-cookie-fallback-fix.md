# TODO: Browser-Cookie DPAPI Failure Fallback + Console Unicode Crash Fix

See [`plan-cookie-fallback-fix.md`](plan-cookie-fallback-fix.md) for full detail, acceptance criteria, and verification steps. Spec: [`../SPEC-cookie-fallback-fix.md`](../SPEC-cookie-fallback-fix.md).

## Task 1: `VideoDownload.py` — console-encoding safety
- [x] Add `library_common.make_console_encoding_safe()` call at `VideoDownload.py:15-16`, right after the existing `library_common.ensure_stdio_not_none()` call
- [x] Confirm no other change needed in `library_common.py` (function already exists) or `gui.py` (no observed crash there)
- [x] `tests/test_phase3_fixes.py`: add `test_the_replacement_stream_survives_tonights_crashed_string`
- [x] `tests/test_phase3_fixes.py`: add `test_videodownload_makes_console_encoding_safe_before_anything_prints` (source-presence check)
- [x] `pytest tests/test_phase3_fixes.py -v` green (33 passed)

## ▶ Checkpoint 1
- [x] `pytest tests/ -v` full suite green (698 passed, 1 skipped)
- [x] Manual read-check: new call precedes `import updater`/`updater.prefer_cached_ytdlp()` and `import yt_dlp`

## Task 2: `VideoDownload.py` — cookie-decrypt detection + retry helper + `_base_opts()` gate
- [x] Add `_COOKIES_BROKEN = False` global next to `USE_BROWSER_COOKIES`/`COOKIE_BROWSER` (`:642-643`)
- [x] Add `_COOKIE_ERROR_SIGNS` tuple and `_is_cookie_decrypt_error(exc)` helper (mirrors `is_bot_error`)
- [x] Add `_run_ytdlp_with_cookie_fallback(opts, fn)` helper (catch, warn-once, set flag, retry cookie-free)
- [x] Gate `_base_opts()` with `and not _COOKIES_BROKEN`
- [x] Confirm `configure_cookies()` body is unchanged — does not touch `_COOKIES_BROKEN`
- [x] `tests/test_cookie_support.py`: extend `setup_function`/`teardown_function` to also reset `vd._COOKIES_BROKEN = False`
- [x] `tests/test_cookie_support.py`: add fake `yt_dlp.YoutubeDL` stand-in class (context manager, configurable behaviors list)
- [x] `test_is_cookie_decrypt_error_matches_dpapi_and_cookie_load_text`
- [x] `test_is_cookie_decrypt_error_does_not_match_bot_or_unrelated_errors`
- [x] `test_base_opts_omits_cookies_for_the_next_call_once_broken`
- [x] `test_run_ytdlp_with_cookie_fallback_succeeds_normally_with_one_construction`
- [x] `test_run_ytdlp_with_cookie_fallback_retries_once_cookie_free_on_dpapi_failure`
- [x] `test_run_ytdlp_with_cookie_fallback_reraises_non_cookie_errors_without_retry`
- [x] `test_cookie_fallback_logs_the_warning_exactly_once`
- [x] (plus 2 extra tests beyond plan: exact-case-insensitivity check, and "no cookiesfrombrowser in opts" ignores DPAPI text)
- [x] `pytest tests/test_cookie_support.py -v` green (17 passed)

## ▶ Checkpoint 2
- [x] `pytest tests/ -v` full suite green (706 passed, 1 skipped)
- [x] Manual read-check: `configure_cookies()` diff is empty; `_COOKIES_BROKEN` only touched in the three new/changed spots

## Task 3: Wire `search_candidates()`
- [x] Replace bare `with yt_dlp.YoutubeDL(opts) as ydl: ...` with `_run_ytdlp_with_cookie_fallback(opts, lambda ydl: ydl.extract_info(...))`, existing `try/except` wrapping unchanged
- [x] `test_search_candidates_retries_without_cookies_after_dpapi_failure`
- [x] `test_search_candidates_bot_error_is_not_treated_as_a_cookie_failure`
- [x] `pytest tests/test_cookie_support.py -v` green (19 passed)

## Task 4: Wire `fetch_audio()`
- [x] Replace bare `with yt_dlp.YoutubeDL(opts) as ydl: ...` with `_run_ytdlp_with_cookie_fallback(opts, lambda ydl: ydl.extract_info(url, download=True))`, swallow-on-failure contract unchanged
- [x] `test_fetch_audio_recovers_after_dpapi_failure`
- [x] (plus `test_fetch_audio_still_swallows_non_cookie_non_bot_errors` — pins the pre-existing contract explicitly)
- [x] Manual read-check: `select_video` and `process_resync` call sites unaffected (return shape untouched)
- [x] `pytest tests/test_cookie_support.py -v` green (21 passed)

## Task 5: Wire `download_video()`
- [x] Extract inner `process_ie_result`/`download` fallback logic into a `_do(ydl)` closure
- [x] Call `_run_ytdlp_with_cookie_fallback(opts, _do)` in place of the current `with yt_dlp.YoutubeDL(opts) as ydl:` block
- [x] Confirm remux logic below the `try` block is untouched
- [x] `test_download_video_retries_without_cookies_after_dpapi_failure`
- [x] (plus `test_download_video_bot_error_is_not_treated_as_a_cookie_failure`)
- [x] `pytest tests/test_cookie_support.py -v` green (23 passed)

## ▶ Checkpoint 3
- [x] `pytest tests/test_cookie_support.py -v` green (Tasks 2-5 combined, 23 passed)
- [x] `pytest tests/ -v` full suite green (712 passed, 1 skipped) — `test_select_video.py`, `test_dump_video.py` both unaffected
- [x] Manual read-check: all three call sites still preserve their original `except Exception as e: if is_bot_error(e): raise BotDetected(...)` wrapping

## Task 6: Full regression + scope/diff review
- [x] `pytest tests/ -v` full suite green (712 passed, 1 skipped)
- [x] `git diff --stat` shows only `VideoDownload.py`, `tests/test_cookie_support.py`, `tests/test_phase3_fixes.py` code changes (plus new spec/plan/todo docs)
- [x] Re-confirm spec's "Never Do" list against the final diff: no `time.sleep`/backoff added inside `_run_ytdlp_with_cookie_fallback`; `RotatingFileHandler` setup untouched (empty diff)

## ▶ Final Checkpoint
- [x] `pytest tests/ -v` full suite green (712 passed, 1 skipped)
- [x] Diff review: exactly `VideoDownload.py`, `tests/test_cookie_support.py`, `tests/test_phase3_fixes.py` touched — `gui.py`, `library_common.py`, `settings.json` untouched
- [x] Both spec success criteria confirmed by tests: (1) DPAPI cookie failure recovers the same song and only warns once (Task 2/3/4/5 tests); (2) non-cp1252 titles can't crash print() (Task 1 tests)
- [x] Confirm `configure_cookies()` never resets `_COOKIES_BROKEN` (diff for that function is empty — verified at Checkpoint 2)

---

### Notes
- Line numbers verified live against current code at plan time (2026-08-04) — re-verify at `/build` time if this drifts.
- Task 1 is fully independent of Tasks 2-5 — any order, or parallel.
- Tasks 3, 4, 5 each depend on Task 2 but are independent of each other.
