# TODO: Fail-Fast Preconditions + Consecutive-Failure Circuit Breaker

See [`plan-fail-fast-preconditions.md`](plan-fail-fast-preconditions.md) for full
detail, acceptance criteria and verification steps.
Spec: [`../SPEC-fail-fast-preconditions.md`](../SPEC-fail-fast-preconditions.md).

## Task 1: Import guard — `VideoDownload.py:60`
- [ ] Add `_assert_ytdlp_usable(mod)` predicate defined **above** `import yt_dlp`
- [ ] Call it immediately after `import yt_dlp`, not wrapped in `try/except`
- [ ] Comment cites the 2026-08-05 incident and why it raises rather than logs
- [ ] `tests/test_fail_fast.py` (new): `test_assert_ytdlp_usable_raises_when_youtubedl_missing`
- [ ] `tests/test_fail_fast.py`: `test_assert_ytdlp_usable_message_names_the_resolved_path`
- [ ] `tests/test_fail_fast.py`: `test_assert_ytdlp_usable_passes_for_a_real_module`
- [ ] `pytest tests/test_fail_fast.py -v` green

## ▶ Checkpoint 1
- [ ] `pytest tests/ -q` full suite green
- [ ] Manual: launch via `Launch BackstageHero.bat`, app starts, a song downloads
- [ ] Read-check: guard is not swallowed by any `try/except`

## Task 2: `CONSECUTIVE_ERROR_LIMIT` constant
- [ ] Add next to `BOT_BACKOFF_SECONDS`/`LONG_BACKOFF_SECONDS` (`VideoDownload.py:203-218`)
- [ ] Comment explains the count-only (non-similarity) choice and cites the incident
- [ ] `tests/test_fail_fast.py`: `test_consecutive_error_limit_is_a_sane_positive_int`
- [ ] `pytest tests/test_fail_fast.py -v` green

## Task 3: GUI loop counts and trips — `gui.py:3036-3105`
- [ ] Add `consecutive_errors` local alongside `clean_streak`/`pace`
- [ ] Update it in the three existing outcome branches (`skipped` / `errored` / done)
- [ ] Confirm the throttle `'stop'` path leaves the counter unchanged
- [ ] Foreground trip: post new `('error_streak', s, i, total, last_error)` and return
- [ ] Add the matching `_queue` consumer so the user actually sees it
- [ ] `test_counter_increments_on_error_and_resets_on_success`
- [ ] `test_counter_resets_on_skipped_song`
- [ ] `test_nineteen_errors_then_a_success_does_not_trip`
- [ ] `test_trips_at_exactly_the_limit_not_before`
- [ ] `test_foreground_trip_posts_error_streak_not_rate_limited`
- [ ] `pytest tests/test_fail_fast.py -v` green

## Task 4: Background trip handler — `gui.py`, sibling of `_handle_background_throttle`
- [ ] New handler reusing persist → post → cancellable-wait mechanics
- [ ] **Its own** escalation counter — `throttle_count` must not be incremented
- [ ] **No** adaptive throttle episode recorded (highest-risk line in this task)
- [ ] Reset `consecutive_errors` after the wait; retry same song (do not advance `i`)
- [ ] `test_background_trip_persists_state_before_waiting`
- [ ] `test_background_trip_does_not_record_a_throttle_episode`
- [ ] `test_background_trip_leaves_throttle_count_alone`
- [ ] `test_background_trip_wait_is_cancellable_by_stop`
- [ ] `test_counter_resets_after_background_backoff`
- [ ] Tests use a fake clock / stubbed `_stop_evt` — no real sleeping
- [ ] `pytest tests/test_fail_fast.py -v` green

## ▶ Checkpoint 2
- [ ] `pytest tests/ -q` full suite green
- [ ] Read-check: `_handle_background_throttle()` diff empty or additive only
- [ ] Read-check: adaptive-schedule recording path untouched

## Task 5: CLI loop counts and trips — `VideoDownload.py:1685-1707`
- [ ] Derive per-song errored from `len(errored)` before vs. after the call
- [ ] On trip: print a reason naming the repeated error, set `interrupted = True`, `break`
- [ ] No long backoff in the CLI path (background-mode-only by design)
- [ ] `test_cli_loop_trips_after_limit_consecutive_errors`
- [ ] `test_cli_loop_success_resets_the_count`
- [ ] `pytest tests/test_fail_fast.py -v` green

## Task 6: Full regression + scope/diff review
- [ ] `pytest tests/ -q` full suite green
- [ ] `git diff --stat` shows only `VideoDownload.py`, `gui.py`, `tests/test_fail_fast.py` + docs
- [ ] Re-read the spec's "Never" list against the final diff

## ▶ Final Checkpoint
- [ ] All six spec Success Criteria confirmed by a named test or named manual check
- [ ] Existing throttle-path tests untouched and passing
- [ ] Manual smoke: launch via the .bat, normal operation confirmed

---

### Notes
- Line numbers verified live at plan time (2026-08-05, after `623e881`) — re-verify at `/build`.
- Task 1 is fully independent of Tasks 2-5 — any order, or parallel.
- Tasks 3 and 5 both depend on Task 2 but are independent of each other. Task 4 depends on Task 3.
- Three spec Open Questions are unresolved and do not block: the choice of 20,
  whether a breaker trip deserves a shorter first backoff step than a throttle,
  and whether the CLI path (Task 5) is dead code in the frozen build.
