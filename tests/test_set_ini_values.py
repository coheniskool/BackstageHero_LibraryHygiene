# Locks VideoDownload.set_ini_values()'s existing behavior before Task 6
# (metadata enrichment) reuses it as its writer instead of porting our old
# patch_song_ini_keys() (plan.md finding 2). Includes two tests that
# document real quirks rather than an idealized guarantee -- see each
# docstring.

import VideoDownload as vd

_SONG_INI = (
    '[song]\n'
    'name = Test Song\n'
    'artist = Test Artist\n'
    'album = Test Album\n'
    'year = 2020\n'
    'genre = Rock\n'
    'charter = someone\n'
    'diff_guitar = 3\n'
    '; a stray comment that must survive untouched\n'
    'video_start_time = -3000\n'
)


def _write(path, text):
    # newline='' so our fixture's exact bytes land on disk, unaffected by
    # Python's universal-newline write-side translation.
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(text)


def test_set_ini_values_touches_only_target_key(tmp_path):
    _write(tmp_path / 'song.ini', _SONG_INI)

    ok = vd.set_ini_values(str(tmp_path), {'video_start_time': '1234'})

    assert ok is True
    result_lines = (tmp_path / 'song.ini').read_text(encoding='utf-8').splitlines()
    assert 'video_start_time = 1234' in result_lines
    original_lines = _SONG_INI.splitlines()
    for orig, new in zip(original_lines, result_lines):
        if orig.lower().startswith('video_start_time'):
            continue
        assert orig == new, f'unrelated line changed: {orig!r} -> {new!r}'


def test_set_ini_values_inserts_missing_key(tmp_path):
    _write(tmp_path / 'song.ini', _SONG_INI)

    ok = vd.set_ini_values(str(tmp_path), {'backstagehero_source': 'abc123XYZ90'})

    assert ok is True
    result = (tmp_path / 'song.ini').read_text(encoding='utf-8')
    assert 'backstagehero_source = abc123XYZ90' in result
    assert 'name = Test Song' in result
    assert 'video_start_time = -3000' in result


def test_set_ini_values_no_song_section_returns_false(tmp_path):
    _write(tmp_path / 'song.ini', '[other]\nfoo = bar\n')

    ok = vd.set_ini_values(str(tmp_path), {'video_start_time': '1'})

    assert ok is False


def test_set_ini_values_missing_file_returns_false(tmp_path):
    ok = vd.set_ini_values(str(tmp_path), {'video_start_time': '1'})
    assert ok is False


def test_set_ini_values_crlf_round_trip_on_windows(tmp_path):
    """Documents actual behavior, not an idealized guarantee: a CRLF-original
    file stays CRLF after a touch. Python's universal-newline read
    normalizes all line endings to '\\n' in memory, and the platform-native
    write (also without newline='') re-emits '\\r\\n' on Windows -- so a
    CRLF file round-trips cleanly. (An LF-only original would flip to CRLF
    on a Windows write; not exercised here since real song.ini files in this
    project's library are already CRLF.)"""
    crlf_text = _SONG_INI.replace('\n', '\r\n')
    _write(tmp_path / 'song.ini', crlf_text)

    vd.set_ini_values(str(tmp_path), {'video_start_time': '999'})

    raw = (tmp_path / 'song.ini').read_bytes()
    assert b'\r\n' in raw
    assert b'\n' not in raw.replace(b'\r\n', b'')  # no lone \n anywhere


def test_set_ini_values_lowercases_touched_key_label(tmp_path):
    """Known quirk, not something this task fixes: set_ini_values() rebuilds
    a touched line as f'{key}...' using the lookup key, which is always
    lowercase -- so an original mixed-case label like 'Video_Start_Time'
    becomes 'video_start_time' once its value changes. A key whose value we
    don't touch keeps its original casing."""
    _write(tmp_path / 'song.ini', '[song]\nVideo_Start_Time = -3000\nName = Foo\n')

    vd.set_ini_values(str(tmp_path), {'video_start_time': '42'})

    result = (tmp_path / 'song.ini').read_text(encoding='utf-8')
    assert 'video_start_time = 42' in result
    assert 'Video_Start_Time' not in result  # touched key's original casing is lost
    assert 'Name = Foo' in result  # untouched key keeps its casing
