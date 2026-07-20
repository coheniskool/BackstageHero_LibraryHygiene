# tests/test_library_chart_parser.py
# Covers library_chart_parser.parse_chart_instruments -- Task 1.1 of the
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
