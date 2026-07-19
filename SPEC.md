# Spec: BackstageHero + Library Hygiene Merge

## Description

Rebase our Clone Hero video-sync tooling onto BackstageHero: keep its GUI, packaged exe, fingerprint-gated matching, and lightweight sync engine as the foundation, then port our VFR/codec repair, chart-rename, dedupe, metadata enrichment, and test suite on top — one tool with both projects' strengths.

## Objective

`BackstageHero_LibraryHygiene` (GitHub: `coheniskool/BackstageHero_LibraryHygiene`, a fork of `jmb988/BackstageHero`, currently sitting at upstream commit `5b69ae5` with zero divergent commits) becomes the successor to `clonehero-video-downloader`. BackstageHero's fingerprint-gated video matching and `audiosync.py` offset engine replace our title-confidence matching and `audio-offset-finder`-based offset engine — a structural upgrade per the comparison at `Projects/clonehero-video-downloader/COMPARISON-BackstageHero.md`. On top of that base, this spec ports the library-hygiene features BackstageHero doesn't have: VFR detection + CFR re-encode, unplayable-codec (VP9/AV1 WebM) repair, chart-file-name repair, and acoustic-fingerprint duplicate detection. Metadata enrichment and a pytest test suite come along as defaults.

This phase runs **from source** (`python gui.py`), no PyInstaller build. The **community resolver stays enabled by default** (BackstageHero's out-of-the-box behavior): the user has opted into contributing to and benefiting from the shared pool at the upstream author's hosted instance (`backstage.jimmyproton.co.uk`) — lookups skip the YouTube search for known charts, and confident matches are reported back to help others. The GUI "Share matches" toggle retains control over the outbound half. What is **out of scope** is only the self-*updater*/packaging: no PyInstaller exe build and no GitHub-release self-update pipeline this phase (the user has no infrastructure to maintain a release channel). The resolver requires no infrastructure on the user's end — it's the author's hosted service.

**User**: solo hobbyist (same as before, now on BackstageHero's GUI instead of the old CLI), local/trusted execution, same Windows environment.

**Success looks like**: point the GUI at the Songs library folder, and for every song missing a video, it downloads a fingerprint-verified match and auto-syncs it via `audiosync.py`. Separately, VFR and bad-codec videos already in the library get detected and repaired, chart-name and duplicate-detection scans are available (GUI or CLI, TBD in `/plan`), blank `song.ini` metadata gets filled from Chorus Encore, and the ported/new code is covered by a pytest suite — all without any outbound call to a resolver or update server the user didn't explicitly turn on.

## Tech Stack

- Python 3.x, `customtkinter` (GUI), `yt-dlp`, `ffmpeg`/`ffprobe` on PATH, `numpy` (the only real dependency of `audiosync.py` beyond stdlib)
- **Removed**: `audio-offset-finder`, `librosa`, `numba` — and with them, the `numpy>=2,<=2.4` version-pin constraint that plagued the old offset engine
- **Carried over from our project**: `requests` (`chorus_client.py`, metadata enrichment), `pyacoustid` + external `fpcalc` binary (duplicate detection) — same trust bar as `ffmpeg`, get `fpcalc` from the official AcoustID/Chromaprint release only
- **Active by default**: `resolver_client.py` (community resolver client) — lookups + reporting on, contributing to the shared pool. `resolver/` (the server) is the author's hosted service, not something we run. `updater.py`'s app self-update (GitHub exe channel) stays present but is `_frozen()`-gated, so it never fires from a source run this phase
- No PyInstaller/`build.py` packaging step in this phase

## Commands

```
Setup:  pip install -r requirements.txt   (customtkinter, yt-dlp, numpy, requests, pyacoustid added;
                                            audio-offset-finder/librosa removed)
        ffmpeg/ffprobe on PATH            (not pip-installable; verify with `ffmpeg -version`)
        fpcalc on PATH                     (AcoustID/Chromaprint release build; needed for dedupe confirmation)
Run:    python gui.py                      (main entry point, replaces CH-VideoScript.py)
Dedupe: python dedupe_report.py --library-path <path> [--dry-run]   (ported, largely as-is)
Test:   pytest tests/ -v
```

Chart-name repair and metadata enrichment's exact entry points (GUI menu item vs. CLI flag) are an open question — see **Open Questions**.

## Project Structure

Retained from BackstageHero, unmodified in intent:
```
VideoDownload.py     -> search/select/download/sync orchestration. resolver_client calls stay
                         active (community lookups + reporting) -- unchanged from BackstageHero.
gui.py                -> the app window. "Share matches" checkbox stays (defaults on, as upstream)
                         -- the user controls the outbound half from here.
audiosync.py          -> unchanged. Sole offset/matching engine going forward.
resolver_client.py    -> unchanged. Talks to the author's hosted resolver by default.
resolver/             -> the author's server code, unchanged, NOT deployed by us.
updater.py            -> app self-update (GitHub release channel) stays _frozen()-gated, so it
                         never fires from source this phase; yt-dlp PyPI auto-update kept (also
                         _frozen()-gated -> a no-op from source, pip owns yt-dlp here).
```

Retired:
```
clonehero_video_offset.py's compute_offset()/extract_audio()/find_offset_between_files() path
    -> removed. audiosync.py replaces it entirely.
```

Ported / adapted (new modules in this repo):
```
video_repair.py       -> new module carrying over probe_frame_rate(), reencode_to_cfr(),
                         probe_video_codec() from clonehero_video_offset.py, adapted to
                         BackstageHero's video.mp4-only naming convention (no video.avi/.webm/.ogv
                         variants to handle -- BackstageHero always writes video.mp4).
chart_rename.py        -> new module carrying over scan_song_folder_chart_names(),
                         process_chart_folder_names(), scan_song_folder_audio_stems(),
                         scan_song_folder_album_art(), move_to_needs_review() from
                         clonehero_video_offset.py, reformatted to this repo's style (4-space,
                         no tabs).
chorus_client.py       -> ported as-is (Chorus Encore API client).
dedupe_report.py       -> ported, updated for BackstageHero's video.mp4-only convention and its
                         song.ini backstagehero_source/backstagehero_res keys where relevant to
                         keeper scoring.
tests/                 -> new pytest suite: hygiene-module tests adapted from the old suite
                         (test_offset_file_discovery.py-style fixtures, VFR/codec probe tests),
                         plus smoke coverage of the BackstageHero paths this phase now depends on
                         (audiosync.py's sign convention, set_ini_values() byte-preservation) --
                         BackstageHero ships no tests today.
```

## Code Style

Match BackstageHero's existing conventions (confirmed: 4-space indentation, zero tabs across all 7 top-level `.py` files) — this is now the base, not the guest:

- 4-space indentation, not tabs (our old script used tabs — every ported module gets reformatted, not carried over verbatim)
- Plain functions, one per discrete step; `Song`/config-style state as a `@dataclass` only where `gui.py` already does it — no new classes introduced for hygiene logic
- Comments explain *why*, matching the style already visible in `audiosync.py` (e.g. the `MIN_MATCHES`/`MIN_SCORE`/`MIN_CONC` tunables' comment block) and `VideoDownload.py`
- Atomic writes (temp file + `os.replace`) for every `song.ini` mutation — both codebases already do this (`set_ini_values()` here, `patch_song_ini()` there); the ported hygiene modules must follow the same pattern
- f-strings throughout (matches this codebase; our old `.format()`-style calls get converted during porting, not preserved)

## Testing Strategy

BackstageHero ships no tests today; this phase introduces a `tests/` directory and ports our pytest discipline onto the new codebase.

- **Unit-tested**: the ported hygiene modules — `video_repair.py`'s `probe_frame_rate()`/`probe_video_codec()` (canned `ffprobe` JSON, no real ffmpeg call), `chart_rename.py`'s detection/verification functions (synthetic folder fixtures, both plain and ID-suffixed naming), `dedupe_report.py`'s scoring logic.
- **Regression-tested against BackstageHero's existing (previously untested) code**, scoped to what this phase now depends on: `audiosync.compute_offset_ms()`'s sign convention (already empirically documented as matching our old convention — verify with a synthetic fixture, same method our old `test_compute_offset_sign.py` used), `VideoDownload.set_ini_values()`'s byte-preservation (mirrors our old `test_song_ini_patch.py`).
- **Not unit-tested**: `reencode_to_cfr()` itself and real fingerprint-matching against real videos — validated by manual in-game playtest, same as before.
- No CI — local script, run on-demand.

## Boundaries

- **Always**:
  - Keep BackstageHero's fingerprint-gated candidate selection and `audiosync.py` as the sole matching/sync engine — never reintroduce `audio-offset-finder`.
  - Run VFR detection (and CFR re-encode if needed) on every downloaded/existing video before it's considered final — this is the one correctness gap BackstageHero has that we're closing.
  - Preserve `song.ini` line/comment/key integrity on every write (both codebases already guarantee this — do not regress it in ported code).
  - Keep the community resolver **on by default** (BackstageHero's upstream behavior): lookups + reporting to the shared pool. Preserve the GUI "Share matches" toggle so the user retains control of the outbound half. Only chart hash / video id / offset / anonymous UUID / confidence / artist+title are ever sent — never file paths, library contents, or personal data.
  - Preserve BackstageHero's MIT license and attribution to `jmb988` in the README.

- **Ask first**:
  - Standing up our *own* resolver instance, or re-enabling the app self-*update* channel / packaging an exe, once/if the user wants that infrastructure.
  - Adding any dependency beyond what's listed above.

- **Never**:
  - Never widen what the resolver reports beyond the existing fields (no file paths, no library listing, no personal data) — the on-by-default decision covers the current payload only.
  - Never modify a `.chart`/`.mid` file or any note-timing key — scoped to `video_start_time` and the `backstagehero_*` keys only, same rule as before.
  - Never delete a song folder's chart, audio, or metadata files.
  - Never commit `song.ini`-adjacent library data, logs, or `video_meta.json`-equivalent files to git — user data, not repo content.

## Success Criteria

- [ ] `python gui.py` runs from source against a real Songs library folder
- [ ] Missing-video songs get fingerprint-verified matches via `audiosync.py`, auto-synced, using BackstageHero's existing selection/download flow unmodified
- [ ] VFR source videos are detected and CFR re-encoded (ported `video_repair.py`, unit-tested)
- [ ] Unplayable VP9/AV1 WebM videos already in the library are detected and repaired/flagged (ported codec-repair logic)
- [ ] Chart-name repair is available and verified against real-shaped fixtures (ID-suffixed `song.ini`/`notes.chart`/audio-stem/album-art, `needs_review` relocation with the cross-volume-copy verification our old code had)
- [ ] Duplicate detection is available (`dedupe_report.py` ported, AcoustID-gated, borrow-candidate flagging intact)
- [ ] Metadata enrichment is available (Chorus Encore blank-field fill, ported `chorus_client.py`)
- [ ] Community resolver works by default (lookups skip the search for known charts; confident matches are reported) and the "Share matches" toggle still gates the outbound half; app self-update stays dormant from source
- [ ] pytest suite passes, covering every ported hygiene module plus the BackstageHero regression checks listed under Testing Strategy
- [ ] README updated to describe the merged tool and credit BackstageHero/`jmb988` per its MIT license

## Resolved Decisions

- **GUI surfacing for hygiene features** (resolved): chart-name repair, dedupe, and metadata enrichment are exposed as **GUI actions**, not CLI-only scripts — surfaced in `gui.py` (menu items and/or a "Library tools" panel; exact placement is a `/plan` design detail, but they are reachable from the app, not a separate terminal invocation). `dedupe_report.py`'s standalone CLI can remain as a secondary entry point, but the primary surface is the GUI.
- **Community resolver** (resolved): **on by default** — the user has opted into contributing to and benefiting from the shared pool at the author's hosted instance. See the Boundaries note on the exact (benign) payload. GUI "Share matches" toggle retained.
- **yt-dlp auto-update channel** (resolved): **keep** `updater.maybe_update_ytdlp()` in place — but note it's `_frozen()`-gated, so it's a no-op from source this phase (pip owns yt-dlp); it only becomes active if an exe is ever packaged. Only the *app self-update* channel stays dormant from source.
- **Existing-library migration** (resolved): **no migration pass**. Existing videos are left alone — BackstageHero already skips any song that has a `video.mp4`. Only newly-downloaded videos use the new `audiosync.py` engine. Old `video_meta.json` files from `clonehero-video-downloader` are ignored (not read, not deleted).

## Notes

- **fpcalc distribution**: since exe packaging is out of scope this phase, `fpcalc` stays a manual "install locally" requirement, same as before — no change needed, just noting it's still true.
