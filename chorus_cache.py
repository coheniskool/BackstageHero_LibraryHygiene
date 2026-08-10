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

# Batched-write thresholds (SPEC-chorus-cache-write-perf.md). Measured against
# the live cache on 2026-08-10: 126.42 MB across 7,743 entries, 0.75s just to
# json.dumps it -- and _save() rewrote all of it after every single lookup, so
# a full pass over the ~7,800-song library meant hours of serialization and
# close to a terabyte of writes to persist a few hundred KB of metadata.
#
# These numbers bound WORST-CASE ENTRY LOSS ON A KILL, not throughput. Once
# the file is small a flush costs single-digit milliseconds, so the only
# question they answer is "how much re-lookup does an abrupt exit cost?" --
# 25 entries is a few seconds of re-work on the next run. The time bound is
# what covers a slow, rate-limited run that trickles in roughly one lookup a
# minute and would otherwise never reach the insert threshold at all.
FLUSH_EVERY_N_INSERTS = 25
FLUSH_EVERY_SECONDS = 60


# The only consumer of a cached result is library_enrichment._enrich_one_song,
# which reads exactly these six fields. The full Chorus response carries far
# more: measured on the live cache 2026-08-10, its notesData field alone was
# 112.8 MB of the 126.4 MB file, read by nobody.
#
# An ALLOWLIST by deliberate choice, not a notesData denylist -- a future fat
# field Chorus adds must not silently reintroduce the problem. Widening it is
# an Ask First decision (approved at spec review 2026-08-10 as these six), the
# same rule metadata_enrichment.CHORUS_FILLABLE_KEYS carries for its own list.
_CACHED_RESULT_FIELDS = ('name', 'artist', 'album', 'genre', 'year', 'charter')


def _cache_key(artist, title):
    return (library_common.normalize_lookup_value(artist)
            + '\x1f' + library_common.normalize_lookup_value(title))


def _trim(result):
    """Project a Chorus response down to the fields anything actually reads.

    Absent fields stay absent rather than being filled with None -- the cache
    shouldn't invent keys the API never sent, and every consumer reads through
    .get() anyway. `None` (a cached no-match) passes straight through.
    """
    if not isinstance(result, dict):
        return result
    return {k: result[k] for k in _CACHED_RESULT_FIELDS if k in result}


class CachedChorusClient:
    """Wraps chorus_client.search_by_artist_title() with an artist+title
    keyed cache. `None` results (a confirmed no-match) are cached too --
    repeating a lookup Chorus doesn't have shouldn't re-hit the network on
    every scan. Optional on-disk persistence (cache_path) survives across
    runs; without it, the cache is in-memory only for this instance's life.

    NOT a drop-in for chorus_client.search_by_artist_title(): this returns a
    projection over _CACHED_RESULT_FIELDS, not the raw Chorus response. The
    raw response's notesData field alone was 112.8 MB of the 126.4 MB live
    cache on 2026-08-10 and no consumer reads it; narrowing the contract was
    approved at spec review the same day (SPEC-chorus-cache-write-perf.md).
    Callers needing the full response should call chorus_client directly --
    metadata_enrichment.py and dedupe_report.py already do.

    Writes are batched, so a caller doing a run of lookups must flush() when
    the run ends.
    """

    def __init__(self, cache_path=None, ttl_days=DEFAULT_TTL_DAYS):
        self.cache_path = Path(cache_path) if cache_path else None
        self.ttl_seconds = ttl_days * _SECONDS_PER_DAY
        self._entries = {}
        # Counts unpersisted changes. MUST be initialized before _load(),
        # which sets it when it drops or rewrites what it read -- that mark is
        # how a shrunken cache reaches disk. Moving this below _load() would
        # silently discard it and the file would never actually shrink.
        self._dirty = 0
        self._load()
        self._last_flush = time.time()
        # Set after a failed write to pace the retry -- see _save().
        self._retry_not_before = 0.0

    def _load(self):
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            with open(self.cache_path, encoding='utf-8') as f:
                self._entries = json.load(f)
        except (OSError, ValueError) as e:
            log.warning('Could not read Chorus cache %s: %s', self.cache_path, e)
            self._entries = {}
            return
        if not isinstance(self._entries, dict):
            # Valid JSON of the wrong shape. Same outcome as unparseable --
            # start empty rather than let it fail later at the first lookup.
            log.warning('Chorus cache %s is not an object; ignoring', self.cache_path)
            self._entries = {}
            return
        self._compact()

    def _compact(self):
        """Drop entries the TTL already made unreachable, and trim legacy
        full-payload results down to _CACHED_RESULT_FIELDS.

        Pruning: search_by_artist_title() has always checked the TTL on read,
        so an expired entry can never be returned to a caller -- but nothing
        ever removed one, so it was re-serialized by every save forever. On
        the live cache on 2026-08-10 that was 5,379 of 7,743 entries (69%)
        riding along in every 126 MB rewrite.

        Trimming: entries written before the payload trim carry the whole
        Chorus response. This is their migration path -- the file shrinks in
        place. Rebuilding it instead would mean ~7,800 fresh lookups against
        an API already known to rate-limit (SPEC-chorus-reliability-fix.md).

        Either change marks the cache dirty, so the shrunken form actually
        reaches disk. Without that, a run with zero cache misses would compact
        in memory and leave the file exactly as big as it was.
        """
        now = time.time()
        kept = {}
        changed = False
        for key, entry in self._entries.items():
            try:
                age = now - entry['cached_at']
                result = entry['result']
            except (KeyError, TypeError):
                # Malformed entry: unreadable is indistinguishable from
                # expired, and the read path would have raised on it.
                changed = True
                continue
            if age >= self.ttl_seconds:
                changed = True
                continue
            compacted = {'result': _trim(result), 'cached_at': entry['cached_at']}
            if compacted != entry:
                changed = True
            kept[key] = compacted
        if changed:
            self._entries = kept
            self._dirty += 1

    def _save(self):
        """Atomic write (temp file + os.replace), returning True if the cache
        reached disk.

        Batching changed WHEN we write, never HOW. An enrichment pass can span
        days, so a crash or forced-close mid-write must leave the previous
        valid cache intact, never a half-written one the next run would parse
        and trust -- the same reasoning gui.py's _save_background_state spells
        out for background_state.json.
        """
        if not self.cache_path:
            return False
        tmp_path = self.cache_path.with_name(self.cache_path.name + '.tmp')
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self._entries, f)
            os.replace(tmp_path, self.cache_path)
        except OSError as e:
            log.warning('Could not write Chorus cache %s: %s', self.cache_path, e)
            # _dirty deliberately survives: the pending window is retried, not
            # discarded. But that leaves _dirty over the threshold, so without
            # pacing every later lookup would re-attempt a failing write --
            # and the known real failure here (WinError 32, see
            # SPEC-chorus-reliability-fix.md) is transient but repeated.
            self._retry_not_before = time.time() + FLUSH_EVERY_SECONDS
            return False
        self._dirty = 0
        self._last_flush = time.time()
        return True

    def _maybe_flush(self):
        """Persist only once the pending batch is big enough or old enough."""
        if not self.cache_path or not self._dirty:
            return
        if time.time() < self._retry_not_before:
            return
        if (self._dirty >= FLUSH_EVERY_N_INSERTS
                or (time.time() - self._last_flush) >= FLUSH_EVERY_SECONDS):
            self._save()

    def flush(self):
        """Persist pending entries now. A caller doing a run of lookups must
        call this when the run ends -- the batch thresholds alone would leave
        the run's final partial batch unwritten.

        Cheap and safe to call repeatedly; a no-op when nothing is pending.
        Unlike _maybe_flush() this ignores the post-failure retry pacing: a
        paced retry must not cause a run's last chance to write to be skipped.
        """
        if not self.cache_path or not self._dirty:
            return
        self._save()

    def search_by_artist_title(self, artist, title, force=False):
        key = _cache_key(artist, title)
        if not force:
            entry = self._entries.get(key)
            if entry is not None and (time.time() - entry['cached_at']) < self.ttl_seconds:
                return entry['result']

        # Trimmed before storing AND returned trimmed, so a miss and a hit
        # hand back the same shape -- otherwise a lookup's result would depend
        # on cache state, which only shows up as a bug in production.
        result = _trim(chorus_client.search_by_artist_title(artist, title))
        self._entries[key] = {'result': result, 'cached_at': time.time()}
        self._dirty += 1
        self._maybe_flush()
        return result
