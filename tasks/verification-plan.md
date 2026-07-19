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

### Phase 3 outcome (2026-07-19)

Two independent `devils-advocate` agents ran. **Both were provisioned without an execution
tool**, so everything they produced is a static trace, not a measurement -- they each
disclosed this honestly and refused to fabricate numbers, which is the right call. Every
finding below was therefore **re-tested by execution in the main session** before being
recorded. That re-testing changed the verdict on several.

| # | Finding | Status after execution |
|---|---|---|
| **P3-H1** | Songs in an OLD nested `Songs/_needs_review/` are invisible to the repair scan (`iter_song_folders` skips `_`), but fully live to `gui._scan_library`'s `**/song.ini` glob, to Clone Hero, and to auto-download/resync | **CONFIRMED by execution.** Affects the user's real Kryptonite folder. No migration exists; re-running the tool will never find it. |
| **P3-H2** | `static_art` converts (deletes) videos whose motion is real but thin/small; margin erodes with resolution | **CONFIRMED by measurement.** See table below. |
| **P3-M1** | `dedupe_report` fuzzy-matches two similarly-named PACK folders as duplicate songs and could relocate a whole pack | **CONFIRMED by execution** (found in main session, not by an agent). Corrects the Phase 2.5 commit claim that the un-migrated tools "do not relocate". |
| **P3-M2** | `make_console_encoding_safe()` was wired into `chart_rename` only; three sibling tools share the crash surface | **PARTIALLY confirmed.** Structural gap real. Only `metadata_enrichment` demonstrably crashed on the fixture; `video_repair`/`dedupe_report` did not reach a printing path. Agent overstated demonstrated blast radius. |
| **P3-M3** | The `pythonw` `stdout=None` guard has zero test coverage; "180/180 passing" is cited as if it corroborated it | **CONFIRMED by inspection.** The branch is structurally unreachable under pytest. Suite-count-only evidence. |
| **P3-M4** | The `SYNC_MANUAL` code comment tells the reader to "clear the offset in the sync editor" -- no such control exists | **CONFIRMED.** My comment, written in Phase 2.5. The real escape hatch is a forced re-download. |
| **P3-L1** | `convert_to_album_art` writes the marker BEFORE promoting `album.png`, and the `os.replace` promote is the one unguarded file op | Plausible, code-evident, **not yet execution-tested**. Video is not lost either way. |
| **P3-L2** | `_apply_renames_atomically` reports `len(done)` rather than the count that actually failed to unwind | **NOT confirmed** -- my test fixture was broken (`drums_2` is itself a valid stem role, so the failure never triggered). Code-evident but unproven. |

#### P3-H2 measured -- the number the whole feature rests on

`_luminance_grid` resamples the **native-resolution** frame to a fixed 32x32, so a
fixed-pixel-size element covers proportionally less of one cell as resolution rises. Real
ffmpeg fixtures, real `probe_static_video()`, thresholds `hash<=2` **and** `cell<=24`:

| content | 640x640 | 1280x720 | 1920x1080 | verdict |
|---|---|---|---|---|
| baseline held still | 1 | 1 | 1 | static (correct -- convert) |
| CRF 40 heavy compression still | 6 | 3 | 2 | static (correct -- convert) |
| scrolling lyric line, 4% tall | 228 | 228 | 230 | video (**safe**) |
| scrolling lyric line, 2% tall | 129 | 131 | 132 | near_static (**safe**) |
| corner equalizer bar | 80 | 84 | 84 | near_static (safe) |
| moving dot, proportional to frame | 90 | 123 | 122 | near_static (safe) |
| moving dot, fixed 16px | **133** | **53** | **31** | near_static -- but 4x erosion |
| thin progress bar crawling | **19** | **17** | **17** | **static -- DELETED** |

Threshold-crossing sweep at 1920x1080: 32px dot = 88, 24px = 63, 16px = 25 (**one point**
above the threshold), 12px = 19 **deleted**, 8px = 8 **deleted**.

**What this means, stated carefully.** The catastrophic reading is wrong: lyric videos,
visualizers, equalizers and anything with proportionate motion score 80-230, an order of
magnitude clear of the threshold. The feature does not eat real music videos. But two claims
are falsified as written:

1. README: *"Anything with real motion -- a slow zoom, a visualizer, a locked-off
   performance -- is reported but never touched."* A thin progress bar is real motion and is
   touched, at every resolution tested.
2. `static_art.py`'s own comment: *"roughly 2x headroom over the worst legitimate still and
   5x clearance under the smallest real motion."* At 1080p the smallest real motion tested
   clears by **1.04x**, not 5x. The comment's numbers were measured at 640x640 only.

The practical harm is bounded -- what slips through is content whose motion is genuinely
trivial (a progress bar, a small logo bug), which arguably *should* convert. The defect is
that the guarantee is stated far more strongly than the implementation delivers, and that
the margin silently depends on a variable (resolution) nothing in the tests controls for.

**Recommended, in order:** (a) correct the README and the code comment to state the real
behaviour and the real measured margins; (b) resolution-normalise -- downscale the source
frame to a fixed size *before* building the 32x32 grid, so cell size stops depending on
input resolution; (c) pin the measured `cell` values for the CRF40 and small-motion fixtures
in tests so erosion fails CI instead of rotting silently in a comment.

### Phase 3 remediation (2026-07-19) -- all findings fixed, suite 264 -> 296

| # | Fix |
|---|---|
| P3-H1 | `library_common.migrate_legacy_review_folders()` + a sixth Library Tools entry, **Move old review folders out of the library**. Moves each song individually so an existing sibling folder is merged rather than clobbered; a name that already exists on the far side is reported and left in place; every move is written to the same JSONL manifest the other tools use. Only the two literal legacy names are touched -- a user's own `_`-prefixed folder is left alone. |
| P3-H2 | `_normalise()` scales every frame to a fixed 640px long edge **before** either measure, so a score describes content and not download quality. `STATIC_MAX_CELL_DELTA` re-derived from measurement: **24 -> 14**, between the worst legitimate still (10) and the smallest real motion (17). The progress bar now correctly reports `near_static` at every resolution instead of being deleted. Both edges pinned by tests that assert measured values, not just categorical verdicts. README and the code comment now state the real behaviour, including the honest limit (motion under ~1% of frame can still convert). |
| P3-M1 | All four remaining tools switched to `iter_song_folders()`. `dedupe_report` no longer groups pack folders; `metadata_enrichment` no longer emits a spurious error per pack; `video_repair` and `static_art` now actually process nested libraries instead of silently finding nothing. |
| P3-M2 | `make_console_encoding_safe()` wired into all five scans, with a parametrised cp1252 test covering every one. |
| P3-M3 | Guard extracted to `library_common.ensure_stdio_not_none()`, called from both entry points, now covered by three tests -- including one proving the replacement stream itself survives non-cp1252 text, which the old `open(os.devnull, 'w')` did not. |
| P3-M4 | The misleading comment now describes the real escape hatch (force a re-download) and states plainly that no "clear the marker" control exists. |
| P3-L1 | `convert_to_album_art` promotes the art **before** committing the marker, with the `os.replace` guarded; a failure there now lands in the same clean state as a failed extraction, and a marker failure removes only art this call created. |
| P3-L2 | The rollback message reports the files **actually** still displaced, by name, instead of `len(done)`. |

Two of these were caught only because the fix broke an existing test, and in both cases the
**test was right and the first fix was wrong**: `looks_like_song_folder()` did not count a
folder holding only a video file (which is exactly what `video_repair` exists for), and the
reordered promote initially left a stray `album.png` behind on a marker failure.

**Verified end-to-end**, not just by unit test: a real library with a nested pack plus a song
stranded in an old nested `_needs_review`, driven through the actual `LibraryToolsDialog`
worker. Dry run produced a zero-line filesystem diff; the real run moved only the stranded
song, wrote a manifest, left the pack untouched, removed the song from the app's own song
list, and let `chart_rename` reach both nested songs afterwards. A second run reports
"nothing to migrate".

---

## Phase 4 -- Empirical verification *(user-run; logs over screenshots)*

> Screenshots have been unreliable for us. **Every check below is verified by reading a log
> file or a diffed `song.ini`, not by looking at the screen.** The app writes a rotating log to
> `C:\Users\aaron\AppData\Local\BackstageHero\log.txt` (`VideoDownload._setup_logging()`).

**Model: Opus 4.8 interprets the returned logs** -- the user runs the steps, and the log
output is the evidence to reason over. Diagnosing a real-library failure from log text is
high-stakes inference, not pattern matching.

> **Read this before starting -- updated 2026-07-19.** Two things about the code under test
> changed since this phase was written, and one of them would have invalidated the whole
> session:
>
> 1. **All the work is on `claude/backstage-hero-library-hygiene-1d83f9`. `main` is still at
>    upstream `5b69ae5` and contains none of it.** Run Phase 4 from the worktree, or merge
>    first -- but do not assume double-clicking something in the main folder is testing this.
> 2. **`Launch BackstageHero.bat` used to `cd` to a hardcoded absolute path** (the main
>    checkout) before running `gui.py`, so the copy inside the worktree launched *main's*
>    code. Now fixed to `cd /d "%~dp0"`, i.e. whatever folder the launcher itself is in.
>    **4a is only meaningful with that fix in place** -- otherwise it re-tests upstream.
> 3. Since this phase was written the tool count went four -> six, `static_art`'s threshold
>    was re-derived (24 -> 14) and has never run against real videos, and the library now
>    exports a CSV that makes most of 4c's PowerShell obsolete.

### 4-zero. Migrate the stranded review folders *(do this FIRST)*
Phase 3 confirmed by execution what 4d was written to find out: songs in the old nested
`Songs/_needs_review/` are invisible to every repair scan but fully live to the app's song
list, auto-download and Clone Hero. 4d's premise is therefore already answered -- the folder
is stranded, and there is now a tool for it.

- [ ] Library Tools -> **Move old review folders out of the library**, **dry run first**.
      Read what it says it would move. Then run it for real.
- [ ] Confirm `Songs_needs_review/` now sits **beside** the library, and that
      `Songs/_needs_review/` is gone.
- [ ] Confirm the manifest exists: `Songs_needs_review_manifest.jsonl`.

### 4a. Launcher works from a real double-click
Regression check on the `pythonw` stdout=None fix -- which was **rewritten since this plan
was written** (it now lives in `library_common.ensure_stdio_not_none()`, called from both
entry points), so this is a check on new code, not a re-run of an old pass.
```
:: Double-click the "Launch BackstageHero.bat" INSIDE THE WORKTREE, from Explorer --
:: NOT from a terminal. (A nested shell invocation inherits valid stdio and will not
:: reproduce the bug.) Then, in PowerShell:
Get-Content "<WORKTREE>\launch_log.txt"
```
**Expect**: a `==== launch at ... ====` header and **no traceback**, no `exited with code 1`
until you close the window yourself.
**Also capture**: `Get-Content "$env:LOCALAPPDATA\BackstageHero\log.txt" -Tail 40`

### 4b. Hygiene tools -- DRY RUN FIRST, against a copy
> Do **not** point these at `M:\_Organized` until the dry run has been read and approved.
> Use a copied subset (as with the Kryptonite folder) or a small test library.

- [ ] Capture the tree **before**:
      ```powershell
      Get-ChildItem -Recurse "<COPY>" | Select-Object -Expand FullName | Sort-Object > before.txt
      ```
- [ ] `python dedupe_report.py --library-path "<COPY>" --dry-run` -> capture full stdout to a file.
- [ ] Run each of the **six** GUI tools with **dry run left ON** (it is the default):
      Repair videos, Fix chart names, Enrich metadata, Find duplicates,
      **Find static album-art videos**, Move old review folders.
      Then read `%LOCALAPPDATA%\BackstageHero\log.txt`.
- [ ] Re-capture the tree and diff. A dry run must produce a **zero-line diff**:
      ```powershell
      Get-ChildItem -Recurse "<COPY>" | Select-Object -Expand FullName | Sort-Object > after.txt
      Compare-Object (Get-Content before.txt) (Get-Content after.txt)
      ```
      Expect **no output at all** except `backstagehero_library.csv`, which the app writes on
      every scan by design. Anything else appearing here is a blocker.
- [ ] **Pay particular attention to "Find static album-art videos".** Its threshold was
      re-derived from measurement in Phase 3 (24 -> 14) and has **never run against real
      YouTube downloads** -- only against synthetic ffmpeg fixtures. Read its dry-run list
      carefully: every song it proposes to convert should be one you agree is just a still
      cover. Anything on that list with real motion is the single most important finding this
      phase can produce, and it means **do not run it for real**.
- [ ] Only then, apply for real on the copy and re-diff. Confirm review folders land as a
      **sibling** (`<name>_needs_review`), never nested inside the scanned root.

### 4c. Offset verification -- the in-game playtest *(Checkpoint 3, still unchecked)*
This has never been trustworthy without the game, same discipline as the predecessor project.

**The PowerShell that used to live here is obsolete.** The app now writes
`backstagehero_library.csv` into the Songs folder on every scan, with an **Offset source**
column -- exactly the data those greps were reconstructing. Open it in Excel and sort on
that column instead.

- [ ] Open `<LIBRARY>\backstagehero_library.csv`, sort by **Offset source**.
      - `measured` -- audiosync fingerprint-matched this exact video.
      - `community` -- offset came from the resolver pool.
      - `guess`  -- **never matched at all**; running on `DEFAULT_START_TIME` (the Snow case).
      - `manual` -- you set it by hand; the resync sweep now leaves these alone.
- [ ] Pick **at least 3** songs to play, deliberately mixed: one `measured`, one `guess`, and
      one `manual` if you have one.
- [ ] Load each in Clone Hero and confirm the video is genuinely in sync. Report *perceived*
      drift direction and rough magnitude for any that are off -- sign errors are the classic
      failure here, and `tests/test_audiosync_sign.py` only covers the synthetic case.
- [ ] **The result that matters most**: a song marked `measured` that is visibly out of sync
      is a genuine audiosync bug and the most valuable finding this whole plan can produce.
      Report it with the song name. A `guess` being out of sync is expected, not a bug --
      that marker exists precisely so the two can be told apart.
- [ ] While you are in there, try the two new controls on a song you know is wrong:
      right-click -> **Adjust sync offset** (confirm it now goes past -30s, and that typing an
      exact value works), and right-click -> **Dump this video** on anything that turns out to
      be the wrong song entirely. After dumping, re-run a download for that song and confirm
      from `log.txt` that it does **not** come back with the same upload
      (`Skipping N previously dumped result(s)`).

### 4d. Real-library smoke on the unreplayed fixes
The original first item here -- "confirm the sibling-folder fix handles pre-existing nested
state, or confirm it's stranded" -- **was answered in Phase 3 by execution: it is stranded.**
4-zero above is the fix. What remains is confirming the migration worked on real data and
that the duration floor behaves.

- [ ] After 4-zero, re-run **Fix chart names** and confirm the migrated Kryptonite folder is
      now *reachable* (it should appear in the scan counts, where before it was invisible).
- [ ] Run one full download cycle on 2-3 songs with no video and confirm from `log.txt` that
      the duration floor either attaches a plausible video or attaches **nothing** -- never an
      unrelated one. (This is the Green Day/Kryptonite failure mode.)
- [ ] Nested-library check, if any part of your library is `Songs/<Pack>/<Song>/`: confirm the
      scans now report songs from inside packs. Before Phase 2.5 a flat scan found **zero**
      songs there, and `chart_rename` would have relocated whole packs.

### Phase 4 results so far (2026-07-19)

**Nothing destructive has run.** The app log contains no move, rename, removal or
conversion for the whole session -- dry runs only, real library untouched.

**4b, static-art detector, validated on real data for the first time.** All 434 videos in
`M:\_Organized\Songs` probed read-only (`probe_static_video` directly; the conversion
function was never called, not even with `dry_run=True`):

| | count | measured cell delta |
|---|---|---|
| would convert | 97 | **0 .. 4** |
| would keep | 337 | **50 .. 255** |
| threshold | | **14** |

**Nothing at all scored between 5 and 49.** The populations are sharply bimodal with a wide
empty gap, so the threshold could sit anywhere in 5..49 and classify this library
identically. That is a far stronger result than the synthetic fixtures gave, and it
retroactively confirms the Phase 3 change from 24 -> 14 was safe in both directions.
Worst case on the convert side is `Quiet Company - How Do You Do It` at cell delta 4, still
3.5x clear of the line. Full per-song report:
`C:\Users\aaron\AppData\Local\Temp\static_art_dryrun_report.csv`.

97 of 434 videos (22%) are static album art -- the disk saving this feature exists for.

**Two real bugs found by the real library that no fixture would have produced:**

- `_Weird Al_ Yankovic - White & Nerdy` is a genuine song whose folder starts with `_`
  (the quotes in the artist name became underscores). Discovery skipped any `_`-prefixed
  folder, so it was invisible to every hygiene tool, silently. Fixed in `abcc554`;
  discovery now matches review folders by name. 5245 song folders now found.
- The predecessor's `_NeedsReview` folder and a `Has video: no` row sitting next to a
  visible `video.webm`. Fixed in `0a70016`.

**Still outstanding**: 4c, the in-game sync playtest. Nothing else can substitute for it.

### What to send back
Logs over screenshots, as above. The useful bundle is:
`launch_log.txt`, `%LOCALAPPDATA%\BackstageHero\log.txt`, the before/after tree diff,
`backstagehero_library.csv`, and the dedupe dry-run stdout -- plus, in your own words, which
songs were out of sync in-game and what their **Offset source** column said.

---

## Phase 5 -- Synthesis and go/no-go

**Model: Opus 4.8** (main thread) -- reconciling five parallel reviews plus real-world log
evidence into one commit decision is the integration step, and it owns the final call.

- [ ] Collect Phase 2a-2e + Phase 3 findings; de-duplicate; rank by blast radius.
- [ ] Triage each into: **blocks the commit** / **fix now** / **file as follow-up**.
- [ ] Re-run `pytest tests/ -q` after any fix lands.
- [ ] Update `tasks/todo.md` -- tick Checkpoint 3 only if 4c actually happened in-game.
- [ ] Write the commit message covering the merge honestly, including what remains unverified.

### Phase 5 outcome (2026-07-19) -- GO, with what remains unverified stated

**Suite: 358 passed, 0 skipped, across 20 test files. Baseline was 185.**

#### The four explicit blockers, checked

| # | Blocker | Status |
|---|---|---|
| 1 | `HANDOFF.md` / `launch_log.txt` staged | **Clear.** Neither is tracked; both are in `.gitignore`, along with `__pycache__/` and `.pytest_cache/`. Working tree clean. |
| 2 | Any dry run producing a non-zero filesystem diff | **Clear.** Verified three times on real trees: nested-library scan, migration against a copy of the real library, and the six-tool sweep. Zero-line diffs every time, on a cp1252 console. |
| 3 | Any Station-3 finding from 2a with no fix and no test | **Clear.** All four 2a findings fixed with tests proven red-on-revert. `_apply_renames_atomically` and `make_console_encoding_safe` are covered through their callers rather than by name. |
| 4 | Checkpoint 3 (4c) not performed | **PERFORMED.** The in-game playtest happened and produced the single most valuable finding of the whole plan. Long-standing; no longer outstanding. |

#### What the in-game playtest found, and why it was worth the whole exercise

> "ALMOST ALL OF THE MEASURED VIDEOS ARE LYRIC VIDEOS, GUITAR HERO/ROCKBAND VIDEOS.
>  Learn to Live had a real video but the offset was wrong"

Not a sync bug. **Fingerprinting confirms the audio and is blind to the picture.** A
lyric video, a Rock Band capture and the official music video carry identical audio, so
audiosync confirms all three and stamps `measured` on each. `measured` means "the audio
lines up" -- which the plan, the code and this document had all been reading as "this one
is good". Nothing in `select_video` had ever looked at what kind of video a candidate was,
despite the title being fetched and then used only for display.

Fixed by ranking on (duration, kind, search order) and by recording the attached video's
title so the library can be audited. Measured over 358 real videos: 26% are not music
videos at all (66 gameplay, 14 lyric, 12 audio-only).

#### Findings that only real data produced

Five bugs surfaced **after Phases 2 and 3 had signed off**, every one from pointing the
code at the real library rather than a fixture. The pattern is the point:

| Finding | The fixture that hid it |
|---|---|
| Candidate kind never judged | No test ever asserted anything about a candidate's title |
| `select_video`'s early returns did no filtering at all | Tests only covered the fingerprint path |
| Resync's "zero network requests" path never applies to our own downloads | Verified by **muxing synthetic audio into an MP4** -- a shape the downloader never produces |
| `_Weird Al_ Yankovic` invisible to every tool | No fixture had a song folder starting with `_` |
| Predecessor's `_NeedsReview` folder unmigrated | Fixtures used this project's spelling only |

Plus one caught by the revert discipline alone: the first unicode test **passed without its
fix**, because its fixture songs never printed their own names.

**This is the honest lesson of the exercise.** Phases 2 and 3 were thorough -- five parallel
reviewers, two adversarial passes, every finding remediated with red-on-revert tests -- and
they still could not see any of the above, because they were reasoning about the same
fixtures the code was written against. A green suite measures agreement between code and
fixtures. Only real data measures agreement between code and the world.

Two of the adversarial agents also ran **without an execution tool** and said so; re-testing
their findings by hand changed several verdicts and demoted two. Agent findings are
hypotheses.

#### Verified against the real 5,130-song library

- **static-art detector**, the only feature that deletes files: all 434 videos probed
  read-only. Convert side 0-4, keep side 50-255, threshold 14, **nothing between 5 and 49**.
  Sharply bimodal, so the threshold could sit anywhere in that gap and behave identically.
- **Discovery**: 5,245 song folders found, including the `_`-prefixed one that was invisible.
- **Migration**: dry-run zero-diff, real run moved only the intended folders, manifest written.
- **Title backfill**: 358 songs, 0 lookup failures, 242 ids recovered from the predecessor's
  `video_meta.json` -- without which most of the library could not have been audited at all.

#### What remains genuinely unverified

- **In-game sync was spot-checked, not swept.** A handful of songs were played. The
  `measured`-but-wrong case (`Learn to Live`) was hand-fixed before it could be diagnosed,
  so the original computed offset is gone and that specific failure is unexplained.
- **199 of 358 videos remain `unknown`** by kind. 148 are plain `Artist - Title`, which
  genuinely carries no signal; the rest may hide gameplay captures with plain titles.
- **27 songs have no recoverable video id**, so they cannot be audited without re-downloading.
- **266 videos predate this app** and were never filtered by anything.
- **The new kind-ranking has never run a real download.** It is tested and measured against
  real titles, but no song has been downloaded through it end to end.
- **`fpcalc` is present but pyacoustid cannot load chromaprint** on this machine, so the
  dedupe confirm path has never executed. Its "fails closed" behaviour is environmental
  luck here, not a demonstrated guard.
- **Cross-volume `move_to_review`** (the `M:` drive case) is still untested on real data.

#### Verdict

**GO.** All four blockers are clear, Checkpoint 3 is done, and every finding has a fix and a
test. The commit message must carry the unverified list above -- particularly that in-game
sync was spot-checked rather than swept, and that the kind-ranking has not yet driven a real
download.

---

### Explicit commit blockers
1. `HANDOFF.md` or `launch_log.txt` still staged.
2. Any dry run that produced a non-zero filesystem diff.
3. Any Station-3 finding from 2a with no fix and no test.
4. Checkpoint 3 (4c) not performed -- **or**, if you choose to commit before playtesting,
   the commit message must say the in-game sync verification is outstanding.
