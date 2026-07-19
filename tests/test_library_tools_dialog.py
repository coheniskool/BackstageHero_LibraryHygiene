# First tests to touch LibraryToolsDialog. They exist for one finding:
# closing the dialog mid-run used to release the modal grab and destroy the
# window while the worker thread carried on renaming and relocating files.
# Nothing then stopped the user pressing Start in the main window, so a
# download run and a rename sweep could mutate the same library at once.
#
# Needs a real Tk display, so the whole module skips without one rather than
# failing on a headless machine.

import threading
import time

import pytest

ctk = pytest.importorskip('customtkinter')
import gui


@pytest.fixture(scope='module')
def root():
    """One Tk root for the whole module, deliberately.

    A per-test root meant this module built and tore down eight Tcl
    interpreters in a row, and the later ones intermittently failed with
    "Can't find a usable init.tcl" -- which surfaced as a test that SKIPPED
    on some runs and passed on others. A test that sometimes silently proves
    nothing is worse than no test, so the churn is removed rather than the
    symptom tolerated. Each test still gets its own dialog.
    """
    try:
        r = ctk.CTk()
    except Exception as exc:                      # genuinely no display / no Tk
        pytest.skip(f'Tk unavailable: {exc}')
    r.withdraw()
    yield r
    try:
        r.destroy()
    except Exception:
        pass


@pytest.fixture
def dialog(root, tmp_path, monkeypatch):
    """A dialog wired to record the run-state callback and the close callback."""
    events = {'run_state': [], 'closed': 0}
    monkeypatch.setattr(gui, '_asset_path', lambda name: str(tmp_path / name))
    d = gui.LibraryToolsDialog(
        root, str(tmp_path),
        on_close=lambda: events.__setitem__('closed', events['closed'] + 1),
        on_run_state=lambda running: events['run_state'].append(running))
    d.withdraw()
    d._events = events
    yield d
    try:
        if d.winfo_exists():
            d._running_key = None
            d.destroy()
    except Exception:
        pass


def _pump(root, seconds, until):
    """Wait for `until` while running a REAL mainloop.

    Not root.update() in a loop: Tk's after() raises "main thread is not in
    main loop" when called from a worker thread while no mainloop is running,
    so an update()-polling harness silently fails to deliver exactly the
    cross-thread hand-off under test -- and would report the production code
    as broken when it is the harness that is.
    """
    deadline = time.time() + seconds
    done = []

    def poll():
        if until():
            done.append(True)
            root.quit()
        elif time.time() > deadline:
            root.quit()
        else:
            root.after(20, poll)

    root.after(20, poll)
    root.mainloop()
    return bool(done)


def _blocking_scan(release, result):
    """A tool that parks until released, then returns a real counts dict."""
    def scan(folder, dry_run=False):
        release.wait(5)
        return result
    return scan


# --- closing while a tool is running --------------------------------------

def test_closing_mid_run_asks_first_and_declining_keeps_the_dialog(dialog, monkeypatch):
    asked = []
    monkeypatch.setattr(gui.messagebox, 'askokcancel',
                        lambda *a, **k: asked.append(a) or False)
    dialog._running_key = 'fix_chart_names'
    dialog._dry_run_of_current = False

    dialog._close()

    assert asked, 'closed a running destructive scan without asking'
    assert dialog.winfo_exists()


def test_closing_mid_run_does_not_reload_the_library(dialog, monkeypatch):
    """on_close reloads the song list. Running it while a rename sweep is
    still going reads a folder tree being rewritten underneath it."""
    monkeypatch.setattr(gui.messagebox, 'askokcancel', lambda *a, **k: True)
    dialog._running_key = 'fix_chart_names'
    dialog._dry_run_of_current = False

    dialog._close()

    assert not dialog.winfo_exists()
    assert dialog._events['closed'] == 0


def test_closing_when_idle_needs_no_confirmation_and_does_reload(dialog, monkeypatch):
    monkeypatch.setattr(gui.messagebox, 'askokcancel',
                        lambda *a, **k: pytest.fail('asked about an idle dialog'))

    dialog._close()

    assert not dialog.winfo_exists()
    assert dialog._events['closed'] == 1


def test_the_warning_distinguishes_a_dry_run_from_a_real_one(dialog, monkeypatch):
    seen = {}
    monkeypatch.setattr(gui.messagebox, 'askokcancel',
                        lambda title, message, **k: seen.update(msg=message) or False)
    dialog._running_key = 'fix_chart_names'

    dialog._dry_run_of_current = True
    dialog._close()
    assert 'previewing' in seen['msg']

    dialog._dry_run_of_current = False
    dialog._close()
    assert 'changing files in' in seen['msg']


# --- the run-state contract the main window depends on --------------------

def test_run_state_reports_false_even_when_the_dialog_was_closed_mid_run(dialog, root, monkeypatch):
    """The core of the fix. The main window stays locked for the WORKER's
    lifetime, not the dialog's -- so the unlock has to arrive after the
    dialog is gone, posted to the parent's loop rather than the dead one."""
    release = threading.Event()
    monkeypatch.setattr(gui.chart_rename, 'scan_and_fix_chart_library',
                        _blocking_scan(release, {'confirmed_ok': 1}))
    monkeypatch.setattr(gui.messagebox, 'askokcancel', lambda *a, **k: True)

    dialog._run_tool('fix_chart_names')
    assert dialog._events['run_state'] == [True], 'main window never locked'

    dialog._close()                        # user closes while the scan runs
    assert not dialog.winfo_exists()

    release.set()                          # scan finishes with no dialog left
    ok = _pump(root, 5.0, until=lambda: dialog._events['run_state'] == [True, False])
    assert ok, 'main window never unlocked after the orphaned worker finished'


def test_run_state_reports_false_when_the_tool_raises(dialog, root, monkeypatch):
    """A crashing scan must not leave the main window locked forever."""
    def boom(folder, dry_run=False):
        raise RuntimeError('scan exploded')
    monkeypatch.setattr(gui.chart_rename, 'scan_and_fix_chart_library', boom)

    dialog._run_tool('fix_chart_names')

    ok = _pump(root, 5.0,
               until=lambda: dialog._events['run_state'] == [True, False]
               and 'Error' in dialog._status_labels['fix_chart_names'].cget('text'))
    assert ok, f'run state was {dialog._events["run_state"]}'


def test_a_second_tool_cannot_start_while_one_is_running(dialog, root, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(gui.chart_rename, 'scan_and_fix_chart_library',
                        _blocking_scan(release, {}))
    started = []

    def repair(folder, dry_run=False):
        started.append(1)
        return {}
    monkeypatch.setattr(gui.video_repair, 'scan_and_repair_video_library', repair)

    dialog._run_tool('fix_chart_names')
    assert dialog._running_key == 'fix_chart_names'
    dialog._run_tool('repair_videos')          # must be a no-op

    release.set()
    _pump(root, 5.0, until=lambda: dialog._running_key is None)
    assert started == [], 'a second tool ran over the same library'


def test_dry_run_defaults_on_for_every_tool(dialog):
    """Station-3 tools must never default to applying changes."""
    for key, _, _ in gui._LIBRARY_TOOLS:
        assert dialog._dry_run_vars[key].get() is True, key
