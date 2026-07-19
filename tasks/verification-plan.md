# Pre-Commit Verification Plan

**Target commit**: the BackstageHero + Library Hygiene merge (SPEC.md), currently uncommitted
on `main` at upstream `5b69ae5`.

**Scope**: `gui.py` +242, `VideoDownload.py` +99, `README.md` +30, `requirements.txt` +4;
new modules `library_common.py`, `chart_rename.py`, `dedupe_report.py`, `video_repair.py`,
`metadata_enrichment.py`, `chorus_client.py`; `tests/` (13 files, 185 tests); `SPEC.md`,
`SPEC-static-art-video.md`, `tasks/`, `Launch BackstageHero.bat`.
Phase A adds `static_art.py` + `tests/test_static_art.py` on top of this.

**Baseline established 2026-07-18**: `pytest tests/ -q` -> **180 passed** in 3.45s on
`C:\Python314\python.exe` (3.14.4). All runtime deps import except `mpv` (OSError --
`libmpv-2.dll` absent; expected from source, `_load_mpv()` returns None and the ffplay
fallback covers it).

**Landed since baseline** (2026-07-18): sync provenance (`backstagehero_sync`) --
`video_start_time` is now written alongside a `measured` / `community` / `guess` / `manual`
marker, so a fallback guess is no longer byte-indistinguishable from a real measurement.
`DEFAULT_START_TIME` is unchanged, so in-game playback is unaffected. 5 new tests in
`tests/test_sync_provenance.py`; `tests/test_process_resync.py` updated to pin the marker.
**Suite now at 185 passed.**

---

## Phase A -- Static album-art video detection *(FIRST -- implement before verifying)*

**Spec**: `SPEC-static-art-video.md` (written 2026-07-18). Per the user's direction this is
implemented ahead of the verification phases, because it changes `process_download()` and
`gui.py` -- reviewing those files in Phase 2 before this lands would mean reviewing them twice.

**Why it belongs before the commit**: the `select_video()` duration floor stops *wrong*
videos but not *contentless* ones -- a static album-art upload of the correct song is exactly
the right duration and sails through the floor. This is the other half of the same gap.

| Step | Model | Why this model |
|---|---|---|
| A1. `static_art.py` detector core -- frame sampling, average-hash, strict/loose thresholds | **Opus 4.8** | The hash-distance threshold *is* the correctness surface; wrong = real videos deleted. Note the counterintuitive bit: lossy encoding means "static" frames are not byte-identical, so exact equality fails and a perceptual tolerance is mandatory. |
| A2. `convert_to_album_art()` + `process_download()` gate + `backstagehero_video` marker | **Opus 4.8, max effort** | Station 3 -- deletes files from a real library. Also owns the re-download-loop guard, which silently makes the whole feature useless if wrong. |
| A3. `tests/test_static_art.py` -- ffmpeg-generated fixtures, threshold boundaries, fail-safe cases | Sonnet 5 | Mechanical fixture plumbing against a fixed rubric. Escalate any failing boundary case to Opus. |
| A4. GUI tool in `LibraryToolsDialog` (dry-run default) | Sonnet 5 | Follows three existing sibling tools. |
| A5. `chart_rename.py` docstring fix ("No content verification is feasible for album art" is no longer true) + README | Haiku 4.5 | One right answer, mechanical. |

**Gate**: suite green (185 + new tests), and a dry-run scan over a **copy** of a real library
produces a zero-line filesystem diff. Only then proceed to Phase 0.

---

## Risk classification (drives the model assignments below)

| Area | Station | Why |
|---|---|---|
| `chart_rename.py`, `library_common.py`, `dedupe_report.py` | **3** | Renames and relocates files in a real ~5,130-song library. Irreversible against user data. |
| `VideoDownload.select_video` hard floor | **2** | Changes which video gets attached to a song; wrong = silent bad data. Self-reported as never empirically replayed. |
| `gui.py` `LibraryToolsDialog` | **2** | Puts the Station-3 tools one click away, on a worker thread. |
| Outbound calls (`resolver_client`, `chorus_client`) | **2** | Network egress on by default per SPEC. |
| Docs, tests, requirements | 1 | Reviewable, reversible. |

**Model assignment principle**: Haiku 4.5 for mechanical checks with a right answer.
Sonnet 5 for broad-but-bounded sweeps over a lot of code. **Opus 4.8 for anything where
being wrong destroys user data or the failure mode is adversarial/subtle** -- that is
Phases 2a, 2b, 2d, and 3.

---

## Phase 0 -- Commit hygiene *(blocking; do first)*

**Model: Haiku 4.5** -- purely mechanical, each item has a single correct answer, no
judgment required. Wasting Opus here buys nothing.

- [ ] **`HANDOFF.md` -- do not commit.** It is an auto-generated Captain's Log artifact
      from the **Playlist Sentiment** project (references `Playlist Sentiment/frontend/src`,
      `.fleet/reports/2026-07-02-1635-architecture-review.md`). It has nothing to do with
      this repo. Delete it, and add `HANDOFF.md` to `.gitignore` so the Stop hook stops
      re-dropping it here.
- [ ] **`launch_log.txt` -- do not commit.** Runtime diagnostic output, rewritten every
      launch. Add to `.gitignore`. (Its current contents are the *pre-fix* `ModuleNotFoundError`
      that motivated the hardcoded interpreter path in the `.bat` -- keep the finding, drop the file.)
- [ ] Confirm `.gitignore` already covers `tests/__pycache__/` (it does -- `__pycache__/`).
- [ ] Confirm `SPEC.md` + `tasks/` are *intentionally* committed (they document the merge;
      recommend yes, they are the provenance for this whole change).
- [ ] `git add -A` then `git status` -- eyeball the final file list before anything else runs.

**Gate**: nothing from another project, and no runtime artifacts, in the staged set.

---

## Phase 1 -- Automated baseline

**Model: Haiku 4.5** (execution) -> escalate to **Sonnet 5** only if something fails and
needs diagnosis. These are pass/fail commands, not analysis.

- [ ] `python -m pytest tests/ -q` -- expect 185 passed, plus whatever Phase A adds.
      *(Confirmed green at plan time; re-run after Phase 0's deletions to prove nothing
      depended on them.)*
- [ ] Import smoke every new module standalone:
      `python -c "import library_common, chart_rename, dedupe_report, video_repair, metadata_enrichment, chorus_client"`
- [ ] `python dedupe_report.py --help` -- the only new module with a CLI. Confirm it parses.
- [ ] Confirm the other four hygiene modules are **GUI-only** (no `__main__`) and that
      `README.md` does not document a CLI for them that doesn't exist. *(Grep at plan time
      found `__main__` in `dedupe_report.py` only -- SPEC.md's "Open Questions" flagged this
      entry-point choice as unresolved; verify README matches what shipped.)*
- [ ] Assert the test suite makes **no live network calls** (no real hits to
      `backstage.jimmyproton.co.uk` or Chorus Encore) -- grep tests for the mock/monkeypatch
      boundary on `resolver_client` and `chorus_client`.

**Gate**: green suite, clean imports, docs match reality.

---

## Phase 2 -- Deep code review, split by blast radius

Run 2a-2e **in parallel** as separate reviewers; they do not depend on each other.

### 2a. Destructive file operations -- **the highest-stakes review**
**Model: Opus 4.8, maximum reasoning effort. Agent: `code-reviewer`.**
*Why Opus:* this code moves and renames files inside a 5,130-song library the user cannot
easily reconstruct. A subtle wrong branch here is unrecoverable data damage, not a bug
report. This is the one review where cost is irrelevant next to being right.

Files: `library_common.py`, `chart_rename.py`, `dedupe_report.py`

Focus:
- [ ] **Re-derive the two claimed fixes from the code, independently of `todo.md`'s account.**
      (i) `move_to_review()` places review folders as a *sibling* of `home_folder`, never nested
      -- verify no remaining path where a review folder can land inside the scanned root.
      (ii) `apply_stem_renames()` / `apply_album_art_rename()` judge each stem role
      *independently* -- verify no aggregate-status shortcut survived the restructure.
- [ ] Partial-failure behavior: a rename fails halfway through a folder -- what state is the
      folder left in? Is there any rollback, or does it leave a half-renamed song Clone Hero
      can't load?
- [ ] Windows-specific: case-insensitive collisions (`Song.ogg` vs `song.ogg`), `MAX_PATH`
      /long paths, reserved names, Unicode in artist/title, files locked by another process.
- [ ] The `song`-role duration safety check (`MID_DURATION_TOLERANCE_MS` vs `song_length`) --
      is the tolerance right, and what happens when `song.ini` has no `song_length`?
- [ ] `dry_run=True` is genuinely side-effect-free on **every** path (this is the user's
      primary safety net in Phase 4).
- [ ] TOCTOU guards: `todo.md` admits the collision branches are unreachable by natural
      fixture and are tested by monkeypatching `Path.exists`. Confirm the guard logic is
      correct *as written*, since the tests can't prove it organically.

### 2b. Match correctness -- the least-verified change
**Model: Opus 4.8. Agent: `code-reviewer` or `debugger`.**
*Why Opus:* `todo.md` explicitly states this fix "wasn't (and can't easily be) replayed
against real data -- confidence rests on tests + code-reading." When empirical verification
is unavailable, the reasoning *is* the verification, so use the strongest reasoner.

Files: `VideoDownload.py` (`select_video`, `process_download`, `process_resync`)

- [ ] The `candidates[0]` -> `ordered[0]` change: confirm `ordered` is always populated and
      correctly sorted wherever the fallback reads it.
- [ ] The new hard floor returning `(None, None, DEFAULT_START_TIME, False, 0.0, None)` --
      trace **every** caller of `select_video` and confirm each handles the all-None return.
      `process_resync` is claimed safe via its existing `if matched` guard; verify that, don't
      assume it.
- [ ] Confirm a floored song is genuinely retried on a later run and not silently marked done.
- [ ] **DONE, now review it**: the Snow incident showed a `-3000` in `song.ini` is
      indistinguishable from a real computed offset. Resolved by writing a `backstagehero_sync`
      provenance marker rather than by dropping the offset -- `DEFAULT_START_TIME` is still the
      best available guess, and omitting it would have handed playback to Clone Hero's own
      default (never verified). Review the marker for: every `set_ini_values` call that writes
      `video_start_time` also writes the marker; a resync **upgrades** `guess` -> `measured`;
      a fallback-to-a-different-video **downgrades** `measured` -> `guess`; and `manual` is
      never silently overwritten by an automatic pass.

### 2c. GUI integration
**Model: Sonnet 5. Agent: `general-purpose`.**
*Why Sonnet:* +237 lines of conventional Tk dialog and threading code. Broad but idiomatic
-- pattern-matching against known threading/UI hazards, which Sonnet does well and cheaply.

File: `gui.py` (`LibraryToolsDialog`, `_run_tool`, `_worker`, `_finish`, `_close`)

- [ ] Worker-thread exceptions: does a raise inside `_worker` surface to the user, or die silently?
- [ ] Closing the dialog mid-run -- does `_finish` touch a destroyed widget? Is the thread joined?
- [ ] **Is `dry_run` the default in the UI?** For Station-3 tools it must be, with an explicit
      opt-in to apply.
- [ ] Any Tk widget touched from the worker thread instead of the main loop.
- [ ] Re-entrancy: can the user launch the same tool twice, or two tools at once, over one library?

### 2d. Security and privacy
**Model: Opus 4.8. Agent: `security-auditor`.**
*Why Opus:* SPEC.md makes network egress **on by default** a deliberate decision. Verifying
that what actually leaves the machine matches what the spec promises is exactly the kind of
claim-vs-implementation gap that rewards deep reasoning.

- [ ] What precisely does `resolver_client` send outbound, and does the "Share matches"
      toggle actually gate the reporting half? Confirm no library paths or personal data leak.
- [ ] `chorus_client` / `metadata_enrichment`: response handling, timeouts, and what happens
      on a malformed or hostile response.
- [ ] `subprocess` calls to `ffmpeg`/`ffprobe`/`fpcalc`: argument construction with
      attacker-influenced filenames (song titles are arbitrary strings from the library);
      confirm no `shell=True` and no unsanitized interpolation.
- [ ] Path traversal via `song.ini` fields or archive-sourced folder names.

### 2e. Test quality -- do the tests prove what they claim?
**Model: Sonnet 5. Agent: `test-engineer`.**
*Why Sonnet:* reading 12 test files and mapping assertions onto behaviors is a breadth task
with a clear rubric. Escalate any *specific* suspicious test to Opus rather than running the
whole sweep there.

- [ ] For each fix named in `todo.md`, find the test that would **fail** if the fix were
      reverted. If no such test exists, the fix is unverified regardless of the green suite.
- [ ] `tests/test_select_video.py` (4 tests): do they cover the floor firing, *not* firing when
      `chart_dur` is unavailable, and `process_download` skipping without calling `download_with_fallback`?
- [ ] Over-mocking check: any test that passes only because the thing under test was stubbed out.
- [ ] Coverage gaps on the destructive paths specifically -- partial failure, permission
      denied, disk full.

---

## Phase 2.5 -- Remediate Phase 2 findings *(blocking; before Phase 3)*

Five parallel Phase 2 reviews landed 2026-07-19. Two findings converged independently
(2b and 2e both found the `SYNC_MANUAL` gap from different angles -- code-reading vs.
test-coverage-mapping -- which is why it's ranked first). Everything in **Blocks** below
trips blocker rule 3 (Station-3-or-adjacent finding, no fix, no test in a green suite).
Fix in the order listed; re-run the full suite after each item, not just at the end, so a
regression is attributable to the item that caused it.

### Blocks the commit

| # | Finding | File(s) | Model | Why this model |
|---|---|---|---|---|
| 1 | `SYNC_MANUAL` written, never read -- an automatic resync sweep silently overwrites a user's hand-fixed offset | `VideoDownload.py` (`process_resync`, `process_download`, wherever `backstagehero_sync` is written) | **Opus 4.8, max effort** | Station 3-adjacent: destroys a user's manual correction with no warning. Confirmed by two independent reviewers -- highest-confidence finding in the pass. |
| 2 | 2a-1: nested library -- non-recursive `chart_rename` scan disagrees with `VideoDownload`'s recursive scan; a folder with no `.ini` (e.g. a pack subfolder) gets relocated whole | `chart_rename.py` (`:579`), `library_common.py`, `dedupe_report.py` | **Opus 4.8, max effort** | Proven to empty a `Songs/<Artist>/<Song>/` layout on first run. Real data loss, not a bug report. |
| 3 | 2a-2: non-cp1252 song name crashes the scan *after* the move has already happened; fires during dry-run too, so a truncated report reads as clean | `chart_rename.py` (`:588`), `gui.py` (`:19`, `open(os.devnull, 'w')`) | **Opus 4.8** | Silent truncation of a safety report is exactly the failure mode the dry-run gate exists to prevent. |
| 4 | 2a-3: no rollback -- one locked/in-use file mid-rename leaves the folder half-renamed, scan aborts | `chart_rename.py` (`apply_stem_renames`), `library_common.py` (`move_to_review`) | **Opus 4.8** | Ordinary trigger on Windows (AV scan, Explorer preview lock). Needs either a two-phase rename (stage, then commit) or a recorded-and-resumable partial state. |
| 5 | 2a-4: song-role duration guard fails open on missing/unparseable `song_length` or an ffprobe failure -- rename proceeds with the safety check silently skipped | `chart_rename.py` (duration guard) | **Sonnet 5**, escalate to Opus if the fix touches the rename decision itself | Mechanical once the intended behavior is named: mirror the `.mid` path's existing fail-safe convention (`:145`) -- missing signal means "cannot verify," not "assume fine." |
| 6 | 2b-2: resync's search-fallback path fingerprints a *new* candidate but never replaces `video.mp4`, then stamps the offset `measured` -- manufactures the exact false-`measured` signal Phase 4c is built to catch | `VideoDownload.py` (`process_resync`, `:743-745`) | **Opus 4.8, max effort** | Needs a decision, not just a patch (see below), and the failure mode is adversarial to Phase 4c's own verification method. |
| 7 | 2c: `_close()` doesn't check `_running_key` -- closing the dialog mid-run on a real (non-dry-run) tool orphans the worker thread while releasing the modal grab, and the main window's Start button isn't blocked from racing a second mutation path over the same library | `gui.py` (`LibraryToolsDialog._close`, `_run_tool`) | **Sonnet 5** | Conventional Tk fix (block close / disable Start) once the race is named; same station as the existing `App._running` guard it needs to extend. |

**Finding 6 needs a decision before it's a patch**: either (a) write `guess` instead of
`measured` whenever the fallback path re-searched rather than reused the on-disk video, or
(b) actually re-download the matched candidate so `measured` stays true to what's on disk.
**Recommendation: (a)** -- it's the smaller change, it doesn't add a download to a resync
pass, and it matches the marker's original intent (a fallback guess is not a measurement).
Flag this recommendation to the user before implementing in case they want (b) instead.

### Fix now (not blocking, but land before Phase 3)

| # | Finding | File(s) | Model |
|---|---|---|---|
| 8 | 2b-3: `is_plausible(None) == True` lets a duration-unknown wrong-song candidate outrank a duration-matched one at `ordered[0]` | `VideoDownload.py` (`is_plausible`, `:508`) | Sonnet 5 |
| 9 | 2d M-1: `chorus_client` checks truthiness, not shape -- a schema change throws `AttributeError` uncaught, aborting a whole-library run | `chorus_client.py` (`:76`) | Haiku 4.5 |
| 10 | 2d M-2: `response.json()` unbounded, unlike `resolver_client`'s 1 MiB cap | `chorus_client.py` (`:69`) | Haiku 4.5 |

### Test obligations (per 2e's own rule: a fix with no test that fails on revert is unverified)

- [ ] `test_sync_provenance.py`: manual marker survives an automatic resync pass (covers #1).
- [ ] New nested-library fixture (the "Rock Pack" shape from 2a): confirm no subtree outside
      a leaf song folder is ever relocated (covers #2).
- [ ] Unicode song-name fixture (`♥` or CJK title): scan completes, dry-run report is
      complete, not truncated (covers #3).
- [ ] Simulated locked-file-mid-rename: folder ends in a defined, documented state, not a
      silent partial rename (covers #4).
- [ ] Missing/unparseable `song_length` and an ffprobe failure: rename does *not* proceed
      (covers #5).
- [ ] `process_resync` fallback-search path: asserts marker is `guess`, not `measured`, when
      the on-disk video is unchanged (covers #6).
- [ ] Extend `verify_gui_tool.py`'s pattern (or a new `test_gui.py`) to close the dialog
      mid-run on a non-dry-run tool and assert the worker is blocked from a second concurrent
      mutation path (covers #7).
- [ ] `chorus_client` malformed-shape and oversized-response fixtures (covers #9, #10).

**Gate**: full suite green, all eight test obligations above pass, and each of #1-#7 has a
test that **fails if reverted** -- prove this by temporarily reverting the fix and confirming
red, per 2e's own standard. Only then proceed to Phase 3.

### Outcome (2026-07-19) -- all ten landed, suite 235 -> 264

Every fix was reverted individually and its tests confirmed red before being restored. Three
things are worth carrying into Phase 3 rather than leaving buried in the diff:

**Two review findings were partially rejected on the evidence.** Recording them because
Phase 3's job is to attack claims, and these are claims:

- **#5 (duration guard).** 2a called the fail-open behavior a bug in all three "can't check"
  cases, citing the `.mid` path as precedent. Two existing tests encoded it as deliberate,
  and the full fail-closed fix broke the marquee Kryptonite integration test. 2a missed an
  asymmetry: in `verify_chart_content_match`'s `.mid` branch the duration IS the only
  evidence (a `.mid` carries no name/artist text), whereas in `apply_stem_renames` the real
  protection is "exactly one candidate for this role" and duration is secondary. Final line:
  *no reference value* (missing/unparseable `song_length`) stays fail-open; *reference value
  present but ffprobe couldn't check it* now fails closed. A test pins the asymmetry so it
  isn't "fixed" by mistake later.
- **#9 (chorus_client).** 2d's mechanism was wrong -- the body is already inside
  `except Exception`, so `.get()` on a list is caught. The real escape is a `'data'` that is
  a truthy **string**: `results[0]` returned a single character, and the caller's `.get()` on
  it raised *outside* the try. Finding real, diagnosis corrected.

**#6 was decided by the user**, not by the plan's recommendation. Closer reading showed the
fallback path only runs when the video ON DISK failed to match, so writing a fresh
candidate's offset describes a file the user doesn't have. Chosen: leave the timing
unchanged and tell the user to re-download. (The plan had suggested writing `guess`.)

**Two bugs were found in the fixes/tests themselves, both by the revert discipline** -- worth
noting because a green suite would have hidden both:

- The first unicode test **passed without the fix**. Its fixture songs were all `confirmed_ok`,
  and that path never prints a folder name. Rewritten against `needs_review` folders, which
  do -- now reproduces a genuine `UnicodeEncodeError` on revert.
- #7's first implementation posted the unlock via `after()` inside a bare `except: pass`.
  Diagnosing a test failure showed `after()` raises `RuntimeError: main thread is not in main
  loop` from a worker thread when no mainloop is running -- so a failed schedule would have
  left the main window **locked forever, silently**. Restructured: the guard flag is a plain
  atomic write from the worker thread, and only the UI refresh goes through `after()`.
  (The test harness needed a real `mainloop()`, not `update()` polling -- the same trap
  Phase A hit, now documented in the helper.)

**Empirical check beyond the suite**: a real nested library on disk (`Songs/Rock Pack/<song>/`
plus `Songs/Nirvana/<song>/`, a cp1252-hostile unicode folder, and an unrecognisable folder)
was scanned dry then for real. Dry run produced a **zero-line filesystem diff** on a cp1252
console; the real run left both packs and the junk folder untouched, relocated only the
genuinely broken folder, and put the review folder as a **sibling** of the library. Counts
identical between dry and real runs.

**Still open, carried to Phase 3 / follow-up**: `dedupe_report`, `video_repair`,
`metadata_enrichment` and `static_art` still use the flat one-level `iterdir()` scan. They are
not data-loss risks the way `chart_rename` was (they don't relocate unrecognised folders), but
on a nested library they silently process **nothing**. `library_common.iter_song_folders()`
now exists for them; switching them over is a follow-up, not part of this phase.

---

## Phase 3 -- Adversarial pass on the claimed fixes

**Model: Opus 4.8. Agent: `devils-advocate` (or the `opponents-view` skill).**
*Why Opus:* the job is to attack confident, well-written prose claims in `todo.md` and find
where the code doesn't match the story. Steel-manning and then breaking an argument is the
highest-reasoning task in this plan.

- [ ] Feed the reviewer `tasks/todo.md`'s fix narratives **plus** the actual diff, and have it
      hunt for claim/implementation mismatches.
- [ ] Specifically attack: "176/176 passing" and "180/180 passing" are cited as evidence for
      the fixes. A green suite proves no regression, not that the fix is correct. Which claims
      rest only on the suite?
- [ ] The sibling review-folder fix "only applies going forward, hasn't been re-run against
      real data" -- what is the state of songs already relocated into the OLD nested location?
      Is there a migration, or are they silently stranded and still visible to Clone Hero?
- [ ] Attack **Phase A's own** detector once it lands, on the same terms: a threshold that
      deletes real videos is the worst possible outcome in this repo. Specifically probe the
      strict/loose boundary and the fail-safe paths -- does *every* error route really end in
      "do nothing", or is there a path where a partial failure still deletes?

---

## Phase 4 -- Empirical verification *(user-run; logs over screenshots)*

> Screenshots have been unreliable for us. **Every check below is verified by reading a log
> file or a diffed `song.ini`, not by looking at the screen.** The app writes a rotating log to
> `C:\Users\aaron\AppData\Local\BackstageHero\log.txt` (`VideoDownload._setup_logging()`).

**Model: Opus 4.8 interprets the returned logs** -- the user runs the steps, and the log
output is the evidence to reason over. Diagnosing a real-library failure from log text is
high-stakes inference, not pattern matching.

### 4a. Launcher works from a real double-click
Regression check on the `pythonw` stdout=None fix.
```
:: Double-click "Launch BackstageHero.bat" from Explorer -- NOT from a terminal.
:: (A nested shell invocation inherits valid stdio and will not reproduce the bug.)
:: Then, in PowerShell:
Get-Content "C:\Users\aaron\Claude and Projects\Projects\BackstageHero_LibraryHygiene\launch_log.txt"
```
**Expect**: a `==== launch at ... ====` header and **no traceback**, no `exited with code 1`
until you close the window yourself.
**Also capture**: `Get-Content "$env:LOCALAPPDATA\BackstageHero\log.txt" -Tail 40`

### 4b. Hygiene tools -- DRY RUN FIRST, against a copy
> Do **not** point these at `M:\_Organized` until the dry run has been read and approved.
> Use a copied subset (as with the Kryptonite folder) or a small test library.

- [ ] `python dedupe_report.py --library-path "<COPY>" --dry-run` -> capture full stdout to a file.
- [ ] Chart-rename has **no CLI** -- run it from the GUI's Library Tools dialog with dry-run
      enabled, then read `%LOCALAPPDATA%\BackstageHero\log.txt`.
- [ ] **Before/after `git`-style diff of the folder tree**: capture
      `Get-ChildItem -Recurse "<COPY>" | Select-Object FullName` before and after, and diff.
      A dry run must produce a **zero-line diff** -- this is the empirical proof of 2a's
      dry-run audit.
- [ ] Only then, apply for real on the copy and re-diff. Confirm review folders land as a
      **sibling** (`<name>_needs_review`), never nested inside the scanned root.

### 4c. Offset verification -- the in-game playtest *(Checkpoint 3, still unchecked)*
This has never been trustworthy without the game, same discipline as the predecessor project.

- [ ] Pick **at least 3** songs the app auto-synced this run, including one that was
      fingerprint-confirmed and one that fell back.
- [ ] Read the written offsets without opening the game:
      ```powershell
      Get-ChildItem -Recurse -Filter song.ini "<LIBRARY>" |
        Select-String -Pattern "video_start_time|song_length|name=" |
        Out-File offsets_check.txt
      ```
- [ ] **Flag every `backstagehero_sync = guess`** -- those songs were never actually matched
      (the Snow case); they are running on `DEFAULT_START_TIME`, not a measurement. Grep the
      marker, *not* the `-3000` value: a real measurement can legitimately land on `-3000`,
      which is precisely why the marker exists.
      ```powershell
      Select-String -Path (Get-ChildItem -Recurse -Filter song.ini "<LIBRARY>").FullName `
        -Pattern "backstagehero_sync\s*=\s*guess" | Out-File guesses.txt
      ```
      Expect songs listed here to be the *only* ones out of sync in 4c. If a song marked
      `measured` is visibly out of sync in-game, that is a genuine audiosync bug and the most
      valuable finding this whole plan can produce -- report it with the song name.
- [ ] Load each song in Clone Hero and confirm the video is genuinely in sync. Report
      *perceived* drift direction and rough magnitude for any that are off -- sign errors are
      the classic failure here, and `tests/test_audiosync_sign.py` only covers the synthetic case.

### 4d. Real-library smoke on the two unreplayed fixes
- [ ] Re-run chart-rename against the **real Kryptonite folder** still sitting in the OLD
      nested `_needs_review` location -- confirm the sibling-folder fix handles pre-existing
      nested state, or confirm it's stranded (per Phase 3).
- [ ] Run one full download cycle on 2-3 songs with no video and confirm from `log.txt` that
      the duration floor either attaches a plausible video or attaches **nothing** -- never an
      unrelated one. (This is the Green Day/Kryptonite failure mode.)

---

## Phase 5 -- Synthesis and go/no-go

**Model: Opus 4.8** (main thread) -- reconciling five parallel reviews plus real-world log
evidence into one commit decision is the integration step, and it owns the final call.

- [ ] Collect Phase 2a-2e + Phase 3 findings; de-duplicate; rank by blast radius.
- [ ] Triage each into: **blocks the commit** / **fix now** / **file as follow-up**.
- [ ] Re-run `pytest tests/ -q` after any fix lands.
- [ ] Update `tasks/todo.md` -- tick Checkpoint 3 only if 4c actually happened in-game.
- [ ] Write the commit message covering the merge honestly, including what remains unverified.

### Explicit commit blockers
1. `HANDOFF.md` or `launch_log.txt` still staged.
2. Any dry run that produced a non-zero filesystem diff.
3. Any Station-3 finding from 2a with no fix and no test.
4. Checkpoint 3 (4c) not performed -- **or**, if you choose to commit before playtesting,
   the commit message must say the in-game sync verification is outstanding.
