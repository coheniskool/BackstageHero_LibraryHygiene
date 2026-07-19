# Verifies Task 1's decision: the community resolver stays ON by default
# (user opt-in, see SPEC.md). These tests pin the exact outbound payload and
# confirm the "Share matches" toggle (resolver_client.set_sharing) gates
# report()/ping() without affecting resolve() lookups.

import json

import pytest

import resolver_client as rc


class _FakeResponse:
    def __init__(self, payload=b'{"status": "none"}'):
        self._payload = payload

    def read(self, n=-1):
        return self._payload

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _recording_urlopen(calls, payload=b'{"status": "none"}'):
    def _urlopen(req, timeout=None):
        body = json.loads(req.data.decode('utf-8')) if req.data else None
        calls.append({'url': req.full_url, 'method': req.get_method(), 'body': body})
        return _FakeResponse(payload)
    return _urlopen


@pytest.fixture(autouse=True)
def _stub_client_id(monkeypatch):
    # _client_id() would otherwise read/write a real file under the user's
    # LOCALAPPDATA on every test run -- keep tests hermetic.
    monkeypatch.setattr(rc, '_client_id', lambda: 'test-client-id-000')


@pytest.fixture(autouse=True)
def _restore_sharing():
    original = rc.sharing_enabled()
    yield
    rc.set_sharing(original)


def test_resolver_enabled_by_default():
    assert rc.enabled() is True
    assert rc.RESOLVER_BASE  # non-empty: points at the shared community pool


def test_sharing_on_by_default_in_this_environment():
    # Reflects _sharing's module-level default (BACKSTAGEHERO_NO_SHARE unset).
    assert rc.sharing_enabled() is True


def test_resolve_performs_a_lookup_regardless_of_sharing_toggle(monkeypatch):
    rc.set_sharing(False)
    calls = []
    monkeypatch.setattr(rc.urllib.request, 'urlopen', _recording_urlopen(calls))

    rc.resolve('ch1:deadbeef')

    assert len(calls) == 1
    assert calls[0]['method'] == 'GET'
    assert calls[0]['url'].startswith(rc.RESOLVER_BASE + '/resolve')


def test_resolve_parses_an_approved_hit(monkeypatch):
    payload = json.dumps({'status': 'approved', 'video_id': 'dQw4w9WgXcQ', 'start_ms': -3000}).encode()
    monkeypatch.setattr(rc.urllib.request, 'urlopen', lambda req, timeout=None: _FakeResponse(payload))

    hit = rc.resolve('ch1:deadbeef')

    assert hit == {'status': 'approved', 'video_id': 'dQw4w9WgXcQ', 'start_ms': -3000}


def test_resolve_rejects_malformed_video_id(monkeypatch):
    payload = json.dumps({'status': 'approved', 'video_id': '<script>'}).encode()
    monkeypatch.setattr(rc.urllib.request, 'urlopen', lambda req, timeout=None: _FakeResponse(payload))
    assert rc.resolve('ch1:deadbeef') is None


def test_report_sends_exactly_the_documented_payload(monkeypatch):
    rc.set_sharing(True)
    calls = []
    monkeypatch.setattr(rc.urllib.request, 'urlopen', _recording_urlopen(calls))

    rc.report('ch1:deadbeef', 'dQw4w9WgXcQ', -3000, 0.8,
              artist='Rick Astley', title='Never Gonna Give You Up')

    assert len(calls) == 1
    assert calls[0]['method'] == 'POST'
    body = calls[0]['body']
    assert set(body.keys()) == {'hash', 'video_id', 'start_ms', 'client_id', 'confidence', 'artist', 'title'}
    assert body['hash'] == 'ch1:deadbeef'
    assert body['video_id'] == 'dQw4w9WgXcQ'
    assert body['start_ms'] == -3000
    assert body['confidence'] == 0.8
    assert body['artist'] == 'Rick Astley'
    assert body['title'] == 'Never Gonna Give You Up'
    # opaque anonymous id, never a filesystem path or personal identifier
    assert isinstance(body['client_id'], str) and body['client_id']


def test_report_suppressed_when_sharing_is_off(monkeypatch):
    rc.set_sharing(False)
    calls = []
    monkeypatch.setattr(rc.urllib.request, 'urlopen', _recording_urlopen(calls))

    rc.report('ch1:deadbeef', 'dQw4w9WgXcQ', -3000, 0.8)

    assert calls == []


def test_ping_suppressed_when_sharing_is_off(monkeypatch):
    rc.set_sharing(False)
    calls = []
    monkeypatch.setattr(rc.urllib.request, 'urlopen', _recording_urlopen(calls))

    rc.ping(app_version='9.9.9')

    assert calls == []


def test_ping_sent_when_sharing_is_on(monkeypatch):
    rc.set_sharing(True)
    calls = []
    monkeypatch.setattr(rc.urllib.request, 'urlopen', _recording_urlopen(calls))

    rc.ping(app_version='9.9.9')

    assert len(calls) == 1
    assert calls[0]['url'] == rc.RESOLVER_BASE + '/ping'
    assert set(calls[0]['body'].keys()) == {'client_id', 'sharing', 'app_version'}
