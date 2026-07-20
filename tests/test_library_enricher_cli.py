# tests/test_library_enricher_cli.py
# Covers library_enricher.py's CLI (argument parsing, exit codes) --
# Task 2.2 of the library-enrichment plan. See tasks/plan-library-enrichment.md.

import library_enricher as cli


def test_parse_args_requires_library_path():
    args = cli.parse_args(['--library-path', '/some/songs'])
    assert args.library_path == '/some/songs'
    assert args.dry_run is False
    assert args.force is False
    assert args.ch_data is None
    assert args.chorus_cache is None
    assert args.verbose is False


def test_parse_args_all_flags():
    args = cli.parse_args([
        '--library-path', '/songs', '--dry-run', '--force',
        '--ch-data', '/ch/data', '--chorus-cache', '/cache.json', '-v',
    ])
    assert args.dry_run is True
    assert args.force is True
    assert args.ch_data == '/ch/data'
    assert args.chorus_cache == '/cache.json'
    assert args.verbose is True


def test_main_returns_zero_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'enrich_library', lambda **kwargs: {
        'songs_processed': 1, 'songs_skipped': 0, 'new_data_written': 1,
        'problems_found': 0, 'duration_seconds': 0.1,
    })
    exit_code = cli.main(['--library-path', str(tmp_path)])
    assert exit_code == 0


def test_main_returns_one_when_library_path_does_not_exist(tmp_path):
    missing = tmp_path / 'does_not_exist'
    exit_code = cli.main(['--library-path', str(missing)])
    assert exit_code == 1


def test_main_passes_flags_through_to_enrich_library(tmp_path, monkeypatch):
    captured = {}

    def fake_enrich_library(**kwargs):
        captured.update(kwargs)
        return {'songs_processed': 0, 'songs_skipped': 0, 'new_data_written': 0,
                'problems_found': 0, 'duration_seconds': 0.0}

    monkeypatch.setattr(cli, 'enrich_library', fake_enrich_library)
    cli.main(['--library-path', str(tmp_path), '--dry-run', '--force', '-v'])

    assert captured['library_path'] == str(tmp_path)
    assert captured['dry_run'] is True
    assert captured['force'] is True
    assert captured['verbose'] is True
