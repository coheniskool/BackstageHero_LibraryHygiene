# library_common.py
# Shared helpers used by the ported library-hygiene modules (video_repair.py,
# chart_rename.py, dedupe_report.py, and metadata enrichment). Ported from
# clonehero-video-downloader's clonehero_video_offset.py / CH-VideoScript.py,
# where several of these existed as near-duplicates across two files -- this
# module is the single home for them going forward.

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def make_console_encoding_safe():
    """Make print() incapable of killing a library scan.

    Windows consoles (and pythonw's devnull sink) default to cp1252, which
    cannot encode a song title containing a heart, an umlaut in the wrong
    codepage, or any CJK text. Printing one raised UnicodeEncodeError from
    inside the scan loop -- after the folder had already been moved, and with
    every remaining folder left unprocessed. Worse, it fired during dry runs
    too, so a report truncated by the crash read as a clean bill of health.

    Idempotent and safe to call from any entry point; streams that don't
    support reconfigure (pytest's capture, a frozen build's null sink) are
    left alone rather than replaced.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError, OSError):
            pass

# --- File discovery -------------------------------------------------------
#
# Clone Hero folders in the wild use inconsistent filenames depending on
# which tool produced the chart (a numeric-ID suffix like "song_1877.ogg"
# instead of "song.ogg" is common). These helpers glob rather than match a
# fixed name so a song folder is never silently skipped just because of that.

# Every canonical background-video filename Clone Hero recognizes.
# BackstageHero's own downloads only ever produce "video.mp4", but a real
# library can contain video files left by other tools (or this project's own
# predecessor) using the other three -- the standalone video-repair scan
# still needs to find and judge those.
VIDEO_NAMES = ('video.mp4', 'video.avi', 'video.webm', 'video.ogv')


# --- Song-folder discovery ------------------------------------------------
#
# What counts as a song folder, and why this isn't simply "has song.ini".
#
# The app itself finds songs with a recursive **/song.ini glob (see
# gui._scan_library and gui._validate_folder, which tells the user to pick
# "the folder that contains all your song packs"). So a nested library --
# Songs/<Pack>/<Song>/, or deeper -- is the normal case, not an edge case.
#
# The hygiene tools originally walked exactly one level with iterdir() and
# treated every immediate child as a song folder. On a nested library that
# silently disagreed with the app: each PACK folder looked like a song folder
# with no .ini, so a tool that relocates unrecognised folders would move the
# whole pack -- every valid song inside it -- into _needs_review, reported as
# one innocuous line. Pointed at a Songs/<Artist>/<Song>/ layout, the first
# run emptied the library.
#
# "Has song.ini" is too strict a test to fix it with, because repairing an
# ID-suffixed song_2400.ini is precisely what chart_rename exists to do and
# that folder still has to be found. So: a folder is a SONG folder if it
# directly holds any of the file types a song is made of; a folder that holds
# none of them but does contain song folders is a CONTAINER to descend into;
# and a folder that is neither is left alone entirely. Unrecognised is not
# the same as broken, and only the tools' own explicit checks -- never the
# directory walk -- should be able to send a folder to review.

SONG_FOLDER_MARKER_EXTENSIONS = ('.ini', '.chart', '.mid', '.midi', '.sng')


def looks_like_song_folder(folder):
    """True if this directory holds song content itself, rather than holding
    other song folders. Deliberately generous: an ID-suffixed song_2400.ini
    or a bare notes.chart counts, since those broken states are exactly what
    the hygiene tools are for."""
    try:
        for path in Path(folder).iterdir():
            if path.is_file() and path.suffix.lower() in SONG_FOLDER_MARKER_EXTENSIONS:
                return True
    except OSError:
        pass
    return False


def iter_song_folders(home_folder, skip_prefixes=('_',)):
    """Yield every song folder under home_folder, at any depth, sorted.

    Mirrors the recursive discovery the app uses, so the hygiene tools and the
    downloader agree on what a song is. Folders whose names start with any of
    skip_prefixes (the review folders these tools create) are never entered,
    nor are symlinked directories -- a symlink pointing back up the tree would
    otherwise recurse forever. A song folder is never descended into either: a
    song does not contain other songs, and its stems must not be mistaken for
    a nested library.
    """
    try:
        entries = sorted(p for p in Path(home_folder).iterdir() if p.is_dir())
    except OSError:
        return
    for folder in entries:
        if folder.name.startswith(skip_prefixes) or folder.is_symlink():
            continue
        if looks_like_song_folder(folder):
            yield folder
        else:
            yield from iter_song_folders(folder, skip_prefixes)


def find_song_audio(song_dir):
    """The song folder's full backing-mix audio file, or None.

    Filename is always "song*" regardless of numeric suffix -- some
    libraries use "song.ogg", others "song_1877.ogg" from a different chart
    source. Falls back to a lone audio file only when there's exactly one
    candidate in the folder (never guesses between multiple stems).
    """
    song_dir = Path(song_dir)
    for ext in ('.ogg', '.opus', '.mp3', '.wav'):
        matches = sorted(song_dir.glob('song*' + ext))
        if matches:
            return matches[0]
    audio_files = [f for f in song_dir.glob('*') if f.suffix.lower() in {'.ogg', '.mp3', '.wav', '.opus'}]
    return audio_files[0] if len(audio_files) == 1 else None


def find_song_ini(song_dir):
    """The song folder's song.ini, or None.

    There is always exactly one *.ini file per song folder regardless of
    chart source, so match on that, preferring the literal "song.ini" name
    when more than one .ini exists (an ID-suffixed leftover alongside an
    already-correct one).
    """
    song_dir = Path(song_dir)
    ini_files = sorted(song_dir.glob('*.ini'))
    if not ini_files:
        return None
    return next((p for p in ini_files if p.name.lower() == 'song.ini'), ini_files[0])


def find_video_file(song_dir):
    """The song folder's background video file (any recognized name), or None."""
    song_dir = Path(song_dir)
    for name in VIDEO_NAMES:
        candidate = song_dir / name
        if candidate.exists():
            return candidate
    return None


def read_song_ini_fields(ini_path, keys):
    """Read specific top-level fields from a song.ini file, read-only.

    Regex-based (not configparser) so this stays consistent with the
    byte-preserving-regex philosophy the writer side of this project uses --
    but this is read-only, so no formatting-preservation concerns apply.
    """
    ini_path = Path(ini_path)
    try:
        text = ini_path.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return {}
    fields = {}
    for key in keys:
        match = re.search(rf'(?im)^[ \t]*{re.escape(key)}[ \t]*=[ \t]*(.*?)[ \t]*$', text)
        if match:
            fields[key.lower()] = match.group(1)
    return fields


_CHART_NAME_RE = re.compile(r'(?im)^\s*name\s*=\s*"([^"]*)"')
_CHART_ARTIST_RE = re.compile(r'(?im)^\s*artist\s*=\s*"([^"]*)"')


def read_chart_song_fields(chart_path):
    """Read the [Song] section's Name/Artist text fields from a .chart file.

    Plain-text regex, not a full .chart parser. Returns {} if the file can't
    be read or neither field is present.
    """
    chart_path = Path(chart_path)
    try:
        text = chart_path.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return {}
    fields = {}
    name_match = _CHART_NAME_RE.search(text)
    if name_match:
        fields['name'] = name_match.group(1)
    artist_match = _CHART_ARTIST_RE.search(text)
    if artist_match:
        fields['artist'] = artist_match.group(1)
    return fields


def probe_audio_duration_ms(audio_path):
    """Return an audio file's duration in milliseconds via ffprobe, or None on failure."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(audio_path)],
            check=True, capture_output=True, text=True,
        )
        data = json.loads(result.stdout)
        duration = data.get('format', {}).get('duration')
        if duration is None:
            return None
        return round(float(duration) * 1000)
    except Exception as e:
        logging.error(f'ffprobe duration probe error {audio_path}: {e}')
        return None


# --- Title/name normalization -----------------------------------------------
#
# One canonical normalizer, replacing two near-identical copies that used to
# live in clonehero_video_offset.py (_normalize_for_match) and
# CH-VideoScript.py (normalize_lookup_value).

def normalize_lookup_value(value):
    """Lowercase, alnum-only normalization for fuzzy artist/title matching."""
    if value is None:
        return ''
    return re.sub(r'[^a-z0-9]+', ' ', str(value).strip().lower()).strip()


_TRAILING_PAREN_NOISE_RE = re.compile(
    r'\s*[\(\[]\s*(?:'
    r'feat\.?|ft\.?|featuring|live|radio\s*edit|acoustic|explicit|clean|'
    r'single\s*version|album\s*version|remix|'
    r'remaster(?:ed)?(?:\s*\d{2,4})?'
    r')\b[^)\]]*[\)\]]\s*$',
    re.IGNORECASE,
)

_TRAILING_DASH_NOISE_RE = re.compile(
    r'\s*-\s*(?:'
    r'feat\.?|ft\.?|featuring|live|radio\s*edit|'
    r'single\s*version|album\s*version|'
    r'remaster(?:ed)?(?:\s*\d{2,4})?|\d{2,4}\s*remaster'
    r')\b.*$',
    re.IGNORECASE,
)

_PEDAL_NOISE_RE = re.compile(
    r'\s*[\(\[]\s*(?:\d+\s*x\s+)?(?:bass\s+)?pedal(?:\s*\d+\s*x)?'
    r'(?:\s*(?:expert|hard|medium|easy|normal|plus|\+))?[^)\]]*[\)\]]\s*$',
    re.IGNORECASE,
)

_PEDAL_IN_TITLE_RE = re.compile(
    r'\b(?:\d+\s*x\s+)?(?:bass\s+)?pedal(?:\s*\d+\s*x)?'
    r'(?:\s*(?:expert|hard|medium|easy|normal|plus|\+))?\b',
    re.IGNORECASE,
)


def strip_title_noise(title):
    """Strip trailing noise patterns (feat./live/remaster/pedal markers) from a title."""
    if not title:
        return title
    cleaned = title
    for pattern in (_TRAILING_DASH_NOISE_RE, _TRAILING_PAREN_NOISE_RE, _PEDAL_NOISE_RE):
        candidate = pattern.sub('', cleaned).strip()
        if candidate:
            cleaned = candidate
    cleaned = _PEDAL_IN_TITLE_RE.sub('', cleaned).strip()
    return cleaned


def parse_folder_name(folder_name):
    """Parse a Clone Hero folder name into (artist, title).

    Common patterns: "Artist - Song Title", "Song Title", "Artist-Song Title".
    Returns ("", folder_name) if no separator is found.
    """
    if ' - ' in folder_name:
        parts = folder_name.split(' - ', 1)
        artist, title = parts[0].strip(), parts[1].strip()
    elif ' -' in folder_name and folder_name.count(' -') == 1:
        parts = folder_name.split(' -', 1)
        artist, title = parts[0].strip(), parts[1].strip()
    elif '- ' in folder_name and folder_name.count('- ') == 1:
        parts = folder_name.split('- ', 1)
        artist, title = parts[0].strip(), parts[1].strip()
    else:
        artist, title = '', folder_name

    artist = strip_title_noise(artist)
    title = strip_title_noise(title)
    return artist, title


# --- Review-folder relocation -----------------------------------------------
#
# Unifies clonehero_video_offset.py's move_to_needs_review() and
# dedupe_report.py's move_to_duplicates_review(), which were the same
# same-volume/cross-volume/manifest logic copy-pasted into two modules.
#
# The review folder is a SIBLING of home_folder, never nested inside it.
# Clone Hero recursively scans its library root for song.ini/notes.chart/
# notes.mid with no awareness of this project's folder-naming convention --
# confirmed empirically (2026-07-18): a folder relocated to a nested
# "_needs_review" was still visible and loaded in-game, in whatever broken
# state caused the relocation in the first place. A review folder only
# actually keeps something "out of the game's way" if it lives outside the
# path Clone Hero is pointed at.
#
# review_root_name is a suffix like "_needs_review" or "_duplicates_review";
# the review folder is named f"{home_folder.name}{review_root_name}" (e.g.
# "Songs_needs_review") so review folders from different libraries sharing
# a parent directory never collide. The manifest file lives alongside it as
# f"{home_folder.name}{review_root_name}_manifest.jsonl".

def _dest_is_same_volume(source, dest_parent):
    try:
        return os.stat(source).st_dev == os.stat(dest_parent).st_dev
    except OSError:
        return False


def _folder_size_and_count(folder):
    total_size = 0
    count = 0
    for p in Path(folder).rglob('*'):
        if p.is_file():
            total_size += p.stat().st_size
            count += 1
    return total_size, count


def _review_root(home_folder, review_root_name):
    home_folder = Path(home_folder)
    return home_folder.parent / f'{home_folder.name}{review_root_name}'


def _append_review_manifest(home_folder, review_root_name, source, dest, reason,
                             cross_volume, verification, extra_fields=None):
    home_folder = Path(home_folder)
    manifest_path = home_folder.parent / f'{home_folder.name}{review_root_name}_manifest.jsonl'
    entry = {
        'source': str(source),
        'destination': str(dest),
        'reason': reason,
        'cross_volume': cross_volume,
        'verification': verification,
    }
    if extra_fields:
        entry.update(extra_fields)
    with manifest_path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry) + '\n')


def move_to_review(song_dir, home_folder, review_root_name, reason,
                    extra_manifest_fields=None, dry_run=False):
    """Relocate song_dir intact into a review folder that is a SIBLING of
    home_folder (never nested inside it -- see the module comment above).

    Same-volume moves use shutil.move() (atomic rename under the hood).
    Cross-volume moves copy to the destination first, verify total file
    count and byte size match the source, and only remove the source after
    that verification passes -- an interrupted cross-volume move must never
    leave the library in a state where the folder exists nowhere complete.
    Every move (either case) is appended to a JSONL manifest. Raises
    RuntimeError (source left untouched) if cross-volume verification fails.

    dry_run=True returns None immediately without moving, copying, or
    logging anything -- callers can pass dry_run through uniformly instead
    of guarding the call themselves.
    """
    if dry_run:
        return None

    song_dir = Path(song_dir)
    home_folder = Path(home_folder)
    review_root = _review_root(home_folder, review_root_name)
    review_root.mkdir(parents=True, exist_ok=True)

    dest = review_root / song_dir.name
    if dest.exists():
        suffix = 1
        while (review_root / f'{song_dir.name} [dup{suffix}]').exists():
            suffix += 1
        dest = review_root / f'{song_dir.name} [dup{suffix}]'

    cross_volume = not _dest_is_same_volume(song_dir, review_root)

    if not cross_volume:
        shutil.move(str(song_dir), str(dest))
        _append_review_manifest(home_folder, review_root_name, song_dir, dest,
                                 reason, cross_volume, 'not_applicable', extra_manifest_fields)
        return dest

    source_size, source_count = _folder_size_and_count(song_dir)
    shutil.copytree(str(song_dir), str(dest))
    dest_size, dest_count = _folder_size_and_count(dest)

    if dest_size != source_size or dest_count != source_count:
        _append_review_manifest(home_folder, review_root_name, song_dir, dest,
                                 reason, cross_volume, 'failed', extra_manifest_fields)
        raise RuntimeError(
            f'cross-volume copy verification failed for {song_dir.name}: '
            f'source had {source_count} files/{source_size} bytes, '
            f'destination has {dest_count} files/{dest_size} bytes -- source left untouched, '
            f'incomplete copy left at {dest}'
        )

    shutil.rmtree(str(song_dir))
    _append_review_manifest(home_folder, review_root_name, song_dir, dest,
                             reason, cross_volume, 'ok', extra_manifest_fields)
    return dest
