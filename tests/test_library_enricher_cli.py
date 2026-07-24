# tests/test_library_enricher_cli.py
# Covers library_enricher.py's CLI (argument parsing, exit codes, and the
# interactive path-prompting UX) -- Task 2.2 of the library-enrichment plan.
# See tasks/plan-library-enrichment.md.
#
# main() takes an injectable input_fn (default: builtin input) so tests can
# drive the interactive prompts without blocking on real stdin -- same
# dependency-injection convention as chorus_cache's injectable time.time.

import library_enricher as cli


def _fake_input(*responses):
    """Returns an input_fn that yields each response in order, one per
    call -- raises if more prompts happen than responses given, so a test
    fails loudly instead of hanging if a prompt shows up unexpectedly."""
    it = iter(responses)
    def _input(prompt=''):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f'unexpected extra prompt: {prompt!r}')
    return _input


def test_parse_args_library_path_now_optional():
    """--library-path used to be required=True, which meant omitting it was
    an immediate argparse SystemExit before main() ever got a chance to
    prompt interactively. It must be optional now -- prompting only works
    if argparse lets a missing value through as None."""
    args = cli.parse_args([])
    assert args.library_path is None
    assert args.ch_data is None


def test_parse_args_all_flags():
    args = cli.parse_args([
        '--library-path', '/songs', '--dry-run', '--force',
        '--ch-data', '/ch/data', '--chorus-cache', '/cache.json', '-v',
    ])
    assert args.library_path == '/songs'
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
    exit_code = cli.main(['--library-path', str(tmp_path), '--ch-data', ''])
    assert exit_code == 0


def test_main_returns_one_when_library_path_does_not_exist(tmp_path):
    missing = tmp_path / 'does_not_exist'
    exit_code = cli.main(['--library-path', str(missing), '--ch-data', ''])
    assert exit_code == 1


def test_main_passes_flags_through_to_enrich_library(tmp_path, monkeypatch):
    captured = {}

    def fake_enrich_library(**kwargs):
        captured.update(kwargs)
        return {'songs_processed': 0, 'songs_skipped': 0, 'new_data_written': 0,
                'problems_found': 0, 'duration_seconds': 0.0}

    monkeypatch.setattr(cli, 'enrich_library', fake_enrich_library)
    cli.main(['--library-path', str(tmp_path), '--ch-data', '', '--dry-run', '--force', '-v'])

    assert captured['library_path'] == str(tmp_path)
    assert captured['dry_run'] is True
    assert captured['force'] is True
    assert captured['verbose'] is True


# --- interactive prompting --------------------------------------------------

def test_no_prompt_when_library_path_given_on_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'enrich_library', lambda **kwargs: {
        'songs_processed': 0, 'songs_skipped': 0, 'new_data_written': 0,
        'problems_found': 0, 'duration_seconds': 0.0,
    })
    # _fake_input() with zero responses -> any prompt call raises.
    exit_code = cli.main(['--library-path', str(tmp_path), '--ch-data', ''],
                          input_fn=_fake_input())
    assert exit_code == 0


def test_prompts_for_library_path_when_omitted(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'enrich_library', lambda **kwargs: {
        'songs_processed': 0, 'songs_skipped': 0, 'new_data_written': 0,
        'problems_found': 0, 'duration_seconds': 0.0,
    })
    exit_code = cli.main([], input_fn=_fake_input(str(tmp_path), ''))
    assert exit_code == 0


def test_library_path_prompt_shows_example_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'enrich_library', lambda **kwargs: {
        'songs_processed': 0, 'songs_skipped': 0, 'new_data_written': 0,
        'problems_found': 0, 'duration_seconds': 0.0,
    })
    cli.main([], input_fn=_fake_input(str(tmp_path), ''))
    out = capsys.readouterr().out
    # Real paths confirmed against an actual library this session -- not
    # placeholders, so a user has something concrete to pattern-match against.
    assert 'Clone Hero' in out
    assert 'Songs' in out


def test_library_path_prompt_retries_on_invalid_path_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'enrich_library', lambda **kwargs: {
        'songs_processed': 0, 'songs_skipped': 0, 'new_data_written': 0,
        'problems_found': 0, 'duration_seconds': 0.0,
    })
    bad = str(tmp_path / 'does_not_exist')
    exit_code = cli.main([], input_fn=_fake_input(bad, str(tmp_path), ''))
    assert exit_code == 0


def test_library_path_prompt_gives_up_after_max_retries(tmp_path):
    bad = str(tmp_path / 'does_not_exist')
    exit_code = cli.main([], input_fn=_fake_input(bad, bad, bad))
    assert exit_code == 1


def test_prompts_for_ch_data_when_omitted_blank_means_skip(tmp_path, monkeypatch):
    captured = {}

    def fake_enrich_library(**kwargs):
        captured.update(kwargs)
        return {'songs_processed': 0, 'songs_skipped': 0, 'new_data_written': 0,
                'problems_found': 0, 'duration_seconds': 0.0}

    monkeypatch.setattr(cli, 'enrich_library', fake_enrich_library)
    cli.main(['--library-path', str(tmp_path)], input_fn=_fake_input(''))
    assert captured['ch_data_path'] is None


def test_ch_data_prompt_shows_example_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'enrich_library', lambda **kwargs: {
        'songs_processed': 0, 'songs_skipped': 0, 'new_data_written': 0,
        'problems_found': 0, 'duration_seconds': 0.0,
    })
    cli.main(['--library-path', str(tmp_path)], input_fn=_fake_input(''))
    out = capsys.readouterr().out
    # Confirmed real Unity persistentDataPath for Clone Hero (see
    # SPEC-library-enrichment.md), not a guess.
    assert 'LocalLow' in out
    assert 'srylain' in out


def test_ch_data_given_on_cli_skips_its_prompt_but_library_path_still_prompts(tmp_path, monkeypatch):
    captured = {}

    def fake_enrich_library(**kwargs):
        captured.update(kwargs)
        return {'songs_processed': 0, 'songs_skipped': 0, 'new_data_written': 0,
                'problems_found': 0, 'duration_seconds': 0.0}

    monkeypatch.setattr(cli, 'enrich_library', fake_enrich_library)
    cli.main(['--ch-data', '/ch/data'], input_fn=_fake_input(str(tmp_path)))
    assert captured['library_path'] == str(tmp_path)
    assert captured['ch_data_path'] == '/ch/data'
