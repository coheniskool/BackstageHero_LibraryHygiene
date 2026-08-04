# Spec: Browser-Cookie DPAPI Failure Fallback + Console Unicode Crash Fix

## Objective

A run tonight (2026-08-02 22:13 through 2026-08-03 01:47, `%LOCALAPPDATA%\BackstageHero\log.txt` + its two rotated backups, ~196 songs attempted) downloaded **zero videos**. 193 of 196 `Error on song` log entries are the exact same failure, and no `log.txt` across the three rotated files contains a single successful-download marker.

**User**: same solo hobbyist as the other fork specs, running the GUI on Windows against a real library overnight/unattended, with `use_browser_cookies` turned on (an opt-in setting meant to help with YouTube's "sign in to confirm you're not a bot" errors).

**Success looks like**: a broken browser-cookie store degrades gracefully — one warning logged, then the run continues without cookies — instead of silently failing every single song for the rest of the run. Separately, no song's console status line can crash song processing just because a title contains a character outside the console's codepage.

## Root Cause 1 — Browser-cookie extraction is unconditionally required, with no fallback

`settings.json` has `use_browser_cookies: true`, `cookie_browser: "chrome"`. Whenever that's set, `configure_cookies()` (`VideoDownload.py:656-675`) flips on `USE_BROWSER_COOKIES`/`COOKIE_BROWSER`, and every subsequent `_base_opts()` call (`VideoDownload.py:678-697`) — used by `search_candidates`, `fetch_audio`, and `download_video` — sets `opts['cookiesfrombrowser'] = (COOKIE_BROWSER,)`, unconditionally, for every song.

`search_candidates()` (`VideoDownload.py:700-721`) is the first thing `process_download()` does for every song. On this run, every single call into it fails the same way:

```
yt_dlp\cookies.py:1100, in _decrypt_windows_dpapi
yt_dlp.utils.DownloadError: ERROR: Failed to decrypt with DPAPI.
See https://github.com/yt-dlp/yt-dlp/issues/10927 for more info
```

This is a known, widely-reported yt-dlp issue: modern Chrome ("App-Bound Encryption", rolled out progressively since Chrome 127) wraps the cookie-decryption key in a way plain Windows DPAPI can no longer unwrap. It is not specific to this library, this machine's state, or this run — it will keep happening on every song, every run, until either Chrome/yt-dlp change or this app stops depending on it working.

`is_bot_error()` (`VideoDownload.py:458-468`) checks the exception text against `_BOT_SIGNS` (`'sign in to confirm'`, `"you're not a bot"`, `'http error 429'`, `'too many requests'`). The DPAPI error message matches none of these, so it is never wrapped as `BotDetected`, and `run_song_with_backoff()`'s retry/backoff loop (`VideoDownload.py:1667-1707`) never engages for it — the exception just falls straight through to the loop's outer `except Exception`, which logs `Error on song: <name>` and moves on. There is no retry, no fallback, no recovery: the same broken cookie path gets attempted again on the very next song, and the one after that, for the rest of the run.

Net effect: a feature that exists specifically to reduce download failures (bot-detection avoidance) instead guaranteed 100% failure for the entire run — strictly worse than never having turned it on, since without cookies this app's TV-embedded/Android-client search path (per `README.md`'s "Why it still works") doesn't need them at all for ordinary searches.

## Root Cause 2 — Non-cp1252 characters in a console `print()` crash song processing

One song ("Lechuga O Vurdón" — logged as `Lechuga O Vurd�n`, i.e. containing a U+FFFD replacement character from an earlier mojibake'd source) crashed at:

```
VideoDownload.py:1079, in process_download
    print('\nLooking on YouTube for: ' + query)
UnicodeEncodeError: 'charmap' codec can't encode character '�' in position 40:
character maps to <undefined>
```

`log.error(...)` calls in this module go through `RotatingFileHandler(..., encoding='utf-8')` (`VideoDownload.py:43-45`) and are safe — the `Error on song: Lechuga O Vurd�n` line landed in `log.txt` just fine. The crash is specific to raw `print()` going to a `cp1252` (Windows default codepage) console/stream, which cannot encode arbitrary Unicode. `VideoDownload.py` has ~50 other `print()` call sites (grep confirms), several of which interpolate the same kind of externally-sourced text — song titles from `song.ini`, YouTube video titles, search queries, and `str(exception)` messages that may themselves quote a title (lines 970, 976, 1012, 1046, 1074, 1091, 1108, 1205, 1217, 1247, 1707, among others). Any of these can hit the same crash given an unusual-enough title; tonight's log happened to trip only the one at line 1079.

This is a narrower instance of the exact problem `library_common.ensure_stdio_not_none()` already exists to solve one level up — that function's docstring is literally about `print()` dying because of the console environment, just for the "no console at all" case (`stdout is None`) rather than "console exists but can't encode this character."

## Behavior Change

1. If browser-cookie extraction fails with the DPAPI/`CookieLoadError` failure class, the run logs one clear warning and continues **without** browser cookies for the rest of that process's lifetime — it does not retry the same broken path on every remaining song, and it does not touch `settings.json` (next launch tries cookies fresh, in case Chrome/yt-dlp has been fixed by then).
2. `stdout`/`stderr` become resilient to characters outside the console's codepage for the whole module, not just the one call site that crashed tonight — a title with an unencodable character degrades to a replacement character in the console instead of killing song processing.

## Implementation

### VideoDownload.py — cookie fallback

- Add a way to recognize this specific failure class, alongside the existing `is_bot_error`/`BotDetected` pattern rather than folding into it (it is not a bot/throttle condition and must not be retried with backoff — retrying a DPAPI failure just wastes the backoff wait for a certainty). Match on the exception chain's `yt_dlp.cookies.CookieLoadError` type if reachable, or conservatively on message text (`'failed to decrypt with dpapi'`, `'failed to load cookies'`) as a fallback for cases where yt-dlp wraps it in a bare `DownloadError` without preserving the original exception type.
- Add a module-level flag (e.g. `_COOKIES_BROKEN = False`) alongside `USE_BROWSER_COOKIES`/`COOKIE_BROWSER`. On first detection of this failure class (in `search_candidates`, `fetch_audio`, or `download_video` — wherever it's first hit), set the flag, log a single `log.warning(...)` explaining what happened and that the run is continuing without cookies, and have `_base_opts()` stop adding `cookiesfrombrowser` once the flag is set.
- The failing call itself still needs to not permanently kill *that* song: once the flag is set, the function that caught the failure should retry its own yt-dlp call immediately with cookie-free opts rather than surfacing the failure up as a dead song — the user's `search_candidates`/`fetch_audio`/`download_video` call should succeed cookie-free on the same song that first hit the DPAPI wall, not just on the next one.
- `configure_cookies()` (`VideoDownload.py:656-675`) is called again at process start every launch (`gui.py:1939`) and resets module state from `settings.json` — leave this untouched, so `_COOKIES_BROKEN` naturally resets on the next app launch and cookies are attempted fresh. This is what keeps the fix in-memory-only and out of `gui.py`'s settings-persistence path, per this fix's scope.
- Do not touch `gui.py`'s cookie checkbox UI or `_persist_setting` calls. The checkbox will show "on" while a broken run silently continues without cookies — acceptable per the in-memory-only scope decided for this fix; the `log.warning` is the source of truth for what actually happened, same as this module's existing "logged, not surfaced live" philosophy elsewhere (e.g. throttle backoff events).

### library_common.py — console Unicode safety

- Extend `ensure_stdio_not_none()` (or add a sibling function called alongside it from both entry points, matching the existing "lives here so it's testable" rationale in its docstring) to also make real (non-`None`) `stdout`/`stderr` streams tolerant of unencodable characters: where the stream supports it, `sys.stdout.reconfigure(errors='replace')` / `sys.stderr.reconfigure(errors='replace')` (Python's `TextIOWrapper.reconfigure`, available on the streams this app targets). Guard with `hasattr(stream, 'reconfigure')` and a broad `except Exception: pass`, matching this codebase's established defensive-failure style for console/environment quirks (e.g. `configure_cookies`'s own docstring, `_setup_logging`'s `try/except Exception: pass` around the file handler).
- This is a one-time, one-place fix that covers every `print()` call in `VideoDownload.py` (current and future) rather than patching each of the ~50 call sites individually — satisfies the "audit all similar print() status lines" scope without a mechanical per-line diff, and without changing any of their actual text/formatting.
- No change to the `RotatingFileHandler`/`log.*` path — it's already UTF-8 and already unaffected.

## Testing Strategy

- `tests/test_video_download.py` (existing or new): mock `yt_dlp.YoutubeDL.extract_info` to raise a `DownloadError`/`CookieLoadError`-shaped exception carrying the DPAPI message on first call, succeed on a second cookie-free call; assert `search_candidates()` (or `process_download()`) still returns a usable result for that same song, `_COOKIES_BROKEN` is now set, and a subsequent unrelated call no longer passes `cookiesfrombrowser` in its opts.
- Add a case asserting a normal `BotDetected`-matching error is *not* caught by the new DPAPI-specific path and still goes through the existing backoff/retry flow unchanged.
- `tests/test_library_common.py` (existing, extends `ensure_stdio_not_none` coverage): assert that after calling the new/extended function against a fake stream exposing `reconfigure`, `reconfigure` was called with `errors='replace'`; assert a stream without `reconfigure` (or one that raises) doesn't propagate an exception out.
- Manual/regression: none of this changes `search_candidates`/`fetch_audio`/`download_video`'s return shape or `_base_opts()`'s output when `USE_BROWSER_COOKIES` is `False` (today's default) — existing tests exercising the cookie-free path should be unaffected.

## Boundaries

### Always Do
- Keep the DPAPI-failure detection narrow (this specific failure class only) — a broad catch-and-continue around cookie extraction risks silently swallowing a *different*, actually-actionable cookie problem (e.g. a genuinely malformed `cookie_browser` setting) under the same "continue without cookies" umbrella. Prefer erring toward under-matching (falls through to today's existing per-song failure behavior) over over-matching.
- Log exactly once per process when cookies get disabled this way — not once per song — so a multi-hour run doesn't produce hundreds of duplicate warnings.
- Preserve `search_by_artist_title`/`_base_opts`/`search_candidates`'s existing "never raises for a reason the caller can't act on" contracts; the new fallback is an additional recovery path, not a replacement for existing error handling.

### Ask First
- Persisting the cookie-disable to `settings.json` (turning the GUI checkbox off automatically) — explicitly deferred per this fix's scope; revisit only if in-memory-only proves insufficient in practice (e.g. user finds the checkbox misleading).
- Any change to `gui.py`'s cookie checkbox/browser-picker UI.

### Never Do
- Do not retry the DPAPI failure with `BotDetected`'s backoff wait — it is not a transient/throttle condition, and waiting minutes before failing the same way again wastes the run's time for a near-certain repeat failure.
- Do not change `RotatingFileHandler`'s encoding or `log.*` call sites — they are already correct and unaffected by this bug.

## Open Questions

1. **Should a broken cookie store be surfaced in the GUI itself** (status bar, toast) rather than only `log.txt`? Deferred consistent with this codebase's existing "log it, don't interrupt an unattended run" philosophy (background-mode throttle events work the same way) — worth revisiting if the user finds themselves checking `log.txt` after every run specifically to see if this happened.
2. **Chrome's App-Bound Encryption may eventually get first-class yt-dlp support** (tracked in yt-dlp issue #10927) — once the pinned/auto-updated yt-dlp version picks that up, this fallback becomes dead code for most users but remains correct/harmless to keep, since a cookie store can still be broken for other reasons (locked profile, corrupted store, unsupported browser).

---

**Next phase**: `/plan` to break this into tasks (cookie-fallback detection + module flag + tests, stdio Unicode-safety helper + tests), then `/build`.
