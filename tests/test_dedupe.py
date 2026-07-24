import json
from pathlib import Path

import chart_rename
import dedupe_report as dr


def _make_song(root, name, ini_text='[song]\nname = Test\nartist = Test Artist\n'):
    folder = root / name
    folder.mkdir()
    (folder / 'song.ini').write_text(ini_text, encoding='utf-8')
    return folder


class _FakeAcoustidMatching:
    @staticmethod
    def fingerprint_file(path):
        return (180, 'FAKEPRINT')

    @staticmethod
    def compare_fingerprints(fp1, fp2):
        return 0.99


# --- _clean_folder_name / _version_tag --------------------------------------------------

def test_clean_folder_name_strips_dup_suffix():
    assert dr._clean_folder_name('Weezer - My Name Is Jonas [dup3]') == 'Weezer - My Name Is Jonas'


def test_version_tag_detects_live():
    assert dr._version_tag('Kryptonite (Live)') == 'live'


def test_version_tag_none_when_absent():
    assert dr._version_tag('Kryptonite') is None


# --- group_candidates --------------------------------------------------

def test_group_candidates_groups_fuzzy_matching_folders(tmp_path):
    a = _make_song(tmp_path, '3 Doors Down - Kryptonite')
    b = _make_song(tmp_path, '3 Doors Down - Kryptonite [dup2]')
    groups = dr.group_candidates([a, b])
    assert len(groups) == 1
    assert set(groups[0]) == {a, b}


def test_group_candidates_never_groups_across_version_tags(tmp_path):
    studio = _make_song(tmp_path, '3 Doors Down - Kryptonite')
    live = _make_song(tmp_path, '3 Doors Down - Kryptonite (Live)')
    assert dr.group_candidates([studio, live]) == []


def test_group_candidates_no_group_for_unrelated_songs(tmp_path):
    a = _make_song(tmp_path, '3 Doors Down - Kryptonite')
    b = _make_song(tmp_path, 'Styx - Mr. Roboto')
    assert dr.group_candidates([a, b]) == []


def test_group_candidates_matches_across_adjacent_length_buckets(tmp_path):
    """Boundary test for the blocking-key optimization (SPEC finding C16).

    A real near-duplicate pair whose normalized-title lengths straddle a
    length-bucket boundary must still be grouped. Here a dropped leading
    character turns 'my name is jonas' (len 16, bucket 16//4 = 4) into
    'y name is jonas' (len 15, bucket 15//4 = 3): the two titles are ~97%
    similar (SequenceMatcher) with an identical artist, yet a NAIVE blocking
    scheme would silently drop them --

      * disjoint length buckets: 4 != 3, so they never get compared;
      * a first-1-2-char prefix key: 'my' != 'y ', likewise dropped.

    The chosen strategy (compare EQUAL-OR-ADJACENT buckets) keeps them
    together because |4 - 3| == 1. This proves the bucketing does not lose a
    genuine boundary-straddling duplicate. If LENGTH_BUCKET_WIDTH or the
    adjacency tolerance were ever tightened past this pair, this test fails.
    """
    keeper = _make_song(tmp_path, 'Weezer - My Name Is Jonas')
    typo = _make_song(tmp_path, 'Weezer - y Name Is Jonas')  # dropped leading 'M'

    # Guard: confirm the pair really does land in two DIFFERENT disjoint
    # buckets, so the test exercises the adjacency path rather than passing
    # trivially because both titles share a bucket.
    import library_common as lc
    len_keeper = len(lc.normalize_lookup_value('My Name Is Jonas'))
    len_typo = len(lc.normalize_lookup_value('y Name Is Jonas'))
    assert (len_keeper // dr.LENGTH_BUCKET_WIDTH) != (len_typo // dr.LENGTH_BUCKET_WIDTH)

    groups = dr.group_candidates([keeper, typo])
    assert len(groups) == 1
    assert set(groups[0]) == {keeper, typo}


# --- confirm_group --------------------------------------------------

def test_confirm_group_returns_empty_when_acoustid_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, 'acoustid', None)
    a = _make_song(tmp_path, 'Song A')
    (a / 'song.ogg').write_bytes(b'x')
    assert dr.confirm_group([a]) == []


def test_confirm_group_confirms_matching_fingerprints(tmp_path, monkeypatch):
    a = _make_song(tmp_path, 'Song A')
    b = _make_song(tmp_path, 'Song B')
    (a / 'song.ogg').write_bytes(b'x')
    (b / 'song.ogg').write_bytes(b'x')

    monkeypatch.setattr(dr, 'acoustid', _FakeAcoustidMatching())

    assert set(dr.confirm_group([a, b])) == {a, b}


def test_confirm_group_rejects_dissimilar_fingerprints(tmp_path, monkeypatch):
    a = _make_song(tmp_path, 'Song A')
    b = _make_song(tmp_path, 'Song B')
    (a / 'song.ogg').write_bytes(b'x')
    (b / 'song.ogg').write_bytes(b'x')

    class _FakeAcoustidMismatch:
        @staticmethod
        def fingerprint_file(path):
            return (180, 'FAKEPRINT')

        @staticmethod
        def compare_fingerprints(fp1, fp2):
            return 0.1  # different recordings

    monkeypatch.setattr(dr, 'acoustid', _FakeAcoustidMismatch())

    assert dr.confirm_group([a, b]) == []


def test_confirm_group_empty_when_fewer_than_two_have_audio(tmp_path, monkeypatch):
    a = _make_song(tmp_path, 'Song A')
    (a / 'song.ogg').write_bytes(b'x')
    b = _make_song(tmp_path, 'Song B')  # no audio file

    monkeypatch.setattr(dr, 'acoustid', _FakeAcoustidMatching())

    assert dr.confirm_group([a, b]) == []


# --- score_folder --------------------------------------------------

def test_score_folder_weights_instrument_completeness_heavily():
    video_meta = {'video_status': 'no_video'}
    rich_fields = {'diff_guitar': 3, 'diff_bass': 2, 'diff_drums': 4}
    sparse_fields = {'diff_guitar': 3}
    rich_score, _ = dr.score_folder(None, video_meta, rich_fields, None)
    sparse_score, _ = dr.score_folder(None, video_meta, sparse_fields, None)
    assert rich_score > sparse_score


def test_score_folder_video_presence_bonus():
    with_video, _ = dr.score_folder(None, {'video_status': 'present'}, {}, None)
    without_video, _ = dr.score_folder(None, {'video_status': 'no_video'}, {}, None)
    assert with_video == without_video + 10


def test_score_folder_chorus_signal_bonus():
    clean_chorus = {'folderIssues': None, 'metadataIssues': None}
    with_signal, _ = dr.score_folder(None, {'video_status': 'no_video'}, {}, clean_chorus)
    without_signal, _ = dr.score_folder(None, {'video_status': 'no_video'}, {}, None)
    assert with_signal == without_signal + 5


def test_score_folder_string_minus_one_is_not_charted():
    """Regression: ini fields arrive as STRINGS, and real song.ini files list
    uncharted instruments explicitly as 'diff_bass = -1'. The original
    (inherited) comparison `fields.get(key, -1) != -1` compared "-1" != -1
    and counted every explicitly-uncharted instrument as charted."""
    video_meta = {'video_status': 'no_video'}
    fields = {'diff_guitar': '3', 'diff_bass': '-1', 'diff_drums': '-1'}
    score, breakdown = dr.score_folder(None, video_meta, fields, None)
    assert breakdown['instrument_count'] == 5  # only diff_guitar counts


def test_score_folder_string_values_via_real_ini_read(tmp_path):
    """Same regression through the real read path (_build_score_inputs),
    so string-typed values from an actual song.ini are what gets scored."""
    folder = _make_song(
        tmp_path, 'Real Shaped Song',
        '[song]\nname = Test\nartist = Test Artist\n'
        'diff_guitar = 4\ndiff_bass = -1\ndiff_drums = -1\ndiff_keys = -1\n')
    video_meta, ini_fields = dr._build_score_inputs(folder)
    score, breakdown = dr.score_folder(folder, video_meta, ini_fields, None)
    assert breakdown['instrument_count'] == 5  # only diff_guitar, not the three -1 entries


def test_score_folder_unparseable_diff_value_not_charted():
    _, breakdown = dr.score_folder(
        None, {'video_status': 'no_video'}, {'diff_guitar': 'garbage'}, None)
    assert breakdown['instrument_count'] == 0


def test_flag_borrow_candidates_string_minus_one_never_flags():
    """Regression companion: a loser whose diff_drums = "-1" (string) must
    not generate a false 'loser has it' borrow flag."""
    flags = dr.flag_borrow_candidates(
        keeper_ini_fields={'diff_guitar': '3'},
        keeper_video_meta={'video_status': 'no_video'},
        loser_ini_fields={'diff_guitar': '3', 'diff_drums': '-1'},
        loser_video_meta={'video_status': 'no_video'},
    )
    assert flags == []


# --- is_keeper_eligible / select_keeper --------------------------------------------------

def test_is_keeper_eligible_true_only_for_confirmed_ok():
    assert dr.is_keeper_eligible({'chart_rename_status': 'confirmed_ok'}) is True
    assert dr.is_keeper_eligible({'chart_rename_status': 'needs_review'}) is False
    assert dr.is_keeper_eligible({}) is False  # absence treated as not-eligible


def test_select_keeper_picks_highest_score_among_eligible():
    a, b = Path('a'), Path('b')
    scores = {a: 10, b: 50}
    eligibility = {a: True, b: True}
    assert dr.select_keeper([a, b], scores, eligibility) == b


def test_select_keeper_never_returns_ineligible_folder_even_if_highest_scoring():
    a, b = Path('a'), Path('b')
    scores = {a: 10, b: 50}
    eligibility = {a: True, b: False}  # b scores highest but is ineligible
    assert dr.select_keeper([a, b], scores, eligibility) == a


def test_select_keeper_none_when_all_ineligible():
    a, b = Path('a'), Path('b')
    scores = {a: 10, b: 50}
    eligibility = {a: False, b: False}
    assert dr.select_keeper([a, b], scores, eligibility) is None


# --- flag_borrow_candidates --------------------------------------------------

def test_flag_borrow_candidates_flags_missing_instrument():
    flags = dr.flag_borrow_candidates(
        keeper_ini_fields={'diff_guitar': 3},
        keeper_video_meta={'video_status': 'no_video'},
        loser_ini_fields={'diff_guitar': 3, 'diff_drums': 2},
        loser_video_meta={'video_status': 'no_video'},
    )
    assert any('diff_drums' in f for f in flags)


def test_flag_borrow_candidates_flags_missing_video():
    flags = dr.flag_borrow_candidates(
        keeper_ini_fields={}, keeper_video_meta={'video_status': 'no_video'},
        loser_ini_fields={}, loser_video_meta={'video_status': 'present'},
    )
    assert any('video background' in f for f in flags)


def test_flag_borrow_candidates_empty_when_keeper_has_everything():
    flags = dr.flag_borrow_candidates(
        keeper_ini_fields={'diff_guitar': 3}, keeper_video_meta={'video_status': 'present'},
        loser_ini_fields={'diff_guitar': 3}, loser_video_meta={'video_status': 'no_video'},
    )
    assert flags == []


# --- generate_dedupe_report (integration) --------------------------------------------------

def test_generate_dedupe_report_relocates_lower_scoring_duplicate(tmp_path, monkeypatch):
    home = tmp_path / 'Library'
    home.mkdir()
    keeper = _make_song(home, '3 Doors Down - Kryptonite',
                         '[song]\nname = Kryptonite\nartist = 3 Doors Down\ndiff_guitar = 3\n')
    loser = _make_song(home, '3 Doors Down - Kryptonite [dup2]',
                        '[song]\nname = Kryptonite\nartist = 3 Doors Down\n')
    (keeper / 'song.ogg').write_bytes(b'x')
    (loser / 'song.ogg').write_bytes(b'x')

    # both folders must be keeper-eligible for select_keeper to pick one
    chart_rename.save_chart_rename_status(keeper, 'confirmed_ok')
    chart_rename.save_chart_rename_status(loser, 'confirmed_ok')

    monkeypatch.setattr(dr, 'acoustid', _FakeAcoustidMatching())
    monkeypatch.setattr(dr.chorus_client, 'search_by_artist_title', lambda artist, title: None)

    result = dr.generate_dedupe_report(home)

    assert keeper.exists()
    assert not loser.exists()
    # relocated OUTSIDE home (a sibling) -- see library_common.move_to_review
    assert (tmp_path / 'Library_duplicates_review' / '3 Doors Down - Kryptonite [dup2]').exists()
    assert result == {'candidate_groups': 1, 'resolved': 1,
                       'skipped_all_ineligible': 0, 'skipped_not_confirmed': 0}

    manifest = tmp_path / 'Library_duplicates_review_manifest.jsonl'
    entry = json.loads(manifest.read_text(encoding='utf-8').strip())
    assert 'score' in entry


def test_generate_dedupe_report_skips_group_when_all_ineligible(tmp_path, monkeypatch):
    home = tmp_path
    a = _make_song(home, '3 Doors Down - Kryptonite',
                   '[song]\nname = Kryptonite\nartist = 3 Doors Down\n')
    b = _make_song(home, '3 Doors Down - Kryptonite [dup2]',
                   '[song]\nname = Kryptonite\nartist = 3 Doors Down\n')
    (a / 'song.ogg').write_bytes(b'x')
    (b / 'song.ogg').write_bytes(b'x')
    # neither folder has a confirmed_ok chart_rename_status

    monkeypatch.setattr(dr, 'acoustid', _FakeAcoustidMatching())
    monkeypatch.setattr(dr.chorus_client, 'search_by_artist_title', lambda artist, title: None)

    dr.generate_dedupe_report(home)

    assert a.exists()
    assert b.exists()  # nothing relocated, both stayed put


def test_generate_dedupe_report_dry_run_relocates_nothing(tmp_path, monkeypatch):
    home = tmp_path
    keeper = _make_song(home, '3 Doors Down - Kryptonite',
                         '[song]\nname = Kryptonite\nartist = 3 Doors Down\ndiff_guitar = 3\n')
    loser = _make_song(home, '3 Doors Down - Kryptonite [dup2]',
                        '[song]\nname = Kryptonite\nartist = 3 Doors Down\n')
    (keeper / 'song.ogg').write_bytes(b'x')
    (loser / 'song.ogg').write_bytes(b'x')
    chart_rename.save_chart_rename_status(keeper, 'confirmed_ok')
    chart_rename.save_chart_rename_status(loser, 'confirmed_ok')

    monkeypatch.setattr(dr, 'acoustid', _FakeAcoustidMatching())
    monkeypatch.setattr(dr.chorus_client, 'search_by_artist_title', lambda artist, title: None)

    dr.generate_dedupe_report(home, dry_run=True)

    assert keeper.exists()
    assert loser.exists()  # nothing moved in dry-run
    assert not (home / '_duplicates_review').exists()
