# tests/test_library_chart_parser.py
# Covers library_chart_parser's notes.chart parsing -- Task 1.1 of the
# library-enrichment plan. See tasks/plan-library-enrichment.md.

import library_chart_parser as lcp

CHART_WITH_GUITAR_AND_BASS = (
    '[Song]\n{\n  Name = "Test Song"\n  Artist = "Test Artist"\n  Resolution = 192\n}\n'
    '[SyncTrack]\n{\n  0 = TS 4\n  0 = B 120000\n}\n'
    '[Events]\n{\n}\n'
    '[ExpertSingle]\n{\n  0 = N 0 0\n  192 = N 1 0\n  384 = N 2 0\n}\n'
    '[ExpertDoubleBass]\n{\n  0 = N 0 0\n}\n'
)

CHART_EMPTY_DIFFICULTY_SECTION = (
    '[Song]\n{\n  Name = "Test Song"\n  Artist = "Test Artist"\n}\n'
    '[ExpertDrums]\n{\n}\n'
)

CHART_NO_SECTIONS = '[Song]\n{\n  Name = "Test Song"\n}\n'

ALL_ABSENT = {
    'guitar': -1, 'bass': -1, 'drums': -1, 'keys': -1,
    'vocals': -1, 'rhythm': -1, 'guitarghl': -1,
}


def test_parse_chart_instruments_detects_guitar_and_bass(tmp_path):
    chart = tmp_path / 'notes.chart'
    chart.write_text(CHART_WITH_GUITAR_AND_BASS, encoding='utf-8')
    result = lcp.parse_chart_instruments(chart)
    assert result['guitar'] == 1
    assert result['bass'] == 1
    assert result['drums'] == -1
    assert result['keys'] == -1
    assert result['rhythm'] == -1
    assert result['guitarghl'] == -1


def test_parse_chart_instruments_vocals_always_absent():
    """A .chart file has no vocals track -- CH reads vocals from song.ini/.mid,
    never from .chart. This parser must never claim to verify vocals."""
    result = lcp.parse_chart_instruments_from_text(CHART_WITH_GUITAR_AND_BASS)
    assert result['vocals'] == -1


def test_parse_chart_instruments_empty_section_counts_as_absent(tmp_path):
    """A section header with zero N lines inside (a charter left it blank)
    must not count as a playable instrument."""
    chart = tmp_path / 'notes.chart'
    chart.write_text(CHART_EMPTY_DIFFICULTY_SECTION, encoding='utf-8')
    result = lcp.parse_chart_instruments(chart)
    assert result['drums'] == -1


def test_parse_chart_instruments_no_note_sections(tmp_path):
    chart = tmp_path / 'notes.chart'
    chart.write_text(CHART_NO_SECTIONS, encoding='utf-8')
    assert lcp.parse_chart_instruments(chart) == ALL_ABSENT


def test_parse_chart_instruments_missing_file_returns_all_absent(tmp_path):
    """No crash, no exception -- a missing chart is a safe default, not an error
    the caller must guard against."""
    missing = tmp_path / 'does_not_exist.chart'
    assert lcp.parse_chart_instruments(missing) == ALL_ABSENT


def test_parse_chart_instruments_multiple_difficulties_same_instrument(tmp_path):
    """Notes on any one difficulty is enough to mark the instrument present --
    booklet consumers care whether the part exists, not which tiers do."""
    text = (
        '[Song]\n{\n  Name = "Test"\n}\n'
        '[EasySingle]\n{\n  0 = N 0 0\n}\n'
        '[MediumSingle]\n{\n}\n'
    )
    result = lcp.parse_chart_instruments_from_text(text)
    assert result['guitar'] == 1


# --- parse_chart_nps --------------------------------------------------
#
# avg_nps is defined as (note_count - 1) / (last_note_time - first_note_time)
# over the chosen instrument's Expert difficulty -- an average rate between
# the first and last note, the standard "notes per second" meaning. Chords
# (multiple simultaneous notes on one tick) each count individually.
#
# Guitar Expert is used when present; otherwise the priority order in
# _EXPERT_SECTION_PRIORITY is tried. Only Expert is considered -- a
# deliberate v1 simplification (see module docstring), not an omission.

CHART_CONSTANT_BPM = (
    '[Song]\n{\n  Name = "Test"\n  Resolution = 192\n}\n'
    '[SyncTrack]\n{\n  0 = TS 4\n  0 = B 120000\n}\n'
    '[ExpertSingle]\n{\n  0 = N 0 0\n  192 = N 1 0\n  384 = N 2 0\n  576 = N 0 0\n}\n'
)

CHART_BPM_CHANGE = (
    '[Song]\n{\n  Name = "Test"\n  Resolution = 192\n}\n'
    '[SyncTrack]\n{\n  0 = TS 4\n  0 = B 120000\n  384 = B 240000\n}\n'
    '[ExpertSingle]\n{\n  0 = N 0 0\n  192 = N 1 0\n  384 = N 2 0\n  576 = N 0 0\n}\n'
)


def test_parse_chart_nps_constant_bpm():
    # 4 notes at 192-tick spacing, 120 BPM, resolution 192 -> 0.5s/note ->
    # span 0 to 1.5s, (4-1)/1.5 = 2.0 NPS.
    assert lcp.parse_chart_nps_from_text(CHART_CONSTANT_BPM) == 2.0


def test_parse_chart_nps_integrates_across_bpm_change():
    # First 2 ticks (0->384) at 120 BPM = 1.0s; next 192 ticks (384->576) at
    # 240 BPM = 0.25s. Total span 1.25s, (4-1)/1.25 = 2.4 NPS.
    assert lcp.parse_chart_nps_from_text(CHART_BPM_CHANGE) == 2.4


def test_parse_chart_nps_none_when_fewer_than_two_notes():
    text = (
        '[Song]\n{\n  Name = "Test"\n}\n'
        '[ExpertSingle]\n{\n  0 = N 0 0\n}\n'
    )
    assert lcp.parse_chart_nps_from_text(text) is None


def test_parse_chart_nps_none_when_no_expert_section():
    text = '[Song]\n{\n  Name = "Test"\n}\n'
    assert lcp.parse_chart_nps_from_text(text) is None


def test_parse_chart_nps_missing_file_returns_none(tmp_path):
    missing = tmp_path / 'does_not_exist.chart'
    assert lcp.parse_chart_nps(missing) is None


def test_parse_chart_nps_reads_from_file(tmp_path):
    chart = tmp_path / 'notes.chart'
    chart.write_text(CHART_CONSTANT_BPM, encoding='utf-8')
    assert lcp.parse_chart_nps(chart) == 2.0


# --- parse_chart_features --------------------------------------------------
#
# Grounded in TheNathannator/GuitarGame_ChartFormats' documented .chart
# format (docs/Chart-File-Formats/chart-format/Tracks/{5-Fret-Guitar,Drums,
# Lyrics}.md), not guessed:
#   - solos are the *local events* `E solo` / `E soloend`, NOT `S 2` --
#     `S 2` is Star Power and would be a wrong-by-one-letter bug if used here.
#   - open notes are note type 7, on 5-fret guitar-family sections only.
#   - 2x kick is note type 32, on Drums sections only.
#   - roll lanes are special-phrase types 65 (single) / 66 (double), on
#     Drums sections only.
#   - lyrics are `E "lyric <word>"` / `E lyric <word>` events in [Events].

NO_FEATURES = {
    'has_lyrics': False, 'has_solos': False,
    'has_open_notes': False, 'has_2x_kick': False, 'has_roll_lanes': False,
}


def test_parse_chart_features_detects_solo_event_not_star_power():
    text = (
        '[Song]\n{\n  Name = "Test"\n}\n'
        '[ExpertSingle]\n{\n  0 = N 0 0\n  0 = S 2 192\n}\n'
    )
    # S 2 alone (Star Power) must NOT be mistaken for a solo.
    assert lcp.parse_chart_features_from_text(text)['has_solos'] is False

    text_with_solo = (
        '[Song]\n{\n  Name = "Test"\n}\n'
        '[ExpertSingle]\n{\n  0 = N 0 0\n  0 = E solo\n  192 = N 1 0\n  192 = E soloend\n}\n'
    )
    assert lcp.parse_chart_features_from_text(text_with_solo)['has_solos'] is True


def test_parse_chart_features_solo_event_survives_soloend_substring():
    """'soloend' contains 'solo' as a substring -- a naive search must not
    let it register as an (unstarted) solo on its own."""
    text = (
        '[Song]\n{\n  Name = "Test"\n}\n'
        '[ExpertSingle]\n{\n  0 = N 0 0\n  192 = E soloend\n}\n'
    )
    assert lcp.parse_chart_features_from_text(text)['has_solos'] is False


def test_parse_chart_features_detects_open_note_on_guitar():
    text = (
        '[Song]\n{\n  Name = "Test"\n}\n'
        '[ExpertSingle]\n{\n  0 = N 0 0\n  192 = N 7 0\n}\n'
    )
    assert lcp.parse_chart_features_from_text(text)['has_open_notes'] is True


def test_parse_chart_features_detects_2x_kick_on_drums():
    text = (
        '[Song]\n{\n  Name = "Test"\n}\n'
        '[ExpertDrums]\n{\n  0 = N 0 0\n  192 = N 32 0\n}\n'
    )
    assert lcp.parse_chart_features_from_text(text)['has_2x_kick'] is True


def test_parse_chart_features_detects_roll_lanes_on_drums():
    text = (
        '[Song]\n{\n  Name = "Test"\n}\n'
        '[ExpertDrums]\n{\n  0 = N 1 0\n  0 = S 65 768\n}\n'
    )
    assert lcp.parse_chart_features_from_text(text)['has_roll_lanes'] is True

    text_double = (
        '[Song]\n{\n  Name = "Test"\n}\n'
        '[ExpertDrums]\n{\n  0 = N 1 0\n  0 = S 66 768\n}\n'
    )
    assert lcp.parse_chart_features_from_text(text_double)['has_roll_lanes'] is True


def test_parse_chart_features_detects_lyrics():
    text = (
        '[Song]\n{\n  Name = "Test"\n}\n'
        '[Events]\n{\n  576 = E "phrase_start"\n  768 = E "lyric This"\n  '
        '1536 = E "phrase_end"\n}\n'
    )
    assert lcp.parse_chart_features_from_text(text)['has_lyrics'] is True


def test_parse_chart_features_none_present(tmp_path):
    chart = tmp_path / 'notes.chart'
    chart.write_text(CHART_WITH_GUITAR_AND_BASS, encoding='utf-8')
    assert lcp.parse_chart_features(chart) == NO_FEATURES


def test_parse_chart_features_missing_file_returns_no_features(tmp_path):
    missing = tmp_path / 'does_not_exist.chart'
    assert lcp.parse_chart_features(missing) == NO_FEATURES


# --- parse_chart_note_count --------------------------------------------------
#
# Same Expert-section selection as parse_chart_nps (see its comment above) --
# note_count and avg_nps must describe the same track to be meaningful
# together in the sidecar/booklet ("1247 notes, 7.3 NPS").

def test_parse_chart_note_count_counts_all_note_lines_including_chords():
    text = (
        '[Song]\n{\n  Name = "Test"\n}\n'
        '[ExpertSingle]\n{\n  0 = N 0 0\n  0 = N 1 0\n  192 = N 2 0\n}\n'
    )
    # Tick 0 has a 2-note chord + one more note at 192 = 3 total notes.
    assert lcp.parse_chart_note_count_from_text(text) == 3


def test_parse_chart_note_count_matches_nps_selected_section():
    assert lcp.parse_chart_note_count_from_text(CHART_CONSTANT_BPM) == 4


def test_parse_chart_note_count_none_when_no_expert_section():
    text = '[Song]\n{\n  Name = "Test"\n}\n'
    assert lcp.parse_chart_note_count_from_text(text) is None


def test_parse_chart_note_count_reads_from_file(tmp_path):
    chart = tmp_path / 'notes.chart'
    chart.write_text(CHART_CONSTANT_BPM, encoding='utf-8')
    assert lcp.parse_chart_note_count(chart) == 4


def test_parse_chart_note_count_missing_file_returns_none(tmp_path):
    missing = tmp_path / 'does_not_exist.chart'
    assert lcp.parse_chart_note_count(missing) is None
