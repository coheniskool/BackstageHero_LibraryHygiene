# Auto-resume-on-launch (Task 13 of SPEC-background-mode.md).
#
# Tasks 11-12 built the background-mode state machine and GUI toggle, but
# nothing resumed a run automatically when the app restarted mid-run. This
# module covers the dispatcher (_maybe_resume_background) and the two resume
# paths it can take (_resume_background_library_tools /
# _resume_background_downloading), plus the one-shot gate in
# _on_library_scanned that must fire the check exactly once per app session.
#
# Follows tests/test_background_mode_controller.py's and
# tests/test_background_mode_gui_wiring.py's conventions: a bare
# object.__new__(gui.App), a fake stop-event whose wait() never really sleeps,
# monkeypatched _BACKGROUND_STATE_FILE for a real temp-file round trip, and
# threading.Thread monkeypatched to run synchronously so the worker's logic
# (including the self.after(0, ...) bounce back to the "main thread") can be
# asserted without a real background thread racing the test.

import queue
import time

import pytest

ctk = pytest.importorskip('customtkinter')
import gui


# --- fakes -------------------------------------------------------------

class _FakeSong:
    def __init__(self, folder, has_video=False):
        self.folder = folder
        self.has_video = has_video


class _FakeStopEvent:
    """Never really blocks. wait() records the timeout it was asked for and
    returns False (timeout elapsed) unless this wait-call's index is in
    `cancel_on`, in which case it behaves like a Stop pressed mid-wait: sets
    itself and returns True."""

    def __init__(self, cancel_on=()):
        self.waits = []
        self._set = False
        self._cancel_on = set(cancel_on)

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def clear(self):
        self._set = False

    def wait(self, timeout=None):
        idx = len(self.waits)
        self.waits.append(timeout)
        if idx in self._cancel_on:
            self._set = True
            return True
        return False


class _FakeLabel:
    def __init__(self):
        self.text = None

    def configure(self, text=None, **kwargs):
        if text is not None:
            self.text = text


class _SyncThread:
    """Stand-in for threading.Thread that runs its target synchronously on
    .start(), so the worker function's logic (including its self.after(0, ...)
    bounce) executes inline where the test can inspect the results."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def _bare_app(tmp_path, monkeypatch, songs_folder='C:/Songs'):
    monkeypatch.setattr(gui, '_BACKGROUND_STATE_FILE',
                        str(tmp_path / 'background_state.json'))
    monkeypatch.setattr(gui.threading, 'Thread', _SyncThread)

    app = object.__new__(gui.App)
    app._songs_folder = songs_folder
    app._songs = []
    app._running = False
    app._background_mode = False
    app._stop_evt = _FakeStopEvent()
    app._status_lbl = _FakeLabel()
    app._update_buttons = lambda: None
    # self.after(0, fn) -> run fn immediately, as if bounced onto the (only)
    # main thread synchronously, matching this test suite's no-real-Tk-loop style.
    app.after = lambda ms, fn=None: (fn() if fn else None)
    return app


def _save_state(**overrides):
    state = {
        'phase': 'downloading',
        'resume_at': None,
        'throttle_count': 0,
        'songs_folder': 'C:/Songs',
        'quality': '720p',
        'replace': False,
        'resync': False,
        'remaining_folders': [],
        'tool_dry_run': {},
    }
    state.update(overrides)
    gui._save_background_state(state)
    return state


# --- _maybe_resume_background dispatch --------------------------------------

def test_no_state_file_is_complete_noop(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch)
    calls = []
    app._resume_background_downloading = lambda state: calls.append('downloading')
    app._resume_background_library_tools = lambda state: calls.append('library_tools')

    app._maybe_resume_background()

    assert calls == []
    assert app._running is False
    assert app._status_lbl.text is None


def test_songs_folder_mismatch_is_noop(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch, songs_folder='C:/Songs/Other')
    _save_state(songs_folder='C:/Songs')
    calls = []
    app._resume_background_downloading = lambda state: calls.append('downloading')
    app._resume_background_library_tools = lambda state: calls.append('library_tools')

    app._maybe_resume_background()

    assert calls == []
    assert app._running is False


def test_phase_done_is_noop(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch)
    _save_state(phase='done')
    calls = []
    app._resume_background_downloading = lambda state: calls.append('downloading')
    app._resume_background_library_tools = lambda state: calls.append('library_tools')

    app._maybe_resume_background()

    assert calls == []


def test_phase_library_tools_dispatches(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch)
    state = _save_state(phase='library_tools')
    calls = []
    app._resume_background_downloading = lambda s: calls.append(('downloading', s))
    app._resume_background_library_tools = lambda s: calls.append(('library_tools', s))

    app._maybe_resume_background()

    assert len(calls) == 1
    assert calls[0][0] == 'library_tools'
    assert calls[0][1]['songs_folder'] == 'C:/Songs'


def test_phase_downloading_dispatches(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch)
    state = _save_state(phase='downloading')
    calls = []
    app._resume_background_downloading = lambda s: calls.append(('downloading', s))
    app._resume_background_library_tools = lambda s: calls.append(('library_tools', s))

    app._maybe_resume_background()

    assert len(calls) == 1
    assert calls[0][0] == 'downloading'


# --- songs_folder normalization (Task 3 of plan-background-mode-fixes.md) ---

def test_songs_folder_case_difference_still_resumes(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch, songs_folder='c:/songs')
    _save_state(phase='downloading', songs_folder='C:/Songs')
    calls = []
    app._resume_background_downloading = lambda s: calls.append(s)
    app._resume_background_library_tools = lambda s: calls.append(s)

    app._maybe_resume_background()

    assert len(calls) == 1


def test_songs_folder_trailing_slash_difference_still_resumes(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch, songs_folder='C:/Songs/')
    _save_state(phase='downloading', songs_folder='C:/Songs')
    calls = []
    app._resume_background_downloading = lambda s: calls.append(s)
    app._resume_background_library_tools = lambda s: calls.append(s)

    app._maybe_resume_background()

    assert len(calls) == 1


def test_songs_folder_mismatch_logs_why(tmp_path, monkeypatch, caplog):
    app = _bare_app(tmp_path, monkeypatch, songs_folder='C:/Songs/Other')
    _save_state(songs_folder='C:/Songs')

    with caplog.at_level('INFO', logger='backstagehero'):
        app._maybe_resume_background()

    assert any('not resuming' in r.message for r in caplog.records)


def test_phase_done_logs_why(tmp_path, monkeypatch, caplog):
    app = _bare_app(tmp_path, monkeypatch)
    _save_state(phase='done')

    with caplog.at_level('INFO', logger='backstagehero'):
        app._maybe_resume_background()

    assert any('nothing to resume' in r.message for r in caplog.records)


# --- _resume_background_library_tools ---------------------------------------

def test_resume_library_tools_marks_running_and_calls_run_all(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch)
    state = _save_state(phase='library_tools')
    calls = []
    app._run_background_library_tools = lambda done, skipped, errors: calls.append(
        (done, skipped, errors))

    app._resume_background_library_tools(state)

    assert app._running is True
    assert app._background_mode is True
    assert calls == [(0, 0, 0)]
    assert 'Library Tools' in app._status_lbl.text


# --- _resume_background_downloading -----------------------------------------

def test_resume_downloading_future_resume_at_waits_then_launches(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch)
    app._songs = [_FakeSong('C:/Songs/A', has_video=False),
                  _FakeSong('C:/Songs/B', has_video=False)]
    now = time.time()
    resume_at = now + 500
    state = _save_state(phase='downloading', resume_at=resume_at,
                        remaining_folders=['C:/Songs/A', 'C:/Songs/B'],
                        replace=True, resync=True)
    calls = []
    app._launch_background = lambda targets, replace, resync: calls.append(
        (targets, replace, resync, app._running))

    app._resume_background_downloading(state)

    assert len(app._stop_evt.waits) == 1
    waited = app._stop_evt.waits[0]
    assert waited == pytest.approx(resume_at - now, abs=2)
    assert len(calls) == 1
    targets, replace, resync, running_at_call = calls[0]
    assert sorted(s.folder for s in targets) == ['C:/Songs/A', 'C:/Songs/B']
    assert replace is True
    assert resync is True
    # The critical gotcha: _running must be reset to False before
    # _launch_background is invoked, or its own guard silently no-ops.
    assert running_at_call is False


def test_resume_downloading_past_resume_at_launches_without_waiting(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch)
    app._songs = [_FakeSong('C:/Songs/A', has_video=False)]
    resume_at = time.time() - 1000
    state = _save_state(phase='downloading', resume_at=resume_at,
                        remaining_folders=['C:/Songs/A'])
    calls = []
    app._launch_background = lambda targets, replace, resync: calls.append(
        (targets, replace, resync))

    app._resume_background_downloading(state)

    assert app._stop_evt.waits == []
    assert len(calls) == 1
    assert [s.folder for s in calls[0][0]] == ['C:/Songs/A']


def test_resume_downloading_all_remaining_now_have_video_skips_to_library_tools(
        tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch)
    # The fresh scan shows both previously-pending folders now have video --
    # nothing left to download.
    app._songs = [_FakeSong('C:/Songs/A', has_video=True),
                  _FakeSong('C:/Songs/B', has_video=True)]
    state = _save_state(phase='downloading', resume_at=None,
                        remaining_folders=['C:/Songs/A', 'C:/Songs/B'])
    launch_calls = []
    lt_calls = []
    app._launch_background = lambda targets, replace, resync: launch_calls.append(targets)
    app._resume_background_library_tools = lambda s: lt_calls.append(s)

    app._resume_background_downloading(state)

    assert launch_calls == []
    assert len(lt_calls) == 1
    assert lt_calls[0]['songs_folder'] == 'C:/Songs'


def test_resume_downloading_no_resume_at_launches_without_waiting(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch)
    app._songs = [_FakeSong('C:/Songs/A', has_video=False)]
    state = _save_state(phase='downloading', resume_at=None,
                        remaining_folders=['C:/Songs/A'])
    calls = []
    app._launch_background = lambda targets, replace, resync: calls.append(targets)

    app._resume_background_downloading(state)

    assert app._stop_evt.waits == []
    assert len(calls) == 1


def test_stop_during_pre_resume_wait_never_launches_and_leaves_state(tmp_path, monkeypatch):
    app = _bare_app(tmp_path, monkeypatch)
    app._songs = [_FakeSong('C:/Songs/A', has_video=False)]
    app._stop_evt = _FakeStopEvent(cancel_on={0})
    resume_at = time.time() + 500
    state = _save_state(phase='downloading', resume_at=resume_at,
                        remaining_folders=['C:/Songs/A'])
    calls = []
    app._launch_background = lambda targets, replace, resync: calls.append(targets)

    app._resume_background_downloading(state)

    assert calls == []
    assert app._running is False
    assert app._background_mode is False
    assert 'stopped' in app._status_lbl.text.lower()
    # background_state.json must be left in place -- same policy as Task 11's
    # mid-backoff Stop -- so a future resume attempt can still pick it up.
    reloaded = gui._load_background_state()
    assert reloaded.get('phase') == 'downloading'
    assert reloaded.get('resume_at') == resume_at


# --- one-shot gate on _on_library_scanned ------------------------------------

def test_on_library_scanned_only_fires_resume_check_once(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, '_BACKGROUND_STATE_FILE',
                        str(tmp_path / 'background_state.json'))
    _save_state(phase='downloading')

    app = object.__new__(gui.App)
    app._songs_folder = 'C:/Songs'
    app._songs = []
    app._pending_background_resume_check = True
    app._apply_filter = lambda: None
    app._export_library_csv = lambda: None
    app._update_buttons = lambda: None
    app._maybe_start_enrichment = lambda: None
    app._status_lbl = _FakeLabel()

    calls = []
    app._maybe_resume_background = lambda: calls.append(1)

    app._on_library_scanned([])
    assert calls == [1]
    assert app._pending_background_resume_check is False

    # A later rescan in the same session (e.g. after a Library Tools run)
    # must NOT re-trigger the resume dispatch.
    app._on_library_scanned([])
    assert calls == [1]
