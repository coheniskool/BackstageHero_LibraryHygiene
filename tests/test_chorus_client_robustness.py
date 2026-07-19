# chorus_client returns a dict or None, and every caller relies on that by
# calling .get() on the result. These pin that promise against responses the
# server has no obligation to keep sending in the shape we expect.
#
# The escape route that mattered: the function body is wrapped in a broad
# `except Exception`, so a malformed response mostly degrades to None on its
# own. But a 'data' value that is a truthy STRING passed the old check, and
# results[0] handed back a single CHARACTER. The AttributeError then fired in
# the caller's per-song loop -- outside this try -- taking the whole
# library scan down with it.

import io
import json

import chorus_client as cc


class _FakeRaw:
    def __init__(self, payload):
        self._buf = io.BytesIO(payload)

    def read(self, amt=None, decode_content=True):
        return self._buf.read(amt)


class _FakeResponse:
    def __init__(self, payload):
        self.raw = _FakeRaw(payload)

    def raise_for_status(self):
        pass


def _respond(monkeypatch, payload):
    if not isinstance(payload, (bytes, bytearray)):
        payload = json.dumps(payload).encode('utf-8')
    monkeypatch.setattr(cc.requests, 'post',
                        lambda *a, **k: _FakeResponse(payload))


def test_a_well_formed_response_returns_the_first_result(monkeypatch):
    _respond(monkeypatch, {'data': [{'name': 'Kryptonite', 'artist': '3 Doors Down'}]})
    assert cc.search_by_artist_title('3 Doors Down', 'Kryptonite') == {
        'name': 'Kryptonite', 'artist': '3 Doors Down'}


def test_a_string_where_a_result_list_belongs_returns_none(monkeypatch):
    """The whole finding: 'evilstring' is truthy AND indexable, so the old
    check returned 'e' -- and the caller's .get() on it raised outside the
    try, aborting the library scan rather than skipping one song."""
    _respond(monkeypatch, {'data': 'evilstring'})
    assert cc.search_by_artist_title('A', 'B') is None


def test_a_list_of_non_dicts_returns_none(monkeypatch):
    _respond(monkeypatch, {'data': ['not', 'dicts']})
    assert cc.search_by_artist_title('A', 'B') is None


def test_a_top_level_list_returns_none(monkeypatch):
    _respond(monkeypatch, [{'name': 'x'}])
    assert cc.search_by_artist_title('A', 'B') is None


def test_empty_and_missing_data_return_none(monkeypatch):
    _respond(monkeypatch, {'data': []})
    assert cc.search_by_artist_title('A', 'B') is None
    _respond(monkeypatch, {})
    assert cc.search_by_artist_title('A', 'B') is None


def test_invalid_json_returns_none(monkeypatch):
    _respond(monkeypatch, b'<html>gateway error</html>')
    assert cc.search_by_artist_title('A', 'B') is None


def test_an_oversized_response_is_refused_before_parsing(monkeypatch):
    """timeout= bounds socket silence, not total bytes. Without a cap, an
    endless response is read straight into memory."""
    payload = b'{"data": [' + b'{"name": "x"},' * 200_000 + b'{"name": "y"}]}'
    assert len(payload) > cc.MAX_RESPONSE_BYTES
    _respond(monkeypatch, payload)
    assert cc.search_by_artist_title('A', 'B') is None


def test_a_response_just_under_the_cap_is_still_parsed(monkeypatch):
    """Guard against the cap being so eager it rejects legitimate traffic."""
    filler = 'x' * 1000
    _respond(monkeypatch, {'data': [{'name': 'Kryptonite', 'padding': filler}]})
    result = cc.search_by_artist_title('A', 'B')
    assert result is not None and result['name'] == 'Kryptonite'
