# library_enrichment.py
# Main orchestrator for the library-enrichment tool (Task 2.1 of
# tasks/plan-library-enrichment.md). Scans a Songs library and writes a JSON
# sidecar of booklet-relevant data -- see SPEC-library-enrichment.md's
# Sidecar Format for the exact shape this produces.
#
# The sidecar's `songs` key is resolver_client.chart_hash() (SHA256,
# content-based) -- reused as-is, not reinvented, and it doubles as the
# incremental-cache key: a song whose chart bytes haven't changed hashes to
# the same key, so it's already in `songs` and gets skipped without
# re-running Chorus lookups or chart parsing. `notes_mid_md5` (a SEPARATE
# hash, see library_scores.py) is only used to look up scores -- the two
# must never be conflated, per spec.

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import chorus_cache
import library_chart_parser
import library_common
import library_scores
import resolver_client
import VideoDownload

log = logging.getLogger('backstagehero')

SIDECAR_FILENAME = 'backstagehero_enrichment.json'
CHORUS_CACHE_FILENAME = 'backstagehero_chorus_cache.json'
SIDECAR_VERSION = 1

_AUDIO_STEM_EXTENSIONS = ('.ogg', '.mp3', '.wav', '.opus')
_ALBUM_ART_EXTENSIONS = ('.png', '.jpg', '.jpeg')


def _utcnow_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _list_audio_stems(folder):
    folder = Path(folder)
    names = []
    for ext in _AUDIO_STEM_EXTENSIONS:
        names.extend(p.name for p in folder.glob('*' + ext))
    return sorted(names)


def _has_album_art(folder):
    folder = Path(folder)
    return any(any(folder.glob('album*' + ext)) for ext in _ALBUM_ART_EXTENSIONS)


def _find_chart_file(folder):
    path = Path(folder) / 'notes.chart'
    return path if path.exists() else None


def _load_sidecar(path):
    if path.exists():
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get('songs'), dict):
                return data
        except (OSError, ValueError) as e:
            log.warning('Could not read sidecar %s: %s', path, e)
    return {'version': SIDECAR_VERSION, 'scanned_at': None, 'cache': {}, 'songs': {}}


def _save_sidecar(path, sidecar):
    tmp_path = path.with_name(path.name + '.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(sidecar, f, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except OSError as e:
        log.warning('Could not write sidecar %s: %s', path, e)


def _enrich_one_song(folder, chorus_client, scoredata):
    """Returns (chart_hash, entry_dict). chart_hash is None (entry_dict a
    problems list instead) when the folder has no notes.chart/notes.mid/
    notes.eof at all -- there's nothing to key the sidecar entry by."""
    folder = Path(folder)
    chart_hash = resolver_client.chart_hash(str(folder))
    if chart_hash is None:
        return None, ['no notes.chart/notes.mid/notes.eof found -- cannot identify this song']

    problems = []

    ini_path = library_common.find_song_ini(folder)
    artist, title = None, None
    song_length_ms = None
    if ini_path:
        artist, title, _ = VideoDownload.scan_song(str(folder))
        fields = library_common.read_song_ini_fields(ini_path, ['song_length'])
        raw_length = fields.get('song_length')
        if raw_length:
            try:
                song_length_ms = int(raw_length)
            except ValueError:
                problems.append(f'song.ini song_length {raw_length!r} is not numeric')
    else:
        problems.append('no song.ini found')

    chart_path = _find_chart_file(folder)
    if chart_path:
        instruments = library_chart_parser.parse_chart_instruments(chart_path)
        avg_nps = library_chart_parser.parse_chart_nps(chart_path)
        features = library_chart_parser.parse_chart_features(chart_path)
        note_count = library_chart_parser.parse_chart_note_count(chart_path)
    else:
        problems.append('no notes.chart found -- instrument/NPS/feature data unavailable')
        instruments = {name: -1 for name in library_chart_parser.INSTRUMENT_NAMES}
        avg_nps = None
        features = {
            'has_lyrics': False, 'has_solos': False,
            'has_open_notes': False, 'has_2x_kick': False, 'has_roll_lanes': False,
        }
        note_count = None

    mid_md5 = library_scores.notes_mid_md5(folder)
    score_entry = scoredata.get(mid_md5) if mid_md5 else None
    high_score = None
    if score_entry and score_entry.get('instruments'):
        high_score = max(i['score'] for i in score_entry['instruments'].values())

    chorus_match = None
    if artist and title:
        result = chorus_client.search_by_artist_title(artist, title)
        if result:
            # Match-confidence gating is metadata_enrichment.py's job (it
            # decides whether to WRITE ini fields); this is descriptive data
            # collection only, so no confidence score is claimed here.
            chorus_match = {
                'name': result.get('name'),
                'artist': result.get('artist'),
                'album': result.get('album'),
                'genre': result.get('genre'),
                'year': result.get('year'),
                'charter': result.get('charter'),
            }

    entry = {
        'folder': str(folder),
        'notes_mid_md5': mid_md5,
        'song_length_ms': song_length_ms,
        'instruments': instruments,
        'note_count': note_count,
        'avg_nps': avg_nps,
        'features': features,
        'stems': _list_audio_stems(folder),
        'has_album_art': _has_album_art(folder),
        'high_score': high_score,
        'score_detail': score_entry,
        'problems': problems,
        'chorus_match': chorus_match,
        'last_updated': _utcnow_iso(),
        'status': 'success',
    }
    return chart_hash, entry


def enrich_library(library_path, ch_data_path=None, dry_run=False, force=False,
                    chorus_cache_path=None, verbose=False):
    """Scan library_path and write/update backstagehero_enrichment.json.

    Incremental by default: a song whose resolver_client.chart_hash() is
    already a key in the sidecar is skipped without re-running Chorus
    lookups or chart parsing. force=True reprocesses everything (Chorus
    lookups still go through the cache, which has its own independent TTL).
    dry_run=True never mutates the library itself (this function never does
    that regardless), but the sidecar -- a read-only computation cache, not
    a library mutation -- is written either way, so a dry run's work is
    reused by the next real run instead of discarded.

    Returns a summary dict: songs_processed, songs_skipped, new_data_written,
    problems_found, duration_seconds.
    """
    start = time.time()
    library_path = Path(library_path)
    sidecar_path = library_path / SIDECAR_FILENAME
    sidecar = _load_sidecar(sidecar_path)

    scoredata = library_scores.read_scoredata(ch_data_path) if ch_data_path else {}
    cache_path = Path(chorus_cache_path) if chorus_cache_path else library_path / CHORUS_CACHE_FILENAME
    client = chorus_cache.CachedChorusClient(cache_path=cache_path)

    songs_processed = 0
    songs_skipped = 0
    new_data_written = 0
    problems_found = 0

    for folder in library_common.iter_song_folders(library_path):
        chart_hash, result = _enrich_one_song(folder, client, scoredata)

        if chart_hash is None:
            problems_found += 1
            if verbose:
                log.warning('%s: %s', folder, '; '.join(result))
            continue

        if not force and chart_hash in sidecar['songs']:
            songs_skipped += 1
            continue

        songs_processed += 1
        new_data_written += 1
        if result['problems']:
            problems_found += 1
        sidecar['songs'][chart_hash] = result
        if verbose:
            log.info('%s: enriched (%d problems)', folder, len(result['problems']))

    sidecar['scanned_at'] = _utcnow_iso()

    _save_sidecar(sidecar_path, sidecar)

    return {
        'songs_processed': songs_processed,
        'songs_skipped': songs_skipped,
        'new_data_written': new_data_written,
        'problems_found': problems_found,
        'duration_seconds': time.time() - start,
    }
