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
    enrichment -- matches _export_library_csv's own philosophy."""
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    cache_path = tmp_path / 'chorus_cache.json'

    def _denied(*a, **k):
        raise OSError(13, 'Permission denied')
    monkeypatch.setattr('builtins.open', _denied)

    client = cc.CachedChorusClient(cache_path=cache_path)
    result = client.search_by_artist_title('3 Doors Down', 'Kryptonite')  # must not raise
    assert result == _RESULT_A


def test_disk_cache_written_as_valid_json(tmp_path, monkeypatch):
    calls = []
    _stub(monkeypatch, calls, result=_RESULT_A)
    cache_path = tmp_path / 'chorus_cache.json'
    client = cc.CachedChorusClient(cache_path=cache_path)
    client.search_by_artist_title('3 Doors Down', 'Kryptonite')
    with open(cache_path, encoding='utf-8') as f:
        data = json.load(f)
    assert isinstance(data, dict)
