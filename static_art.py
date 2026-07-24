# static_art.py
# Detects "videos" that are really just one still image -- an album cover held
# on screen for the length of the song -- and turns them into what they
# actually are: album art. These uploads are common on YouTube and cost tens of
# megabytes each to display a single frame, times however many songs in a large
# library.
#
# This closes the half of the Kryptonite/Green Day gap that select_video()'s
# duration floor cannot. That floor rejects videos of the wrong *length*, but a
# static album-art upload of the correct song is exactly the right length and
# sails through it. Wrong videos and contentless videos are different failure
# modes and need different checks.
#
# Everything here is built so that uncertainty means "leave it alone". This
# module deletes files from a library the user cannot easily reconstruct, so a
# missed static video (costs disk) is always preferred over a false positive
# (destroys a real video).

import io
import itertools
import json
import logging
import os
import subprocess
from pathlib import Path

import library_common

try:
    from PIL import Image
except ImportError:  # Pillow is in requirements.txt; absence is still fail-safe
    Image = None

log = logging.getLogger('backstagehero')

# Suppress the console window each ffmpeg/ffprobe child would otherwise flash
# on a windowed (--noconsole) build; harmless 0 elsewhere. Matches
# video_repair.py's convention.
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# Pillow moved the resampling filters onto an enum in 9.1. requirements.txt
# allows 9.0, and the old module-level alias still exists in current Pillow, so
# look the enum up first and fall back rather than pinning a newer floor.
_RESAMPLE = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', None) if Image else None

# song.ini marker recording that this song was deliberately left without a
# video. Follows the existing backstagehero_source / _res / _sync convention.
VIDEO_MARKER_KEY = 'backstagehero_video'
VIDEO_MARKER_STATIC_ART = 'static_art'

# Clone Hero's recognized album-art filenames, in the order we'd prefer to find
# them. Existing art is never overwritten, so this is only used to answer
# "does this song already have art?".
ALBUM_ART_NAMES = ('album.png', 'album.jpg', 'album.jpeg')

# Maximum pairwise Hamming distance (out of 64 bits) across the sampled frames.
#
# The counterintuitive part, and the single thing most likely to be "fixed"
# into uselessness by a later reader: frames of a genuinely static video are
# NOT byte-identical. H.264 is lossy and re-quantises every frame, so a held
# still still differs slightly from itself frame to frame. An exact-equality
# check finds almost no static videos at all. A perceptual hash with a real
# tolerance is mandatory here, not a refinement of a stricter check.
#
# Strict acts, loose only reports. At or under STRICT is one held image and is
# safe to convert. Between STRICT and LOOSE covers slow zooms, spinning vinyl
# and visualisers -- real motion, but little enough that it's worth logging as
# a candidate for a future pass once there's evidence from a real library.
# Above LOOSE is footage.
STATIC_STRICT_DISTANCE = 2
STATIC_LOOSE_DISTANCE = 10

# Second, independent check, and the one that actually protects real videos.
#
# A 64-bit hash of an 8x8 downscale is far too coarse on its own: it describes
# the whole frame in 64 numbers, so motion confined to a small part of the
# picture averages away completely. Measured on real encoded fixtures, a fixed
# album cover with a single 28px blinking dot -- unmistakably a real video --
# came back at hash distance 0, and a moving test pattern at distance 2. Both
# would have been deleted on the hash alone. That is not a threshold that needs
# tightening; a global average simply cannot see local motion, and tightening
# it only breaks legitimately static videos instead.
#
# So compare frames cell by cell too, on a 32x32 luminance grid (of the
# normalised frame -- see _normalise), and take the single largest change in
# any one cell. Lossy noise on a held still is small and spread evenly, while
# real movement spikes at least one cell hard, even when the frame-wide average
# barely shifts.
#
# Measured on real ffmpeg fixtures at 640x640, 720p and 1080p. Convert side:
# a held still scores 1-2, the same still under a grain overlay 2, and at a
# deliberately awful CRF 40 it reaches 6 (10 on an earlier, harsher fixture).
# Keep side: a scrolling lyric line scores 129-230, a corner equaliser 80-84, a
# proportionally-sized moving element 90-122.
#
# The number was 24 and that was wrong. A thin element -- the crawling progress
# bar common on album-art uploads -- occupies a fraction of a cell's height, so
# its delta averages down to 17-19 and it was being CONVERTED, i.e. a video
# with real motion was deleted, which is the one outcome this module exists to
# prevent. 14 sits between the worst legitimate still measured (10) and the
# smallest real motion measured (17).
#
# That is a deliberately narrow band, and the direction of each error is why it
# is acceptable: too high deletes real video, too low merely keeps a static one
# and wastes disk. Erring low is the cheap mistake. Both edges are pinned by
# tests asserting the measured values, so this stops being a comment that can
# quietly rot away from the code.
#
# Known and accepted limit: motion occupying well under ~1% of the frame (a
# small logo bug, an 8px blinking dot at 1080p) still scores below 14 and will
# convert. Detecting that reliably needs a finer measure than a 32x32 grid, and
# the README says so rather than promising otherwise.
#
# Validated on real data (2026-07-19), which the numbers above were not: all
# 434 videos in a real 5,130-song library were probed read-only. The result is
# sharply bimodal with a wide empty gap, not a judgement call near a line:
#
#     would convert   97 videos   cell delta   0 ..   4
#     would keep     337 videos   cell delta  50 .. 255
#     nothing whatsoever scored between 5 and 49
#
# So the threshold could sit anywhere in 5..49 and classify this library
# identically; 14 is comfortably inside that gap, 3.5x above the worst video it
# converts and 3.5x below the tamest it keeps. Real static album-art uploads
# genuinely are near-perfectly still, and real footage genuinely is not.
STATIC_GRID = 32
STATIC_MAX_CELL_DELTA = 14

# Every frame is scaled to this long edge before either measure is taken, so a
# score describes content and not resolution. See _normalise().
NORMALISE_LONG_EDGE = 640

# Videos shorter than this are never classified at all: too few distinct frames
# to distinguish a still from a short shot that happens to hold steady.
MIN_PROBE_SECONDS = 10

# Frames sampled per video, and how many must survive extraction and decoding
# before a verdict is allowed. Requiring most of them is deliberate -- a verdict
# drawn from two surviving frames that happen to match is exactly the kind of
# thin evidence that would delete a real video, and thin evidence must never be
# allowed to point in the acting direction.
SAMPLE_FRAMES = 8
MIN_SAMPLES_REQUIRED = 6

# A held still compresses to a small fraction of the bitrate real footage
# needs, so anything well above this ceiling is certainly not static. Used only
# to skip the decode work on obvious real videos.
PREFILTER_BITRATE_CEILING = 2_000_000


def _probe_duration_and_bitrate(video_path):
    """(duration_seconds, bit_rate_bps) for a video, or (None, None) on failure.

    bit_rate is frequently absent from container metadata. None means
    "unknown", which the prefilter must treat as "analyse it properly" rather
    than as grounds to skip.
    """
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration,bit_rate',
             '-of', 'json', str(video_path)],
            check=True, capture_output=True, creationflags=_NO_WINDOW,
            **library_common.TEXT_UTF8,
        )
        fmt = json.loads(result.stdout).get('format', {})
        duration = fmt.get('duration')
        bit_rate = fmt.get('bit_rate')
        return (float(duration) if duration is not None else None,
                int(bit_rate) if bit_rate is not None else None)
    except Exception as e:
        log.error(f'ffprobe static-art probe error {video_path}: {e}')
        return None, None


def _extract_frame_png(video_path, timestamp):
    """One frame at `timestamp` seconds as raw PNG bytes, or None on failure.

    -ss before -i is the fast keyframe seek: this runs eight times per video
    across a whole library, and frame-exact seeking would decode from the start
    of the file on every call. image2pipe is the muxer intended for stdout, so
    no temp files are involved.
    """
    try:
        result = subprocess.run(
            ['ffmpeg', '-nostdin', '-v', 'error', '-ss', f'{timestamp:.3f}',
             '-i', str(video_path), '-frames:v', '1',
             '-f', 'image2pipe', '-c:v', 'png', '-'],
            check=True, capture_output=True, stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        return result.stdout or None
    except Exception as e:
        log.error(f'ffmpeg frame extract error {video_path} @ {timestamp:.1f}s: {e}')
        return None


def _normalise(image):
    """Scale a frame to a fixed long edge before any measurement is taken.

    Without this the cell-delta score depends on the video's resolution, not
    just its content. _luminance_grid resamples whatever it is given down to a
    fixed grid, so one cell covers 20 source pixels on a 640px frame but 60 on
    a 1920px one -- and a moving element smaller than a cell has its
    contribution averaged down in proportion. Measured on identical content: a
    fixed 16px moving dot scored 133 at 640x640, 53 at 720p and 31 at 1080p.
    Same video, same motion, a 4x difference in the number the delete decision
    is made on, purely from resolution.

    That made the thresholds untestable in any durable way: every number in
    this module was measured at 640x640, while real downloads run at 720p or
    1080p. Normalising first makes the score a function of how much of the
    FRAME moves, which is the property actually being asked about.
    """
    if max(image.size) <= NORMALISE_LONG_EDGE:
        return image
    scale = NORMALISE_LONG_EDGE / max(image.size)
    return image.resize((max(1, round(image.width * scale)),
                         max(1, round(image.height * scale))), _RESAMPLE)


def _luminance_grid(image, size):
    """A size x size grayscale downscale of a PIL image, as a bytes of 0-255.

    tobytes() on mode 'L' is contiguous row-major with no padding, so the
    result indexes as plain ints and needs no per-pixel Python loop to build.
    """
    return image.convert('L').resize((size, size), _RESAMPLE).tobytes()


def _average_hash(grid):
    """64-bit average hash of an 8x8 luminance grid from _luminance_grid.

    One bit set per cell brighter than that frame's own mean. Bit i is cell i
    in row-major order; the absolute positions don't matter, only that two
    hashes assign them consistently.

    Known limitation, recorded because it looks like a bug when first met: on a
    nearly uniform frame (a plain dark cover) every cell sits close to the
    mean, so encoder noise flips many bits and distances come out large. That
    biases such videos toward 'video' -- a miss, never a false positive -- so
    it is safe, but it does mean the flattest album art is the hardest to
    detect.
    """
    mean = sum(grid) / len(grid)
    bits = 0
    for i, cell in enumerate(grid):
        if cell > mean:
            bits |= 1 << i
    return bits


def _hamming(a, b):
    """Number of differing bits between two hashes."""
    return (a ^ b).bit_count()


def _max_cell_delta(a, b):
    """Largest brightness change in any single cell between two grids.

    Deliberately a maximum and not an average: the whole point is to notice
    motion confined to a small part of the frame, which any frame-wide average
    would dilute into nothing.
    """
    return max(abs(x - y) for x, y in zip(a, b))


def _sample_frames(video_path, duration, samples=SAMPLE_FRAMES):
    """Sample frames across the middle 90% of the video.

    Returns a list of (average_hash, luminance_grid) pairs -- both measures come
    off the same decoded frame, so the finer grid costs no extra ffmpeg calls.
    Frames that fail to extract or decode are skipped rather than guessed at;
    the caller decides whether enough survived to judge on.

    The first and last 5% are excluded deliberately. Fades and leading black
    frames are near-identical to each other on a real video and identical on a
    static one, so including them biases the comparison toward 'static' on
    precisely the material this must not get wrong.
    """
    window_start = duration * 0.05
    window_span = duration * 0.90
    step = window_span / (samples - 1) if samples > 1 else 0.0

    frames = []
    for i in range(samples):
        png = _extract_frame_png(video_path, window_start + i * step)
        if not png:
            continue
        try:
            with Image.open(io.BytesIO(png)) as frame:
                norm = _normalise(frame)
                frames.append((_average_hash(_luminance_grid(norm, 8)),
                               _luminance_grid(norm, STATIC_GRID)))
        except Exception as e:
            log.error(f'frame decode error {video_path} (sample {i}): {e}')
    return frames


def probe_static_video(video_path):
    """Classify a video as 'static', 'near_static', 'video', or 'unknown'.

    'static'      -- effectively one held image. Safe to convert to album art.
    'near_static' -- a slow zoom, spinning vinyl, a visualiser. Reported for
                     review, never acted on.
    'video'       -- real footage.
    'unknown'     -- could not be judged. Every caller treats this exactly like
                     'video'; it is a separate status only so the log
                     distinguishes "looked, and it's real" from "couldn't look".

    Every error path returns 'unknown'. This function is the gate in front of an
    irreversible delete, so it must never guess in the acting direction.
    """
    verdict, _duration = _probe_static_video_verdict(video_path)
    return verdict


def _probe_static_video_verdict(video_path):
    """Same classification as probe_static_video(), but also returns the
    duration ffprobe already measured along the way (or None), so a caller
    about to extract a frame at the video's midpoint (convert_to_album_art)
    can reuse it instead of spending a second ffprobe call on the same file."""
    video_path = Path(video_path)
    if not video_path.exists():
        return 'unknown', None
    if Image is None:
        log.error('Pillow unavailable - cannot judge static video %s', video_path)
        return 'unknown', None

    duration, bit_rate = _probe_duration_and_bitrate(video_path)
    if duration is None:
        return 'unknown', None
    if duration < MIN_PROBE_SECONDS:
        log.info('static-art probe %s: unknown (%.1fs is below the %ds floor)',
                 video_path.name, duration, MIN_PROBE_SECONDS)
        return 'unknown', duration

    # Cheap prefilter. This can only ever conclude 'video' -- bitrate alone
    # cannot tell a still from a dark, slow scene, so it is never allowed to
    # conclude 'static'. An absent bit_rate falls through to the real analysis.
    if bit_rate is not None and bit_rate > PREFILTER_BITRATE_CEILING:
        log.info('static-art probe %s: video (bitrate %d bps is above the still ceiling)',
                 video_path.name, bit_rate)
        return 'video', duration

    frames = _sample_frames(video_path, duration)
    if len(frames) < MIN_SAMPLES_REQUIRED:
        log.error('static-art probe %s: unknown (only %d of %d frames were readable)',
                  video_path.name, len(frames), SAMPLE_FRAMES)
        return 'unknown', duration

    pairs = list(itertools.combinations(frames, 2))
    distance = max(_hamming(a[0], b[0]) for a, b in pairs)
    cell_delta = max(_max_cell_delta(a[1], b[1]) for a, b in pairs)

    # Acting requires both measures to agree that nothing moves. The hash rules
    # out frame-wide change and the cell delta rules out local change; either
    # one alone has a blind spot the other covers. A frame-wide match with a
    # local spike is the interesting middle -- a locked-off shot, a visualiser,
    # a lyric video -- so it lands in the report-only tier rather than being
    # written off, but it is never acted on.
    if distance <= STATIC_STRICT_DISTANCE and cell_delta <= STATIC_MAX_CELL_DELTA:
        verdict = 'static'
    elif distance <= STATIC_LOOSE_DISTANCE:
        verdict = 'near_static'
    else:
        verdict = 'video'

    log.info('static-art probe %s: %s (hash distance %d, max cell delta %d, over %d frames)',
             video_path.name, verdict, distance, cell_delta, len(frames))
    return verdict, duration


def _find_album_art(song_dir):
    """The song's existing album art file, or None."""
    song_dir = Path(song_dir)
    for name in ALBUM_ART_NAMES:
        candidate = song_dir / name
        if candidate.exists():
            return candidate
    return None


def _write_static_art_marker(song_dir):
    """Record backstagehero_video = static_art in song.ini. True on success.

    Imported lazily because VideoDownload imports this module for its
    download-time gate; at module scope this would be a cycle. set_ini_values
    is the project's byte-preserving song.ini writer and deliberately isn't
    duplicated here -- it only rewrites the lines it owns, leaving comments,
    casing and key order intact.
    """
    try:
        import VideoDownload
        return bool(VideoDownload.set_ini_values(
            song_dir, {VIDEO_MARKER_KEY: VIDEO_MARKER_STATIC_ART}))
    except Exception as e:
        log.error(f'could not write static-art marker for {song_dir}: {e}')
        return False


def convert_to_album_art(song_dir, dry_run=False):
    """Turn a song's static video into album art. Returns {'status', 'detail'}.

    Statuses: 'ok' (real video, or no video present), 'converted',
    'near_static' (reported only), 'unknown' (could not judge -- video kept),
    'failed' (something went wrong -- video kept).

    The order of operations matters more than anything else in this module. The
    video is deleted LAST, after the frame has been written and the song.ini
    marker has been committed. Every step that can fail happens while the video
    is still on disk, so a failure anywhere leaves a song that still plays -- at
    worst with a redundant album.png. Deleting first and failing afterwards
    would destroy the video and leave the song still looking like it needs one,
    re-downloading the same static upload on every run forever.

    dry_run=True returns the same status and detail without touching the
    filesystem at all.
    """
    song_dir = Path(song_dir)
    video_path = library_common.find_video_file(song_dir)
    if video_path is None:
        return {'status': 'ok', 'detail': 'no video file'}

    verdict, duration = _probe_static_video_verdict(video_path)
    if verdict == 'video':
        return {'status': 'ok', 'detail': ''}
    if verdict == 'unknown':
        return {'status': 'unknown',
                'detail': f'{video_path.name}: could not be judged - video kept'}
    if verdict == 'near_static':
        return {'status': 'near_static',
                'detail': f'{video_path.name}: nearly static - reported only, not converted'}

    existing_art = _find_album_art(song_dir)

    if dry_run:
        art_note = (f'keeping existing {existing_art.name}' if existing_art
                    else 'extracting album.png from it')
        return {'status': 'converted',
                'detail': f'{video_path.name}: static album art - would remove video, '
                          f'{art_note} (dry-run, not applied)'}

    # 1. Extract the frame to a temp file beside the target. A song that
    #    already has art keeps it -- the art on disk was chosen deliberately,
    #    a frame grab is only ever a fallback.
    art_tmp = None
    if existing_art is None:
        # duration came from the verdict probe above -- a 'static' verdict
        # always has one, but the check stays as a defensive fallback rather
        # than assuming that invariant holds forever.
        if duration is None:
            return {'status': 'failed',
                    'detail': f'{video_path.name}: could not re-probe duration - video kept'}
        png = _extract_frame_png(video_path, duration / 2.0)
        if not png:
            return {'status': 'failed',
                    'detail': f'{video_path.name}: frame extraction failed - video kept'}
        art_tmp = song_dir / 'album.png.tmp'
        try:
            art_tmp.write_bytes(png)
        except OSError as e:
            log.error(f'could not write album art for {song_dir}: {e}')
            return {'status': 'failed',
                    'detail': f'{video_path.name}: album art could not be written - video kept'}

    # 2. Promote the art to its final name BEFORE the marker is committed.
    #    This used to run after, and unguarded: a failing os.replace (a lock,
    #    a full disk, a long path) then threw with the marker already written,
    #    leaving a state with no name in the vocabulary above -- marker set,
    #    video still present, no album.png, an orphaned .tmp -- and one that
    #    process_download's video-exists check permanently skips, so only the
    #    library scan could ever heal it. Promoting first means a failure here
    #    lands in the same clean, already-tested place as a failed extraction.
    if art_tmp is not None:
        try:
            os.replace(art_tmp, song_dir / 'album.png')
        except OSError as e:
            log.error(f'could not finalise album art for {song_dir}: {e}')
            art_tmp.unlink(missing_ok=True)
            return {'status': 'failed',
                    'detail': f'{video_path.name}: album art could not be finalised - video kept'}

    # 3. Commit the marker. Without it the song reads as "still needs a video"
    #    and the same static upload comes back on the next run, so a marker we
    #    couldn't write means we must not proceed -- a song.ini with no [song]
    #    section stops us here, with the video still on disk. Undo the art we
    #    just promoted so a failed conversion leaves nothing behind; only art
    #    this call created is removed (art_tmp is None when the song already
    #    had its own), so a user's existing album.png is never touched.
    if not _write_static_art_marker(song_dir):
        if art_tmp is not None:
            (song_dir / 'album.png').unlink(missing_ok=True)
        return {'status': 'failed',
                'detail': f'{video_path.name}: song.ini has no [song] section - video kept'}

    # 4. Only now, with the art and the marker both committed, remove the
    #    video. Failing here is harmless: the song is already marked, so it
    #    won't re-download, and a later scan will retry the delete.
    try:
        video_path.unlink()
    except OSError as e:
        log.error(f'could not remove static video {video_path}: {e}')
        return {'status': 'failed',
                'detail': f'{video_path.name}: art and marker written, but the video '
                          f'could not be removed ({e})'}

    art_note = f'kept existing {existing_art.name}' if existing_art else 'extracted album.png'
    return {'status': 'converted',
            'detail': f'{video_path.name}: static album art - video removed, {art_note}'}


def scan_and_convert_static_art_library(home_folder, dry_run=False):
    """Scan every song folder under home_folder and convert static-art videos.

    Standalone pass over videos already on disk, sharing convert_to_album_art
    with the inline download-time gate in VideoDownload.process_download --
    same detector, same fail-safe rules, just walking an existing library
    instead of judging a video the moment it's downloaded.

    Returns the counts dict (status -> number of songs), matching
    video_repair.scan_and_repair_video_library's shape so the GUI can build
    its own summary without re-parsing printed output.
    """
    library_common.make_console_encoding_safe()
    print('=' * 70)
    print('SCANNING FOR STATIC ALBUM-ART VIDEOS' + (' (DRY RUN)' if dry_run else ''))
    print('=' * 70)

    counts = {}
    # recursive, matching the app's own **/song.ini discovery -- a one-level
    # walk finds zero songs in a Songs/<Pack>/<Song>/ library
    for folder in library_common.iter_song_folders(home_folder):
        try:
            label = str(folder.relative_to(home_folder))
        except ValueError:
            label = folder.name
        result = convert_to_album_art(folder, dry_run=dry_run)
        counts[result['status']] = counts.get(result['status'], 0) + 1
        if result['status'] in ('converted', 'near_static', 'failed'):
            print(f"  {label}: {result['detail']}")

    print()
    print(
        f"Scan complete: {counts.get('ok', 0)} ok, "
        f"{counts.get('converted', 0)} converted to album art, "
        f"{counts.get('near_static', 0)} near-static (reported only), "
        f"{counts.get('unknown', 0)} could not be judged, "
        f"{counts.get('failed', 0)} failed."
    )
    print('=' * 70)
    print()
    return counts
