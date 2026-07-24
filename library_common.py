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


def ensure_stdio_not_none():
    """Give print() somewhere to go when there is no console at all.

    pythonw.exe with no console attached (a plain Explorer double-click, or
    this project's own .bat launcher) sets sys.stdout/stderr to None -- not
    closed, None. The first print() or warnings.warn() anywhere in the app or
    any dependency then dies instantly with no window and no error.

    Lives here, called from both entry points, specifically so it can be
    tested: as import-time code in gui.py it was structurally unreachable
    under pytest (pytest never sets sys.stdout to None), so deleting it would
    not have failed a single test in the suite despite the suite count being
    cited as evidence the fix worked.
    """
    opened = []
    for name in ('stdout', 'stderr'):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, 'w', encoding='utf-8', errors='replace'))
            opened.append(name)
    return opened


# --- legacy review-folder migration ---------------------------------------
#
# Review folders used to be created INSIDE the scanned library root
# (Songs/_needs_review/...). That was fixed to place them as a sibling, but
# only for folders relocated afterwards -- anything already moved stayed put,
# and is now in the worst of both worlds: iter_song_folders() skips any name
# starting with '_', so no repair scan will ever find it again, while
# gui._scan_library()'s recursive **/song.ini glob has no such filter, so the
# app's song list, auto-download and auto-resync all still act on it, and
# Clone Hero still shows it. Confirmed by execution, not assumed.
#
# The names are the literal ones the old code produced, matched exactly rather
# than by prefix -- a user's own folder starting with '_' must not be swept up.

# Matched case-insensitively against a closed list, never by '_' prefix: a
# folder merely starting with an underscore is the user's own business and
# must not be swept up by a tool that moves things.
#
# '_NeedsReview' is this project's PREDECESSOR (clonehero-video-downloader),
# found in a real library during Phase 4 alongside its video_meta.json files.
# Different tool, identical problem -- it sits inside the library root, so
# Clone Hero still loads the broken songs in it while no repair scan can reach
# them -- so it gets the same remedy.
LEGACY_REVIEW_FOLDER_NAMES = ('_needs_review', '_duplicates_review',
                              '_NeedsReview', '_DuplicatesReview')


def find_legacy_review_folders(home_folder):
    """Old-style review folders sitting inside the library root, if any."""
    home_folder = Path(home_folder)
    wanted = {name.lower() for name in LEGACY_REVIEW_FOLDER_NAMES}
    found = []
    try:
        entries = sorted(p for p in home_folder.iterdir() if p.is_dir())
    except OSError:
        return found
    for candidate in entries:
        if candidate.name.lower() in wanted and not candidate.is_symlink():
            found.append(candidate)
    return found


def migrate_legacy_review_folders(home_folder, dry_run=False):
    """Move old nested review folders out to the sibling location.

    Returns a counts dict. Each song folder is moved individually into the
    sibling review root so an existing sibling folder is merged with rather
    than clobbered, and a name that already exists on the far side is left
    where it is and reported -- never overwritten.
    """
    home_folder = Path(home_folder)
    counts = {}

    def bump(key):
        counts[key] = counts.get(key, 0) + 1

    for legacy in find_legacy_review_folders(home_folder):
        # Normalise the destination: '_NeedsReview' and '_needs_review' are the
        # same thing under two tools' naming, and must not land in two separate
        # sibling folders for the user to hunt through.
        canonical = ('_duplicates_review' if 'duplicate' in legacy.name.lower()
                     else '_needs_review')
        review_root = _review_root(home_folder, canonical)
        try:
            entries = sorted(p for p in legacy.iterdir() if p.is_dir())
        except OSError as e:
            log.error('could not read legacy review folder %s: %s', legacy, e)
            bump('unreadable')
            continue

        for song_dir in entries:
            dest = review_root / song_dir.name
            if dest.exists():
                print(f'  CONFLICT: {song_dir.name} already exists in {review_root.name} '
                      f'- left in place')
                bump('conflict')
                continue
            if dry_run:
                print(f'  Would move: {legacy.name}/{song_dir.name} -> {review_root.name}/')
                bump('would_move')
                continue
            try:
                review_root.mkdir(parents=True, exist_ok=True)
                shutil.move(str(song_dir), str(dest))
            except (OSError, shutil.Error) as e:
                log.error('could not migrate %s: %s', song_dir, e)
                print(f'  FAILED: {song_dir.name} ({e})')
                bump('failed')
                continue
            _append_review_manifest(home_folder, canonical, song_dir, dest,
                                    f'migrated out of nested {legacy.name}/',
                                    cross_volume=False, verification='migration')
            print(f'  Moved: {legacy.name}/{song_dir.name} -> {review_root.name}/')
            bump('moved')

        # only remove the old container once it is genuinely empty
        if not dry_run:
            try:
                if not any(legacy.iterdir()):
                    legacy.rmdir()
                    bump('emptied')
            except OSError:
                pass
    return counts


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

# How to read text output from ffmpeg/ffprobe/fpcalc.
#
# subprocess's text=True decodes the child's output with the LOCALE encoding,
# which is cp1252 on a default Windows install. ffprobe echoes the file path in
# its messages, so a song folder named with a heart or any CJK text produced
# bytes cp1252 cannot decode -- and the decode happens on subprocess's internal
# reader thread, where the UnicodeDecodeError surfaced as a dead thread and a
# failed probe rather than as anything a caller could catch. ffmpeg emits UTF-8,
# so decode as UTF-8 and never let an undecodable byte break a scan.
TEXT_UTF8 = {'text': True, 'encoding': 'utf-8', 'errors': 'replace'}


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


def _list_folder_entries(folder):
    """One directory listing of `folder`'s direct children (files and
    subdirectories alike), or [] if it can't be read. The single-scan
    primitive behind list_song_folder_files() and iter_song_folders()'s
    walk, so a directory is listed once instead of once per caller."""
    try:
        return list(Path(folder).iterdir())
    except OSError:
        return []


def list_song_folder_files(song_dir, entries=None):
    """The files (not subdirectories) directly inside song_dir, from one
    directory listing. Pass `entries` (from _list_folder_entries) to reuse a
    listing the caller already has instead of scanning again. Shared by
    looks_like_song_folder/find_song_audio/find_video_file, which used to
    each do their own independent glob()/exists() calls over the same
    folder."""
    if entries is None:
        entries = _list_folder_entries(song_dir)
    return [p for p in entries if p.is_file()]


def looks_like_song_folder(folder, files=None):
    """True if this directory holds song content itself, rather than holding
    other song folders. Deliberately generous: an ID-suffixed song_2400.ini
    or a bare notes.chart counts, since those broken states are exactly what
    the hygiene tools are for.

    A recognised background-video file counts too. Clone Hero only ever reads
    one from inside a song folder, so its presence identifies one just as well
    as a chart does -- and video_repair's whole job is folders that have a
    video, including any whose chart files are missing or misnamed.

    Pass `files` (from list_song_folder_files) to reuse a listing the caller
    already has.
    """
    if files is None:
        files = list_song_folder_files(folder)
    for path in files:
        if path.suffix.lower() in SONG_FOLDER_MARKER_EXTENSIONS:
            return True
        if path.name.lower() in VIDEO_NAMES:
            return True
    return False


def is_review_folder_name(name):
    """True for a folder these tools created to hold songs pulled out for review.

    Matched by NAME, never by a leading-underscore rule. That rule looked
    tidy and was wrong: a real 5,130-song library turned out to contain
    `_Weird Al_ Yankovic - White & Nerdy` -- a perfectly good song whose
    folder starts with an underscore only because the quotes in
    "Weird Al" Yankovic became underscores when it was named. Under the
    prefix rule every hygiene tool silently skipped it, so a genuine song was
    invisible to repair, enrichment and dedupe alike with nothing to indicate
    it had been passed over.

    Two shapes count: the legacy names that used to sit inside the library,
    and the current sibling naming (`Songs_needs_review`), which lands inside
    the scanned root whenever the user points the tools one level higher.
    """
    lowered = name.lower()
    if lowered in {n.lower() for n in LEGACY_REVIEW_FOLDER_NAMES}:
        return True
    return lowered.endswith(('_needs_review', '_duplicates_review'))


def iter_song_folders(home_folder, skip_names=None):
    """Yield every song folder under home_folder, at any depth, sorted.

    Mirrors the recursive discovery the app uses, so the hygiene tools and the
    downloader agree on what a song is. Review folders are never entered (see
    is_review_folder_name), nor are symlinked directories -- a symlink
    pointing back up the tree would otherwise recurse forever. A song folder
    is never descended into either: a song does not contain other songs, and
    its stems must not be mistaken for a nested library.

    skip_names, when given, replaces the review-folder test entirely; it
    exists for callers that know exactly what they want excluded.
    """
    extra = {n.lower() for n in skip_names} if skip_names is not None else None
    yield from _iter_song_folders(Path(home_folder), extra, skip_names, entries=None)


def _iter_song_folders(folder, extra, skip_names, entries):
    """Recursive worker for iter_song_folders(). `entries` is this folder's
    own listing if the caller already has one (from the parent's walk),
    avoiding a second iterdir() of the same directory that the old
    single-function version incurred on every container folder: one via
    looks_like_song_folder(), one via the recursive call re-listing it."""
    if entries is None:
        entries = _list_folder_entries(folder)
    dirs = sorted(p for p in entries if p.is_dir())
    for child in dirs:
        skip = (child.name.lower() in extra if extra is not None
                else is_review_folder_name(child.name))
        if skip or child.is_symlink():
            continue
        child_entries = _list_folder_entries(child)
        files = list_song_folder_files(child, entries=child_entries)
        if looks_like_song_folder(child, files=files):
            yield child
        else:
            yield from _iter_song_folders(child, extra, skip_names, entries=child_entries)


_AUDIO_EXTS = ('.ogg', '.opus', '.mp3', '.wav')


def find_song_audio(song_dir, files=None):
    """The song folder's full backing-mix audio file, or None.

    Filename is always "song*" regardless of numeric suffix -- some
    libraries use "song.ogg", others "song_1877.ogg" from a different chart
    source. Falls back to a lone audio file only when there's exactly one
    candidate in the folder (never guesses between multiple stems).

    Pass `files` (from list_song_folder_files) to reuse a listing the caller
    already has instead of re-scanning the folder up to 5 times (once per
    extension, plus the fallback pass).
    """
    song_dir = Path(song_dir)
    if files is None:
        files = list_song_folder_files(song_dir)
    for ext in _AUDIO_EXTS:
        matches = sorted(f for f in files
                          if f.suffix.lower() == ext and f.name.lower().startswith('song'))
        if matches:
            return matches[0]
    audio_files = [f for f in files if f.suffix.lower() in _AUDIO_EXTS]
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


def find_video_file(song_dir, files=None):
    """The song folder's background video file (any recognized name), or None.

    Pass `files` (from list_song_folder_files) to reuse a listing the caller
    already has instead of a separate .exists() stat call per candidate name.
    """
    song_dir = Path(song_dir)
    if files is None:
        files = list_song_folder_files(song_dir)
    lookup = {f.name.lower(): f for f in files}
    for name in VIDEO_NAMES:
        candidate = lookup.get(name)
        if candidate is not None:
            return candidate
    return None


_INI_LINE_RE = re.compile(r'(?im)^[ \t]*([^=\r\n]+?)[ \t]*=[ \t]*(.*?)[ \t]*$')


def _parse_ini_lines(text):
    """Every top-level key=value line in an .ini-shaped text, as a dict with
    lowercased keys. One regex pass over the whole text rather than one
    search per key. A key's FIRST occurrence wins on a duplicate, matching
    what a per-key re.search would have found."""
    fields = {}
    for match in _INI_LINE_RE.finditer(text):
        fields.setdefault(match.group(1).lower(), match.group(2))
    return fields


def read_song_ini_fields(ini_path, keys):
    """Read specific top-level fields from a song.ini file, read-only.

    Regex-based (not configparser) so this stays consistent with the
    byte-preserving-regex philosophy the writer side of this project uses --
    but this is read-only, so no formatting-preservation concerns apply.
    One parse of the file regardless of how many keys are requested.
    """
    ini_path = Path(ini_path)
    try:
        text = ini_path.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return {}
    parsed = _parse_ini_lines(text)
    wanted = {key.lower() for key in keys}
    return {k: v for k, v in parsed.items() if k in wanted}


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
            check=True, capture_output=True, **TEXT_UTF8,
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
