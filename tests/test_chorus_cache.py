# tests/test_chorus_cache.py
# Covers chorus_cache.CachedChorusClient -- Task 1.3 of the
# library-enrichment plan. See tasks/plan-library-enrichment.md.

import json

import chorus_cache as cc

_RESULT_A = {'name': 'Kryptonite', 'artist': '3 Doors Down', 'genre': 'Rock'}
_RESULT_B = {'name': 'Mr. Roboto', 'artist': 'Styx', 'genre': 'Rock'}


def _stub(monkeypatch, calls, result_by_call=None, result=None):
    """Records every (artist, title) call into `calls` and returns either a
    fixed `result` or, if result_by_call is given, one item per call in
    order -- lets a test assert the underlying chorus_client was (or
    wasn't) hit again on a cache hit/expiry/force case."""
    def fake_search(artist, title):
        calls.append((artist, title))
        if result_by_call is not None:
            return result_by_call[len(calls) - 1]
        return result
    monkeypatch.setattr(cc.chorus_client, 'search_by_artist_title', fake_search)


def test_cache_miss_calls_chorus_client(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    client = cc.CachedChorusClient()
    result = client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    assert result == _RESULT_A
    assert calls == [('3 Doors Down', 'Kryptonite')]


def test_cache_hit_does_not_call_chorus_client_again(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    client = cc.CachedChorusClient()
    client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    second = client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    assert second == _RESULT_A
    assert len(calls) == 1


def test_different_artist_title_is_a_separate_cache_entry(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result_by_call=[_RESULT_A, _RESULT_B])
    client = cc.CachedChorusClient()
    first = client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    second = client.search_by_artist_title('Styx', 'Mr. Roboto')
    assert first == _RESULT_A
    assert second == _RESULT_B
    assert len(calls) == 2


def test_cache_key_is_case_and_whitespace_insensitive(monkeypatch):
    """Matches library_common.normalize_lookup_value's fuzzy-key behavior --
    a re-request with different casing/spacing must still hit cache."""
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    client = cc.CachedChorusClient()
    client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    second = client.search_by_artist_title('  3 DOORS DOWN  ', 'kryptonite')
    assert second == _RESULT_A
    assert len(calls) == 1


def test_force_true_always_calls_chorus_client(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result_by_call=[_RESULT_A, _RESULT_B])
    client = cc.CachedChorusClient()
    client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    forced = client.search_by_artist_title('3 Doors Down', 'Kryptonite', force=True)
    assert forced == _RESULT_B
    assert len(calls) == 2


def test_entry_expires_after_ttl(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result_by_call=[_RESULT_A, _RESULT_B])
    fake_now = [1_000_000.0]
    monkeypatch.setattr(cc.time, 'time', lambda: fake_now[0])

    client = cc.CachedChorusClient(ttl_days=7)
    client.search_by_artist_title('3 Doors Down', 'Kryptonite')

    fake_now[0] += 6 * 86400  # still within TTL
    still_cached = client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    assert still_cached == _RESULT_A
    assert len(calls) == 1

    fake_now[0] += 2 * 86400  # now past the 7-day TTL (8 days total elapsed)
    refreshed = client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    assert refreshed == _RESULT_B
    assert len(calls) == 2


def test_none_result_is_cached_too(monkeypatch):
    """A confirmed 'no match' is itself worth caching -- repeating a lookup
    that Chorus doesn't have shouldn't re-hit the network every scan."""
    calls = []
    _stub(monkeypatch, calls, result=None)
    client = cc.CachedChorusClient()
    client.search_by_artist_title('Nobody', 'Nothing')
    client.search_by_artist_title('Nobody', 'Nothing')
    assert len(calls) == 1


def test_disk_cache_persists_across_instances(tmp_path, monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    cache_path = tmp_path / 'chorus_cache.json'

    first_client = cc.CachedChorusClient(cache_path=cache_path)
    first_client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    first_client.flush()  # writes are batched now -- see SPEC-chorus-cache-write-perf.md
    assert cache_path.exists()

    second_client = cc.CachedChorusClient(cache_path=cache_path)
    result = second_client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    assert result == _RESULT_A
    assert len(calls) == 1  # second instance reused the on-disk entry


def test_corrupt_disk_cache_is_ignored_not_raised(tmp_path, monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    cache_path = tmp_path / 'chorus_cache.json'
    cache_path.write_text('{not valid json', encoding='utf-8')

    client = cc.CachedChorusClient(cache_path=cache_path)
    result = client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    assert result == _RESULT_A
    assert len(calls) == 1


def test_disk_write_failure_does_not_raise(tmp_path, monkeypatch):
    """A convenience cache must never cost the user the ability to run
    enrichment -- matches _export_library_csv's own philosophy.

    The explicit flush() is load-bearing, not decoration: writes are batched
    now, so a single lookup writes nothing and this test would pass without
    ever entering the failing-open path -- green, and asserting nothing.
    """
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    cache_path = tmp_path / 'chorus_cache.json'

    attempts = []

    def _denied(*a, **k):
        attempts.append(1)
        raise OSError(13, 'Permission denied')
    monkeypatch.setattr('builtins.open', _denied)

    client = cc.CachedChorusClient(cache_path=cache_path)
    result = client.search_by_artist_title('3 Doors Down', 'Kryptonite')  # must not raise
    client.flush()  # must not raise either
    assert result == _RESULT_A
    assert attempts, 'the failing-open path was never exercised'


def test_disk_cache_written_as_valid_json(tmp_path, monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    cache_path = tmp_path / 'chorus_cache.json'
    client = cc.CachedChorusClient(cache_path=cache_path)
    client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    client.flush()  # writes are batched now -- see SPEC-chorus-cache-write-perf.md
    with open(cache_path, encoding='utf-8') as f:
        data = json.load(f)
    assert isinstance(data, dict)


# --- Batched-write behavior (SPEC-chorus-cache-write-perf.md) ----------------
# Before this, search_by_artist_title() called _save() after every miss, and
# _save() re-serializes the WHOLE dict: measured 2026-08-10 against the live
# cache, that was a 126.42 MB rewrite and 0.75s of json.dumps per song, over a
# ~7,800-song library. These tests pin the batching that replaced it.


def _count_saves(monkeypatch):
    """Record every completed atomic write by wrapping os.replace -- _save()'s
    last step. Counting the write itself rather than calls to _save() keeps
    these assertions about observable disk behavior, not internal structure."""
    saves = []
    real_replace = cc.os.replace

    def counting_replace(src, dst):
        saves.append(dst)
        return real_replace(src, dst)
    monkeypatch.setattr(cc.os, 'replace', counting_replace)
    return saves


def test_writes_are_batched_not_per_lookup(tmp_path, monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    monkeypatch.setattr(cc, 'FLUSH_EVERY_N_INSERTS', 100)
    cache_path = tmp_path / 'chorus_cache.json'

    client = cc.CachedChorusClient(cache_path=cache_path)
    saves = _count_saves(monkeypatch)
    for i in range(10):
        client.search_by_artist_title(f'Artist {i}', 'Title')

    assert saves == [], 'ten lookups under the batch threshold must not write'

    client.flush()
    assert len(saves) == 1
    with open(cache_path, encoding='utf-8') as f:
        assert len(json.load(f)) == 10  # one write, all ten entries


def test_flush_happens_after_n_inserts(tmp_path, monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    monkeypatch.setattr(cc, 'FLUSH_EVERY_N_INSERTS', 3)
    cache_path = tmp_path / 'chorus_cache.json'

    client = cc.CachedChorusClient(cache_path=cache_path)
    saves = _count_saves(monkeypatch)
    client.search_by_artist_title('Artist 1', 'Title')
    client.search_by_artist_title('Artist 2', 'Title')
    assert saves == []

    client.search_by_artist_title('Artist 3', 'Title')  # crosses the threshold
    assert len(saves) == 1


def test_flush_happens_after_time_threshold(tmp_path, monkeypatch):
    """A slow, rate-limited run trickles in one lookup at a time and would
    never reach the insert threshold -- the time bound is what caps how much
    such a run can lose to a kill."""
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    monkeypatch.setattr(cc, 'FLUSH_EVERY_N_INSERTS', 100)
    fake_now = [1_000_000.0]
    monkeypatch.setattr(cc.time, 'time', lambda: fake_now[0])
    cache_path = tmp_path / 'chorus_cache.json'

    client = cc.CachedChorusClient(cache_path=cache_path)
    saves = _count_saves(monkeypatch)
    client.search_by_artist_title('Artist 1', 'Title')
    assert saves == []

    fake_now[0] += cc.FLUSH_EVERY_SECONDS + 1
    client.search_by_artist_title('Artist 2', 'Title')
    assert len(saves) == 1


def test_flush_with_nothing_pending_is_a_noop(tmp_path, monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    cache_path = tmp_path / 'chorus_cache.json'

    client = cc.CachedChorusClient(cache_path=cache_path)
    saves = _count_saves(monkeypatch)
    client.flush()
    client.flush()

    assert saves == []
    assert not cache_path.exists()


def test_failed_save_keeps_dirty_window_for_retry(tmp_path, monkeypatch):
    """A failed write must not clear the pending window -- otherwise a
    transient error silently discards up to FLUSH_EVERY_N_INSERTS entries
    that were never written anywhere."""
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    monkeypatch.setattr(cc, 'FLUSH_EVERY_N_INSERTS', 100)
    cache_path = tmp_path / 'chorus_cache.json'

    client = cc.CachedChorusClient(cache_path=cache_path)
    client.search_by_artist_title('3 Doors Down', 'Kryptonite')

    real_open = open
    fail = [True]

    def flaky_open(*a, **k):
        if fail[0]:
            raise OSError(13, 'Permission denied')
        return real_open(*a, **k)
    monkeypatch.setattr('builtins.open', flaky_open)

    client.flush()  # fails, must not raise
    assert not cache_path.exists()

    fail[0] = False
    client.flush()  # the entry must still be pending, and land this time

    with open(cache_path, encoding='utf-8') as f:
        assert len(json.load(f)) == 1


def test_failed_save_does_not_retry_on_every_lookup(tmp_path, monkeypatch):
    """The known real write failure here is WinError 32 (see
    SPEC-chorus-reliability-fix.md) -- transient, but repeated. Keeping the
    dirty window means _dirty stays over the threshold, so without pacing
    every subsequent lookup would re-attempt a failing write."""
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    monkeypatch.setattr(cc, 'FLUSH_EVERY_N_INSERTS', 1)
    fake_now = [1_000_000.0]
    monkeypatch.setattr(cc.time, 'time', lambda: fake_now[0])
    cache_path = tmp_path / 'chorus_cache.json'

    attempts = []

    def _denied(*a, **k):
        attempts.append(1)
        raise OSError(13, 'Permission denied')

    client = cc.CachedChorusClient(cache_path=cache_path)
    monkeypatch.setattr('builtins.open', _denied)

    for i in range(5):
        client.search_by_artist_title(f'Artist {i}', 'Title')
    assert len(attempts) == 1, 'only the first failure should have been attempted'

    fake_now[0] += cc.FLUSH_EVERY_SECONDS + 1
    client.search_by_artist_title('Artist 9', 'Title')
    assert len(attempts) == 2, 'the retry should resume at the time threshold'


# --- TTL prune on load (SPEC-chorus-cache-write-perf.md) --------------------
# The TTL was only ever checked on READ, so an expired entry was unreachable
# but never removed -- it rode along in every save forever. Measured on the
# live cache 2026-08-10: 5,379 of 7,743 entries (69%) were already expired.


def _write_cache(cache_path, entries):
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(entries, f)


def test_expired_entries_pruned_on_load(tmp_path, monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    now = 1_000_000.0
    monkeypatch.setattr(cc.time, 'time', lambda: now)
    cache_path = tmp_path / 'chorus_cache.json'
    fresh_key = cc._cache_key('Styx', 'Mr. Roboto')
    stale_key = cc._cache_key('3 Doors Down', 'Kryptonite')
    _write_cache(cache_path, {
        fresh_key: {'result': _RESULT_B, 'cached_at': now - 86400},
        stale_key: {'result': _RESULT_A, 'cached_at': now - 30 * 86400},
    })

    client = cc.CachedChorusClient(cache_path=cache_path)
    client.flush()

    with open(cache_path, encoding='utf-8') as f:
        assert list(json.load(f)) == [fresh_key]


def test_prune_on_load_does_not_lose_fresh_entries(tmp_path, monkeypatch):
    """The guard against an over-eager prune: a within-TTL entry must still
    serve a lookup from cache rather than going back to the network."""
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    now = 1_000_000.0
    monkeypatch.setattr(cc.time, 'time', lambda: now)
    cache_path = tmp_path / 'chorus_cache.json'
    _write_cache(cache_path, {
        cc._cache_key('Styx', 'Mr. Roboto'): {'result': _RESULT_B, 'cached_at': now - 86400},
    })

    client = cc.CachedChorusClient(cache_path=cache_path)

    assert client.search_by_artist_title('Styx', 'Mr. Roboto') == _RESULT_B
    assert calls == []  # served from the surviving entry, no network


def test_malformed_cached_at_treated_as_expired(tmp_path, monkeypatch):
    """Before the prune, the read path would have raised on these. Unreadable
    is indistinguishable from expired, and a cache must never be the reason a
    run can't start."""
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    now = 1_000_000.0
    monkeypatch.setattr(cc.time, 'time', lambda: now)
    cache_path = tmp_path / 'chorus_cache.json'
    good_key = cc._cache_key('Styx', 'Mr. Roboto')
    _write_cache(cache_path, {
        'missing-timestamp': {'result': _RESULT_A},
        'non-numeric-timestamp': {'result': _RESULT_A, 'cached_at': 'yesterday'},
        good_key: {'result': _RESULT_B, 'cached_at': now - 86400},
    })

    client = cc.CachedChorusClient(cache_path=cache_path)  # must not raise
    client.flush()

    with open(cache_path, encoding='utf-8') as f:
        assert list(json.load(f)) == [good_key]


def test_prune_on_load_persists_at_next_flush(tmp_path, monkeypatch):
    """A run with zero cache misses must still shrink the file -- otherwise
    the expired entries are only dropped in memory and the 126 MB measured on
    2026-08-10 is never actually reclaimed on disk."""
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    now = 1_000_000.0
    monkeypatch.setattr(cc.time, 'time', lambda: now)
    cache_path = tmp_path / 'chorus_cache.json'
    _write_cache(cache_path, {
        'stale': {'result': _RESULT_A, 'cached_at': now - 30 * 86400},
    })

    client = cc.CachedChorusClient(cache_path=cache_path)
    saves = _count_saves(monkeypatch)
    client.flush()  # no lookups happened at all

    assert len(saves) == 1


def test_load_with_nothing_to_prune_does_not_mark_dirty(tmp_path, monkeypatch):
    """The other side of the eager-shrink rule: a cache that needed no
    pruning must not be rewritten for nothing on every run."""
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    now = 1_000_000.0
    monkeypatch.setattr(cc.time, 'time', lambda: now)
    cache_path = tmp_path / 'chorus_cache.json'
    _write_cache(cache_path, {
        cc._cache_key('Styx', 'Mr. Roboto'): {'result': _RESULT_B, 'cached_at': now - 86400},
    })

    client = cc.CachedChorusClient(cache_path=cache_path)
    saves = _count_saves(monkeypatch)
    client.flush()

    assert saves == []


# --- Payload trim (SPEC-chorus-cache-write-perf.md) -------------------------
# The cache stored the ENTIRE Chorus response. Measured 2026-08-10, its
# notesData field alone was 112.8 MB of the 126.4 MB file -- read by nobody.
# library_enrichment._enrich_one_song is the only consumer of a cached result
# and reads exactly _CACHED_RESULT_FIELDS.

_FAT_RESULT = {
    'name': 'Kryptonite', 'artist': '3 Doors Down', 'album': 'The Better Life',
    'genre': 'Rock', 'year': '2000', 'charter': 'Somebody',
    'notesData': {'noteCounts': list(range(50))},  # the 112.8 MB field
    'md5': 'abc123', 'chartHash': 'def456', 'loading_phrase': 'go',
}
_TRIMMED_FAT_RESULT = {
    'name': 'Kryptonite', 'artist': '3 Doors Down', 'album': 'The Better Life',
    'genre': 'Rock', 'year': '2000', 'charter': 'Somebody',
}


def test_only_consumed_fields_are_cached(tmp_path, monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result=_FAT_RESULT)
    cache_path = tmp_path / 'chorus_cache.json'

    client = cc.CachedChorusClient(cache_path=cache_path)
    client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    client.flush()

    with open(cache_path, encoding='utf-8') as f:
        entry = next(iter(json.load(f).values()))
    assert entry['result'] == _TRIMMED_FAT_RESULT
    assert 'notesData' not in entry['result']


def test_trimmed_result_is_returned_on_both_miss_and_hit(monkeypatch):
    """A miss must not hand back the raw response while a hit hands back the
    projection -- callers would see the same lookup change shape depending on
    cache state, which is the kind of bug that only shows up in production."""
    calls = []
    _stub(monkeypatch, calls, result=_FAT_RESULT)
    client = cc.CachedChorusClient()

    on_miss = client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    on_hit = client.search_by_artist_title('3 Doors Down', 'Kryptonite')

    assert len(calls) == 1
    assert on_miss == _TRIMMED_FAT_RESULT
    assert on_hit == _TRIMMED_FAT_RESULT


def test_absent_fields_stay_absent_rather_than_filled_with_none(monkeypatch):
    """The cache projects the response down; it doesn't invent keys the API
    never sent. Every consumer reads through .get() anyway."""
    calls = []
    _stub(monkeypatch, calls, result={'name': 'Kryptonite', 'artist': '3 Doors Down'})
    client = cc.CachedChorusClient()

    result = client.search_by_artist_title('3 Doors Down', 'Kryptonite')

    assert result == {'name': 'Kryptonite', 'artist': '3 Doors Down'}


def test_legacy_full_payload_entry_is_trimmed_on_load(tmp_path, monkeypatch):
    """The migration path for the live 126 MB cache: it must shrink in place,
    not be discarded -- rebuilding it means ~7,800 lookups against an API
    already known to rate-limit (SPEC-chorus-reliability-fix.md)."""
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    now = 1_000_000.0
    monkeypatch.setattr(cc.time, 'time', lambda: now)
    cache_path = tmp_path / 'chorus_cache.json'
    key = cc._cache_key('3 Doors Down', 'Kryptonite')
    _write_cache(cache_path, {key: {'result': _FAT_RESULT, 'cached_at': now - 86400}})

    client = cc.CachedChorusClient(cache_path=cache_path)
    client.flush()

    with open(cache_path, encoding='utf-8') as f:
        entry = json.load(f)[key]
    assert entry['result'] == _TRIMMED_FAT_RESULT
    assert entry['cached_at'] == now - 86400  # TTL position preserved

    # and the surviving entry still serves a lookup without going to network
    assert client.search_by_artist_title('3 Doors Down', 'Kryptonite') == _TRIMMED_FAT_RESULT
    assert calls == []
