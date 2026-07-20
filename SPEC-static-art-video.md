# Spec: Static Album-Art Video Detection

> Companion spec to `SPEC.md` (the BackstageHero + Library Hygiene merge).
> `SPEC.md` remains the spec of record for that merge; this file covers one
> feature on top of it. Listed as an open item in `tasks/todo.md` under
> "Still-frame/album-art-photo video detection".

## Description

Some YouTube "videos" are just an album cover held on screen for the length of
the song (the *My Name Is Jonas* case). BackstageHero currently downloads these
as full video files: tens of megabytes to display one still image, times however
many songs in a 5,130-song library. Detect them, keep the image as `album.png`,
drop the video, and record the decision so the app doesn't re-download the same
static video on every subsequent run.

## Objective

A static-image "video" is recognised as such and never occupies a `video.mp4`
slot. Instead the single frame it contains becomes the song's album art (if the
song has none), the video file is removed, and `song.ini` records that this song
was deliberately left without a video — so the download loop treats it as
*resolved*, not as *still missing a video*.

**Why this matters beyond disk**: the existing duration-plausibility hard floor
in `select_video()` does **not** catch these. A static album-art upload of the
correct song is exactly the right duration, so it passes the floor cleanly. This
is the remaining half of the gap the Kryptonite/Green Day bug exposed — that fix
stopped *wrong* videos, this one stops *contentless* ones.

**Success looks like**: run the library scan over a real library, and the songs
whose videos are just a held album cover come back as album art with the video
gone and no re-download on the next pass — with zero real videos touched.

## Scope decisions (settled)

| Decision | Choice |
|---|---|
| Detection bar | **Strict acts, loose reports.** Only effectively-identical frames trigger the action. Slow zooms, spinning vinyl, and visualizers are *logged as loose candidates* for later review, never acted on. |
| On detection | Extract one frame to `album.png` **only if the song has no album art**, delete `video.mp4`, write a `song.ini` marker. |
| Where it runs | **Both** — a download-time gate (before a new static video is committed) and a library-wide scan (for videos already on disk), sharing one detector. |

## Tech Stack

No new dependencies. Everything needed is already present:

- `ffprobe` / `ffmpeg` on PATH — already required; reuse `video_repair.py`'s
  subprocess conventions exactly (`_NO_WINDOW`, JSON output, `check=True`,
  broad `except` → safe default, `log.error` on failure).
- `Pillow` (`pillow>=9.0`, already in `requirements.txt` for logo/splash
  rendering) — frame hashing. **No new pip install, and specifically not
  OpenCV or imagehash.**

### Detection algorithm

1. **Cheap prefilter (skip work, never decide).** `ffprobe` for duration,
   dimensions, and bitrate. A held still encodes to a far lower bitrate than
   real footage. Used *only* to skip decoding on obvious real videos — it must
   never on its own classify anything as static.
2. **Sample frames.** Extract N=8 frames at even intervals across the middle
   90% of the duration (skip the first and last 5% to ignore fade-in/fade-out
   and leading black frames). Pipe each as PNG to stdout via
   `ffmpeg -ss <t> -i <video> -frames:v 1 -f image2 -` — no temp files.
3. **Perceptual hash each frame.** 64-bit average-hash on an 8×8 grayscale
   downscale, via Pillow.
   > **Critical detail**: consecutive frames of a "static" video are *not*
   > byte-identical. Lossy H.264 encoding introduces per-frame noise, so exact
   > equality fails on genuinely static videos. A perceptual hash with a
   > tolerance is required, not an equality check.
4. **Classify by maximum pairwise Hamming distance.**
   - `<= STATIC_STRICT_DISTANCE` (2) → **static**. Act.
   - `<= STATIC_LOOSE_DISTANCE` (10) → **near-static**. Log only, never act.
   - otherwise → real video. Do nothing.

### Fail-safe rule (non-negotiable)

Every failure path — ffprobe error, ffmpeg error, too-short video, unreadable
frame, missing Pillow, zero samples — resolves to **"this is a real video, do
nothing."** The feature deletes files; uncertainty must never delete. A missed
static video costs disk, a false positive destroys a real one.

Videos shorter than `MIN_PROBE_SECONDS` (10s) are never classified — too few
samples to be confident.

## Project Structure

New module, following `video_repair.py`'s shape:

```
static_art.py
    probe_static_video(video_path)   -> 'static' | 'near_static' | 'video' | 'unknown'
    _sample_frame_hashes(path, n)    -> [int] (internal)
    _average_hash(pil_image)         -> int   (internal)
    convert_to_album_art(song_dir, dry_run=False)
                                     -> result dict, same vocabulary as
                                        chart_rename.py's scan/apply functions
```

Integration points:

```
VideoDownload.process_download()  -> after download, before set_ini_values:
                                     if static, convert instead of committing
                                     the video. Do NOT report to the resolver.
gui.py LibraryToolsDialog         -> new "Find static album-art videos" tool,
                                     alongside dedupe / chart-rename / repair.
                                     Dry-run default, same as its siblings.
chart_rename.py                   -> update apply_album_art_rename()'s docstring
                                     claim that "No content verification is
                                     feasible for album art" -- it now is, for
                                     art sourced from a video.
```

### `song.ini` marker

```ini
backstagehero_video = static_art
```

Consistent with the existing `backstagehero_source` / `backstagehero_res` /
`backstagehero_sync` key convention. `process_download()` must treat this marker
the same way it treats an existing `video.mp4`: **skip the song** unless
`replace=True`. Without this, every run re-downloads the same static video, and
the feature accomplishes nothing beyond churn.

## Commands

```
Scan (dry run):   via GUI -> Library Tools -> "Find static album-art videos"
                  dry-run ON by default; reports counts + a per-song list
Apply:            same dialog, dry-run unchecked
Test:             pytest tests/test_static_art.py -v
```

## Code Style

Match `video_repair.py` and `chart_rename.py`: 4-space indent, no tabs, module
logger `log = logging.getLogger('backstagehero')`, `_NO_WINDOW` on every
subprocess, result dicts with `status` + `detail` keys, `dry_run=False`
parameter on anything that touches the filesystem.

Comments explain *why*, in this repo's established voice — particularly the
lossy-encoding rationale for perceptual hashing, which is the single most
counterintuitive part of the implementation.

## Testing Strategy

`tests/test_static_art.py`, pytest, matching the existing suite's conventions.

- **Synthetic fixtures generated at test time with ffmpeg**, skipped via
  `pytest.mark.skipif` when ffmpeg is absent:
  - a single PNG looped to 60s → must classify `static`
  - `testsrc` moving pattern → must classify `video`
  - a slow zoom on one image → must classify `near_static` (reported, **not**
    acted on) — this is the regression guard on the strict/loose boundary
  - a 5s clip → `unknown` (below `MIN_PROBE_SECONDS`)
- **Unit tests with no ffmpeg dependency**: `_average_hash` on constructed PIL
  images; Hamming-distance thresholds at their exact boundaries (2, 3, 10, 11).
- **Fail-safe tests**: monkeypatch ffprobe/ffmpeg to raise, return garbage, and
  return empty — every case must yield `unknown`, and `convert_to_album_art`
  must delete nothing.
- **Non-destruction tests**: existing `album.png` is never overwritten;
  `dry_run=True` produces a zero-byte filesystem change.
- **Loop test**: after conversion, a second `process_download()` pass on the
  same folder must skip the song, not re-download it.

## Boundaries

**Always**
- Fail safe to "real video" on any uncertainty or error.
- Honour `dry_run` on every filesystem-touching path.
- Preserve existing album art — never overwrite `album.{png,jpg,jpeg}`.
- Log every detection with the measured distance, so decisions are auditable
  from `%LOCALAPPDATA%\BackstageHero\log.txt` rather than from screenshots.

**Ask first**
- Loosening `STATIC_STRICT_DISTANCE`, or promoting the loose tier to act.
- Deleting a video when the song already has album art (the current spec keeps
  the art and still drops the video — confirm against real results).

**Never**
- Delete a video the detector did not positively classify as `static`.
- Report a static-art song to the community resolver — it has no video match
  to contribute, and reporting one would poison the shared pool.
- Add a new pip dependency for image hashing.

## Model Assignment

| Work | Model | Why |
|---|---|---|
| Detector core (`probe_static_video`, hashing, thresholds) | **Opus 4.8** | The perceptual-hash threshold is the entire correctness surface, and getting it wrong deletes real videos. Subtle, adversarial, and irreversible when wrong. |
| Delete + marker path (`convert_to_album_art`, `process_download` gate) | **Opus 4.8, max effort** | Station 3: removes files from a real library. Also owns the re-download-loop guard, which is easy to get subtly wrong. |
| ffmpeg fixture generation + test suite | Sonnet 5 | Mechanical fixture plumbing against a clear rubric, once thresholds are fixed. Escalate any failing boundary case to Opus. |
| GUI tool wiring in `LibraryToolsDialog` | Sonnet 5 | Follows three existing sibling tools; pattern-matching work. |
| Docstring fix in `chart_rename.py`, README | Haiku 4.5 | Mechanical text edit with one right answer. |

## Open Questions

1. **Do near-static videos deserve their own action later?** Deferred by
   design — collect real loose-tier hits from your library first, then decide
   with evidence instead of guessing at a threshold.
2. **Should the extracted frame be upscaled or cropped?** YouTube static-art
   uploads are often 1080×1080 square padded into 16:9. Cropping to the actual
   art bounds would be nicer, but adds an edge-detection problem. Starting with
   the raw frame; revisit if the results look bad in practice.
3. **Retroactive pass over songs already marked with a `-3000` guess?** Many
   existing static-art videos are likely also unmatched guesses. Running the two
   scans together might be a natural cleanup pairing.
