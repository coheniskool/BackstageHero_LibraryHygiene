# tests/test_library_enricher_integration.py
# End-to-end integration coverage for the library-enrichment tool -- Task
# 3.3 of tasks/plan-library-enrichment.md.
#
# Runs the real CLI entry point (library_enricher.main()) against a
# synthetic multi-song library on disk -- real file I/O, real chart
# parsing, real hashing, real sidecar write, nothing mocked except the
# Chorus network call (which cannot run in a test). This is the part of
# Task 3.3 that doesn't depend on the user's own populated Clone Hero
# library: the "validate against a real installation's scoredata.bin /
# real known scores" half stays blocked on that (see
# tasks/todo-library-enrichment.md, Task 1.2 spike notes) and is NOT
# covered here.

import json

import library_enricher
import library_enrichment

CHART_WITH_SOLO_AND_2X_KICK = (
    '[Song]\n{\n  Name = "Kryptonite"\n  Artist = "3 Doors Down"\n  Resolution = 192\n}\n'
    '[SyncTrack]\n{\n  0 = TS 4\n  0 = B 140000\n}\n'
    '[ExpertSingle]\n{\n  0 = N 0 0\n  0 = E solo\n  192 = N 1 0\n  384 = N 2 0\n  '
    '384 = E soloend\n  576 = N 7 0\n}\n'
    '[ExpertDrums]\n{\n  0 = N 0 0\n  192 = N 1 0\n  384 = N 32 0\n}\n'
)

CHART_SIMPLE = (
    '[Song]\n{\n  Name = "Mr. Roboto"\n  Artist = "Styx"\n  Resolution = 192\n}\n'
    '[ExpertSingle]\n{\n  0 = N 0 0\n  192 = N 1 0\n}\n'
)


def _build_test_library(root):
    """A small but structurally real multi-song library: two full songs
    (chart + ini + stems + album art) and one deliberately incomplete song
    (ini only, no chart at all) to exercise the problems-not-crashes path."""
    kryptonite = root / '3 Doors Down - Kryptonite'
    kryptonite.mkdir()
    (kryptonite / 'song.ini').write_text(
        '[song]\nname = Kryptonite\nartist = 3 Doors Down\nsong_length = 199000\n',
        encoding='utf-8')
    (kryptonite / 'notes.chart').write_text(CHART_WITH_SOLO_AND_2X_KICK, encoding='utf-8')
    (kryptonite / 'guitar.ogg').write_bytes(b'fake guitar stem')
    (kryptonite / 'drums.ogg').write_bytes(b'fake drums stem')
    (kryptonite / 'album.png').write_bytes(b'fake album art')

    roboto = root / 'Styx - Mr. Roboto'
    roboto.mkdir()
    (roboto / 'song.ini').write_text(
        '[song]\nname = Mr. Roboto\nartist = Styx\nsong_length = 187000\n', encoding='utf-8')
    (roboto / 'notes.chart').write_text(CHART_SIMPLE, encoding='utf-8')

    incomplete = root / 'Someone - Unfinished Chart'
    incomplete.mkdir()
    (incomplete / 'song.ini').write_text(
        '[song]\nname = Unfinished Chart\nartist = Someone\n', encoding='utf-8')
    # No notes.chart/notes.mid/notes.eof at all -- can't be identified or indexed.

    return kryptonite, roboto, incomplete


def test_full_cli_run_against_a_real_synthetic_library(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(library_enrichment.chorus_cache.chorus_client,
                         'search_by_artist_title', lambda artist, title: None)
    kryptonite, roboto, incomplete = _build_test_library(tmp_path)

    exit_code = library_enricher.main(['--library-path', str(tmp_path), '--ch-data', '', '-v'])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert 'Enrichment complete' in out
    assert '2 processed' in out  # kryptonite + roboto; the incomplete song isn't indexable
    assert '1 problem' in out    # the unidentifiable folder

    sidecar_path = tmp_path / library_enrichment.SIDECAR_FILENAME
    assert sidecar_path.exists()
    with open(sidecar_path, encoding='utf-8') as f:
        sidecar = json.load(f)

    assert sidecar['version'] == 1
    assert len(sidecar['songs']) == 2

    import resolver_client
    kryptonite_entry = sidecar['songs'][resolver_client.chart_hash(str(kryptonite))]
    assert kryptonite_entry['instruments']['guitar'] == 1
    assert kryptonite_entry['instruments']['drums'] == 1
    assert kryptonite_entry['instruments']['bass'] == -1
    assert kryptonite_entry['features']['has_solos'] is True
    assert kryptonite_entry['features']['has_open_notes'] is True
    assert kryptonite_entry['features']['has_2x_kick'] is True
    assert kryptonite_entry['song_length_ms'] == 199000
    assert sorted(kryptonite_entry['stems']) == ['drums.ogg', 'guitar.ogg']
    assert kryptonite_entry['has_album_art'] is True
    assert kryptonite_entry['problems'] == []

    roboto_entry = sidecar['songs'][resolver_client.chart_hash(str(roboto))]
    assert roboto_entry['instruments']['guitar'] == 1
    assert roboto_entry['has_album_art'] is False
    assert roboto_entry['stems'] == []


def test_full_cli_run_dry_run_leaves_no_trace(tmp_path, monkeypatch):
    monkeypatch.setattr(library_enrichment.chorus_cache.chorus_client,
                         'search_by_artist_title', lambda artist, title: None)
    _build_test_library(tmp_path)

    exit_code = library_enricher.main(['--library-path', str(tmp_path), '--ch-data', '', '--dry-run'])
    assert exit_code == 0
    assert not (tmp_path / library_enrichment.SIDECAR_FILENAME).exists()


def test_second_run_is_incremental_third_run_with_force_reprocesses(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(library_enrichment.chorus_cache.chorus_client,
                         'search_by_artist_title', lambda artist, title: None)
    _build_test_library(tmp_path)

    library_enricher.main(['--library-path', str(tmp_path), '--ch-data', ''])
    capsys.readouterr()  # discard first run's output

    library_enricher.main(['--library-path', str(tmp_path), '--ch-data', ''])
    second_out = capsys.readouterr().out
    assert '0 processed' in second_out
    assert '2 skipped' in second_out

    library_enricher.main(['--library-path', str(tmp_path), '--ch-data', '', '--force'])
    third_out = capsys.readouterr().out
    assert '2 processed' in third_out
    assert '0 skipped' in third_out
