# Plan: Browser-Cookie Fallback Chain + Pacing + Launch Reliability

**Specs**: [`SPEC-cookie-chain-and-pacing.md`](../SPEC-cookie-chain-and-pacing.md) (Tasks 1, 2, 4)
and [`SPEC-launch-reliability.md`](../SPEC-launch-reliability.md) (Task 3)

**Naming note**: `/plan`'s defaults are `tasks/plan.md` / `tasks/todo.md`, both of
which already belong to this project's original whole-app plan. This follows the
established `plan-<feature>.md` / `todo-<feature>.md` convention.

## Overview

Five tasks in strict dependency order: every independent task lands before the
one task that depends on any of them, and the review pass lands last. Each task
carries its own tests — no separate "write tests" task, matching this project's
existing plans.

**A convenient consequence of `SPEC-cookie-fallback-fix.md`:** all three yt-dlp
construction sites (`search_candidates` `:787`, `fetch_audio` `:824`,
`download_video` `:897`) already route through
`_run_ytdlp_with_cookie_fallback`, and `_base_opts()` is the single place
`cookiesfrombrowser` is set. Task 2 therefore needs **no call-site wiring at
all** — it is contained entirely within `VideoDownload.py:657-780`. That is what
makes it a one-task change rather than the five-task shape the previous cookie
spec needed.

## Task Summary

| # | Task | Layer | Model | Why that model |
|---|---|---|---|---|
| 1 | Part C — diagnostics | 0 | **Sonnet 5** | Two log lines and three tests, fully specified. Only demand is this repo's comment convention (every constant cites its incident). |
| 2 | Part A — cookie chain | 0 | **Opus 5** | Highest reasoning load: boolean→set state migration, a retry rewritten as a loop while preserving a subtle contract, 11 tests, and a byte-identical regression to protect. |
| 3 | Single-instance + retry wait | 0 | **Opus 5** | Highest blast radius: a mistake here stops the app launching at all. ctypes signatures, handle lifetime, an exit-code interaction with the `.bat`, and tests that must not acquire a real mutex. |
| 4 | Part B — pacing persistence | 1 | **Sonnet 5** | Mechanical plumbing through three call layers with a clear contract; the one subtlety (missing-key forward compat) is specified explicitly. |
| 5 | Regression + scope review | 2 | **Opus 5** | Judgment, not code: four diffs read against two specs' Never lists, plus deciding whether eight Open Questions still hold. |

Task 1 is the only place a cheaper model is arguably right — **Haiku 4.5** could
carry it. Sonnet is the pick because this codebase rejects bare constants and
expects every comment to cite the dated incident behind it, which smaller models
tend to under-serve. That is a style judgment, not a correctness one.

## Dependency Graph

```
Layer 0 (independent -- any order, or in parallel)
  Task 1: Part C -- diagnostics (VideoDownload.py:1801, gui.py:3266)
  Task 2: Part A -- cookie chain state + helper loop
  Task 3: single-instance guard + launcher retry wait     [second spec]

Layer 1
  Task 4: Part B -- persist/restore pace + clean_streak   ── depends on Task 1

Layer 2
  Task 5: full regression + scope/diff review             ── depends on 1, 2, 3, 4
```

**The only ordering constraint inside Layer 0 is that there is none** — the
three touch disjoint code (`run_song_with_backoff`'s except clause, the cookie
helper, and `gui.run()` plus the `.bat`). They are numbered 1-3 by descending
urgency, not by dependency: Task 1 unblocks Task 4, Task 2 is the fix that
would have saved the 2026-08-09 run outright, Task 3 prevents a data-corrupting
double-launch.

**Task 4's dependency on Task 1 is informational, not mechanical.** Task 1
produces the logged pace data that would justify ever changing the pacing
constants — which Task 4 deliberately does not do. Task 4 would compile and pass
without Task 1; it would just be unobservable in a real run.

**Task 3 is governed by a different spec** and shares no code with the others.
It is carried in this plan rather than its own because it is one task against
one working branch; it can be lifted out and landed separately without touching
anything else.

---

## Task 1: Part C — make the logs answer the tuning question

**Model**: Sonnet 5 · **Layer**: 0 · **Spec**: cookie-chain-and-pacing

**Description**: Two one-line diagnostic changes. Neither alters control flow.

**Exact change 1** (`VideoDownload.py:1793-1802`): `run_song_with_backoff`'s
`except BotDetected:` gives up after the short retries and logs
`log.warning('Rate-limited and gave up on %s', song_name)`, discarding the
exception. `BotDetected` is raised with `str(e)` at `:493` and `:797`, so the
text is already in hand — bind it and pass it through, truncated:

```python
except BotDetected as e:
    ...
    # Which sign fired matters: a "sign in to confirm you're not a bot"
    # challenge and an HTTP 429 imply different remedies, and the 2026-08-09
    # log could not tell them apart because this line dropped the text.
    log.warning('Rate-limited and gave up on %s (%s)', song_name, str(e)[:200])
```

**Exact change 2** (`gui.py:3265-3266`): `_handle_background_throttle`'s
`log.info(...)` names the song, `resume_at` and escalation step but not the
pace the run had reached. Add it. This requires `pace` to be visible at that
call site — pass it as an argument rather than promoting it to an attribute
(the method already takes seven, and a local stays a local).

**Acceptance criteria:**
- The give-up warning contains the triggering exception's text, truncated to a
  bounded length so a pathological yt-dlp message cannot flood the rotating log.
- The background-throttle info line contains the current pace.
- No control flow, return value, or signature change other than
  `_handle_background_throttle` gaining a `pace` parameter.
- `BotDetected`'s own message is unchanged; `_BOT_SIGNS` is untouched.

**Verification:**
- `tests/test_background_mode_backoff.py`: add
  `test_throttle_log_line_names_the_current_pace` — `caplog` at INFO, assert the
  emitted record's message carries the pace value it was called with.
- `tests/test_fail_fast.py` (already owns `run_song_with_backoff` coverage):
  add `test_give_up_warning_carries_the_bot_error_text` and
  `test_give_up_warning_truncates_a_pathological_error_message`.
- `pytest tests/test_background_mode_backoff.py tests/test_fail_fast.py -v` green.

**Dependencies**: None.

**Files touched:** `VideoDownload.py`, `gui.py`, `tests/test_fail_fast.py`,
`tests/test_background_mode_backoff.py`

---

## ▶ Checkpoint 1
- `pytest tests/ -q` full suite green (735 + new tests, 1 skipped)
- Manual read-check: the two log lines are the only non-test diff; no signature
  change beyond `_handle_background_throttle`'s new parameter
- Commit (Part C, independently revertable)

---

## Task 2: Part A — cookie fallback chain

**Model**: Opus 5 · **Layer**: 0 · **Spec**: cookie-chain-and-pacing

**Description**: Replace the single `COOKIE_BROWSER` + boolean `_COOKIES_BROKEN`
with an ordered chain and a per-browser broken set. Contained to
`VideoDownload.py:657-780`; no call site changes.

**State** (replacing `:668-675`'s `_COOKIES_BROKEN`):

```python
# Ordered fallback, not a single choice: on 2026-08-09 a run went 9.5 hours
# cookie-free because Chrome's App-Bound Encryption (yt-dlp #10927) made its
# store unreadable, while a perfectly good signed-in Firefox store sat on the
# same machine untouched. Firefox leads because it is the only one of the three
# not subject to Chromium App-Bound Encryption. The user's dropdown choice
# still leads over this -- see _cookie_chain().
_COOKIE_CHAIN_ORDER = ('firefox', 'edge', 'chrome')

# Browsers whose store proved unreadable THIS PROCESS. Same stickiness the
# boolean had, at finer grain: re-paying a known failure per song is exactly
# the cost SPEC-cookie-fallback-fix.md existed to remove. Never reset by
# configure_cookies() -- tests must reset it explicitly.
_BROKEN_COOKIE_BROWSERS = set()
```

**`configure_cookies()`** keeps its signature and its defensive contract
(unsupported name → warn, leave disabled). It additionally builds and stores the
chain: preferred browser first, then `_COOKIE_CHAIN_ORDER` with the preferred
entry de-duplicated out, then filter to `_SUPPORTED_COOKIE_BROWSERS`.

**`_base_opts()`** keeps its signature. It sets `cookiesfrombrowser` from the
first chain entry not in `_BROKEN_COOKIE_BROWSERS`, and omits the key entirely
when the chain is exhausted — the same shape today's `_COOKIES_BROKEN` gate
produces, so the off/exhausted output stays byte-identical.

**`_run_ytdlp_with_cookie_fallback()`** becomes a loop instead of a single
retry. On an `_is_cookie_decrypt_error` match: add the current browser to the
broken set, log one warning naming both the browser that failed and the next to
try, rebuild opts, retry the same `fn`. When no candidates remain, log the
chain-exhausted warning once and make a final cookie-free attempt. Non-cookie
exceptions still propagate untouched.

**Acceptance criteria:**
- With preference `chrome` and a chain of three, a DPAPI failure on chrome
  advances to firefox and the **same** call returns its result.
- Failures on all candidates end cookie-free, having logged one warning per
  transition plus one chain-exhausted line — never one per song.
- A browser in `_BROKEN_COOKIE_BROWSERS` is never offered again this process.
- All three `_COOKIE_ERROR_SIGNS` strings trigger advancement.
- A non-cookie exception propagates without advancing the chain or mutating the
  broken set.
- With cookies off, `_base_opts()` output is byte-identical to today — the
  central regression `tests/test_cookie_support.py` exists to prove.
- Nothing writes `settings.json`; the dropdown's three values are unchanged.
- No cookie *value* reaches any log line — only browser names.

**Verification:**
- `tests/test_cookie_support.py`: extend `setup_function`/`teardown_function` to
  reset `vd._BROKEN_COOKIE_BROWSERS` (a leaked set poisons every later test in
  the file — the same trap the existing `_COOKIES_BROKEN` reset documents).
  Reuse the existing `_make_fake_ydl_class(behaviors)` stand-in; a three-entry
  behaviors list now exercises a full chain traversal.
  - `test_chain_puts_the_preferred_browser_first_then_the_defaults`
  - `test_chain_does_not_repeat_the_preferred_browser`
  - `test_chain_filters_unsupported_browser_names`
  - `test_dpapi_failure_advances_to_the_next_browser_and_the_same_call_returns`
  - `test_exhausting_every_browser_ends_cookie_free`
  - `test_each_cookie_error_sign_advances_the_chain` (parametrized over
    `_COOKIE_ERROR_SIGNS`)
  - `test_non_cookie_error_propagates_without_advancing`
  - `test_a_broken_browser_is_not_retried_on_a_later_call`
  - `test_one_warning_per_transition_not_one_per_song`
  - `test_base_opts_is_byte_identical_when_cookies_are_off` (regression)
- `tests/test_cookie_settings_gui.py`: add
  `test_falling_back_never_writes_cookie_browser_to_settings`.
- `pytest tests/test_cookie_support.py tests/test_cookie_settings_gui.py -v` green.

**Dependencies**: None.

**Files touched:** `VideoDownload.py`, `tests/test_cookie_support.py`,
`tests/test_cookie_settings_gui.py`

---

## ▶ Checkpoint 2
- `pytest tests/ -q` full suite green
- Manual read-check: `search_candidates`, `fetch_audio`, `download_video` diffs
  are **empty** — if any of them changed, the chain leaked out of the helper
- Manual read-check: `_COOKIE_ERROR_SIGNS` and the dropdown values unchanged
- Commit (Part A, independently revertable)

---

## Task 3: Single-instance guard + launcher retry wait

**Model**: Opus 5 · **Layer**: 0 · **Spec**:
[`SPEC-launch-reliability.md`](../SPEC-launch-reliability.md)

**Description**: Nothing currently stops a second instance, and `App.__init__`
schedules `_maybe_resume_background` — so a stray double-click during a
multi-day background run puts two processes on the same
`background_state.json`, each overwriting the other's `remaining_folders`.
Separately, the launcher's 5-second retry wait was shown too short by the
2026-08-09 20:02 failure.

**Exact change 1** (`gui.py:3696`, inside `run()`, immediately after the
existing `SetCurrentProcessExplicitAppUserModelID` block and **before**
`App()`): acquire a named Win32 mutex via `ctypes`. On
`ERROR_ALREADY_EXISTS (183)`: best-effort `FindWindowW` +
`ShowWindow`/`SetForegroundWindow` on `f'BackstageHero  v{__version__}'` (two
spaces, matching `gui.py:1873`), falling back to `MessageBoxW`, then
`sys.exit(0)`.

**The exit code is the trap.** `Launch BackstageHero.bat:49-82` retries on
non-zero `%ERRORLEVEL%` and then opens the log in notepad. A second instance
exiting non-zero would be retried, blocked again, and would report "could not
start twice in a row" — turning the guard into a false alarm. It must exit 0.

Store the handle in a module-level global; a local would be garbage-collected
and release the mutex mid-life. Wrap the ctypes calls so any failure starts the
app normally — a guard that can prevent launching is worse than no guard.

**Exact change 2** (`Launch BackstageHero.bat:69`): raise the retry wait from
5s to 20s, updating the comment to cite 2026-08-09 and the restart-shaped
pattern across all four occurrences. `timeout`/`ping` fallback mechanics
unchanged — only the number moves.

**Acceptance criteria:**
- A second launch focuses the existing window, constructs no `App`, and exits 0.
- Focus failure still shows a message and still exits 0 — never a silent exit.
- A raising ctypes call starts the app normally.
- The mutex handle is retained in module state.
- No PID lock file anywhere; a hard-killed app relaunches with no recovery step.
- A blocked second launch leaves `background_state.json`'s mtime untouched.
- First launch behavior is unchanged, and the AppUserModelID call still runs
  before the window is created.

**Verification:**
- New `tests/test_single_instance.py`. Tests must **never** acquire a real
  named mutex — the pytest process would hold it and poison later tests and
  concurrent runs. Write the guard as an extractable predicate with its Win32
  surface injectable, then `monkeypatch` it, following
  `tests/test_cookie_support.py`'s stand-in-object pattern.
  - `test_first_instance_proceeds_to_construct_the_app`
  - `test_second_instance_exits_zero_and_never_constructs_the_app`
  - `test_second_instance_falls_back_to_a_message_box_when_focus_fails`
  - `test_a_raising_win32_call_starts_the_app_anyway`
  - `test_the_mutex_handle_is_retained_in_module_state`
  - `test_app_user_model_id_still_runs_before_the_window` (regression)
- `pytest tests/test_single_instance.py -v` green.
- Change 2 has no test surface; it is covered by the manual checklist below.

**Dependencies**: None.

**Files touched:** `gui.py`, `Launch BackstageHero.bat`,
`tests/test_single_instance.py`

---

## ▶ Checkpoint 3
- `pytest tests/ -q` full suite green
- Manual read-check: the guard precedes `App()`; no PID file introduced; the
  `.bat` diff is one number and one comment
- Commit (single-instance + retry wait, independently revertable)

---

## Task 4: Part B — persist and restore pacing

**Model**: Sonnet 5 · **Layer**: 1 · **Spec**: cookie-chain-and-pacing

**Description**: `pace` and `clean_streak` are locals in `_dl_thread`
(`gui.py:3021-3022`), so every resume restarts at `1.0` — typically right after
a throttle, which is the worst possible moment to return to baseline speed.
Persist them alongside the fields the loop already rewrites.

**Where to save.** `_save_background_state` has four call sites: `:2994`
(launch snapshot), `:3220` (error-streak backoff), `:3263` (throttle backoff),
`:3325` (Library Tools hand-off). `remaining_folders` is written only at the two
backoff sites, not per song — pace joins it at exactly those, plus the launch
snapshot for initialization. No new save points, no extra disk writes per song.

**Where to restore.** `_resume_background_downloading` (`:3405`) already reads
the state dict; it reads the two new keys, clamps them, and threads them through
`_finish_resume_and_launch` → `_launch_background` → `_dl_thread` as optional
parameters defaulting to today's `1.0` / `0`.

**Clamping.** Restored `pace` is clamped into `[0.5, 6.0]` — the same band the
live logic enforces — and `clean_streak` to a non-negative int. Rationale
mirrors `_MIN_BACKOFF_SECONDS`: a corrupt or hand-edited file must not be able
to produce a pace of 0 (a busy-loop hammering YouTube) or an absurd one (a run
that looks hung). This is a floor on machine safety, not politeness.

**Explicitly not in scope:** the 2.0 growth factor, the 0.7 decay, the 8-song
streak threshold, the 6.0 ceiling, and the 0.5 floor. Spec Open Question 1;
Task 1 produces the data that would justify touching them.

**Acceptance criteria:**
- `pace` and `clean_streak` round-trip through save/load of
  `background_state.json`.
- A resumed background run continues at the persisted pace, not `1.0`.
- A state file written before this change (neither key present) resumes at
  today's defaults without error — the forward-compatibility case that matters,
  since a real in-flight run will cross this change.
- Out-of-band values (`0`, `-1`, `999`, `"fast"`, `None`) clamp or fall back;
  none raise.
- Foreground runs neither read nor write the new keys.
- No new `_save_background_state` call sites; no per-song disk write.

**Verification:**
- `tests/test_background_state.py`: `test_pace_and_clean_streak_round_trip`,
  `test_missing_pace_keys_load_at_todays_defaults`,
  `test_out_of_band_pace_is_clamped` (parametrized over the bad values above).
- `tests/test_background_mode_resume.py`:
  `test_resume_restores_the_persisted_pace`,
  `test_resume_without_pace_keys_starts_at_neutral`,
  `test_foreground_run_does_not_persist_pace`.
- `tests/test_background_mode_backoff.py`:
  `test_throttle_save_includes_the_current_pace`.
- `pytest tests/test_background_state.py tests/test_background_mode_resume.py tests/test_background_mode_backoff.py -v` green.

**Dependencies**: Task 1 (informational — Task 1's log line is what makes the
persisted pace observable in a real run).

**Files touched:** `gui.py`, `tests/test_background_state.py`,
`tests/test_background_mode_resume.py`,
`tests/test_background_mode_backoff.py`

---

## ▶ Checkpoint 4
- `pytest tests/ -q` full suite green
- Manual read-check: exactly four `_save_background_state` call sites still, and
  the pacing constants are untouched
- Commit (Part B, independently revertable)

---

## Task 5: Full regression and scope review

**Model**: Opus 5 · **Layer**: 2 · **Spec**: both

**Description**: The review pass this project's previous specs all ended with.

**Acceptance criteria:**
- `pytest tests/ -q` green, 735 + new tests, no pre-existing test modified
  except by addition.
- `python -m py_compile VideoDownload.py gui.py` clean.
- Four separate commits, each independently revertable.
- Diff review against the launch-reliability spec's **Never** list: no PID lock
  file, no non-zero exit from a blocked second instance, no silent exit, no
  writes to any state file from the second instance.
- Diff review against the cookie/pacing spec's **Never** list: no rotation on
  rate-limit, no `settings.json` write of the fallen-back browser, no unclamped
  restored pace, no changes to `LONG_BACKOFF_SECONDS` / `next_resume_at` / the
  adaptive recompute / `throttle_history.json`, no cookie values in logs.
- Both specs' Open Questions re-read: still open and still accurate, or updated
  with what implementation revealed.

**Verification:** `git diff main --stat` reviewed file by file against both
specs' Boundaries sections.

**Dependencies**: Tasks 1, 2, 3, 4.

---

## Manual verification (post-merge, real run)

Automated tests cannot prove the things that actually motivated these specs.

Task 3's checks can all be forced on demand:

1. With the app running, double-click the desktop shortcut: the existing window
   comes forward, no second process appears in Task Manager.
2. `launch_log.txt` from that attempt reports exit code 0, and notepad does not
   open.
3. `background_state.json`'s modification time is unchanged by the blocked
   launch.
4. Kill the app from Task Manager, relaunch immediately: it starts, with no
   stale-lock recovery step.

Tasks 1, 2 and 4 depend on a real launch and on YouTube's behavior — only step 5
can be forced (set the dropdown to `chrome`); the rest are observations to make,
not gates to pass:

5. `log.txt` shows the Chrome DPAPI warning followed by a transition line naming
   firefox — **not** "continuing without browser cookies".
6. The run proceeds with cookies; no chain-exhausted line appears.
7. Stop and relaunch mid-run: the resume reports a restored pace rather than
   `1.0`.
8. On the next throttle, the log line names both the pace and what YouTube
   actually said.
9. If the `yt_dlp` import transient recurs, record the per-attempt timestamps
   from `launch_log.txt` against launch-reliability Open Question 2 — no
   occurrence has yet bounded how long the condition actually persists.
