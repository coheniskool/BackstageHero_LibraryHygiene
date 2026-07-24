# tests/test_library_scores.py
# Covers library_scores -- Task 1.2 of the library-enrichment plan.
# See tasks/plan-library-enrichment.md.
#
# read_scoredata()'s byte layout is reverse-engineered from a real Clone
# Hero installation's scores.bin (2026-07-20), NOT the raw-16-byte-MD5
# format a third-party reader's README described (that format garbage-
# parsed against the real file -- confirmed by trying it first and getting
# nonsense: num_scores=52, difficulty=55 out of a 0-3 range, etc). The
# layout below was cross-validated against all 7 entries in a real file:
# exact byte accounting (350 payload bytes / 7 entries with zero remainder
# after parsing), clean EOF at exactly the file's length, and sane
# percent/score magnitudes -- not a guess. See
# tasks/todo-library-enrichment.md's Task 1.2 notes for the full derivation.
#
# Confirmed layout:
#   4 bytes  opaque header (skipped, not validated)
#   4 bytes  LE uint32 song count
#   per song:
#     1 byte   checksum string length
#     N bytes  ASCII hex checksum (uppercase in the real file; normalized
#              to lowercase here to match notes_mid_md5()'s hexdigest())
#     1 byte   number of instruments with scores
#     1 byte   plays
#     per instrument:
#       4 bytes  LE uint32 instrument index
#       1 byte   difficulty index (0=easy..3=expert)
#       2 bytes  LE uint16 percent numerator
#       2 bytes  LE uint16 percent denominator (100 in every real sample)
#       1 byte   stars
#       1 byte   unknown, always 1 in every real sample -- skipped
#       4 bytes  LE uint32 score
#
# high_score_streak is NOT part of the sidecar for songs sourced this way:
# no streak field exists anywhere in this confirmed layout, and the
# original spec's mention of one was based on the (wrong) third-party
# README, not real data -- see SPEC-library-enrichment.md's Sidecar Format.

import struct

import library_scores as ls


def _build_scores_bin(entries):
    """entries: list of (checksum_hex, plays, [(instrument_idx, difficulty_idx,
    numerator, denominator, stars, score), ...]). Builds real bytes matching
    the confirmed layout above."""
    out = b'\xfc\xec\x33\x01'  # opaque header, real bytes from a live file
    out += struct.pack('<I', len(entries))
    for checksum, plays, instruments in entries:
        checksum_bytes = checksum.upper().encode('ascii')
        out += bytes([len(checksum_bytes)]) + checksum_bytes
        out += bytes([len(instruments), plays])
        for instr_idx, diff_idx, numerator, denominator, stars, score in instruments:
            out += struct.pack('<I', instr_idx)
            out += bytes([diff_idx])
            out += struct.pack('<H', numerator)
            out += struct.pack('<H', denominator)
            out += bytes([stars, 1])  # stars, then the unknown always-1 byte
            out += struct.pack('<I', score)
    return out


def test_read_scoredata_parses_a_real_shaped_file(tmp_path):
    checksum = '62057549d38dafd406ecb76849290f4'
    data = _build_scores_bin([
        (checksum, 1, [(0, 0, 70, 100, 3, 44725)]),
    ])
    ch_data = tmp_path / 'ch_data'
    ch_data.mkdir()
    (ch_data / ls.SCOREDATA_FILENAME).write_bytes(data)

    result = ls.read_scoredata(ch_data)

    assert checksum in result
    entry = result[checksum]
    assert entry['plays'] == 1
    assert entry['instruments']['lead']['difficulty'] == 'easy'
    assert entry['instruments']['lead']['percent_numerator'] == 70
    assert entry['instruments']['lead']['percent_denominator'] == 100
    assert entry['instruments']['lead']['stars'] == 3
    assert entry['instruments']['lead']['score'] == 44725


def test_read_scoredata_checksum_normalized_to_lowercase_matching_notes_mid_md5(tmp_path):
    """The real file stores checksums as UPPERCASE hex; notes_mid_md5()
    returns lowercase (hashlib's default). A caller must be able to look up
    scoredata[notes_mid_md5(folder)] directly without normalizing case
    itself -- that's this function's job, not every caller's."""
    data = _build_scores_bin([('ABCDEF0123456789ABCDEF0123456789'.lower(), 1,
                               [(0, 3, 97, 100, 5, 999999)])])
    ch_data = tmp_path / 'ch_data'
    ch_data.mkdir()
    (ch_data / ls.SCOREDATA_FILENAME).write_bytes(data)

    result = ls.read_scoredata(ch_data)
    assert 'abcdef0123456789abcdef0123456789' in result


def test_read_scoredata_multiple_songs_and_instruments(tmp_path):
    data = _build_scores_bin([
        ('a' * 32, 5, [(0, 3, 95, 100, 4, 500000), (1, 2, 80, 100, 3, 200000)]),
        ('b' * 32, 2, [(7, 1, 60, 100, 2, 30000)]),
    ])
    ch_data = tmp_path / 'ch_data'
    ch_data.mkdir()
    (ch_data / ls.SCOREDATA_FILENAME).write_bytes(data)

    result = ls.read_scoredata(ch_data)
    assert set(result) == {'a' * 32, 'b' * 32}
    assert set(result['a' * 32]['instruments']) == {'lead', 'bass'}
    assert result['b' * 32]['instruments']['keys']['score'] == 30000


def test_read_scoredata_no_scores_bin_returns_empty_dict(tmp_path):
    ch_data = tmp_path / 'ch_data'
    ch_data.mkdir()
    assert ls.read_scoredata(ch_data) == {}


def test_read_scoredata_missing_dir_returns_empty_dict(tmp_path):
    assert ls.read_scoredata(tmp_path / 'does_not_exist') == {}


def test_read_scoredata_truncated_file_does_not_raise(tmp_path):
    """A truncated/corrupt file must degrade to no scores, never crash a
    library-wide enrichment run."""
    ch_data = tmp_path / 'ch_data'
    ch_data.mkdir()
    good = _build_scores_bin([('a' * 32, 1, [(0, 0, 70, 100, 3, 100)])])
    (ch_data / ls.SCOREDATA_FILENAME).write_bytes(good[:10])  # cut mid-entry
    assert ls.read_scoredata(ch_data) == {}


def test_read_scoredata_unknown_instrument_index_falls_back_to_str(tmp_path):
    """The instrument lookup table only covers 8 known slots. An index
    outside that range must not crash -- fall back to its numeric string."""
    data = _build_scores_bin([('a' * 32, 1, [(99, 0, 70, 100, 3, 100)])])
    ch_data = tmp_path / 'ch_data'
    ch_data.mkdir()
    (ch_data / ls.SCOREDATA_FILENAME).write_bytes(data)

    result = ls.read_scoredata(ch_data)
    assert '99' in result['a' * 32]['instruments']


def test_read_scoredata_high_score_streak_is_not_part_of_this_layout(tmp_path):
    """Documents a deliberate omission: no streak field exists anywhere in
    the confirmed real layout. This test exists so a future change adding
    one is a conscious decision, not an accidental silent no-op."""
    data = _build_scores_bin([('a' * 32, 1, [(0, 0, 70, 100, 3, 100)])])
    ch_data = tmp_path / 'ch_data'
    ch_data.mkdir()
    (ch_data / ls.SCOREDATA_FILENAME).write_bytes(data)

    result = ls.read_scoredata(ch_data)
    assert 'streak' not in result['a' * 32]
    assert 'streak' not in result['a' * 32]['instruments']['lead']


# --- notes_mid_md5 (unchanged from prior implementation) --------------------

import hashlib
import resolver_client


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
    (tmp_path / 'notes.chart').write_text('[Song]\n{\n}\n', encoding='utf-8')
    (tmp_path / 'notes.mid').write_bytes(b'MThd fake midi bytes')
    assert ls.notes_mid_md5(tmp_path) != resolver_client.chart_hash(str(tmp_path))
