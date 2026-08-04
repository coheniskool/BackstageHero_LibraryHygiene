import os
import sys

# A console-less launch (pythonw.exe, or a --noconsole frozen build without
# the bootloader's own stdio shim covering every case) sets sys.stdout/
# stderr to None, not just closed -- the first print() or warnings.warn()
# anywhere in this module or a dependency then crashes immediately with no
# visible error. This is the file build.py's PyInstaller invocation
# actually targets, so the guard belongs here, not only in gui.py. Must run
# before any other import.
# The guard itself lives in library_common so it can be unit-tested -- inline
# import-time code here is unreachable under pytest, so its removal would not
# fail a single test. library_common imports only stdlib and prints nothing.
import library_common
library_common.ensure_stdio_not_none()
library_common.make_console_encoding_safe()

import configparser
import glob
import json
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
import static_art
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

# strip these from titles before searching so we don't get playthrough results
# longer forms first, otherwise 'RB3' strips inside '(RB3 version)' and leaves '( version)'
TITLE_ISSUES = ['(2x Bass Pedal Expert+)', '(2x Bass Pedal)', '(RB3 version)', '(Rh)', 'RB3']

# leftover files from interrupted runs - safe to delete, video.mp4 is never in here
TEMP_ARTIFACTS = [
    'video.download.mp4', 'video.download.mp4.part', 'video.download.mp4.ytdl',
    'video.tmp.mp4', 'output.mp4', 'video.mp4.part', 'video.mp4.ytdl',
    'song.ini.tmp',
]

# how many search results to pull, and how many to fingerprint-test before falling back.
# Pulling more than we gate on is deliberate: ranking (duration, then what KIND
# of video the title says it is) needs somewhere to demote the junk TO. The
# extra results cost one search call and no extra audio downloads.
SEARCH_RESULTS = 8
GATE_CANDIDATES = 3

# What the candidate's own title says it is.
#
# Fingerprinting confirms the AUDIO matches the chart. It is completely blind to
# what is on screen -- a lyric video, a Guitar Hero playthrough and the official
# music video all carry identical audio, so audiosync confirms all three with
# equal confidence and writes `measured` for each. Found in a real playtest
# (2026-07-19): almost every fingerprint-confirmed video in a 5,130-song library
# was a lyric video or Rock Band / Guitar Hero gameplay footage. The offsets were
# right; the videos were simply not what anyone wanted to watch.
#
# The candidate title comes back free with the flat search and was previously
# only ever printed. Matching on it is the one signal available that can tell
# these apart at all, before any download happens.
#
# Deliberately demote rather than exclude: for an obscure custom chart a lyric
# video may be the only thing that exists, and this project's own rule is that a
# video is worth having unless it is confidently wrong. A real video simply wins
# whenever one is present.
# Plain substrings are enough for the unambiguous ones.
_GAMEPLAY_MARKERS = (
    'guitar hero', 'rock band', 'rockband', 'clone hero', 'rocksmith',
    'playthrough', 'play through', 'gameplay', 'sightread', 'sight read',
    'chart preview', 'custom chart', 'drum chart', 'note chart', 'notes chart',
    'expert+', 'expert +', '100% fc', 'full combo', 'beat saber',
    # The Rock Band Network naming convention, which is what this library is
    # actually full of: "<Song> by <Artist> Full Band FC #123". Measured on 42
    # real fingerprint-confirmed songs -- the first version of this list caught
    # 8 of them, because it looked for the words a person would use to describe
    # gameplay rather than the words these uploads actually use.
    'full band', 'expert guitar', 'expert bass', 'expert drums',
    'expert vocals', 'pro drums', 'pro guitar', 'harmonies',
)

# Short, collision-prone tokens: matched on word boundaries so 'rbn' does not
# fire inside an ordinary word and 'fc' does not fire inside 'fcuk'.
# rbn\d* rather than rbn: the real library contains "RBN2 EA - Calling to
# Dance", which \brbn\b cannot match because RBN2 is a single word.
_GAMEPLAY_TOKEN_RE = re.compile(
    r'\b(rbn\d*|rb[123]|gh[1235]|fc\s*#?\d*|dc\d+)\b', re.I)
_LYRIC_MARKERS = (
    'lyric', 'karaoke', 'sing along', 'singalong', 'sing-along',
)
_AUDIO_ONLY_MARKERS = (
    'official audio', 'full album', 'audio only', '(audio)', '[audio]',
    'visualizer', 'visualiser',
)
# "official video" alone missed most of the real thing. Measured against 216
# titles this classifier had called 'unknown': "Official HD Video" does not
# contain the substring "official video", and plenty of genuine uploads say
# only "Music Video", "(the music video)" or "Promovideo". Since the lyric and
# gameplay checks run first, "Official Lyric Video" is still a lyric video --
# these markers only ever get to promote something nothing else objected to.
_OFFICIAL_MARKERS = (
    'music video', 'promovideo', 'promo video', 'officialvideo',
)

# "official <anything> video" -- HD, 4K, HQ, music, live clip. Enumerating the
# variants was the wrong shape: the real library had "Official HD Video" and
# "Official 4K Video", and the next one would have been missed too.
_OFFICIAL_RE = re.compile(r'official\s+(?:\w+\s+){0,2}(?:video|clip)', re.I)


def classify_candidate_title(title):
    """What kind of video this title claims to be.

    Returns 'official', 'gameplay', 'lyric', 'audio_only' or 'unknown'.
    Checked most-specific first: a title can say both "official video" and
    "lyrics", and the strongest negative signal should win, because being wrong
    in the direction of keeping junk is what this exists to stop.
    """
    low = (title or '').lower()
    if any(m in low for m in _GAMEPLAY_MARKERS) or _GAMEPLAY_TOKEN_RE.search(low):
        return 'gameplay'
    if any(m in low for m in _LYRIC_MARKERS):
        return 'lyric'
    if any(m in low for m in _AUDIO_ONLY_MARKERS):
        return 'audio_only'
    if any(m in low for m in _OFFICIAL_MARKERS) or _OFFICIAL_RE.search(low):
        return 'official'
    return 'unknown'


# Lower sorts earlier. 'unknown' beats a self-declared lyric video, because an
# ordinary upload of a real video usually says nothing special about itself.
CANDIDATE_KIND_RANK = {
    'official': 0,
    'unknown': 1,
    'audio_only': 2,
    'lyric': 3,
    'gameplay': 4,
}

# random pause between songs so it doesn't look like a bot hammering the API
SONG_DELAY_MIN = 1.0
SONG_DELAY_MAX = 3.0

# if YouTube rate-limits us, wait and retry the same song before giving up
BOT_BACKOFF_SECONDS = [60, 180, 420]

# Background-mode-only. A separate, much longer backoff layered ABOVE the
# short per-song retry above -- it only comes into play once
# run_song_with_backoff has already exhausted BOT_BACKOFF_SECONDS and
# returned 'stop'. It is not a replacement for the short retry, and nothing
# here changes BOT_BACKOFF_SECONDS' own behavior.
#
# 1h / 4h / 12h / 24h, capped and repeating at 24h. This is a starting-point
# schedule, not a measured fact -- how long YouTube's throttle actually lasts
# is unknown. A later task (gap-logging + adaptive recompute, tracked
# separately) may replace this with a schedule derived from real observed
# throttle-and-resume data; next_resume_at() below accepts a schedule
# override for exactly that purpose.
LONG_BACKOFF_SECONDS = [3600, 14400, 43200, 86400]


def next_resume_at(throttle_count, now, schedule=LONG_BACKOFF_SECONDS):
    """Unix timestamp to resume at, given how many consecutive long-backoff
    throttles have happened so far in this run (0-indexed).

    Indexes into `schedule`, clamping to the last (repeating) entry once
    `throttle_count` runs past the list's length -- this must never raise
    IndexError no matter how large `throttle_count` gets, since background
    mode retries indefinitely and never gives up on its own."""
    index = min(throttle_count, len(schedule) - 1)
    return now + schedule[index]


# --- Gap logging + adaptive backoff (Task 8 of SPEC-background-mode.md) ------
#
# Every completed throttle episode (first 'stop' -> the retry that finally
# succeeds) is recorded to its own file so the LONG_BACKOFF_SECONDS *guess*
# above can eventually be replaced by a schedule derived from what YouTube's
# throttle actually does in this user's environment. Kept in this module (next
# to the schedule it recomputes) rather than in gui.py so all backoff-related
# logic lives in one place.
#
# Its own file, NOT folded into background_state.json or settings.json: those
# hold run state and UI prefs respectively; this holds an append-only episode
# log plus the one derived schedule. Atomic writes (temp + os.replace), same
# discipline gui.py's _save_background_state uses -- a multi-day unattended run
# is exactly the crash-mid-write scenario that discipline exists for.
_THROTTLE_HISTORY_FILE = os.path.join(updater.data_dir(), 'throttle_history.json')

# Keep only the most recent N episodes. Rationale: (1) an app pointed at a real
# library and left running for days/weeks could accumulate throttle episodes
# indefinitely, and an unbounded append-only file is a slow leak; (2) more
# importantly, a schedule recomputed from episodes months old would be fitting
# to a YouTube throttle policy that may have since changed -- a recent window
# tracks current behavior better than an all-time average. 50 is comfortably
# more than the 5-episode recompute threshold, so the median stays stable.
_THROTTLE_HISTORY_MAX = 50

# Number of recorded episodes before the schedule is allowed to recompute from
# real data (spec Resolved Decision: 5). Below this we keep using the guess.
_RECOMPUTE_THRESHOLD = 5

# Crash-prevention clamp ONLY -- NOT the 1h policy floor the user explicitly
# declined. The user chose throughput over an extra safety margin: if the data
# says YouTube unblocks in under an hour, the schedule is allowed to shrink
# below an hour (that is the whole point of the adaptive recompute). This 5-min
# minimum exists purely so a degenerate/corrupt history (a negative gap from
# clock skew, an empty window slipping through, a future formula tuned too
# aggressively) can never produce a near-zero wait that turns the retry loop
# into a busy-loop hammering YouTube. It is a floor on *machine safety*, not on
# *politeness* -- those are different concerns and must not be conflated.
_MIN_BACKOFF_SECONDS = 300


def _load_throttle_data():
    """Returns {'episodes': list, 'schedule': list|None}, defensively -- any
    read/parse failure yields the empty shape rather than raising, same
    contract as gui.py's _load_background_state(). Tolerates a bare-list file
    (treated as episodes) so an older format can't crash a load."""
    try:
        with open(_THROTTLE_HISTORY_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {'episodes': [], 'schedule': None}
    if isinstance(data, list):
        return {'episodes': data, 'schedule': None}
    if isinstance(data, dict):
        episodes = data.get('episodes')
        schedule = data.get('schedule')
        return {
            'episodes': episodes if isinstance(episodes, list) else [],
            'schedule': schedule if isinstance(schedule, list) and schedule else None,
        }
    return {'episodes': [], 'schedule': None}


def _save_throttle_data(data):
    """Atomic write (temp file + os.replace), mirroring _save_background_state.
    Swallows failures rather than crashing a long unattended run -- losing a
    single episode record is acceptable; taking down the run over it is not."""
    try:
        tmp_path = _THROTTLE_HISTORY_FILE + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp_path, _THROTTLE_HISTORY_FILE)
    except Exception:
        pass


def record_throttle_episode(started_at, resolved_at, escalation_steps_used,
                            schedule=LONG_BACKOFF_SECONDS):
    """Append one completed throttle episode and, if enough have accumulated,
    recompute and persist an adaptive schedule.

    Load-modify-save: reads the existing episode list, appends
    {started_at, resolved_at, escalation_steps_used}, trims to the most recent
    _THROTTLE_HISTORY_MAX, then writes the whole file back atomically. The
    recomputed schedule (if any) is persisted in the *same* file alongside the
    raw records, so a restart keeps a schedule that took real data to earn --
    get_active_schedule() below is how the rest of background mode reads it.

    Returns the episodes list actually persisted (useful for callers/tests)."""
    data = _load_throttle_data()
    episodes = data['episodes']
    episodes.append({
        'started_at': started_at,
        'resolved_at': resolved_at,
        'escalation_steps_used': escalation_steps_used,
    })
    # Trim from the front -- keep the most recent window (see _THROTTLE_HISTORY_MAX).
    if len(episodes) > _THROTTLE_HISTORY_MAX:
        episodes = episodes[-_THROTTLE_HISTORY_MAX:]

    new_schedule = maybe_recompute_schedule(episodes, schedule=schedule)
    if new_schedule is not None:
        old_schedule = data.get('schedule') or list(schedule)
        direction = ('grew' if sum(new_schedule) > sum(old_schedule)
                     else 'shrank' if sum(new_schedule) < sum(old_schedule)
                     else 'unchanged')
        log.info('Background mode: adaptive backoff schedule %s after %d episodes '
                 '(old=%s new=%s)', direction, len(episodes), old_schedule, new_schedule)
        data['schedule'] = new_schedule

    data['episodes'] = episodes
    _save_throttle_data(data)
    return episodes


def get_active_schedule():
    """The schedule background mode should actually back off on: the persisted,
    adaptively-recomputed one if it exists, otherwise the LONG_BACKOFF_SECONDS
    starting guess. This is what survives a restart -- the recomputed schedule
    is written to disk by record_throttle_episode, so a relaunch mid-run keeps
    using the earned schedule rather than falling back to the guess."""
    schedule = _load_throttle_data()['schedule']
    return list(schedule) if schedule else list(LONG_BACKOFF_SECONDS)


def _median(values):
    """Plain median. No numpy dependency in this project -- and a hand-rolled
    median is more robust than a mean for the tiny (n>=5), noisy, right-censored
    samples this feeds (one clock-skew outlier shouldn't swing the schedule)."""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def maybe_recompute_schedule(history, schedule=LONG_BACKOFF_SECONDS):
    """Derive a new LONG_BACKOFF_SECONDS-shaped schedule from observed throttle
    episodes, or None to signal "keep using the current schedule".

    Returns None (a no-op signal, chosen over returning the unchanged schedule
    so callers can cheaply tell "nothing to persist") when there are fewer than
    _RECOMPUTE_THRESHOLD episodes.

    ---- The judgment call, spelled out (this is the high-reasoning bit) ----
    The signal we drive from is `escalation_steps_used`: which step of the
    schedule the block finally cleared on. We deliberately do NOT derive step
    values from the raw `resolved_at - started_at` gaps, even though the data is
    there. The reason is the right-censoring the spec Notes flag: the observed
    gap is a product of BOTH YouTube's real block length AND our own schedule --
    if our first wait is 1h and the real block was 10min, every gap reads ~1h,
    so feeding gaps back in would just re-encode our own schedule into itself
    and learn nothing. Escalation depth ("where did it clear?") is the more
    honest signal: it's censored too, but it tells us the *shape* of where
    blocks resolve relative to our steps, which is exactly what we can act on.

    Direction (grow vs. shrink) is driven off the MEDIAN escalation depth versus
    the schedule's own midpoint:
      - clears consistently at step 0-1 (median low)  -> we're over-waiting -> SHRINK
      - clears consistently at the last step (median high) -> real block is longer
        than our top step -> GROW
      - clears around the middle -> schedule is about right -> ~unchanged
    We scale the DEFAULT `schedule` (not the previously-recomputed one) by a
    single ratio derived from that median. Scaling the default each time keeps
    the function stateless and idempotent for a given history -- no compounding
    drift from repeated recomputes -- which is also what makes it testable on a
    fabricated history alone.

    ratio = (median_depth + 1) / (midpoint + 1), midpoint = (len-1)/2

    Growth is bounded, never unbounded (spec: "grows or stays capped at the
    top"): each episode's depth is clamped into [0, max_index + 1] before the
    median, so even a run that repeated at the top 100 times reads as one step
    past the end -- a clear "grow" signal without letting a runaway count blow
    the schedule up. For the default 4-step schedule that caps a single
    recompute at 2x. Negative/garbage depths clamp up to 0 (a "shrink" signal),
    so degenerate input can only ever push toward the crash-prevention floor,
    never past it.

    Every resulting step is finally clamped to _MIN_BACKOFF_SECONDS -- the
    crash-prevention floor, NOT the declined policy floor (see that constant)."""
    if not schedule:
        return None
    if len(history) < _RECOMPUTE_THRESHOLD:
        return None

    max_index = len(schedule) - 1
    # Clamp each observed depth into [0, max_index + 1]: below 0 is corrupt
    # (treat as an early clear -> shrink); above max_index means it repeated at
    # the top (treat as one-past-the-end -> a bounded grow signal). This is the
    # single guard that keeps growth from ever being unbounded.
    depths = []
    for record in history:
        try:
            depth = int(record.get('escalation_steps_used', 0))
        except (TypeError, ValueError):
            depth = 0
        depths.append(max(0, min(depth, max_index + 1)))

    median_depth = _median(depths)
    midpoint = max_index / 2.0
    ratio = (median_depth + 1) / (midpoint + 1)

    return [
        max(_MIN_BACKOFF_SECONDS, int(round(step * ratio)))
        for step in schedule
    ]


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


# Optional, opt-in browser-cookie support for yt-dlp requests -- off by
# default, matching today's behavior exactly. gui.py's settings.json is the
# source of truth for the toggle/browser choice, but it lives entirely
# outside this module, so configure_cookies() is the one push: gui.py calls
# it once at startup (with whatever was persisted) and again whenever the
# toggle/dropdown changes, so a change takes effect on the next download
# without an app restart. Only a browser *name* string ever passes through
# here -- yt-dlp itself reads that browser's cookie store; no cookie value is
# ever read, logged, or persisted by this code.
USE_BROWSER_COOKIES = False
COOKIE_BROWSER = None
# Set once a browser-cookie store proves unusable this process (DPAPI/App-
# Bound Encryption failure, locked profile, corrupted store, etc.) -- see
# _run_ytdlp_with_cookie_fallback() below. Deliberately NOT reset by
# configure_cookies(): once broken this process, it stays broken until the
# app restarts, even if the user re-toggles the checkbox mid-session. That
# keeps the fallback entirely in-memory -- it never touches settings.json or
# gui.py's checkbox state.
_COOKIES_BROKEN = False

# yt-dlp's own supported --cookies-from-browser browser names. Kept here as a
# defense-in-depth guard: today's only caller (gui.py's footer dropdown) is
# hardcoded to a 3-item subset of this list, but configure_cookies() has no
# way to know that -- a future caller (a config file, a different UI element)
# could otherwise pass an unsupported string straight through to yt-dlp.
_SUPPORTED_COOKIE_BROWSERS = frozenset({
    'brave', 'chrome', 'chromium', 'edge', 'firefox', 'opera', 'safari',
    'vivaldi', 'whale',
})


def configure_cookies(use_cookies, browser):
    """Push gui.py's persisted cookie-support setting into this module.

    use_cookies=False (the default, and what happens if this is never
    called at all) leaves _base_opts()'s output byte-identical to before
    this feature existed. An unsupported browser name is logged and leaves
    cookie support disabled rather than raising -- matching this module's
    existing defensive-failure style (e.g. _load_throttle_data/
    _save_throttle_data) -- so a bad value never reaches _base_opts()."""
    global USE_BROWSER_COOKIES, COOKIE_BROWSER
    if use_cookies and browser and browser.lower() in _SUPPORTED_COOKIE_BROWSERS:
        USE_BROWSER_COOKIES = True
        COOKIE_BROWSER = browser.lower()
        return

    if use_cookies and browser and browser.lower() not in _SUPPORTED_COOKIE_BROWSERS:
        log.warning('configure_cookies: unsupported browser %r, cookie '
                     'support left disabled', browser)
    USE_BROWSER_COOKIES = False
    COOKIE_BROWSER = None


def _base_opts():
    # No player_client override here on purpose: a hardcoded list (previously
    # ['tv_embedded', 'android_vr', 'android']) goes stale the moment yt-dlp's
    # maintainers deprecate or remove a client upstream -- tv_embedded was
    # removed as broken in yt-dlp's 2026.01.31 release, which meant every
    # download wasted its first attempt on a dead client for months before
    # this was caught. yt-dlp's own built-in default is maintainer-managed and
    # stays current without this project having to track it by hand.
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': 1,
        'sleep_interval_requests': 1,
    }
    if USE_BROWSER_COOKIES and COOKIE_BROWSER and not _COOKIES_BROKEN:
        # yt-dlp's Python-API equivalent of --cookies-from-browser: a
        # 1-tuple of (browser_name,). yt-dlp reads that browser's own cookie
        # store directly -- nothing here touches a cookie value.
        opts['cookiesfrombrowser'] = (COOKIE_BROWSER,)
    return opts


_COOKIE_ERROR_SIGNS = ('failed to decrypt with dpapi', 'failed to load cookies')


def _is_cookie_decrypt_error(exc):
    msg = str(exc).lower()
    return any(sign in msg for sign in _COOKIE_ERROR_SIGNS)


def _run_ytdlp_with_cookie_fallback(opts, fn):
    """Run fn(ydl) with opts. If it fails because the browser-cookie store
    couldn't be read (Windows DPAPI / Chrome App-Bound Encryption -- yt-dlp
    issue #10927), disable cookies for the rest of this process and retry
    fn once without them.

    Matches on message text, not exception type: yt-dlp's YoutubeDL.cookiejar
    property catches the internal CookieLoadError and re-raises it as a plain
    DownloadError carrying the original message -- by the time it reaches
    here the type information is already gone, only the text survives."""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return fn(ydl)
    except Exception as e:
        if not (opts.get('cookiesfrombrowser') and _is_cookie_decrypt_error(e)):
            raise
        global _COOKIES_BROKEN
        if not _COOKIES_BROKEN:
            log.warning('Browser cookie extraction failed (%s); continuing '
                        'this run without browser cookies.', e)
        _COOKIES_BROKEN = True
        retry_opts = dict(opts)
        retry_opts.pop('cookiesfrombrowser', None)
        with yt_dlp.YoutubeDL(retry_opts) as ydl:
            return fn(ydl)


def search_candidates(query, n=SEARCH_RESULTS):
    """Top results as (url, title, duration_seconds) tuples, most-relevant first.
    Duration comes free with the flat search and may be None."""
    opts = _base_opts()
    opts['extract_flat'] = True
    try:
        info = _run_ytdlp_with_cookie_fallback(
            opts, lambda ydl: ydl.extract_info(f'ytsearch{n}:{query}', download=False))
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
                           capture_output=True, timeout=10,
                           creationflags=NO_WINDOW, **library_common.TEXT_UTF8)
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
    # Drop anything the user has already thrown away for this song. Done first
    # so every path below -- the fingerprint gate, the duration ranking and the
    # final fallback alike -- is working from candidates that are actually
    # allowed, rather than each having to remember to re-check.
    rejected = get_rejected_sources(folder)
    if rejected:
        allowed = [c for c in candidates if video_id_of(c[0]) not in rejected]
        if not allowed:
            print(f'  Every result was previously dumped for this song '
                  f'({len(rejected)} rejected) - leaving without a video')
            return None, None, DEFAULT_START_TIME, False, 0.0, None
        if len(allowed) != len(candidates):
            print(f'  Skipping {len(candidates) - len(allowed)} previously dumped result(s)')
        candidates = allowed

    # Rank BEFORE the early returns below, not after.
    #
    # Those two paths -- no ffmpeg, or a song with no stems to fingerprint
    # against -- used to hand back candidates[0]: raw YouTube search order,
    # with no duration check and no look at what the video even was. On a
    # library of Guitar Hero charts the top result for a song is very often a
    # Rock Band playthrough, so the one path with no fingerprint to protect it
    # was also the one path doing no filtering at all.
    #
    # rank by plausible length before spending downloads. the song has to fit
    # inside the video, plus some intro/outro, so a 30s short or a 20min live
    # set can't be it. unknown durations aren't punished. doesn't exclude
    # anything outright, just tries the believable ones first.
    chart_dur = _chart_duration(folder)
    if chart_dur:
        def is_plausible(dur):
            return dur is None or (chart_dur - 25) <= dur <= (chart_dur + 150)

        # Three tiers, not two. An unknown duration still isn't punished --
        # it stays eligible, and the floor below still lets it through, since
        # "no duration data" is no evidence of a mismatch. But it must not
        # outrank a candidate we have positively confirmed fits: with two
        # tiers, unknown and confirmed-fitting shared tier 0 and ties broke on
        # search order, so a duration-less result could take ordered[0] ahead
        # of a verified one and sail past the floor on its behalf.
        def rank(item):
            i, (_url, title, dur) = item
            if dur is None:
                tier = 1                     # unknown: eligible, but never preferred
            elif is_plausible(dur):
                tier = 0                     # confirmed to fit this chart
            else:
                tier = 2                     # known not to fit
            # Duration first: a wrong-length result is the wrong SONG, which
            # matters more than what kind of video it is. Kind then decides
            # between candidates that all fit, which is where lyric videos and
            # gameplay footage were quietly winning on search rank alone.
            return (tier, CANDIDATE_KIND_RANK.get(
                classify_candidate_title(title), 1), i)
        ordered = [c for _, c in sorted(enumerate(candidates), key=rank)]
    else:
        # No chart duration to judge against, but the titles are still readable,
        # so the kind preference still applies here rather than falling back to
        # raw search order.
        is_plausible = None
        ordered = [c for _, c in sorted(
            enumerate(candidates),
            key=lambda item: (CANDIDATE_KIND_RANK.get(
                classify_candidate_title(item[1][1]), 1), item[0]))]

    if not sync_ready:
        url, title = ordered[0][0], ordered[0][1]
        return url, title, DEFAULT_START_TIME, False, 0.0, None

    # no stems in this folder = nothing to fingerprint against, so don't waste
    # audio downloads finding that out the hard way
    if audiosync is None or not audiosync.chart_stems(folder):
        url, title = ordered[0][0], ordered[0][1]
        return url, title, DEFAULT_START_TIME, False, 0.0, None

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

    # a song deliberately left without a video is done, not still missing one.
    # without this the static-art conversion defeats itself: every run would
    # re-download the same album-art upload, re-detect it, and delete it again,
    # forever. treated exactly like an existing video.mp4 above, including
    # honouring replace=True.
    if not replace and _read_ini_value(folder, static_art.VIDEO_MARKER_KEY) == \
            static_art.VIDEO_MARKER_STATIC_ART:
        return 'skipped'

    artist, title = read_metadata(folder)
    ch = resolver_client.chart_hash(folder) if resolver_client.enabled() else None

    # check the community resolver first - skips the YouTube search entirely for known charts.
    # A video this user has dumped is not acceptable just because the pool
    # likes it: their rejection is about this library, and overriding it would
    # make the dump silently useless for exactly the songs it matters on.
    hit = resolver_client.resolve(ch)
    if hit and hit.get('video_id') in get_rejected_sources(folder):
        print('  Community video for this chart was previously dumped here - searching instead')
        hit = None
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
            values = {'video_start_time': str(offset),
                      'backstagehero_sync': how,
                      'backstagehero_source': hit['video_id']}
            # video.mp4 is already on disk (download_video just finished), so
            # fold the resolution into this same write instead of a second
            # read-modify-write of song.ini right after it.
            res = _probe_resolution_value(folder)
            if res and res != '?':
                values['backstagehero_res'] = res
            if not set_ini_values(folder, values):
                raise Exception('song.ini missing [song] section')
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

    # some "videos" are just the album cover held for the length of the song.
    # the duration floor in select_video can't catch those - a static upload of
    # the right song is exactly the right length - so judge the file we just
    # downloaded on its contents. only a strict-tier match converts; anything
    # uncertain keeps the video and carries on as normal.
    if static_art.probe_static_video(os.path.join(folder, 'video.mp4')) == 'static':
        result = static_art.convert_to_album_art(folder)
        if result['status'] == 'converted':
            print('  ' + result['detail'])
            # no resolver report: there's no video match to contribute here,
            # and reporting one would poison the shared pool for everyone else.
            return
        log.warning('Static-art conversion failed for %s (%s); keeping the video',
                    song_name, result['detail'])

    # offset was measured against `url`. if a fallback candidate downloaded
    # instead, that offset is for a different video, so drop it and don't report
    # it (otherwise we feed the community a bogus match).
    if matched and used_url != url:
        log.info('Fallback used a different video for %s; dropping fingerprint offset', song_name)
        offset, matched = DEFAULT_START_TIME, False

    vid = video_id_of(used_url)
    # Record what we actually attached, by name. Only the video ID was stored
    # before, which meant a library full of lyric videos and gameplay footage
    # was indistinguishable from a library of real ones without re-querying
    # YouTube for every song. The title is the one thing that tells them apart,
    # and it costs nothing to keep. Titles come from a search result, so a fall
    # back to a different candidate must report THAT one's title, not the
    # originally-selected one.
    used_title = next((c[1] for c in candidates if c[0] == used_url), vid_title)
    values = {'video_start_time': str(offset),
              'backstagehero_sync': SYNC_MEASURED if matched else SYNC_GUESS,
              'backstagehero_source': vid,
              'backstagehero_video_title': used_title or ''}
    # video.mp4 is already on disk at this point, so fold the resolution
    # into this same write instead of a second one right after it.
    res = _probe_resolution_value(folder)
    if res and res != '?':
        values['backstagehero_res'] = res
    if set_ini_values(folder, values):
        note = 'auto-synced' if matched else 'default offset'
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

    Uses the video file already on disk WHEN IT HAS AN AUDIO TRACK: ffmpeg can
    decode that directly, so the recheck costs zero network requests.

    Measured against a real library (2026-07-19), because the original claim
    here was that this covered "the common case" -- it does not, and the
    opposite is true for anything this app downloaded:

        downloaded by this app :   0 with audio,  87 without
        left by the predecessor:  33 with audio,   0 without

    quality_format() asks for `bestvideo[...]` first, which is a video-only
    stream, so a successful download normally has no audio at all. The
    optimisation therefore only ever applies to videos that came from
    somewhere else. It was verified originally by muxing synthetic audio into
    a test MP4 -- a file shaped unlike anything the downloader produces, which
    is why the gap survived a green suite.

    Checking for the audio stream up front rather than letting the decode fail
    matters at library scale: without it, every resync of an app-downloaded
    video hands a 60MB file to ffmpeg only to be told there is nothing to
    decode. Falls back to the stored YouTube source, then a fresh search,
    exactly as before.

    Returns 'skipped' for a song this pass deliberately left alone.
    """
    if not sync_ready:
        return
    if is_converted(folder):
        return

    # a hand-set offset outranks anything automatic. the sync editor writes
    # SYNC_MANUAL for exactly this check -- without it, "Auto-sync" over a
    # checked library silently re-breaks the songs the user already fixed by
    # hand, which are by definition the ones audiosync got wrong the first
    # time. The way back, if the user does want this song re-synced
    # automatically: re-download it (select it and confirm the re-download
    # prompt, which passes replace=True). process_download does not consult
    # this marker, because a new video makes the old hand-set offset
    # meaningless anyway. There is deliberately no "clear the marker" control
    # -- the sync editor only ever writes SYNC_MANUAL, never clears it.
    if _read_ini_value(folder, 'backstagehero_sync') == SYNC_MANUAL:
        print('  Manually synced - leaving as-is')
        return 'skipped'

    artist, title = read_metadata(folder)

    local_video = os.path.join(folder, 'video.mp4')
    if os.path.exists(local_video) and _has_audio_stream(local_video):
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
        if ms is not None:
            values = {'video_start_time': str(ms), 'backstagehero_sync': SYNC_MEASURED}
            # fold the resolution into this same write instead of a second
            # one right after it; _probe_resolution_value() is a safe no-op
            # if video.mp4 isn't actually present.
            res = _probe_resolution_value(folder)
            if res and res != '?':
                values['backstagehero_res'] = res
            if set_ini_values(folder, values):
                print('  Re-synced: ' + info)
                return
        print('  Known source no longer matches - falling back to search')

    # Last resort: search for a candidate that matches the chart. Note what this
    # can and cannot tell us -- we only get here because the video actually on
    # disk did NOT fingerprint-match. A fresh candidate matching the chart says
    # nothing about the local file's timing, so its offset must not be written
    # over the local video's: that would store a measurement of a video the user
    # doesn't have, and (worse) stamp it SYNC_MEASURED, manufacturing exactly the
    # false "this one is trustworthy" signal the provenance marker exists to
    # prevent. Report the mismatch and leave the timing alone; fixing it needs a
    # re-download, which is Search & Download's job, not Auto-sync's.
    query = build_query(artist, title)
    print('\nRe-syncing: ' + query)
    candidates = search_candidates(query)
    _, vid_title, _, matched, _, _ = select_video(folder, candidates, sync_ready)
    if matched:
        print('  The video on disk no longer matches this chart (a different '
              'upload does: ' + vid_title + ').')
        print('  Timing left unchanged - re-download the song to fix it.')
    else:
        print('  No confident match - timing left unchanged')


def _read_ini_section(folder):
    """Parse song.ini's [song] section once. Returns a dict of key->value
    (lowercased keys, stripped values), or None if the file/section is
    missing. The shared parse-once primitive behind _read_ini_value and
    every multi-key reader, so a caller needing several fields opens and
    parses the file once instead of once per key."""
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
            return {k: (v or '').strip() for k, v in cp.items(sec)}
    return None


def _read_ini_value(folder, key):
    """Return a single [song] value from song.ini (stripped), or None."""
    section = _read_ini_section(folder)
    if section is None:
        return None
    return section.get(key.lower()) or None


def _has_audio_stream(path):
    """True if this file carries an audio track ffmpeg could decode.

    Fails CLOSED on any probe error: the only caller uses this to decide
    whether to bother trying a local fingerprint, and "couldn't tell" should
    fall through to the network path that works rather than attempt a decode
    that is about to fail anyway.
    """
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'a',
             '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', str(path)],
            capture_output=True, timeout=15, creationflags=NO_WINDOW,
            **library_common.TEXT_UTF8)
        return 'audio' in (result.stdout or '')
    except Exception:
        return False


def get_stored_source(folder, section=None):
    """The backstagehero_source video ID stored in song.ini, or None.
    Pass `section` (from _read_ini_section) to reuse an already-parsed
    file instead of re-reading it."""
    if section is None:
        return _read_ini_value(folder, 'backstagehero_source')
    return section.get('backstagehero_source') or None


# Uploads the user has thrown away for this song, comma-separated in song.ini.
#
# Deleting a bad video is not enough on its own: the YouTube search is
# effectively deterministic, so the very next run finds the same wrong upload,
# downloads it again, and the user is back where they started. Remembering
# what was rejected is what makes "dump this video" actually mean something.
REJECTED_KEY = 'backstagehero_rejected'


def get_rejected_sources(folder, section=None):
    """Video IDs the user has dumped for this song, as a set."""
    if section is None:
        raw = _read_ini_value(folder, REJECTED_KEY) or ''
    else:
        raw = section.get(REJECTED_KEY.lower()) or ''
    return {part.strip() for part in raw.split(',') if part.strip()}


def dump_video(folder):
    """Throw away this song's video and remember not to fetch it again.

    Returns {'status', 'detail'}. Statuses: 'dumped', 'nothing_to_dump',
    'failed'.

    Ordered so the song is never left in a state that hides the problem: the
    rejection is recorded BEFORE the file is removed, because a delete that
    succeeds without a recorded rejection is the one outcome that guarantees
    the same wrong video comes straight back.
    """
    folder = str(folder)
    video_path = os.path.join(folder, 'video.mp4')
    section = _read_ini_section(folder) or {}
    marker = section.get(static_art.VIDEO_MARKER_KEY.lower()) or None
    was_converted = marker == static_art.VIDEO_MARKER_STATIC_ART
    if not os.path.exists(video_path) and not was_converted:
        return {'status': 'nothing_to_dump', 'detail': 'this song has no video'}

    vid = get_stored_source(folder, section)
    values = {'backstagehero_source': ''}
    if vid:
        rejected = get_rejected_sources(folder, section)
        rejected.add(vid)
        values[REJECTED_KEY] = ','.join(sorted(rejected))
    if was_converted:
        # the static-art pass turned this upload into album art. Dumping it
        # has to undo that too, or the song stays permanently skipped and the
        # picture the user didn't want stays on disk.
        values[static_art.VIDEO_MARKER_KEY] = ''

    if not set_ini_values(folder, values):
        return {'status': 'failed',
                'detail': 'song.ini has no [song] section - nothing was removed'}

    removed = []
    if os.path.exists(video_path):
        try:
            os.remove(video_path)
            removed.append('video.mp4')
        except OSError as e:
            log.error('could not remove dumped video %s: %s', video_path, e)
            return {'status': 'failed',
                    'detail': f'video.mp4 could not be removed ({e}); '
                              f'the upload is recorded as rejected either way'}
    if was_converted:
        # only art this app extracted -- the marker is what says so. A user's
        # own album art was never touched by the conversion and isn't now.
        art = os.path.join(folder, 'album.png')
        if os.path.exists(art):
            try:
                os.remove(art)
                removed.append('album.png')
            except OSError as e:
                log.warning('could not remove extracted album art %s: %s', art, e)

    note = ' and '.join(removed) if removed else 'nothing on disk'
    detail = f'removed {note}'
    if vid:
        detail += f'; {vid} will be skipped in future searches'
    else:
        detail += '; no source ID was stored, so it cannot be excluded by ID'
    return {'status': 'dumped', 'detail': detail}


def get_stored_resolution(folder, section=None):
    """The stored backstagehero_res value (e.g. '720p'), or None."""
    if section is None:
        return _read_ini_value(folder, 'backstagehero_res')
    return section.get('backstagehero_res') or None


def _probe_resolution_value(folder):
    """Read video.mp4's height via ffmpeg without writing it anywhere.
    Returns 'NNNp' on success, '?' if it couldn't be read, or None if
    there's nothing to probe (no ffmpeg, no video.mp4 yet). The pure-compute
    half of probe_resolution() - lets a caller that's about to write several
    song.ini fields at once fold the resolution into that single write
    instead of triggering probe_resolution()'s own separate one."""
    if not ffmpegAvailable:
        return None
    video = os.path.join(folder, 'video.mp4')
    if not os.path.exists(video):
        return None
    try:
        r = subprocess.run(
            ['ffmpeg', '-hide_banner', '-i', video],
            capture_output=True, timeout=10,
            creationflags=NO_WINDOW, **library_common.TEXT_UTF8)
        # Search only the Video: stream line so an embedded cover-art stream
        # (e.g. mjpeg 640x640) doesn't shadow the real video dimensions.
        video_line = next((l for l in r.stderr.splitlines() if 'Video:' in l), '')
        m = re.search(r'(\d{3,4})x(\d{3,4})', video_line or r.stderr)
        if m:
            return f'{int(m.group(2))}p'
    except Exception:
        log.debug('Resolution probe failed for %s', folder, exc_info=True)
    return '?'


def probe_resolution(folder):
    """Read video.mp4's height via ffmpeg, cache it in song.ini, and return it
    (e.g. '720p'), '?' if it couldn't be read, or None if there's nothing to
    probe. Shared by the download flow and the GUI's library scan."""
    res = _probe_resolution_value(folder)
    if res and res != '?':
        set_ini_values(folder, {'backstagehero_res': res})
    return res


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
                if process_resync(folder, song_name, sync_ready) == 'skipped':
                    return 'skipped'
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
