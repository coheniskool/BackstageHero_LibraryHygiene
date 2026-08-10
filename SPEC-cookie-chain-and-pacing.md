# Spec: Browser-Cookie Fallback Chain + Pacing That Survives a Restart

**Naming note**: `/spec`'s default filename is `SPEC.md`, but that is already this
project's original whole-app spec. This follows the established
`SPEC-<feature>.md` convention, alongside `SPEC-cookie-fallback-fix.md`,
`SPEC-fail-fast-preconditions.md` and the rest.

## Objective

**User**: the same solo hobbyist as the other specs here — running the GUI on
Windows against a ~7,900-folder library, unattended, in background mode.

`SPEC-cookie-fallback-fix.md` made a broken cookie store stop killing the run.
It succeeded: a run now degrades to cookie-free instead of failing every song.
This spec addresses what that fix left on the table — the run degrades to
cookie-free **even when a perfectly good cookie store is sitting on the same
machine**, and it stays that way for the whole process. It also fixes the fact
that the run's adaptive pacing, the one signal that most directly governs how
hard this app leans on YouTube, is thrown away on every restart.

Neither part tries to defeat YouTube's rate limiting. Both aim at the same
narrower goal: stop losing hours to conditions the app already has the
information to avoid.

### The evidence

From the 2026-08-09 run (`%LOCALAPPDATA%\BackstageHero\log.txt` and its rotated
backups), started 10:11:32, still running at 19:45:

**Cookie extraction failed three minutes in, and the run never recovered.**

```
2026-08-09 10:14:06 WARNING Browser cookie extraction failed (ERROR: ERROR:
Failed to decrypt with DPAPI. See https://github.com/yt-dlp/yt-dlp/issues/10927
for more info); continuing this run without browser cookies.
```

`settings.json` had `cookie_browser: "chrome"`. Chrome's App-Bound Encryption
makes that store unreadable and will keep doing so. Direct extraction tests run
against this machine's actual browser stores during the investigation:

| Browser | Result |
|---|---|
| chrome | `Failed to decrypt with DPAPI` (yt-dlp #10927) — App-Bound Encryption |
| edge | `Failed to decrypt with DPAPI` — same, once Edge was closed |
| firefox | **OK** — 49 cookies, 25 youtube-scoped, 9 signed-in session cookies |

A readable, signed-in Firefox store existed the whole time. `_COOKIES_BROKEN` is
a process-wide boolean, so the first failure on Chrome sent the entire 9.5-hour
run cookie-free with no attempt to look anywhere else.

> **Correction, 2026-08-10 — cookies are not the fix, and this spec's original
> premise was wrong.**
>
> The reasoning above assumed a working cookie store would have rescued the
> run. Tested directly the next day, on this machine, with the Firefox store
> that had just been made available:
>
> ```
> no cookies       total=26  video=19  avc=7  mp4+avc=7
> firefox cookies  FAILED: Requested format is not available
> ```
>
> Four separate video IDs, same result every time: cookie-free returns 6–7
> usable AVC/mp4 formats, and attaching the signed-in session returns nothing.
> The failure happens during *extraction*, before any format selector is
> applied, which is why `quality_format()`'s fallback chain down to bare `best`
> cannot rescue it — the known interaction where an authenticated session
> pushes YouTube onto a client path requiring a PO token.
>
> The live evidence agrees. The 2026-08-09 run downloaded 160 videos
> **cookie-free**. The 2026-08-10 run, with Firefox cookies finally working,
> downloaded **zero** — six songs dead in a row on "Requested format is not
> available" — and recovered within seconds of the checkbox being unticked.
>
> **What survives:** the chain is still correct *graceful degradation*. If a
> store is unreadable, trying the next one beats going blind, and every
> constraint in the Never list still holds.
>
> **What does not:** the claim that this would have saved the 2026-08-09 run.
> It would have broken it sooner. On this machine `use_browser_cookies` should
> stay **off**, which is already the shipped default (`gui.py` reads it with
> `.get('use_browser_cookies', False)`); only the persisted setting had been
> turned on.
>
> The genuine cause of the 2026-08-09 throttling therefore remains
> **unidentified**. It is per-IP and it cleared on elapsed time alone.

**Time actually spent working: about 2 hours of 9.5.** The run was throttled
11:12–16:36 (5.4h, cleared at escalation step 1) and again from 17:33 onward,
with the next retry not due until 22:44. Throughput while actually running was
healthy — ~210 download attempts, ~207 of which produced a video or a
static-art conversion — so throttling, not failure rate, is what cost the day.

**Pacing is the lever, and it resets to neutral on every launch.**
`_dl_thread` (`gui.py:3014`) already implements adaptive pacing: `pace = 1.0`,
doubled to a ceiling of 6.0 whenever a song reports a throttle event, eased by
0.7 (floor 0.5) after every 8 clean songs. It works. But `pace` and
`clean_streak` are **local variables**. Background mode is explicitly designed
to resume across restarts (`_maybe_resume_background`, Task 13 of
`SPEC-background-mode.md`), and every one of those resumes starts back at
`pace = 1.0` — hammering at baseline speed into a YouTube that may have just
finished blocking us for five hours.

This is the one adaptive signal in the project that does not survive a restart.
`resume_at`, `throttle_count` and `remaining_folders` all persist to
`background_state.json`; the adaptive backoff schedule persists to
`throttle_history.json` specifically so, in that module's own words, a restart
keeps a schedule that took real data to earn. Pacing is the exception.

**And the logs cannot answer the obvious follow-up.** `pace` is never logged, so
there is no way to tell what pace the run was at when it got throttled at 17:33
— which is exactly the number needed to decide whether the 6.0 ceiling is high
enough. Separately, `VideoDownload.py:1801` logs `Rate-limited and gave up on %s`
and drops the exception text, so the log cannot distinguish a "sign in to confirm
you're not a bot" challenge from an `HTTP Error 429`. Those imply different
remedies.

### What success looks like

1. A run whose preferred browser's cookie store is unreadable falls through to
   the next browser and continues **with cookies**, instead of going cookie-free
   for the rest of the process.
2. A run only goes cookie-free when every candidate browser has failed, and says
   so once, clearly.
3. A background run that is restarted or resumes at launch picks up the pacing it
   had learned, rather than resetting to neutral.
4. The diagnostics log can answer "what pace were we at when we got throttled,
   and what did YouTube actually say" without a code change.
5. No change to behavior for a run whose cookies work fine on the first browser.

## Tech Stack

Unchanged. Python 3.14, stdlib only. `pytest` for tests. No new dependencies.
No new GUI controls, no new user-facing settings keys. One additive change to
`background_state.json`'s shape (see Boundaries).

## Commands

```
Test (targeted):  C:\Python314\python.exe -m pytest tests/test_cookie_support.py tests/test_background_mode_resume.py -v
Test (full):      C:\Python314\python.exe -m pytest tests/ -q
Compile check:    C:\Python314\python.exe -m py_compile VideoDownload.py gui.py
Run from source:  C:\Python314\pythonw.exe gui.py
Launcher:         "Launch BackstageHero.bat"
Frozen build:     C:\Python314\python.exe build.py
```

## Project Structure

No new files beyond tests. Touched:

```
VideoDownload.py   → cookie chain state + _run_ytdlp_with_cookie_fallback
                     (Part A); bot-sign logging at :1801 (Part C)
gui.py             → persist/restore pace + clean_streak (Part B);
                     pace in the throttle log line (Part C)
tests/
  test_cookie_support.py        → extend: chain traversal, exhaustion
  test_background_mode_resume.py → extend: pace round-trips a resume
  test_background_state.py       → extend: new keys load defensively
```

## Code Style

Match the surrounding code: module-level constants beside their siblings,
comments that explain *why* and cite the incident or issue that motivated them,
no new abstractions for a single use. The existing `_COOKIE_ERROR_SIGNS` block
and `LONG_BACKOFF_SECONDS` are the models — both name the specific yt-dlp issue
or run that caused them.

```python
# Ordered fallback, not a single choice: on 2026-08-09 a run went 9.5 hours
# cookie-free because Chrome's App-Bound Encryption (yt-dlp #10927) made its
# store unreadable, while a perfectly good signed-in Firefox store sat on the
# same machine untouched. The user's dropdown choice still leads -- this only
# decides where to look next when that one turns out to be unreadable.
_COOKIE_CHAIN_ORDER = ('firefox', 'edge', 'chrome')
```

## Design

### Part A — cookie fallback chain

**Chain composition.** The dropdown value (`settings.json` `cookie_browser`) is
the *preferred* browser and is always tried first. The remainder of
`_COOKIE_CHAIN_ORDER` follows, in that fixed order, with the preferred entry
de-duplicated out. Firefox leads the default order because it is the only one of
the three not subject to Chromium App-Bound Encryption. Entries not in
`_SUPPORTED_COOKIE_BROWSERS` are filtered out, preserving `configure_cookies()`'s
existing defensive contract.

**State.** Replace the `_COOKIES_BROKEN` boolean with a position into the chain
plus a set of browsers already known-broken this process. Stickiness is
preserved exactly as today, just at a finer grain: a browser that has failed once
is never retried this process, because re-paying that failure per song is the
cost the original fix existed to eliminate. When the chain is exhausted, the run
goes cookie-free for the remainder of the process — today's terminal state,
reached later and only on real evidence.

**`_base_opts()` keeps its signature.** It reads the current chain head from
module state, exactly as it reads `COOKIE_BROWSER` today. Callers are unchanged.

**`_run_ytdlp_with_cookie_fallback()` becomes a loop** over the remaining chain
rather than a single retry. On a `_is_cookie_decrypt_error` match it marks the
current browser broken, logs one warning naming both the browser that failed and
the one being tried next, advances, and retries the *same* call. The existing
contract holds: the song that first hits the wall is the song that succeeds on
the fallback, not the one after it. Non-cookie exceptions still propagate
untouched — a misspelled browser name must still fail loudly rather than be
silently downgraded.

**Logging.** One warning per browser transition, plus one final warning when the
chain is exhausted. A run that falls through all three produces three lines, not
one per song.

**Out of scope, deliberately.** No writes to `settings.json`, no change to the
dropdown or its three values, no change to `_COOKIE_ERROR_SIGNS`. The checkbox
will still read "on" while a run has fallen through to a different browser than
the one displayed — the same in-memory-only tradeoff `SPEC-cookie-fallback-fix.md`
accepted, with the log as the source of truth.

### Part B — pacing that survives a restart

**Persist `pace` and `clean_streak`** into `background_state.json` alongside the
volatile fields the download loop already rewrites (`resume_at`,
`throttle_count`, `remaining_folders`, `phase` — `gui.py:2992`). They are written
on the same cadence and by the same code path; no new save points.

**Restore on resume.** `_resume_background_downloading` threads the restored
values into `_dl_thread` instead of letting it start at `1.0`. Absent or
unparseable values fall back to today's defaults — `_load_background_state()`'s
existing defensive contract, which never raises on a bad file.

**Clamp on load.** Restored `pace` is clamped into the same `[0.5, 6.0]` band the
live logic enforces. A corrupt or hand-edited file must not be able to produce a
pace of 0 (a busy-loop hammering YouTube) or an absurd one (a run that appears
hung). This mirrors `_MIN_BACKOFF_SECONDS`' rationale in `VideoDownload.py`: a
floor on *machine safety*, not on politeness.

**Foreground runs are unchanged.** They do not persist state and have no resume
path; `pace` stays a local starting at `1.0`.

**Not in scope:** changing the 2.0 growth factor, the 0.7 decay, or the 8-song
streak threshold. Those are tuning decisions with no logged data to tune
against — which Part C fixes first.

**In scope after measurement:** the ceiling. Open Question 1 was resolved with
real cadence data rather than deferred: `_PACE_MAX` moves 6.0 → 24.0, because
a measured 26.1s median song cycle means the old ceiling could only cut the
request rate by 28%. `_PACE_MIN` and `_PACE_DEFAULT` are unchanged.

### Part C — make the logs answer the tuning question

Two one-line diagnostics, both prerequisites for ever tuning Part B honestly:

1. Include the current `pace` in the background-throttle log line
   (`gui.py:3266`), so a future reader can tell whether the run was already
   backed off when YouTube pushed back.
2. Include the triggering exception's text in
   `log.warning('Rate-limited and gave up on %s', song_name)`
   (`VideoDownload.py:1801`), truncated. `BotDetected` is raised with
   `str(e)` already (`VideoDownload.py:493`), so the text is in hand at the
   `except` — it is simply not being passed through. Without it the log cannot
   distinguish a sign-in challenge from an HTTP 429.

## Testing Strategy

`pytest`, offline, no network and no real `yt_dlp` calls. Follow
`tests/test_cookie_support.py`'s existing patterns: `monkeypatch` over module
globals, a fake stand-in for the collaborator, `setup_function`/
`teardown_function` restoring any module state the tests mutate — the chain
state is process-global and *will* leak between tests if it is not reset.

Required cases:

**Part A**
- Preferred browser leads the chain; the remaining defaults follow in order with
  no duplicate.
- A cookie error on browser 1 advances to browser 2 and the same call succeeds —
  asserting the *same* invocation returns, not a later one.
- Errors on all three exhaust the chain, go cookie-free, and log once.
- Each of the three `_COOKIE_ERROR_SIGNS` strings triggers advancement.
- A non-cookie exception propagates without advancing the chain.
- A browser marked broken is not retried on a subsequent call.
- An unsupported name in the configured preference is filtered, not passed to
  yt-dlp.
- `settings.json` is never written (assert via the GUI-settings test module).

**Part B**
- `pace`/`clean_streak` round-trip through a save/load of
  `background_state.json`.
- A resume restores them rather than resetting to `1.0`/`0`.
- Missing keys (a state file written by the previous version) load at today's
  defaults — the forward-compatibility case that matters, since a real user will
  resume an in-flight run across this change.
- Out-of-band values (`0`, `-1`, `999`) clamp into `[0.5, 6.0]`.
- Foreground runs neither read nor write the new keys.

**Part C**
- The throttle log line contains the pace; the give-up line contains the
  exception text.

**Regression**: a run whose first browser works must produce byte-identical
`_base_opts()` output and identical call counts to today. The full suite (735
tests collected at time of writing) must stay green, with no change to existing
throttle-path or resume tests beyond the additions above.

## Boundaries

**Always**
- Keep Parts A, B and C independently revertable — three commits, not one.
- Cite the 2026-08-09 run and the yt-dlp issue numbers in comments. A bare
  `_COOKIE_CHAIN_ORDER` with Firefox first, unexplained, is exactly the kind of
  ordering that gets "tidied" alphabetically later.
- Preserve per-process stickiness. Never re-attempt a browser that has already
  failed this process.
- Keep `_base_opts()`'s signature and `_run_ytdlp_with_cookie_fallback()`'s
  outer contract (same song succeeds on fallback) unchanged.

**Ask first**
- This spec **is** the asking for the one item prior specs gated: adding keys to
  `background_state.json`. The change is additive and backward-compatible — a
  state file without the new keys loads at today's defaults — but the shape is
  changing, which `SPEC-fail-fast-preconditions.md` listed under Ask First.
- Any change to the pacing constants themselves (2.0 / 0.7 / 8 / 6.0 / 0.5).
- Any change to `_COOKIE_ERROR_SIGNS`, or to what counts as a cookie failure.
- Adding a GUI control or `settings.json` key for pacing.

**Never**
- Do not rotate browsers in response to *rate limiting*. It was considered and
  rejected: YouTube's throttle here is keyed to the IP, not the browser, and the
  2026-08-09 episodes cleared on elapsed time alone with no cookie change. Same
  account across three browsers is one identity in three stores; different
  accounts would spread per-account quota, not the per-IP limit, and drifts into
  ToS territory. The chain is an **extraction-time** fallback only.
- Do not write the fallen-back-to browser into `settings.json`. The next launch
  must try the user's actual preference fresh, in case Chrome or yt-dlp has been
  fixed.
- Do not let a restored `pace` skip the clamp.
- Do not touch `LONG_BACKOFF_SECONDS`, `next_resume_at`, the adaptive-schedule
  recompute, or `throttle_history.json`. Different spec, already shipped.
- Do not add cookie *values* to any log line. Today's code passes only browser
  names to yt-dlp and that property must survive this change.

## Success Criteria

1. With `cookie_browser: "chrome"` on this machine, a run logs the Chrome DPAPI
   failure, falls through to Firefox, and proceeds **with** cookies — verifiable
   in `log.txt` on the next real run.
2. A run whose every browser fails logs one line per browser plus one
   chain-exhausted line, then behaves exactly as today's cookie-free run.
3. Stopping and relaunching a throttled background run resumes at the pace it
   had reached, not `1.0`.
4. A `background_state.json` written before this change resumes without error at
   today's defaults.
5. The throttle log line names the pace; the give-up line names what YouTube
   actually said.
6. `pytest tests/ -q` green, 735+ tests, no existing test modified except by
   addition.

## Open Questions — all resolved 2026-08-10

1. **Is the 6.0 pace ceiling high enough?** **No — raised to 24.0.**
   Measured rather than guessed. `pace` only scales the 1–3s inter-song gap
   (~2s mean), and the 2026-08-09 run's median song cycle was **26.1s**
   (n=71 consecutive gaps under 5 min, from `video.mp4` mtimes; mean 37.0s,
   p10/p90 17.7s/75.8s). So the old ceiling could cut the request rate by at
   most **28%** — a weak brake for something that only engages after YouTube
   has already pushed back:

   | pace | avg gap | cycle | rate | vs baseline |
   |---|---|---|---|---|
   | 1.0 | 2s | 26.1s | 2.30/min | — |
   | 6.0 | 12s | 36.1s | 1.66/min | −28% |
   | 12.0 | 24s | 48.1s | 1.25/min | −46% |
   | **24.0** | 48s | 72.1s | 0.83/min | **−64%** |

   24.0 is not reachable by accident: escalation doubles, so 1.0 → 24.0 takes
   five separate throttle events, and the 0.7-per-8-clean-songs decay walks it
   back to neutral over ~71 clean songs. `_PACE_MIN`/`_PACE_DEFAULT` unchanged,
   and the growth factor (2.0), decay (0.7) and streak threshold (8) are
   untouched.

   *Still unmeasured:* what pace the run was actually at when it got throttled.
   Part C now logs it, so the next occurrence answers whether 24.0 is enough.

2. **Should a resumed run start slower than it left off?** **No — verbatim.**
   A penalty would be a guess layered on a guess, and the error self-corrects
   within one song either way: if YouTube is still blocking, the very next song
   throttles and doubles pace anyway. Documented at the restore site.

3. **Does the CLI loop need pacing at all?** **No — flat delay, documented as
   such.** `VideoDownload.main()` runs only via `python VideoDownload.py` from
   source: `build.py`'s frozen exe launches the GUI, and the README documents
   no CLI entry point for it (it documents `dedupe_report.py` as standalone,
   not this). Duplicating the adaptive-pacing policy there would put two
   implementations of one rule in the codebase for a path nobody ships, and
   they would drift — a risk this project already guards against elsewhere
   ("deliberately NOT a shared code path"). A comment at the sleep site now
   says so, and says to extract a shared helper rather than copy if the CLI
   ever becomes supported. This closes the same question
   `SPEC-fail-fast-preconditions.md` left open.

4. **Should the chain be tried at startup rather than on first failure?**
   **No — lazy, and cost is not the reason.** Measured 2026-08-09: probing all
   three stores costs firefox 10ms, edge 655ms, chrome 1589ms. Since the chain
   is sticky, that work happens exactly once either way — eager probing moves
   it earlier, it does not add it. The real argument is that an eager probe
   would **bake in a transient failure**: the "could not copy Chrome cookie
   database" case (yt-dlp #7271) just means the browser is running and holding
   its store open, and launch — right after the user clicked a shortcut with
   their browser open — is the worst possible moment to sample it. Since
   `_BROKEN_COOKIE_BROWSERS` is sticky for the process, a browser condemned at
   startup would stay condemned for a multi-day run.
