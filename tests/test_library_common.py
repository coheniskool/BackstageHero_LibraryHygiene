import json
import wave

import pytest

import library_common as lc


# --- find_song_audio ---------------------------------------------------

def test_find_song_audio_plain_name(tmp_path):
    (tmp_path / 'song.ogg').write_bytes(b'x')
    assert lc.find_song_audio(tmp_path).name == 'song.ogg'


def test_find_song_audio_id_suffixed_name(tmp_path):
    (tmp_path / 'song_1877.ogg').write_bytes(b'x')
    assert lc.find_song_audio(tmp_path).name == 'song_1877.ogg'


def test_find_song_audio_falls_back_to_sole_audio_file(tmp_path):
    (tmp_path / 'guitar.ogg').write_bytes(b'x')
    assert lc.find_song_audio(tmp_path).name == 'guitar.ogg'


def test_find_song_audio_never_guesses_between_multiple_stems(tmp_path):
    (tmp_path / 'guitar.ogg').write_bytes(b'x')
    (tmp_path / 'drums.ogg').write_bytes(b'x')
    assert lc.find_song_audio(tmp_path) is None


def test_find_song_audio_no_audio_returns_none(tmp_path):
    assert lc.find_song_audio(tmp_path) is None


# --- shared folder-listing helper (perf-simplification pass) --------------

def test_shared_listing_avoids_rescanning_the_folder(tmp_path, monkeypatch):
    """find_song_audio/find_video_file used to each do their own independent
    glob()/exists() scans of the same folder. Passing one shared listing
    (from list_song_folder_files) must mean the folder is only scanned once."""
    (tmp_path / 'song.ogg').write_bytes(b'x')
    (tmp_path / 'video.mp4').write_bytes(b'x')

    calls = []
    real = lc._list_folder_entries

    def _counting(folder):
        calls.append(folder)
        return real(folder)

    monkeypatch.setattr(lc, '_list_folder_entries', _counting)

    files = lc.list_song_folder_files(tmp_path)
    assert lc.find_song_audio(tmp_path, files=files).name == 'song.ogg'
    assert lc.find_video_file(tmp_path, files=files).name == 'video.mp4'

    assert len(calls) == 1


def test_iter_song_folders_lists_each_container_directory_once(tmp_path, monkeypatch):
    """A container folder (holding other song folders, not song content
    itself) used to be listed twice per level: once via looks_like_song_folder
    to check whether it was a song folder, once by the recursive walk
    re-listing it to find its children. No directory should appear twice."""
    pack = tmp_path / 'Pack'
    song = pack / 'Song'
    song.mkdir(parents=True)
    (song / 'song.ini').write_text('[song]\n', encoding='utf-8')

    calls = []
    real = lc._list_folder_entries

    def _counting(folder):
        calls.append(str(folder))
        return real(folder)

    monkeypatch.setattr(lc, '_list_folder_entries', _counting)

    found = list(lc.iter_song_folders(tmp_path))

    assert [f.name for f in found] == ['Song']
    assert len(calls) == len(set(calls))   # no directory listed more than once


# --- find_song_ini -------------------------------------------------------

def test_find_song_ini_prefers_literal_name(tmp_path):
    (tmp_path / 'song_2400.ini').write_text('[song]\n', encoding='utf-8')
    (tmp_path / 'song.ini').write_text('[song]\n', encoding='utf-8')
    assert lc.find_song_ini(tmp_path).name == 'song.ini'


def test_find_song_ini_falls_back_to_id_suffixed(tmp_path):
    (tmp_path / 'song_2400.ini').write_text('[song]\n', encoding='utf-8')
    assert lc.find_song_ini(tmp_path).name == 'song_2400.ini'


def test_find_song_ini_none_when_absent(tmp_path):
    assert lc.find_song_ini(tmp_path) is None


# --- find_video_file -------------------------------------------------------

@pytest.mark.parametrize('name', lc.VIDEO_NAMES)
def test_find_video_file_recognizes_every_canonical_name(tmp_path, name):
    (tmp_path / name).write_bytes(b'x')
    assert lc.find_video_file(tmp_path).name == name


def test_find_video_file_none_when_absent(tmp_path):
    assert lc.find_video_file(tmp_path) is None


# --- read_song_ini_fields -------------------------------------------------

def test_read_song_ini_fields_parses_requested_keys(tmp_path):
    ini = tmp_path / 'song.ini'
    ini.write_text('[song]\nName = My Song\nArtist = My Artist\nyear=2020\n', encoding='utf-8')
    fields = lc.read_song_ini_fields(ini, ('name', 'artist', 'year', 'genre'))
    assert fields == {'name': 'My Song', 'artist': 'My Artist', 'year': '2020'}


def test_read_song_ini_fields_missing_file_returns_empty(tmp_path):
    assert lc.read_song_ini_fields(tmp_path / 'missing.ini', ('name',)) == {}


def test_read_song_ini_fields_single_pass_matches_per_key_semantics(tmp_path):
    """Regression for the single-pass rewrite (one regex pass over the file
    instead of one search per key): still case-insensitive on both the
    requested key and the file's own key casing, still skips keys that
    aren't present, and a duplicate key's FIRST occurrence still wins --
    matching what per-key re.search used to find."""
    ini = tmp_path / 'song.ini'
    ini.write_text(
        '[song]\nName = My Song\nARTIST = My Artist\nyear=2020\n'
        'diff_guitar = 3\ndiff_guitar = 5\n',
        encoding='utf-8')

    fields = lc.read_song_ini_fields(ini, ('name', 'Artist', 'year', 'diff_guitar', 'genre'))

    assert fields == {'name': 'My Song', 'artist': 'My Artist', 'year': '2020',
                       'diff_guitar': '3'}


# --- read_chart_song_fields -------------------------------------------------

def test_read_chart_song_fields_extracts_name_and_artist(tmp_path):
    chart = tmp_path / 'notes.chart'
    chart.write_text('[Song]\n{\n  Name = "Kryptonite"\n  Artist = "3 Doors Down"\n}\n', encoding='utf-8')
    fields = lc.read_chart_song_fields(chart)
    assert fields == {'name': 'Kryptonite', 'artist': '3 Doors Down'}


def test_read_chart_song_fields_missing_file_returns_empty(tmp_path):
    assert lc.read_chart_song_fields(tmp_path / 'missing.chart') == {}


# --- probe_audio_duration_ms -------------------------------------------------

def _write_silent_wav(path, seconds, sr=8000):
    n = int(seconds * sr)
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b'\x00\x00' * n)


def test_probe_audio_duration_ms_real_ffprobe(tmp_path):
    wav = tmp_path / 'clip.wav'
    _write_silent_wav(wav, seconds=2.0)
    duration_ms = lc.probe_audio_duration_ms(wav)
    assert duration_ms is not None
    assert abs(duration_ms - 2000) <= 50


def test_probe_audio_duration_ms_missing_file_returns_none(tmp_path):
    assert lc.probe_audio_duration_ms(tmp_path / 'missing.wav') is None


# --- normalize_lookup_value / strip_title_noise / parse_folder_name --------

def test_normalize_lookup_value_lowercases_and_strips_punctuation():
    assert lc.normalize_lookup_value("Rock & Roll Feeling!") == 'rock roll feeling'


def test_normalize_lookup_value_none_is_empty_string():
    assert lc.normalize_lookup_value(None) == ''


def test_strip_title_noise_removes_live_marker():
    assert lc.strip_title_noise('Kryptonite (Live)') == 'Kryptonite'


def test_strip_title_noise_removes_pedal_marker():
    assert lc.strip_title_noise('Freebird (2x Bass Pedal Expert+)') == 'Freebird'


def test_parse_folder_name_splits_artist_and_title():
    artist, title = lc.parse_folder_name('3 Doors Down - Kryptonite')
    assert artist == '3 Doors Down'
    assert title == 'Kryptonite'


def test_parse_folder_name_no_separator_returns_whole_name_as_title():
    artist, title = lc.parse_folder_name('Kryptonite')
    assert artist == ''
    assert title == 'Kryptonite'


# --- move_to_review ---------------------------------------------------------
#
# The review folder is always a SIBLING of home_folder (e.g. home_folder =
# tmp_path/'Library' -> review folder = tmp_path/'Library_needs_review'),
# never nested inside it -- Clone Hero scans its library root recursively
# with no awareness of this project's naming convention, so a review folder
# living inside the scanned root would still get loaded by the game.

def _make_song_folder(root, name, files):
    folder = root / name
    folder.mkdir()
    for fname, content in files.items():
        (folder / fname).write_text(content, encoding='utf-8')
    return folder


def _make_home(tmp_path):
    home = tmp_path / 'Library'
    home.mkdir()
    return home


def test_move_to_review_dry_run_touches_nothing(tmp_path):
    home = _make_home(tmp_path)
    song = _make_song_folder(home, 'Some Song', {'song.ini': 'x'})
    result = lc.move_to_review(song, home, '_needs_review', 'test reason', dry_run=True)
    assert result is None
    assert song.exists()
    assert not (tmp_path / 'Library_needs_review').exists()


def test_move_to_review_lands_outside_home_folder(tmp_path):
    home = _make_home(tmp_path)
    song = _make_song_folder(home, 'Some Song', {'song.ini': 'x', 'notes.chart': 'y'})

    dest = lc.move_to_review(song, home, '_needs_review', 'ambiguous: two .ini files')

    assert not song.exists()
    assert dest.exists()
    assert (dest / 'song.ini').exists()
    assert (dest / 'notes.chart').exists()
    # the whole point: the review folder is a sibling, not nested inside home
    assert dest.parent == tmp_path / 'Library_needs_review'
    assert dest.parent.parent == home.parent
    assert not any(p.name == '_needs_review' for p in home.iterdir() if p.is_dir())

    manifest_path = tmp_path / 'Library_needs_review_manifest.jsonl'
    assert manifest_path.exists()
    entry = json.loads(manifest_path.read_text(encoding='utf-8').strip())
    assert entry['reason'] == 'ambiguous: two .ini files'
    assert entry['cross_volume'] is False
    assert entry['verification'] == 'not_applicable'
    assert entry['destination'] == str(dest)


def test_move_to_review_collision_gets_dup_suffix(tmp_path):
    home = _make_home(tmp_path)
    review_root = tmp_path / 'Library_needs_review'
    review_root.mkdir()
    (review_root / 'Some Song').mkdir()  # pre-existing folder at the destination name

    song = _make_song_folder(home, 'Some Song', {'song.ini': 'x'})
    dest = lc.move_to_review(song, home, '_needs_review', 'collision test')

    assert dest.name == 'Some Song [dup1]'
    assert dest.exists()


def test_move_to_review_cross_volume_success(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    song = _make_song_folder(home, 'Some Song', {'song.ini': 'x', 'notes.chart': 'y'})

    monkeypatch.setattr(lc, '_dest_is_same_volume', lambda source, dest_parent: False)

    dest = lc.move_to_review(song, home, '_duplicates_review', 'lower score than keeper',
                              extra_manifest_fields={'score': 12})

    assert not song.exists()  # source removed only after verified copy
    assert dest.exists()
    assert (dest / 'song.ini').exists()

    manifest_path = tmp_path / 'Library_duplicates_review_manifest.jsonl'
    entry = json.loads(manifest_path.read_text(encoding='utf-8').strip())
    assert entry['cross_volume'] is True
    assert entry['verification'] == 'ok'
    assert entry['score'] == 12


def test_move_to_review_cross_volume_verification_failure_leaves_source_untouched(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    song = _make_song_folder(home, 'Some Song', {'song.ini': 'x'})

    monkeypatch.setattr(lc, '_dest_is_same_volume', lambda source, dest_parent: False)

    calls = {'n': 0}
    real_size_and_count = lc._folder_size_and_count

    def _flaky(folder):
        calls['n'] += 1
        if calls['n'] == 1:
            return real_size_and_count(folder)  # source: real count
        return (999999, 999)  # dest: forced mismatch

    monkeypatch.setattr(lc, '_folder_size_and_count', _flaky)

    with pytest.raises(RuntimeError, match='cross-volume copy verification failed'):
        lc.move_to_review(song, home, '_duplicates_review', 'test failure')

    assert song.exists()  # source untouched
    manifest_path = tmp_path / 'Library_duplicates_review_manifest.jsonl'
    entry = json.loads(manifest_path.read_text(encoding='utf-8').strip())
    assert entry['verification'] == 'failed'
