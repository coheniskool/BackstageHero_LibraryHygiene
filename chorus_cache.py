# chorus_cache.py
# Caches chorus_client.search_by_artist_title() responses so a library-wide
# enrichment run doesn't re-hit the Chorus API for every song on every scan
# (see SPEC-library-enrichment.md / tasks/plan-library-enrichment.md, Task 1.3).
#
# chorus_client is imported as a module, not `from chorus_client import
# search_by_artist_title`, so tests can monkeypatch
# chorus_cache.chorus_client.search_by_artist_title -- the same convention
# metadata_enrichment.py's own tests already use for the same function.

import json
import logging
import os
import time
from pathlib import Path

import chorus_client
import library_common

log = logging.getLogger('backstagehero')

DEFAULT_TTL_DAYS = 7
_SECONDS_PER_DAY = 86400


def _cache_key(artist, title):
    return (library_common.normalize_lookup_value(artist)
            + '\x1f' + library_common.normalize_lookup_value(title))


class CachedChorusClient:
    """Wraps chorus_client.search_by_artist_title() with an artist+title
    keyed cache. `None` results (a confirmed no-match) are cached too --
    repeating a lookup Chorus doesn't have shouldn't re-hit the network on
    every scan. Optional on-disk persistence (cache_path) survives across
    runs; without it, the cache is in-memory only for this instance's life.
    """

    def __init__(self, cache_path=None, ttl_days=DEFAULT_TTL_DAYS):
        self.cache_path = Path(cache_path) if cache_path else None
        self.ttl_seconds = ttl_days * _SECONDS_PER_DAY
        self._entries = {}
        self._load()

    def _load(self):
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            with open(self.cache_path, encoding='utf-8') as f:
                self._entries = json.load(f)
        except (OSError, ValueError) as e:
            log.warning('Could not read Chorus cache %s: %s', self.cache_path, e)
            self._entries = {}

    def _save(self):
        if not self.cache_path:
            return
        tmp_path = self.cache_path.with_name(self.cache_path.name + '.tmp')
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self._entries, f)
            os.replace(tmp_path, self.cache_path)
        except OSError as e:
            log.warning('Could not write Chorus cache %s: %s', self.cache_path, e)

    def search_by_artist_title(self, artist, title, force=False):
        key = _cache_key(artist, title)
        if not force:
            entry = self._entries.get(key)
            if entry is not None and (time.time() - entry['cached_at']) < self.ttl_seconds:
                return entry['result']

        result = chorus_client.search_by_artist_title(artist, title)
        self._entries[key] = {'result': result, 'cached_at': time.time()}
        self._save()
        return result
