import configparser
import glob
import os
import random
import re
import shutil
import subprocess
import sys
import time

# updater goes first so a cached newer yt-dlp gets onto sys.path before yt_dlp loads
import updater
updater.prefer_cached_ytdlp()

import yt_dlp
from tqdm import tqdm

import resolver_client

__version__ = '2.0.0'

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


def _parse_song_ini(path):
    """Returns (artist, title) from a song.ini, or (None, None) on failure."""
    try:
        text = _read_text(path)
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.read_string(text)
    except Exception:
        return None, None
    for sec in cp.sections():
        if sec.lower() == 'song':
            artist = (cp.get(sec, 'artist', fallback='') or '').strip()
            title = (cp.get(sec, 'name', fallback='') or '').strip()
            return (artist or None), (title or None)
    return None, None


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
    ini = os.path.join(folder, 'song.ini')
    if os.path.exists(ini):
        artist, title = _parse_song_ini(ini)
        if title:
            return artist, title
    chart = os.path.join(folder, 'notes.chart')
    if os.path.exists(chart):
        artist, title = _parse_chart(chart)
        if title:
            return artist, title
    return None, os.path.basename(folder)


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
    """Return a list of (url, title) for the top results, most-relevant first."""
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
            candidates.append((url, entry.get('title', 'Unknown')))
    if not candidates:
        raise Exception('No search results found for: ' + query)
    return candidates


def fetch_audio(folder, url):
    """Download audio-only for fingerprinting. Returns the file path, or None on failure."""
    cleanup_temp_files(folder)
    opts = _base_opts()
    opts.update({'format': 'bestaudio/best',
                 'outtmpl': os.path.join(folder, 'video.sync.%(ext)s')})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        if is_bot_error(e):
            raise BotDetected(str(e))
        return None
    matches = glob.glob(os.path.join(folder, 'video.sync.*'))
    return matches[0] if matches else None


def download_video(folder, url, quality):
    """Download video to a temp file, then rename to video.mp4 once complete."""
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


def select_video(folder, candidates, sync_ready):
    """Pick the right candidate. With sync available, fingerprints each one against
    the chart audio until we get a confident match. Returns (url, title, offset_ms, matched)."""
    if not sync_ready:
        url, title = candidates[0]
        return url, title, DEFAULT_START_TIME, False

    for url, title in candidates[:GATE_CANDIDATES]:
        audio = fetch_audio(folder, url)
        try:
            if not audio:
                continue
            ms, info = audiosync.compute_offset_ms(folder, audio)
        except BotDetected:
            raise
        except Exception as e:
            print('  Sync check failed for a candidate (' + str(e) + ')')
            ms = None
        finally:
            cleanup_temp_files(folder)
        if ms is not None:
            print('  Matched: ' + title + '  (' + info + ')')
            return url, title, ms, True

    # nothing matched - use top result with default offset
    url, title = candidates[0]
    print('  No confident match - using top result with default offset')
    return url, title, DEFAULT_START_TIME, False


def download_with_fallback(folder, primary_url, candidates, quality):
    """Try the primary URL, fall back through other candidates if it fails."""
    urls = [primary_url] + [u for u, _ in candidates if u != primary_url]
    last_err = None
    for url in urls:
        try:
            download_video(folder, url, quality)
            return url
        except BotDetected:
            raise
        except Exception as e:
            last_err = e
            print('  Download failed (' + str(e) + '); trying next result')
            cleanup_temp_files(folder)
    raise last_err if last_err else Exception('No downloadable candidate')


def process_download(folder, song_name, quality, sync_ready, replace):
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
            offset = DEFAULT_START_TIME if offset is None else offset
            if set_ini_values(folder, {'video_start_time': str(offset),
                                       'backstagehero_source': hit['video_id']}):
                _probe_and_store_resolution(folder)
                print('  Song ready (community offset). Next song...')
            return
        except BotDetected:
            raise
        except Exception as e:
            print('  Community video could not be downloaded (' + str(e) + '); searching instead')
            cleanup_temp_files(folder)

    # resolver miss - do a search and fingerprint-check candidates
    query = build_query(artist, title)
    print('\nLooking on YouTube for: ' + query)

    candidates = search_candidates(query)
    url, vid_title, offset, matched = select_video(folder, candidates, sync_ready)
    print('Downloading: ' + vid_title)

    # old video stays until new one is fully downloaded, so nothing is left half-done
    used_url = download_with_fallback(folder, url, candidates, quality)

    if is_converted(folder):
        print('  Phase Shift converter file detected - video kept, timing left as-is')
        return

    vid = video_id_of(used_url)
    values = {'video_start_time': str(offset), 'backstagehero_source': vid}
    if set_ini_values(folder, values):
        note = 'auto-synced' if matched else 'default offset'
        _probe_and_store_resolution(folder)
        print('  Song ready (' + note + '). Next song...')
    else:
        print('  Could not update song.ini (no [song] section?). Video kept.')
        raise Exception('song.ini missing [song] section')

    # only report confident fingerprint matches - no point voting on guesses
    if matched:
        resolver_client.report(ch, vid, offset, 1.0, artist, title)


def process_resync(folder, song_name, sync_ready):
    """Re-fingerprint an existing video to fix its timing."""
    if not sync_ready:
        return
    if is_converted(folder):
        return

    artist, title = read_metadata(folder)

    source = _parse_song_ini_raw(folder)

    if source:
        url = f'https://www.youtube.com/watch?v={source}'
        print('\nRe-syncing: ' + build_query(artist, title) + '  (known source)')
        audio = fetch_audio(folder, url)
        try:
            ms, info = (audiosync.compute_offset_ms(folder, audio)
                        if audio else (None, 'no audio'))
        finally:
            cleanup_temp_files(folder)
        if ms is not None and set_ini_values(folder, {'video_start_time': str(ms)}):
            _probe_and_store_resolution(folder)
            print('  Re-synced: ' + info)
            return
        print('  Known source no longer matches - falling back to search')

    query = build_query(artist, title)
    print('\nRe-syncing: ' + query)
    candidates = search_candidates(query)
    _, vid_title, offset, matched = select_video(folder, candidates, sync_ready)
    if matched and set_ini_values(folder, {'video_start_time': str(offset)}):
        print('  Re-synced against: ' + vid_title)
    else:
        print('  No confident match - timing left unchanged')


def get_stored_source(folder):
    """Return the backstagehero_source video ID stored in song.ini, or None."""
    return _parse_song_ini_raw(folder)


def _parse_song_ini_raw(folder):
    """Return the stored backstagehero_source video id, if any."""
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
            val = cp.get(sec, 'backstagehero_source', fallback='').strip()
            return val or None
    return None


def get_stored_resolution(folder):
    """Return the stored backstagehero_res value (e.g. '720p'), or None."""
    path = os.path.join(folder, 'song.ini')
    if not os.path.exists(path):
        return None
    try:
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.read_string(_read_text(path))
        for sec in cp.sections():
            if sec.lower() == 'song':
                return cp.get(sec, 'backstagehero_res', fallback='').strip() or None
    except Exception:
        pass
    return None


def _probe_and_store_resolution(folder):
    """Read the resolution from video.mp4 and cache it in song.ini."""
    if not ffmpegAvailable:
        return
    video = os.path.join(folder, 'video.mp4')
    if not os.path.exists(video):
        return
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
            set_ini_values(folder, {'backstagehero_res': f'{int(m.group(2))}p'})
    except Exception:
        pass


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


def run_song_with_backoff(folder, song_name, quality, sync_ready, replace, resync, errored):
    """Process one song, retrying with increasing waits if YouTube throttles us."""
    for attempt in range(len(BOT_BACKOFF_SECONDS) + 1):
        try:
            if resync:
                process_resync(folder, song_name, sync_ready)
            else:
                process_download(folder, song_name, quality, sync_ready, replace)
            return 'ok'
        except KeyboardInterrupt:
            raise
        except BotDetected:
            cleanup_temp_files(folder)
            if attempt >= len(BOT_BACKOFF_SECONDS):
                print('\nYouTube is still asking to "confirm you\'re not a bot" after several')
                print('waits. Your IP is being rate-limited. Stopping now - wait a while and')
                print('re-run; everything already downloaded is skipped automatically.')
                return 'stop'
            wait = BOT_BACKOFF_SECONDS[attempt]
            print('\nYouTube rate-limit hit. Waiting ' + str(wait) + 's before retrying this song...')
            time.sleep(wait)
        except Exception as e:
            print('  ' + str(e))
            print('Error on song: ' + song_name + '. Skipping')
            cleanup_temp_files(folder)
            errored.append(song_name)
            return 'ok'
    return 'ok'


if __name__ == '__main__':
    if getattr(sys, 'frozen', False):
        # frozen exe - launch the GUI
        import gui
        gui.run()
    else:
        # Running from source — terminal mode for developers.
        main()
