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


@pytest.fixture
def dialog_with_dry_run_prefs(root, tmp_path, monkeypatch):
    """A dialog seeded with persisted dry-run prefs (Task 12), recording every
    on_dry_run_change call so a test can assert the checkbox toggle reaches it."""
    monkeypatch.setattr(gui, '_asset_path', lambda name: str(tmp_path / name))
    changes = []
    d = gui.LibraryToolsDialog(
        root, str(tmp_path),
        dry_run_prefs={'fix_chart_names': False},
        on_dry_run_change=lambda key, value: changes.append((key, value)))
    d.withdraw()
    d._changes = changes
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


# --- (Task 12) seeding checkbox state from persisted prefs -----------------

def test_dry_run_checkbox_seeded_from_dry_run_prefs(dialog_with_dry_run_prefs):
    """A tool explicitly toggled off last time reopens off, not the True
    default -- everything else still defaults to True."""
    d = dialog_with_dry_run_prefs
    assert d._dry_run_vars['fix_chart_names'].get() is False
    for key, _, _ in gui._LIBRARY_TOOLS:
        if key != 'fix_chart_names':
            assert d._dry_run_vars[key].get() is True, key


def test_toggling_dry_run_checkbox_calls_on_dry_run_change(dialog_with_dry_run_prefs):
    d = dialog_with_dry_run_prefs
    d._dry_run_vars['repair_videos'].set(False)

    d._on_dry_toggle('repair_videos')

    assert d._changes == [('repair_videos', False)]

    d._dry_run_vars['repair_videos'].set(True)
    d._on_dry_toggle('repair_videos')

    assert d._changes == [('repair_videos', False), ('repair_videos', True)]


def test_on_dry_toggle_is_a_no_op_without_on_dry_run_change_callback(dialog):
    """Existing callers (and existing tests) that construct LibraryToolsDialog
    without the new Task 12 params must keep working unmodified -- toggling
    must not raise just because no callback was supplied."""
    dialog._dry_run_vars['repair_videos'].set(False)
    dialog._on_dry_toggle('repair_videos')  # must not raise


# --- "Run all tools" -------------------------------------------------------

def _stub_every_tool(monkeypatch, overrides=None):
    """Patch every tool _RUN_ALL_ORDER can dispatch to a no-op returning {},
    except whatever's in `overrides` (key -> callable(folder, dry_run=False))."""
    owners = {
        'migrate_review_folders': (gui.library_common, 'migrate_legacy_review_folders'),
        'fix_chart_names': (gui.chart_rename, 'scan_and_fix_chart_library'),
        'repair_videos': (gui.video_repair, 'scan_and_repair_video_library'),
        'find_static_art': (gui.static_art, 'scan_and_convert_static_art_library'),
        'enrich_metadata': (gui.metadata_enrichment, 'enrich_song_ini_metadata_library'),
        'find_duplicates': (gui.dedupe_report, 'generate_dedupe_report'),
    }
    overrides = overrides or {}
    for key, (owner, name) in owners.items():
        fn = overrides.get(key, lambda folder, dry_run=False: {})
        monkeypatch.setattr(owner, name, fn)


def test_run_all_executes_in_dependency_order(dialog, root, monkeypatch):
    order = []

    def _record(key):
        def _fn(folder, dry_run=False):
            order.append(key)
            return {}
        return _fn

    _stub_every_tool(monkeypatch, {k: _record(k) for k in gui._RUN_ALL_ORDER})

    dialog._run_all()
    ok = _pump(root, 5.0, until=lambda: dialog._running_key is None)

    assert ok, 'Run all never finished'
    assert order == list(gui._RUN_ALL_ORDER)


def test_run_all_survives_one_tool_failing(dialog, root, monkeypatch):
    def boom(folder, dry_run=False):
        raise RuntimeError('scan exploded')

    _stub_every_tool(monkeypatch, {'fix_chart_names': boom})

    dialog._run_all()
    ok = _pump(root, 5.0, until=lambda: dialog._running_key is None)

    assert ok
    assert 'Error' in dialog._status_labels['fix_chart_names'].cget('text')
    # every OTHER tool still ran, despite fix_chart_names blowing up
    assert dialog._status_labels['find_duplicates'].cget('text') != 'Ready'
    status = dialog._run_all_status_lbl.cget('text')
    assert '5/6 tools completed' in status and '1 failed' in status


def test_run_all_is_mutually_exclusive_with_a_single_tool(dialog, root, monkeypatch):
    release = threading.Event()
    _stub_every_tool(monkeypatch, {
        'migrate_review_folders': _blocking_scan(release, {}),
    })

    dialog._run_all()
    assert dialog._running_key == 'run_all'
    dialog._run_tool('repair_videos')          # must be a no-op while run_all holds the lock
    assert dialog._running_key == 'run_all'

    release.set()
    ok = _pump(root, 5.0, until=lambda: dialog._running_key is None)
    assert ok


# --- module-level dispatch, callable with no dialog instance at all --------
#
# A future background-mode controller needs to run the same scans during an
# unattended run, when no LibraryToolsDialog is ever opened. gui._run_library_tool
# must work standalone, taking songs_folder as a plain argument.

def test_run_library_tool_works_with_no_dialog_instance(monkeypatch, tmp_path):
    seen = {}

    def fake_scan(folder, dry_run=False):
        seen['folder'] = folder
        seen['dry_run'] = dry_run
        return {'confirmed_ok': 3, 'needs_review': 1}

    monkeypatch.setattr(gui.chart_rename, 'scan_and_fix_chart_library', fake_scan)

    result = gui._run_library_tool(str(tmp_path), 'fix_chart_names', dry_run=True)

    assert result == {'confirmed_ok': 3, 'needs_review': 1}
    assert seen == {'folder': str(tmp_path), 'dry_run': True}
