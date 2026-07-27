# Library Enrichment Tool

Scans a Clone Hero songs library and writes `backstagehero_enrichment.json`,
a sidecar file of booklet-ready data: verified instruments and difficulty
presence, average notes-per-second, chart features (lyrics, solos, open
notes, 2x kick, roll lanes), song length, audio stems present, album art
presence, Chorus Encore metadata, and (once Task 1.2 is finished — see
below) Clone Hero high scores. It's the data-collection half of a future
printable song booklet; this tool does not render a booklet itself.

Full design background: [`SPEC-library-enrichment.md`](SPEC-library-enrichment.md),
[`tasks/plan-library-enrichment.md`](tasks/plan-library-enrichment.md),
[`tasks/todo-library-enrichment.md`](tasks/todo-library-enrichment.md).

## Usage

```bash
# Scan a library and write/update the sidecar
python library_enricher.py --library-path "C:\path\to\Songs"

# Preview without touching song.ini or other library files (sidecar cache still written)
python library_enricher.py --library-path "C:\path\to\Songs" --dry-run -v

# Recompute every song, ignoring the incremental skip
python library_enricher.py --library-path "C:\path\to\Songs" --force

# Or just run it with no flags at all -- it will ask
python library_enricher.py
```

If `--library-path` or `--ch-data` are omitted, the tool prompts for them
interactively, with real example paths to pattern-match against:

```
No --library-path given. Where is your Clone Hero Songs library?
Examples:
  F:\Clone Hero\Library\Songs
  M:\_Organized\Songs
  C:\Users\<you>\Documents\Clone Hero\Songs
Songs library path:
```

An invalid entry re-prompts (up to 3 attempts) rather than failing
immediately — but an *explicitly wrong* `--library-path` passed on the
command line still fails fast with no retry, since you already told it
where to look. Leaving the `--ch-data` prompt blank skips high scores for
that run, same as omitting the flag entirely.

Or from the GUI: the "Enrich after scan" checkbox (on by default, next to
"Share matches" in the footer) runs this automatically in a background
thread after every library scan settles — no prompting there, since the
GUI already knows the library path from its own folder picker.

### Flags

| Flag | Meaning |
|---|---|
| `--library-path` | Root folder containing your song subfolders. Prompted for interactively if omitted. |
| `--dry-run` | Preview mode: never touches `song.ini` or other library files. Still writes the enrichment sidecar cache (same as a normal run), so the work carries over to your next run. |
| `--force` | Recompute every song, ignoring the incremental (unchanged) skip. |
| `--ch-data` | Clone Hero user data directory (for `scores.bin`). Prompted for interactively if omitted; leave blank there (or pass `--ch-data ""`) to skip high scores for that run. |
| `--chorus-cache` | Chorus response cache file path. Defaults to a file next to the sidecar. |
| `-v`, `--verbose` | Log each song's status. |

## How incremental scanning works

Each song is keyed by `resolver_client.chart_hash()` — the same
content-based hash (SHA256 of `notes.chart`/`notes.mid`/`notes.eof`) the
community resolver already uses. If a song's chart bytes haven't changed
since the last run, its hash is already a key in the sidecar and it's
skipped entirely — no re-parsing, no Chorus lookup. Editing or replacing a
chart's notes file changes its hash, so it's treated as a new entry on the
next run (the old entry isn't cleaned up automatically — see Known
Limitations).

## Known Limitations

**High scores are read from `scores.bin` (not `scoredata.bin`).** A hex
dump of a real Clone Hero installation (2026-07-20) found the actual file
is named `scores.bin`, and its byte layout differs from what a third-party
tool's README described (checksums are a length-prefixed ASCII hex string,
not 16 raw bytes). The corrected layout was cross-validated against all 7
real entries in that installation's score file — exact byte accounting to
a clean EOF, sane percent/score/stars magnitudes — and is now implemented.
Full layout: [`tests/test_library_scores.py`](tests/test_library_scores.py)'s
header comment.

Scores are stored **per instrument**, not one number per song (a song can
have separate lead-guitar and bass scores). The sidecar's `high_score` is
the best of them; `score_detail` keeps the full per-instrument breakdown
(plays, difficulty, percent, stars, score) if you want more than one
number. There is no `high_score_streak` field — no streak of any kind
exists anywhere in the confirmed real format, so the original spec's
assumption of one (based on the same wrong README) was dropped rather than
kept as a permanently-`null` placeholder.

**Still open**: whether `.chart`-only songs (no `notes.mid`) ever get a
`scores.bin` entry at all, or whether Clone Hero synthesizes a `.mid`
internally to hash for them. The only real `scores.bin` available to test
against predates the current library entirely (unmodified since 2022,
while the library itself is from 2026) and its checksums matched none of
5,205 `notes.mid` files hashed across the current library — so there was
no currently-relevant song to check this specific question against. Not a
blocker for the implemented format, just an unconfirmed edge case.

**No manual "Enrich now" button.** The checkbox plus a rescan achieves the
same result; a dedicated button was deferred to keep the change to `gui.py`
(a large, high-coupling, low-test-coverage file) as small as possible.

**No live progress in the GUI.** Enrichment runs silently in the
background; success or failure is logged to the app's usual rotating log
file, not surfaced in the status bar. This was a deliberate choice: the
background thread touches zero GUI widgets, avoiding a whole class of
cross-thread bugs in a file where introducing one would be easy to miss.

**No stale-entry cleanup.** If a song's chart file changes, its old sidecar
entry (under the old hash) isn't removed — only a new entry under the new
hash is added. Not expected to matter in practice (chart files are rarely
edited in place), but worth knowing.

**Setlist/playlist data isn't collected.** Researched and dropped from
scope — Clone Hero's real Setlists live outside the Songs library
(`Documents/Clone Hero/Setlists/`) in an undocumented format. See the spec's
Boundaries section.

## Sidecar format

See [`SPEC-library-enrichment.md`](SPEC-library-enrichment.md#sidecar-format)
for the full JSON shape and field-by-field explanation, including why the
sidecar uses two different hashes for two different purposes (the resolver's
identity hash vs. the scores.bin lookup hash) rather than one.

## Testing

```bash
# Everything this tool touches
pytest tests/test_library_chart_parser.py tests/test_library_scores.py \
       tests/test_chorus_cache.py tests/test_library_enrichment.py \
       tests/test_library_enricher_cli.py tests/test_gui_enrichment_integration.py \
       tests/test_library_enricher_integration.py -v

# Or just run everything
pytest tests/ -v
```

The integration test (`test_library_enricher_integration.py`) runs the real
CLI end-to-end against a synthetic multi-song library — real file I/O, real
chart parsing, real hashing, real sidecar write, nothing mocked except the
Chorus network call. It does **not** cover the real-`scores.bin` validation
described above; that needs your own populated library.
