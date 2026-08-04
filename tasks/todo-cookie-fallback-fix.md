# TODO: Browser-Cookie DPAPI Failure Fallback + Console Unicode Crash Fix

See [`plan-cookie-fallback-fix.md`](plan-cookie-fallback-fix.md) for full detail, acceptance criteria, and verification steps. Spec: [`../SPEC-cookie-fallback-fix.md`](../SPEC-cookie-fallback-fix.md).

## Task 1: `VideoDownload.py` — console-encoding safety
- [ ] Add `library_common.make_console_encoding_safe()` call at `VideoDownload.py:15-16`, right after the existing `library_common.ensure_stdio_not_none()` call
- [ ] Confirm no other change needed in `library_common.py` (function already exists) or `gui.py` (no observed crash there)
- [ ] `tests/test_phase3_fixes.py`: add `test_the_replacement_stream_survives_tonights_crashed_string`
- [ ] `tests/test_phase3_fixes.py`: add `test_videodownload_makes_console_encoding_safe_before_anything_prints` (source-presence check)
- [ ] `pytest tests/test_phase3_fixes.py -v` green

## ▶ Checkpoint 1
- [ ] `pytest tests/ -v` full suite green
- [ ] Manual read-check: new call precedes `import updater`/`updater.prefer_cached_ytdlp()` and `import yt_dlp`

## Task 2: `VideoDownload.py` — cookie-decrypt detection + retry helper + `_base_opts()` gate
- [ ] Add `_COOKIES_BROKEN = False` global next to `USE_BROWSER_COOKIES`/`COOKIE_BROWSER` (`:642-643`)
- [ ] Add `_COOKIE_ERROR_SIGNS` tuple and `_is_cookie_decrypt_error(exc)` helper (mirrors `is_bot_error`)
- [ ] Add `_run_ytdlp_with_cookie_fallback(opts, fn)` helper (catch, warn-once, set flag, retry cookie-free)
- [ ] Gate `_base_opts()` with `and not _COOKIES_BROKEN`
- [ ] Confirm `configure_cookies()` body is unchanged — does not touch `_COOKIES_BROKEN`
- [ ] `tests/test_cookie_support.py`: extend `setup_function`/`teardown_function` to also reset `vd._COOKIES_BROKEN = False`
- [ ] `tests/test_cookie_support.py`: add fake `yt_dlp.YoutubeDL` stand-in class (context manager, configurable behaviors list)
- [ ] `test_is_cookie_decrypt_error_matches_dpapi_and_cookie_load_text`
- [ ] `test_is_cookie_decrypt_error_does_not_match_bot_or_unrelated_errors`
- [ ] `test_base_opts_omits_cookies_for_the_next_call_once_broken`
- [ ] `test_run_ytdlp_with_cookie_fallback_succeeds_normally_with_one_construction`
- [ ] `test_run_ytdlp_with_cookie_fallback_retries_once_cookie_free_on_dpapi_failure`
- [ ] `test_run_ytdlp_with_cookie_fallback_reraises_non_cookie_errors_without_retry`
- [ ] `test_cookie_fallback_logs_the_warning_exactly_once`
- [ ] `pytest tests/test_cookie_support.py -v` green

## ▶ Checkpoint 2
- [ ] `pytest tests/ -v` full suite green
- [ ] Manual read-check: `configure_cookies()` diff is empty; `_COOKIES_BROKEN` only touched in the three new/changed spots

## Task 3: Wire `search_candidates()`
- [ ] Replace bare `with yt_dlp.YoutubeDL(opts) as ydl: ...` with `_run_ytdlp_with_cookie_fallback(opts, lambda ydl: ydl.extract_info(...))`, existing `try/except` wrapping unchanged
- [ ] `test_search_candidates_retries_without_cookies_after_dpapi_failure`
- [ ] `test_search_candidates_bot_error_is_not_treated_as_a_cookie_failure`
- [ ] `pytest tests/test_cookie_support.py -v` green

## Task 4: Wire `fetch_audio()`
- [ ] Replace bare `with yt_dlp.YoutubeDL(opts) as ydl: ...` with `_run_ytdlp_with_cookie_fallback(opts, lambda ydl: ydl.extract_info(url, download=True))`, swallow-on-failure contract unchanged
- [ ] `test_fetch_audio_recovers_after_dpapi_failure`
- [ ] Manual read-check: `select_video` (`:955`) and `process_resync` (`:1218`) call sites unaffected
- [ ] `pytest tests/test_cookie_support.py -v` green

## Task 5: Wire `download_video()`
- [ ] Extract inner `process_ie_result`/`download` fallback logic into a `_do(ydl)` closure
- [ ] Call `_run_ytdlp_with_cookie_fallback(opts, _do)` in place of the current `with yt_dlp.YoutubeDL(opts) as ydl:` block
- [ ] Confirm remux logic below the `try` block is untouched
- [ ] `test_download_video_retries_without_cookies_after_dpapi_failure`
- [ ] `pytest tests/test_cookie_support.py -v` green

## ▶ Checkpoint 3
- [ ] `pytest tests/test_cookie_support.py -v` green (Tasks 2-5 combined)
- [ ] `pytest tests/ -v` full suite green (check `test_select_video.py`, `test_dump_video.py` especially)
- [ ] Manual read-check: all three call sites still preserve their original `except Exception as e: if is_bot_error(e): raise BotDetected(...)` wrapping

## Task 6: Full regression + scope/diff review
- [ ] `pytest tests/ -v` full suite green
- [ ] `git diff --stat` shows only `VideoDownload.py`, `tests/test_cookie_support.py`, `tests/test_phase3_fixes.py`
- [ ] Re-confirm spec's "Never Do" list against the final diff

## ▶ Final Checkpoint
- [ ] `pytest tests/ -v` full suite green
- [ ] Diff review: exactly the three files above touched — `gui.py`, `library_common.py`, `settings.json` untouched
- [ ] Both spec success criteria confirmed
- [ ] Confirm `configure_cookies()` never resets `_COOKIES_BROKEN`

---

### Notes
- Line numbers verified live against current code at plan time (2026-08-04) — re-verify at `/build` time if this drifts.
- Task 1 is fully independent of Tasks 2-5 — any order, or parallel.
- Tasks 3, 4, 5 each depend on Task 2 but are independent of each other.
