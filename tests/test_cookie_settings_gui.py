# GUI coverage for the "Use browser cookies" toggle + browser dropdown
# (SPEC-background-mode.md, Task 6). Mirrors tests/test_library_tools_dialog.py's
# convention: a real (non-mocked) ctk widget tree, module-skipped without a
# display rather than failing on a headless machine.
#
# gui.App() is itself the ctk.CTk() root (unlike LibraryToolsDialog, which is
# a Toplevel built against a separate shared root), so one App instance is
# built for the whole module and reused across tests -- building more than
# one real Tk root in a single process is exactly the flakiness
# test_library_tools_dialog.py's own `root` fixture docstring warns about.

import pytest

ctk = pytest.importorskip('customtkinter')
import gui


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    # gui.App() reads gui._SETTINGS_FILE (real per-machine settings.json) at
    # construction time via _load_settings() -- without isolating it here,
    # "fresh install" defaults below only hold on a machine that has never
    # toggled cookies on for real. monkeypatch is function-scoped and can't
    # be used by a module-scoped fixture, hence pytest.MonkeyPatch() directly.
    mp = pytest.MonkeyPatch()
    fake_settings = tmp_path_factory.mktemp('cookie_settings_gui') / 'settings.json'
    mp.setattr(gui, '_SETTINGS_FILE', str(fake_settings))
    try:
        a = gui.App()
    except Exception as exc:                      # genuinely no display / no Tk
        mp.undo()
        pytest.skip(f'Tk unavailable: {exc}')
    a.withdraw()
    yield a
    try:
        a.destroy()
    except Exception:
        pass
    mp.undo()


def test_cookie_toggle_and_browser_dropdown_exist_with_defaults(app):
    assert hasattr(app, '_cookies_var')
    assert hasattr(app, '_cookie_browser_var')
    # Off by default, matching a fresh install's settings.json (no
    # use_browser_cookies/cookie_browser keys yet).
    assert app._cookies_var.get() is False
    assert app._cookie_browser_var.get() == 'chrome'


def test_toggling_cookies_on_persists_via_persist_setting(app, monkeypatch):
    saved = []
    monkeypatch.setattr(gui, '_save_settings', lambda data: saved.append(dict(data)))
    try:
        app._cookies_var.set(True)
        app._on_cookies_toggle()
        assert app._settings['use_browser_cookies'] is True
        assert saved and saved[-1]['use_browser_cookies'] is True
    finally:
        app._cookies_var.set(False)
        app._on_cookies_toggle()


def test_changing_browser_persists_via_persist_setting(app, monkeypatch):
    saved = []
    monkeypatch.setattr(gui, '_save_settings', lambda data: saved.append(dict(data)))
    try:
        app._cookie_browser_var.set('firefox')
        app._on_cookie_browser_change()
        assert app._settings['cookie_browser'] == 'firefox'
        assert saved and saved[-1]['cookie_browser'] == 'firefox'
    finally:
        app._cookie_browser_var.set('chrome')
        app._on_cookie_browser_change()


def test_toggle_and_browser_change_both_push_into_videodownload(app, monkeypatch):
    """A change must take effect on the next download without a restart --
    that's VideoDownload.configure_cookies() being re-called, not just the
    settings file being rewritten."""
    calls = []
    monkeypatch.setattr(gui, 'configure_cookies',
                        lambda use, browser: calls.append((use, browser)))
    monkeypatch.setattr(gui, '_save_settings', lambda data: None)
    try:
        app._cookies_var.set(True)
        app._cookie_browser_var.set('edge')
        app._on_cookies_toggle()
        assert calls[-1] == (True, 'edge')

        app._cookie_browser_var.set('firefox')
        app._on_cookie_browser_change()
        assert calls[-1] == (True, 'firefox')
    finally:
        app._cookies_var.set(False)
        app._cookie_browser_var.set('chrome')
        app._on_cookies_toggle()


def test_falling_back_never_writes_cookie_browser_to_settings(app, monkeypatch):
    """SPEC-cookie-chain-and-pacing.md, Never list: the chain is in-memory
    only. Falling through chrome -> firefox must not rewrite the user's
    dropdown choice, so the next launch tries their actual preference fresh
    in case Chrome or yt-dlp has been fixed since."""
    import VideoDownload as vd

    saved = []
    monkeypatch.setattr(gui, '_save_settings', lambda data: saved.append(dict(data)))

    dpapi = Exception('ERROR: Failed to decrypt with DPAPI. See issues/10927')

    class FakeYDL:
        _behaviors = [dpapi, 'ok']

        def __init__(self, opts):
            self.opts = opts
            self._behavior = FakeYDL._behaviors.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, *a, **kw):
            if isinstance(self._behavior, Exception):
                raise self._behavior
            return self._behavior

    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)
    monkeypatch.setattr(vd, '_COOKIES_BROKEN', False)
    vd._BROKEN_COOKIE_BROWSERS.clear()
    # Snapshot rather than asserting a literal: earlier tests in this module
    # share the module-scoped `app` and leave their own cookie_browser value
    # behind. What matters is that the fallback changes nothing, not what the
    # value happens to be.
    before = dict(app._settings)
    try:
        vd.configure_cookies(True, 'chrome')
        vd._run_ytdlp_with_cookie_fallback(
            vd._base_opts(), lambda ydl: ydl.extract_info())

        # The fallback happened...
        assert vd._BROKEN_COOKIE_BROWSERS == {'chrome'}
        # ...and nothing was persisted or mutated by it.
        assert saved == []
        assert app._settings == before
    finally:
        vd.configure_cookies(False, None)
        vd._COOKIES_BROKEN = False
        vd._BROKEN_COOKIE_BROWSERS.clear()
