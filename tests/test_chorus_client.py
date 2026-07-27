# Covers chorus_client.search_by_artist_title()'s retry/backoff behavior on
# 429/503/transient connection errors (SPEC-chorus-reliability-fix.md,
# Root Cause 1). A live run against a real ~7,600-song library showed the
# Chorus API rate-limiting almost every request with no recovery mid-run,
# because the old code made exactly one attempt per song with no backoff.
#
# tests/test_chorus_client_robustness.py already covers malformed-response
# shapes; this file is scoped purely to retry/backoff, using a queued-
# response fake (one item per requests.post() call) rather than a single
# fixed response.

import email.utils
import time

import pytest
import requests

import chorus_client as cc


class _FakeRaw:
    def __init__(self, payload):
        self._buf = payload

    def read(self, amt=None, decode_content=True):
        buf, self._buf = self._buf, b''
        return buf


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        body = payload if payload is not None else {'data': [{'name': 'ok'}]}
        if not isinstance(body, (bytes, bytearray)):
            import json
            body = json.dumps(body).encode('utf-8')
        self.raw = _FakeRaw(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f'{self.status_code} error', response=self)


def _queue(monkeypatch, items):
    """items: a list of _FakeResponse or Exception instances, consumed one
    per requests.post() call. Returns the list of kwargs each call received."""
    calls = []
    remaining = list(items)

    def fake_post(*args, **kwargs):
        calls.append(kwargs)
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(cc.requests, 'post', fake_post)
    return calls


def _record_sleeps(monkeypatch):
    sleeps = []
    monkeypatch.setattr(cc.time, 'sleep', lambda s: sleeps.append(s))
    return sleeps


def test_429_with_retry_after_header_then_success(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    _queue(monkeypatch, [
        _FakeResponse(status_code=429, headers={'Retry-After': '2'}),
        _FakeResponse(status_code=200, payload={'data': [{'name': 'Kryptonite'}]}),
    ])
    result = cc.search_by_artist_title('3 Doors Down', 'Kryptonite')
    assert result == {'name': 'Kryptonite'}
    assert sleeps == [2.0]


def test_503_with_no_retry_after_uses_exponential_backoff(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    _queue(monkeypatch, [
        _FakeResponse(status_code=503),
        _FakeResponse(status_code=503),
        _FakeResponse(status_code=200, payload={'data': [{'name': 'ok'}]}),
    ])
    result = cc.search_by_artist_title('A', 'B')
    assert result == {'name': 'ok'}
    assert sleeps == [1.0, 2.0]


def test_connection_error_is_retried_then_succeeds(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    _queue(monkeypatch, [
        requests.exceptions.ConnectionError('refused'),
        _FakeResponse(status_code=200, payload={'data': [{'name': 'ok'}]}),
    ])
    result = cc.search_by_artist_title('A', 'B')
    assert result == {'name': 'ok'}
    assert sleeps == [1.0]


def test_timeout_is_retried_then_succeeds(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    _queue(monkeypatch, [
        requests.exceptions.Timeout('timed out'),
        _FakeResponse(status_code=200, payload={'data': [{'name': 'ok'}]}),
    ])
    result = cc.search_by_artist_title('A', 'B')
    assert result == {'name': 'ok'}
    assert sleeps == [1.0]


def test_retries_exhausted_returns_none_without_raising(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    _queue(monkeypatch, [
        _FakeResponse(status_code=429),
        _FakeResponse(status_code=503),
        _FakeResponse(status_code=429),
    ])
    result = cc.search_by_artist_title('A', 'B')
    assert result is None
    assert sleeps == [1.0, 2.0]


def test_non_retryable_http_status_returns_none_with_no_retry(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    calls = _queue(monkeypatch, [_FakeResponse(status_code=404)])
    result = cc.search_by_artist_title('A', 'B')
    assert result is None
    assert sleeps == []
    assert len(calls) == 1


def test_retry_after_as_http_date_is_parsed(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    monkeypatch.setattr(cc.time, 'time', lambda: 1_000_000.0)
    future = email.utils.formatdate(1_000_002.0, usegmt=True)
    _queue(monkeypatch, [
        _FakeResponse(status_code=429, headers={'Retry-After': future}),
        _FakeResponse(status_code=200, payload={'data': [{'name': 'ok'}]}),
    ])
    result = cc.search_by_artist_title('A', 'B')
    assert result == {'name': 'ok'}
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(2.0, abs=1.0)


def test_timeout_kwarg_is_identical_on_every_attempt(monkeypatch):
    _record_sleeps(monkeypatch)
    calls = _queue(monkeypatch, [
        _FakeResponse(status_code=429),
        _FakeResponse(status_code=503),
        _FakeResponse(status_code=200, payload={'data': [{'name': 'ok'}]}),
    ])
    cc.search_by_artist_title('A', 'B')
    assert len(calls) == 3
    assert all(k['timeout'] == cc.CHORUS_REQUEST_TIMEOUT_SECONDS for k in calls)
