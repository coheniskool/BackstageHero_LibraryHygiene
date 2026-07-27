# Plan: Let Dry Runs Persist the Enrichment Sidecar

**Spec**: [`SPEC-dry-run-cache.md`](../SPEC-dry-run-cache.md) (amends [`SPEC-library-enrichment.md`](../SPEC-library-enrichment.md))

## Overview

Remove the `not dry_run` gate on `_save_sidecar()` in `enrich_library()` so the enrichment sidecar (`backstagehero_enrichment.json`) is always persisted, reclassifying it as a read-only computation cache (same category as the already-unconditional Chorus cache in `chorus_cache.py`) rather than a mutation `--dry-run` is supposed to prevent. This is a single-behavior change confirmed against current code (line numbers below verified live, not stale from the spec doc): the gate itself, one docstring, one CLI help string, one test rename + assertion inversion, one new test, and two doc rows. No sidecar schema change, no new flags — the "Open Questions" escape-hatch flag (`--no-cache-write`) is explicitly deferred, not part of this plan.

**Key scope boundary**: this touches only `library_enrichment.py`'s `enrich_library()` write gate and its docstring, `library_enricher.py`'s CLI help text, one test file, and two docs. It does not touch any other tool's `dry_run` handling (`metadata_enrichment.py`, `chart_rename.py`, `video_repair.py`, `static_art.py`, `dedupe_report.py`) — those gate real filesystem mutations and are explicitly out of scope per the spec's Boundaries → Ask First.

---

## Dependency Graph

```
Task 1: library_enrichment.py — remove dry_run gate + update docstring (foundation)
  │
  ├── Task 2: tests/test_library_enrichment.py — rename + invert assertion, add new test  (needs Task 1's behavior to pass)
  ├── Task 3: library_enricher.py — CLI --dry-run help text                                (independent of 2, describes Task 1's new behavior)
  └── Task 4: README_ENRICHER.md + SPEC-library-enrichment.md — doc rows                   (independent of 2/3, describes Task 1's new behavior)
```

Task 1 must land first — it's the only behavior change; everything else documents or verifies it. Tasks 2, 3, and 4 have no dependency on each other and can be done in any order (or in parallel) once Task 1 is in.

---

## Task Breakdown

### Task 1: Remove the `dry_run` gate on the sidecar write

**Description**: In `enrich_library()` (`library_enrichment.py`), remove the `if not dry_run:` guard so `_save_sidecar(sidecar_path, sidecar)` runs unconditionally on every call, dry-run or not. Update the function's docstring to describe the new behavior instead of the old "writes nothing" claim.

**Acceptance criteria:**
- `library_enrichment.py:217-218` no longer gates `_save_sidecar()` on `dry_run` — the call happens every time `enrich_library()` runs, regardless of the `dry_run` argument.
- The docstring (currently lines 167-179, the `"dry_run=True computes everything but writes nothing."` line at 175) is updated to state that the sidecar is written either way, and that `dry_run` now only means "no library mutations" (there are none in this module today) rather than "no disk writes."
- No other logic in `enrich_library()` changes — the incremental skip check (`chart_hash in sidecar['songs']`, line 203) and the atomic tmp-file + `os.replace` pattern inside `_save_sidecar()` are untouched.

**Verification:**
- Manual read-check: confirm the `if not dry_run:` conditional is gone and `_save_sidecar(...)` is a bare, unconditional call at the same call site.
- `pytest tests/test_library_enrichment.py -v` will still show one failure at this point (`test_enrich_library_dry_run_writes_nothing`, which asserts the sidecar does *not* exist) — that failure is expected and gets resolved by Task 2, not this task. Do not "fix" it here by touching the test.

**Dependencies**: None.

**Files touched:**
- `library_enrichment.py`

---

### Task 2: Update dry-run test coverage (needs Task 1)

**Description**: Flip the existing dry-run test to assert the new behavior, and add a new test proving the cache-reuse payoff the spec is written to deliver: a dry run followed immediately by a real run should skip everything, because the dry run already populated the sidecar.

**Acceptance criteria:**
- `tests/test_library_enrichment.py:63` — `test_enrich_library_dry_run_writes_nothing` is renamed to `test_enrich_library_dry_run_writes_sidecar`, and its assertion `assert not (tmp_path / le.SIDECAR_FILENAME).exists()` is inverted to assert the sidecar *does* exist after a dry run and contains the computed entry (e.g. assert the file exists, load it, and assert the expected song's chart_hash key is present in `sidecar['songs']`).
- A new test `test_dry_run_then_real_run_skips_everything` is added: call `enrich_library(..., dry_run=True)` against a small fixture library, then call `enrich_library(..., dry_run=False)` against the same unchanged library, and assert the second call's `songs_processed == 0` and `songs_skipped == <song count>` (mirroring the existing `test_chorus_cache_reuse`-style incremental pattern already used elsewhere in this file, e.g. `test_enrich_library_incremental_skips_unchanged_song` at line 73).
- `test_enrich_library_incremental_skips_unchanged_song` (line 73) and `test_enrich_library_force_reprocesses_unchanged_song` (line 86) are left unmodified and still pass — the skip logic itself isn't changing, only what populates `sidecar['songs']` before that check runs.

**Verification:**
- `pytest tests/test_library_enrichment.py -v` — full file green, including the renamed test and the new test.
- `pytest tests/ -v` — full suite green (no regression in unrelated modules).

**Dependencies**: Task 1.

**Files touched:**
- `tests/test_library_enrichment.py`

---

### Task 3: Update CLI `--dry-run` help text (needs Task 1)

**Description**: `library_enricher.py`'s `--dry-run` argparse help string currently says `"Compute everything and print a summary without writing the sidecar."` (line 45), which is now false. Update it to reflect that the sidecar is written either way, and that `--dry-run` only means no library mutations would happen (there are none in this tool today).

**Acceptance criteria:**
- `library_enricher.py:45` help text no longer claims the sidecar isn't written during a dry run.
- The `"Dry run: ..."` vs `"Enrichment complete: ..."` printed-summary distinction in `main()` (line 115) is left unchanged — the spec's Boundaries → Always Do explicitly requires keeping that label even though the on-disk effect is now identical either way.

**Verification:**
- Manual read-check of the new help string against the spec's wording (`SPEC-dry-run-cache.md` Implementation section).
- `python library_enricher.py --help` prints the updated text without a traceback.
- `pytest tests/test_library_enricher_cli.py -v` (existing CLI arg-parsing tests) still green — no assertion in that file pins the exact help string, so this should be a no-op for tests, but confirm.

**Dependencies**: Task 1.

**Files touched:**
- `library_enricher.py`

---

### Task 4: Update docs (needs Task 1)

**Description**: Two doc rows describe the old "dry run writes nothing" behavior and need to match the new behavior: `README_ENRICHER.md`'s `--dry-run` flag table row and `SPEC-library-enrichment.md`'s `--dry-run` CLI flag bullet (the parent spec, amended by `SPEC-dry-run-cache.md` but not yet edited to reflect it).

**Acceptance criteria:**
- `README_ENRICHER.md:59` — the `--dry-run` table row (`"Compute everything, print a summary, write nothing."`) no longer says "write nothing"; reworded to state the sidecar is written and only library mutations (none exist in this tool) would be skipped.
- `SPEC-library-enrichment.md:37` — the `--dry-run` bullet (`"Print a summary of what would be written ... without touching the sidecar."`) is updated the same way, staying consistent with `SPEC-dry-run-cache.md`'s redefinition so the parent spec doesn't contradict its own amendment.
- No other content in either doc changes — this is a wording fix on one line/row each, not a broader doc pass.

**Verification:**
- Manual read-check: both docs now describe identical `--dry-run` behavior to each other and to `library_enrichment.py`'s updated docstring (Task 1) and `library_enricher.py`'s updated help text (Task 3).
- No automated test covers doc prose; this task is verified by review only.

**Dependencies**: Task 1.

**Files touched:**
- `README_ENRICHER.md`
- `SPEC-library-enrichment.md`

---

## Checkpoint (final)

- [ ] `pytest tests/ -v` full suite green.
- [ ] `library_enrichment.py`'s gate, docstring, `library_enricher.py`'s help text, and both docs all describe the same behavior consistently.
- [ ] Diff review: exactly 5 files touched across all 4 tasks (`library_enrichment.py`, `library_enricher.py`, `tests/test_library_enrichment.py`, `README_ENRICHER.md`, `SPEC-library-enrichment.md`) — matches the spec's Implementation + Testing Strategy sections exactly, no scope creep into the deferred `--no-cache-write` flag or into other tools' `dry_run` handling.

---

## Out of Scope (explicitly, per spec)

- A `--no-cache-write` escape hatch for read-only-mounted libraries (spec's Open Question #1) — flagged for the user to weigh in on later, not built here.
- Any change to `dry_run` handling in `metadata_enrichment.py`, `chart_rename.py`, `video_repair.py`, `static_art.py`, or `dedupe_report.py` (spec's Boundaries → Ask First) — those gate real mutations, not a cache, and each needs its own review if ever revisited.
- Sidecar schema/version changes — none needed; only *when* the sidecar is written changes, not its shape.

## Risks

| Risk | Mitigation |
|------|-----------|
| Line numbers cited here (and in the spec) drift before implementation | Re-verify exact line numbers at `/build` time, same caveat this project's other plans carry (see `perf-simplification-plan.md`'s Risks section) |
| Task 1 lands without Task 2, leaving `test_enrich_library_dry_run_writes_nothing` red | Task 2 must land in the same work session as Task 1 before calling this change done; the plan's checkpoint requires a full green suite |
