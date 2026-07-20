# tests/test_library_scores.py
# Covers library_scores -- Task 1.2 of the library-enrichment plan.
# See tasks/plan-library-enrichment.md.
#
# read_scoredata() (the scores.bin binary parser) stays a stub pending the
# real-install format spike -- see tasks/todo-library-enrichment.md's Task
# 1.2 notes. Only notes_mid_md5() is fully implemented here: it's a plain
# MD5-of-a-file operation with no format ambiguity to spike against.

import hashlib

import library_scores as ls


def test_notes_mid_md5_matches_hashlib(tmp_path):
    mid = tmp_path / 'notes.mid'
    mid.write_bytes(b'MThd fake midi bytes for hashing')
    expected = hashlib.md5(mid.read_bytes()).hexdigest()
    assert ls.notes_mid_md5(tmp_path) == expected


def test_notes_mid_md5_none_when_no_notes_mid(tmp_path):
    (tmp_path / 'notes.chart').write_text('[Song]\n{\n}\n', encoding='utf-8')
    assert ls.notes_mid_md5(tmp_path) is None


def test_notes_mid_md5_none_for_missing_folder(tmp_path):
    assert ls.notes_mid_md5(tmp_path / 'does_not_exist') is None


def test_notes_mid_md5_differs_from_resolver_client_chart_hash(tmp_path):
    """The whole point of keeping this a separate function: it must NOT be
    interchangeable with resolver_client.chart_hash() (SHA256, prefers
    notes.chart when both exist) -- see spec Sidecar Format."""
    import resolver_client
    (tmp_path / 'notes.chart').write_text('[Song]\n{\n}\n', encoding='utf-8')
    (tmp_path / 'notes.mid').write_bytes(b'MThd fake midi bytes')
    assert ls.notes_mid_md5(tmp_path) != resolver_client.chart_hash(str(tmp_path))


def test_read_scoredata_returns_empty_dict_pending_format_spike(tmp_path):
    """Stub behavior: read_scoredata() has no real parser yet (blocked on
    validating the observed scores.bin layout against a real install -- see
    Task 1.2 notes), so it must return {} rather than raise or fabricate
    data, keeping every caller's 'no scores available' path already correct."""
    ch_data = tmp_path / 'fake_ch_data'
    ch_data.mkdir()
    (ch_data / 'scores.bin').write_bytes(b'\x00\x01\x02')
    assert ls.read_scoredata(ch_data) == {}


def test_read_scoredata_missing_dir_returns_empty_dict(tmp_path):
    assert ls.read_scoredata(tmp_path / 'does_not_exist') == {}
