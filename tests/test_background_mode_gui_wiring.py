# GUI wiring for background mode (Task 12 of SPEC-background-mode.md).
#
# Task 11 built the background-mode state machine (_launch_background /
# _dl_thread background_mode=True) but nothing called _launch_background yet,
# and its tool_dry_run snapshot was hardcoded to True for every tool. This
# module covers the two things Task 12 adds:
#   - the "Run in background" footer checkbox, read at Start-click time in
#     _start_download to choose _launch vs _launch_background;
#   - real per-tool dry-run capture (App._tool_dry_run_prefs /
#     _on_tool_dry_run_change), persisted to settings.json so
#     _launch_background can read the user's last choice even when the
#     Library Tools dialog was never opened.
#
# Follows tests/test_background_mode_controller.py's convention: a bare
# object.__new__(gui.App) with just the attributes each method under test
# touches, never a real Tk window, never a real background thread (Thread is
# monkeypatched to a no-op so _launch_background's synchronous
# background_state.json write can be inspected without a worker racing it).

import types

import pytest

ctk = pytest.importorskip('customtkinter')
import gui


# --- fakes -------------------------------------------------------------

class _FakeVar:
    """Stand-in for a tk.BooleanVar/StringVar -- just .get()/.set()."""

    def __init__(self, value):
        self._v = value

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class _FakeThread:
    """Stand-in for threading.Thread that never actually runs its target,
    so _launch_background's pre-thread-start work (validation, state save)
    can be inspected without a worker thread racing the test."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        pass


def _make_song(folder, checked=True, has_video=False):
    return gui.Song(filename=folder + '/song.ini', folder=folder, label=folder,
                     key=folder.lower(), has_video=has_video, res='-',
                     checked=checked)


def _bare_app_for_start_download(background):
    """App with just what _start_download touches, plus recording stand-ins
    for _launch/_launch_background so we can see which one fired."""
    app = object.__new__(gui.App)
    app._running = False
    app._songs = [_make_song('C:/Songs/A'), _make_song('C:/Songs/B')]
    app._background_var = _FakeVar(background)
    app._confirm_batch = lambda work, verb: True
    app._update_row = lambda s: None

    calls = {'launch': [], 'launch_background': []}
    app._launch = lambda targets, replace, resync: calls['launch'].append(
        (targets, replace, resync))
    app._launch_background = lambda targets, replace, resync: calls[
        'launch_background'].append((targets, replace, resync))
    return app, calls


# --- (a)/(b): the "Run in background" checkbox picks _launch vs _launch_background

def test_background_checkbox_off_calls_launch_not_launch_background():
    app, calls = _bare_app_for_start_download(background=False)

    app._start_download()

    assert len(calls['launch']) == 1
    assert calls['launch_background'] == []
    targets, replace, resync = calls['launch'][0]
    assert replace is False
    assert resync is False
    assert [s.folder for s in targets] == ['C:/Songs/A', 'C:/Songs/B']


def test_background_checkbox_on_calls_launch_background_with_right_args():
    app, calls = _bare_app_for_start_download(background=True)

    app._start_download()

    assert calls['launch'] == []
    assert len(calls['launch_background']) == 1
    targets, replace, resync = calls['launch_background'][0]
    assert replace is False
    assert resync is False
    assert [s.folder for s in targets] == ['C:/Songs/A', 'C:/Songs/B']


# --- (c): _tool_dry_run_prefs -------------------------------------------

def test_tool_dry_run_prefs_defaults_true_with_no_saved_settings():
    app = object.__new__(gui.App)
    app._settings = {}

    prefs = app._tool_dry_run_prefs()

    assert set(prefs) == {key for key, _, _ in gui._LIBRARY_TOOLS}
    assert all(v is True for v in prefs.values())


def test_tool_dry_run_prefs_honors_persisted_overrides():
    app = object.__new__(gui.App)
    app._settings = {'library_tool_dry_run': {'fix_chart_names': False}}

    prefs = app._tool_dry_run_prefs()

    assert prefs['fix_chart_names'] is False
    assert all(prefs[k] is True for k, _, _ in gui._LIBRARY_TOOLS
               if k != 'fix_chart_names')


# --- (d): _on_tool_dry_run_change ----------------------------------------

def test_on_tool_dry_run_change_updates_and_persists_settings(monkeypatch):
    app = object.__new__(gui.App)
    app._settings = {}
    saved = []
    monkeypatch.setattr(gui, '_save_settings', lambda data: saved.append(dict(data)))

    app._on_tool_dry_run_change('repair_videos', False)

    assert app._settings['library_tool_dry_run'] == {'repair_videos': False}
    assert saved and saved[-1]['library_tool_dry_run'] == {'repair_videos': False}

    app._on_tool_dry_run_change('enrich_metadata', False)

    assert app._settings['library_tool_dry_run'] == {
        'repair_videos': False, 'enrich_metadata': False}
    assert saved[-1]['library_tool_dry_run'] == {
        'repair_videos': False, 'enrich_metadata': False}


# --- (f): _launch_background persists the real per-tool prefs -----------

def test_launch_background_persists_tool_dry_run_prefs_not_hardcoded_true(
        monkeypatch, tmp_path):
    monkeypatch.setattr(gui, '_BACKGROUND_STATE_FILE',
                        str(tmp_path / 'background_state.json'))
    monkeypatch.setattr(gui.threading, 'Thread', _FakeThread)

    app = object.__new__(gui.App)
    app._running = False
    app._quality_var = _FakeVar('720p')
    app._stop_evt = types.SimpleNamespace(clear=lambda: None)
    app._progress = types.SimpleNamespace(set=lambda v: None)
    app._update_buttons = lambda: None
    app._update_row = lambda s: None
    app._songs_folder = 'C:/Songs'
    app._settings = {'library_tool_dry_run': {'fix_chart_names': False}}

    targets = [_make_song('C:/Songs/A')]
    app._launch_background(targets, replace=False, resync=False)

    state = gui._load_background_state()
    expected = app._tool_dry_run_prefs()
    assert state['tool_dry_run'] == expected
    assert state['tool_dry_run']['fix_chart_names'] is False
    assert all(state['tool_dry_run'][k] is True for k, _, _ in gui._LIBRARY_TOOLS
               if k != 'fix_chart_names')
