# Plan: Background-Mode Review Fixes

Spec: [`SPEC-background-mode-fixes.md`](../SPEC-background-mode-fixes.md). Amends [`SPEC-background-mode.md`](../SPEC-background-mode.md) — all Resolved Decisions/Boundaries there still apply.

## Context

`/review` on the 14-task background-mode feature (uncommitted, 560/561 tests passing) found 1 Critical bug, 1 Important gap, and 3 Suggestions. All 5 are in scope per the spec. The Critical finding was reproduced empirically during review (not just inferred from reading): feeding `record_throttle_episode` the *already-adapted* schedule instead of the fixed default causes the adaptive backoff to compound-collapse to its floor within ~7 cycles under a perfectly stable real-world signal — silently defeating the feature's core "data-driven, no fixed floor" design promise.

## Dependency Graph

```
Task 1 (Critical fix + regression test) ─┐
                                          ├─> Task 4 (_dl_thread refactor) ─┐
Task 2 (cookie validation) ──────────────┘                                 ├─> ▶ Checkpoint 3 (final)
                                                                            │
Task 3 (resume path-match + logging) ────────────────────────────────────>┤
                                                                            │
                                          Task 5 (background badge) ──────>┘
```

- Tasks 1 and 2 touch different files (`gui.py`+`tests/test_throttle_history.py` vs. `VideoDownload.py`+`tests/test_cookie_support.py`) — safe to run in parallel.
- Task 3 touches `gui.py`'s `_maybe_resume_background` — a different region than Task 1's `_dl_thread` change, but same file. Sequenced after Task 1 rather than parallelized, matching this session's established discipline of not running two agents against `gui.py` concurrently even in non-overlapping regions (avoids patch-apply races).
- **Task 4 must come after Task 1**, not just for file-conflict reasons: Task 1's fix (dropping the `schedule=` kwarg) lives *inside* the exact block Task 4 extracts into a helper. Doing the surgical fix first means Task 4 extracts already-correct code, rather than the extraction needing to also carry the fix.
- Task 5 (badge) touches `_launch_background`/`_resume_background_*`/`_handle_msg` — none of which Task 4 restructures (Task 4 is scoped to inside `_dl_thread`'s loop body only) — but still sequenced after Task 4 since both are `gui.py` edits.

## Tasks

- [ ] **Task 1** *(Model: Sonnet 5)* — **[Critical]** `gui.py`: in `_dl_thread`'s episode-resolution branch (~line 2552-2554), drop the `schedule=get_active_schedule()` kwarg from the `record_throttle_episode(...)` call so it uses the function's own `LONG_BACKOFF_SECONDS` default every time, per `maybe_recompute_schedule`'s own documented contract. Do **not** touch the `next_resume_at(..., schedule=get_active_schedule())` call a few lines above (~line 2491-2492) — that one is correct and must keep using the live/adapted schedule. Add a new regression test to `tests/test_throttle_history.py` calling the *real* `record_throttle_episode`/`get_active_schedule` (no mocks) across at least 3 recompute cycles with a stable signal (e.g. `escalation_steps_used=0` every episode), asserting the schedule converges after the first recompute rather than continuing to shrink. Starting point for the repro (verified during review):
  ```python
  for i in range(20):
      vd.record_throttle_episode(float(i), float(i)+10, 0, schedule=vd.get_active_schedule())
  # BEFORE fix: shrinks every call, hits [300,300,300,300] by ~episode 10
  # AFTER fix (schedule kwarg dropped): stabilizes after the first recompute
  ```
  **Acceptance**: the new test fails against current `gui.py`/`VideoDownload.py` behavior if you temporarily revert the fix (prove it actually catches the bug), passes after. Full suite green.

- [ ] **Task 2** *(Model: Sonnet 5)* — **[Suggestion #5]** `VideoDownload.py`: `configure_cookies(use_cookies, browser)` (~line 646) validates `browser` against yt-dlp's known `--cookies-from-browser` browser list before storing it in `COOKIE_BROWSER`. An unsupported value is rejected the same way this module treats other defensive failures — logged, not raised into the caller (the GUI's dropdown is fixed to 3 known-good values, so this is pure defense-in-depth for future callers, not a live bug). New test in `tests/test_cookie_support.py` covering a valid browser (accepted, unchanged behavior) and an invalid one (rejected, `COOKIE_BROWSER`/`USE_BROWSER_COOKIES` left in their prior state, no exception escapes).
  **Acceptance**: existing cookie tests pass unmodified; new tests cover both branches.

- [ ] **▶ CHECKPOINT 1** *(after Tasks 1, 2)* — Full `pytest tests/ -q` green. Independently re-run the Task 1 reproduction script against the fixed code to confirm the schedule converges (not just that the new test passes — verify the actual numbers, matching the review's own verification method).

- [ ] **Task 3** *(Model: Sonnet 5, sequenced after Task 1 on `gui.py`)* — **[Important]** `gui.py`: `_maybe_resume_background` (~line 2610-2622) normalizes both sides of the `songs_folder` comparison (`os.path.normcase(os.path.normpath(...))`) before comparing, so a case or trailing-slash difference between the persisted path and the freshly-scanned one doesn't silently skip a resume. Add `log.info` to every no-op branch in this dispatcher: the `songs_folder` mismatch, `phase == 'done'`, and the missing-state case — each stating *why* resume was skipped, so a "why didn't it resume?" question is answerable from `log.txt` alone. New tests: mismatched-case/trailing-slash paths still match; each no-op branch produces a log record (via `caplog`, matching Task 14's existing pattern in this feature).
  **Acceptance**: existing resume tests (`tests/test_background_mode_resume.py`) pass unmodified; new tests cover normalization and logging.

- [ ] **▶ CHECKPOINT 2** *(after Task 3)* — Full `pytest tests/ -q` green.

- [ ] **Task 4** *(Model: Opus 4.8 — structural change inside the highest-risk function in a Station-2-flagged file; a subtly wrong extraction could break background-mode behavior while every existing test still passes)* — **[Suggestion #4]** `gui.py`: extract the background-mode-only throttle-wait logic and episode-resolution bookkeeping out of `_dl_thread`'s loop body into one or two clearly-named private helper methods (e.g. `_handle_background_throttle(...)`, `_resolve_background_episode(...)` — exact split at the implementer's discretion, but each helper should have one clear responsibility). Pure refactor: **zero behavior change** on both the background and non-background paths. The non-background path's control flow (the `if not background_mode:` early-returns) must remain easy to read as unaffected by the extraction. Every existing test in `tests/test_background_mode_controller.py`, `tests/test_background_mode_backoff.py`, `tests/test_background_mode_resume.py`, and the two `test_default_path_*` regressions must pass **unmodified** — do not touch any existing test to make this land; if a test can't pass unmodified after the extraction, the extraction has changed behavior and needs to be reworked, not the test.
  **Acceptance**: `_dl_thread`'s own body is measurably shorter/flatter; all pre-existing tests pass with zero edits; no new test failures.

- [ ] **Task 5** *(Model: Sonnet 5, sequenced after Task 4 on `gui.py`)* — **[Suggestion #3]** `gui.py`: wire `self._background_mode` to a small visible status indicator (e.g. a "● Background" label shown near the existing status line) — set visible whenever `self._background_mode` becomes `True` (fresh start via `_launch_background`, either resume path), cleared when it becomes `False` (`_handle_msg`'s `background_done`/`background_stopped`, `_on_resume_wait_stopped`). Must not disturb the existing status-label layout or any test asserting its current text/position for the non-background path. New GUI test confirming the badge appears on a background run and clears on completion/stop, following the existing style in this feature's test suite (real `ctk` construction where feasible, module-skipped without a display).
  **Acceptance**: badge visible only while `self._background_mode` is `True`; existing non-background-path GUI tests pass unmodified.

- [ ] **▶ CHECKPOINT 3 (final)** *(after Tasks 4, 5)* — Full `pytest tests/ -q` green, count documented against the 560-passed/1-skipped starting point. Whole-diff review of this fix pass (should be small and isolated relative to the 14-task feature it's amending). Confirm the Critical finding's fix is independently re-verified (diff + reproduction script + full suite), not just agent-reported, per this session's established verification discipline.

## Notes

- This plan only touches `gui.py`, `VideoDownload.py`, and test files already established in the parent feature — no new files, no new dependencies, no scope beyond the 5 review findings.
- Every task's "Acceptance" line above is the bar for marking it `[x]` in `tasks/todo-background-mode-fixes.md` — verified independently (git diff + real test run), not accepted on an agent's summary alone.
- Task 4 is the only task carrying real risk (structural change to the highest-complexity function in the feature) — hence Opus and the explicit "don't touch existing tests to make it pass" guardrail.
