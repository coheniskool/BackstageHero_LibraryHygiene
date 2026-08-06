# Spec: Fail-Fast Preconditions + Consecutive-Failure Circuit Breaker

**Naming note**: `/spec`'s default filename is `SPEC.md`, but that is already this
project's original whole-app spec. This follows the established `SPEC-<feature>.md`
convention instead, alongside `SPEC-cookie-fallback-fix.md`,
`SPEC-background-mode.md` and the rest.

## Objective

**User**: the same solo hobbyist as the other specs here — running the GUI on
Windows against a ~7,400-song library, unattended overnight, in background mode.

Twice now a single broken precondition has silently failed thousands of songs in
a row while the app reported itself as running normally. This spec makes that
class of failure loud and self-limiting. It does **not** try to fix any
particular underlying cause — those get their own specs — it fixes the fact that
the app cannot tell the difference between "this song is a dud" and "nothing has
worked for hours."

### The evidence

Both incidents come from the 2026-08-05 run's `log.txt`:

**Incident 1 — a broken import nobody noticed.** A process started at 22:42:54
imported `yt_dlp` successfully but got a module with no attributes on it (the
signature of a namespace-package import: the directory resolved, `__init__.py`
did not). Startup succeeded, the GUI opened, background mode resumed and reported
`7441 song(s) still pending` — and then every song died on
`AttributeError: module 'yt_dlp' has no attribute 'YoutubeDL'`. It cleared on
restart, and a replay of the exact launch conditions resolved `yt_dlp` correctly,
so the transient itself is not reproducible and is explicitly **not** what this
spec tries to fix. What it fixes is that a completely unusable downloader
produced zero startup signal.

`Launch BackstageHero.bat` already carries the machinery for exactly this — a
one-shot retry, added after a 2026-07-19 import failure that "worked before and
after and could not be reproduced from a shell", and a notepad pop-up on a second
consecutive failure. It never fired, because the import did not raise.

**Incident 2 — a systemic failure the run could not see.** After that restart,
every song failed again, this time on
`DownloadError: ERROR: ERROR: Could not copy Chrome cookie database` — Chrome was
open and holding its cookie DB, so yt-dlp could not read it. The run kept going
song after song. That specific error is now handled
(`SPEC-cookie-fallback-fix.md`, Task 7), but the pattern is the point: the loop
has no notion of "this has failed N times in a row, something is systemically
wrong."

### What success looks like

1. An unusable `yt_dlp` stops the app at startup with a visible error, and the
   launcher's existing retry gets its chance to paper over a transient.
2. A run that fails 20 songs in a row stops digging: it backs off for a long,
   escalating interval instead of burning the rest of the night, and says so.
3. Neither mechanism changes behavior for a run that is merely having a bad
   patch — scattered failures among successes are still just scattered failures.

## Tech Stack

Unchanged. Python 3.14, stdlib only for both parts. `pytest` for tests. No new
dependencies, no new settings keys, no new GUI controls.

## Commands

```
Test (targeted):  C:\Python314\python.exe -m pytest tests/test_fail_fast.py -v
Test (full):      C:\Python314\python.exe -m pytest tests/ -q
Compile check:    C:\Python314\python.exe -m py_compile VideoDownload.py gui.py
Run from source:  C:\Python314\pythonw.exe gui.py
Launcher:         "Launch BackstageHero.bat"
Frozen build:     C:\Python314\python.exe build.py
```

## Project Structure

No new files beyond the test module. Touched:

```
VideoDownload.py   → import guard (Part A); CONSECUTIVE_ERROR_LIMIT constant;
                     CLI download loop's breaker (Part B)
gui.py             → GUI/background download loop's breaker (Part B)
tests/
  test_fail_fast.py  → new: both parts
```

## Code Style

Match the surrounding code: module-level constants near their siblings, comments
that explain *why* and cite the incident or issue that motivated them, no new
abstractions for a single use. The existing `BOT_BACKOFF_SECONDS` /
`LONG_BACKOFF_SECONDS` block is the model.

```python
# Two ways a run goes bad: one song at a time (normal -- a dud video, a
# private upload) or everything at once (the downloader is broken, the
# network is gone, a precondition failed). Only the second is worth
# reacting to, and 20-in-a-row with no successes between them is the
# cheapest signal that separates them. Deliberately NOT similarity-aware:
# a broken precondition can produce differently-worded errors per song,
# and the count alone is enough to know something systemic is wrong.
CONSECUTIVE_ERROR_LIMIT = 20
```

## Design

### Part A — import guard

Immediately after `import yt_dlp` (`VideoDownload.py:60`), assert the one
attribute the entire module depends on, and raise `ImportError` if it is absent.

- Raise, do not log-and-continue. The launcher's retry only engages on a non-zero
  exit code, and turning tonight's 7,441 silent failures into one visible startup
  error is the entire point.
- The message must name the resolved `yt_dlp.__file__`, since the failure mode is
  "imported the wrong thing", not "did not import".
- Guard `hasattr(yt_dlp, 'YoutubeDL')` specifically. It is what all three call
  sites use, and it is precisely what was missing.

### Part B — consecutive-failure circuit breaker

**Counting.** Both download loops keep a local consecutive-error count. Any song
that errors increments it; any song that succeeds, is skipped, or is stopped
resets it to zero. The counter lives in the loops, not in
`run_song_with_backoff()` — that function sees one song and cannot count across
them, and leaving its signature and return contract alone keeps the blast radius
small.

**Tripping.** At `CONSECUTIVE_ERROR_LIMIT` the loop stops advancing and the
behavior forks by context, exactly as the existing `'stop'` return already forks:

| Context | On trip |
|---|---|
| GUI, background mode | Persist state, post a distinct queue message, wait the long escalating backoff, reset the counter, retry the same song |
| GUI, foreground | End the run with its own message — not `rate_limited`, which would misattribute the cause |
| CLI (`VideoDownload.py` `__main__`) | Break the loop with a clear printed reason, same `interrupted` path as today |

**Backoff schedule.** Background mode reuses the *mechanics* of
`_handle_background_throttle()` — persist-before-waiting, cancellable
`_stop_evt.wait()`, `next_resume_at()` escalation — but with a **separate**
episode counter from `throttle_count`, and it must **not** be recorded as a
throttle episode for adaptive-schedule learning. A cookie failure is not evidence
about how long YouTube throttles for; feeding it into that dataset would corrupt
the adaptive backoff the background-mode spec built.

**After the wait.** Reset the counter and retry the same song, mirroring the
throttle path. If the cause was transient (Chrome got closed) the run recovers on
its own; if it is permanent the breaker simply trips again 20 songs later, which
is the intended floor on wasted work rather than a bug.

## Testing Strategy

`pytest`, new `tests/test_fail_fast.py`, unit-level and offline — no network, no
real `yt_dlp`, no real sleeping. Follow `tests/test_cookie_support.py`'s existing
patterns: `monkeypatch` over the module globals, a fake stand-in for the
collaborator, `setup_function`/`teardown_function` restoring any module state the
tests mutate.

Required cases:

- **Part A**: guard raises when `YoutubeDL` is absent; does not raise when
  present; the message names the offending `__file__`. Test the guard as an
  extractable predicate — import-time code cannot be exercised under pytest, so a
  source-presence check alone (the pattern Task 1 of the cookie spec used) is the
  fallback, not the primary.
- **Part B**: counter increments on error and resets on success, on skip, and on
  a mixed sequence that never reaches the limit; trips at exactly the limit, not
  before; background trip persists state and waits without recording a throttle
  episode; foreground and CLI trips end their runs with their own distinct
  signal; the counter resets after a background wait.
- **Regression**: the existing throttle path and its adaptive-schedule bookkeeping
  are unchanged — a `'stop'` return still does exactly what it does today.

The full suite (714 passed, 1 skipped at time of writing) must stay green.

## Boundaries

**Always**
- Run the full suite before committing; keep each part independently revertable.
- Cite the incident in comments — a bare `CONSECUTIVE_ERROR_LIMIT = 20` with no
  explanation is the kind of magic number that gets "cleaned up" later.
- Keep `run_song_with_backoff()`'s signature and return values as they are.

**Ask first**
- Any change to `background_state.json`'s shape or to what clears it.
- Any change to `BOT_BACKOFF_SECONDS`, `LONG_BACKOFF_SECONDS`, or the adaptive
  schedule's recorded-episode format.
- Making either threshold user-configurable (settings key or GUI control).

**Never**
- Do not make the breaker similarity-aware. It was considered and explicitly
  rejected: a broken precondition can word its error differently per song.
- Do not record a breaker trip as a throttle episode.
- Do not let the breaker end a background run outright — background mode retries
  indefinitely by design and never gives up on its own.
- Do not swallow the import guard in a `try/except`; a caught guard is no guard.
- Do not touch the cookie-fallback code, `configure_cookies()`, or
  `_COOKIE_ERROR_SIGNS` — different spec, already shipped.

## Success Criteria

1. A `yt_dlp` module lacking `YoutubeDL` fails the app at startup with a message
   naming the resolved path, exits non-zero, and lets the launcher retry once.
2. Twenty consecutive song errors in background mode trigger one long backoff
   with its own log line and queue message, not a 7,441-song grind.
3. Nineteen consecutive errors followed by one success trip nothing and reset the
   count to zero.
4. A breaker trip does not appear in the adaptive backoff's episode data.
5. Foreground and CLI runs end on a trip with a message that names the real cause
   and is distinguishable from the rate-limit message.
6. `pytest tests/ -q` green, with no change to existing throttle-path tests.

## Open Questions

1. **Is 20 the right floor?** At ~2-3s per failing song it is roughly a minute of
   wasted work before the breaker reacts — cheap. But a genuinely bad *patch* of
   library (20 consecutive private/deleted videos in one alphabetical stretch)
   would trip it and cost a long backoff for no reason. Assumed acceptable:
   background mode retries indefinitely, so the cost is delay, not lost songs.
2. **Should the first backoff step be shorter for a breaker trip than for a
   throttle?** A throttle wants hours because YouTube's block lasts hours. A
   broken precondition might clear in minutes (close Chrome). Sharing
   `LONG_BACKOFF_SECONDS` is simple and was what was asked for; a separate,
   shorter first step is the obvious alternative if overnight recovery turns out
   too slow in practice.
3. **Does the CLI path still matter?** `build.py` produces a GUI-only frozen exe,
   so the `__main__` loop may be dev-only. Included here for consistency; drop it
   if it is dead code.
