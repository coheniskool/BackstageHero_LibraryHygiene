# video_repair.py
# Closes the one correctness gap BackstageHero's own download flow doesn't
# cover: Variable Frame Rate (VFR) source video, which causes progressive
# audio/video desync that a single static video_start_time offset can't fix
# (it only corrects the start point, not a drift that grows over the
# video's duration). Also repairs unplayable video codecs (VP9/AV1 WebM)
# already sitting in an existing library from other tools. Ported from
# clonehero-video-downloader's clonehero_video_offset.py.

import json
import logging
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import library_common

log = logging.getLogger('backstagehero')

# Each video's repair is an independent ffprobe/ffmpeg subprocess pipeline,
# so running them serially across a whole library means the wall-clock cost
# is (per-video probe+encode time) x (video count). A bounded pool overlaps
# those subprocess calls instead -- matches gui.py's own ThreadPoolExecutor
# convention for resolution probing (_probe_resolutions).
REPAIR_WORKERS = 4

# Suppress the console window each ffmpeg/ffprobe child would otherwise
# flash on a windowed (--noconsole) build; harmless 0 elsewhere. Matches
# VideoDownload.py's NO_WINDOW convention.
_TEXT_UTF8 = library_common.TEXT_UTF8   # see the note on TEXT_UTF8 there
_NO_WINDOW =getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def probe_frame_rate(video_path):
    """True if video_path is Variable Frame Rate (VFR), False for CFR or on any failure.

    r_frame_rate (the stream's nominal/container rate) and avg_frame_rate
    (the actual average over the whole stream) disagree exactly when the
    video is VFR; they match for CFR.
    """
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=r_frame_rate,avg_frame_rate',
             '-of', 'json', str(video_path)],
            check=True, capture_output=True, creationflags=_NO_WINDOW,
            **_TEXT_UTF8,
        )
        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        if not streams:
            return False
        stream = streams[0]
        return stream.get('r_frame_rate', '0/0') != stream.get('avg_frame_rate', '0/0')
    except Exception as e:
        log.error(f'ffprobe VFR probe error {video_path}: {e}')
        return False


def probe_video_codec(video_path):
    """The video stream's codec name (e.g. 'h264', 'vp8', 'vp9'), or None on failure.

    Used to catch WebM files encoded with VP9 -- YouTube's default for
    "bestvideo[ext=webm]" on virtually all current uploads -- which this
    Clone Hero build cannot decode at all (confirmed via a real playtest:
    "Unsupported video codec 'VP9'", video never renders). Only VP8 is safe.
    """
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name',
             '-of', 'json', str(video_path)],
            check=True, capture_output=True, creationflags=_NO_WINDOW,
            **_TEXT_UTF8,
        )
        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        if not streams:
            return None
        return streams[0].get('codec_name')
    except Exception as e:
        log.error(f'ffprobe codec probe error {video_path}: {e}')
        return None


def _probe_video_info(video_path):
    """(codec_name_or_None, is_vfr) for a video, in one ffprobe call.

    probe_video_codec() and probe_frame_rate() each ask ffprobe a separate,
    single-purpose question; ensure_playable() used to call both of them,
    spending two subprocess spawns on the same file when it needed both
    answers (a webm being checked for both codec support and VFR). This asks
    for codec_name, r_frame_rate and avg_frame_rate together so that caller
    gets both facts from one probe. Fails safe the same way each individual
    probe does: (None, False) on any error, so a caller falls through to
    "leave it alone" rather than acting on a guess.
    """
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name,r_frame_rate,avg_frame_rate',
             '-of', 'json', str(video_path)],
            check=True, capture_output=True, creationflags=_NO_WINDOW,
            **_TEXT_UTF8,
        )
        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        if not streams:
            return None, False
        stream = streams[0]
        codec = stream.get('codec_name')
        is_vfr = stream.get('r_frame_rate', '0/0') != stream.get('avg_frame_rate', '0/0')
        return codec, is_vfr
    except Exception as e:
        log.error(f'ffprobe combined probe error {video_path}: {e}')
        return None, False


def reencode_to_cfr(video_path, fps=30):
    """Overwrite video_path in place with a constant-frame-rate re-encode.

    No backup kept (keeps disk usage flat for a large library). Writes to a
    temp file in the same directory first so a crash mid-encode can't leave
    a partial/corrupt file at the real path.
    """
    video_path = Path(video_path)
    fd, tmp_path = tempfile.mkstemp(dir=str(video_path.parent), suffix=video_path.suffix)
    os.close(fd)
    try:
        subprocess.run(
            ['ffmpeg', '-nostdin', '-i', str(video_path), '-r', str(fps), '-y', tmp_path],
            check=True, capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
        os.replace(tmp_path, str(video_path))
        return True
    except Exception as e:
        log.error(f'CFR re-encode error {video_path}: {e}')
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def ensure_playable(video_path, *, allow_codec_removal=False, dry_run=False):
    """Repair one video file in place. Returns {'status': ..., 'detail': ...}.

    Always VFR-detects and CFR-re-encodes if VFR is found. When
    allow_codec_removal is True (the standalone library scan only -- never
    the inline post-download hook, since BackstageHero's own downloads are
    always safe remuxed AVC), also removes a non-VP8 WebM file entirely
    rather than trying to re-encode it in place; the caller notices the
    video is now missing and it gets picked up for a fresh download.

    dry_run=True computes and returns the same status/detail without
    unlinking or re-encoding anything -- the reported outcome describes
    what WOULD happen.

    Statuses: 'ok' (no action needed, or no file at video_path),
    'reencoded_cfr' (was VFR, now CFR), 'reencode_failed',
    'removed_unsupported_codec' (allow_codec_removal only).
    """
    video_path = Path(video_path)
    if not video_path.exists():
        return {'status': 'ok', 'detail': 'no video file'}

    # One ffprobe call for both facts this function needs, instead of a
    # separate codec probe and frame-rate probe (finding B15).
    codec, is_vfr = _probe_video_info(video_path)

    if allow_codec_removal and video_path.suffix.lower() == '.webm':
        if codec is not None and codec != 'vp8':
            if not dry_run:
                video_path.unlink()
            detail = f'{video_path.name} ({codec}) removed -- needs a fresh download'
            if dry_run:
                detail += ' (dry-run, not applied)'
            return {'status': 'removed_unsupported_codec', 'detail': detail}

    if is_vfr:
        if dry_run:
            return {'status': 'reencoded_cfr',
                     'detail': f'{video_path.name}: VFR -> CFR (dry-run, not applied)'}
        if reencode_to_cfr(video_path):
            return {'status': 'reencoded_cfr', 'detail': f'{video_path.name}: VFR -> CFR'}
        return {'status': 'reencode_failed', 'detail': f'{video_path.name}: CFR re-encode failed, left as-is'}

    return {'status': 'ok', 'detail': ''}


def scan_and_repair_video_library(home_folder, dry_run=False):
    """Scan every song folder under home_folder and repair VFR/unsupported-codec videos.

    Standalone pass (opt-in, not run automatically at download time) --
    checks every recognized video filename (library_common.VIDEO_NAMES), not
    just BackstageHero's own video.mp4, since a real library can contain
    files left by other tools or this project's predecessor.

    Returns the counts dict (status -> number of videos), so a caller (e.g.
    the GUI) can build its own summary without re-parsing printed output.

    Runs each video's repair on a bounded thread pool (REPAIR_WORKERS):
    every video's ffprobe/ffmpeg work is independent, so this used to spend
    (probe+encode time) x (video count) fully serially. One video's repair
    raising must never lose the rest of the batch -- caught per-future below
    and recorded as 'reencode_failed', same bucket ensure_playable() itself
    uses for a failed re-encode. Progress lines print in COMPLETION order
    now, not folder-scan order; the returned counts are still exact
    regardless of that order.
    """
    library_common.make_console_encoding_safe()
    print('=' * 70)
    print('SCANNING VIDEO LIBRARY FOR VFR / UNSUPPORTED CODECS' + (' (DRY RUN)' if dry_run else ''))
    print('=' * 70)

    counts = {}
    # recursive, matching the app's own **/song.ini discovery -- a one-level
    # walk finds zero songs in a Songs/<Pack>/<Song>/ library. Every
    # recognized video file, not just the first -- a folder with a good
    # video.mp4 can still hold a stale VP9 video.webm from another tool, and
    # checking only find_video_file()'s first hit would leave that one
    # unexamined forever.
    videos = [(folder, folder / name)
              for folder in library_common.iter_song_folders(home_folder)
              for name in library_common.VIDEO_NAMES
              if (folder / name).exists()]

    with ThreadPoolExecutor(max_workers=REPAIR_WORKERS) as pool:
        futures = {pool.submit(ensure_playable, video,
                               allow_codec_removal=True, dry_run=dry_run): (folder, video)
                   for folder, video in videos}
        for fut in as_completed(futures):
            folder, video = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                result = {'status': 'reencode_failed',
                          'detail': f'{video.name}: unexpected error ({e})'}
            counts[result['status']] = counts.get(result['status'], 0) + 1
            if result['status'] in ('reencoded_cfr', 'removed_unsupported_codec', 'reencode_failed'):
                print(f"  {folder.name}: {result['detail']}")

    print()
    print(
        f"Scan complete: {counts.get('ok', 0)} ok, "
        f"{counts.get('reencoded_cfr', 0)} re-encoded (VFR -> CFR), "
        f"{counts.get('removed_unsupported_codec', 0)} removed (unsupported codec), "
        f"{counts.get('reencode_failed', 0)} re-encode failed."
    )
    print('=' * 70)
    print()
    return counts
