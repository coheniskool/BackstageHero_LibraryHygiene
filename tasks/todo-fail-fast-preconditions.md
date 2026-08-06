# TODO: Fail-Fast Preconditions + Consecutive-Failure Circuit Breaker

See [`plan-fail-fast-preconditions.md`](plan-fail-fast-preconditions.md) for full
detail, acceptance criteria and verification steps.
Spec: [`../SPEC-fail-fast-preconditions.md`](../SPEC-fail-fast-preconditions.md).

## Task 1: Import guard — `VideoDownload.py:60`
- [x] Add `_assert_ytdlp_usable(mod)` predicate defined **above** `import yt_dlp`
- [x] Call it immediately after `import yt_dlp`, not wrapped in `try/except`
- [x] Comment cites the 2026-08-05 incident and why it raises rather than logs
- [x] `tests/test_fail_fast.py` (new): `test_assert_ytdlp_usable_raises_when_youtubedl_missing`
- [x] `tests/test_fail_fast.py`: `test_assert_ytdlp_usable_message_names_the_resolved_path`
- [x] `tests/test_fail_fast.py`: `test_assert_ytdlp_usable_passes_for_a_real_module`
- [x] `pytest tests/test_fail_fast.py -v` green

## ▶ Checkpoint 1
- [x] `pytest tests/ -q` full suite green
- [~] Manual: launch via `Launch BackstageHero.bat` -- DEFERRED, a live app instance (PID 15316) owns background_state.json. Verified headlessly instead: `pythonw.exe` import of VideoDownload succeeds, guard returns None against the real yt_dlp. Do the full launch check before merging.
- [x] Read-check: guard is not swallowed by any `try/except`

## Task 2: `CONSECUTIVE_ERROR_LIMIT` constant
- [x] Add next to `BOT_BACKOFF_SECONDS`/`LONG_BACKOFF_SECONDS` (`VideoDownload.py:203-218`)
- [x] Comment explains the count-only (non-similarity) choice and cites the incident
- [x] `tests/test_fail_fast.py`: `test_consecutive_error_limit_is_a_sane_positive_int`
- [x] `pytest tests/test_fail_fast.py -v` green

## Task 3: GUI loop counts and trips — `gui.py:3036-3105`
- [x] Add `consecutive_errors` local alongside `clean_streak`/`pace`
- [x] Update it in the three existing outcome branches (`skipped` / `errored` / done)
- [x] Confirm the throttle `'stop'` path leaves the counter unchanged
- [x] Foreground trip: post new `('error_streak', s, i, total, last_error)` and return
- [x] Add the matching `_queue` consumer so the user actually sees it
- [x] `test_counter_increments_on_error_and_resets_on_success`
- [x] `test_counter_resets_on_skipped_song`
- [x] `test_nineteen_errors_then_a_success_does_not_trip`
- [x] `test_trips_at_exactly_the_limit_not_before`
- [x] `test_foreground_trip_posts_error_streak_not_rate_limited`
- [x] `pytest tests/test_fail_fast.py -v` green

## Task 4: Background trip handler — `gui.py`, sibling of `_handle_background_throttle`
- [x] New handler reusing persist → post → cancellable-wait mechanics
- [x] **Its own** escalation counter — `throttle_count` must not be incremented
- [x] **No** adaptive throttle episode recorded (highest-risk line in this task)
- [x] Reset `consecutive_errors` after the wait; retry same song (do not advance `i`)
- [x] `test_background_trip_persists_state_before_waiting`
- [x] `test_background_trip_does_not_record_a_throttle_episode`
- [x] `test_background_trip_leaves_throttle_count_alone`
- [x] `test_background_trip_wait_is_cancellable_by_stop`
- [x] `test_counter_resets_after_background_backoff`
- [x] Tests use a fake clock / stubbed `_stop_evt` — no real sleeping
- [x] `pytest tests/test_fail_fast.py -v` green

## ▶ Checkpoint 2
- [x] `pytest tests/ -q` full suite green
- [x] Read-check: `_handle_background_throttle()` diff empty or additive only
- [x] Read-check: adaptive-schedule recording path untouched

## Task 5: CLI loop counts and trips — `VideoDownload.py:1685-1707`
- [x] Derive per-song errored from `len(errored)` before vs. after the call
- [x] On trip: print a reason naming the repeated error, set `interrupted = True`, `break`
- [x] No long backoff in the CLI path (background-mode-only by design)
- [x] `test_cli_loop_trips_after_limit_consecutive_errors`
- [x] `test_cli_loop_success_resets_the_count`
- [x] `pytest tests/test_fail_fast.py -v` green

## Task 6: Full regression + scope/diff review
- [x] `pytest tests/ -q` full suite green (733 passed, 1 skipped)
- [x] `git diff --stat` shows only `VideoDownload.py`, `gui.py`, `tests/test_fail_fast.py` + docs
- [x] Both source files are purely additive -- zero removed lines
- [x] Re-read the spec's "Never" list against the final diff: no similarity matching,
      no `record_throttle_episode(` call added, no `throttle_count +=` added, guard not
      inside any `try/except`, zero lines touching `_COOKIE_ERROR_SIGNS` /
      `configure_cookies` / `_COOKIES_BROKEN` / `_run_ytdlp_with_cookie_fallback`

## ▶ Final Checkpoint
- [x] Success criteria 2-6 each confirmed by a named test (see mapping below)
- [~] Criterion 1 partially: the raise, the message and the call site are tested; that an
      uncaught ImportError exits non-zero and the .bat retry then fires is untested --
      it is launcher behaviour, not app behaviour. Confirm during the manual launch.
- [x] Existing throttle-path tests untouched and passing (`test_background_mode_*`,
      `test_throttle_history` all green, none modified)
- [~] Manual smoke: DEFERRED for the same reason as Checkpoint 1 -- a live app instance
      (PID 15316) owns `background_state.json` and the shared log. Verified headlessly:
      `pythonw.exe` imports `gui` cleanly, `CONSECUTIVE_ERROR_LIMIT` resolves to 20, and
      `_handle_background_error_streak` is present on `App`. **Do the real launch before
      merging.**

### Success criteria mapping
1. Broken yt_dlp fails at startup naming the path -> `test_assert_ytdlp_usable_raises_when_youtubedl_missing`,
   `test_assert_ytdlp_usable_message_names_the_resolved_path`, `test_videodownload_calls_the_guard_right_after_importing_yt_dlp`
2. 20 consecutive errors in background back off once -> `test_background_trip_backs_off_instead_of_ending_the_run`,
   `test_counter_resets_after_a_background_backoff`
3. 19 then a success trips nothing -> `test_one_short_of_the_limit_does_not_trip`,
   `test_a_success_between_two_near_miss_runs_resets_the_count`, `test_a_skipped_song_between_two_near_miss_runs_resets_the_count`
4. A trip never reaches the adaptive episode data -> `test_background_trip_does_not_record_a_throttle_episode`,
   `test_background_trip_leaves_throttle_count_alone`
5. Foreground and CLI end with their own message -> `test_foreground_trip_does_not_claim_a_rate_limit`,
   `test_trips_at_exactly_the_limit_and_stops_advancing`, `test_cli_loop_trips_after_limit_consecutive_errors`
6. Suite green, throttle tests unchanged -> 733 passed, 1 skipped

---

### Notes
- Line numbers verified live at plan time (2026-08-05, after `623e881`) — re-verify at `/build`.
- Task 1 is fully independent of Tasks 2-5 — any order, or parallel.
- Tasks 3 and 5 both depend on Task 2 but are independent of each other. Task 4 depends on Task 3.
- Three spec Open Questions are unresolved and do not block: the choice of 20,
  whether a breaker trip deserves a shorter first backoff step than a throttle,
  and whether the CLI path (Task 5) is dead code in the frozen build.

## Review fixes (/review, 2026-08-05)
- [x] **Important** `error_streak_count` never reset -> escalation was per-process, not
      per-incident. A days-long background run reached the 24h step after four unrelated
      incidents and stayed there. Now reset on any healthy verdict, matching how
      `_resolve_background_episode` resets `throttle_count`.
- [x] **Important** No test covered a second trip, which is what hid the above.
      `test_a_second_streak_after_recovery_starts_the_backoff_over` asserts both incidents
      back off at step 0.
- [x] **Suggestion** Import guard moved to `library_common.assert_ytdlp_usable()`, beside
      `ensure_stdio_not_none` / `make_console_encoding_safe` -- the convention
      `VideoDownload.py:10-13` already states for import-time guards.
- [x] **Suggestion** Retried song was counted in `errors` twice, so
      `done + skipped + errors` could exceed the song count. Un-counted before the retry;
      `test_the_retried_song_is_not_counted_as_an_error_twice` pins the arithmetic.
- [x] **Nit** Cancellable-wait test now identifies the backoff by its duration rather than
      by counting the per-song delays before it.
- [x] `pytest tests/ -q` 735 passed, 1 skipped
- [x] Never-list re-checked against source files only: 0 cookie lines, 0
      `record_throttle_episode(` calls, 0 `throttle_count +=`, 0 removed lines
