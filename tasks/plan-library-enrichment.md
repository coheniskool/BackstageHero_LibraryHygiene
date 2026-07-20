# Plan: Library Enrichment Tool for Karaoke Booklet CSV

**Spec**: [`SPEC-library-enrichment.md`](../SPEC-library-enrichment.md)

## Overview

Build a library enrichment tool that scans Clone Hero song folders and populates `backstagehero_library.csv` with booklet-ready metadata. The tool extracts instrument/difficulty data from chart files, reads Clone Hero high scores from `scoredata.bin`, caches Chorus API responses, and writes a JSON sidecar file. It runs incrementally (skipping unchanged songs), supports `--dry-run`, and integrates with the GUI as a post-scan background task.

**Key scope**: Data collection and sidecar JSON generation. CSV integration and booklet output are future phases.

---

## Dependency Graph

```
library_chart_parser.py (foundational)
  └─ Parse notes.chart / notes.mid for instruments, NPS, features, note count

library_scores.py (foundational)
  └─ Parse Clone Hero's scoredata.bin for high scores, keyed by MD5(notes.mid)
     (ported from clonehero-score-exporter's documented format — distinct from
     resolver_client.chart_hash(), which library_enrichment.py uses separately
     as the sidecar's own identity/cache key)

chorus_cache.py (foundational)
  └─ Cache Chorus API responses to avoid redundant lookups

library_enrichment.py (integrator)
  ├─ Orchestrates chart_parser, scores, chorus_cache
  ├─ Computes chart hashes (MD5)
  ├─ Implements incremental logic (skip unchanged songs)
  ├─ Reads stems, album art, playlist
  ├─ Writes backstagehero_enrichment.json sidecar
  └─ Depends on: chart_parser, scores, chorus_cache, library_common, VideoDownload

library_enricher.py (CLI entry point)
  ├─ Argument parsing (--library-path, --dry-run, --force, --ch-data, --chorus-cache, -v)
  ├─ Logging setup (rotating file log)
  └─ Depends on: library_enrichment

gui.py modifications (UI integration)
  ├─ Add checkbox "Enrich after scan" (default: on)
  ├─ Spawn library_enricher subprocess after _probe_resolutions()
  ├─ Monitor progress and emit queue updates
  └─ Depends on: library_enricher, subprocess communication

tests/test_library_enricher.py (comprehensive coverage)
  └─ Unit and integration tests for all above modules
```

---

## Task Breakdown (Vertical Slices)

### Phase 1: Foundation Parsers

Parallel-friendly foundation layer. No integration yet; each module is independent and testable.

#### Task 1.1: Chart Parser — notes.chart Parsing
**Prior art to evaluate before hand-rolling**: [Kenny2github/chparse](https://github.com/Kenny2github/chparse), a pure-Python `.chart` parser exposing `chart.instruments[difficulty][instrument][note_index]`. If its license and API fit, adapt/vendor it rather than writing a parser from zero — confirm license compatibility first (check its repo for LICENSE file), and confirm it exposes (or can be extended to expose) note timing needed for NPS calculation and section markers needed for solo/lyric/2x-kick detection, which its public API as described doesn't obviously cover. If it doesn't cover those, hand-roll only the gap (feature/NPS extraction), not the whole parser.

**Acceptance Criteria:**
- `library_chart_parser.py` exports:
  - `parse_chart_instruments(path: str) -> Dict[str, int]` (diff_guitar, diff_bass, ..., -1 = absent)
  - `parse_chart_nps(path: str) -> Optional[float]` (average NPS, None on failure)
  - `parse_chart_features(path: str) -> Dict[str, bool]` (has_lyrics, has_solos, has_open_notes, has_2x_kick, has_roll_lanes)
  - `parse_chart_note_count(path: str) -> Optional[int]` (total note count)
- Handles malformed/missing files gracefully (logs warnings, returns safe defaults)
- Byte-safe: validates UTF-8 encoding, never crashes on bad input
- No external dependencies beyond stdlib (if `chparse` is vendored, its source is copied in, not pip-installed — matches this project's "no new runtime deps" constraint)

**Verification:**
- Unit tests: each parser function with real notes.chart samples
- Edge cases: empty chart, corrupt [Notes] section, missing [Song] block, non-existent file
- Verify instrument names match Clone Hero's 7-instrument set

---

#### Task 1.2: Score Reader — scoredata.bin Parsing
**Prior art (format source, not blind reverse-engineering)**: [coolcarp/clonehero-score-exporter](https://github.com/coolcarp/clonehero-score-exporter) documents the format in its source: each entry is keyed by **16 raw bytes = MD5(notes.mid)**, followed by little-endian fields — instrument id (2B), difficulty (1B), percent numerator (2B), stars (1B), score (4B) — preceded by an instrument count and a play count per song. Port/adapt this reader rather than reverse-engineering from a hex dump; confirm its license before copying code verbatim (attribute if required).

**Critical scoping note**: This key is **MD5 of `notes.mid` specifically** — not `resolver_client.chart_hash()` (SHA256, prefers `notes.chart` when both exist) and not any hash this project already computes. The two hashes serve different purposes and must not be conflated: `resolver_client.chart_hash()` stays the sidecar's incremental-cache/identity key (reused, not reinvented); this task computes a second, narrower `notes_mid_md5` used only for the scoredata.bin join, and only for songs that actually have a `notes.mid`.

**Acceptance Criteria:**
- `library_scores.py` exports:
  - `notes_mid_md5(song_folder: str) -> Optional[str]` — MD5 hex digest of `notes.mid`, or `None` if absent
  - `read_scoredata(ch_data_path: str) -> Dict[str, Dict[str, int]]` keyed by the same MD5 hex digest
  - Each song entry contains: `high_score`, `high_score_streak` (best streak/stars — confirm exact field semantics against the source project during implementation), `date_scored` if the format exposes one (verify; not confirmed above)
- Auto-detects `scoredata.bin` using Unity's `persistentDataPath` convention (Windows: `%USERPROFILE%\AppData\LocalLow\srylain Inc_\Clone Hero`; Mac: `~/Library/Application Support/com.srylain.CloneHero`; Linux: `~/.config/unity3d/srylain Inc_/Clone Hero`) or `--ch-data` override — see [Clone Hero Wiki](https://wiki.clonehero.net/books/clone-hero-manual/page/data-locations)
- Gracefully handles missing/corrupt/version-mismatched files: logs warning, returns `{}`
- Byte-safe: does not crash on corrupted/truncated binary file
- Songs with no `notes.mid` get `notes_mid_md5: None` and `high_score: None` in the sidecar — never a fabricated `0`, and the `problems` field must not flag this as an error (it's an expected, common case for `.chart`-only songs)

**Verification:**
- Unit tests: mock scoredata.bin fragment (hand-crafted binary, built from the documented layout above)
- Tests for version mismatch detection, missing file, unreadable/truncated file
- Verify MD5(notes.mid) → score lookup works correctly
- **Spike required before this task is considered done**: validate against a real Clone Hero installation with known scores — confirm the documented format actually matches the local `scoredata.bin`, and confirm/deny whether `.chart`-only songs (no `notes.mid`) ever get a scoredata.bin entry (does CH synthesize a `.mid` internally and hash that, or do they simply never get scored this way?). This directly resolves Open Question #6 in the spec.

**Risk**: scoredata.bin format is version-dependent and only documented via one third-party reader (not an official spec); graceful degradation is critical, and the spike above must run before other tasks depend on this module's exact return shape.

---

#### Task 1.3: Chorus Response Cacher
**Acceptance Criteria:**
- `chorus_cache.py` exports `CachedChorusClient` class:
  - `.search_by_artist_title(artist: str, title: str, force: bool = False) -> Optional[Dict]`
  - Caches responses in memory and optionally on disk (JSON file in sidecar directory)
  - Reuses cache for 7 days; `force=True` clears cache
  - Wraps `chorus_client.search_by_artist_title()` transparently
- No new external dependencies beyond existing `requests`

**Verification:**
- Unit tests: cache hit, cache miss, cache expiry, force-clear
- Tests: concurrent access (if multi-threaded later)
- Verify cached response structure matches original Chorus schema

---

### Phase 2: Integration & Orchestration

#### Task 2.1: Enrichment Engine — Main Orchestrator
**Acceptance Criteria:**
- `library_enrichment.py` exports:
  - `enrich_library(library_path, ch_data_path=None, dry_run=False, force=False, chorus_cache_path=None, verbose=False) -> Dict[str, Any]`
  - Returns summary: `{'songs_processed': int, 'songs_skipped': int, 'new_data_written': int, 'problems_found': int, 'duration_seconds': float}`

- **Processing logic per song:**
  1. Compute the sidecar identity/cache key via `resolver_client.chart_hash()` (reused, not reinvented)
  2. Check incremental cache: skip if unchanged (unless `force=True`)
  3. Extract: song_length, instruments, NPS, features, note_count
  4. Compute `notes_mid_md5` (via `library_scores.notes_mid_md5()`) and fetch high score from scoredata.bin if present; fetch Chorus match (with caching)
  5. Scan: stems present, album art present
  6. Collect: problems (missing required files, parse errors)

  No playlist/setlist data is collected this phase (dropped — see spec Boundaries → Never Do). Clone Hero's real Setlists live outside the Songs library in an undocumented format; out of scope until a separate spec covers it.

- **Sidecar JSON output** (`backstagehero_enrichment.json`):
  - Matches SPEC exactly: version, scanned_at, cache, songs dict
  - Incremental: only modified songs get updated
  - Partial writes on crash are valid; rerun picks up where it left off

- **Flags:**
  - `--dry-run`: Compute everything, print summary, write nothing
  - `--force`: Recompute all songs, clear Chorus cache
  - `--verbose`: Per-song status output

- **Integration:**
  - Respects existing "Share matches" GUI setting (reads resolver_client config)
  - Never blocks; designed for background execution

**Verification:**
- Unit tests: incremental cache logic (hash-based skip)
- Unit tests: sidecar JSON structure validation
- Unit tests: dry-run behavior (no writes), force behavior (full recompute)
- Integration test: run on real test library, verify sidecar output
- Edge cases: empty library, songs with missing chart files, corrupt files, no high score available

---

#### Task 2.2: CLI Tool — library_enricher.py
**Acceptance Criteria:**
- Entry point: `python library_enricher.py --library-path /path/to/Songs [OPTIONS]`
- Argument parsing:
  - `--library-path <path>` (required)
  - `--dry-run` (optional)
  - `--force` (optional)
  - `--ch-data <path>` (optional; auto-detect if omitted)
  - `--chorus-cache <path>` (optional; default: sidecar dir)
  - `-v, --verbose` (optional)

- **Behavior:**
  - Sets up rotating file log (like VideoDownload.py)
  - Calls `library_enrichment.enrich_library()`
  - Reports results to stdout
  - Exit code: 0 on success, 1 on error
  - Handles Ctrl+C gracefully (partial sidecar is valid)

- **No GUI**: CLI-only (GUI integration is Task 3.1)

**Verification:**
- Unit tests: argument parsing, logging setup
- Smoke test: actual run on test library
- Error handling: missing library path, invalid ch_data path, etc.

---

### Phase 3: GUI Integration & Polish

#### Task 3.1: GUI Integration — Checkbox & Subprocess
**Acceptance Criteria:**
- `gui.py` modifications:
  - Add checkbox "Enrich after scan" (default: on) in settings/preferences
  - After `_probe_resolutions()` completes, spawn `python library_enricher.py ...` in background thread
  - Pass: current library path, CH data path (if known), sidecar path
  - Monitor subprocess: read stdout/stderr, emit updates to `self._queue` (progress, final status)
  - Handle subprocess failure gracefully: log warning, do not crash app
  - Add manual "Enrich now" button (bypasses auto-setting)

- **User experience:**
  - Enrichment runs transparently after scan
  - Status bar shows progress (e.g., "Enriching library... 45/150 songs processed")
  - User can cancel by closing the app or clicking "Stop" (if subprocess supports it)

**Verification:**
- GUI tests: checkbox visible and toggleable
- GUI tests: subprocess spawning (mock subprocess.Popen)
- GUI tests: progress updates reflected in status bar
- Integration test: full scan + enrichment from GUI
- Edge cases: subprocess timeout, subprocess crash, missing library path

---

#### Task 3.2: Test Suite — Comprehensive Coverage
**Acceptance Criteria:**
- `tests/test_library_enricher.py` covers:
  - Chart parser: instruments, NPS, features, note count (real notes.chart samples)
  - Score reader: scoredata.bin parsing (mock data)
  - Chorus cacher: cache hit, miss, expiry, force-clear (mock responses)
  - Enrichment engine: incremental runs, sidecar structure, dry-run, force, problem detection
  - CLI: argument parsing, logging, exit codes
  - GUI: checkbox, subprocess spawning, progress updates
  - Error recovery: partial writes on crash, invalid input files

- **Test fixtures:**
  - Minimal test songs with known chart structure (real notes.chart samples)
  - Mock scoredata.bin fragment
  - Mock Chorus responses

- **Coverage target**: 80%+ on new modules

**Verification:**
- `pytest tests/test_library_enricher.py -v --cov=library_enricher,library_enrichment,library_chart_parser,library_scores,chorus_cache`
- All tests pass
- No coverage gaps in critical paths (incremental logic, hash computation, sidecar write)

---

#### Task 3.3: Integration Test & Real Library Validation
**Acceptance Criteria:**
- **Full end-to-end test:**
  1. Create test library with 5+ real song folders (videos, charts, ini files)
  2. Run CLI dry-run: `python library_enricher.py --library-path test_songs --dry-run --verbose`
  3. Verify dry-run output shows expected song count, fields extracted, problems detected
  4. Run CLI without dry-run: `python library_enricher.py --library-path test_songs --verbose`
  5. Verify sidecar JSON is created with correct structure
  6. Verify sidecar data is accessible (ready for future CSV export)
  7. Run CLI again (incremental): should skip unchanged songs, reprocess modified ones
  8. Run from GUI: verify "Enrich after scan" works and sidecar is updated

- **Documentation**: Create `README_ENRICHER.md` documenting known edge cases, limitations, usage examples

**Verification:**
- Integration test script passes
- Manual spot-check: verify sidecar JSON entries against actual chart files
- No data loss on re-runs
- GUI integration works end-to-end

---

## Checkpoints

### Checkpoint 1 (end of Phase 1)
- [ ] Chart parser complete and tested
- [ ] Score reader complete and tested
- [ ] Chorus cacher complete and tested
- All foundation modules work independently

### Checkpoint 2 (end of Phase 2)
- [ ] Enrichment engine complete and tested
- [ ] CLI tool complete and tested
- [ ] Full library scan works; sidecar JSON generated correctly
- Ready for GUI integration

### Checkpoint 3 (end of Phase 3)
- [ ] GUI integration complete
- [ ] Checkbox and subprocess spawning work
- [ ] End-to-end flow works from GUI
- Ready for production use

---

## Implementation Notes

1. **No new external dependencies**: All work uses stdlib + existing imports (requests, configparser, struct).

2. **Incremental first**: Hash-based caching is core to performance; implement from the start, not as an afterthought.

3. **Graceful degradation**: Missing scoredata.bin, corrupt Chorus responses, malformed charts → all should be warnings, not crashes. The tool is optional; it must never block the app.

4. **Byte safety**: Always validate encoding; never assume UTF-8 without error handling.

5. **Logging**: Every song's status goes into the sidecar JSON. Verbose flag enables per-song stdout output.

6. **Partial writes**: If enrichment crashes mid-run, the sidecar contains everything computed so far; a rerun picks up where it left off.

7. **Windows focus**: Auto-detect CH data directory from Windows APPDATA; test on Windows first.

---

## Known Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| scoredata.bin format is third-party-documented only, no official spec, version-dependent | Port from clonehero-score-exporter's documented layout; run the Task 1.2 spike against a real installation with known scores before locking the format as final; graceful degradation (skip scores with warning) if it doesn't match |
| `.chart`-only songs (no notes.mid) may never appear in scoredata.bin | Confirmed as a real, expected gap (not a bug) via the Task 1.2 spike; sidecar represents it as `null`, never a fabricated `0` |
| Conflating the two chart-identity hashes (resolver's SHA256 vs. CH's MD5(notes.mid)) | Kept as two explicitly separate fields (`songs` key vs. `notes_mid_md5`) in the sidecar schema from the start — see spec Sidecar Format |
| Chorus API rate-limiting | Cache responses; --force clears cache; long TTL (7 days) reduces requests |
| Malformed chart files crash parser | Validate encoding; catch parse exceptions; log problems entry |
| Long enrichment blocks GUI | Run as subprocess in background thread; don't wait synchronously |
| Sidecar JSON corrupted mid-write | Write to temp file first, atomic rename on success |

