# Spec: Single-Instance Enforcement + Launcher Retry Hardening

**Naming note**: follows the established `SPEC-<feature>.md` convention;
`SPEC.md` is this project's original whole-app spec.

**Scope note**: Part A (single-instance) is what was asked for. Part B
(launcher retry wait) is an addition, proposed on evidence that landed while
Part A was being written — the 2026-08-09 20:02 launch failure. It is separable;
cut it and Part A still stands on its own.

## Objective

**User**: the same solo hobbyist as the other specs here — running the GUI on
Windows against a ~7,900-folder library, unattended, in background mode, and
launching it by double-clicking a desktop shortcut.

Two ways a launch currently goes wrong that the app does not defend against:
double-clicking while an instance is already running, and a transient import
failure that outlives the launcher's one retry.

### The evidence

**Nothing prevents a second instance.** `gui.run()` (`gui.py:3696`) constructs
`App()` unconditionally. `_maybe_resume_background` (`gui.py:3350`) then fires
automatically at startup and, if `background_state.json` says `phase:
downloading`, launches the download loop without asking. Two instances would
therefore both auto-resume the same run and both write
`background_state.json`, `log.txt` and the same song folders. `_save_background_state`
is atomic per write (`gui.py:184`), so the file never tears — but atomicity does
not help when two processes each hold a different idea of `remaining_folders`
and take turns overwriting each other. The last writer wins and the other
instance's progress is silently discarded.

This is not hypothetical for this user: a run can span days, the window spends
most of its time doing nothing visible (2026-08-09 spent ~7.5 of 9.5 hours
asleep in throttle backoff), and a desktop shortcut now exists to make launching
one click. "Is it already running?" is a genuinely easy mistake to make.

**A transient import failure outlived the retry.** At 20:02:26 on 2026-08-09,
seconds after the long-running instance was closed, `Launch BackstageHero.bat`
failed twice and popped its notepad diagnostic:

```
ImportError: yt_dlp imported but has no YoutubeDL -- got it from None.
```

Attempts were 20:02:28 and 20:02:33 — the full 5-second wait added on
2026-08-08 for exactly this. Both failed. Re-probed at 20:15 from the same
`pythonw.exe`, `find_spec('yt_dlp').origin` resolved to the real
`__init__.py` and `hasattr(yt_dlp, 'YoutubeDL')` was `True`. Nothing was
reinstalled in between.

That is the fourth occurrence of this transient (2026-07-19, 08-05, 08-08,
08-09) and the second in a documented restart-shaped context. The pattern the
dates now support: it clusters around a *previous instance exiting*, which is
consistent with `__pycache__` rewriting or an antivirus scan holding files
briefly after process exit. 5 seconds was not enough. The guard and the
launcher both behaved correctly — the wait is simply mistuned.

### What success looks like

1. Double-clicking the shortcut while an instance is running surfaces the
   existing window instead of starting a second one, and never touches
   `background_state.json`.
2. A second launch attempt never looks like a crash to the launcher and never
   pops the notepad diagnostic.
3. A launch that hits the transient import failure has a realistic chance of
   clearing on retry.
4. A normal first launch is completely unchanged.

## Tech Stack

Unchanged. Python 3.14, stdlib only — `ctypes` for the Win32 calls, which
`gui.run()` already uses for `SetCurrentProcessExplicitAppUserModelID`
(`gui.py:3700-3704`). `pytest` for tests. No new dependencies, no new settings
keys, no new GUI controls.

## Commands

```
Test (targeted):  C:\Python314\python.exe -m pytest tests/test_single_instance.py -v
Test (full):      C:\Python314\python.exe -m pytest tests/ -q
Compile check:    C:\Python314\python.exe -m py_compile gui.py
Run from source:  C:\Python314\pythonw.exe gui.py
Launcher:         "Launch BackstageHero.bat"
```

## Project Structure

```
gui.py                        → single-instance guard in run() (Part A)
Launch BackstageHero.bat      → retry ladder (Part B)
tests/
  test_single_instance.py     → new: Part A
```

## Code Style

Match the surrounding code: comments that cite the incident that motivated them,
`ctypes` calls wrapped so a failure degrades rather than crashes (the existing
`SetCurrentProcessExplicitAppUserModelID` block is the model — `try/except
Exception: pass` around a best-effort Win32 call).

## Design

### Part A — single-instance enforcement

**Mechanism: a named Win32 mutex**, not a PID lock file. `CreateMutexW` +
`GetLastError() == ERROR_ALREADY_EXISTS (183)`. The decisive property is that
Windows releases the mutex when the process dies **by any means** — crash, kill,
power loss. A lock file has to encode staleness detection (is this PID alive? is
it *our* app or a recycled PID?) and gets it wrong in exactly the scenario that
matters: a hard-killed app leaving a lock nobody can clear, turning a
convenience feature into a reason the app won't start at all. That trade is
unacceptable for the one app this user leaves running for days.

**Where.** In `run()` (`gui.py:3696`), before `App()` is constructed, next to
the existing AppUserModelID call. It must precede `App()` because `App.__init__`
schedules `_maybe_resume_background`, and a second instance must never reach the
code that auto-resumes a background run.

**Handle lifetime.** The returned handle is stored in a module-level global.
If it were a local it would be garbage-collected, the mutex released, and the
guard would stop working partway through the process's life.

**What the second instance does**, in order:

1. Best-effort: find the existing window by title (`FindWindowW`) and bring it
   forward with `ShowWindow` + `SetForegroundWindow`. The title comes from
   `_window_title()`, the same helper `App.__init__` sets it with, so the
   lookup string and the real title cannot drift — see Open Question 1.
2. If that fails, show a `MessageBoxW` saying an instance is already running.
   A double-click must never appear to do nothing — silent failure is the
   defect the launcher's whole diagnostic apparatus exists to prevent.
3. **Exit with code 0.**

**Point 3 is the one that will break if it is not deliberate.** `Launch
BackstageHero.bat` reads `%ERRORLEVEL%` after `pythonw.exe gui.py` and, on
non-zero, waits and retries, then opens the failure log in notepad
(`Launch BackstageHero.bat:49-82`). A second instance exiting non-zero would be
retried, blocked again, and would then tell the user their app "could not start
twice in a row" — the exact false alarm the launcher was built to make
meaningful. The guard must exit 0.

**Degradation.** Any failure of the ctypes calls themselves (unexpected
platform, hardened environment) is caught and the app starts normally. A guard
that can prevent the app from launching is worse than no guard.

**Out of scope:** the CLI path in `VideoDownload.py`'s `__main__`, which is a
different entry point with a separate open question about whether it is still
live code (`SPEC-fail-fast-preconditions.md`, Open Question 3).

### Part B — launcher retry ladder

Replace the launcher's single 5-second retry with three spaced attempts — 20s,
40s, 60s (≈ 3.5 min total) — each stamping its own exit code and timestamp into
`launch_log.txt`, and each short-circuiting to `:done` on success.

Two purposes, one change. It tries harder at a transient that 5s demonstrably
did not outlast; and because every rung is timestamped, whichever attempt
succeeds bounds how long the condition actually persisted — the number nobody
has ever recorded, because the launcher always gave up after two tries. See
Open Question 2.

Cost is bounded and paid only on a launch that has already failed. A healthy
launch exits at the first attempt and never reaches any of this. Both existing
mechanisms are unchanged: `timeout` with the `ping -n` fallback for redirected
stdin — and the two must move together on every rung, since `ping` waits n-1
seconds.

This is explicitly **not** an attempt to fix the underlying transient, whose
cause remains unidentified and unreproducible on demand.

## Testing Strategy

`pytest`, new `tests/test_single_instance.py`, offline and Win32-free — the
tests must **never** acquire a real named mutex, or the pytest process itself
holds it and poisons every later test and every concurrent run.

The guard is therefore written as an extractable predicate that takes its Win32
surface as injectable seams (`monkeypatch` over the module-level `ctypes`
accessor), following `tests/test_cookie_support.py`'s stand-in-object pattern.

Required cases:
- First instance: mutex created, no existing-instance branch, `run()` proceeds
  to construct `App`.
- Second instance: `ERROR_ALREADY_EXISTS` → focus attempted → **exit code 0**,
  and `App` is never constructed.
- Second instance with focus failing → `MessageBoxW` shown → still exit 0.
- A raising ctypes call → app starts normally (degradation), no exit.
- The handle is retained in module state, not dropped.
- Regression: `SetCurrentProcessExplicitAppUserModelID` still runs, and still
  runs before the window is created.

Part B has no runtime test surface, but the ladder's numbers are pinned by
source-scan tests (each wait paired with its `ping -n` fallback, every rung
stamped, four short-circuits) so a future edit cannot silently desynchronise
them. Behavioural verification is the manual checklist below.

The full suite (735 tests at time of writing, plus whatever
`SPEC-cookie-chain-and-pacing.md` adds) must stay green.

## Boundaries

**Always**
- Exit 0 from the second instance. Re-read `Launch BackstageHero.bat:49-82`
  before changing anything about the exit path.
- Wrap every Win32 call so a failure starts the app rather than blocking it.
- Keep the mutex handle referenced for the process lifetime.
- Run the guard before `App()` is constructed.

**Ask first**
- Any change to what `_maybe_resume_background` does on launch.
- Making single-instance behavior configurable or bypassable.
- Any additional change to `Launch BackstageHero.bat` beyond the wait duration.

**Never**
- Do not use a PID lock file. A hard kill would leave a stale lock that stops
  the app from starting — strictly worse than the problem being solved.
- Do not let the second instance write `background_state.json`, `settings.json`,
  `log.txt`, or `launch_log.txt`.
- Do not silently exit. A double-click that appears to do nothing is the
  failure mode this project already has a launcher-plus-notepad apparatus to
  avoid.
- Do not attempt to hand off arguments or state to the running instance. Focus
  it and stop; anything more is IPC and a different spec.

## Success Criteria

1. With the app running, double-clicking the desktop shortcut brings the
   existing window forward and starts no second process.
2. That second attempt leaves `launch_log.txt` reporting exit code 0 and does
   not open notepad.
3. `background_state.json`'s modification time is unchanged by a blocked second
   launch.
4. Killing the app via Task Manager and relaunching works immediately — no
   stale-lock recovery step.
5. A first launch with nothing running behaves exactly as today.
6. `pytest tests/ -q` green.

## Open Questions — all resolved 2026-08-10

1. **Should the second instance focus, or just message?** **Focus — and the
   brittleness was removed rather than accepted.** The concern was that
   `FindWindowW` matches an exact title built from a duplicated f-string
   (`f'BackstageHero  v{__version__}'`, two spaces), so editing the title in
   `App.__init__` would silently stop the guard ever finding the window —
   invisibly, because the message-box fallback still blocks the launch and
   still looks correct. Fixed by extracting `_window_title()` and having both
   `App.__init__` and the guard call it, so the two cannot drift.

   The embedded `__version__` turned out to be a non-issue: both instances are
   the same code at the same version, so they can only disagree if two
   different builds are launched side by side.

2. **Is 20 seconds the right retry wait?** **Unknown — and now made
   answerable, which is the real fix.** No occurrence has ever bounded how
   long the transient persists, because the launcher gave up after two
   attempts; every data point bounds it only from below. 2026-08-09 is the one
   case with an upper bound too, and it is uselessly wide: between 5 seconds
   and 13 minutes.

   Rather than guess a single better number, the retry is now a **ladder** —
   attempts after 20s, 40s and 60s (≈ 3.5 min total). Each rung stamps its own
   exit code and timestamp into `launch_log.txt`, so whichever attempt
   succeeds bounds the recovery window to within one interval. The log becomes
   the instrument: no separate probe to maintain, nothing extra runs on a
   healthy launch, and every second is paid only after a launch has already
   failed. The next occurrence answers this question as a side effect of the
   app simply starting.

3. **Should the mutex be per-user or global?** **Per-user (unprefixed /
   `Local\`).** `Global\` would also block a second instance running as a
   *different* Windows user — the wrong policy, since two users on one machine
   have separate `settings.json`, separate `background_state.json` and
   separate libraries, so they are not the collision this guard exists to
   prevent. `Global\` can also require `SeCreateGlobalPrivilege`, which would
   make the guard throw and fall back to starting normally in exactly the
   hardened environments where that is least expected. Documented at the
   constant.
