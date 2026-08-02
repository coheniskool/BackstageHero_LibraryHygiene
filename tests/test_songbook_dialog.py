# Tests for SongbookDialog, mirroring test_library_tools_dialog.py's shape
# (module-scoped root, _pump for cross-thread assertions, monkeypatched
# _asset_path) since it's the closest existing analog: another CTkToplevel
# that runs a library-wide operation on a background thread.
#
# Needs a real Tk display, so the whole module skips without one rather than
# failing on a headless machine.

import threading
import time

import pytest

ctk = pytest.importorskip('customtkinter')
import gui
import songbook


@pytest.fixture(scope='module')
def root():
    try:
        r = ctk.CTk()
    except Exception as exc:
        pytest.skip(f'Tk unavailable: {exc}')
    r.withdraw()
    yield r
    try:
        r.destroy()
    except Exception:
        pass


class _FakeSong:
    """Duck-typed stand-in for gui.py's Song dataclass -- only .folder is read."""
    def __init__(self, folder):
        self.folder = folder


def _write_song(tmp_path, name, artist, title):
    folder = tmp_path / name
    folder.mkdir()
    (folder / 'song.ini').write_text(
        f'[song]\nartist = {artist}\nname = {title}\n', encoding='utf-8')
    return folder


def _library(tmp_path):
    f1 = _write_song(tmp_path, 'a', 'Sublime', 'Santeria')
    f2 = _write_song(tmp_path, 'b', 'Sum 41', 'Fat Lip')
    return [_FakeSong(str(f1)), _FakeSong(str(f2))]


@pytest.fixture
def dialog(root, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, '_asset_path', lambda name: str(tmp_path / name))
    songs = _library(tmp_path)
    changes = []
    d = gui.SongbookDialog(
        root, str(tmp_path), songs,
        options={'columns': 3, 'binding_margin': 0.9, 'accent': 'denim', 'cover': 'red'},
        on_option_change=lambda key, value: changes.append((key, value)))
    d.withdraw()
    d._changes = changes
    yield d
    try:
        if d.winfo_exists():
            d._generating = False
            d.destroy()
    except Exception:
        pass


def _pump(root, seconds, until):
    """Wait for `until` while running a REAL mainloop -- see
    test_library_tools_dialog.py's identical helper for why root.update()
    polling doesn't work for cross-thread self.after() hand-offs."""
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


# --- option controls --------------------------------------------------------

def test_dialog_builds_with_seeded_options(dialog):
    assert dialog._prefs['columns'] == 3
    assert dialog._prefs['binding_margin'] == 0.9
    assert dialog._prefs['accent'] == 'denim'
    assert dialog._prefs['cover'] == 'red'


def test_changing_columns_updates_selection_and_persists(dialog):
    dialog._on_columns_change(4)
    assert dialog._prefs['columns'] == 4
    assert dialog._changes == [('columns', 4)]
    assert dialog._col_buttons[4].cget('fg_color') == gui._BLUE


def test_changing_margin_rounds_to_nearest_quarter_step_and_persists(dialog):
    dialog._on_margin_change(1.07)   # nearest 0.05 step is 1.05
    assert dialog._prefs['binding_margin'] == 1.05
    assert dialog._changes == [('binding_margin', 1.05)]
    assert '1.05' in dialog._margin_lbl.cget('text')


def test_changing_accent_updates_selection_and_persists(dialog):
    dialog._on_accent_change('red')
    assert dialog._prefs['accent'] == 'red'
    assert dialog._changes == [('accent', 'red')]
    assert dialog._accent_buttons['red'].cget('border_width') == 2
    assert dialog._accent_buttons['denim'].cget('border_width') == 0


def test_changing_cover_updates_selection_and_persists(dialog):
    dialog._on_cover_change('yellow')
    assert dialog._prefs['cover'] == 'yellow'
    assert dialog._changes == [('cover', 'yellow')]


# --- generate: success/failure/busy-guard -----------------------------------

def test_generate_success_updates_status_and_enables_open(dialog, root, monkeypatch, tmp_path):
    fake_result = {'pdf_path': tmp_path / 'out.pdf', 'html_path': tmp_path / 'out.html',
                   'page_count': 5, 'stats': {'totalArtists': 2, 'totalSongs': 2}}
    monkeypatch.setattr(gui.songbook, 'generate_songbook', lambda *a, **k: fake_result)

    dialog._generate()
    ok = _pump(root, 5.0, until=lambda: not dialog._generating)

    assert ok, 'generation never finished'
    assert dialog._result == fake_result
    assert dialog._open_btn.cget('state') == 'normal'
    assert 'Done' in dialog._status_lbl.cget('text')
    assert dialog._status_lbl.cget('text_color') == gui._GREEN


def test_generate_empty_library_shows_specific_error(dialog, root, monkeypatch):
    def boom(*a, **k):
        raise songbook.EmptyLibraryError('nothing to print')
    monkeypatch.setattr(gui.songbook, 'generate_songbook', boom)

    dialog._generate()
    ok = _pump(root, 5.0, until=lambda: not dialog._generating)

    assert ok
    assert dialog._result is None
    assert dialog._open_btn.cget('state') == 'disabled'
    assert 'nothing to print' in dialog._status_lbl.cget('text')
    assert dialog._status_lbl.cget('text_color') == gui._RED


def test_generate_browser_missing_shows_specific_error(dialog, root, monkeypatch):
    def boom(*a, **k):
        raise songbook.BrowserNotFoundError('Could not find Chrome or Edge')
    monkeypatch.setattr(gui.songbook, 'generate_songbook', boom)

    dialog._generate()
    ok = _pump(root, 5.0, until=lambda: not dialog._generating)

    assert ok
    assert 'Chrome or Edge' in dialog._status_lbl.cget('text')
    assert dialog._status_lbl.cget('text_color') == gui._RED


def test_generate_is_a_noop_while_already_running(dialog, root, monkeypatch):
    release = threading.Event()
    calls = []

    def blocking(*a, **k):
        calls.append(1)
        release.wait(5)
        return {'pdf_path': None, 'html_path': None, 'page_count': 1,
                'stats': {'totalArtists': 1, 'totalSongs': 1}}
    monkeypatch.setattr(gui.songbook, 'generate_songbook', blocking)

    dialog._generate()
    assert dialog._generating is True
    dialog._generate()          # must be a no-op -- generation already running

    release.set()
    ok = _pump(root, 5.0, until=lambda: not dialog._generating)
    assert ok
    assert calls == [1], 'a second generation ran concurrently'


# --- open + close ------------------------------------------------------------

def test_open_pdf_opens_the_result_path(dialog, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(gui, '_open_in_file_manager', lambda path: opened.append(path))
    dialog._result = {'pdf_path': tmp_path / 'Clone Hero Songbook.pdf'}

    dialog._open_pdf()

    assert opened == [str(tmp_path / 'Clone Hero Songbook.pdf')]


def test_open_pdf_is_a_noop_before_any_generation(dialog, monkeypatch):
    monkeypatch.setattr(gui, '_open_in_file_manager',
                        lambda path: pytest.fail('opened a file before generating anything'))
    dialog._open_pdf()   # must not raise, must not open anything


def test_closing_while_generating_asks_first(dialog, monkeypatch):
    asked = []
    monkeypatch.setattr(gui.messagebox, 'askokcancel',
                        lambda *a, **k: asked.append(a) or False)
    dialog._generating = True

    dialog._close()

    assert asked, 'closed mid-generation without asking'
    assert dialog.winfo_exists()


def test_closing_when_idle_needs_no_confirmation(dialog, monkeypatch):
    monkeypatch.setattr(gui.messagebox, 'askokcancel',
                        lambda *a, **k: pytest.fail('asked about an idle dialog'))

    dialog._close()

    assert not dialog.winfo_exists()


# --- end-to-end through the real songbook module ----------------------------
#
# Everything above mocks gui.songbook.generate_songbook to isolate the
# dialog's own wiring. This one calls the REAL function through the real
# background thread, proving the GUI path and the CLI path
# (test_generate_songbook_from_song_list_gui_mode_skips_rescan in
# test_songbook.py) are the same function, not a GUI-only duplicate.

def test_generate_end_to_end_through_real_songbook_module(dialog, root):
    dialog._generate()
    ok = _pump(root, 20.0, until=lambda: not dialog._generating)

    assert ok, 'real generation never finished'
    assert dialog._status_lbl.cget('text_color') == gui._GREEN, dialog._status_lbl.cget('text')
    assert dialog._result is not None
    assert dialog._result['html_path'].exists()
    assert dialog._result['stats']['totalArtists'] == 2
