# metadata_enrichment.py
# Fills blank song.ini metadata (year/genre/charter/album) from a confident
# Chorus Encore match. Never overwrites a field that already has a value.
# Ported from clonehero-video-downloader's CH-VideoScript.py, writing via
# VideoDownload.set_ini_values() instead of a separately-ported ini-patch
# function (see tasks/plan.md finding 2 -- avoids shipping two competing
# ini writers).

import re
from difflib import SequenceMatcher
from pathlib import Path

import chorus_client
import library_common
import VideoDownload

# Chorus fields are spliced into song.ini via set_ini_values(), not parsed
# by configparser -- an unsanitized value with a stray [, ], ;, #, or
# embedded newline could corrupt the file or desync a later key. Rejects
# rather than partially cleans, so a bad value never silently becomes a
# different bad value.
_UNSAFE_INI_CHARS_RE = re.compile(r'[\[\];#\r\n]')


def sanitize_chorus_field(value):
    """Reject a Chorus-sourced field value unless it's safe to write to song.ini.

    Returns None if the value is unsafe, non-string, or empty after
    stripping; otherwise the stripped value, capped at 200 characters.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    if _UNSAFE_INI_CHARS_RE.search(value):
        return None
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return None
    return value.strip()[:200]


# Real fields Chorus Encore returns that this feature fills. Widening this
# list is a deliberate "ask first" decision, not a default.
CHORUS_FILLABLE_KEYS = ('year', 'genre', 'charter', 'album')

# SequenceMatcher ratio*100 on normalized name+artist, both required --
# lower than chart-rename's 85 because a wrong metadata fill (a slightly-off
# genre or year) is far less destructive than a bad file rename.
CHORUS_MATCH_CONFIDENCE_THRESHOLD = 70


def _chorus_match_confidence(ini_fields, chorus_result):
    """Return the weaker of the name/artist SequenceMatcher scores (0-100)."""
    name_score = SequenceMatcher(
        None,
        library_common.normalize_lookup_value(ini_fields.get('name')),
        library_common.normalize_lookup_value(chorus_result.get('name')),
    ).ratio() * 100
    artist_score = SequenceMatcher(
        None,
        library_common.normalize_lookup_value(ini_fields.get('artist')),
        library_common.normalize_lookup_value(chorus_result.get('artist')),
    ).ratio() * 100
    return min(name_score, artist_score)


def fill_song_ini_metadata(song_folder, dry_run=False):
    """Look up a song on Chorus Encore and fill blank song.ini fields from a confident match.

    Returns {'status': ..., 'detail': ...}. Statuses: 'filled' (>=1 field
    written), 'no_change' (song.ini already complete, or Chorus had nothing
    safe/new to add), 'no_match' (no Chorus result, or the best result's
    confidence is below CHORUS_MATCH_CONFIDENCE_THRESHOLD), 'error' (no
    song.ini found, it's missing name/artist to look up by, or it has no
    [song] section for set_ini_values() to write into).

    dry_run=True computes the same outcome without writing anything.
    """
    folder = Path(song_folder)
    ini_path = library_common.find_song_ini(folder)
    if ini_path is None:
        return {'status': 'error', 'detail': 'no song.ini found'}

    ini_fields = library_common.read_song_ini_fields(ini_path, ('name', 'artist') + CHORUS_FILLABLE_KEYS)
    if not ini_fields.get('name') or not ini_fields.get('artist'):
        return {'status': 'error', 'detail': 'song.ini missing name/artist, cannot look up'}

    chorus_result = chorus_client.search_by_artist_title(ini_fields['artist'], ini_fields['name'])
    if chorus_result is None:
        return {'status': 'no_match', 'detail': 'no Chorus result found'}

    confidence = _chorus_match_confidence(ini_fields, chorus_result)
    if confidence < CHORUS_MATCH_CONFIDENCE_THRESHOLD:
        return {
            'status': 'no_match',
            'detail': f'best match confidence {confidence:.0f} below threshold {CHORUS_MATCH_CONFIDENCE_THRESHOLD}',
        }

    to_fill = {}
    for key in CHORUS_FILLABLE_KEYS:
        if ini_fields.get(key):
            continue
        safe_value = sanitize_chorus_field(chorus_result.get(key))
        if safe_value is not None:
            to_fill[key] = safe_value

    if not to_fill:
        return {'status': 'no_change', 'detail': 'no fillable blank fields, or no safe values'}

    if not dry_run:
        if not VideoDownload.set_ini_values(str(folder), to_fill):
            return {'status': 'error', 'detail': 'song.ini missing [song] section'}
    detail = ', '.join(sorted(to_fill))
    if dry_run:
        detail += ' (dry-run, not applied)'
    return {'status': 'filled', 'detail': detail}


def enrich_song_ini_metadata_library(home_folder, dry_run=False):
    """Scan every song folder under home_folder and fill blank metadata from Chorus Encore.

    Mirrors the other hygiene scans' aggregate/summary style -- logs every
    song's outcome with a reason, never silently skips one.

    Returns the counts dict (status -> number of folders), so a caller
    (e.g. the GUI) can build its own summary without re-parsing printed
    output.
    """
    library_common.make_console_encoding_safe()
    print('=' * 70)
    print('ENRICHING SONG METADATA FROM CHORUS ENCORE' + (' (DRY RUN)' if dry_run else ''))
    print('=' * 70)

    counts = {}
    # recursive, matching the app's own **/song.ini discovery. The flat walk
    # both missed every song in a nested library AND reported one spurious
    # "no song.ini found" error per pack folder it mistook for a song.
    for folder in library_common.iter_song_folders(home_folder):
        result = fill_song_ini_metadata(str(folder), dry_run=dry_run)
        counts[result['status']] = counts.get(result['status'], 0) + 1

        if result['status'] == 'filled':
            print(f"  Filled: {folder.name}: {result['detail']}")
        elif result['status'] == 'error':
            print(f"  ERROR: {folder.name}: {result['detail']}")

    print()
    print(
        f"Enrichment complete: {counts.get('filled', 0)} filled, "
        f"{counts.get('no_change', 0)} no change needed, "
        f"{counts.get('no_match', 0)} no confident match, "
        f"{counts.get('error', 0)} error(s)."
    )
    print('=' * 70)
    print()
    return counts
