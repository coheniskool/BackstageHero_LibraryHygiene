# Verifies Task 1's decision: the community resolver stays ON by default
# (user opt-in, see SPEC.md). These tests pin the exact outbound payload and
# confirm the "Share matches" toggle (resolver_client.set_sharing) gates
# report()/ping() without affecting resolve() lookups.

import json

import pytest

import resolver_client as rc

# Captured before the autouse _stub_client_id fixture below ever runs, so the
# _client_id caching tests can restore the real implementation for
# themselves instead of exercising the test-wide stub.
_REAL_CLIENT_ID = rc._client_id


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


# --- _client_id caching (perf-simplification) -------------------------------
#
# The autouse _stub_client_id fixture above replaces _client_id() entirely
# for every other test in this file, so these exercise the REAL
# implementation directly instead.

def test_client_id_is_cached_after_first_read(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, '_client_id', _REAL_CLIENT_ID)  # bypass the autouse stub
    monkeypatch.setattr(rc, '_cached_client_id', None)  # isolate from other tests/order
    monkeypatch.setattr(rc.updater, 'data_dir', lambda: str(tmp_path))

    first = rc._client_id()
    # the file changing must not affect a second call -- if the cache were
    # not working, this would prove it by returning the new value instead
    (tmp_path / 'client_id').write_text('a-different-value-entirely')
    second = rc._client_id()

    assert first == second
    assert second != 'a-different-value-entirely'


def test_client_id_reads_the_file_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, '_client_id', _REAL_CLIENT_ID)
    monkeypatch.setattr(rc, '_cached_client_id', None)
    monkeypatch.setattr(rc.updater, 'data_dir', lambda: str(tmp_path))
    (tmp_path / 'client_id').write_text('preexisting-id')

    real_open = open
    calls = []

    def _counting_open(path, *a, **k):
        if str(path) == str(tmp_path / 'client_id'):
            calls.append(path)
        return real_open(path, *a, **k)

    monkeypatch.setattr(rc, 'open', _counting_open, raising=False)

    rc._client_id()
    rc._client_id()
    rc._client_id()

    assert len(calls) == 1


def test_client_id_failure_is_not_cached_and_retries(tmp_path, monkeypatch):
    """A transient read failure must not permanently stick the process with
    'anon' -- the next call should retry the real file, not stay poisoned."""
    monkeypatch.setattr(rc, '_client_id', _REAL_CLIENT_ID)
    monkeypatch.setattr(rc, '_cached_client_id', None)

    calls = {'n': 0}

    def _flaky_data_dir():
        calls['n'] += 1
        if calls['n'] == 1:
            raise OSError('transient failure')
        return str(tmp_path)

    monkeypatch.setattr(rc.updater, 'data_dir', _flaky_data_dir)

    first = rc._client_id()
    assert first == 'anon'

    second = rc._client_id()
    assert second != 'anon'   # retried, and this time it succeeded
