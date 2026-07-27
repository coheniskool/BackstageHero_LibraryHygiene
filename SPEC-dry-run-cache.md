# Spec: Let Dry Runs Persist the Enrichment Sidecar

**Parent spec**: SPEC-library-enrichment.md (this document only amends its `--dry-run` behavior and Sidecar Format section; everything else there — tech stack, sidecar shape, chart/score parsing — is unchanged and not restated here).

## Objective

Today, `library_enricher.py --dry-run` computes the full enrichment result for every song (chart parsing, `notes.mid` hashing, `scores.bin` lookup, Chorus match) and then throws it away — `library_enrichment.py:217-218` gates `_save_sidecar()` on `not dry_run`. A regular run immediately afterward has no record of that work: `chart_hash in sidecar['songs']` (line 203) still misses every song the dry run touched, so it's all redone from scratch. The only thing that currently survives a dry run is the Chorus API response cache (`chorus_cache.py`), which saves unconditionally.

This change makes the enrichment sidecar behave the same way: **persist it regardless of `dry_run`**, so a dry run's parsing/hashing/lookup work is reused by the next real run instead of discarded.

**User**: same solo hobbyist as the parent spec. Concretely this serves the workflow of running `--dry-run -v` to preview problems/coverage on a large library, then running for real — currently that second run repays the entire cost of the first; after this change it doesn't.

**Success looks like**: run `--dry-run` on a library, then run without `--dry-run` immediately after — the second run's `songs_skipped` count reflects everything the dry run already computed, and its `duration_seconds` is roughly the incremental-skip cost, not a full rescan.

## Behavior Change

**Redefinition**: "dry run" for this tool changes from *zero disk writes* to *zero library mutations*. The enrichment sidecar (`backstagehero_enrichment.json`) is reclassified as a read-only computation cache — the same category the Chorus cache is already in — not a mutation, because `enrich_library()` never writes into the Songs library itself (no `song.ini` edits, no renames, no deletes; that's `metadata_enrichment.py`'s and other tools' job, out of scope here per the parent spec's Boundaries).

**Consequence worth stating plainly**: because this tool's *only* write today is the sidecar, removing the `dry_run` gate on it means `--dry-run` becomes functionally a no-op for `library_enrichment.py` specifically — it still prints `"Dry run: N processed..."` instead of `"Enrichment complete: ..."` (`library_enricher.py:115`), but the on-disk result is identical either way. That's the intended trade: today's dry run computes a throwaway answer; after this change, today's dry run *is* tomorrow's up-to-date sidecar.

## Implementation

- `library_enrichment.py:217-218` — remove the `if not dry_run:` guard; call `_save_sidecar(sidecar_path, sidecar)` unconditionally.
- `library_enrichment.py`'s docstring for `enrich_library()` (lines 167-179) — update `"dry_run=True computes everything but writes nothing"` to describe the new behavior.
- `library_enricher.py` — CLI flag help text (line 45: `"...without writing the sidecar."`) needs updating to reflect that the sidecar *is* written; only library mutations (none exist in this tool today) would be skipped.
- No sidecar schema change — same shape as SPEC-library-enrichment.md's Sidecar Format. No version bump required since the on-disk format is untouched, only when it's written.

## Testing Strategy

- `tests/test_library_enrichment.py:63` — `test_enrich_library_dry_run_writes_nothing` currently asserts `not (tmp_path / le.SIDECAR_FILENAME).exists()` after a dry run. This assertion inverts: rename to `test_enrich_library_dry_run_writes_sidecar` and assert the sidecar *does* exist and contains the computed entry.
- New test: `test_dry_run_then_real_run_skips_everything` — run `enrich_library(..., dry_run=True)`, then `enrich_library(..., dry_run=False)` against the same unchanged library, and assert the second call's `songs_processed == 0` and `songs_skipped == <song count>` (mirrors the existing incremental-skip test at a similar spot to `test_chorus_cache_reuse`, which already proves this pattern for the Chorus cache).
- Existing `test_incremental_run_skips_unchanged`-style coverage should still pass unmodified — the skip logic itself (line 203) isn't changing, only what populates `sidecar['songs']` before the check runs.

## Boundaries

### Always Do
- Keep the sidecar write atomic (`_save_sidecar`'s existing tmp-file + `os.replace` pattern) — dry runs writing more often makes atomicity more load-bearing, not less.
- Keep printing the `"Dry run: ..."` vs `"Enrichment complete: ..."` distinction in `library_enricher.py`, even though the on-disk effect is now identical — the user still typed `--dry-run` and expects that label back.

### Ask First
- Any change to *other* tools' (`metadata_enrichment.py`, `chart_rename.py`, `video_repair.py`, `static_art.py`, `dedupe_report.py`) `dry_run` handling. Those tools gate actual filesystem mutations (renames, tag writes, deletes) on `dry_run`, not a cache — this spec's reasoning does not transfer to them and each would need its own review.

### Never Do
- Do not let this change start writing to `song.ini` or any library file during a dry run. The redefinition is "the sidecar isn't a mutation," not "dry run mutations are now OK." If a future change adds an actual library mutation to `enrich_library()`, it must stay gated on `dry_run`.

## Open Questions

1. **Is a no-op `--dry-run` still worth keeping as-is, or does it need a true "don't touch disk at all" escape hatch** (e.g. `--no-cache-write`) for a user previewing against a read-only-mounted library? Not building this now — flagged for the user to weigh in on if it comes up.
2. **Docs to update alongside code**: `README_ENRICHER.md:59` (`--dry-run` table row: "write nothing" → needs new wording) and `SPEC-library-enrichment.md:37` (same). Included here as a checklist item for `/plan`, not done as part of this spec doc itself.

---

**Next phase**: `/plan` to break this into tasks (code change, test rename + new test, doc updates), then `/build`.
