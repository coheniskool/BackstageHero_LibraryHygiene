import os
import sys

# A console-less launch (pythonw.exe, or a --noconsole frozen build without
# the bootloader's own stdio shim covering every case) sets sys.stdout/
# stderr to None, not just closed -- the first print() or warnings.warn()
# anywhere in this module or a dependency then crashes immediately with no
# visible error. This is the file build.py's PyInstaller invocation
# actually targets, so the guard belongs here, not only in gui.py. Must run
# before any other import.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

import configparser
import glob
import logging
import logging.handlers
import random
import re
import shutil
import subprocess
import time

# updater goes first so a cached newer yt-dlp gets onto sys.path before yt_dlp loads
import updater
updater.prefer_cached_ytdlp()


def _setup_logging():
    """Rotating log file in the data dir. The packaged exe is --noconsole so
    print() goes nowhere; this is the only way to see what broke. From source it
    also prints to the console."""
    lg = logging.getLogger('backstagehero')
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S')
    try:
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(updater.data_dir(), 'log.txt'),
            maxBytes=512 * 1024, backupCount=2, encoding='utf-8')
        fh.setFormatter(fmt)
        lg.addHandler(fh)
    except Exception:
        pass
    if not getattr(sys, 'frozen', False):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        lg.addHandler(sh)
    return lg


log = _setup_logging()

import yt_dlp
from tqdm import tqdm

import resolver_client
import video_repair

__version__ = '2.2.0'

try:
    import audiosync
except Exception:
    audiosync = None

# make bundled ffmpeg findable when running as a frozen exe
if getattr(sys, 'frozen', False):
    exe_dir = os.path.dirname(sys.executable)
    bundle_dir = getattr(sys, '_MEIPASS', exe_dir)
    os.environ['PATH'] = exe_dir + os.pathsep + bundle_dir + os.pathsep + os.environ.get('PATH', '')

ffmpegAvailable = shutil.which('ffmpeg') is not None
ffplayPath      = shutil.which('ffplay')

# On a windowed (--noconsole) build, every child process would otherwise pop up
# a console window for a split second. This flag suppresses that on Windows and
# is a harmless 0 everywhere else.
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# tv_embedded gets DASH streams (needed for bestvideo above 360p) without PO tokens.
# android_vr / android are fallbacks if embedded is blocked.
YOUTUBE_CLIENTS = ['tv_embedded', 'android_vr', 'android']

# strip these from titles before searching so we don't get playthrough results
# longer forms first, otherwise 'RB3' strips inside '(RB3 version)' and leaves '( version)'
TITLE_ISSUES = ['(2x Bass Pedal Expert+)', '(2x Bass Pedal)', '(RB3 version)', '(Rh)', 'RB3']

# leftover files from interrupted runs - safe to delete, video.mp4 is never in here
TEMP_ARTIFACTS = [
    'video.download.mp4', 'video.download.mp4.part', 'video.download.mp4.ytdl',
    'video.tmp.mp4', 'output.mp4', 'video.mp4.part', 'video.mp4.ytdl',
    'song.ini.tmp',
]

# how many search results to pull, and how many to fingerprint-test before falling back
SEARCH_RESULTS = 5
GATE_CANDIDATES = 3

# random pause between songs so it doesn't look like a bot hammering the API
SONG_DELAY_MIN = 1.0
SONG_DELAY_MAX = 3.0

# if YouTube rate-limits us, wait and retry the same song before giving up
BOT_BACKOFF_SECONDS = [60, 180, 420]

# default lead-in when we can't fingerprint-match - most music videos have a few seconds of intro
DEFAULT_START_TIME = -3000

# How the video_start_time we wrote was arrived at, recorded alongside it as
# `backstagehero_sync`. Without this, a written offset is unfalsifiable: a real
# measurement that happens to land near the default is byte-identical to a pure
# guess, so there's no way to tell which songs are actually synced and which are
# just running on DEFAULT_START_TIME. (Found the hard way -- a song reported as
# out of sync turned out to have never been matched at all; its -3000 was the
# fallback constant, indistinguishable from a measured value on inspection.)
# Deliberately kept separate from the offset itself so playback is unaffected:
# the guess is still the best guess, it's just now labelled as one.
SYNC_MEASURED  = 'measured'    # audiosync fingerprint-matched this exact video
SYNC_COMMUNITY = 'community'   # offset came from the resolver pool, not measured here
SYNC_GUESS     = 'guess'       # never measured - DEFAULT_START_TIME fallback
SYNC_MANUAL    = 'manual'      # user set it by hand in the sync editor

_BOT_SIGNS = ('sign in to confirm', "you're not a bot", 'not a bot',
              'http error 429', 'too many requests')


class BotDetected(Exception):
    """Raised when YouTube returns a bot-challenge / rate-limit response."""


def is_bot_error(exc):
    msg = str(exc).lower()
    return any(sign in msg for sign in _BOT_SIGNS)


def quality_format(max_height):
    """yt-dlp format string: best AVC video-only stream up to max_height.
    Video-only because Clone Hero plays its own audio anyway, and it lets us
    pick a proper 720p/1080p instead of whatever progressive stream exists."""
    return (
        f'bestvideo[height<={max_height}][ext=mp4][vcodec^=avc]/'
        f'bestvideo[height<={max_height}][vcodec^=avc]/'
        f'best[height<={max_height}][ext=mp4]/'
        f'best[height<={max_height}]/'
        f'best[ext=mp4]/best'
    )


def cleanup_temp_files(folder):
    """Remove leftover temp files from an interrupted download. Leaves video.mp4 alone."""
    for name in TEMP_ARTIFACTS:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    for path in glob.glob(os.path.join(folder, 'video.sync.*')):
        try:
            os.remove(path)
        except OSError:
            pass


def _read_text(path):
    # utf-8-sig handles the BOM some song.ini/.chart files have
    with open(path, encoding='utf-8-sig', errors='replace') as f:
        return f.read()


def _parse_chart(path):
    """Returns (artist, title) from a notes.chart [Song] block."""
    try:
        text = _read_text(path)
    except Exception:
        return None, None
    m = re.search(r'\[Song\](.*?)(?:\n\s*\[|\Z)', text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None, None
    body = m.group(1)
    fields = {}
    for line in body.splitlines():
        if '=' not in line:
            continue
        key, val = line.split('=', 1)
        fields[key.strip().lower()] = val.strip().strip('"').strip()
    return (fields.get('artist') or None), (fields.get('name') or None)


def read_metadata(folder):
    """Get artist/title for a song folder.
    Checks song.ini first, falls back to notes.chart, then the folder name."""
    artist, title, _ = scan_song(folder)
    return artist, title


def scan_song(folder):
    """(artist, title, stored_res) for a song folder in a single song.ini parse.
    The library scan calls this once per song, so the file is only opened once
    instead of once for the metadata and again for the resolution."""
    res = None
    ini = os.path.join(folder, 'song.ini')
    if os.path.exists(ini):
        try:
            cp = configparser.ConfigParser(strict=False, interpolation=None)
            cp.read_string(_read_text(ini))
            for sec in cp.sections():
                if sec.lower() == 'song':
                    artist = (cp.get(sec, 'artist', fallback='') or '').strip()
                    title = (cp.get(sec, 'name', fallback='') or '').strip()
                    res = cp.get(sec, 'backstagehero_res', fallback='').strip() or None
                    if title:
                        return (artist or None), title, res
                    break
        except Exception:
            pass
    chart = os.path.join(folder, 'notes.chart')
    if os.path.exists(chart):
        artist, title = _parse_chart(chart)
        if title:
            return artist, title, res
    return None, os.path.basename(folder), res


def build_query(artist, title):
    clean = title or ''
    for issue in TITLE_ISSUES:
        clean = clean.replace(issue, '')
    # clean up empty brackets and extra spaces left by the removals
    clean = re.sub(r'\(\s*\)', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    if artist:
        return f'{artist} {clean}'.strip()
    return clean


def is_converted(folder):
    """True if the song.ini has a //Converted marker (Phase Shift export - leave it alone)."""
    path = os.path.join(folder, 'song.ini')
    try:
        return '//Converted' in _read_text(path)
    except Exception:
        return False


def set_ini_values(folder, values):
    """Write keys into song.ini's [song] section, touching only those lines.
    Preserves everything else - comments, casing, order. Returns False if no [song] section."""
    path = os.path.join(folder, 'song.ini')
    try:
        text = _read_text(path)
    except Exception:
        return False

    lines = text.splitlines(keepends=True)
    sec_start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == '[song]':
            sec_start = i
            break
    if sec_start is None:
        return False

    sec_end = len(lines)
    for i in range(sec_start + 1, len(lines)):
        if lines[i].lstrip().startswith('['):
            sec_end = i
            break

    remaining = dict(values)
    for i in range(sec_start + 1, sec_end):
        line = lines[i]
        if '=' not in line:
            continue
        key = line.split('=', 1)[0].strip().lower()
        if key in remaining:
            nl = '\n' if line.endswith('\n') else ''
            lines[i] = f'{key} = {remaining.pop(key)}{nl}'

    if remaining:
        if not lines[sec_start].endswith('\n'):
            lines[sec_start] += '\n'
        inserts = [f'{k} = {v}\n' for k, v in remaining.items()]
        lines[sec_start + 1:sec_start + 1] = inserts

    tmp = os.path.join(folder, 'song.ini.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(''.join(lines))
    os.replace(tmp, path)
    return True


def video_id_of(url):
    m = re.search(r'[?&]v=([^&]+)', url)
    return m.group(1) if m else url


def _base_opts():
    return {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': 1,
        'extractor_args': {'youtube': {'player_client': YOUTUBE_CLIENTS}},
        'sleep_interval_requests': 1,
    }


def search_candidates(query, n=SEARCH_RESULTS):
    """Top results as (url, title, duration_seconds) tuples, most-relevant first.
    Duration comes free with the flat search and may be None."""
    opts = _base_opts()
    opts['extract_flat'] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f'ytsearch{n}:{query}', download=False)
    except Exception as e:
        if is_bot_error(e):
            raise BotDetected(str(e))
        raise
    entries = (info or {}).get('entries') or []
    candidates = []
    for entry in entries:
        if entry and entry.get('id'):
            url = f"https://www.youtube.com/watch?v={entry['id']}"
            candidates.append((url, entry.get('title', 'Unknown'),
                               entry.get('duration')))
    if not candidates:
        raise Exception('No search results found for: ' + query)
    return candidates


def fetch_audio(folder, url):
    """Download audio-only for fingerprinting. Returns (path, max_video_height,
    info_dict), or (None, 0, None) on failure.

    Grabs a low-bitrate stream on purpose: the fingerprinter analyses at 8 kHz
    mono, so a ~50-70kbps opus carries everything it can use at a fraction of
    the bytes (and requests) of bestaudio. Quality floor first, then whatever.

    The max video height and the full info dict come free out of the same
    extraction: the height lets select_video prefer a higher-res source, and
    the info dict (with every format URL in it) lets download_video skip a
    second extraction of the same video."""
    cleanup_temp_files(folder)
    opts = _base_opts()
    opts.update({'format': 'bestaudio[abr<=80]/worstaudio/bestaudio/best',
                 'outtmpl': os.path.join(folder, 'video.sync.%(ext)s')})
    max_h = 0
    info = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info:
            heights = [f.get('height') for f in (info.get('formats') or [])
                       if f.get('height')]
            max_h = max(heights) if heights else 0
    except Exception as e:
        if is_bot_error(e):
            raise BotDetected(str(e))
        return None, 0, None
    matches = glob.glob(os.path.join(folder, 'video.sync.*'))
    return (matches[0] if matches else None), max_h, info


def _chart_duration(folder):
    """Length of the chart audio in seconds (longest stem), or None. One local
    ffmpeg call, used to throw out search results that can't be the right song."""
    if not ffmpegAvailable or audiosync is None:
        return None
    stems = audiosync.chart_stems(folder)
    if not stems:
        return None
    # song.ogg is the safest bet for full length, otherwise the biggest file
    pick = next((s for s in stems if os.path.basename(s).lower().startswith('song.')),
                None) or max(stems, key=os.path.getsize)
    try:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-i', pick],
                           capture_output=True, text=True, timeout=10,
                           creationflags=NO_WINDOW)
        m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', r.stderr)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return None


def download_video(folder, url, quality, info=None):
    """Download video to a temp file, then rename to video.mp4 once complete.

    If `info` is the info dict from an earlier extraction of the same URL
    (select_video already pulled one to fingerprint the audio), the format
    URLs in it are reused instead of asking YouTube to extract the video all
    over again - one less round of API requests per song, which is exactly
    what the rate limiter punishes. Falls back to a fresh extraction if the
    reuse fails for any reason."""
    cleanup_temp_files(folder)
    dl = os.path.join(folder, 'video.download.mp4')
    opts = _base_opts()
    opts.update({
        'outtmpl': dl,
        'format': quality,
        'nooverwrites': 0,
        'sleep_interval': 1,
        'max_sleep_interval': 3,
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            done = False
            if info:
                try:
                    # drop the audio run's selections, keep the format list
                    clean = {k: v for k, v in info.items()
                             if not k.startswith('requested')}
                    ydl.process_ie_result(clean, download=True)
                    done = True
                except Exception:
                    log.info('Cached extraction reuse failed; extracting fresh',
                             exc_info=True)
                    cleanup_temp_files(folder)
            if not done:
                ydl.download([url])
    except Exception as e:
        if is_bot_error(e):
            raise BotDetected(str(e))
        raise

    final = os.path.join(folder, 'video.mp4')
    if not ffmpegAvailable:
        # AVC download plays fine in Clone Hero as-is
        os.replace(dl, final)
        return

    print('  Formatting downloaded video for Clone Hero')
    tmp = os.path.join(folder, 'video.tmp.mp4')
    try:
        # Stream-copy into a clean mp4 container - no re-encode, just a remux.
        subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-i', dl, '-c', 'copy', '-y', tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW, check=True)
    except Exception as e:
        log.warning('Remux failed (%s); keeping raw download', e)
        print('  Could not remux (using raw download instead). Error: ' + str(e))
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        os.replace(dl, final)
    else:
        os.remove(dl)
        os.replace(tmp, final)
        print('  Video ready')

    # VFR source video causes progressive audio/video desync that a single
    # static video_start_time offset can't fix -- BackstageHero's own remux
    # step above doesn't touch frame timing, only the container.
    repair = video_repair.ensure_playable(final, allow_codec_removal=False)
    if repair['status'] == 'reencoded_cfr':
        print('  Video was variable-frame-rate; re-encoded to constant frame rate for sync accuracy')
    elif repair['status'] == 'reencode_failed':
        log.warning('CFR re-encode failed for %s', final)


def select_video(folder, candidates, sync_ready, target_h=0):
    """Pick the right candidate. With sync available, fingerprints each one against
    the chart audio until we get a confident match.
    Returns (url, title, offset_ms, matched, confidence, info_dict) - info_dict
    is the winner's cached extraction so the download can reuse it, or None.
    url is None if nothing fingerprint-confirmed AND even the best
    duration-ranked candidate is implausible for this chart's length -- the
    caller should leave the song without a video rather than attach a
    confidently-wrong one. (Real case found in testing: with no confirmed
    match, the old fallback used the raw top search result with zero
    duration check at all, and attached a completely unrelated video -- a
    different artist's static album-art upload -- to a chart it shares no
    content with.)

    target_h is the resolution the user asked for. Among the candidates that
    match the song, a match that actually has that resolution wins; if the first
    match is lower-res we keep looking for a better one and only settle for it
    if nothing higher turns up. Stops as soon as a match meets the target, so a
    good first hit costs nothing extra."""
    if not sync_ready:
        url, title = candidates[0][0], candidates[0][1]
        return url, title, DEFAULT_START_TIME, False, 0.0, None

    # no stems in this folder = nothing to fingerprint against, so don't waste
    # audio downloads finding that out the hard way
    if audiosync is None or not audiosync.chart_stems(folder):
        url, title = candidates[0][0], candidates[0][1]
        return url, title, DEFAULT_START_TIME, False, 0.0, None

    # rank by plausible length before spending downloads. the song has to fit
    # inside the video, plus some intro/outro, so a 30s short or a 20min live
    # set can't be it. unknown durations aren't punished. doesn't exclude
    # anything outright, just tries the believable ones first.
    chart_dur = _chart_duration(folder)
    if chart_dur:
        def is_plausible(dur):
            return dur is None or (chart_dur - 25) <= dur <= (chart_dur + 150)

        def rank(item):
            i, (_, _, dur) = item
            return (0 if is_plausible(dur) else 1, i)
        ordered = [c for _, c in sorted(enumerate(candidates), key=rank)]
    else:
        is_plausible = None
        ordered = list(candidates)

    best = None   # (url, title, ms, height, conf, vinfo) - best match so far
    for url, title, _ in ordered[:GATE_CANDIDATES]:
        audio, max_h, vinfo = fetch_audio(folder, url)
        try:
            if not audio:
                continue
            ms, info, conf = audiosync.compute_offset_ms(folder, audio)
        except BotDetected:
            raise
        except Exception as e:
            print('  Sync check failed for a candidate (' + str(e) + ')')
            ms = None
        finally:
            cleanup_temp_files(folder)
        if ms is None:
            continue
        if max_h >= target_h:
            print(f'  Matched: {title}  ({info}, {max_h or "?"}p)')
            return url, title, ms, True, conf, vinfo
        # right song but below the resolution we want - remember the best one
        # and keep looking for something higher
        if best is None or max_h > best[3]:
            best = (url, title, ms, max_h, conf, vinfo)
        print(f'  Matched {title} but only {max_h}p, looking for higher')

    if best is not None:
        print(f'  Using best available match ({best[3]}p)')
        return best[0], best[1], best[2], True, best[4], best[5]

    # nothing fingerprint-confirmed -- fall back to the best duration-ranked
    # candidate rather than the raw top search result (ordered already puts
    # a plausible-length candidate first when duration data exists), but
    # refuse entirely if even that one is implausible: a confidently-wrong
    # video is worse than no video at all.
    top_url, top_title, top_dur = ordered[0]
    if chart_dur and not is_plausible(top_dur):
        print(f'  No plausible match (best candidate is {top_dur}s vs chart ~{chart_dur:.0f}s) '
              '- leaving without a video')
        return None, None, DEFAULT_START_TIME, False, 0.0, None

    print('  No confident match - using best duration-ranked result with default offset')
    return top_url, top_title, DEFAULT_START_TIME, False, 0.0, None


def download_with_fallback(folder, primary_url, candidates, quality, info=None):
    """Try the primary URL, fall back through other candidates if it fails.
    `info` is the primary's cached extraction, if we have one."""
    urls = [primary_url] + [c[0] for c in candidates if c[0] != primary_url]
    last_err = None
    for url in urls:
        try:
            download_video(folder, url, quality,
                           info=info if url == primary_url else None)
            return url
        except BotDetected:
            raise
        except Exception as e:
            last_err = e
            log.warning('Download failed for %s (%s); trying next result', url, e)
            print('  Download failed (' + str(e) + '); trying next result')
            cleanup_temp_files(folder)
    raise last_err if last_err else Exception('No downloadable candidate')


def process_download(folder, song_name, quality, sync_ready, replace):
    # already got one? leave it alone unless we're told to replace. stops a
    # re-run (or a batch with already-done songs) from hammering YouTube for
    # nothing.
    if not replace and os.path.exists(os.path.join(folder, 'video.mp4')):
        return 'skipped'

    artist, title = read_metadata(folder)
    ch = resolver_client.chart_hash(folder) if resolver_client.enabled() else None

    # check the community resolver first - skips the YouTube search entirely for known charts
    hit = resolver_client.resolve(ch)
    if hit and hit.get('video_id'):
        url = 'https://www.youtube.com/watch?v=' + hit['video_id']
        print('\nResolved (community-confirmed): ' + (build_query(artist, title) or song_name))
        try:
            download_video(folder, url, quality)
            if is_converted(folder):
                print('  Phase Shift converter file detected - video kept, timing left as-is')
                return
            offset = hit.get('start_ms')
            # a resolver hit without a start_ms is a known video but an unknown
            # offset - the video is trustworthy, the timing is still a guess
            how = SYNC_COMMUNITY if offset is not None else SYNC_GUESS
            offset = DEFAULT_START_TIME if offset is None else offset
            if not set_ini_values(folder, {'video_start_time': str(offset),
                                            'backstagehero_sync': how,
                                            'backstagehero_source': hit['video_id']}):
                raise Exception('song.ini missing [song] section')
            _probe_and_store_resolution(folder)
            print('  Song ready (community offset). Next song...')
            return
        except BotDetected:
            raise
        except Exception as e:
            log.warning('Community video failed for %s (%s); searching instead', song_name, e)
            print('  Community video could not be downloaded (' + str(e) + '); searching instead')
            cleanup_temp_files(folder)

    # resolver miss - do a search and fingerprint-check candidates
    query = build_query(artist, title)
    print('\nLooking on YouTube for: ' + query)

    candidates = search_candidates(query)
    # the format string caps at height<=N; pass N so selection prefers a source
    # that actually has it
    m = re.search(r'height<=(\d+)', quality)
    target_h = int(m.group(1)) if m else 0
    url, vid_title, offset, matched, conf, vinfo = select_video(
        folder, candidates, sync_ready, target_h)
    if url is None:
        print('  No video attached - nothing plausible found. Will try again next run.')
        return
    print('Downloading: ' + vid_title)

    # old video stays until new one is fully downloaded, so nothing is left half-done
    used_url = download_with_fallback(folder, url, candidates, quality, info=vinfo)

    if is_converted(folder):
        print('  Phase Shift converter file detected - video kept, timing left as-is')
        return

    # offset was measured against `url`. if a fallback candidate downloaded
    # instead, that offset is for a different video, so drop it and don't report
    # it (otherwise we feed the community a bogus match).
    if matched and used_url != url:
        log.info('Fallback used a different video for %s; dropping fingerprint offset', song_name)
        offset, matched = DEFAULT_START_TIME, False

    vid = video_id_of(used_url)
    values = {'video_start_time': str(offset),
              'backstagehero_sync': SYNC_MEASURED if matched else SYNC_GUESS,
              'backstagehero_source': vid}
    if set_ini_values(folder, values):
        note = 'auto-synced' if matched else 'default offset'
        _probe_and_store_resolution(folder)
        print('  Song ready (' + note + '). Next song...')
    else:
        print('  Could not update song.ini (no [song] section?). Video kept.')
        raise Exception('song.ini missing [song] section')

    # only report confident fingerprint matches - no point voting on guesses.
    # the measured confidence goes with it so strong matches carry more weight
    # in the pool than borderline ones.
    if matched:
        resolver_client.report(ch, vid, offset, conf or 1.0, artist, title)


def process_resync(folder, song_name, sync_ready):
    """Re-fingerprint an existing video to fix its timing.

    Prefers the video file already on disk: ffmpeg decodes its audio track
    directly (compute_offset_ms/_decode don't care whether the input is a
    dedicated audio file or a muxed video), so the common case -- the video
    hasn't changed, only its stored timing needs a recheck -- costs zero
    network requests. Falls back to the stored YouTube source, then a fresh
    search, exactly as before, for a local video that's missing, corrupted,
    or genuinely no longer fingerprint-matches.
    """
    if not sync_ready:
        return
    if is_converted(folder):
        return

    artist, title = read_metadata(folder)

    local_video = os.path.join(folder, 'video.mp4')
    if os.path.exists(local_video):
        print('\nRe-syncing: ' + build_query(artist, title) + '  (from local video, no download)')
        ms, info, _ = audiosync.compute_offset_ms(folder, local_video)
        if ms is not None and set_ini_values(folder, {'video_start_time': str(ms),
                                                      'backstagehero_sync': SYNC_MEASURED}):
            print('  Re-synced: ' + info)
            return
        print('  Could not confirm sync from the local video - falling back to source lookup')

    source = get_stored_source(folder)

    if source:
        url = f'https://www.youtube.com/watch?v={source}'
        print('\nRe-syncing: ' + build_query(artist, title) + '  (known source)')
        audio, _, _ = fetch_audio(folder, url)
        try:
            ms, info, _ = (audiosync.compute_offset_ms(folder, audio)
                           if audio else (None, 'no audio', 0.0))
        finally:
            cleanup_temp_files(folder)
        if ms is not None and set_ini_values(folder, {'video_start_time': str(ms),
                                                      'backstagehero_sync': SYNC_MEASURED}):
            _probe_and_store_resolution(folder)
            print('  Re-synced: ' + info)
            return
        print('  Known source no longer matches - falling back to search')

    query = build_query(artist, title)
    print('\nRe-syncing: ' + query)
    candidates = search_candidates(query)
    _, vid_title, offset, matched, _, _ = select_video(folder, candidates, sync_ready)
    if matched and set_ini_values(folder, {'video_start_time': str(offset),
                                           'backstagehero_sync': SYNC_MEASURED}):
        print('  Re-synced against: ' + vid_title)
    else:
        print('  No confident match - timing left unchanged')


def _read_ini_value(folder, key):
    """Return a single [song] value from song.ini (stripped), or None."""
    path = os.path.join(folder, 'song.ini')
    if not os.path.exists(path):
        return None
    try:
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.read_string(_read_text(path))
    except Exception:
        return None
    for sec in cp.sections():
        if sec.lower() == 'song':
            return cp.get(sec, key, fallback='').strip() or None
    return None


def get_stored_source(folder):
    """The backstagehero_source video ID stored in song.ini, or None."""
    return _read_ini_value(folder, 'backstagehero_source')


def get_stored_resolution(folder):
    """The stored backstagehero_res value (e.g. '720p'), or None."""
    return _read_ini_value(folder, 'backstagehero_res')


def probe_resolution(folder):
    """Read video.mp4's height via ffmpeg, cache it in song.ini, and return it
    (e.g. '720p'), '?' if it couldn't be read, or None if there's nothing to
    probe. Shared by the download flow and the GUI's library scan."""
    if not ffmpegAvailable:
        return None
    video = os.path.join(folder, 'video.mp4')
    if not os.path.exists(video):
        return None
    try:
        r = subprocess.run(
            ['ffmpeg', '-hide_banner', '-i', video],
            capture_output=True, text=True, timeout=10,
            creationflags=NO_WINDOW)
        # Search only the Video: stream line so an embedded cover-art stream
        # (e.g. mjpeg 640x640) doesn't shadow the real video dimensions.
        video_line = next((l for l in r.stderr.splitlines() if 'Video:' in l), '')
        m = re.search(r'(\d{3,4})x(\d{3,4})', video_line or r.stderr)
        if m:
            res = f'{int(m.group(2))}p'
            set_ini_values(folder, {'backstagehero_res': res})
            return res
    except Exception:
        log.debug('Resolution probe failed for %s', folder, exc_info=True)
    return '?'


# Back-compat name used by the download flow (return value ignored there).
_probe_and_store_resolution = probe_resolution


def parse_selection(sel, maxn):
    """Parse '1,3,5-8' or 'a' into a sorted list of valid 1-based indices."""
    sel = sel.strip().lower()
    if sel in ('a', 'all'):
        return list(range(1, maxn + 1))
    out = set()
    for part in sel.replace(' ', '').split(','):
        if not part:
            continue
        if '-' in part:
            try:
                a, b = (int(x) for x in part.split('-', 1))
            except ValueError:
                continue
            for x in range(min(a, b), max(a, b) + 1):
                if 1 <= x <= maxn:
                    out.add(x)
        else:
            try:
                x = int(part)
            except ValueError:
                continue
            if 1 <= x <= maxn:
                out.add(x)
    return sorted(out)


def select_songs_manually(song_inis, page=50):
    """Terminal search-and-pick for large libraries. Returns chosen song.ini paths."""
    print('\nIndexing %d songs...' % len(song_inis))
    entries = []   # (filename, label, search_text, has_video)
    for filename in song_inis:
        folder = os.path.dirname(filename)
        artist, title = read_metadata(folder)
        label = build_query(artist, title) or os.path.basename(folder)
        search_text = (label + ' ' + os.path.basename(folder)).lower()
        has_video = os.path.exists(os.path.join(folder, 'video.mp4'))
        entries.append((filename, label, search_text, has_video))

    chosen = []
    print('Manual mode: search for songs, pick the ones you want, repeat as needed.')
    print("A '*' marks songs you have already selected; [has video] already has one.")
    while True:
        term = input("\nSearch (text to filter, blank to list all, "
                     "'done' to start, 'q' to cancel): ").strip()
        low = term.lower()
        if low == 'q':
            return []
        if low == 'done':
            return chosen

        matches = ([(i, e) for i, e in enumerate(entries) if low in e[2]]
                   if term else list(enumerate(entries)))
        if not matches:
            print('  No matches.')
            continue

        shown = matches[:page]
        for n, (_, e) in enumerate(shown, 1):
            mark = '  [has video]' if e[3] else ''
            picked = ' *' if e[0] in chosen else ''
            print('  %2d. %s%s%s' % (n, e[1], mark, picked))
        if len(matches) > len(shown):
            print('  ...and %d more - narrow your search.' % (len(matches) - len(shown)))

        sel = input("Add which? e.g. 1,3,5-8  -  'a' = all shown  -  "
                    "Enter = search again: ").strip()
        if not sel:
            continue
        before = len(chosen)
        for k in parse_selection(sel, len(shown)):
            filename = shown[k - 1][1][0]   # shown[k-1] = (idx, entry); entry[0] = path
            if filename not in chosen:
                chosen.append(filename)
        print('  %d selected in total (added %d).' % (len(chosen), len(chosen) - before))


def main():
    # update check runs first - if there's a new version the user accepts,
    # it restarts the exe and this doesn't continue
    try:
        updater.run_startup_updates(__version__, getattr(yt_dlp.version, '__version__', '0'))
    except SystemExit:
        raise
    except Exception:
        pass

    print('Checking for home folder...')
    songs_folder = os.path.join(os.getcwd(), 'songs')
    if not os.path.exists(songs_folder):
        input("Did not detect a 'Songs' folder. Check you have placed the .exe file "
              "in the directory one level above it. Press any button to exit")
        return

    print('Songs folder found\n')
    replace = False
    resync = False
    manual = False
    video_quality = quality_format(720)

    quality_input = input(
        'Type the number to pick from the following options:\n'
        '1. Default quality (720p)\n'
        '2. Best quality (1080p, where available; significantly bigger files):\n'
        '3. [EXPERIMENTAL] Replace existing videos with 1080p\n'
        '4. Re-sync existing videos (keeps your videos, only fixes their timing)\n'
        '5. Pick specific songs to download (manual - good for huge libraries)\n\n'
        'Pick between 1-5: ')

    if quality_input == '1':
        print('Set to 720p')
        video_quality = quality_format(720)
    elif quality_input == '2':
        print('Set to 1080p. Poor hard drive!')
        video_quality = quality_format(1080)
    elif quality_input == '3':
        print('Replacing all videos with 1080p. You have time for a nap!')
        video_quality = quality_format(1080)
        replace = True
    elif quality_input == '4':
        resync = True
    elif quality_input == '5':
        manual = True
        # Songs you explicitly pick are always (re)downloaded, even if they
        # already have a video - that is the point of choosing them by hand.
        replace = True
        q = input('Quality for the songs you pick - 1 = 720p (default), 2 = 1080p: ').strip()
        video_quality = quality_format(1080) if q == '2' else quality_format(720)
        print('Set to 1080p.' if q == '2' else 'Set to 720p.')
    else:
        print('You must choose between 1-5. Try again')
        return

    sync_ready = ffmpegAvailable and audiosync is not None and audiosync.is_available()

    if resync and not sync_ready:
        print('Re-sync needs ffmpeg and numpy. Place ffmpeg.exe next to this program (see the README).')
        return

    if not resync:
        if not ffmpegAvailable:
            print('\nNote: ffmpeg was not found.')
            print('  - Videos are saved as-is (still h264 - they play fine in Clone Hero).')
            print('  - Auto-sync is off, so videos use a default 3-second offset.')
            print('  Place ffmpeg.exe next to this program to enable both (see the README).')
        elif sync_ready:
            print('\nAuto-sync is on: each video is matched to its chart and lined up automatically.')
        else:
            print('\nNote: auto-sync is off (numpy unavailable); using the default offset.')

    print('\nScanning library and cleaning up any interrupted downloads...')
    song_inis = list(glob.iglob(os.path.join(songs_folder, '**', 'song.ini'), recursive=True))
    for filename in song_inis:
        folder = os.path.dirname(filename)
        cleanup_temp_files(folder)
        vid = os.path.join(folder, 'video.mp4')
        if os.path.exists(vid) and os.path.getsize(vid) == 0:
            try:
                os.remove(vid)
            except OSError:
                pass
    total = len(song_inis)

    if manual:
        song_inis = select_songs_manually(song_inis)
        if not song_inis:
            print('\nNo songs selected. Nothing to do.')
            return
        total = len(song_inis)

    print('\n' + '-' * 64)
    print('You can safely stop this program at any time (Ctrl+C or close the')
    print('window). Nothing will be left corrupt, and re-running it later resumes')
    if manual:
        print('where you left off - the songs you picked are processed in order.')
    else:
        print('where you left off - songs that already have a video are skipped.')
    print('-' * 64 + '\n')

    errored = []
    interrupted = False
    current_folder = None

    try:
        with tqdm(total=total, unit='songs') as pbar:
            for filename in song_inis:
                folder = os.path.dirname(filename)
                song_name = os.path.basename(folder)
                current_folder = folder
                pbar.update(1)

                has_video = os.path.exists(os.path.join(folder, 'video.mp4'))
                if resync:
                    if not has_video:
                        continue
                elif has_video and not replace:
                    continue

                ok = run_song_with_backoff(
                    folder, song_name, video_quality, sync_ready, replace, resync, errored)
                if ok == 'stop':
                    interrupted = True
                    break

                # small random delay between songs
                time.sleep(random.uniform(SONG_DELAY_MIN, SONG_DELAY_MAX))
    except KeyboardInterrupt:
        interrupted = True
        if current_folder:
            cleanup_temp_files(current_folder)
        print('\n\nStopped. The song in progress was cleaned up - nothing corrupt was left behind.')
        print('Everything already downloaded is safe. Re-run this program any time to resume.')

    if interrupted:
        input('\nPress Enter to exit...')
    else:
        if errored:
            print('\nThe following songs ran into problems and may need a manual look:')
            for name in errored:
                print('  ' + name)
        print('\nTip: you can re-run this program any time to fill in anything that')
        print('was skipped or errored - it only downloads what is still missing.')
        input('All downloads complete. Checked a total of ' + str(total) + ' songs. Press Enter to exit.')


def run_song_with_backoff(folder, song_name, quality, sync_ready, replace, resync,
                          errored, stop_evt=None, events=None):
    """Process one song, retrying with longer waits each time YouTube throttles us.

    Pass stop_evt (a threading.Event) and the backoff wait becomes cancellable,
    so the GUI's Stop doesn't sit there for minutes. Returns 'stopped' then.
    Pass a list as events and it gets 'throttled' appended whenever YouTube
    pushes back, so the caller can pace itself accordingly."""
    for attempt in range(len(BOT_BACKOFF_SECONDS) + 1):
        try:
            if resync:
                process_resync(folder, song_name, sync_ready)
            else:
                if process_download(folder, song_name, quality, sync_ready,
                                    replace) == 'skipped':
                    return 'skipped'
            return 'ok'
        except KeyboardInterrupt:
            raise
        except BotDetected:
            cleanup_temp_files(folder)
            if events is not None:
                events.append('throttled')
            if attempt >= len(BOT_BACKOFF_SECONDS):
                print('\nYouTube is still asking to "confirm you\'re not a bot" after several')
                print('waits. Your IP is being rate-limited. Stopping now - wait a while and')
                print('re-run; everything already downloaded is skipped automatically.')
                log.warning('Rate-limited and gave up on %s', song_name)
                return 'stop'
            wait = BOT_BACKOFF_SECONDS[attempt]
            print('\nYouTube rate-limit hit. Waiting ' + str(wait) + 's before retrying this song...')
            # cancellable wait, so Stop doesn't leave you waiting minutes
            if stop_evt is not None and stop_evt.wait(wait):
                return 'stopped'
            if stop_evt is None:
                time.sleep(wait)
        except Exception as e:
            log.exception('Error on song: %s', song_name)
            print('  ' + str(e))
            print('Error on song: ' + song_name + '. Skipping')
            cleanup_temp_files(folder)
            errored.append(str(e) or song_name)
            return 'ok'
    return 'ok'


if __name__ == '__main__':
    if getattr(sys, 'frozen', False):
        # frozen exe - launch the GUI
        try:
            import pyi_splash   # only there in the splash-enabled onefile build
            pyi_splash.update_text('Loading interface...')
        except Exception:
            pass
        import gui
        gui.run()
    else:
        # running from source, use the terminal version
        main()
