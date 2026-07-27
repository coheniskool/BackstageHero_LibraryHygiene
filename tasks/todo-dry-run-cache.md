# TODO: Let Dry Runs Persist the Enrichment Sidecar

See [`plan-dry-run-cache.md`](plan-dry-run-cache.md) for full detail, acceptance criteria, and verification steps. Spec: [`../SPEC-dry-run-cache.md`](../SPEC-dry-run-cache.md) (amends [`../SPEC-library-enrichment.md`](../SPEC-library-enrichment.md)).

## Task 1: Remove the `dry_run` gate on the sidecar write
- [ ] `library_enrichment.py:217-218` — drop the `if not dry_run:` guard; `_save_sidecar(sidecar_path, sidecar)` runs unconditionally
- [ ] `library_enrichment.py` `enrich_library()` docstring (lines 167-179, esp. line 175) — replace `"dry_run=True computes everything but writes nothing."` with wording matching the new "no library mutations, sidecar always written" behavior
- [ ] Manual read-check: gate is gone, no other logic in `enrich_library()` touched

## Task 2: Update dry-run test coverage (needs Task 1)
- [ ] `tests/test_library_enrichment.py:63` — rename `test_enrich_library_dry_run_writes_nothing` → `test_enrich_library_dry_run_writes_sidecar`; invert assertion to confirm the sidecar exists and contains the computed entry
- [ ] Add `test_dry_run_then_real_run_skips_everything` — dry run then real run against the same unchanged library; assert second call's `songs_processed == 0` and `songs_skipped == <song count>`
- [ ] Confirm `test_enrich_library_incremental_skips_unchanged_song` (line 73) and `test_enrich_library_force_reprocesses_unchanged_song` (line 86) still pass unmodified
- [ ] `pytest tests/test_library_enrichment.py -v` green
- [ ] `pytest tests/ -v` full suite green

## Task 3: Update CLI `--dry-run` help text (needs Task 1)
- [ ] `library_enricher.py:45` — reword `--dry-run` help text; no longer claims the sidecar isn't written
- [ ] Leave the `"Dry run: ..."` vs `"Enrichment complete: ..."` print distinction in `main()` (line 115) unchanged
- [ ] `python library_enricher.py --help` prints cleanly
- [ ] `pytest tests/test_library_enricher_cli.py -v` still green

## Task 4: Update docs (needs Task 1)
- [ ] `README_ENRICHER.md:59` — `--dry-run` table row no longer says "write nothing"
- [ ] `SPEC-library-enrichment.md:37` — `--dry-run` bullet updated to match, consistent with `SPEC-dry-run-cache.md`'s redefinition
- [ ] Manual read-check: docstring (Task 1), CLI help (Task 3), and both docs all describe the same `--dry-run` behavior

## ▶ Checkpoint (final)
- [ ] `pytest tests/ -v` full suite green
- [ ] Diff review: exactly 5 files touched (`library_enrichment.py`, `library_enricher.py`, `tests/test_library_enrichment.py`, `README_ENRICHER.md`, `SPEC-library-enrichment.md`)
- [ ] No scope creep into `--no-cache-write` (deferred) or other tools' `dry_run` handling (out of scope per spec)

---

### Notes
- Line numbers verified live against current code at plan time (2026-07-24) — re-verify at `/build` time if this drifts.
- Task 1 and Task 2 should land together in the same session; Task 1 alone leaves one test red by design (the pre-invert assertion), which Task 2 resolves.
- Tasks 2, 3, 4 have no dependency on each other — any order works once Task 1 is done.
