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


def test_enrich_library_dry_run_writes_nothing(tmp_path, monkeypatch):
    _stub_chorus(monkeypatch, result=None)
    _make_song(tmp_path, '3 Doors Down - Kryptonite')

    summary = le.enrich_library(tmp_path, dry_run=True)

    assert summary['songs_processed'] == 1
    assert not (tmp_path / le.SIDECAR_FILENAME).exists()


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
