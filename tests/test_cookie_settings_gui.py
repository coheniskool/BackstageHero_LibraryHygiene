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
