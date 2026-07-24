# resolver/app.py's async Cloudflare cache purge (finding E24 of the
# perf-simplification pass). _purge_resolve() used to block admin_set/
# admin_offset's HTTP response on a Cloudflare API round-trip (up to 5s);
# it now fires the actual call on a background thread so a curator action
# returns immediately, with a purge failure still logged rather than
# silently discarded (fire-and-forget means the caller has no other way to
# notice one).
#
# resolver/ is a separately-deployed FastAPI service with its own
# requirements.txt (fastapi/pydantic/uvicorn) that isn't part of this repo's
# own requirements.txt -- these tests are skipped, not failed, wherever that
# service's dependencies aren't installed (matches this suite's existing
# pytest.importorskip convention for other optional deps, e.g.
# customtkinter in tests/test_offset_range_and_csv.py).

import os
import sys
import threading
import time

import pytest

pytest.importorskip('fastapi')

_RESOLVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'resolver')


@pytest.fixture(scope='module')
def resolver_app(tmp_path_factory):
    """Import resolver/app.py once, pointed at a throwaway SQLite DB -- the
    module runs db.init_db(DB_PATH) as an import-time side effect."""
    db_path = str(tmp_path_factory.mktemp('resolver_app_db') / 'test.sqlite3')
    os.environ['BACKSTAGEHERO_DB'] = db_path
    if _RESOLVER_DIR not in sys.path:
        sys.path.insert(0, _RESOLVER_DIR)
    import app as _resolver_app
    return _resolver_app


@pytest.fixture(autouse=True)
def _cf_env(resolver_app, monkeypatch):
    """Every test that wants the purge to actually attempt a call sets these;
    default them to configured-but-fake so accidental real network calls
    can't happen (urlopen is monkeypatched per-test anyway)."""
    monkeypatch.setattr(resolver_app, 'CF_API_TOKEN', 'test-token')
    monkeypatch.setattr(resolver_app, 'CF_ZONE_ID', 'test-zone')
    monkeypatch.setattr(resolver_app, 'PUBLIC_URL', 'https://example.invalid')


def test_purge_resolve_skips_entirely_when_env_vars_unset(resolver_app, monkeypatch):
    monkeypatch.setattr(resolver_app, 'CF_API_TOKEN', '')
    calls = []
    monkeypatch.setattr(resolver_app, '_purge_resolve_now', lambda h: calls.append(h))

    resolver_app._purge_resolve('ch1:deadbeef')

    assert calls == []


def test_purge_resolve_does_not_block_the_caller(resolver_app, monkeypatch):
    """The whole point of the change: a curator action must not wait out a
    slow Cloudflare round-trip before its HTTP response returns."""
    release = threading.Event()

    class _FakeResponse:
        def close(self):
            pass

    def _slow_urlopen(req, timeout=None):
        release.wait(timeout=2)   # simulates a slow/hanging Cloudflare call
        return _FakeResponse()

    monkeypatch.setattr(resolver_app.urllib.request, 'urlopen', _slow_urlopen)

    started = time.monotonic()
    resolver_app._purge_resolve('ch1:deadbeef')
    elapsed = time.monotonic() - started

    assert elapsed < 1.0   # returned immediately -- did not wait for the slow call
    release.set()          # let the background thread finish so it doesn't leak past the test
    time.sleep(0.05)


def test_purge_resolve_now_sends_the_expected_purge_request(resolver_app, monkeypatch):
    calls = []

    class _FakeResponse:
        def close(self):
            pass

    def _fake_urlopen(req, timeout=None):
        calls.append({'url': req.full_url, 'method': req.get_method(),
                      'body': req.data, 'timeout': timeout})
        return _FakeResponse()

    monkeypatch.setattr(resolver_app.urllib.request, 'urlopen', _fake_urlopen)

    resolver_app._purge_resolve_now('ch1:deadbeef')

    assert len(calls) == 1
    assert calls[0]['method'] == 'POST'
    assert 'test-zone' in calls[0]['url']
    assert calls[0]['timeout'] == 5
    assert b'ch1' in calls[0]['body']


def test_purge_resolve_now_logs_a_failure_instead_of_swallowing_it(resolver_app, monkeypatch, caplog):
    def _raise(req, timeout=None):
        raise OSError('cloudflare unreachable')

    monkeypatch.setattr(resolver_app.urllib.request, 'urlopen', _raise)

    with caplog.at_level('ERROR', logger=resolver_app.log.name):
        resolver_app._purge_resolve_now('ch1:deadbeef')

    assert 'ch1:deadbeef' in caplog.text
    assert 'unreachable' in caplog.text
