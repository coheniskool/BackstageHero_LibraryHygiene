# chorus_client.py
# Shared Chorus Encore API client -- used by metadata_enrichment.py and
# dedupe_report.py. One HTTP client, not two. Ported as-is from
# clonehero-video-downloader's chorus_client.py.
#
# Real request/response schema sourced from Bridge's (github.com/Geomitron/Bridge)
# actual TypeScript source -- src-angular/app/core/services/search.service.ts
# and src-shared/interfaces/search.interface.ts. The dead chorus.fightthe.pw
# README describes a GET .../search?query= API that no longer exists; the
# real, live endpoint for an artist/title lookup is a POST to
# /search/advanced with a structured body.

import json
import logging

import requests

log = logging.getLogger('backstagehero')

CHORUS_API_BASE_URL = 'https://api.enchor.us'
CHORUS_REQUEST_TIMEOUT_SECONDS = 15
# Matches resolver_client's cap. A single search result is a few KB.
MAX_RESPONSE_BYTES = 1 << 20


def _blank_text_filter():
    return {'value': '', 'exact': False, 'exclude': False}


def search_by_artist_title(artist, title):
    """Look up a song on Chorus Encore by artist+title.

    Returns the single best-matching chart's data dict (name/artist/album/
    genre/year/charter/chartId/md5/chartHash/song_length/diff_*/...), or
    None on no match, network failure, or any unexpected response shape.
    Never raises -- both consuming features require a single song's lookup
    failure to never abort a library-wide run.

    Match-confidence filtering is the caller's responsibility -- this
    returns the server's top-ranked result for a fuzzy (non-exact)
    name+artist match, nothing more.
    """
    # Every AdvancedSearchSchema field must be present (nullable, not
    # optional) -- Bridge's zod schema requires the key even when unused.
    body = {
        'per_page': 1,
        'page': 1,
        'name': {'value': title or '', 'exact': False, 'exclude': False},
        'artist': {'value': artist or '', 'exact': False, 'exclude': False},
        'album': _blank_text_filter(),
        'genre': _blank_text_filter(),
        'year': _blank_text_filter(),
        'charter': _blank_text_filter(),
        'instrument': None,
        'difficulty': None,
        'drumType': None,
        'drumsReviewed': True,
        'minLength': None, 'maxLength': None,
        'minIntensity': None, 'maxIntensity': None,
        'minAverageNPS': None, 'maxAverageNPS': None,
        'minMaxNPS': None, 'maxMaxNPS': None,
        'modifiedAfter': None,
        'hash': None,
        'hasSoloSections': None, 'hasForcedNotes': None, 'hasOpenNotes': None, 'hasTapNotes': None,
        'hasLyrics': None, 'hasVocals': None, 'hasRollLanes': None, 'has2xKick': None,
        'hasIssues': None, 'hasVideoBackground': None, 'modchart': None,
        'sort': None,
        'source': 'bridge',
    }

    try:
        response = requests.post(
            f'{CHORUS_API_BASE_URL}/search/advanced', json=body, timeout=CHORUS_REQUEST_TIMEOUT_SECONDS,
            stream=True,
        )
        response.raise_for_status()

        # Cap the body before parsing it. timeout= bounds socket silence, not
        # total size, so without this a huge (or endless) response is read
        # straight into memory. resolver_client.resolve() already caps at the
        # same 1 MiB; a single search result is a few KB.
        raw = response.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
        if len(raw) > MAX_RESPONSE_BYTES:
            log.error('Chorus response exceeded %d bytes; ignoring', MAX_RESPONSE_BYTES)
            return None
        data = json.loads(raw.decode('utf-8', 'replace'))

        # Validate the shape rather than just its truthiness. This function
        # promises "a result dict or None", and every caller relies on that by
        # calling .get() on what comes back. A response whose 'data' is a bare
        # string is truthy and indexable, so the old check returned data[0] --
        # a single CHARACTER -- and the AttributeError landed in the caller's
        # per-song loop, outside this try, taking the whole library scan with
        # it. Doesn't need a hostile server; a schema change is enough.
        if not isinstance(data, dict):
            return None
        results = data.get('data')
        if not isinstance(results, list) or not results:
            return None
        first = results[0]
        return first if isinstance(first, dict) else None
    except Exception as e:
        log.error(f'Chorus lookup error for artist={artist!r} title={title!r}: {e}')
        return None
