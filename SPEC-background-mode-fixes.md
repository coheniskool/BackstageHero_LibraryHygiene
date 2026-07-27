# Spec: Background-Mode Review Fixes

## Description

`/review` on the uncommitted background-mode feature (`SPEC-background-mode.md`, all 14 tasks landed, 560/561 tests passing) surfaced 5 findings: 1 Critical, 1 Important, 3 Suggestions. This spec covers fixing all 5 before the feature ships. No new user-facing capability — this is a correctness/quality pass on work already built this session, not a new feature.

## Objective

**User**: same as the parent spec — a solo hobbyist running background mode unattended for days. These fixes exist so that promise actually holds in practice.

**Success looks like**: the adaptive backoff schedule converges to a stable value under a steady real-world signal instead of collapsing to the floor; a resume-on-launch failure (path-format mismatch, done, no state) is diagnosable from `log.txt` instead of silently invisible; a background run is visually distinguishable from a normal run; `_dl_thread` reads cleanly despite carrying two modes; `configure_cookies` can't be called with a browser yt-dlp doesn't support.

**In scope**:
- **[Critical] Adaptive schedule compounding bug.** `gui.py`'s `_dl_thread` calls `record_throttle_episode(..., schedule=get_active_schedule())`, feeding the *previously recomputed* schedule back in as the base to scale — contradicting `maybe_recompute_schedule`'s own documented contract ("scale the DEFAULT schedule... no compounding drift"). Verified by direct reproduction: a stable signal (median escalation depth unchanged across episodes) collapses the schedule from `[3600, 14400, 43200, 86400]` to `[300, 300, 300, 300]` within 7 recompute cycles, purely as a compounding artifact, not because real behavior changed. Fix: drop the `schedule=` kwarg from that one call site so it uses `record_throttle_episode`'s own `LONG_BACKOFF_SECONDS` default every time. (`next_resume_at(..., schedule=get_active_schedule())` is correct as-is and is NOT touched — waiting must use the live/adapted schedule; only the recompute-feeding call was wrong.)
- **[Important] Silent resume-on-launch failures.** `_maybe_resume_background`'s `state.get('songs_folder') != self._songs_folder` is an exact string compare — case/trailing-slash differences between sessions silently skip the entire resume with zero log trace. Fix: normalize both sides (`os.path.normcase(os.path.normpath(...))`) before comparing, and add a `log.info` on every no-op branch (`songs_folder` mismatch, `phase == 'done'`, missing state) stating why resume was skipped.
- **[Suggestion] `self._background_mode` dead state.** Set at 6 call sites, never read. Fix: wire it to a small "● Background" status-line badge so the user can tell a background run apart from a foreground one at a glance.
- **[Suggestion] `_dl_thread` complexity.** Background-mode branches roughly doubled the function's size/nesting. Fix: extract the background-only throttle-wait and episode-resolution bookkeeping into a small private helper (or two), called from the main loop. Pure refactor — no behavior change, and the non-background path's regression tests must still pass unmodified.
- **[Suggestion] `configure_cookies` input validation.** `browser` is passed straight through to yt-dlp with no validation (currently unreachable with a bad value since only the GUI's fixed 3-item dropdown calls it). Fix: validate against yt-dlp's known browser list at the `configure_cookies` boundary; reject/ignore silently-invalid values the same way the rest of this module treats defensive failures (log + no-op, never raise into a caller that isn't expecting it).

**Out of scope**:
- Any change to `LONG_BACKOFF_SECONDS`'s starting values, `_RECOMPUTE_THRESHOLD`, `_MIN_BACKOFF_SECONDS`, or the ratio/median formula inside `maybe_recompute_schedule` — that algorithm's math is correct in isolation; only its caller was wrong.
- Any change to the non-background download path, `BOT_BACKOFF_SECONDS`, or `run_song_with_backoff`.
- Real-world validation of the backoff schedule or the `YOUTUBE_CLIENTS` removal (both already flagged in the parent spec as needing the user's own runs — unaffected by this fix pass).
- Any new feature beyond what's needed to fix the 5 findings (no new settings, no new dry-run semantics, no scope growth).

## Commands

```
Run:    python gui.py
Test:   pytest tests/ -v
```

Unchanged from the parent spec — no new entry points.

## Project Structure

```
VideoDownload.py   -> configure_cookies(): add browser-name validation against
                      yt-dlp's supported list before storing COOKIE_BROWSER.
                      No other changes -- maybe_recompute_schedule/
                      record_throttle_episode/get_active_schedule keep their
                      existing signatures and behavior; only a caller elsewhere
                      changes.

gui.py              -> _dl_thread(): drop `schedule=get_active_schedule()` from
                      the record_throttle_episode(...) call (the one bug fix
                      that matters most here); extract the background-mode
                      throttle-wait and episode-resolution blocks into one or
                      two helper methods for readability (exact shape is a
                      /plan decision, e.g. _handle_background_throttle(...) /
                      _resolve_background_episode(...)).
                      _maybe_resume_background(): normalize songs_folder before
                      comparing; add log.info on every no-op branch.
                      New: a small status-line element (or reuse/extend the
                      existing status label) driven by self._background_mode,
                      shown while a background run (including a resumed one)
                      is active, cleared when it ends -- exact widget shape is
                      a /plan decision, must not disturb existing status-label
                      layout/tests for the non-background path.

tests/              -> New regression test exercising the REAL
                      record_throttle_episode/get_active_schedule cycle
                      (no mocks) across >=3 recompute cycles with a stable
                      signal, asserting convergence rather than drift -- this
                      is the test that should have caught the Critical finding
                      and didn't, per the review's root-cause note.
                      New tests for: normalized songs_folder matching
                      (case/trailing-slash variants), no-op branches logging,
                      the background-mode badge visibility, and
                      configure_cookies rejecting an unsupported browser
                      string.
```

## Code Style

Unchanged from `SPEC-background-mode.md`/`SPEC.md`: 4-space indentation, comments explain *why* not *what*, atomic writes for anything persisted. The `_dl_thread` extraction should follow the same "new, mostly-additive methods" principle the parent spec already established for this fragile file — factor logic *out* into clearly-named helpers, don't restructure the surrounding control flow.

## Testing Strategy

- **Unit-tested (new)**: the compounding-drift regression (real functions, multi-cycle, asserts convergence) — this is the load-bearing test of this whole fix pass.
- **Unit-tested (new)**: `_maybe_resume_background` path normalization (persisted `'C:/Songs'` vs. live `'c:/songs/'`, etc., still matches) and that each no-op branch logs.
- **Regression-tested**: full existing suite (560 passing, 1 skipped) must stay green; the `_dl_thread` extraction in particular must not change any existing non-background-path test's outcome.
- **GUI-tested**: the background-mode badge, in the style already established for this dialog/window (real `ctk` construction where feasible, skip module without a display).
- Not manually verified this pass — none of these 5 fixes touch the two items the parent spec already flagged as needing the user's own real-world runs (backoff duration, DASH/1080p+ without `YOUTUBE_CLIENTS`).

## Boundaries

- **Always**:
  - Verify the Critical fix empirically (reproduce the compounding bug against `main`, then show it's gone after the fix) before considering it done — not just "tests pass."
  - Keep every existing non-background-path test passing unmodified.
  - Independently verify (git diff + full suite re-run) every fix in this pass before marking it complete, per this session's established practice for delegated work.
- **Ask first**:
  - Any change to `next_resume_at`'s call site or its `schedule=get_active_schedule()` usage — that one is correct and must stay as-is; don't "fix" it by symmetry with the `record_throttle_episode` change.
  - Any change to the badge's visual placement if it turns out to conflict with existing footer/status-bar layout in a way not anticipated here.
- **Never**:
  - Never touch `maybe_recompute_schedule`'s internal math, `LONG_BACKOFF_SECONDS`'s values, `_RECOMPUTE_THRESHOLD`, or `_MIN_BACKOFF_SECONDS` as part of this pass — the bug is entirely in the caller, not the algorithm.
  - Never let the `_dl_thread` refactor change behavior on the background path either — same iteration order, same state persisted at the same points, same queue messages, verified via the existing background-mode test suite passing unmodified plus the new tests.

## Success Criteria

- [ ] Reproduction script (5+ recompute cycles, stable signal) shows the schedule converges after one recompute instead of monotonically shrinking toward `_MIN_BACKOFF_SECONDS`
- [ ] `_maybe_resume_background` matches `songs_folder` case/trailing-slash-insensitively, and every no-op path has a corresponding log line
- [ ] A background run (fresh-started or resumed) shows a visible indicator distinguishing it from a foreground run; it clears when the run ends or stops
- [ ] `_dl_thread` is measurably shorter/flatter at the top level, with background-only logic in named helpers; every existing test (background and non-background) still passes unmodified
- [ ] `configure_cookies` rejects an unsupported browser string without raising into the GUI or silently misconfiguring yt-dlp
- [ ] Full suite green, count documented (starting point: 560 passed, 1 skipped)

## Resolved Decisions

- **Dead `_background_mode` state** (resolved): wire it to a visible status badge rather than removing it or leaving it as-is.
- **`_dl_thread` refactor timing** (resolved): do it now, in this same pass, rather than deferring to a separate cleanup — accepted touching the fragile file again to close out the finding while full context is fresh.
- **`configure_cookies` validation** (resolved): add it now even though it's currently unreachable with bad input — cheap, low-risk, closes the gap for any future caller.

## Notes

- The Critical finding's root cause was a cross-module integration gap: `VideoDownload.py`'s own tests only ever call `record_throttle_episode` with its default `schedule` param, and `gui.py`'s tests mock `record_throttle_episode`/`get_active_schedule` out entirely — so no existing test exercised the real call chain where the bug lives. The new regression test in this pass exists specifically to close that gap, not just to check the fix.
- This spec amends/extends `SPEC-background-mode.md`; it does not supersede it. All Resolved Decisions and Boundaries in the parent spec still apply except where this document narrows or adds to them.
