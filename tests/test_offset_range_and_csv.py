# Two user-reported gaps:
#
#   1. The sync editor clamped the offset to [-30s, +90s]. A chart genuinely
#      needing about -78s could not be set at all -- dragging or nudging past
#      -30s snapped straight back, silently leaving the song wrong.
#   2. A full library scan kept everything in memory and wrote nothing out,
#      so there was no way to look over the library in a spreadsheet.

import csv
import io
import sys

import pytest

ctk = pytest.importorskip('customtkinter')
import gui


# --- the offset clamp -----------------------------------------------------
#
# SyncEditor.__init__ builds an mpv/ffplay preview, which a test has no
# business starting. The clamp lived in pure arithmetic on two attributes, so
# these drive that arithmetic directly on an uninitialised instance -- the real
# methods, not a reimplementation of them.

def _editor(start_ms):
    ed = object.__new__(gui.SyncEditor)
    ed._ms_min = gui.SyncEditor._MS_MIN_DEFAULT
    ed._ms_max = gui.SyncEditor._MS_MAX_DEFAULT
    ed._grow_window_for(start_ms)
    return ed


def test_the_old_bounds_are_only_a_starting_window():
    assert gui.SyncEditor._MS_MIN_DEFAULT == -30_000
    assert gui.SyncEditor._MS_MAX_DEFAULT == 90_000
    # and nothing still treats them as limits
    assert not hasattr(gui.SyncEditor, '_MS_MIN')
    assert not hasattr(gui.SyncEditor, '_MS_MAX')


def test_a_song_already_past_the_default_window_opens_at_its_real_offset():
    """The reported song: about -78s stored. Opening the editor must show that
    value, not a clamped -30000 that would be written back on save."""
    ed = _editor(-78_000)
    assert ed._ms_min <= -78_000
    assert ed._ms_min == -78_000 - gui.SyncEditor._MS_WINDOW_PAD


def test_the_window_grows_instead_of_clamping():
    ed = _editor(0)
    assert ed._grow_window_for(-250_000) is True
    assert ed._ms_min <= -250_000
    # and again, further out
    assert ed._grow_window_for(-600_000) is True
    assert ed._ms_min <= -600_000


def test_the_window_never_shrinks_back():
    """Shrinking mid-edit would yank the slider handle out from under the
    user, so growth is one-way for the lifetime of the dialog."""
    ed = _editor(-200_000)
    low = ed._ms_min
    assert ed._grow_window_for(0) is False
    assert ed._ms_min == low


def test_a_value_inside_the_window_needs_no_growth():
    ed = _editor(0)
    assert ed._grow_window_for(-1_000) is False
    assert ed._ms_min == gui.SyncEditor._MS_MIN_DEFAULT


@pytest.mark.parametrize('target', [-78_000, -120_000, -600_000, 500_000])
def test_any_offset_the_user_asks_for_is_representable(target):
    ed = _editor(target)
    assert ed._ms_min <= target <= ed._ms_max


# --- _read_offset (perf-simplification: now reuses the shared ini reader) -

def _bare_editor_for(folder, label='Test'):
    ed = object.__new__(gui.SyncEditor)
    ed._song = _FakeSong(folder, label, True, '720p')
    return ed


def test_read_offset_uses_the_shared_ini_reader(tmp_path):
    """Regression for reusing VideoDownload's shared reader instead of a
    hand-rolled line scan -- must still parse a real stored value."""
    a = _write_song(tmp_path / 'Song', 'Song', video_start_time='-4200')
    assert _bare_editor_for(a)._read_offset() == -4200


def test_read_offset_falls_back_to_default_when_unparsable(tmp_path):
    a = _write_song(tmp_path / 'Song', 'Song', video_start_time='not-a-number')
    assert _bare_editor_for(a)._read_offset() == gui.DEFAULT_START_TIME


def test_read_offset_falls_back_to_default_when_absent(tmp_path):
    a = _write_song(tmp_path / 'Song', 'Song')
    assert _bare_editor_for(a)._read_offset() == gui.DEFAULT_START_TIME


# --- the CSV export -------------------------------------------------------

class _FakeSong:
    def __init__(self, folder, label, has_video, res):
        self.folder, self.label = str(folder), label
        self.has_video, self.res = has_video, res
        self.key = label.lower()


def _app_with(songs, folder):
    """A bare App instance carrying only what _export_library_csv touches --
    constructing the real window would start update checks and a full scan."""
    app = object.__new__(gui.App)
    app._songs = songs
    app._songs_folder = str(folder)
    return app


def _write_song(folder, name, **ini):
    folder.mkdir(parents=True, exist_ok=True)
    body = ''.join(f'{k} = {v}\n' for k, v in ini.items())
    (folder / 'song.ini').write_text(
        f'[song]\nname = {name}\nartist = Someone\n{body}', encoding='utf-8')
    return folder


def _read_csv(path):
    with open(path, newline='', encoding='utf-8-sig') as fh:
        return list(csv.reader(fh))


def test_the_csv_lands_in_the_songs_folder_with_the_song_data(tmp_path):
    a = _write_song(tmp_path / 'Kryptonite', 'Kryptonite',
                    video_start_time='-4200', backstagehero_sync='measured',
                    backstagehero_source='abc123')
    songs = [_FakeSong(a, '3 Doors Down - Kryptonite', True, '1080p')]

    _app_with(songs, tmp_path)._export_library_csv()

    rows = _read_csv(tmp_path / gui.App.CSV_NAME)
    assert rows[0][0] == 'Song'
    body = rows[1]
    assert body[0] == '3 Doors Down - Kryptonite'
    assert body[3] == 'yes'
    assert body[4] == '1080p'
    assert body[5] == '-4200'
    assert body[6] == 'measured'          # provenance, so guesses sort out
    assert body[9] == 'abc123'


def test_the_csv_records_dumped_videos(tmp_path):
    a = _write_song(tmp_path / 'Song', 'Song',
                    backstagehero_rejected='bad1,bad2')
    songs = [_FakeSong(a, 'Song', False, '-')]

    _app_with(songs, tmp_path)._export_library_csv()

    rows = _read_csv(tmp_path / gui.App.CSV_NAME)
    assert 'bad1' in rows[1][10] and 'bad2' in rows[1][10]


def test_the_csv_names_the_kind_of_video_attached(tmp_path):
    """Sorting on this column is how you find every lyric video and gameplay
    capture in one pass -- fingerprinting cannot tell them apart, because
    their audio is identical to the real thing."""
    a = _write_song(tmp_path / 'Ironic', 'Ironic',
                    backstagehero_video_title='Alanis Morissette - Ironic (Lyrics)')
    b = _write_song(tmp_path / 'Living', 'Living',
                    backstagehero_video_title='Among the Living (Rock Band Expert FC)')
    c = _write_song(tmp_path / 'Creep', 'Creep',
                    backstagehero_video_title='Radiohead - Creep')
    songs = [_FakeSong(a, 'Ironic', True, '720p'),
             _FakeSong(b, 'Living', True, '720p'),
             _FakeSong(c, 'Creep', True, '720p')]

    _app_with(songs, tmp_path)._export_library_csv()

    kinds = {r[0]: r[7] for r in _read_csv(tmp_path / gui.App.CSV_NAME)[1:]}
    assert kinds['Ironic'] == 'lyric'
    assert kinds['Living'] == 'gameplay'
    assert kinds['Creep'] == ''        # an ordinary upload is not flagged


def test_the_kind_column_is_blank_for_videos_downloaded_before_titles_were_kept(tmp_path):
    """Existing libraries have no stored title. Blank must mean 'unknown',
    never be mistaken for 'checked and fine'."""
    a = _write_song(tmp_path / 'Old', 'Old', backstagehero_source='abc123')
    _app_with([_FakeSong(a, 'Old', True, '720p')], tmp_path)._export_library_csv()
    assert _read_csv(tmp_path / gui.App.CSV_NAME)[1][7] == ''


def test_a_webm_only_song_says_why_it_reads_as_having_no_video(tmp_path):
    """Real library, Phase 4: a song with video.webm showed 'Has video: no',
    which looks like a broken export. The app counts only video.mp4 on
    purpose -- a webm from another tool is usually VP9 and unplayable here, so
    the song is meant to re-download -- but the column has to say that rather
    than flatly contradict what is in the folder."""
    a = _write_song(tmp_path / 'Fat Lip', 'Fat Lip')
    (a / 'video.webm').write_bytes(b'vp9 bytes')
    songs = [_FakeSong(a, 'Sum 41 - Fat Lip', False, '-')]

    _app_with(songs, tmp_path)._export_library_csv()

    cell = _read_csv(tmp_path / gui.App.CSV_NAME)[1][3]
    assert cell.startswith('no')
    assert 'video.webm' in cell and 're-download' in cell


def test_a_playable_song_still_just_says_yes(tmp_path):
    a = _write_song(tmp_path / 'Good', 'Good')
    (a / 'video.mp4').write_bytes(b'x')
    songs = [_FakeSong(a, 'Good', True, '720p')]

    _app_with(songs, tmp_path)._export_library_csv()

    assert _read_csv(tmp_path / gui.App.CSV_NAME)[1][3] == 'yes'


def test_a_song_with_no_video_reports_no_resolution(tmp_path):
    a = _write_song(tmp_path / 'No Video', 'No Video')
    songs = [_FakeSong(a, 'No Video', False, '-')]

    _app_with(songs, tmp_path)._export_library_csv()

    body = _read_csv(tmp_path / gui.App.CSV_NAME)[1]
    assert body[3] == 'no'
    assert body[4] == ''


def test_the_csv_survives_a_title_cp1252_cannot_encode(tmp_path):
    """Written as utf-8 regardless of console encoding -- the export must not
    become a new way for a unicode song title to break a scan."""
    a = _write_song(tmp_path / 'Kryptonite ♥', 'Kryptonite ♥')
    songs = [_FakeSong(a, 'Kryptonite ♥ 東京', True, '720p')]

    _app_with(songs, tmp_path)._export_library_csv()

    rows = _read_csv(tmp_path / gui.App.CSV_NAME)
    assert '♥' in rows[1][0] and '東京' in rows[1][0]


def test_rows_are_sorted_so_the_file_is_stable_between_runs(tmp_path):
    songs = [
        _FakeSong(_write_song(tmp_path / 'Zebra', 'Zebra'), 'Zebra', False, '-'),
        _FakeSong(_write_song(tmp_path / 'Apple', 'Apple'), 'Apple', False, '-'),
    ]

    _app_with(songs, tmp_path)._export_library_csv()

    rows = _read_csv(tmp_path / gui.App.CSV_NAME)
    assert [r[0] for r in rows[1:]] == ['Apple', 'Zebra']


def test_an_unwritable_library_folder_does_not_break_the_app(tmp_path, monkeypatch):
    """A convenience file must never cost the user the ability to use the app."""
    a = _write_song(tmp_path / 'Song', 'Song')
    app = _app_with([_FakeSong(a, 'Song', False, '-')], tmp_path)

    def _denied(*a, **k):
        raise OSError(13, 'Permission denied')
    monkeypatch.setattr('builtins.open', _denied)

    app._export_library_csv()          # must not raise


def test_no_csv_is_written_before_a_library_is_loaded(tmp_path):
    _app_with([], tmp_path)._export_library_csv()
    assert not (tmp_path / gui.App.CSV_NAME).exists()


def test_export_csv_parses_each_songs_ini_once(tmp_path, monkeypatch):
    """The export used to re-open+parse each song's ini 6-7 times (once per
    ini-derived column). It must now do exactly one _read_ini_section() call
    per song, regardless of how many columns are written from it."""
    a = _write_song(tmp_path / 'Song', 'Song',
                    video_start_time='-4200', backstagehero_sync='measured',
                    backstagehero_video_title='Band - Song (Lyrics)',
                    backstagehero_source='abc123')
    songs = [_FakeSong(a, 'Song', True, '720p')]

    import VideoDownload
    calls = []
    real = VideoDownload._read_ini_section

    def _counting(folder):
        calls.append(folder)
        return real(folder)

    monkeypatch.setattr(VideoDownload, '_read_ini_section', _counting)

    _app_with(songs, tmp_path)._export_library_csv()

    assert len(calls) == 1
    row = _read_csv(tmp_path / gui.App.CSV_NAME)[1]
    assert row[5] == '-4200' and row[6] == 'measured' and row[7] == 'lyric'
    assert row[8] == 'Band - Song (Lyrics)' and row[9] == 'abc123'
