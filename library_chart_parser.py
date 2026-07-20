# library_chart_parser.py
# Parses notes.chart for booklet-relevant facts the library-enrichment tool
# needs (see SPEC-library-enrichment.md / tasks/plan-library-enrichment.md).
#
# This is presence verification, not the charter-assigned difficulty tier.
# song.ini's diff_* keys are a charter's subjective 0-6 rating and cannot be
# derived from the chart itself; what IS derivable is whether an instrument
# has any charted notes at all, which is what parse_chart_instruments
# reports (1 = confirmed present, -1 = absent) -- a useful, honest signal
# for a booklet ("does this song have drums charted?") that doesn't pretend
# to reproduce a rating only the charter can assign.

import logging
import re

log = logging.getLogger('backstagehero')

# Every instrument name the sidecar schema (SPEC-library-enrichment.md) uses.
# 'vocals' is included for schema-shape consistency but a .chart file has no
# vocals track -- CH reads vocals from song.ini/.mid, never from .chart -- so
# it is always -1 out of this parser. Callers needing real vocals detection
# must look elsewhere (a future notes.mid parser, or song.ini itself).
INSTRUMENT_NAMES = ('guitar', 'bass', 'drums', 'keys', 'vocals', 'rhythm', 'guitarghl')

# .chart section name -> instrument. Difficulty prefix (Easy/Medium/Hard/
# Expert) is stripped before matching, since any one difficulty having notes
# is enough to mark the instrument present.
_SECTION_SUFFIX_TO_INSTRUMENT = {
    'Single': 'guitar',
    'DoubleBass': 'bass',
    'DoubleRhythm': 'rhythm',
    'Drums': 'drums',
    'Keyboard': 'keys',
    'GHLGuitar': 'guitarghl',
}

_DIFFICULTY_PREFIXES = ('Easy', 'Medium', 'Hard', 'Expert')

_SECTION_RE = re.compile(r'\[(\w+)\]\s*\{(.*?)\}', re.DOTALL)
_NOTE_LINE_RE = re.compile(r'^\s*\d+\s*=\s*N\s+\d+\s+\d+\s*$', re.MULTILINE)


def _section_instrument(section_name):
    for prefix in _DIFFICULTY_PREFIXES:
        if section_name.startswith(prefix):
            suffix = section_name[len(prefix):]
            return _SECTION_SUFFIX_TO_INSTRUMENT.get(suffix)
    return None


def parse_chart_instruments_from_text(text):
    """Which instruments have at least one charted note, from raw .chart text.

    Returns a dict over INSTRUMENT_NAMES; 1 = confirmed present (any
    difficulty has a note line), -1 = absent. Never raises on malformed
    text -- unmatched or empty content simply yields no sections found.
    """
    result = {name: -1 for name in INSTRUMENT_NAMES}
    for section_name, body in _SECTION_RE.findall(text):
        instrument = _section_instrument(section_name)
        if instrument is None:
            continue
        if _NOTE_LINE_RE.search(body):
            result[instrument] = 1
    return result


def parse_chart_instruments(path):
    """Same as parse_chart_instruments_from_text, reading from a file path.

    Missing, unreadable, or non-UTF-8 files are a safe all-absent default,
    logged as a warning -- a single bad chart must never abort a library-wide
    enrichment run.
    """
    try:
        with open(path, encoding='utf-8-sig', errors='replace') as f:
            text = f.read()
    except OSError as e:
        log.warning('Could not read chart %s: %s', path, e)
        return {name: -1 for name in INSTRUMENT_NAMES}
    return parse_chart_instruments_from_text(text)
