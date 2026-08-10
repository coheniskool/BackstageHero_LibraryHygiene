# TODO: Browser-Cookie Fallback Chain + Pacing + Launch Reliability

See [`plan-cookie-chain-and-pacing.md`](plan-cookie-chain-and-pacing.md) for full detail, acceptance criteria, and verification steps. Specs: [`../SPEC-cookie-chain-and-pacing.md`](../SPEC-cookie-chain-and-pacing.md) (Tasks 1, 2, 4) and [`../SPEC-launch-reliability.md`](../SPEC-launch-reliability.md) (Task 3).

Tasks are in dependency order. **Layer 0** (Tasks 1-3) are mutually independent and may be done in any order or in parallel; **Task 4** needs Task 1 only for observability; **Task 5** needs all four.

| # | Task | Layer | Model |
|---|---|---|---|
| 1 | Part C — diagnostics | 0 | Sonnet 5 |
| 2 | Part A — cookie chain | 0 | Opus 5 |
| 3 | Single-instance + retry wait | 0 | Opus 5 |
| 4 | Part B — pacing persistence | 1 | Sonnet 5 |
| 5 | Regression + scope review | 2 | Opus 5 |

## Task 1: Part C — diagnostics that make Part B tunable  · Sonnet 5 · DONE
- [x] `VideoDownload.py:1793` — bind the exception (`except BotDetected as e:`) and pass `str(e)[:200]` through the give-up `log.warning` at `:1801`
- [x] `gui.py:3235` — add a `pace` parameter to `_handle_background_throttle`, pass it from the `_dl_thread` call site
- [x] `gui.py:3266` — include pace in the `Background mode: throttled on ...` log line
- [x] Confirm `_BOT_SIGNS` and `BotDetected`'s own message are untouched
- [x] `tests/test_fail_fast.py`: `test_give_up_warning_carries_the_bot_error_text`
- [x] `tests/test_fail_fast.py`: `test_give_up_warning_truncates_a_pathological_error_message`
- [x] `tests/test_background_mode_controller.py`: `test_throttle_log_line_names_the_current_pace` (moved here from `test_background_mode_backoff.py` — that file is pure-function-only, no `App`/`_dl_thread` fixture; `test_background_mode_controller.py` already has the exact caplog+`_dl_thread` harness needed, see `test_throttle_episode_resolved_logs_escalation_steps`)
- [x] `pytest tests/test_fail_fast.py tests/test_background_mode_controller.py tests/test_background_mode_backoff.py -v` green (40 passed)

## ▶ Checkpoint 1 — DONE
- [x] `pytest tests/ -q` full suite green (738 passed, 1 skipped — 735 + 3 new)
- [x] Manual read-check: two log lines are the only non-test diff (`git diff --stat -- VideoDownload.py gui.py` = 2 files, 15 insertions/5 deletions, all inside the two call sites); only signature change is `_handle_background_throttle`'s new `pace` parameter
- [ ] Commit (Part C, independently revertable) — not committed; user has not asked for a commit yet

## Task 2: Part A — cookie fallback chain  · Opus 5 · DONE
- [x] `VideoDownload.py` — add `_COOKIE_CHAIN_ORDER = ('firefox', 'edge', 'chrome')` with the 2026-08-09 rationale in a comment
- [x] Add `_BROKEN_COOKIE_BROWSERS = set()`. **Deviation:** `_COOKIES_BROKEN` was KEPT rather than replaced, now meaning "every candidate exhausted". Replacing it outright would have forced rewriting 16 assertions across the existing suite for no behavioral gain; keeping it preserves them and gives the set a clean, narrower meaning.
- [x] `_cookie_chain()` — preference first, defaults after, de-duplicated, filtered to `_SUPPORTED_COOKIE_BROWSERS`. **Deviation:** derived on each call rather than stored at `configure_cookies()` time — `COOKIE_BROWSER` stays the single source of truth, and a stored copy would go stale against direct assignment to the module globals (which is how both a fresh-install default and this project's own tests reach `_base_opts()`).
- [x] `_active_cookie_browser()` — first chain entry not already broken, or None
- [x] `_base_opts()` — sets `cookiesfrombrowser` from the active browser; omits the key when exhausted; signature unchanged
- [x] `_run_ytdlp_with_cookie_fallback()` — loops over remaining candidates; one warning per transition; one chain-exhausted warning; final cookie-free attempt; termination argued in the docstring
- [x] Confirm non-cookie exceptions still propagate without advancing the chain
- [x] `tests/test_cookie_support.py`: `setup_function`/`teardown_function` reset `vd._BROKEN_COOKIE_BROWSERS`
- [x] `test_chain_puts_the_preferred_browser_first_then_the_defaults`
- [x] `test_chain_does_not_repeat_the_preferred_browser`
- [x] `test_chain_is_empty_when_cookies_are_off`
- [x] `test_chain_filters_unsupported_browser_names`
- [x] `test_dpapi_failure_advances_to_the_next_browser_and_the_same_call_returns`
- [x] `test_exhausting_every_browser_ends_cookie_free`
- [x] `test_each_cookie_error_sign_advances_the_chain` (parametrized over `_COOKIE_ERROR_SIGNS`)
- [x] `test_non_cookie_error_propagates_without_advancing`
- [x] `test_a_broken_browser_is_not_retried_on_a_later_call`
- [x] `test_one_warning_per_transition_not_one_per_song`
- [x] `test_chain_exhausted_warning_is_logged_once`
- [x] `test_no_cookie_value_reaches_the_logs`
- [x] `test_base_opts_is_byte_identical_when_cookies_are_off` (regression)
- [x] **4 pre-existing tests updated, not just added to** — `search_candidates`/`fetch_audio`/`download_video` DPAPI tests asserted the retry went cookie-FREE. That is precisely the behavior this spec changes, so they now assert `chrome -> firefox`. The 5th such assertion (`test_run_ytdlp_..._retries_once_cookie_free_on_dpapi_failure`) never calls `configure_cookies`, so its empty chain still yields the cookie-free retry and it was left untouched.
- [x] `tests/test_cookie_settings_gui.py`: `test_falling_back_never_writes_cookie_browser_to_settings`
- [x] `pytest tests/test_cookie_support.py tests/test_cookie_settings_gui.py -q` green (46 passed)

## ▶ Checkpoint 2 — DONE
- [x] `pytest tests/ -q` full suite green (755 passed, 1 skipped)
- [x] Manual read-check: no `search_candidates`/`fetch_audio`/`download_video` line was added or removed — verified by filtering the diff to `+`/`-` lines only (they appear as context, nothing more)
- [x] Manual read-check: `_COOKIE_ERROR_SIGNS` and the dropdown's three values unchanged
- [ ] Commit (Part A, independently revertable) — not committed; user has not asked for a commit yet

## Task 3: Single-instance guard + launcher retry wait  · Opus 5 · DONE
Governed by [`../SPEC-launch-reliability.md`](../SPEC-launch-reliability.md), not the cookie/pacing spec.
- [x] `gui.run()` — acquires a named Win32 mutex via `ctypes`, after the `SetCurrentProcessExplicitAppUserModelID` block and **before** `App()`
- [x] Handle stored in module-level `_SINGLE_INSTANCE_MUTEX` (a local would be GC'd and release the mutex)
- [x] On `ERROR_ALREADY_EXISTS (183)`: `FindWindowW` on `f'BackstageHero  v{__version__}'` (two spaces) + `ShowWindow(9)`/`SetForegroundWindow`
- [x] Falls back to `MessageBoxW` when focus fails **or raises** — the already-running decision is made first and cannot be undone by a later failure
- [x] **`sys.exit(0)`** — non-zero would trigger the `.bat`'s retry-then-notepad path (`Launch BackstageHero.bat:49-82`)
- [x] Every ctypes call wrapped so a failure starts the app normally
- [x] No PID lock file anywhere — pinned by a source-scan test
- [x] `Launch BackstageHero.bat` — retry wait 5s → 20s, comment citing 2026-08-09 and the restart-shaped pattern across all four occurrences
- [x] `timeout`/`ping` fallback mechanics unchanged (`ping -n 21` moved with `timeout /t 20` — ping waits n-1, so they must move as a pair)
- [x] `tests/test_single_instance.py` (new, 15 tests) — never acquires a real mutex; `gui._win32()` is the injected seam
- [x] `test_first_instance_proceeds_to_construct_the_app`
- [x] `test_second_instance_exits_zero_and_never_constructs_the_app`
- [x] `test_second_instance_focuses_the_existing_window`
- [x] `test_focus_targets_the_titlebar_text_app_actually_sets`
- [x] `test_second_instance_falls_back_to_a_message_box_when_focus_fails`
- [x] `test_second_instance_messages_when_focus_raises`
- [x] `test_a_raising_win32_call_starts_the_app_anyway`
- [x] `test_win32_seam_itself_missing_starts_the_app_anyway`
- [x] `test_the_mutex_handle_is_retained_in_module_state`
- [x] `test_a_blocked_launch_retains_no_handle`
- [x] `test_no_pid_lock_file_anywhere_in_the_guard`
- [x] `test_blocked_launch_does_not_touch_background_state`
- [x] `test_guard_runs_before_the_app_is_constructed`
- [x] `test_app_user_model_id_still_runs_before_the_window` (regression)
- [x] `test_launcher_retry_wait_was_raised_to_twenty_seconds` (the `.bat` has no other test surface)
- [x] `pytest tests/test_single_instance.py -q` green (15 passed)

## ▶ Checkpoint 3 — DONE
- [x] `pytest tests/ -q` full suite green (770 passed, 1 skipped)
- [x] Manual read-check: guard precedes `App()`; no PID file; `.bat` diff is the two numbers and one comment block
- [ ] Commit (single-instance + retry wait, independently revertable) — not committed; user has not asked for a commit yet

## Task 4: Part B — persist and restore pacing  · Sonnet 5 · DONE
- [x] Launch snapshot — added `pace` and `clean_streak`
- [x] Both backoff `state.update({...})` calls (throttle + error-streak) include current `pace`/`clean_streak`
- [x] Confirmed still exactly four `_save_background_state` call sites; no per-song disk write
- [x] `_resume_background_downloading` — reads, clamps, and threads both through `_finish_resume_and_launch` → `_launch_background` → `_dl_thread`
- [x] `_dl_thread` / `_launch_background` / `_finish_resume_and_launch` / `_handle_background_error_streak` accept optional pace/clean_streak defaulting to `1.0` / `0`
- [x] `_clamp_pace` / `_clamp_clean_streak` with the machine-safety rationale (a pace of 0 is a zero inter-song delay, i.e. a busy-loop)
- [x] **Deviation:** introduced `_PACE_MIN`/`_PACE_MAX`/`_PACE_DEFAULT` module constants and used them in BOTH the live logic and the clamp. Values are unchanged (0.5 / 6.0 / 1.0); only their home is new. Two literal bands that must stay equal is precisely the drift bug this feature could otherwise introduce. The 2.0 growth factor, 0.7 decay and 8-song threshold remain untouched literals, as specified.
- [x] `tests/test_background_state.py`: `test_pace_and_clean_streak_round_trip`
- [x] `tests/test_background_state.py`: `test_missing_pace_keys_load_at_todays_defaults`
- [x] `tests/test_background_state.py`: `test_out_of_band_pace_is_clamped` (parametrized: `0`, `-1`, `999`, `inf`, `"fast"`, `None`, `NaN`)
- [x] `tests/test_background_state.py`: `test_out_of_band_clean_streak_is_clamped`
- [x] `tests/test_background_state.py`: `test_clamp_band_matches_the_band_the_live_logic_produces`
- [x] `tests/test_background_mode_resume.py`: `test_resume_restores_the_persisted_pace`
- [x] `tests/test_background_mode_resume.py`: `test_resume_without_pace_keys_starts_at_neutral`
- [x] `tests/test_background_mode_resume.py`: `test_resume_clamps_a_corrupt_persisted_pace`
- [x] `tests/test_background_mode_controller.py`: `test_throttle_save_includes_the_current_pace`, `test_foreground_run_does_not_persist_pace`, `test_dl_thread_starts_from_the_pace_it_is_handed` (landed here, not `test_background_mode_backoff.py` — same reason as Task 1: that module is pure-function-only with no `_dl_thread` harness)
- [x] **3 pre-existing resume tests updated** — their `_launch_background` stubs hard-coded the old 3-arg signature and now absorb the two new params with `*rest`
- [x] `pytest tests/test_background_state.py tests/test_background_mode_resume.py tests/test_background_mode_controller.py -q` green (58 passed)

## ▶ Checkpoint 4 — DONE
- [x] `pytest tests/ -q` full suite green (792 passed, 1 skipped)
- [x] Manual read-check: still exactly four `_save_background_state` call sites; `pace * 2.0`, `pace * 0.7`, `clean_streak >= 8` unchanged
- [ ] Commit (Part B, independently revertable) — not committed; user has not asked for a commit yet

## Task 5: Full regression and scope review  · Opus 5 · DONE
- [x] `pytest tests/ -q` green — **792 passed, 1 skipped** (735 baseline + 57 new)
- [x] `python -m py_compile VideoDownload.py gui.py` clean
- [x] **Not** "no pre-existing test modified except by addition" — 7 were changed. All 7 pinned behavior these specs deliberately change (4 cookie-fallback assertions, 3 resume stubs on a changed signature). Called out per task above; this acceptance criterion was written before the test bodies were read and was wrong for Tasks 2 and 4.
- [ ] Four separate commits — not committed; user has not asked for a commit yet
- [x] Diff reviewed against the launch-reliability spec's **Never** list: no PID lock file (source-scan test), exit 0 (test), never silent (test), no state writes from the second instance (test)
- [x] Diff reviewed against the cookie/pacing spec's **Never** list: no rate-limit rotation (extraction-time only), no `settings.json` write (test), no unclamped pace (test), `LONG_BACKOFF_SECONDS`/`next_resume_at`/`maybe_recompute_schedule`/`record_throttle_episode`/`throttle_history.json` all untouched (diff-filtered), no cookie values in logs (test)
- [x] Both specs' Open Questions re-read — all 7 still open after implementing; **subsequently all 7 resolved, see Task 6**

## Task 6: Resolve all seven Open Questions  · Opus 5 · DONE
Evidence gathered first, then decided. Three needed measurement, three were closed by reasoning, one was a user decision.

**Measured**
- [x] Per-song cadence from today's `video.mp4` mtimes: median **26.1s**, mean 37.0s, p10/p90 17.7s/75.8s (n=71 gaps ≤300s)
- [x] Cookie-store probe cost per browser: firefox **10ms** (OK), edge **655ms** (fail), chrome **1589ms** (fail)
- [x] CLI reachability: `main()` runs only via `python VideoDownload.py`; `build.py`'s frozen exe launches the GUI; README documents no CLI entry for it

**Cookie/pacing spec**
- [x] Q1 ceiling — **6.0 → 24.0** (user decision). Old ceiling could only cut request rate 28%; 24.0 cuts it 64%. Takes 5 doublings to reach, ~71 clean songs to decay back
- [x] Q2 resume pace — **verbatim, no penalty**; self-corrects within one song either way
- [x] Q3 CLI pacing — **no**; comment at the sleep site explains why and says extract-don't-copy if it ever ships. Also closes the same question `SPEC-fail-fast-preconditions.md` left open
- [x] Q4 eager chain probe — **no**; cost is a wash (sticky either way), but an eager probe would bake in the transient "browser is running" lock at the worst moment

**Launch-reliability spec**
- [x] Q1 focus vs message — **focus, brittleness removed**: extracted `_window_title()`, used by both `App.__init__` and the guard, so the lookup string can't drift
- [x] Q2 retry wait — **made answerable rather than guessed**: single 20s retry → ladder of 20s/40s/60s, each stamping exit code + timestamp, so whichever rung succeeds bounds the recovery window
- [x] Q3 mutex scope — **per-user**; `Global\` would wrongly block a second Windows user and can require `SeCreateGlobalPrivilege`

**Verification**
- [x] `test_pace_ceiling_is_reachable_by_doubling_and_clamps_there`
- [x] `test_launcher_retry_ladder_waits_and_ping_fallbacks_agree` (parametrized 20/40/60 with their `ping -n` pairs)
- [x] `test_launcher_stamps_every_attempt_so_recovery_time_is_recoverable`
- [x] `pytest tests/ -q` green — **796 passed, 1 skipped**
- [x] `py_compile` clean; both specs' Open Questions sections rewritten as resolved with the evidence

## Manual verification (post-merge, real run)

Forceable on demand (Task 3):
- [ ] With the app running, double-click the shortcut: existing window comes forward, no second process in Task Manager
- [ ] That attempt's `launch_log.txt` reports exit code 0 and notepad does not open
- [ ] `background_state.json` mtime unchanged by the blocked launch
- [ ] Kill from Task Manager and relaunch immediately: starts with no stale-lock recovery step

Observations, not gates (Tasks 1, 2, 4):
- [ ] With `cookie_browser: "chrome"`, `log.txt` shows the DPAPI warning followed by a transition to firefox — not "continuing without browser cookies"
- [ ] No chain-exhausted line appears; the run proceeds with cookies
- [ ] Stop and relaunch mid-run: resume reports a restored pace, not `1.0`
- [ ] Next throttle log line names both the pace and what YouTube actually said
- [ ] (If the import transient recurs) record per-attempt timestamps from `launch_log.txt` against launch-reliability Open Question 2
