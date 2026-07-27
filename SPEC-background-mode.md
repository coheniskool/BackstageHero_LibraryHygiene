# Spec: Unattended Multi-Day Background Mode

## Description

A "Run in background" mode for the main download run, built for real-world libraries (~7,000 songs) where a full pass takes far longer than one sitting and YouTube periodically throttles the app ("confirm you're not a bot") for hours at a time. Today, hitting that throttle after the existing short retry (`VideoDownload.BOT_BACKOFF_SECONDS`, ~7 minutes across 3 attempts) ends the run entirely (`gui.py`'s `'rate_limited'` message handler sets `self._running = False` and shows a warning) — the user has to notice, wait an unknown amount of time, and manually click Start again. Background mode instead backs off in escalating, hours-long steps and keeps retrying indefinitely, survives the app or the machine restarting mid-run, and automatically moves on to running the Library Tools pipeline (`gui._RUN_ALL_ORDER`, already shipped) once downloads are as complete as they can get.

## Objective

**User**: solo hobbyist pointing the app at a real, large library and walking away for days at a time, not babysitting a terminal or a Task Scheduler entry — the app window can be minimized but the process needs to keep running.

**Success looks like**: check a "Run in background" toggle, start the run, close the laptop lid or let Windows reboot for an update partway through, come back days later, and find the app picked back up on its own — retried through however many YouTube throttle windows it hit, finished downloading everything it could, then ran the Library Tools pipeline once, with a log file that tells the whole story of what happened while unattended.

**In scope**:
- Escalating long backoff on YouTube throttle (1h → 4h → 12h → 24h, capped there, starting point only — see gap logging below), retried indefinitely — never gives up on its own.
- Progress persisted to disk so the run survives the app closing or the machine restarting; relaunching the app while a background run was in progress resumes it automatically.
- Automatic hand-off from the download phase to a single Library Tools "Run all" pass (`gui._RUN_ALL_ORDER`) once the download phase ends (every target song is done, skipped, or permanently errored for a non-throttle reason).
- A persistent log file recording every phase transition, throttle/resume event, and error, extending the existing `logging.RotatingFileHandler` setup (`VideoDownload._setup_logging()`, `%LOCALAPPDATA%\BackstageHero\log.txt`).
- A GUI toggle to start it; the app process must stay running (window may be minimized), no headless/CLI/Task-Scheduler entry point this phase.
- **Gap logging + adaptive backoff.** Every throttle episode (from first `'stop'` to the retry that actually succeeds) is recorded — timestamp, how many escalation steps it took, the real elapsed gap. Once 5 episodes are on record, the schedule recomputes itself from that real data instead of staying pinned to the 1h/4h/12h/24h guess forever. No fixed floor: if the data says YouTube unblocks faster than an hour, the schedule is allowed to shrink below it (the user's explicit choice, trading a small re-trigger risk for not wasting unattended time). A tiny sanity-clamp (a handful of minutes) still applies purely to stop a logging glitch from producing a busy-loop — that's crash-prevention, not the policy floor the user declined.
- **Fix the stale `YOUTUBE_CLIENTS` bug (`VideoDownload.py:88`).** `['tv_embedded', 'android_vr', 'android']` hardcodes a client (`tv_embedded`) yt-dlp removed as broken in the 2026.01.31 release; every request currently wastes its first attempt on a dead client. Remove the `player_client` override entirely and let yt-dlp use its own maintained default, so this class of bug can't recur the same way. Needs empirical verification (not just a passing test) that DASH/1080p+ streams still come through without PO tokens once the override is gone, since that was `tv_embedded`'s original justification.
- **Optional cookie support.** A settings toggle + browser dropdown (Chrome/Firefox/Edge — matches yt-dlp's `--cookies-from-browser`) that, when enabled, passes `cookiesfrombrowser` through to yt-dlp's Python API. Off by default. Reduces bot-detection frequency in the first place (yt-dlp's own current top recommendation), independent of and complementary to the backoff/resume machinery above.

**Out of scope this phase**:
- Running more than one full download→library-tools cycle per background run (no auto-repeat/loop back to downloading after library tools finish).
- A headless/no-GUI-window mode (Task Scheduler, CLI flag). The window can be minimized; the process must still be running.
- Any change to the *existing* short per-song retry (`BOT_BACKOFF_SECONDS`) — background mode's long backoff only kicks in after that's already exhausted (i.e. after `run_song_with_backoff` returns `'stop'`).
- Preventing OS sleep. If the machine sleeps, the run pauses and resumes on wake (via the persisted `resume_at` timestamp), same as any other multi-hour wait.

## Commands

```
Run:    python gui.py          (unchanged entry point; "Run in background" is a
                                 toggle inside the existing download-run controls)
Test:   pytest tests/ -v
```

No new CLI entry point or command-line flags this phase (see Out of scope).

## Project Structure

```
VideoDownload.py   -> BOT_BACKOFF_SECONDS / run_song_with_backoff() UNCHANGED.
                      New: a long-backoff schedule constant (e.g.
                      LONG_BACKOFF_SECONDS = [3600, 14400, 43200, 86400], last
                      value repeats) and a resume-at-timestamp helper, used only
                      by the background-mode orchestration below -- the
                      per-song short retry keeps its existing, unmodified
                      behavior and 'stop' return value. The schedule itself is
                      a starting point only -- see throttle_history.json below,
                      which can override it once real data exists.
                      Fix: YOUTUBE_CLIENTS (line 88) and its use in
                      _base_opts()'s extractor_args removed -- yt-dlp's own
                      built-in player_client default is used instead. Requires
                      one real download verified to still get a DASH/1080p+
                      stream without a PO token before this lands.
                      New (opt-in): cookiesfrombrowser passed through
                      _base_opts() when the cookie-support setting is on,
                      sourced from whichever browser the user picked in the GUI.

gui.py              -> New: a background-mode orchestrator (long-lived
                      controller object or a small state machine, exact shape
                      TBD in /plan) that:
                        1. wraps the existing _dl_thread download loop,
                        2. on a 'rate_limited' outcome, persists a resume_at
                           timestamp instead of stopping the run, waits (via
                           the existing self._stop_evt.wait()-based cancellable
                           wait pattern, scaled to hours) or reschedules on
                           next launch if the app was closed,
                        3. on true download-phase completion, calls into
                          LibraryToolsDialog's existing _run_tool_scan()/
                          _RUN_ALL_ORDER pipeline for a single pass,
                        4. persists phase/progress to a state file so a
                          relaunch can resume without re-deriving anything
                          fragile.
                      New GUI element: a "Run in background" checkbox/button
                      near the existing Start/Stop controls.
                      New GUI element: an opt-in cookie-support toggle +
                      browser dropdown, persisted via the existing
                      _persist_setting()/settings.json mechanism (a UI
                      preference, not run state -- lives alongside quality/
                      share_matches/enrich_after_scan, not in
                      background_state.json).

<data_dir>/background_state.json
                    -> New. updater.data_dir() (%LOCALAPPDATA%\BackstageHero)
                      is already where client_id and log.txt live -- this
                      joins them. Holds: phase ('downloading'|'library_tools'|
                      'done'), resume_at (unix timestamp or null), the target
                      song list identity (songs_folder path + quality/replace/
                      resync settings, so a resume can rebuild the same run),
                      and enough progress bookkeeping to skip what's already
                      done without re-deriving it from a fresh library scan
                      alone. Exact schema is a /plan decision.

<data_dir>/log.txt  -> UNCHANGED mechanism (VideoDownload._setup_logging()),
                      new log lines for: background mode started/stopped,
                      each throttle-and-backoff event (with the computed
                      resume_at), each resume (including "resumed after app
                      restart"), download-phase completion, each Library
                      Tools step starting/finishing as part of the auto-run,
                      the final summary, and (new) every schedule adjustment
                      the adaptive algorithm below makes, with the data that
                      drove it.

<data_dir>/throttle_history.json
                    -> New. One record per completed throttle episode:
                      started_at, resolved_at, escalation_steps_used,
                      gap_seconds. Append-only, atomic writes (same discipline
                      as background_state.json). Once len(history) >= 5, a
                      recompute pass derives a new LONG_BACKOFF_SECONDS-shaped
                      schedule from the observed gaps and escalation depth --
                      exact formula is a /plan decision (direction only: grow
                      the schedule if episodes are consistently needing every
                      escalation step, shrink it if episodes consistently
                      resolve on the first or second step). No fixed floor
                      per the user's choice; a small crash-prevention clamp
                      (a few minutes) stops a logging bug from ever producing
                      a zero/negative wait. The recomputed schedule is itself
                      persisted (in this file or background_state.json,
                      /plan decision) so it survives a restart same as
                      everything else here.
```

## Code Style

Match this codebase's existing conventions (unchanged from `SPEC.md`): 4-space indentation, f-strings, plain functions/dataclasses over new classes where the existing style already prefers that (e.g. `LibraryToolsDialog` is a class because it's a `ctk.CTkToplevel`; a background-mode *state* holder should default to a `@dataclass` unless `/plan` finds a concrete reason it needs to be a class). Comments explain *why*, matching this file's own style. Atomic writes (temp file + `os.replace`) for `background_state.json`, same discipline `set_ini_values()`/`chart_rename.py`'s manifest writes already use for any file a mid-write crash could otherwise corrupt.

## Testing Strategy

- **Unit-tested**: the long-backoff schedule/escalation logic (given N consecutive throttle events, what's the computed `resume_at`), the state file's save/load round-trip (including resuming correctly from a `resume_at` that's already in the past vs. still in the future), and the phase-transition logic (download phase truly complete → triggers exactly one Library Tools pass, not zero and not a loop).
- **Regression-tested**: the existing `_dl_thread`/`run_song_with_backoff` short-retry behavior must be provably unchanged when background mode is OFF — this phase must not alter today's default (non-background) run behavior at all.
- **GUI-tested** in the style `tests/test_library_tools_dialog.py` already established: real (non-mocked) `ctk.CTk()`/`ctk.CTkToplevel()` construction where feasible, module-skipped without a display; background-mode start/resume/completion driven directly on the controller rather than through real multi-hour waits (mock the clock / the wait primitive, never actually sleep for the test's own sake).
- **Not unit-tested**: real YouTube throttle behavior itself (whether it actually lifts within 24h, whether the observed pattern holds) — this is empirical, validated by the user's own multi-day runs, same as this project's existing "manual in-game playtest" discipline for anything that needs a real external service.
- **Unit-tested (new)**: `throttle_history.json` round-trip and the recompute trigger (exactly 5 records → recomputes; 4 → doesn't), on synthetic/fabricated history data — never real logged episodes, since those don't exist until the user runs this for real. The recompute algorithm's *direction* (grows/shrinks appropriately given fabricated all-first-step-success vs. all-escalated-to-24h history) is tested; the exact numbers it produces are not asserted precisely, since the formula itself may be tuned after real data comes in.
- **Manually verified (new, before merge)**: one real download after the `YOUTUBE_CLIENTS` override is removed, confirmed to still produce a DASH stream at 1080p+ with no PO-token prompt — this is an empirical claim about yt-dlp's current default behavior, not something a unit test can prove.
- No CI — local, run on-demand, same as `SPEC.md`.

## Boundaries

- **Always**:
  - Persist progress before waiting out a long backoff, not after — a crash or forced-close during the wait must not lose track of `resume_at` or which songs are already done.
  - Keep the existing non-background download run's behavior byte-identical when the new toggle is off.
  - Log every throttle/backoff/resume event with enough detail (timestamp, computed `resume_at`, which song) that the log file alone answers "what happened while I was away."
  - Run the Library Tools auto-pass with the **same dry-run semantics the user has already set** on each tool's own checkbox in `LibraryToolsDialog` (per Resolved Decisions below) — never silently force live mode.
  - Only one background run active at a time per library, matching `LibraryToolsDialog`'s existing "only one tool runs at a time" rule (background mode and a manual Library Tools run must not overlap either).
- **Ask first**:
  - Any change to `BOT_BACKOFF_SECONDS` or `run_song_with_backoff`'s existing short-retry behavior/return contract.
  - Adding a headless/CLI/Task-Scheduler entry point (explicitly out of scope this phase, per the user's own choice above).
  - Preventing OS sleep (e.g. `SetThreadExecutionState` on Windows) — not requested, would be a behavior change with real power/thermal implications for a "walk away for days" feature.
  - Changing the recompute algorithm's aggressiveness/formula after real data comes in and looks wrong — that's a tuning decision, not a bug fix, even though the mechanism itself is pre-approved.
- **Never**:
  - Never mark the download phase "complete" while a throttle backoff is still pending — a resume-in-progress state must not be mistaken for done and trigger Library Tools early.
  - Never run Library Tools' destructive steps (fix_chart_names renames, find_duplicates relocations, etc.) live if the user left those checkboxes on dry-run — background mode observes the existing per-tool toggles, it doesn't override them.
  - Never lose the resume state on a clean app shutdown (the whole point is surviving exactly that).
  - Never enable cookie support without an explicit opt-in, and never send cookies anywhere but directly to yt-dlp's own request to YouTube (no logging of cookie contents, no persistence of the actual cookie values beyond what the browser's own cookie store already holds).
  - Never let the adaptive recompute produce a schedule that can't terminate (e.g. all-zero steps) — the crash-prevention clamp is non-negotiable even though the policy floor above it is not.

## Success Criteria

- [ ] A "Run in background" toggle starts the same download run as today, but a YouTube throttle no longer ends it
- [ ] On throttle, the run backs off on the escalating schedule (1h → 4h → 12h → 24h, then repeats at 24h) and resumes on its own, indefinitely
- [ ] Closing the app (or the machine restarting) mid-backoff-wait or mid-download, then relaunching, resumes the background run automatically from persisted state — no manual restart needed
- [ ] Once the download phase is truly complete (every target song done/skipped/permanently-errored, no pending throttle), the Library Tools "Run all" pipeline (`_RUN_ALL_ORDER`) runs exactly once, respecting each tool's existing dry-run checkbox state
- [ ] `log.txt` contains a readable record of every phase transition, throttle/backoff/resume event, and the final summary
- [ ] The existing non-background download run is provably unchanged (regression tests pass unmodified)
- [ ] Every throttle episode is recorded to `throttle_history.json`; after 5 recorded episodes, the backoff schedule recomputes from real data (verified with fabricated history in tests, not requiring 5 real throttle events to exist)
- [ ] `YOUTUBE_CLIENTS`/the `player_client` override is removed from `VideoDownload.py`; one real download confirms DASH/1080p+ still works without a PO-token prompt
- [ ] An opt-in cookie-support toggle (off by default) + browser dropdown passes `cookiesfrombrowser` to yt-dlp when enabled, and does nothing when disabled (today's default behavior, unchanged)

## Resolved Decisions

- **Restart survival** (resolved): background mode persists progress to disk and auto-resumes on relaunch — not just "the app must stay running." This is the harder-to-build option but was the user's explicit, informed choice given the realistic multi-day/reboot timeline.
- **Throttle backoff shape** (resolved): escalating long backoff (1h → 4h → 12h → 24h, capped and repeating at 24h), retried indefinitely — never gives up on its own. This is a deliberate behavior change *only* inside background mode; the existing short-retry-then-stop behavior is unchanged when background mode is off.
- **Status visibility** (resolved): a persistent log file is the primary mechanism this phase (extends existing `RotatingFileHandler` logging). A GUI "what happened while you were away" summary panel and a Windows toast notification were both raised as options but not chosen for this phase — worth a follow-up if the log file alone proves insufficient in practice.
- **Trigger mechanism** (resolved): a GUI toggle only. The app process must stay running (window may be minimized); no headless/CLI/Task-Scheduler support this phase.
- **Library Tools auto-run's dry-run behavior** (resolved, inferred — confirm in `/plan`): observes whatever each tool's own dry-run checkbox is already set to in `LibraryToolsDialog`, rather than background mode having its own separate live/preview toggle. Matches this codebase's established "destructive steps default to dry-run, the user opts in per tool" pattern (`test_dry_run_defaults_on_for_every_tool`). If the user actually wants background mode to force a live run regardless of those checkboxes, that's a one-line change to flag during `/plan`.
- **`YOUTUBE_CLIENTS` fix approach** (resolved): remove the override entirely rather than hardcoding an updated list, so this class of bug (pinning to a client yt-dlp later deprecates) can't recur the same way again. Trades a small amount of explicit control for yt-dlp's own maintainers keeping the default current. Gated on a real-download verification step before landing.
- **Cookie mechanism** (resolved): browser cookies (`cookiesfrombrowser`), not a cookie file. No export step for the user; the tradeoff (the source browser's cookie DB can lock while it's open, depending on browser) is accepted.
- **Adaptive backoff aggressiveness** (resolved): fully data-driven once triggered, no fixed floor — the user explicitly chose throughput over an extra safety margin here. A crash-prevention clamp (minutes, not the 1h the user declined) still exists purely to keep the schedule from ever producing a busy-loop.
- **Adaptive backoff trigger threshold** (resolved): 5 recorded throttle episodes.

## Notes

- The real unknown this spec can't pin down: how long YouTube's throttle actually lasts in practice. The 1h/4h/12h/24h schedule is a reasonable, conservative *starting point*, not a measured fact — flagged the same way this project already flags "validated by the user's own environment" items (e.g. the in-game sync playtest in `SPEC.md`). The gap-logging + adaptive-recompute mechanism exists specifically to replace this guess with real data over time, but the schedule going into the user's first real run is still a guess.
- Gap logging has an inherent measurement blind spot worth naming: the observed "gap until success" is a function of both YouTube's real block duration AND our own schedule's wait length (we only ever find out a wait was "enough," never the true minimum that would have worked) — statistically, this is right-censored data. The recompute algorithm's direction (grow if consistently maxing out escalation, shrink if consistently succeeding early) is a reasonable response to that limitation, not a way around it.
- `gui.py` is already flagged as a fragile file (high coupling, high churn) by this repo's own fragility scoring — background mode's orchestration should be added as new, mostly-additive methods/state (mirroring how `LibraryToolsDialog`'s "Run all" was just added) rather than restructuring `_dl_thread`/`_handle_msg`'s existing control flow, to keep the blast radius contained.
- The `YOUTUBE_CLIENTS` fix and the cookie-support option are both independent of the throttle/resume/restart-survival machinery — they reduce how *often* throttling happens in the first place, rather than how the app copes with it once it does. Both could ship ahead of the rest of background mode if there's value in landing the quick, lower-risk win first (flag this in `/plan` if sequencing matters).
