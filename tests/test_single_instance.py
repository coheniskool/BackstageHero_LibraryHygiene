# Single-instance guard (SPEC-launch-reliability.md, Part A).
#
# Nothing used to stop a second instance, and App.__init__ schedules
# _maybe_resume_background -- so a stray double-click during a multi-day
# background run put two processes on the same background_state.json, each
# overwriting the other's remaining_folders.
#
# NOTHING HERE MAY ACQUIRE A REAL NAMED MUTEX. gui._win32() exists purely as
# the seam these tests substitute: a real CreateMutexW inside pytest would
# make the test process itself hold the lock for its whole life, poisoning
# every later test in the session and any concurrent run of the suite. Every
# test below goes through _FakeWin32.

import pytest

ctk = pytest.importorskip('customtkinter')
import gui


# --- fakes ------------------------------------------------------------------

class _FakeKernel32:
    def __init__(self, already_exists, handle=1234, raises=False):
        self._already_exists = already_exists
        self._handle = handle
        self._raises = raises
        self.mutex_names = []

    def CreateMutexW(self, sec, initial_owner, name):
        if self._raises:
            raise OSError('CreateMutexW unavailable')
        self.mutex_names.append(name)
        return self._handle

    def GetLastError(self):
        return gui._ERROR_ALREADY_EXISTS if self._already_exists else 0


class _FakeUser32:
    def __init__(self, hwnd=0, focus_raises=False):
        self._hwnd = hwnd
        self._focus_raises = focus_raises
        self.found = []
        self.shown = []
        self.foregrounded = []
        self.message_boxes = []

    def FindWindowW(self, cls, title):
        if self._focus_raises:
            raise OSError('FindWindowW unavailable')
        self.found.append(title)
        return self._hwnd

    def ShowWindow(self, hwnd, cmd):
        self.shown.append((hwnd, cmd))

    def SetForegroundWindow(self, hwnd):
        self.foregrounded.append(hwnd)

    def MessageBoxW(self, hwnd, text, caption, flags):
        self.message_boxes.append((text, caption, flags))
        return 1


class _FakeWin32:
    def __init__(self, already_exists=False, hwnd=0,
                 kernel_raises=False, focus_raises=False, handle=1234):
        self.kernel32 = _FakeKernel32(already_exists, handle, kernel_raises)
        self.user32 = _FakeUser32(hwnd, focus_raises)


class _FakeApp:
    """Stand-in for gui.App so run() never builds a real Tk root."""
    constructed = 0

    def __init__(self):
        _FakeApp.constructed += 1

    def update(self):
        pass

    def mainloop(self):
        pass


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset the module-level handle around every test, and stub the two
    collaborators run() reaches that have nothing to do with this guard."""
    monkeypatch.setattr(gui, '_SINGLE_INSTANCE_MUTEX', None)
    monkeypatch.setattr(gui.updater, '_cleanup_old_exe', lambda: None)
    monkeypatch.setattr(gui, 'App', _FakeApp)
    _FakeApp.constructed = 0
    yield
    gui._SINGLE_INSTANCE_MUTEX = None


# --- the lock itself --------------------------------------------------------

def test_first_instance_proceeds_to_construct_the_app(monkeypatch):
    win32 = _FakeWin32(already_exists=False)
    monkeypatch.setattr(gui, '_win32', lambda: win32)

    gui.run()

    assert _FakeApp.constructed == 1
    assert win32.kernel32.mutex_names == [gui._SINGLE_INSTANCE_MUTEX_NAME]
    # Nothing was focused and nobody was told anything -- this IS the instance.
    assert win32.user32.foregrounded == []
    assert win32.user32.message_boxes == []


def test_second_instance_exits_zero_and_never_constructs_the_app(monkeypatch):
    """The exit code is the trap: 'Launch BackstageHero.bat' retries on a
    non-zero %ERRORLEVEL% and then opens the log in notepad, so a non-zero
    exit here would be retried, blocked again, and would tell the user the app
    'could not start twice in a row'."""
    win32 = _FakeWin32(already_exists=True, hwnd=555)
    monkeypatch.setattr(gui, '_win32', lambda: win32)

    with pytest.raises(SystemExit) as excinfo:
        gui.run()

    assert excinfo.value.code == 0
    assert _FakeApp.constructed == 0


def test_second_instance_focuses_the_existing_window(monkeypatch):
    win32 = _FakeWin32(already_exists=True, hwnd=555)
    monkeypatch.setattr(gui, '_win32', lambda: win32)

    assert gui._acquire_single_instance_lock() is False

    assert win32.user32.foregrounded == [555]
    assert win32.user32.shown == [(555, 9)]        # SW_RESTORE
    # Focus worked, so no message box was needed.
    assert win32.user32.message_boxes == []


def test_focus_targets_the_titlebar_text_app_actually_sets(monkeypatch):
    """Regression on a brittle-by-necessity coupling: the lookup title must
    match gui.py's own self.title(f'BackstageHero  v{__version__}') exactly,
    two spaces and all."""
    win32 = _FakeWin32(already_exists=True, hwnd=555)
    monkeypatch.setattr(gui, '_win32', lambda: win32)

    gui._acquire_single_instance_lock()

    assert win32.user32.found == [f'BackstageHero  v{gui.__version__}']


def test_second_instance_falls_back_to_a_message_box_when_focus_fails(monkeypatch):
    # hwnd 0 == window not found (a stale title, a minimized-to-tray edge
    # case). A double-click must never appear to do nothing.
    win32 = _FakeWin32(already_exists=True, hwnd=0)
    monkeypatch.setattr(gui, '_win32', lambda: win32)

    assert gui._acquire_single_instance_lock() is False

    assert len(win32.user32.message_boxes) == 1
    text, caption, _flags = win32.user32.message_boxes[0]
    assert 'already running' in text.lower()
    assert caption == 'BackstageHero'


def test_second_instance_messages_when_focus_raises(monkeypatch):
    """A raising FindWindowW must still block the launch -- the 'already
    running' decision is made before focusing and must not be undone by a
    later failure."""
    win32 = _FakeWin32(already_exists=True, focus_raises=True)
    monkeypatch.setattr(gui, '_win32', lambda: win32)

    assert gui._acquire_single_instance_lock() is False
    assert len(win32.user32.message_boxes) == 1


def test_a_raising_win32_call_starts_the_app_anyway(monkeypatch, caplog):
    """A guard that can prevent the app from launching is worse than no
    guard."""
    win32 = _FakeWin32(kernel_raises=True)
    monkeypatch.setattr(gui, '_win32', lambda: win32)

    with caplog.at_level('WARNING', logger='backstagehero'):
        assert gui._acquire_single_instance_lock() is True

    assert any('starting normally' in r.message for r in caplog.records)


def test_win32_seam_itself_missing_starts_the_app_anyway(monkeypatch):
    # Non-Windows, or a hardened environment where ctypes.windll is absent.
    def boom():
        raise AttributeError("module 'ctypes' has no attribute 'windll'")
    monkeypatch.setattr(gui, '_win32', boom)

    assert gui._acquire_single_instance_lock() is True


def test_the_mutex_handle_is_retained_in_module_state(monkeypatch):
    """A local would be garbage-collected, releasing the mutex and silently
    disarming the guard partway through the process's life."""
    win32 = _FakeWin32(already_exists=False, handle=99001)
    monkeypatch.setattr(gui, '_win32', lambda: win32)

    gui._acquire_single_instance_lock()

    assert gui._SINGLE_INSTANCE_MUTEX == 99001


def test_a_blocked_launch_retains_no_handle(monkeypatch):
    win32 = _FakeWin32(already_exists=True, hwnd=555)
    monkeypatch.setattr(gui, '_win32', lambda: win32)

    gui._acquire_single_instance_lock()

    assert gui._SINGLE_INSTANCE_MUTEX is None


# --- no PID lock file, and no state writes from a blocked launch ------------

def test_no_pid_lock_file_anywhere_in_the_guard():
    """Never list: a PID lock file would leave a hard-killed app with a stale
    lock nobody can clear -- strictly worse than the problem being solved.
    Windows releases a mutex when the process dies by any means."""
    import inspect
    source = (inspect.getsource(gui._acquire_single_instance_lock)
              + inspect.getsource(gui._focus_existing_instance)
              + inspect.getsource(gui._win32))
    lowered = source.lower()
    for banned in ('open(', 'os.remove', 'lockfile', '.lock', 'getpid'):
        assert banned not in lowered, f'guard must not use {banned!r}'


def test_blocked_launch_does_not_touch_background_state(monkeypatch, tmp_path):
    """A second instance must never write background_state.json -- the whole
    reason this guard exists is two processes fighting over it."""
    state_file = tmp_path / 'background_state.json'
    state_file.write_text('{"phase": "downloading"}', encoding='utf-8')
    before = state_file.read_text(encoding='utf-8')
    monkeypatch.setattr(gui, '_BACKGROUND_STATE_FILE', str(state_file))

    win32 = _FakeWin32(already_exists=True, hwnd=555)
    monkeypatch.setattr(gui, '_win32', lambda: win32)

    with pytest.raises(SystemExit):
        gui.run()

    assert state_file.read_text(encoding='utf-8') == before


# --- source-order regression ------------------------------------------------

def test_guard_runs_before_the_app_is_constructed():
    """Ordering is the correctness property: App.__init__ schedules
    _maybe_resume_background, so a second instance must exit before reaching
    it. Asserted on source order because run()'s early-exit path means a
    behavioral test can only ever observe the App that was NOT built."""
    import inspect
    source = inspect.getsource(gui.run)
    guard = source.index('_acquire_single_instance_lock')
    # 'app = App()', not bare 'App()' -- the latter also matches the prose in
    # run()'s own comments, which sit above the call and would pass trivially.
    construct = source.index('app = App()')
    assert guard < construct


def test_app_user_model_id_still_runs_before_the_window():
    """Pre-existing behavior this task must not disturb."""
    import inspect
    source = inspect.getsource(gui.run)
    appid = source.index('SetCurrentProcessExplicitAppUserModelID')
    # 'app = App()', not bare 'App()' -- the latter also matches the prose in
    # run()'s own comments, which sit above the call and would pass trivially.
    construct = source.index('app = App()')
    assert appid < construct


def _launcher_text():
    import os
    bat = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'Launch BackstageHero.bat')
    with open(bat, encoding='utf-8', errors='replace') as f:
        return f.read()


@pytest.mark.parametrize('wait, ping', [(20, 21), (40, 41), (60, 61)])
def test_launcher_retry_ladder_waits_and_ping_fallbacks_agree(wait, ping):
    """The .bat has no test surface of its own, so pin each rung's wait
    together with its ping fallback -- ping waits n-1 seconds, so the pair
    must move together or the fallback silently waits a different duration
    than the primary path."""
    text = _launcher_text()
    assert f'timeout /t {wait} /nobreak' in text
    assert f'ping -n {ping} 127.0.0.1' in text


def test_launcher_stamps_every_attempt_so_recovery_time_is_recoverable():
    """The ladder doubles as the instrument for launch-reliability Open
    Question 2: whichever attempt finally succeeds bounds how long the import
    transient actually persists. That only works if every rung stamps its own
    exit code and time."""
    text = _launcher_text()
    for n in (2, 3, 4):
        assert f'attempt {n} exited with code %ERRORLEVEL% at %DATE% %TIME%' in text
    # ...and every rung must still short-circuit on success, or a later
    # attempt would relaunch the app on top of a running one.
    assert text.count('if %ERRORLEVEL% EQU 0 goto :done') == 4
