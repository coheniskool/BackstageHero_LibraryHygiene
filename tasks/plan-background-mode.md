# Plan: Unattended Multi-Day Background Mode

*Spec: [`SPEC-background-mode.md`](../SPEC-background-mode.md).*

## Overview

Leads with a small, already-fully-planned, zero-dependency win imported from a separate session (Phase 0), then four independent quick wins and foundational pieces (two of them — the stale client-list bug and cookie support — reduce how *often* YouTube throttles in the first place, and could ship even before the rest of background mode exists); then orchestration — a controller that wraps the existing `_dl_thread` download loop, reacts to throttle by scheduling a long resume instead of stopping, and hands off to a Library Tools pass once downloads are truly done; then the two places the outside world touches this — the GUI toggle, and resuming automatically on app launch. Logging is threaded through orchestration rather than being its own late add-on, since the events worth logging are exactly the state transitions the controller already has to make explicit.

## ⚠ Phase 0 provenance — read before starting it

Phase 0's spec/plan (`SPEC-dry-run-cache.md`, imported below) were authored by a **separate, concurrently-running session** — branch `claude/dry-run-reprocessing-d23e0b`, worktree `karaoke-library-qol-list-8d98ef`, still active as of this plan's last update. It touches a completely different feature (`library_enrichment.py`'s booklet-data sidecar, from the already-merged PR #2) with zero file overlap with the rest of this plan (`VideoDownload.py`/`gui.py`), which is why it's safe to bundle in — but that other session may finish and merge its own PR before this plan reaches Phase 0. **Before starting Phase 0, check `git log origin/main` for a merge referencing this change; if it's already landed, skip Phase 0 entirely and move straight to Phase 1 rather than duplicating the work.** This project already hit exactly this failure mode once (a duplicate `resolver_client.py` caching fix from an untracked session had to be discovered via `git diff` and discarded) — don't repeat it.

## Key implementation decisions (read before starting)

1. **Never touch `run_song_with_backoff`'s existing return contract.** Background mode reacts to the `'stop'` return value the same way `gui.py`'s `'rate_limited'` handler already does today — it just does something different with it (schedule a long resume instead of ending the run). The short retry (`BOT_BACKOFF_SECONDS`, ~7 min) stays exactly as-is; background mode's long backoff is a layer *above* it, not a replacement.
2. **`LibraryToolsDialog._run_tool_scan`'s dispatch table needs to exist without a dialog.** Background mode must run the same six scans `_RUN_ALL_ORDER` already sequences, but there's no open `LibraryToolsDialog` instance during an unattended run. Extract the dispatch (and the summary formatting it feeds) to module-level functions; have the dialog's methods become thin wrappers so its own behavior and existing tests (`tests/test_library_tools_dialog.py`) are unaffected.
3. **State file is separate from `settings.json`.** `_load_settings()`/`_save_settings()` (gui.py, ~line 131-144) hold flat UI preferences (quality, checkboxes, and now the cookie-support toggle) and are loaded once at startup. `background_state.json` holds structured, frequently-rewritten run state (phase, resume_at, progress) and needs atomic writes (temp + `os.replace`) since a crash mid-write during a multi-day unattended run is exactly the failure mode this feature exists to survive. `throttle_history.json` is a third, separate file — append-only episode records, also atomic. Don't fold any of the three together.
4. **Mocked clock in tests, always.** Nothing in this feature's test suite should ever really wait an hour, let alone 24. Every backoff/resume/recompute test drives the computed `resume_at` and a fake "now" directly; the one real `threading.Event.wait()` call in production code is exercised in tests via a very short timeout or by monkeypatching the wait itself.
5. **The client-list fix and cookie support are genuinely independent of the state machine.** Neither touches `background_state.json`, the controller, or the GUI toggle — they change how likely a throttle is to happen, not how the app copes with one. Sequenced early because they're lower-risk and can be verified/shipped on their own.
6. **Phase 0 is independent of everything else in this plan too.** `library_enrichment.py`/`library_enricher.py` (Phase 0) are not `metadata_enrichment.py` (the module `_RUN_ALL_ORDER`/Task 10's Library Tools pipeline actually calls) — similar names, unrelated code paths. Phase 0 exists here purely because it was efficient to bundle a small, ready-to-go, zero-conflict win rather than because background mode needs it.

## Dependency graph

```
Task 1 (library_enrichment.py: remove dry_run gate on sidecar write)   ─── independent (imported plan)
  ├── Task 2 (tests/test_library_enrichment.py: rename + new test)    ← needs 1
  ├── Task 3 (library_enricher.py: CLI --dry-run help text)            ← needs 1
  └── Task 4 (README_ENRICHER.md + SPEC-library-enrichment.md docs)    ← needs 1
                                                                          CHECKPOINT 0
Task 5  (VideoDownload.py: fix stale YOUTUBE_CLIENTS bug)              ─── independent
Task 6  (VideoDownload.py: optional cookie support)                    ─── independent
Task 7  (VideoDownload.py: long-backoff schedule + resume_at helper)   ─── independent
Task 9  (gui.py: background_state.json persistence)                    ─── independent
Task 10 (gui.py: extract Library-Tools dispatch to module level)       ─── independent
        │
Task 8 (VideoDownload.py/gui.py: gap logging + adaptive backoff)  ← needs 7
                                                                          CHECKPOINT 1
        │
Task 11 (gui.py: background-mode controller)  ← needs 7, 8, 9, 10
Task 12 (gui.py: "Run in background" GUI toggle)  ← needs 11
                                                                          CHECKPOINT 2
        │
Task 13 (gui.py: resume automatically on app launch)  ← needs 9, 11
Task 14 (logging for phase/throttle/resume/adjustment events)  ← needs 8, 11 (can run alongside 12/13)
                                                                          CHECKPOINT 3 (final)
```

---

## Phase 0 — Imported: let dry runs persist the enrichment sidecar

*Spec: [`SPEC-dry-run-cache.md`](../SPEC-dry-run-cache.md) (amends `SPEC-library-enrichment.md`), authored by the concurrent session named above. Reproduced here verbatim in shape, not re-derived — see that spec for full rationale.*

**What it does**: `library_enricher.py --dry-run` currently computes the full enrichment result (chart parsing, `notes.mid` hashing, `scores.bin` lookup, Chorus match) and throws it away — `library_enrichment.py:217-218` gates `_save_sidecar()` on `not dry_run`. This reclassifies the sidecar as a read-only computation cache (same category as the already-unconditional Chorus cache) and persists it regardless of `dry_run`, so a preview run's work is reused by the next real run instead of redone from scratch.

### Task 1 — Remove the `dry_run` gate on the sidecar write
- **Model**: Sonnet 5 — a one-line gate removal plus a docstring update.
- **Do**: In `enrich_library()` (`library_enrichment.py:217-218`), remove the `if not dry_run:` guard so `_save_sidecar(sidecar_path, sidecar)` runs unconditionally. Update the docstring (currently lines 167-179, specifically line 175's `"dry_run=True computes everything but writes nothing."`) to state the sidecar is written either way and `dry_run` now means "no library mutations" (there are none in this module today), not "no disk writes."
- **Acceptance**: The call site is unconditional. No other logic in `enrich_library()` changes — the incremental skip check (`chart_hash in sidecar['songs']`, line 203) and `_save_sidecar()`'s atomic tmp-file + `os.replace` pattern are untouched.
- **Verify**: Manual read-check that the guard is gone. `pytest tests/test_library_enrichment.py -v` will show one *expected* failure at this point (`test_enrich_library_dry_run_writes_nothing`) — that's resolved by Task 2, not this task; don't touch the test here.

### Task 2 — Update dry-run test coverage (needs Task 1)
- **Model**: Sonnet 5 — mechanical rename + assertion inversion + one new test mirroring an existing pattern.
- **Do**: Rename `tests/test_library_enrichment.py:63`'s `test_enrich_library_dry_run_writes_nothing` → `test_enrich_library_dry_run_writes_sidecar`, inverting its assertion to confirm the sidecar *exists* and contains the computed entry. Add `test_dry_run_then_real_run_skips_everything`: dry run then real run against the same unchanged library, asserting the second call's `songs_processed == 0` and `songs_skipped == <song count>` (mirrors the existing `test_enrich_library_incremental_skips_unchanged_song` pattern at line 73).
- **Acceptance**: `test_enrich_library_incremental_skips_unchanged_song` (line 73) and `test_enrich_library_force_reprocesses_unchanged_song` (line 86) still pass unmodified — the skip logic itself isn't changing, only what populates `sidecar['songs']` before it runs.
- **Verify**: `pytest tests/test_library_enrichment.py -v` full file green. `pytest tests/ -v` full suite green.

### Task 3 — Update CLI `--dry-run` help text (needs Task 1)
- **Model**: Haiku 4.5 — a one-line argparse help-string wording fix.
- **Do**: `library_enricher.py:45`'s `--dry-run` help text (`"...without writing the sidecar."`) no longer reflects reality — update it to say the sidecar is written either way and only library mutations (none exist in this tool today) would be skipped. Leave the `"Dry run: ..."` vs `"Enrichment complete: ..."` printed distinction in `main()` (line 115) unchanged — the user still typed the flag and expects that label back.
- **Acceptance**: Help text no longer claims the sidecar isn't written. `python library_enricher.py --help` prints cleanly.
- **Verify**: `pytest tests/test_library_enricher_cli.py -v` still green (no test pins the exact string).

### Task 4 — Update docs (needs Task 1)
- **Model**: Haiku 4.5 — two doc-row wording fixes, no logic.
- **Do**: `README_ENRICHER.md:59`'s `--dry-run` table row and `SPEC-library-enrichment.md:37`'s `--dry-run` bullet both currently say "writes nothing" / "without touching the sidecar" — reword both to match the new behavior, consistent with `SPEC-dry-run-cache.md`'s redefinition.
- **Acceptance**: Both docs describe identical `--dry-run` behavior to each other and to Task 1's docstring / Task 3's help text. No other content in either doc changes.
- **Verify**: Manual read-check only (no automated test covers doc prose).

> **CHECKPOINT 0** — Full `pytest tests/ -v` green. Diff review: exactly 5 files touched (`library_enrichment.py`, `library_enricher.py`, `tests/test_library_enrichment.py`, `README_ENRICHER.md`, `SPEC-library-enrichment.md`) — matches the imported spec exactly, no scope creep into its explicitly-deferred `--no-cache-write` escape hatch or into other tools' `dry_run` handling.

---

## Phase 1 — Independent quick wins + foundation

### Task 5 — `VideoDownload.py`: fix the stale `YOUTUBE_CLIENTS` bug
- **Model**: Sonnet 5 — a deletion plus one real-world verification step, low code complexity but needs a genuine manual check, not just green tests.
- **Do**: Remove `YOUTUBE_CLIENTS = ['tv_embedded', 'android_vr', 'android']` (line 88) and its use in `_base_opts()`'s `extractor_args={'youtube': {'player_client': YOUTUBE_CLIENTS}}`. Let yt-dlp use its own built-in default (`visionos,android_vr,web` as of the currently-installed 2026.07.04, with maintainer-managed fallbacks) instead of a hardcoded list this project has to keep current by hand. `tv_embedded` was removed as broken by yt-dlp upstream in the 2026.01.31 release — six months before this fix, confirmed against the actually-installed `yt_dlp.version.__version__`.
- **Acceptance**: `_base_opts()` no longer sets `player_client`. A real download (any song, from source, not mocked) still produces a DASH stream at 1080p+ with no PO-token prompt — the original reason `tv_embedded` was pinned. If that verification fails, this task is not done — do not land the removal without it passing.
- **Verify**: `pytest tests/test_candidate_kind.py tests/test_process_resync.py -q` (existing tests touching `_base_opts()`/download flow) green, unmodified in behavior. Manual: one real `python gui.py` download run, inspect the resulting `video.mp4`'s resolution (`ffprobe`) and confirm it's not capped at 360p or below.

### Task 6 — `VideoDownload.py`: optional cookie support
- **Model**: Sonnet 5 — small, well-scoped addition to an existing options dict, plus a settings toggle following an established pattern.
- **Do**: Add a `use_browser_cookies`/`cookie_browser` pair to `settings.json` (via the existing `_persist_setting()` mechanism in `gui.py`, alongside `quality`/`share_matches`/`enrich_after_scan`) and a GUI toggle + browser dropdown (Chrome/Firefox/Edge) near the other settings checkboxes. When enabled, `_base_opts()` includes `'cookiesfrombrowser': (browser_name,)` (yt-dlp's Python-API equivalent of `--cookies-from-browser`); when disabled (the default), `_base_opts()` is unchanged from today.
- **Acceptance**: Off by default — a fresh install behaves exactly as today. Toggling it on and picking a browser causes yt-dlp requests to carry that browser's YouTube session cookies; toggling off (or leaving the default) sends none. No cookie value is ever logged or written anywhere except passed straight through to yt-dlp's own request.
- **Verify**: Unit test asserting `_base_opts()`'s dict shape with the setting on vs. off (mock `cookiesfrombrowser`'s presence/absence, don't require a real browser profile). GUI test (real `ctk` construction, `tests/test_library_tools_dialog.py` style) confirming the toggle+dropdown exist and persist via `_persist_setting()`.

### Task 7 — `VideoDownload.py`: long-backoff schedule + resume_at helper
- **Model**: Sonnet 5 — small, self-contained, pure-function addition next to existing well-tested backoff code.
- **Do**: Add `LONG_BACKOFF_SECONDS = [3600, 14400, 43200, 86400]` (1h/4h/12h/24h) next to `BOT_BACKOFF_SECONDS`, with a comment explaining it's background-mode-only, and that it's a *starting point* superseded by Task 8's adaptive recompute once enough data exists. Add `next_resume_at(throttle_count, now, schedule=LONG_BACKOFF_SECONDS)`: given how many consecutive long-backoff throttles have happened for this run (0-indexed), the current unix time, and an optional schedule override (so Task 8's recomputed schedule can be passed in without changing this function's shape), returns the next `resume_at` timestamp, indexing into `schedule` and clamping to the last (repeating) value once `throttle_count` exceeds the list length. Do NOT modify `BOT_BACKOFF_SECONDS` or `run_song_with_backoff` itself.
- **Acceptance**: `next_resume_at(0, now)` == `now + 3600`; `next_resume_at(3, now)` == `now + 86400`; `next_resume_at(10, now)` == `now + 86400` (clamped, repeats indefinitely, never raises `IndexError`). Passing a custom `schedule` list uses it instead of the module default. Existing `run_song_with_backoff`/`BOT_BACKOFF_SECONDS` behavior is provably unchanged (existing tests pass unmodified).
- **Verify**: New `tests/test_background_mode_backoff.py` covering the escalation, the clamp, the custom-schedule override, and a same-second-boundary case. `pytest tests/test_candidate_kind.py tests/test_process_resync.py -q` green, unmodified.

### Task 8 — Gap logging + adaptive backoff recompute
- **Model**: Opus 4.8 — the one place in this plan doing real statistical reasoning over noisy, right-censored data (see spec Notes); getting the recompute *direction* right without over-fitting to 5 noisy samples needs more judgment than the plan's other mechanical tasks.
- **Do**: Add `<data_dir>/throttle_history.json` (own file, not folded into `background_state.json` or `settings.json`, atomic writes). Add `record_throttle_episode(started_at, resolved_at, escalation_steps_used)` — appends one record. Add `maybe_recompute_schedule(history)`: no-ops below 5 records; at 5+, derives a new `LONG_BACKOFF_SECONDS`-shaped list from the observed `escalation_steps_used`/gap data (direction: grow the schedule if episodes consistently exhaust every escalation step before succeeding; shrink it if episodes consistently resolve on the first or second step) and returns it. No fixed floor on the shrink direction (the user's explicit choice) — but clamp every computed step to a minimum of a few minutes purely to prevent a busy-loop from a degenerate/glitched history, and document in a comment that this clamp is crash-prevention, not the policy floor that was declined. Persist the recomputed schedule (in `throttle_history.json` alongside the raw records, or in `background_state.json` — implementer's call, document whichever) so a restart doesn't lose a schedule that took real data to earn.
- **Acceptance**: Fewer than 5 records → `maybe_recompute_schedule` returns `None`/the unchanged default (no-op). 5+ records where every episode succeeded on escalation step 0 → schedule shrinks (still respecting the crash-prevention minutes-level clamp, never zero). 5+ records where every episode needed the full 4-step escalation → schedule grows or stays capped at the top. A degenerate/corrupt history (e.g. a negative gap from clock skew) never produces a schedule step below the crash-prevention clamp.
- **Verify**: Tests built entirely on fabricated `throttle_history.json` fixtures (never real logged episodes) covering: below-threshold no-op, all-first-step-success shrinking, all-escalated growing/capping, and the degenerate-input clamp. Exact output numbers are not asserted precisely (the formula may be tuned after real data exists) — assert *direction* and *the clamp holding*, per the spec's testing strategy.

### Task 9 — `gui.py`: `background_state.json` persistence
- **Model**: Sonnet 5 — mirrors the existing `_load_settings`/`_save_settings` pattern one function pair over.
- **Do**: Add `_BACKGROUND_STATE_FILE = os.path.join(updater.data_dir(), 'background_state.json')` near the existing `_SETTINGS_FILE`. Add `_load_background_state()` (same defensive try/except-return-`{}`-on-any-failure shape as `_load_settings`) and `_save_background_state(state)` — but atomic (write to a `.tmp` sibling, `os.replace()` over the real path), unlike `_save_settings`'s plain `open(...).write()`. Define the schema as a plain dict (or `@dataclass` with a `to_dict`/`from_dict` pair): `phase` (`'downloading'|'library_tools'|'done'`), `resume_at` (unix timestamp or `None`), `throttle_count` (int, feeds Task 7's `next_resume_at`), `songs_folder`, `quality`, `replace`, `resync`, a `remaining_folders` list (so a resume doesn't re-derive "what's left" from scratch), and each Library Tool's dry-run preference at the moment background mode was started (per the spec's resolved decision — read once at toggle-on, not from a UI element that may not exist on resume). Also add `_clear_background_state()` for when a run reaches `'done'`.
- **Acceptance**: Save-then-load round-trips exactly. A missing, empty, or corrupt (invalid JSON) state file loads as `{}`/`None`-phase, never raises. A crash simulated mid-write (kill before `os.replace`) leaves the previous valid state file intact, not a half-written one.
- **Verify**: Tests covering round-trip, missing-file, corrupt-file, and the atomic-write guarantee (monkeypatch `os.replace` to raise, assert the original file is untouched).

### Task 10 — `gui.py`: extract Library-Tools dispatch to module level
- **Model**: Sonnet 5 — mechanical extraction of an already-working method, guarded by the dialog's own existing test suite.
- **Do**: Move `LibraryToolsDialog._run_tool_scan`'s body to a module-level function, e.g. `_run_library_tool(songs_folder, key, dry_run)`, taking `songs_folder` as an explicit parameter instead of reading `self._songs_folder`. Do the same for `_format_summary` (already a `@staticmethod`), e.g. `_format_tool_summary(key, counts, dry_run)`. Update `LibraryToolsDialog`'s methods to call the new module-level functions instead, preserving identical behavior. `_RUN_ALL_ORDER`/`_LIBRARY_TOOLS` are already module-level constants — no change needed there.
- **Acceptance**: `LibraryToolsDialog`'s own behavior is byte-identical — every existing test in `tests/test_library_tools_dialog.py` (including the "Run all tools" tests) passes unmodified. The new module-level functions are directly callable with just `(songs_folder, key, dry_run)` / `(key, counts, dry_run)`, no `LibraryToolsDialog` instance required.
- **Verify**: `pytest tests/test_library_tools_dialog.py -q` green with zero test changes. One new test calling `gui._run_library_tool(...)` directly (mocked underlying scan function) to prove it works standalone.

> **CHECKPOINT 1** — Two independent throttle-reduction fixes (client list, cookies) plus the full foundation (backoff math, adaptive recompute, state persistence, dialog-free tool dispatch) all in place. Nothing wired into the download loop or the GUI's Start/Stop flow yet. Run the full suite, review before orchestration.

---

## Phase 2 — Orchestration

### Task 11 — `gui.py`: background-mode controller
- **Model**: Opus 4.8 — the actual state machine (throttle → long backoff → resume → phase transition → Library Tools hand-off) touching `gui.py`, a file this repo's own fragility scoring already flags as high-coupling/high-churn (Station 2). Highest-reasoning tier for the same reason chart_rename.py's consolidation and Task 8's adaptive recompute got it.
- **Do**: Add a controller (a class or a tight set of methods on `App` — implementer's call, but keep it additive: new methods/state, not a restructure of `_dl_thread`/`_handle_msg`'s existing control flow) that:
  1. Runs the same per-song loop `_dl_thread` already runs, but on `run_song_with_backoff` returning `'stop'` (today's `'rate_limited'` trigger), instead of ending the run: increments `throttle_count`, computes `resume_at` via Task 7's `next_resume_at` (passing Task 8's recomputed schedule if one exists), persists state via Task 9's `_save_background_state`, records the episode's *start* via Task 8's `record_throttle_episode` bookkeeping (the record completes once a retry actually succeeds), logs the event (Task 14 hooks here), and waits via `self._stop_evt.wait(seconds)` (cancellable — a manual Stop must still work during a long backoff wait) before re-entering the loop for the remaining not-yet-done targets. On the retry that finally succeeds, finalizes the throttle-episode record (resolved_at, escalation_steps_used) and calls `maybe_recompute_schedule`.
  2. On true completion of the download phase (every target done/skipped/permanently-errored via a non-throttle path, no pending `resume_at`), transitions `phase` to `'library_tools'`, persists, logs, then runs each key in `_RUN_ALL_ORDER` via Task 10's `_run_library_tool(songs_folder, key, dry_run)`, reading each tool's dry-run value from the `background_state.json` snapshot Task 9 captured at toggle-on time (not from a live `LibraryToolsDialog`, which may not be open).
  3. On the Library Tools pass finishing, transitions `phase` to `'done'` and clears state via `_clear_background_state()`.
- **Acceptance**: A throttle mid-run does not end the background run — it's still running (or an equivalent background-mode flag reflects that) after the wait, and resumes the remaining targets. The download phase is never marked complete while a `resume_at` is still pending. The Library Tools pass runs exactly once per background run, in `_RUN_ALL_ORDER`, respecting the captured dry-run settings — never silently forced live; if no captured preference exists for a tool, default to dry-run (True), matching `test_dry_run_defaults_on_for_every_tool`. A manual Stop during a long-backoff wait cancels the wait and ends the background run cleanly (no orphaned timer).
- **Verify**: Tests driving the controller directly with a mocked `run_song_with_backoff` (fast, deterministic 'stop'/'ok'/'skipped' sequences) and a monkeypatched `self._stop_evt.wait` (returns immediately, asserting it was called with the *expected* duration rather than actually waiting). Cover: one throttle then success (episode gets recorded), throttle-count escalation across multiple consecutive throttles, Stop cancels a pending wait, and completion triggers the Library Tools pass exactly once with the right dry-run values.

### Task 12 — `gui.py`: "Run in background" GUI toggle
- **Model**: Sonnet 5 — UI wiring against an already-built controller, same shape as the existing Start/Stop button wiring.
- **Do**: Add a "Run in background" checkbox/button near the existing `_start_btn`/`_stop_btn` (gui.py ~line 1541-1560). Wire it so starting a download run with the toggle on goes through Task 11's controller instead of the plain one-shot `_dl_thread`, and captures each Library Tool's current dry-run checkbox state into `background_state.json` at that moment (per Task 11's Acceptance). Update `_update_buttons()` so Start/Stop/Library-Tools locking correctly reflects a background run in progress (reuse the existing `self._running`/`self._tool_running` mutual-exclusion pattern).
- **Acceptance**: With the toggle on, starting a run behaves identically to today for the parts that don't involve throttling, but a throttle no longer shows the "Rate limited, wait and try again" dead-end. With the toggle off, behavior is provably unchanged from today (regression).
- **Verify**: Real (non-mocked) `ctk` construction test confirming the toggle exists and toggles a flag the controller reads; a regression test confirming toggle-off preserves the exact existing `'rate_limited'` message-handler behavior.

> **CHECKPOINT 2** — The controller works end-to-end from the GUI for a single app session (throttle survival, adaptive recompute wiring, Library Tools hand-off). Restart survival not yet wired. Run the full suite, review before Phase 3.

---

## Phase 3 — Restart survival + logging

### Task 13 — `gui.py`: resume automatically on app launch
- **Model**: Sonnet 5 — read-persisted-state-and-continue, straightforward given Tasks 9 and 11 already exist.
- **Do**: At app startup (`App.__init__`/`_startup`, gui.py ~line 1627), check `_load_background_state()`. If `phase` is `'downloading'` or `'library_tools'` (not `'done'`/absent), auto-resume: rebuild the target song list (re-scan via the existing `_scan_library`, filtered the same way the persisted run's `replace`/`resync` settings originally selected them, minus whatever the persisted `remaining_folders`/progress says is already done — re-derive against a fresh scan rather than trusting a stale list blindly where the two disagree, since the library may have changed while the app was closed), and hand off to Task 11's controller — resuming the wait immediately if `resume_at` is still in the future, or continuing the download loop immediately if it's already past.
- **Acceptance**: Closing the app mid-download (no throttle pending) and relaunching resumes the remaining songs without re-downloading completed ones. Closing during a long-backoff wait and relaunching after `resume_at` has passed resumes immediately; relaunching before it's passed re-schedules the remaining wait (not a fresh full-length one). Relaunching with no persisted state (or `phase: 'done'`) starts up exactly as today.
- **Verify**: Tests seeding a `background_state.json` fixture (each of: mid-download, backoff-pending-future, backoff-pending-past, done/absent) and asserting the controller is invoked (or isn't) correctly, with `_scan_library`/the actual download call mocked out.

### Task 14 — Logging for phase/throttle/resume/adjustment events
- **Model**: Haiku 4.5 — mechanical, a handful of `log.info()` calls at points Tasks 8 and 11 already made explicit.
- **Do**: Using the existing `log = logging.getLogger('backstagehero')`, add log lines at: background mode started, each throttle event (with computed `resume_at`), each resume (distinguishing "same session" vs. "resumed after app restart"), download-phase completion, each Library Tools step starting/finishing during the auto-run, the final summary, background mode stopped (manual or completed), and — new — every time `maybe_recompute_schedule` actually changes the schedule, log the old schedule, the new one, and the data (record count, direction) that drove it.
- **Acceptance**: A `log.txt` from a full run (throttle → resume → schedule recompute → download completion → Library Tools pass → done) reads as a complete narrative of what happened, including *why* the schedule changed if it did.
- **Verify**: A test capturing log records (`caplog`, matching `tests/test_resolver_app.py`'s `caplog.at_level` convention) around a mocked controller run, asserting each expected event type appears with the relevant identifying detail (song name, resume_at, tool key, old/new schedule on a recompute).

> **CHECKPOINT 3 (final)** — Full `pytest tests/ -v` green, including every new test from Tasks 1-14. Whole-diff review (Phase 0 contained to the 5 files listed at Checkpoint 0; Phases 1-3 contained to `VideoDownload.py` + `gui.py` + new test files). Manual verification note: the real YouTube-throttle-duration assumption behind `LONG_BACKOFF_SECONDS`, and the DASH/1080p+ claim behind removing `YOUTUBE_CLIENTS`, can only be validated by the user's own runs against a real library — flag this plainly in the final report, don't claim either as tested.

---

## Risks & watch-items

- **Phase 0 duplicate-work risk**: the other session authoring `SPEC-dry-run-cache.md` is still active. Check before starting Phase 0 (see the provenance note at the top of this plan) — don't rediscover this the hard way like the `resolver_client.py` incident.
- **`gui.py` fragility**: already flagged Station 2 by this repo's own scoring (high coupling/churn). Task 11 is the highest-risk task in this plan for exactly that reason — Opus tier, additive-only changes, and Checkpoint 2 exists specifically to catch problems before restart-survival (Task 13) builds on top of it.
- **Unmeasured throttle duration, twice over**: `LONG_BACKOFF_SECONDS` starts as an estimate, and the adaptive recompute (Task 8) is itself working from a small, right-censored sample (spec Notes) — don't over-invest in precision in either place before real data exists. Expect to revisit both after the user's first real multi-day run.
- **Resume correctness on a changed library**: if the user adds/removes songs between an app close and a resume, the persisted `remaining_folders` could point at folders that no longer make sense. Task 13 re-derives against a fresh scan rather than trusting the persisted list blindly — test this edge case explicitly, not just the happy path.
- **Library Tools dry-run source of truth**: resolved by capturing each tool's dry-run checkbox into `background_state.json` at the moment Task 12's toggle turns on, rather than trying to read live UI state during an unattended/resumed run where no dialog may be open.
- **`YOUTUBE_CLIENTS` removal risk**: this is a real behavior change to every download, not just background-mode runs. Task 5's manual DASH/1080p+ verification is a hard gate, not a nice-to-have — do not consider Task 5 done on green tests alone.
- **Cookie support's blast radius**: Task 6 must not change `_base_opts()`'s output at all when the setting is off (the default). Test this explicitly as a regression, not just "the on-path works."
