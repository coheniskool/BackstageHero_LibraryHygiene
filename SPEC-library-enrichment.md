# Spec: Library Enrichment Tool for Karaoke Booklet CSV

## Objective

Build a standalone enrichment tool that scans the Clone Hero library and populates `backstagehero_library.csv` with booklet-ready metadata: verified instruments and difficulties (parsed from chart files), song length, lyric/solo/open-notes flags, high scores (from Clone Hero's scoredata.bin), and structural data (album art/stems present). The tool runs incrementally (skipping unchanged songs), caches Chorus API responses, supports `--dry-run`, and integrates with the GUI as a post-scan background task.

**Out of scope**: Clone Hero Setlists (the `Documents/Clone Hero/Setlists/` file tree, separate from the Songs library) are not read or represented. Dropped after research showed native Setlist support is minimal and format is undocumented — see `tasks/plan-library-enrichment.md` history. If playlist/grouping data is wanted later, it's a separate spec.

**User**: Solo hobbyist; local trusted execution; Windows. The booklet output is a later phase; this spec focuses on data collection and CSV enrichment only.

**Success looks like**: run the tool once after a scan, it populates a sidecar JSON with enrichment data, the next CSV export left-joins that data into the spreadsheet, and a user can iterate on booklet design using stable enriched columns.

## Tech Stack

- **Language**: Python 3.x (same as rest of project)
- **Dependencies**: existing `requests` (Chorus cache), new parsing for `notes.chart` / `notes.mid` (read-only, no new external deps beyond stdlib `configparser`, `struct` for score binary format)
- **External data**:
  - Clone Hero's `scoredata.bin` binary (user's local CH install)
  - Chorus Encore API (cached; requests already in use)
  - `ffprobe` on PATH for audio duration fallback (already required)
- **No new runtime deps** beyond what BackstageHero already requires
- **Python scope only**: no C extensions, no Rust, no system binaries (fpcalc/ffmpeg already present for other features)

## Commands

```bash
# Scan the library, compute enrichment data, write sidecar JSON
python library_enricher.py --library-path /path/to/Songs [--dry-run] [--force] [--ch-data /path/to/CH/data]

# GUI: button in gui.py spawns the tool as a subprocess after scan completes
# (user can opt out via checkbox: "Enrich library after scan")
```

### CLI Flags

- `--library-path <path>`: Required. Root folder containing song subfolders (each with `song.ini`).
- `--dry-run`: Print a summary of what would be written (counts per field, problems found) without touching the sidecar.
- `--force`: Recompute all songs, ignoring mtime/hash cache. (Default: skip unchanged songs.)
- `--ch-data <path>`: Clone Hero user data directory (contains `scoredata.bin`). Detected automatically if not provided, using Unity's `persistentDataPath` convention: Windows `%USERPROFILE%\AppData\LocalLow\srylain Inc_\Clone Hero`, Mac `~/Library/Application Support/com.srylain.CloneHero`, Linux `~/.config/unity3d/srylain Inc_/Clone Hero`. If not found, high scores are skipped with a warning. (Source: [Clone Hero Wiki — Data Locations](https://wiki.clonehero.net/books/clone-hero-manual/page/data-locations).)
- `--chorus-cache <path>`: Local Chorus cache file (default: sidecar dir). Reuse across runs.
- `-v, --verbose`: Log each song's status (matched on Chorus, parsed chart, etc.).

## Project Structure

```
library_enricher.py           NEW main entry point; CLI orchestrator
  ├── library_enrichment.py   NEW enrichment engine (chart parse, score read, data collection)
  ├── library_chart_parser.py NEW chart/mid parser (instruments, NPS, feature detection).
  │                           Reference: Kenny2github/chparse (pure-Python .chart parser) —
  │                           adapt/vendor rather than hand-roll from zero where its API fits.
  ├── library_scores.py       NEW Clone Hero scoredata.bin reader. Format ported from
  │                           coolcarp/clonehero-score-exporter (documented in its source):
  │                           each entry keyed by 16 raw bytes = MD5(notes.mid), followed by
  │                           little-endian instrument id (2B) / difficulty (1B) / percent
  │                           numerator (2B) / stars (1B) / score (4B), preceded by an
  │                           instrument count and a play count. Songs shipping only
  │                           notes.chart (no notes.mid) have no key to look up by — this is
  │                           a real coverage gap, not a bug, and must be surfaced in the
  │                           sidecar (`high_score: null`, no fabricated zero).
  ├── chorus_cache.py         NEW Chorus API response cacher (or extend chorus_client.py)
  └── [existing imports]
      ├── library_common.py   (existing: normalize, hash, find_song_ini)
      ├── resolver_client.py  (existing: chart_hash() — SHA256 of the first present notes
      │                       file among notes.chart/notes.mid/notes.eof, 'ch1:' prefixed.
      │                       Reused as-is for the sidecar's incremental-cache/identity key.
      │                       NOT the same key scoredata.bin uses — see library_scores.py
      │                       above. Two different hashes for two different purposes; do not
      │                       conflate them into one field.)
      ├── chorus_client.py    (existing: API client, already used by metadata_enrichment.py)
      ├── VideoDownload.py    (existing: ini reading/writing, read_metadata)
      └── chart_rename.py     (existing: song_length parsing)

backstagehero_enrichment.json NEW sidecar output in Songs root (or --sidecar-path)
  [per-song data, keyed by chart hash; updated incrementally]

gui.py MODIFIED
  ├── Add checkbox: "Enrich after scan" (default: on)
  └── After _probe_resolutions() completes, spawn enricher as background task
      └── Emit updates to _queue (progress, completion, any errors)

tests/test_library_enricher.py NEW test suite
  ├── chart parser (instruments, NPS, features)
  ├── scoredata.bin reader (mock data)
  ├── incremental runs (hash logic)
  ├── dry-run behavior
  └── sidecar JSON structure
```

## Sidecar Format

`backstagehero_enrichment.json` lives in the Songs root (one file for the whole library). The top-level `songs` key is `resolver_client.chart_hash()` (SHA256, `'ch1:'`-prefixed) — the same stable-identity hash the resolver already uses, reused rather than reinvented. It is **not** the key used to look up scores; that's a separate field (`notes_mid_md5`) since Clone Hero's own scoredata.bin keys by MD5(notes.mid) specifically, and not every song has a notes.mid to hash:

```json
{
  "version": 1,
  "scanned_at": "2026-07-19T14:22:33Z",
  "cache": {
    "chorus_requests": {...}
  },
  "songs": {
    "<resolver_client.chart_hash() value, e.g. 'ch1:abc123...'>": {
      "folder": "path/to/song",
      "notes_mid_md5": "d41d8cd98f00b204e9800998ecf8427e",
      "song_length_ms": 180000,
      "instruments": {
        "guitar": 5,
        "bass": 4,
        "drums": 4,
        "keys": -1,
        "vocals": -1,
        "rhythm": -1,
        "guitarghl": -1
      },
      "note_count": 1247,
      "avg_nps": 7.3,
      "features": {
        "has_lyrics": true,
        "has_solos": true,
        "has_open_notes": false,
        "has_2x_kick": true,
        "has_roll_lanes": false
      },
      "stems": ["guitar.ogg", "bass.ogg", "drums_1.ogg"],
      "has_album_art": true,
      "high_score": 98750,
      "high_score_streak": 512,
      "problems": [],
      "chorus_match": {
        "confidence": 95,
        "name": "Song Name",
        "artist": "Artist"
      },
      "last_updated": "2026-07-19T14:22:30Z",
      "status": "success"
    }
  }
}
```

`notes_mid_md5` and `high_score`/`high_score_streak` are `null` when the song has no `notes.mid`, when `scoredata.bin` isn't found, or when the song was never played — these are three distinct reasons a score can be absent, and the `status`/`problems` fields (not a fabricated `0`) are what distinguish them.

## CSV Export Changes

`gui.py._export_library_csv()` is **not modified** in this spec (future cleanup). Instead, the new columns are *optionally* added by a separate export method or future enhancement, pending booklet design approval. For now, the sidecar JSON is the source of truth; a future `booklet_export.py` can left-join it into the CSV as needed.

## Code Style

- **Existing conventions**: 4-space indents, no tabs, type hints optional, minimal comments (WHY only, not WHAT).
- **New modules**: follow `library_common.py`, `chart_rename.py` patterns — no class factories, no meta-programming, favor clear functions over decorators.
- **Error handling**: log warnings, skip individual songs, always write a partial sidecar rather than failing hard. (The tool is optional; it must never block the app or a scan.)
- **Byte safety**: chart parsers validate file encoding, reject malformed charts with a `problems` entry, never corrupt the source.

## Testing Strategy

```
tests/test_library_enricher.py
  ├── test_chart_parser_instruments()         # notes.chart parsing
  ├── test_chart_parser_nps()                 # NPS calculation
  ├── test_chart_parser_features()            # lyric/solo detection
  ├── test_mid_parser_basic()                 # notes.mid basic parsing
  ├── test_scoredata_reader()                 # scoredata.bin parsing (mock data)
  ├── test_incremental_run_skips_unchanged()  # hash-based cache logic
  ├── test_dry_run_no_write()                 # --dry-run flag
  ├── test_force_rewrites_all()               # --force flag
  ├── test_chorus_cache_reuse()               # Chorus response caching
  ├── test_sidecar_json_structure()           # output validation
  ├── test_gui_integration()                  # subprocess spawning
  └── test_error_recovery()                   # partial writes on failure
```

Fixtures:
- Minimal test songs with known chart structure (notes.chart + notes.mid examples).
- Mock `scoredata.bin` fragment with test scores.
- Mock Chorus responses keyed by test song metadata.

## Boundaries

### Always Do

- **Never overwrite ini files**. Enrichment reads and collects data only; it does not modify `song.ini` (those writes stay in `metadata_enrichment.py`).
- **Preserve the CSV export logic**. `_export_library_csv()` stays unchanged this phase; enrichment is a sidecar data source.
- **Skip unchanged songs** by default. Hash every chart file; if mtime and hash are unchanged and a sidecar entry exists, skip the song.
- **Log everything**. Every song's status (matched on Chorus, parsed chart, high score found/not found, problems) goes to a `status` field and `--verbose` output.
- **Write partial results**. If enrichment crashes mid-run, the sidecar contains everything computed so far; a rerun picks up where it left off.
- **Cache Chorus responses**. Every API call is stored in the sidecar; identical name+artist queries reuse the cache for 7 days (or --force clears it).

### Ask First

- **Extending the sidecar format post-launch**. Any new field (e.g., "recommended difficulty" ML prediction) requires a spec update and a version bump.
- **Reading other binary formats** (e.g., CH's config, save-game state). Out of scope unless the user explicitly requests it.
- **Writing back to song.ini via enrichment**. That job stays in `metadata_enrichment.py`; the two tools do not compete.
- **GUI integration beyond a checkbox and progress updates**. No modal dialogs, no drag-drop, no real-time preview this phase.

### Never Do

- **Attempt network calls without explicit user action**. Chorus API is opt-in via the existing "Share matches" flow; enrichment respects that setting.
- **Delete or move songs** based on enrichment data. Problems are logged, never auto-fixed.
- **Depend on scoredata.bin being present**. If Clone Hero is not installed or the file is missing/corrupted, the tool logs a warning and continues without high scores.
- **Assume a specific Clone Hero version**. The scoredata.bin format changes; gracefully handle version mismatches (skip scores, log a warning).
- **Block the GUI or scan**. The enricher runs in a background thread; the main app remains responsive.
- **Read or reference Clone Hero Setlists**. The `Documents/Clone Hero/Setlists/` file tree is out of scope for this phase — dropped 2026-07-20 after research showed native Setlist support is minimal (append/remove-last only) and the on-disk format is undocumented. No `playlist` field exists in the sidecar this phase.

## Open Questions / Future Work

1. **Booklet output format**: This spec collects data; how to render it (PDF, HTML, print-safe layout) is phase 2.
2. **Chart parser edge cases**: Genres/instruments marked in the chart metadata vs. track names. Spec assumes track-based detection; metadata parsing may be added later.
3. **High score sync**: Should the tool export scores back to a CSV for backup, or is read-only enough?
4. **Chorus cache expiry**: 7-day default; should this be configurable?
5. **GUI button placement**: Menu item vs. toolbar icon — defer to your UI design preference.
6. **`.chart`-only score coverage**: Does Clone Hero synthesize a `notes.mid` internally (and hash that) for chart-only songs, giving them a scoredata.bin entry anyway, or do they simply never get one? Needs empirical verification against a real library with known scores before Task 1.2 acceptance criteria can be considered final — see `tasks/plan-library-enrichment.md` risk table.

**Resolved**: Playlist/setlist support is dropped from this phase (see Boundaries → Never Do). Clone Hero's real Setlists live outside the Songs library in `Documents/Clone Hero/Setlists/`, in an undocumented format — a separate spec if wanted later.

---

**Next phase**: `/plan` to break this into implementable tasks, then `/build` to code the parser and reader, `/test` for coverage.
