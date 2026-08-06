# Plan: Fail-Fast Preconditions + Consecutive-Failure Circuit Breaker

Spec: [`../SPEC-fail-fast-preconditions.md`](../SPEC-fail-fast-preconditions.md).
Line numbers verified live against the working tree at plan time (2026-08-05,
after commit `623e881`) — re-verify at `/build` time if this drifts.

## Overview

Two independent hardening changes from the 2026-08-05 incident log. Part A (Task
1) is three lines and touches nothing else. Part B (Tasks 2-5) threads a counter
through two existing download loops without changing any function signature.

The whole shape of Part B is: **the counter lives in the loops, the reaction
lives where `'stop'` is already handled.** No new return value from
`run_song_with_backoff()`, no new module, no new settings.

## Dependency Graph

```
Task 1 (import guard) ──────────────────────── independent, do first
                                                (smallest, proves the test
                                                 module scaffolding)

Task 2 (constant + counting helper)
        │
        ├──→ Task 3 (GUI loop: count + trip)
        │            │
        │            └──→ Task 4 (background trip handler)
        │
        └──→ Task 5 (CLI loop: count + trip)

Tasks 3+5 are independent of each other. Task 4 depends on Task 3.
```

## Task 1: Import guard (`VideoDownload.py:60`)

**Change.** After `import yt_dlp`, verify the attribute every call site needs.
Extract the check into a testable predicate rather than writing a bare inline
`if` — import-time code is unreachable under pytest, so an inline-only guard
could be deleted without failing a single test.

```python
import yt_dlp

# 2026-08-05: a process imported yt_dlp successfully and got a module with no
# attributes on it -- the signature of a namespace-package import (the
# directory resolved, __init__.py did not). Startup succeeded, the GUI opened,
# background mode reported 7441 songs pending, and then every single one died
# on `AttributeError: module 'yt_dlp' has no attribute 'YoutubeDL'`. It cleared
# on restart and could not be reproduced. This does not try to fix that
# transient -- it makes it announce itself. Launch BackstageHero.bat already
# retries once on a non-zero exit and shows the log on a second failure; that
# machinery never got a chance because the import did not raise.
_assert_ytdlp_usable(yt_dlp)
```

The predicate itself:

```python
def _assert_ytdlp_usable(mod):
    """Raise if `mod` is not a working yt_dlp. Names the resolved path: the
    failure mode is 'imported the wrong thing', not 'did not import', so the
    path is the diagnostic that matters."""
    if not hasattr(mod, 'YoutubeDL'):
        raise ImportError(
            'yt_dlp imported but has no YoutubeDL -- got %r from %s. This is '
            'usually a stale or half-written install; restarting normally '
            'clears it.' % (mod, getattr(mod, '__file__', 'an unknown path')))
```

Definition order matters: the function must be defined **above** the
`import yt_dlp` line, or moved with it. Simplest is to define it immediately
before the import.

- **Acceptance**: absent `YoutubeDL` → `ImportError` naming the path; present →
  returns silently; app starts normally.
- **Verify**: `pytest tests/test_fail_fast.py -v`; then `pythonw.exe gui.py`
  starts and background mode runs a song.
- **Files**: `VideoDownload.py`, `tests/test_fail_fast.py` (new).

## ▶ CHECKPOINT 1

- `pytest tests/ -q` green (expect 714 + new).
- Manual: launch from the .bat, confirm the app still starts and downloads.
- Confirm the guard is **not** wrapped in `try/except` anywhere.

## Task 2: `CONSECUTIVE_ERROR_LIMIT` + counting rule

**Change.** Add the constant next to `BOT_BACKOFF_SECONDS`/`LONG_BACKOFF_SECONDS`
(`VideoDownload.py:203-218`), with the comment from the spec's Code Style
section. No behavior change yet — this task is the constant plus its tests only,
so Tasks 3 and 5 can proceed in parallel against a settled name.

The counting rule both loops implement, stated once here so they cannot drift:

| Outcome | Counter |
|---|---|
| Song errored | `+= 1` |
| Song downloaded OK | `= 0` |
| Song skipped (already had video) | `= 0` |
| `'stop'` (throttle) | unchanged — the throttle path owns that song |
| `'stopped'` (manual Stop) | irrelevant, run ends |

- **Acceptance**: constant exists, is an int ≥ 1, documented.
- **Verify**: `pytest tests/test_fail_fast.py -v`.
- **Files**: `VideoDownload.py`, `tests/test_fail_fast.py`.

## Task 3: GUI loop counts and trips (`gui.py:3036-3105`)

**Change.** The loop already distinguishes every outcome the counting rule needs
— `result == 'skipped'`, `elif errored:`, `else:` (done) at `gui.py:3093-3100`.
Add `consecutive_errors` alongside the existing `clean_streak`/`pace` locals and
update it in those three branches.

On reaching the limit, fork on `background_mode` — the same fork the `'stop'`
handler already makes at `gui.py:3068-3073`:

- **foreground**: post a new `('error_streak', s, i, total, last_error)` queue
  message and `return`. Deliberately not `rate_limited`: that message tells the
  user YouTube is throttling them, which would be a lie here.
- **background**: hand off to Task 4's helper.

Also needs the matching `_queue` consumer wherever the other run-ending messages
(`rate_limited`, `stopped`, `background_stopped`) are handled, so the user sees
something.

- **Acceptance**: 20 consecutive errors trip; 19-then-a-success does not and
  resets to 0; a skip resets; throttle `'stop'` leaves the counter alone.
- **Verify**: `pytest tests/test_fail_fast.py -v`.
- **Files**: `gui.py`, `tests/test_fail_fast.py`.

## Task 4: Background trip handler (`gui.py`, near `_handle_background_throttle`)

**Change.** A sibling to `_handle_background_throttle()` (`gui.py:3129-3173`),
reusing its mechanics — persist-before-waiting, `next_resume_at()` escalation,
cancellable `_stop_evt.wait()`, `('background_throttled', ...)`-shaped queue
message — with two deliberate differences:

1. **Its own escalation counter**, not `throttle_count`.
2. **No adaptive episode recorded.** A cookie failure is not evidence about how
   long YouTube throttles for. Feeding it into that dataset would corrupt the
   adaptive schedule `SPEC-background-mode.md` Task 8 built. This is the single
   most important line of this task and the easiest to get wrong by copy-paste.

After the wait: reset `consecutive_errors` to 0 and retry the same song (do not
advance `i`), mirroring the throttle path.

Whether this is a genuinely separate method or `_handle_background_throttle()`
grows a flag is an implementation call for `/build` — separate method preferred,
since the two differ in exactly the bookkeeping that must not be shared.

- **Acceptance**: trip persists state before waiting; posts its own message;
  wait is cancellable by Stop; counter resets after; same song retried; **no**
  throttle episode recorded and `throttle_count` unchanged.
- **Verify**: `pytest tests/test_fail_fast.py -v` — assert against a fake clock
  and a stubbed `_stop_evt`, never a real sleep.
- **Files**: `gui.py`, `tests/test_fail_fast.py`.

## ▶ CHECKPOINT 2

- `pytest tests/ -q` green.
- Read-check: `_handle_background_throttle()`'s own diff is empty (or additive
  only), and the adaptive-schedule recording path is untouched.
- Read-check: `throttle_count` is not incremented by the new path.

## Task 5: CLI loop counts and trips (`VideoDownload.py:1685-1707`)

**Change.** Unlike the GUI loop, this one shares a single `errored` list across
all songs (`VideoDownload.py:1681`), so "did this song error?" is
`len(errored)` before vs. after the `run_song_with_backoff()` call. Increment or
reset from that, and on trip set `interrupted = True` and `break` — the existing
path at `:1702-1704` — after printing a reason that names the repeated error.

No long backoff here: the escalating wait is background-mode-only by design, and
an interactive CLI run should not silently sit for an hour.

- **Acceptance**: 20 consecutive errors end the run via the existing
  `interrupted` path with a distinct printed reason; a success resets.
- **Verify**: `pytest tests/test_fail_fast.py -v`.
- **Files**: `VideoDownload.py`, `tests/test_fail_fast.py`.

## Task 6: Full regression + scope review

- `pytest tests/ -q` full suite green.
- `git diff --stat` shows only `VideoDownload.py`, `gui.py`,
  `tests/test_fail_fast.py`, plus the spec/plan/todo docs.
- Re-read the spec's **Never** list against the final diff.

## Final Checkpoint (whole spec)

- All six Success Criteria confirmed by a named test or a named manual check.
- Existing throttle-path tests untouched and passing.
- Manual smoke: launch from the .bat, confirm normal operation.

## Out of Scope (explicitly, per spec)

- Fixing the `yt_dlp` namespace-import transient itself.
- Anything in the cookie-fallback code — that spec shipped.
- Making either threshold user-configurable.
- Similarity-aware error matching (considered and rejected).

## Risks

| Risk | Mitigation |
|---|---|
| Copy-pasting `_handle_background_throttle()` drags the adaptive-episode recording with it, silently corrupting the backoff schedule | Task 4 acceptance asserts no episode recorded; Checkpoint 2 read-checks it separately |
| `gui.py` is 169k and fragility-flagged Station 2 (coverage-exposed); the download loop is its hottest path | Tasks 3 and 4 split so the loop edit and the handler land separately, each independently revertable |
| A legitimately bad 20-song stretch trips the breaker and costs an hour | Accepted in spec Open Question 1 — background mode retries indefinitely, so the cost is delay, not lost songs |
| Import guard placed below its first use, or defined after `import yt_dlp` | Task 1 acceptance includes an actual app start, not just tests |
| CLI loop may be dead code (frozen build is GUI-only) | Spec Open Question 3 — cheap either way; drop Task 5 if confirmed dead |
