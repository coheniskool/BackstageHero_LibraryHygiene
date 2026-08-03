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
    # also persists accent_source='swatch' -- a swatch click always reverts
    # that role away from 'custom' (Custom picker or album art), even if it
    # was already 'swatch' before this click
    assert ('accent', 'red') in dialog._changes
    assert ('accent_source', 'swatch') in dialog._changes
    assert dialog._accent_buttons['red'].cget('border_width') == 2
    assert dialog._accent_buttons['denim'].cget('border_width') == 0


def test_changing_cover_updates_selection_and_persists(dialog):
    dialog._on_cover_change('yellow')
    assert dialog._prefs['cover'] == 'yellow'
    assert ('cover', 'yellow') in dialog._changes
    assert ('cover_source', 'swatch') in dialog._changes


def test_changing_stdev_multiplier_rounds_and_persists(dialog):
    dialog._on_multiplier_change(2.34)
    assert dialog._prefs['stdev_multiplier'] == 2.3
    assert dialog._changes == [('stdev_multiplier', 2.3)]
    assert '2.3' in dialog._multiplier_lbl.cget('text')


def test_generate_passes_stdev_multiplier_through(dialog, root, monkeypatch):
    seen = {}

    def fake_generate(*a, **k):
        seen.update(k)
        return {'pdf_path': None, 'html_path': None, 'page_count': 1,
                'stats': {'totalArtists': 1, 'totalSongs': 1}}
    monkeypatch.setattr(gui.songbook, 'generate_songbook', fake_generate)
    dialog._on_multiplier_change(2.0)

    dialog._generate()
    ok = _pump(root, 5.0, until=lambda: not dialog._generating)

    assert ok
    assert seen['stdev_multiplier'] == 2.0


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


# --- custom color picker (per role) ---------------------------------------------

def test_custom_accent_button_exists_alongside_swatches(dialog):
    assert 'custom' in dialog._accent_buttons
    assert 'custom' in dialog._cover_buttons


def test_choosing_a_custom_accent_color_updates_only_that_role(dialog, monkeypatch):
    monkeypatch.setattr(gui.colorchooser, 'askcolor', lambda **k: ((59, 89, 152), '#3B5998'))
    original_cover = dict(dialog._prefs)

    dialog._on_custom_accent()

    assert dialog._prefs['accent_source'] == 'custom'
    assert dialog._prefs['accent_custom_hex'].startswith('#')
    assert dialog._changes[-1][0] in ('accent_custom_hex', 'accent_source')
    # cover role's own settings must be untouched
    assert dialog._prefs.get('cover_source', 'swatch') == original_cover.get('cover_source', 'swatch')
    assert dialog._prefs.get('cover') == original_cover.get('cover')


def test_choosing_a_custom_cover_color_updates_only_that_role(dialog, monkeypatch):
    monkeypatch.setattr(gui.colorchooser, 'askcolor', lambda **k: ((140, 39, 39), '#8C2727'))
    dialog._on_custom_cover()
    assert dialog._prefs['cover_source'] == 'custom'
    assert dialog._prefs['cover_custom_hex'].startswith('#')
    assert dialog._prefs.get('accent_source', 'swatch') == 'swatch'


def test_custom_color_is_clamped_for_legibility(dialog, monkeypatch):
    # Pure white should not survive clamping unchanged -- proves the dialog
    # actually routes the picked color through songbook._clamp_for_legibility()
    # rather than using the raw OS-dialog value.
    monkeypatch.setattr(gui.colorchooser, 'askcolor', lambda **k: ((255, 255, 255), '#FFFFFF'))
    dialog._on_custom_accent()
    assert dialog._prefs['accent_custom_hex'] != '#FFFFFF'


def test_custom_color_button_shows_the_clamped_hex(dialog, monkeypatch):
    monkeypatch.setattr(gui.colorchooser, 'askcolor', lambda **k: ((255, 255, 255), '#FFFFFF'))
    dialog._on_custom_accent()
    shown = dialog._accent_buttons['custom'].cget('fg_color')
    assert shown == dialog._prefs['accent_custom_hex']


def test_cancelling_the_custom_color_dialog_is_a_noop(dialog, monkeypatch):
    monkeypatch.setattr(gui.colorchooser, 'askcolor', lambda **k: (None, None))
    before = dict(dialog._prefs)
    changes_before = list(dialog._changes)

    dialog._on_custom_accent()

    assert dialog._prefs.get('accent_source', 'swatch') == before.get('accent_source', 'swatch')
    assert dialog._changes == changes_before


def test_clicking_a_swatch_after_custom_reverts_that_role_to_swatch(dialog, monkeypatch):
    monkeypatch.setattr(gui.colorchooser, 'askcolor', lambda **k: ((59, 89, 152), '#3B5998'))
    dialog._on_custom_accent()
    assert dialog._prefs['accent_source'] == 'custom'

    dialog._on_accent_change('red')

    assert dialog._prefs['accent_source'] == 'swatch'
    assert dialog._prefs['accent'] == 'red'


# --- album art row -----------------------------------------------------------------

def _real_art_image(tmp_path, rgb=(200, 30, 30)):
    from PIL import Image
    path = tmp_path / 'art.png'
    Image.new('RGB', (80, 80), rgb).save(path)
    return str(path)


def test_choosing_an_image_sets_both_roles_to_custom(dialog, root, tmp_path, monkeypatch):
    art_path = _real_art_image(tmp_path)
    monkeypatch.setattr(gui.filedialog, 'askopenfilename', lambda **k: art_path)

    dialog._on_choose_image()
    ok = _pump(root, 5.0, until=lambda: dialog._prefs.get('album_art_path') == art_path)

    assert ok, 'album art extraction never finished'
    assert dialog._prefs['accent_source'] == 'custom'
    assert dialog._prefs['cover_source'] == 'custom'
    assert dialog._prefs['accent_custom_hex'].startswith('#')
    assert dialog._prefs['cover_custom_hex'].startswith('#')
    assert dialog._clear_art_btn.cget('state') == 'normal'
    assert dialog._show_on_cover_var.get() is False  # unchecked by default, per spec


def test_choosing_an_image_cancel_dialog_is_a_noop(dialog, monkeypatch):
    monkeypatch.setattr(gui.filedialog, 'askopenfilename', lambda **k: '')
    before = dict(dialog._prefs)

    dialog._on_choose_image()

    assert dialog._prefs.get('album_art_path', '') == before.get('album_art_path', '')
    assert dialog._clear_art_btn.cget('state') == 'disabled'


def test_choosing_a_bad_image_shows_album_art_error(dialog, root, tmp_path, monkeypatch):
    bad_path = tmp_path / 'not_an_image.png'
    bad_path.write_bytes(b'not an image')
    monkeypatch.setattr(gui.filedialog, 'askopenfilename', lambda **k: str(bad_path))

    dialog._on_choose_image()
    ok = _pump(root, 5.0, until=lambda: dialog._status_lbl.cget('text') != 'Ready')

    assert ok
    assert dialog._prefs.get('accent_source', 'swatch') == 'swatch'
    assert dialog._clear_art_btn.cget('state') == 'disabled'


def test_show_on_cover_checkbox_disabled_until_image_loaded(dialog):
    assert dialog._show_on_cover_checkbox.cget('state') == 'disabled'


def test_show_on_cover_checkbox_enabled_after_image_loaded(dialog, root, tmp_path, monkeypatch):
    art_path = _real_art_image(tmp_path)
    monkeypatch.setattr(gui.filedialog, 'askopenfilename', lambda **k: art_path)
    dialog._on_choose_image()
    _pump(root, 5.0, until=lambda: dialog._prefs.get('album_art_path') == art_path)
    assert dialog._show_on_cover_checkbox.cget('state') == 'normal'


def test_clearing_the_image_disables_checkbox_and_reverts_state(dialog, root, tmp_path, monkeypatch):
    art_path = _real_art_image(tmp_path)
    monkeypatch.setattr(gui.filedialog, 'askopenfilename', lambda **k: art_path)
    dialog._on_choose_image()
    _pump(root, 5.0, until=lambda: dialog._prefs.get('album_art_path') == art_path)

    dialog._on_clear_image()

    assert dialog._prefs.get('album_art_path', '') == ''
    assert dialog._clear_art_btn.cget('state') == 'disabled'
    assert dialog._show_on_cover_checkbox.cget('state') == 'disabled'


def test_worker_passes_cover_image_path_only_when_checked_and_loaded(dialog, root, tmp_path, monkeypatch):
    art_path = _real_art_image(tmp_path)
    monkeypatch.setattr(gui.filedialog, 'askopenfilename', lambda **k: art_path)
    dialog._on_choose_image()
    _pump(root, 5.0, until=lambda: dialog._prefs.get('album_art_path') == art_path)

    seen = {}

    def fake_generate(*a, **k):
        seen.update(k)
        return {'pdf_path': None, 'html_path': None, 'page_count': 1,
                'stats': {'totalArtists': 1, 'totalSongs': 1}}
    monkeypatch.setattr(gui.songbook, 'generate_songbook', fake_generate)

    # unchecked -> no cover_image_path
    dialog._generate()
    _pump(root, 5.0, until=lambda: not dialog._generating)
    assert seen.get('cover_image_path') is None

    # checked -> cover_image_path passed through
    dialog._show_on_cover_var.set(True)
    dialog._generate()
    _pump(root, 5.0, until=lambda: not dialog._generating)
    assert seen.get('cover_image_path') == art_path


def test_worker_uses_custom_hex_when_source_is_custom(dialog, root, monkeypatch):
    monkeypatch.setattr(gui.colorchooser, 'askcolor', lambda **k: ((59, 89, 152), '#3B5998'))
    dialog._on_custom_accent()
    expected_hex = dialog._prefs['accent_custom_hex']

    seen = {}

    def fake_generate(*a, **k):
        seen.update(k)
        return {'pdf_path': None, 'html_path': None, 'page_count': 1,
                'stats': {'totalArtists': 1, 'totalSongs': 1}}
    monkeypatch.setattr(gui.songbook, 'generate_songbook', fake_generate)

    dialog._generate()
    _pump(root, 5.0, until=lambda: not dialog._generating)

    assert seen['accent_color'] == expected_hex
