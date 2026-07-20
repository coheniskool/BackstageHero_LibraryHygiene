# Task List: Library Enrichment Tool

**Plan**: [`plan-library-enrichment.md`](plan-library-enrichment.md)  
**Spec**: [`../SPEC-library-enrichment.md`](../SPEC-library-enrichment.md)

---

## Phase 1: Foundation Parsers

### Task 1.1: Chart Parser — notes.chart Parsing
- [x] Create `library_chart_parser.py`
  - [x] `parse_chart_instruments(path) -> Dict[str, int]` (presence: 1/-1, not charter tier — see module docstring)
  - [x] `parse_chart_nps(path) -> Optional[float]` (Expert-only, tempo-map-aware; see module docstring for v1 scoping)
  - [x] `parse_chart_features(path) -> Dict[str, bool]` (has_lyrics, has_solos, has_open_notes, has_2x_kick, has_roll_lanes — grounded in TheNathannator/GuitarGame_ChartFormats docs, not guessed; solo is `E solo`/`E soloend`, NOT `S 2` which is Star Power)
  - [x] `parse_chart_note_count(path) -> Optional[int]` (shares Expert-section selection with parse_chart_nps so both describe the same track)
- [x] Byte-safe error handling (UTF-8 validation, graceful degradation) — all four functions return safe defaults (all-absent dict / None) on missing/unreadable files, never raise
- [x] Unit tests in `tests/test_library_chart_parser.py` (25 tests, own file rather than the shared test_library_enricher.py — kept module-scoped since chart parsing is self-contained)
  - [x] Test with hand-built notes.chart samples (hand-calculated NPS/tick-math values, not just presence/absence)
  - [x] Edge cases: empty chart, corrupt/no [Song] block, non-existent file, chord-tick counting, solo-vs-soloend substring trap, S2-star-power-vs-solo trap
- [x] Code review & verify coverage (348/348 full suite green, no regressions)

**Status**: Complete

---

### Task 1.2: Score Reader — scoredata.bin Parsing
- [ ] **Spike first**: validate the clonehero-score-exporter format against a real Clone Hero install with known scores; confirm/deny `.chart`-only song coverage (resolves spec Open Question #6)
  - **Partial spike findings (2026-07-20, real install)**: the actual filename on a current Clone Hero install is **`scores.bin`**, not `scoredata.bin` (dir itself confirmed correct: `%USERPROFILE%\AppData\LocalLow\srylain Inc_\Clone Hero\`). Hex-dumped a real 358-byte `scores.bin`: it does NOT start with a raw 16-byte MD5 as clonehero-score-exporter's README described — the observed layout is a little-endian uint32 entry count, then per-entry a **1-byte length prefix (0x20=32) followed by 32 ASCII hex characters** (the MD5 as a *string*, not raw bytes), e.g. `20 '62057549D38DAFD406ECB76849290F4'`. This contradicts the assumed raw-bytes format and must be re-verified/re-derived before implementation — do not code against the raw-16-byte assumption. Test library location for a full end-to-end spike (real chart + real score) was not resolved this pass — the configured `F:\Clone Hero\Library\Songs` was empty at spike time.
- [x] Create `library_scores.py`
  - [x] `notes_mid_md5(song_folder) -> Optional[str]` — separate from `resolver_client.chart_hash()`, confirmed different by test
  - [~] `read_scoredata(ch_data_path) -> Dict[str, Dict[str, int]]` — **STUB, returns `{}` unconditionally**. Real-install spike found the file is `scores.bin` (not `scoredata.bin`) and entries are NOT the raw-16-byte-MD5 layout initially assumed (observed: length-prefixed ASCII hex string). Implementing the real parser against either the wrong or an unconfirmed layout risks silently wrong scores, which is worse than none — deferred until re-verified against a real chart+score pair (need: a populated library + a known score to check against; `F:\Clone Hero\Library\Songs` was empty at spike time)
  - [ ] Auto-detect `scores.bin` via Unity `persistentDataPath` convention — not yet wired in since read_scoredata() doesn't parse anything yet
  - [ ] Handle missing/corrupt/version-mismatched files gracefully — N/A until real parsing exists
  - [x] `.chart`-only songs get `None` from `notes_mid_md5`, not `0`
- [x] Unit tests in `tests/test_library_scores.py` (own file, matching precedent)
  - [ ] Mock scoredata.bin fragment — blocked on confirming the real layout first
  - [ ] Version mismatch detection — N/A until real parsing exists
  - [x] Missing/unreadable file scenarios (for `notes_mid_md5` and the `read_scoredata` stub)
  - [x] `.chart`-only song → `None` score via `notes_mid_md5`
  - [x] `notes_mid_md5` confirmed distinct from `resolver_client.chart_hash()` by test
- [x] Code review & verify coverage (6/6 module tests green; stub explicitly tested as a stub, not silently left uncovered)

**Status**: Partial — `notes_mid_md5()` done; `read_scoredata()` stubbed pending real-install spike (needs a populated library + known score)

**Status**: Not started

---

### Task 1.3: Chorus Response Cacher
- [x] Create `chorus_cache.py`
  - [x] `CachedChorusClient` class
  - [x] `.search_by_artist_title(artist, title, force=False)`
  - [x] In-memory + optional disk cache (JSON, atomic tmp+replace write)
  - [x] 7-day TTL; `force=True` bypasses (not "clears" — a forced call still refreshes the entry for future calls)
- [x] Unit tests in `tests/test_chorus_cache.py` (own file, matching test_library_chart_parser.py's precedent — module-scoped, not the shared test_library_enricher.py)
  - [x] Cache hit, miss, expiry (injected clock via monkeypatch, no real sleep), force-bypass
  - [x] `None` (confirmed no-match) is cached too
  - [x] Cache key is case/whitespace-insensitive (reuses library_common.normalize_lookup_value)
  - [x] Disk persistence across instances, corrupt-disk-cache tolerance, valid-JSON-on-disk check
  - [~] Concurrent access: not tested — single-process CLI/subprocess usage per spec, no concurrent-writer scenario in scope
- [x] Code review & verify coverage (10/10 module tests green)

**Status**: Complete

**Checkpoint 1**: All three foundation parsers complete and tested ✓

---

## Phase 2: Integration & Orchestration

### Task 2.1: Enrichment Engine — Main Orchestrator
- [x] Create `library_enrichment.py`
  - [x] `enrich_library(library_path, ch_data_path=None, dry_run=False, force=False, chorus_cache_path=None, verbose=False) -> Dict`
  - [x] Per-song processing:
    - [x] Compute sidecar identity key via `resolver_client.chart_hash()` (reused, not new)
    - [x] Check incremental cache (skip if unchanged) — skip is "already a key in sidecar['songs']", correct since chart_hash is content-based
    - [x] Extract: song_length, instruments, NPS, features, note_count
    - [x] Compute `notes_mid_md5`; fetch high score (via stubbed `read_scoredata`, always `{}` for now), Chorus match (via `CachedChorusClient`, not raw `chorus_client` — regression-tested)
    - [x] Scan: stems, album art (no playlist — dropped, see spec)
    - [x] Collect: problems (no song.ini, no notes.chart, non-numeric song_length, unidentifiable folder)
  - [x] Write sidecar JSON `backstagehero_enrichment.json` (matches SPEC shape; atomic tmp+replace write)
  - [x] Implement incremental logic (hash-based, not mtime-based)
  - [x] `--dry-run` support (compute, don't write — param plumbed through `enrich_library`, CLI wiring is Task 2.2)
  - [x] `--force` support (recompute all; Chorus cache has its own independent TTL, not cleared by force — tested that a forced rerun still hits cache for unchanged artist/title)
  - [~] Respect "Share matches" GUI setting — not yet wired; this module never calls `resolver_client.report()/ping()` itself so there's nothing to gate yet, revisit if that changes
- [x] Unit tests in `tests/test_library_enrichment.py` (own file, matching precedent)
  - [x] Incremental cache logic (skip-unchanged, force-reprocesses)
  - [x] Sidecar JSON structure (version, songs keyed by chart_hash, full entry shape)
  - [x] Dry-run behavior (no file written)
  - [x] Force behavior
  - [x] Problem detection & logging (no chart file at all → excluded + counted; notes.mid-only → indexed with a problem)
  - [x] Edge cases: unidentifiable folder (no chart_hash), missing notes.chart, no scores available → `None` not `0`
- [x] Code review & verify coverage (8/8 module tests green; caught and fixed a redundant double-glob in `_has_album_art` on self-review)

**Status**: Complete

---

### Task 2.2: CLI Tool — library_enricher.py
- [ ] Create `library_enricher.py`
  - [ ] Argument parsing:
    - [ ] `--library-path` (required)
    - [ ] `--dry-run`, `--force`, `--ch-data`, `--chorus-cache`, `-v`
  - [ ] Logging setup (rotating file log)
  - [ ] Call `enrich_library()`
  - [ ] Report results to stdout
  - [ ] Exit codes (0 = success, 1 = error)
  - [ ] Handle Ctrl+C gracefully
- [ ] Unit tests in `tests/test_library_enricher.py`
  - [ ] Argument parsing
  - [ ] Logging setup
  - [ ] Error handling (missing paths, invalid args)
- [ ] Smoke test: actual run on test library
- [ ] Code review & verify coverage

**Status**: Not started

**Checkpoint 2**: Enrichment engine + CLI complete; full library scan works ✓

---

## Phase 3: GUI Integration & Polish

### Task 3.1: GUI Integration — Checkbox & Subprocess
- [ ] Modify `gui.py`
  - [ ] Add checkbox "Enrich after scan" (default: on) in settings
  - [ ] Add manual "Enrich now" button
  - [ ] After `_probe_resolutions()`, spawn `python library_enricher.py ...`
  - [ ] Monitor subprocess progress (stdout/stderr → `self._queue`)
  - [ ] Display status bar updates
  - [ ] Handle subprocess failure gracefully
- [ ] GUI tests (mock subprocess)
  - [ ] Checkbox visible & toggleable
  - [ ] Subprocess spawned with correct args
  - [ ] Progress updates → status bar
- [ ] Integration test: full scan + enrichment from GUI
- [ ] Edge cases: subprocess timeout, crash, missing library path
- [ ] Code review & verify coverage

**Status**: Not started

---

### Task 3.2: Test Suite — Comprehensive Coverage
- [ ] Create/update `tests/test_library_enricher.py`
  - [ ] Chart parser tests (real samples, edge cases)
  - [ ] Score reader tests (mock data)
  - [ ] Chorus cacher tests (mock responses)
  - [ ] Enrichment engine tests (integration, incremental, dry-run, force)
  - [ ] CLI tests (argument parsing, logging, exit codes)
  - [ ] GUI tests (subprocess spawning, progress)
  - [ ] Error recovery tests (partial writes, invalid input)
- [ ] Test fixtures
  - [ ] Minimal test songs (5+ with real charts)
  - [ ] Mock scoredata.bin
  - [ ] Mock Chorus responses
- [ ] Coverage report: target 80%+
  - [ ] `pytest tests/test_library_enricher.py -v --cov=library_enricher,library_enrichment,library_chart_parser,library_scores,chorus_cache`
- [ ] Fix any coverage gaps
- [ ] Code review

**Status**: Not started

---

### Task 3.3: Integration Test & Real Library Validation
- [ ] Create test library with 5+ real song folders
- [ ] CLI dry-run test
  - [ ] Run: `python library_enricher.py --library-path test_songs --dry-run --verbose`
  - [ ] Verify output: song count, fields extracted, problems detected
- [ ] CLI normal run test
  - [ ] Run: `python library_enricher.py --library-path test_songs --verbose`
  - [ ] Verify sidecar JSON created & structure correct
- [ ] Incremental run test
  - [ ] Run again without changes: should skip all songs
  - [ ] Modify one song: should reprocess only that one
- [ ] GUI integration test
  - [ ] Scan library from GUI
  - [ ] Verify "Enrich after scan" runs
  - [ ] Verify sidecar updated
- [ ] Manual spot-check
  - [ ] Verify sidecar entries against actual chart files
  - [ ] Check for data loss/corruption on re-runs
- [ ] Create `README_ENRICHER.md`
  - [ ] Usage examples
  - [ ] Known edge cases
  - [ ] Troubleshooting
- [ ] Code review

**Status**: Not started

**Checkpoint 3**: All features integrated; ready for production ✓

---

## Summary

| Phase | Task | Checkpoint |
|-------|------|-----------|
| 1 | Chart parser + Score reader + Chorus cacher | Checkpoint 1 ✓ |
| 2 | Enrichment engine + CLI tool | Checkpoint 2 ✓ |
| 3 | GUI integration + Tests + Validation | Checkpoint 3 ✓ |

**Total estimated effort**: ~3–4 weeks (depends on real chart sample complexity and scoredata.bin reverse engineering)

**Parallelization**: Phase 1 tasks (1.1, 1.2, 1.3) can run in parallel. Phase 2 tasks cannot start until Phase 1 checkpoint. Phase 3 tasks cannot start until Phase 2 checkpoint.

