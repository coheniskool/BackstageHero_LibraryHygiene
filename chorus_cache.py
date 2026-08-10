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
import math
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

    ttl_days controls what THIS instance will serve, and nothing more. Because
    _compact() prunes at load and the shrunken form is written back, a short
    ttl_days used to permanently delete entries a default-TTL caller would
    still have been served -- a data-loss-shaped consequence of prune-on-load
    that SPEC-chorus-cache-write-perf.md did not anticipate. Deletion is now
    floored at DEFAULT_TTL_DAYS (see _prune_seconds), so a shorter ttl_days
    narrows what you get served without narrowing what anyone else keeps. A
    longer one prunes at its own horizon, which takes nothing from anybody.
    """

    def __init__(self, cache_path=None, ttl_days=DEFAULT_TTL_DAYS):
        self.cache_path = Path(cache_path) if cache_path else None
        self.ttl_seconds = ttl_days * _SECONDS_PER_DAY
        # Reading and pruning are different questions, so they get different
        # horizons. ttl_seconds answers "will I serve this entry?", which is
        # rightly per-instance. This answers "may I DELETE this entry, for
        # everyone?" -- floored at the default so no instance can drop an
        # entry a default-TTL caller would still have been served. Not
        # redundant when ttl_days is the default; it is what keeps a shorter
        # one from being destructive.
        self._prune_seconds = max(self.ttl_seconds, DEFAULT_TTL_DAYS * _SECONDS_PER_DAY)
        self._entries = {}
        # All of these MUST be initialized before _load(). It READS
        # _prune_seconds to decide what to drop, and it SETS _dirty when it
        # drops or rewrites what it read (or resets an unusable file), and
        # that mark is how a shrunken or healed cache reaches disk.
        # Initializing any of them after _load() would silently discard that
        # -- and if _load() ever grows a flush of its own, _maybe_flush()
        # would hit an AttributeError on the other two.
        #
        # Flush cadence runs on the MONOTONIC clock, not time.time(). These
        # measure elapsed time, and a backward wall-clock step (NTP
        # correction, resume from sleep, a manual clock fix) would otherwise
        # park _retry_not_before in the future and stall persistence for the
        # size of the step -- on a multi-day unattended run, exactly when the
        # bounded-loss promise matters. cached_at/ttl_seconds stay on
        # time.time(): those are persisted and compared across runs.
        self._dirty = 0
        self._last_flush = time.monotonic()
        # Set after a failed write to pace the retry -- see _save().
        self._retry_not_before = 0.0
        self._load()

    def _reset(self):
        """Empty the cache AND mark it for replacement on disk.

        The dirty mark is the self-heal. Without it, a file we couldn't make
        sense of survives every run that happens to have zero cache misses,
        and we pay the same failed parse forever. Deliberately NOT used for a
        read failure -- see _load()'s OSError branch.
        """
        self._entries = {}
        self._dirty += 1

    def _load(self):
        if not self.cache_path or not self.cache_path.exists():
            return
        # Handler ORDER is load-bearing: OSError must precede the catch-all,
        # or the one branch that deliberately does NOT overwrite the file
        # becomes unreachable.
        try:
            # Parsed into a local and assigned only once the shape check
            # passes, so no failure path can leave the instance holding a
            # half-processed structure.
            with open(self.cache_path, encoding='utf-8') as f:
                entries = json.load(f)
            if not isinstance(entries, dict):
                # Valid JSON of the wrong shape. Unusable, not unreadable --
                # reset AND replace, same as unparseable.
                log.warning('Chorus cache %s is not an object; starting empty',
                            self.cache_path)
                self._reset()
                return
            self._entries = entries
            self._compact()
        except OSError as e:
            # UNREADABLE, which is not evidence the file is BAD. WinError 32
            # is transient-but-repeated here (SPEC-chorus-reliability-fix.md),
            # and a concurrent process caught mid-os.replace looks identical.
            # Overwriting a file we merely couldn't read THIS ONCE would
            # replace a good cache with {} -- worse than the failure being
            # handled. So: empty in memory, untouched on disk.
            log.warning('Could not read Chorus cache %s: %s', self.cache_path, e)
            self._entries = {}
        except Exception as e:
            # The backstop, and the point is to stop enumerating. Reaching
            # here means the file could not be turned into a usable cache by
            # any path we know. The verified case is RecursionError from
            # deeply nested JSON (2026-08-10, Python 3.14.4: json.load raises
            # it, and it is NOT a ValueError) -- unreachable from the API,
            # since chorus_client.py caps responses at 1 MiB and catches bare
            # Exception, so only a hand-crafted or corrupted disk file gets
            # here.
            #
            # Before this, such a failure escaped __init__ before anything had
            # been written, so the bad file was never pruned or replaced:
            # enrichment died the same way every run until the user deleted
            # the cache by hand. Under the GUI that happens on the daemon
            # enrichment thread -- the app survives, the feature doesn't.
            #
            # Exception, NEVER BaseException: a Ctrl-C during the 0.80s
            # json.load of a 126 MB file has to abort the run, not be
            # swallowed at exactly the moment the user is trying to stop.
            log.warning('Chorus cache %s is unusable (%s: %s); starting empty',
                        self.cache_path, type(e).__name__, e)
            self._reset()

    def _compact(self):
        """Drop entries the TTL already made unreachable, and trim legacy
        full-payload results down to _CACHED_RESULT_FIELDS.

        Pruning: search_by_artist_title() has always checked the TTL on read,
        so an expired entry can never be returned to a caller -- but nothing
        ever removed one, so it was re-serialized by every save forever. On
        the live cache on 2026-08-10 that was 5,379 of 7,743 entries (69%)
        riding along in every 126 MB rewrite.

        Pruning uses _prune_seconds, NOT ttl_seconds, and the difference is
        the point: refusing to serve an entry costs one re-lookup and affects
        only this instance, while deleting it is permanent and affects every
        caller of this file. Only the second one needs a floor.

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
            except (KeyError, TypeError, OverflowError):
                # Malformed entry: unreadable is indistinguishable from
                # expired, and the read path would have raised on it.
                # OverflowError is a JSON integer too large for a float --
                # without it this loop would raise inside __init__ and, since
                # nothing has been written yet, the bad file would never get
                # pruned. Enrichment would stay dead until the user deleted
                # the cache by hand.
                changed = True
                continue
            if not math.isfinite(age) or age >= self._prune_seconds:
                # json accepts the non-standard Infinity/NaN literals, and an
                # infinite cached_at would otherwise read as never-expiring --
                # served forever, refreshed never.
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
        """Atomic write (temp file + os.replace).

        Batching changed WHEN we write, never HOW. An enrichment pass can span
        days, so a crash or forced-close mid-write must leave the previous
        valid cache intact, never a half-written one the next run would parse
        and trust -- the same reasoning gui.py's _save_background_state spells
        out for background_state.json.

        Deliberately returns nothing: a write failure is already logged, and
        no caller has anything useful to do about it -- this is a convenience
        cache, and a run must never fail because it couldn't persist one.
        """
        if not self.cache_path:
            return
        try:
            # Inside the try: with_name() raises ValueError on a degenerate
            # path (a bare drive root, '.'), which --chorus-cache lets a user
            # supply. Outside, that escaped search_by_artist_title() and broke
            # its never-raises contract over a typo.
            tmp_path = self.cache_path.with_name(self.cache_path.name + '.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self._entries, f)
            os.replace(tmp_path, self.cache_path)
        except Exception as e:
            # OSError is the expected case. TypeError/ValueError cover
            # json.dump choking on a value the API sent that isn't
            # JSON-native: flush() is called from a `finally` in
            # enrich_library(), where any raise would replace the run's real
            # exception with this one. A convenience cache must not be able to
            # rewrite what killed a run.
            #
            # Caught as Exception rather than that tuple because the tuple was
            # exactly as complete as _load()'s used to be: json.dump raises
            # RecursionError on deeply nested input too (verified 2026-08-10,
            # Python 3.14.4), and it is neither a TypeError nor a ValueError.
            # Accepted cost, stated rather than discovered later: a catch-all
            # can mask a bug a future refactor introduces as a logged warning
            # instead of a red test. That is the price of the never-raise
            # contract this docstring promises, and the same price already
            # being paid for OSError -- type(e).__name__ in the log line is
            # what keeps an unanticipated cause identifiable in log.txt.
            log.warning('Could not write Chorus cache %s (%s): %s',
                        self.cache_path, type(e).__name__, e)
            # _dirty deliberately survives: the pending window is retried, not
            # discarded. But that leaves _dirty over the threshold, so without
            # pacing every later lookup would re-attempt a failing write --
            # and the known real failure here (WinError 32, see
            # SPEC-chorus-reliability-fix.md) is transient but repeated.
            self._retry_not_before = time.monotonic() + FLUSH_EVERY_SECONDS
            return
        self._dirty = 0
        self._last_flush = time.monotonic()
        # Recovered: drop the pacing gate rather than stay locked out for the
        # rest of a window that a working write has already made moot.
        self._retry_not_before = 0.0

    def _maybe_flush(self):
        """Persist only once the pending batch is big enough or old enough."""
        if not self.cache_path or not self._dirty:
            return
        if time.monotonic() < self._retry_not_before:
            return
        if (self._dirty >= FLUSH_EVERY_N_INSERTS
                or (time.monotonic() - self._last_flush) >= FLUSH_EVERY_SECONDS):
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
