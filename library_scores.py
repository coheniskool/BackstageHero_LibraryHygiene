# library_scores.py
# Clone Hero high-score lookup for the library-enrichment tool (Task 1.2 of
# tasks/plan-library-enrichment.md).
#
# notes_mid_md5() is a plain MD5-of-a-file operation with no format
# ambiguity. It is deliberately a SEPARATE hash from resolver_client.
# chart_hash() (SHA256, prefers notes.chart when both exist) -- Clone
# Hero's own score file keys entries by MD5(notes.mid) specifically, a
# different algorithm serving a different purpose. See
# SPEC-library-enrichment.md's Sidecar Format section.
#
# read_scoredata()'s byte layout is reverse-engineered from a real Clone
# Hero installation's score file (2026-07-20), NOT the raw-16-byte-MD5
# format a third-party reader's README described -- that format
# garbage-parsed against the real file (num_scores=52, difficulty=55 out
# of a documented 0-3 range, checksum bytes that were actually ASCII text).
# The layout below was cross-validated against all 7 entries in a real
# 358-byte file: exact byte accounting to a clean EOF with zero remainder,
# and sane percent/score magnitudes. See tests/test_library_scores.py's
# header comment for the full confirmed layout, and
# tasks/todo-library-enrichment.md's Task 1.2 notes for the derivation.
#
# Two real, concrete corrections from the original assumption:
#   1. The file is named `scores.bin`, not `scoredata.bin`.
#   2. Checksums are a length-prefixed ASCII hex STRING (uppercase in the
#      real file), not 16 raw bytes.
# high_score_streak does not exist anywhere in this confirmed layout --
# see the module's callers for how that's handled (never fabricated).

import hashlib
import logging
import struct
from pathlib import Path

log = logging.getLogger('backstagehero')

SCOREDATA_FILENAME = 'scores.bin'

_DIFFICULTY_NAMES = ('easy', 'medium', 'hard', 'expert')
_INSTRUMENT_NAMES = ('lead', 'bass', 'rhythm', '3', '4', '5', '6', 'keys')

_HEADER_SIZE = 4
_INSTRUMENT_RECORD_SIZE = 4 + 1 + 2 + 2 + 1 + 1 + 4  # index+diff+num+denom+stars+flag+score


def _difficulty_name(index):
    return _DIFFICULTY_NAMES[index] if 0 <= index < len(_DIFFICULTY_NAMES) else str(index)


def _instrument_name(index):
    return _INSTRUMENT_NAMES[index] if 0 <= index < len(_INSTRUMENT_NAMES) else str(index)


def notes_mid_md5(song_folder):
    """MD5 hex digest of song_folder/notes.mid, or None if it doesn't exist
    or can't be read. This is the key Clone Hero's own score file uses --
    NOT resolver_client.chart_hash(), which is a different hash for a
    different purpose (see module docstring)."""
    mid_path = Path(song_folder) / 'notes.mid'
    try:
        with open(mid_path, 'rb') as f:
            h = hashlib.md5()
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
            return h.hexdigest()
    except OSError:
        return None


def _parse_scoredata(data):
    """Raises struct.error/IndexError/UnicodeDecodeError on any malformed
    or truncated input -- read_scoredata() is the boundary that catches
    those and degrades to {}, so this stays a pure, strict parser."""
    pos = _HEADER_SIZE
    num_songs, = struct.unpack_from('<I', data, pos)
    pos += 4

    result = {}
    for _ in range(num_songs):
        length = data[pos]
        pos += 1
        checksum = data[pos:pos + length].decode('ascii').lower()
        pos += length
        if len(checksum) != length:
            raise IndexError('truncated checksum string')

        num_instruments = data[pos]
        pos += 1
        plays = data[pos]
        pos += 1

        instruments = {}
        for _ in range(num_instruments):
            (instr_idx, diff_idx, numerator, denominator, stars, _flag, score) = \
                struct.unpack_from('<IBHHBBI', data, pos)
            pos += _INSTRUMENT_RECORD_SIZE
            instruments[_instrument_name(instr_idx)] = {
                'difficulty': _difficulty_name(diff_idx),
                'percent_numerator': numerator,
                'percent_denominator': denominator,
                'stars': stars,
                'score': score,
            }

        result[checksum] = {'plays': plays, 'instruments': instruments}
    return result


def read_scoredata(ch_data_path):
    """High scores keyed by notes_mid_md5() (lowercase hex -- this function
    normalizes the real file's uppercase-hex checksums itself, so callers
    never need to case-fold). {} on a missing directory, missing file, or
    any parse failure -- a corrupt/truncated/future-version score file must
    degrade to 'no scores available', never crash a library-wide run.
    """
    path = Path(ch_data_path) / SCOREDATA_FILENAME
    try:
        data = path.read_bytes()
    except OSError as e:
        log.warning('Could not read %s: %s', path, e)
        return {}

    try:
        return _parse_scoredata(data)
    except (struct.error, IndexError, UnicodeDecodeError) as e:
        log.warning('Could not parse %s (unexpected format): %s', path, e)
        return {}
