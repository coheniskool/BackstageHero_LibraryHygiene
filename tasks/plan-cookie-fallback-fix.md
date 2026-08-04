# Plan: Browser-Cookie DPAPI Failure Fallback + Console Unicode Crash Fix

**Spec**: [`SPEC-cookie-fallback-fix.md`](../SPEC-cookie-fallback-fix.md)

## Overview

Two independent-at-the-margins fixes for a real overnight run that downloaded zero videos: (1) `VideoDownload.py` gains a retry-without-cookies fallback when browser-cookie extraction fails with a Windows DPAPI/cookie-load error, so one broken cookie store degrades gracefully instead of permanently killing every remaining song; (2) `VideoDownload.py` gains console-Unicode safety (via an already-existing `library_common` helper it just never called) so a title outside the console's codepage can't crash song processing. Each task lands its own tests in the same task — no separate "write tests" task.

## Dependency Graph

```
Task 1: VideoDownload.py console-encoding call + tests/test_phase3_fixes.py       ── independent
Task 2: VideoDownload.py cookie-fallback core (helper + flag + _base_opts gate)   ── independent
  └─ Task 3: wire search_candidates()  ── depends on Task 2
  └─ Task 4: wire fetch_audio()        ── depends on Task 2
  └─ Task 5: wire download_video()     ── depends on Task 2
Task 6: full regression + scope/diff review                                       ── depends on 1, 3, 4, 5
```

Task 1 has zero relationship to Tasks 2-5 (different function, different concern) and can land in any order relative to them. Tasks 3, 4, 5 are independent of each other but all require Task 2's helper to exist first.

---

## Task 1: `VideoDownload.py` — console-encoding safety

**Description**: `library_common.py` already has `make_console_encoding_safe()` (`library_common.py:146-164`) — it reconfigures `sys.stdout`/`sys.stderr` with `errors='replace'`, defensively, idempotently. It's already called by 5 sibling modules (`chart_rename.py`, `metadata_enrichment.py`, `dedupe_report.py`, `static_art.py`, `video_repair.py`) but **not** by `VideoDownload.py`, which is exactly why tonight's `print('\nLooking on YouTube for: ' + query)` (`VideoDownload.py:1079`) crashed on a song title containing a non-cp1252 character. No new function needed — just wire the existing one in.

**Exact change** (`VideoDownload.py:15-16`, immediately after the existing `library_common.ensure_stdio_not_none()` call, before any other import):
```python
import library_common
library_common.ensure_stdio_not_none()
library_common.make_console_encoding_safe()
```
Do not touch `gui.py` (no crash observed there; it's a Tkinter app whose status output goes through widgets, not console `print()`) and do not add anything new to `library_common.py` (the function already exists and needs no changes).

**Acceptance criteria:**
- `library_common.make_console_encoding_safe()` is called at `VideoDownload.py` import time, before any `print()` call site in the module can execute.
- A song title/query containing a character outside cp1252 (e.g. tonight's `'Lechuga O Vurd�n'`) can be `print()`ed without raising `UnicodeEncodeError`, even when `sys.stdout` is a strict-cp1252 stream.
- No change to `library_common.py`, `gui.py`, or any other module.

**Verification:**
- Extend `tests/test_phase3_fixes.py` (the actual home of the existing `ensure_stdio_not_none`/`make_console_encoding_safe` coverage — not `tests/test_library_common.py`, which has none of this despite the spec's Testing Strategy section assuming otherwise):
  - `test_the_replacement_stream_survives_tonights_crashed_string` — mirrors the existing `test_the_replacement_stream_survives_non_cp1252_text` (lines 62-71): `monkeypatch.setattr(sys, 'stdout', None)`, call `ensure_stdio_not_none()` + `make_console_encoding_safe()`, `print('\nLooking on YouTube for: Lechuga O Vurd�n')` must not raise.
  - `test_videodownload_makes_console_encoding_safe_before_anything_prints` — a source-presence test (mirrors the existing `test_no_ffmpeg_call_decodes_child_output_with_the_locale_codec`-style source-scan test in the same file): read `VideoDownload.py`'s source text, assert it contains `'library_common.make_console_encoding_safe()'`. (`VideoDownload.py` has no simple directory-scan function to plug into the file's existing parametrized `SCANS` list — its crash site is deep inside a live-network function — so a targeted test is the right fit rather than forcing it into that list.)
- `pytest tests/test_phase3_fixes.py -v` — full file green, including the pre-existing `test_ensure_stdio_not_none_*` and parametrized `test_no_scan_dies_on_a_song_name_cp1252_cannot_encode` tests (unmodified, confirms no regression).

**Dependencies**: None.

**Files touched:**
- `VideoDownload.py`
- `tests/test_phase3_fixes.py`

---

## ▶ CHECKPOINT 1

- [ ] `pytest tests/test_phase3_fixes.py -v` — all green.
- [ ] `pytest tests/ -v` — full suite green, no regression.
- [ ] Manual read-check: the new call sits before `import updater` (`VideoDownload.py:29`)/`updater.prefer_cached_ytdlp()` (`:30`) and before `import yt_dlp` (`:59`) — confirmed `prefer_cached_ytdlp()` has no `print()` calls of its own, so this ordering isn't load-bearing today, but keep the guard first regardless of what either function does later.

---

## Task 2: `VideoDownload.py` — cookie-decrypt detection + retry helper + `_base_opts()` gate

**Description**: Add the mechanism that makes a broken browser-cookie store recoverable: a message-text-based detector for the DPAPI/cookie-load failure class (mirroring the existing `is_bot_error()` pattern at `VideoDownload.py:458-468` — verified against yt-dlp's actual installed source that the failure surfaces as a plain `DownloadError` with the original `CookieLoadError` type lost, so text-matching is the only viable approach, not a stopgap), a shared retry helper that runs a yt-dlp call and retries once cookie-free on that specific failure class, and a `_base_opts()` gate so once broken, future calls (next song, next call in the same song) stop re-attempting cookies at all. This task adds the mechanism only — nothing calls it yet (Tasks 3-5).

**Exact change** (`VideoDownload.py`, new global at `:642-643` next to the existing cookie globals, new functions between `_base_opts()` (`:678-697`) and `search_candidates()` (`:700`)):

```python
USE_BROWSER_COOKIES = False
COOKIE_BROWSER = None
# Set once a browser-cookie store proves unusable this process (DPAPI/App-
# Bound Encryption failure, locked profile, corrupted store, etc.) -- see
# _run_ytdlp_with_cookie_fallback(). Deliberately NOT reset by
# configure_cookies(): once broken this process, it stays broken until the
# app restarts, even if the user re-toggles the checkbox mid-session -- that
# keeps this fix in-memory-only and out of settings.json/gui.py entirely.
_COOKIES_BROKEN = False
```

```python
def _base_opts():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': 1,
        'sleep_interval_requests': 1,
    }
    if USE_BROWSER_COOKIES and COOKIE_BROWSER and not _COOKIES_BROKEN:
        opts['cookiesfrombrowser'] = (COOKIE_BROWSER,)
    return opts


_COOKIE_ERROR_SIGNS = ('failed to decrypt with dpapi', 'failed to load cookies')


def _is_cookie_decrypt_error(exc):
    msg = str(exc).lower()
    return any(sign in msg for sign in _COOKIE_ERROR_SIGNS)


def _run_ytdlp_with_cookie_fallback(opts, fn):
    """Run fn(ydl) with opts. If it fails because the browser-cookie store
    couldn't be read (Windows DPAPI / Chrome App-Bound Encryption -- yt-dlp
    issue #10927), disable cookies for the rest of this process and retry
    fn once without them.

    Matches on message text, not exception type: yt-dlp's YoutubeDL.cookiejar
    property catches the internal CookieLoadError and re-raises it as a plain
    DownloadError carrying the original message -- by the time it reaches
    here the type information is already gone, only the text survives."""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return fn(ydl)
    except Exception as e:
        if not (opts.get('cookiesfrombrowser') and _is_cookie_decrypt_error(e)):
            raise
        global _COOKIES_BROKEN
        if not _COOKIES_BROKEN:
            log.warning('Browser cookie extraction failed (%s); continuing '
                        'this run without browser cookies.', e)
        _COOKIES_BROKEN = True
        retry_opts = dict(opts)
        retry_opts.pop('cookiesfrombrowser', None)
        with yt_dlp.YoutubeDL(retry_opts) as ydl:
            return fn(ydl)
```

`configure_cookies()` (`:656-675`) is **not modified** — it must never touch `_COOKIES_BROKEN`.

**Acceptance criteria:**
- `_is_cookie_decrypt_error` returns `True` for both DPAPI and cookie-load message variants (case-insensitively), `False` for bot-detection text and unrelated errors.
- `_base_opts()` omits `cookiesfrombrowser` whenever `_COOKIES_BROKEN` is `True`, regardless of `USE_BROWSER_COOKIES`/`COOKIE_BROWSER`.
- `_run_ytdlp_with_cookie_fallback`: on success, one `YoutubeDL` construction, returns `fn(ydl)`'s value. On a cookie-decrypt failure with `cookiesfrombrowser` present in `opts`: sets `_COOKIES_BROKEN = True`, logs exactly one `WARNING` (even across multiple calls), retries cookie-free, returns/raises based on the retry. On any other exception, or when `cookiesfrombrowser` isn't in `opts`: re-raises immediately, no retry, no flag change, no second `YoutubeDL` construction.
- No existing test in `tests/test_cookie_support.py` changes behavior.

**Verification:**
- Extend `tests/test_cookie_support.py`. **`setup_function`/`teardown_function` must add `vd._COOKIES_BROKEN = False`** alongside the existing `vd.configure_cookies(False, None)` reset — required because `configure_cookies()` deliberately never touches this flag, so without the reset the first fallback test to run leaks `_COOKIES_BROKEN = True` into every later test in the file.
- Add a small fake `yt_dlp.YoutubeDL` stand-in (context manager; constructor records `opts` and consumes one entry from a `behaviors` list — an `Exception` to raise or a value/callable to return from `extract_info`/`download`/`process_ie_result`). No existing test in the repo mocks `yt_dlp.YoutubeDL` directly, so this is new but self-contained; place it near the top of the file for reuse by Tasks 3-5.
- `test_is_cookie_decrypt_error_matches_dpapi_and_cookie_load_text`
- `test_is_cookie_decrypt_error_does_not_match_bot_or_unrelated_errors`
- `test_base_opts_omits_cookies_for_the_next_call_once_broken`
- `test_run_ytdlp_with_cookie_fallback_succeeds_normally_with_one_construction`
- `test_run_ytdlp_with_cookie_fallback_retries_once_cookie_free_on_dpapi_failure`
- `test_run_ytdlp_with_cookie_fallback_reraises_non_cookie_errors_without_retry`
- `test_cookie_fallback_logs_the_warning_exactly_once` — call the helper twice in a row with cookie-decrypt failures each time; assert exactly one `WARNING`-level log record mentioning cookies.
- `pytest tests/test_cookie_support.py -v` — full file green.

**Dependencies**: None (foundation for Tasks 3-5, but self-contained and independently testable).

**Files touched:**
- `VideoDownload.py`
- `tests/test_cookie_support.py`

---

## ▶ CHECKPOINT 2

- [ ] `pytest tests/test_cookie_support.py -v` — all green (pre-existing + new).
- [ ] `pytest tests/ -v` — full suite green, no regression.
- [ ] Manual read-check: `configure_cookies()` body is unchanged (diff shows zero lines touched in that function); `_COOKIES_BROKEN` only appears in the three new/changed spots (global declaration, `_base_opts()` gate, `_run_ytdlp_with_cookie_fallback`).

---

## Task 3: Wire `search_candidates()` — first real call site

**Description**: `search_candidates()` (`VideoDownload.py:700-721`) is the simplest of the three bodies (single `extract_info` call, no inner try/except, no filesystem side effects) — best place to prove the FakeYDL/monkeypatch pattern cheaply before the more complex bodies in Tasks 4-5.

**Exact change**: replace the bare `with yt_dlp.YoutubeDL(opts) as ydl: info = ydl.extract_info(...)` with:
```python
info = _run_ytdlp_with_cookie_fallback(
    opts, lambda ydl: ydl.extract_info(f'ytsearch{n}:{query}', download=False))
```
inside the same existing `try: ... except Exception as e: if is_bot_error(e): raise BotDetected(str(e)); raise` — that wrapping is untouched.

**Acceptance criteria:**
- A DPAPI failure on the first `YoutubeDL` construction still yields the correct `candidates` list from the retry, with `cookiesfrombrowser` present in the first construction's opts and absent from the second.
- A bot-shaped error still raises `BotDetected`; `_COOKIES_BROKEN` stays `False`; no retry attempted.
- Pre-existing `_base_opts()`-only tests in the file (cookies-off path) pass unmodified.

**Verification:**
- `test_search_candidates_retries_without_cookies_after_dpapi_failure`
- `test_search_candidates_bot_error_is_not_treated_as_a_cookie_failure`
- `pytest tests/test_cookie_support.py -v` — full file green.

**Dependencies**: Task 2.

**Files touched:**
- `VideoDownload.py`
- `tests/test_cookie_support.py`

---

## Task 4: Wire `fetch_audio()` — swallow-not-raise contract preserved

**Description**: `fetch_audio()` (`VideoDownload.py:724-754`) has a different contract than `search_candidates`/`download_video`: its `except Exception as e:` branch **swallows** non-bot errors and returns `(None, 0, None)` instead of raising. That contract must not change — the helper's re-raise (when the retry also fails, or the failure wasn't cookie-related) surfaces at the exact point the old bare call used to raise, so the existing branch still fires normally.

**Exact change**: replace the bare `with yt_dlp.YoutubeDL(opts) as ydl: info = ydl.extract_info(url, download=True)` with:
```python
info = _run_ytdlp_with_cookie_fallback(opts, lambda ydl: ydl.extract_info(url, download=True))
```
inside the existing `try/except` — unchanged otherwise.

**Acceptance criteria:**
- A DPAPI failure on the first construction still yields a usable `(path, max_h, info)` from the retry.
- `_COOKIES_BROKEN` is `True` afterward.
- The existing `except Exception as e: if is_bot_error(e): raise BotDetected(...); return None, 0, None` contract is unchanged for every other failure shape.
- `select_video`'s call site (`VideoDownload.py:955`) needs no changes — `fetch_audio`'s return shape is unchanged.

**Verification:**
- `test_fetch_audio_recovers_after_dpapi_failure`
- Manual read-check confirming `select_video` (`:955`) and `process_resync` (`:1218`) call sites are unaffected.
- `pytest tests/test_cookie_support.py -v` — full file green.

**Dependencies**: Task 2.

**Files touched:**
- `VideoDownload.py`
- `tests/test_cookie_support.py`

---

## Task 5: Wire `download_video()` — most complex body, do last

**Description**: `download_video()` (`VideoDownload.py:780-818`) has an inner `process_ie_result`/`download` fallback with its own bare `except Exception:`. Wrap the whole inner block in a closure and route it through the same helper.

**Exact change**:
```python
def download_video(folder, url, quality, info=None):
    cleanup_temp_files(folder)
    dl = os.path.join(folder, 'video.download.mp4')
    opts = _base_opts()
    opts.update({
        'outtmpl': dl,
        'format': quality,
        'nooverwrites': 0,
        'sleep_interval': 1,
        'max_sleep_interval': 3,
    })

    def _do(ydl):
        done = False
        if info:
            try:
                clean = {k: v for k, v in info.items()
                         if not k.startswith('requested')}
                ydl.process_ie_result(clean, download=True)
                done = True
            except Exception:
                log.info('Cached extraction reuse failed; extracting fresh',
                         exc_info=True)
                cleanup_temp_files(folder)
        if not done:
            ydl.download([url])

    try:
        _run_ytdlp_with_cookie_fallback(opts, _do)
    except Exception as e:
        if is_bot_error(e):
            raise BotDetected(str(e))
        raise
    # remux logic below: unchanged
```
Cookie-store access happens at `YoutubeDL` construction time via a lazy `cookiejar` property, before `_do` runs — so on the failing attempt, `_do`'s own inner try/except is never reached. If a future yt-dlp version changes that timing, correctness still holds (worst case: one extra "Cached extraction reuse failed" log line before the outer helper catches the DPAPI failure on the same `ydl`).

**Acceptance criteria:**
- A DPAPI failure on the first construction still results in a successful download via the retry.
- `cookiesfrombrowser` present on the first `YoutubeDL(...)` call, absent on the second.
- `_COOKIES_BROKEN` is `True` afterward.
- Remux logic (lines after the `try` block) is byte-for-byte unchanged.
- Both call sites — `download_with_fallback` (`:1004`, itself called from `process_download:1094`) and the community-resolver-hit path (`process_download:1048`) — need no changes (external signature unchanged).

**Verification:**
- `test_download_video_retries_without_cookies_after_dpapi_failure` (use `info=None` so it exercises the simpler `ydl.download([url])` branch; a follow-up test for the `info=<dict>`/`process_ie_result` branch is nice-to-have, not required for this fix).
- `pytest tests/test_cookie_support.py -v` — full file green.

**Dependencies**: Task 2.

**Files touched:**
- `VideoDownload.py`
- `tests/test_cookie_support.py`

---

## ▶ CHECKPOINT 3

- [ ] `pytest tests/test_cookie_support.py -v` — all green (Tasks 2-5 combined).
- [ ] `pytest tests/ -v` — full suite green, in particular `tests/test_select_video.py` and `tests/test_dump_video.py` (which monkeypatch `search_candidates`/`fetch_audio`/`download_video` as whole functions on the `vd` module — should be unaffected by internal changes to those functions' bodies).
- [ ] Manual read-check: all three call sites preserve their existing outer `except Exception as e: if is_bot_error(e): raise BotDetected(...)` wrapping exactly as before; no new bare `except` was introduced anywhere.

---

## Task 6: Full regression + scope/diff review

**Description**: One full-suite pass and a spec-boundary re-read after all pieces are in, rather than after each task.

**Acceptance criteria:**
- Full `pytest` suite green.
- `git diff --stat` shows only `VideoDownload.py`, `tests/test_cookie_support.py`, `tests/test_phase3_fixes.py` — `gui.py`, `library_common.py`, `settings.json` untouched.
- Spec's "Never Do" list re-confirmed against the final diff: no backoff/retry-with-wait added for the DPAPI path (fail-fast-then-fallback, never sleep), `RotatingFileHandler`/`log.*` setup untouched.

**Verification:**
- `pytest tests/ -v` — full suite green.
- `git diff --stat`
- Manual read-check against `SPEC-cookie-fallback-fix.md`'s Boundaries section.

**Dependencies**: Tasks 1, 3, 4, 5.

---

## Final Checkpoint (whole spec)

- [ ] `pytest tests/ -v` — full suite green.
- [ ] Diff review: exactly `VideoDownload.py`, `tests/test_cookie_support.py`, `tests/test_phase3_fixes.py` touched.
- [ ] Both spec success criteria confirmed by the tests above: (1) a broken browser-cookie store degrades gracefully — one warning, then the run continues without cookies — instead of failing every song for the rest of the run; (2) no song can crash purely because its title contains a character outside the console's codepage.
- [ ] Confirm `configure_cookies()` never resets `_COOKIES_BROKEN` (in-memory-only, no `settings.json`/`gui.py` changes) — this is the property the whole fix's scope boundary depends on.

---

## Out of Scope (explicitly, per spec)

- Persisting the cookie-disable to `settings.json` / auto-unchecking the GUI checkbox — deferred, in-memory-only per the spec's confirmed scope decision.
- Any change to `gui.py`'s cookie checkbox/browser-picker UI.
- Retrying the DPAPI failure with `BotDetected`'s backoff wait — it is not a transient/throttle condition.
- Any change to `RotatingFileHandler`'s encoding or `log.*` call sites — already correct.
- Adding `make_console_encoding_safe()` to `gui.py` — no observed crash there; can be added later if that assumption proves wrong.

## Risks

| Risk | Mitigation |
|------|-----------|
| Line numbers cited here drift before implementation | Re-verify exact line numbers at build time (verified live on 2026-08-04) |
| `_COOKIES_BROKEN` global leaks across test functions within the same pytest process | Task 2's `setup_function`/`teardown_function` explicitly reset it; Checkpoint 2 re-runs the full file, not just new tests |
| A future call site builds `opts` by hand instead of through `_base_opts()`, bypassing the `_COOKIES_BROKEN` gate | `_run_ytdlp_with_cookie_fallback`'s own `opts.get('cookiesfrombrowser')` check is a second, independent guard — belt-and-suspenders, not solely reliant on `_base_opts()` |
| yt-dlp changes its cookie-load error wrapping/message text in a future version, silently breaking detection | `_is_cookie_decrypt_error` matches two independent message signs; if yt-dlp's pinned version changes this, the existing per-song `Error on song` fallback (today's behavior) is the safety net — no worse than before this fix |
