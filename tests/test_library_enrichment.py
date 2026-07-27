# tests/test_library_enrichment.py
# Covers library_enrichment.enrich_library() -- Task 2.1 of the
# library-enrichment plan. See tasks/plan-library-enrichment.md.

import json

import library_enrichment as le
import resolver_client

CHART_TEXT = (
    '[Song]\n{\n  Name = "Kryptonite"\n  Artist = "3 Doors Down"\n  Resolution = 192\n}\n'
    '[ExpertSingle]\n{\n  0 = N 0 0\n  192 = N 1 0\n  384 = N 2 0\n}\n'
)

INI_TEXT = (
    '[song]\nname = Kryptonite\nartist = 3 Doors Down\nsong_length = 200000\n'
)


def _make_song(root, name, chart_text=CHART_TEXT, ini_text=INI_TEXT, extra_files=None):
    folder = root / name
    folder.mkdir()
    (folder / 'song.ini').write_text(ini_text, encoding='utf-8')
    (folder / 'notes.chart').write_text(chart_text, encoding='utf-8')
    for filename, content in (extra_files or {}).items():
        (folder / filename).write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
    return folder


def _stub_chorus(monkeypatch, result=None):
    monkeypatch.setattr(le.chorus_cache.chorus_client, 'search_by_artist_title',
                         lambda artist, title: result)


def test_enrich_library_writes_sidecar_with_expected_entry(tmp_path, monkeypatch):
    _stub_chorus(monkeypatch, result=None)
    song = _make_song(tmp_path, '3 Doors Down - Kryptonite',
                       extra_files={'guitar.ogg': b'fake', 'album.png': b'fake'})

    summary = le.enrich_library(tmp_path)

    assert summary['songs_processed'] == 1
    assert summary['songs_skipped'] == 0

    sidecar_path = tmp_path / le.SIDECAR_FILENAME
    assert sidecar_path.exists()
    with open(sidecar_path, encoding='utf-8') as f:
        sidecar = json.load(f)

    assert sidecar['version'] == le.SIDECAR_VERSION
    assert len(sidecar['songs']) == 1
    chart_hash = resolver_client.chart_hash(str(song))
    entry = sidecar['songs'][chart_hash]
    assert entry['instruments']['guitar'] == 1
    assert entry['instruments']['bass'] == -1
    assert entry['note_count'] == 3
    assert entry['song_length_ms'] == 200000
    assert entry['stems'] == ['guitar.ogg']
    assert entry['has_album_art'] is True
    assert entry['status'] == 'success'


def test_enrich_library_dry_run_writes_sidecar(tmp_path, monkeypatch):
    _stub_chorus(monkeypatch, result=None)
    song = _make_song(tmp_path, '3 Doors Down - Kryptonite')

    summary = le.enrich_library(tmp_path, dry_run=True)

    assert summary['songs_processed'] == 1

    sidecar_path = tmp_path / le.SIDECAR_FILENAME
    assert sidecar_path.exists()
    with open(sidecar_path, encoding='utf-8') as f:
        sidecar = json.load(f)

    chart_hash = resolver_client.chart_hash(str(song))
    assert chart_hash in sidecar['songs']


def test_dry_run_then_real_run_skips_everything(tmp_path, monkeypatch):
    _stub_chorus(monkeypatch, result=None)
    _make_song(tmp_path, '3 Doors Down - Kryptonite')

    dry = le.enrich_library(tmp_path, dry_run=True)
    assert dry['songs_processed'] == 1
    assert dry['songs_skipped'] == 0

    real = le.enrich_library(tmp_path, dry_run=False)
    assert real['songs_processed'] == 0
    assert real['songs_skipped'] == 1


def test_enrich_library_incremental_skips_unchanged_song(tmp_path, monkeypatch):
    _stub_chorus(monkeypatch, result=None)
    _make_song(tmp_path, '3 Doors Down - Kryptonite')

    first = le.enrich_library(tmp_path)
    assert first['songs_processed'] == 1
    assert first['songs_skipped'] == 0

    second = le.enrich_library(tmp_path)
    assert second['songs_processed'] == 0
    assert second['songs_skipped'] == 1


def test_enrich_library_force_reprocesses_unchanged_song(tmp_path, monkeypatch):
    _stub_chorus(monkeypatch, result=None)
    _make_song(tmp_path, '3 Doors Down - Kryptonite')

    le.enrich_library(tmp_path)
    forced = le.enrich_library(tmp_path, force=True)

    assert forced['songs_processed'] == 1
    assert forced['songs_skipped'] == 0


def test_enrich_library_song_with_no_chart_file_is_a_problem_not_a_crash(tmp_path):
    folder = tmp_path / 'Broken Song'
    folder.mkdir()
    (folder / 'song.ini').write_text('[song]\nname = Broken\n', encoding='utf-8')
    # No notes.chart/notes.mid/notes.eof at all -> no chart_hash -> can't be indexed.

    summary = le.enrich_library(tmp_path)

    assert summary['songs_processed'] == 0
    assert summary['problems_found'] == 1
    sidecar_path = tmp_path / le.SIDECAR_FILENAME
    with open(sidecar_path, encoding='utf-8') as f:
        sidecar = json.load(f)
    assert sidecar['songs'] == {}


def test_enrich_library_missing_notes_chart_flags_problem_but_still_indexes(tmp_path, monkeypatch):
    """Has notes.mid (so it's identifiable) but no notes.chart -- instrument/
    NPS/feature data is unavailable, and that must be visible as a problem,
    not silently absent."""
    _stub_chorus(monkeypatch, result=None)
    folder = tmp_path / 'Mid Only'
    folder.mkdir()
    (folder / 'song.ini').write_text('[song]\nname = Mid Only\nartist = Someone\n', encoding='utf-8')
    (folder / 'notes.mid').write_bytes(b'MThd fake midi')

    summary = le.enrich_library(tmp_path)
    assert summary['songs_processed'] == 1

    sidecar_path = tmp_path / le.SIDECAR_FILENAME
    with open(sidecar_path, encoding='utf-8') as f:
        sidecar = json.load(f)
    entry = next(iter(sidecar['songs'].values()))
    assert entry['instruments']['guitar'] == -1
    assert entry['note_count'] is None
    assert any('notes.chart' in p for p in entry['problems'])


def test_enrich_library_uses_cached_chorus_client_not_raw_chorus_client(tmp_path, monkeypatch):
    """Regression guard: enrichment must go through the cache wrapper, not
    call chorus_client directly -- otherwise Task 1.3's caching is dead code
    for this integration path."""
    calls = []

    def fake_search(artist, title):
        calls.append((artist, title))
        return {'name': 'Kryptonite', 'artist': '3 Doors Down', 'genre': 'Rock'}

    monkeypatch.setattr(le.chorus_cache.chorus_client, 'search_by_artist_title', fake_search)
    _make_song(tmp_path, '3 Doors Down - Kryptonite')

    le.enrich_library(tmp_path)
    le.enrich_library(tmp_path, force=True)  # reprocessed, but same artist/title -> should hit cache

    assert len(calls) == 1


def test_enrich_library_corrupt_sidecar_is_ignored_not_raised(tmp_path, monkeypatch):
    _stub_chorus(monkeypatch, result=None)
    _make_song(tmp_path, '3 Doors Down - Kryptonite')
    (tmp_path / le.SIDECAR_FILENAME).write_text('{not valid json', encoding='utf-8')

    summary = le.enrich_library(tmp_path)  # must not raise
    assert summary['songs_processed'] == 1


def test_enrich_library_sidecar_write_failure_does_not_raise(tmp_path, monkeypatch):
    """A convenience sidecar must never cost the user the ability to run a
    scan -- matches _export_library_csv's own philosophy."""
    _stub_chorus(monkeypatch, result=None)
    _make_song(tmp_path, '3 Doors Down - Kryptonite')

    real_open = open
    def _deny_writes(path, mode='r', *a, **k):
        if 'w' in mode:
            raise OSError(13, 'Permission denied')
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr('builtins.open', _deny_writes)

    summary = le.enrich_library(tmp_path)  # must not raise
    assert summary['songs_processed'] == 1


def test_enrich_library_non_numeric_song_length_is_a_problem(tmp_path, monkeypatch):
    _stub_chorus(monkeypatch, result=None)
    _make_song(tmp_path, '3 Doors Down - Kryptonite',
               ini_text='[song]\nname = Kryptonite\nartist = 3 Doors Down\nsong_length = not_a_number\n')

    le.enrich_library(tmp_path)

    sidecar_path = tmp_path / le.SIDECAR_FILENAME
    with open(sidecar_path, encoding='utf-8') as f:
        sidecar = json.load(f)
    entry = next(iter(sidecar['songs'].values()))
    assert entry['song_length_ms'] is None
    assert any('song_length' in p for p in entry['problems'])


def test_enrich_library_chart_present_but_no_song_ini_is_a_problem_but_still_indexes(tmp_path, monkeypatch):
    _stub_chorus(monkeypatch, result=None)
    folder = tmp_path / 'No Ini'
    folder.mkdir()
    (folder / 'notes.chart').write_text(CHART_TEXT, encoding='utf-8')
    # No song.ini at all, but the chart is enough to compute a chart_hash.

    summary = le.enrich_library(tmp_path)
    assert summary['songs_processed'] == 1

    sidecar_path = tmp_path / le.SIDECAR_FILENAME
    with open(sidecar_path, encoding='utf-8') as f:
        sidecar = json.load(f)
    entry = next(iter(sidecar['songs'].values()))
    assert 'no song.ini found' in entry['problems']


def test_enrich_library_no_scores_available_is_none_not_zero(tmp_path, monkeypatch):
    """No ch_data_path given -> no scores available. Must be None (unknown),
    never a fabricated 0 (which would look like a real, terrible score)."""
    _stub_chorus(monkeypatch, result=None)
    _make_song(tmp_path, '3 Doors Down - Kryptonite')

    le.enrich_library(tmp_path)

    sidecar_path = tmp_path / le.SIDECAR_FILENAME
    with open(sidecar_path, encoding='utf-8') as f:
        sidecar = json.load(f)
    entry = next(iter(sidecar['songs'].values()))
    assert entry['high_score'] is None
    assert entry['score_detail'] is None


def test_enrich_library_high_score_is_max_across_scored_instruments(tmp_path, monkeypatch):
    """A song can have separate scores per instrument (lead/bass/etc). The
    booklet-facing high_score is the best of them; score_detail keeps the
    full per-instrument breakdown for anything that wants more than one
    number. Confirmed against a real installation -- see
    tests/test_library_scores.py's header comment."""
    _stub_chorus(monkeypatch, result=None)
    song = _make_song(tmp_path, '3 Doors Down - Kryptonite',
                       extra_files={'notes.mid': b'MThd fake midi bytes'})

    import library_scores
    mid_md5 = library_scores.notes_mid_md5(song)
    fake_scoredata = {
        mid_md5: {
            'plays': 3,
            'instruments': {
                'lead': {'difficulty': 'expert', 'percent_numerator': 95,
                          'percent_denominator': 100, 'stars': 5, 'score': 500000},
                'bass': {'difficulty': 'hard', 'percent_numerator': 80,
                         'percent_denominator': 100, 'stars': 3, 'score': 200000},
            },
        },
    }
    monkeypatch.setattr(le.library_scores, 'read_scoredata', lambda ch_data_path: fake_scoredata)

    le.enrich_library(tmp_path, ch_data_path='/fake/ch/data')

    sidecar_path = tmp_path / le.SIDECAR_FILENAME
    with open(sidecar_path, encoding='utf-8') as f:
        sidecar = json.load(f)
    entry = next(iter(sidecar['songs'].values()))
    assert entry['high_score'] == 500000
    assert entry['score_detail']['plays'] == 3
    assert entry['score_detail']['instruments']['lead']['score'] == 500000
    assert entry['score_detail']['instruments']['bass']['score'] == 200000
    assert 'high_score_streak' not in entry  # removed -- no such field exists in the real format
