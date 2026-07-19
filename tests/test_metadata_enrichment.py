import metadata_enrichment as me

SONG_INI = (
    '[song]\n'
    'name = Kryptonite\n'
    'artist = 3 Doors Down\n'
    'genre = \n'
    'year = \n'
)


def _write_song_ini(folder, text=SONG_INI):
    (folder / 'song.ini').write_text(text, encoding='utf-8')


def _stub_chorus(monkeypatch, result):
    monkeypatch.setattr(me.chorus_client, 'search_by_artist_title', lambda artist, title: result)


# --- sanitize_chorus_field --------------------------------------------------

def test_sanitize_chorus_field_accepts_clean_value():
    assert me.sanitize_chorus_field('Rock') == 'Rock'


def test_sanitize_chorus_field_rejects_non_string():
    assert me.sanitize_chorus_field(2020) is None


def test_sanitize_chorus_field_rejects_empty_after_strip():
    assert me.sanitize_chorus_field('   ') is None


def test_sanitize_chorus_field_rejects_bracket():
    assert me.sanitize_chorus_field('Rock [bad]') is None


def test_sanitize_chorus_field_rejects_semicolon():
    assert me.sanitize_chorus_field('Rock; DROP TABLE') is None


def test_sanitize_chorus_field_rejects_hash():
    assert me.sanitize_chorus_field('Rock # comment') is None


def test_sanitize_chorus_field_rejects_embedded_newline():
    assert me.sanitize_chorus_field('Rock\nInjected') is None


def test_sanitize_chorus_field_caps_length():
    result = me.sanitize_chorus_field('x' * 300)
    assert result is not None
    assert len(result) == 200


# --- _chorus_match_confidence --------------------------------------------------

def test_chorus_match_confidence_perfect_match():
    conf = me._chorus_match_confidence(
        {'name': 'Kryptonite', 'artist': '3 Doors Down'},
        {'name': 'Kryptonite', 'artist': '3 Doors Down'},
    )
    assert conf == 100


def test_chorus_match_confidence_uses_weaker_of_the_two_scores():
    conf = me._chorus_match_confidence(
        {'name': 'Kryptonite', 'artist': '3 Doors Down'},
        {'name': 'Kryptonite', 'artist': 'Totally Different Artist'},
    )
    assert conf < 50


# --- fill_song_ini_metadata --------------------------------------------------

def test_fill_song_ini_metadata_fills_blank_fields(tmp_path, monkeypatch):
    _write_song_ini(tmp_path)
    _stub_chorus(monkeypatch, {
        'name': 'Kryptonite', 'artist': '3 Doors Down',
        'genre': 'Rock', 'year': '2000', 'charter': 'Someone', 'album': 'The Better Life',
    })

    result = me.fill_song_ini_metadata(str(tmp_path))

    assert result['status'] == 'filled'
    text = (tmp_path / 'song.ini').read_text(encoding='utf-8')
    assert 'genre = Rock' in text
    assert 'year = 2000' in text
    assert 'charter = Someone' in text
    assert 'album = The Better Life' in text
    assert 'name = Kryptonite' in text  # untouched, was already populated


def test_fill_song_ini_metadata_never_overwrites_populated_field(tmp_path, monkeypatch):
    _write_song_ini(tmp_path, SONG_INI.replace('genre = \n', 'genre = Alt Rock\n'))
    _stub_chorus(monkeypatch, {
        'name': 'Kryptonite', 'artist': '3 Doors Down',
        'genre': 'Should Not Appear', 'year': '2000',
    })

    result = me.fill_song_ini_metadata(str(tmp_path))

    assert result['status'] == 'filled'  # year gets filled, confirming this genuinely ran
    text = (tmp_path / 'song.ini').read_text(encoding='utf-8')
    assert 'genre = Alt Rock' in text
    assert 'Should Not Appear' not in text
    assert 'year = 2000' in text


def test_fill_song_ini_metadata_no_match_when_chorus_returns_nothing(tmp_path, monkeypatch):
    _write_song_ini(tmp_path)
    _stub_chorus(monkeypatch, None)

    result = me.fill_song_ini_metadata(str(tmp_path))

    assert result['status'] == 'no_match'


def test_fill_song_ini_metadata_no_match_below_confidence_threshold(tmp_path, monkeypatch):
    _write_song_ini(tmp_path)
    _stub_chorus(monkeypatch, {'name': 'Completely Different', 'artist': 'Nobody', 'genre': 'Rock'})

    result = me.fill_song_ini_metadata(str(tmp_path))

    assert result['status'] == 'no_match'
    assert 'below threshold' in result['detail']


def test_fill_song_ini_metadata_no_change_when_nothing_fillable(tmp_path, monkeypatch):
    full_ini = (
        '[song]\nname = Kryptonite\nartist = 3 Doors Down\n'
        'genre = Rock\nyear = 2000\ncharter = Someone\nalbum = The Better Life\n'
    )
    _write_song_ini(tmp_path, full_ini)
    _stub_chorus(monkeypatch, {'name': 'Kryptonite', 'artist': '3 Doors Down', 'genre': 'Rock'})

    result = me.fill_song_ini_metadata(str(tmp_path))

    assert result['status'] == 'no_change'


def test_fill_song_ini_metadata_error_when_no_song_ini(tmp_path):
    result = me.fill_song_ini_metadata(str(tmp_path))
    assert result['status'] == 'error'


def test_fill_song_ini_metadata_error_when_missing_name_or_artist(tmp_path):
    _write_song_ini(tmp_path, '[song]\nname = \nartist = \n')
    result = me.fill_song_ini_metadata(str(tmp_path))
    assert result['status'] == 'error'


def test_fill_song_ini_metadata_dry_run_writes_nothing(tmp_path, monkeypatch):
    _write_song_ini(tmp_path)
    _stub_chorus(monkeypatch, {'name': 'Kryptonite', 'artist': '3 Doors Down', 'genre': 'Rock', 'year': '2000'})

    result = me.fill_song_ini_metadata(str(tmp_path), dry_run=True)

    assert result['status'] == 'filled'
    assert '(dry-run, not applied)' in result['detail']
    text = (tmp_path / 'song.ini').read_text(encoding='utf-8')
    assert 'genre = Rock' not in text  # nothing written


def test_fill_song_ini_metadata_unsafe_value_never_written(tmp_path, monkeypatch):
    _write_song_ini(tmp_path)
    _stub_chorus(monkeypatch, {'name': 'Kryptonite', 'artist': '3 Doors Down', 'genre': 'Rock; DROP', 'year': '2000'})

    me.fill_song_ini_metadata(str(tmp_path))

    text = (tmp_path / 'song.ini').read_text(encoding='utf-8')
    assert 'DROP' not in text
    assert 'year = 2000' in text  # the safe field still gets filled


def test_fill_song_ini_metadata_no_song_section_returns_error(tmp_path, monkeypatch):
    _write_song_ini(tmp_path, '[other]\nname = Kryptonite\nartist = 3 Doors Down\n')
    _stub_chorus(monkeypatch, {'name': 'Kryptonite', 'artist': '3 Doors Down', 'genre': 'Rock'})

    result = me.fill_song_ini_metadata(str(tmp_path))

    assert result['status'] == 'error'
    assert 'song' in result['detail'].lower()


# --- enrich_song_ini_metadata_library --------------------------------------------------

def test_enrich_song_ini_metadata_library_processes_all_folders(tmp_path, monkeypatch):
    home = tmp_path
    song = home / 'Kryptonite'
    song.mkdir()
    _write_song_ini(song)
    (home / '_needs_review').mkdir()  # must be skipped

    _stub_chorus(monkeypatch, {'name': 'Kryptonite', 'artist': '3 Doors Down', 'genre': 'Rock', 'year': '2000'})

    counts = me.enrich_song_ini_metadata_library(home)

    text = (song / 'song.ini').read_text(encoding='utf-8')
    assert 'genre = Rock' in text
    assert counts == {'filled': 1}
