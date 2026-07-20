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
_NOTE_TICK_RE = re.compile(r'^\s*(\d+)\s*=\s*N\s+\d+\s+\d+\s*$', re.MULTILINE)
_BPM_EVENT_RE = re.compile(r'^\s*(\d+)\s*=\s*B\s+(\d+)\s*$', re.MULTILINE)
_RESOLUTION_RE = re.compile(r'(?im)^\s*Resolution\s*=\s*(\d+)\s*$')

_DEFAULT_RESOLUTION = 192
_DEFAULT_BPM = 120.0

# Expert-only difficulty priority for the single representative NPS figure a
# booklet needs -- a deliberate v1 simplification (see module docstring),
# not full per-difficulty NPS.
_EXPERT_SECTION_PRIORITY = (
    'ExpertSingle', 'ExpertDoubleBass', 'ExpertDrums',
    'ExpertDoubleRhythm', 'ExpertKeyboard', 'ExpertGHLGuitar',
)

# --- Feature detection -------------------------------------------------
#
# Grounded in TheNathannator/GuitarGame_ChartFormats' documented .chart
# format (github.com/TheNathannator/GuitarGame_ChartFormats, docs/
# Chart-File-Formats/chart-format/Tracks/{5-Fret-Guitar,Drums,Lyrics}.md).
# Notably: solos are the LOCAL EVENTS `E solo`/`E soloend`, not the `S 2`
# special phrase -- `S 2` is Star Power, a different, unrelated concept that
# would have been a wrong-by-one-character bug if assumed instead of checked.

# 5-fret guitar-family sections only -- open notes (note type 7) are a
# 5-fret concept; GHLGuitar (6-fret) uses different note numbering entirely.
_FIVE_FRET_SECTION_SUFFIXES = ('Single', 'DoubleGuitar', 'DoubleBass', 'DoubleRhythm', 'Keyboard')

_SOLO_START_RE = re.compile(r'^\s*\d+\s*=\s*E\s+"?solo"?\s*$', re.MULTILINE)
_LYRIC_EVENT_RE = re.compile(r'^\s*\d+\s*=\s*E\s+"?lyric\s', re.MULTILINE)
_OPEN_NOTE_RE = re.compile(r'^\s*\d+\s*=\s*N\s+7\s+\d+\s*$', re.MULTILINE)
_TWO_X_KICK_RE = re.compile(r'^\s*\d+\s*=\s*N\s+32\s+\d+\s*$', re.MULTILINE)
_ROLL_LANE_RE = re.compile(r'^\s*\d+\s*=\s*S\s+6[56]\s+\d+\s*$', re.MULTILINE)


def parse_chart_features_from_text(text):
    """has_lyrics/has_solos/has_open_notes/has_2x_kick/has_roll_lanes, from
    raw .chart text. Solos and lyrics are checked across the whole file
    (both can appear on any instrument); open notes are scoped to 5-fret
    guitar-family sections, 2x-kick and roll lanes to Drums sections --
    matching where the format actually defines those note/phrase types.
    """
    sections = _SECTION_RE.findall(text)

    has_open_notes = False
    has_2x_kick = False
    has_roll_lanes = False
    for section_name, body in sections:
        is_five_fret = any(section_name.endswith(suffix) for suffix in _FIVE_FRET_SECTION_SUFFIXES)
        is_drums = section_name.endswith('Drums')
        if is_five_fret and _OPEN_NOTE_RE.search(body):
            has_open_notes = True
        if is_drums and _TWO_X_KICK_RE.search(body):
            has_2x_kick = True
        if is_drums and _ROLL_LANE_RE.search(body):
            has_roll_lanes = True

    return {
        'has_lyrics': bool(_LYRIC_EVENT_RE.search(text)),
        'has_solos': bool(_SOLO_START_RE.search(text)),
        'has_open_notes': has_open_notes,
        'has_2x_kick': has_2x_kick,
        'has_roll_lanes': has_roll_lanes,
    }


_NO_FEATURES = {
    'has_lyrics': False, 'has_solos': False,
    'has_open_notes': False, 'has_2x_kick': False, 'has_roll_lanes': False,
}


def parse_chart_features(path):
    """Same as parse_chart_features_from_text, reading from a file path.
    Missing or unreadable files return all-False rather than raising."""
    try:
        with open(path, encoding='utf-8-sig', errors='replace') as f:
            text = f.read()
    except OSError as e:
        log.warning('Could not read chart %s: %s', path, e)
        return dict(_NO_FEATURES)
    return parse_chart_features_from_text(text)


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


def _parse_resolution(text):
    m = _RESOLUTION_RE.search(text)
    return int(m.group(1)) if m else _DEFAULT_RESOLUTION


def _parse_bpm_events(text):
    """Sorted (tick, bpm) list from [SyncTrack]. BPM events store bpm*1000
    as an integer, per the .chart format. Falls back to a single
    (0, _DEFAULT_BPM) event when the sync track has none -- a malformed or
    truncated chart still needs *some* tempo to convert ticks to seconds."""
    events = [(int(tick), int(raw_bpm) / 1000.0)
              for tick, raw_bpm in _BPM_EVENT_RE.findall(text)]
    if not events:
        return [(0, _DEFAULT_BPM)]
    events.sort(key=lambda e: e[0])
    if events[0][0] != 0:
        events.insert(0, (0, _DEFAULT_BPM))
    return events


def _tick_to_seconds(tick, resolution, bpm_events):
    """Integrate tempo changes to convert an absolute tick position to
    elapsed seconds from the start of the chart."""
    seconds = 0.0
    for i, (event_tick, bpm) in enumerate(bpm_events):
        segment_end = bpm_events[i + 1][0] if i + 1 < len(bpm_events) else tick
        segment_end = min(segment_end, tick)
        if segment_end <= event_tick:
            continue
        seconds += (segment_end - event_tick) / resolution * (60.0 / bpm)
        if segment_end >= tick:
            break
    return seconds


def _extract_note_ticks(body):
    return sorted(int(tick) for tick in _NOTE_TICK_RE.findall(body))


def _select_expert_note_ticks(text, minimum=1):
    """Note ticks (including chord duplicates) from whichever instrument in
    _EXPERT_SECTION_PRIORITY appears first with at least `minimum` notes, or
    None if none qualify. Shared by parse_chart_nps and
    parse_chart_note_count so both describe the same track."""
    sections = dict(_SECTION_RE.findall(text))
    for section_name in _EXPERT_SECTION_PRIORITY:
        body = sections.get(section_name)
        if body is None:
            continue
        candidate = _extract_note_ticks(body)
        if len(candidate) >= minimum:
            return candidate
    return None


def parse_chart_nps_from_text(text):
    """Average notes-per-second across the chart's Expert difficulty, using
    whichever instrument in _EXPERT_SECTION_PRIORITY appears first with two
    or more notes. None when no Expert section has enough notes to define a
    rate (fewer than 2), including when the file has no note sections at all.
    """
    ticks = _select_expert_note_ticks(text, minimum=2)
    if ticks is None:
        return None

    resolution = _parse_resolution(text)
    bpm_events = _parse_bpm_events(text)
    first_s = _tick_to_seconds(ticks[0], resolution, bpm_events)
    last_s = _tick_to_seconds(ticks[-1], resolution, bpm_events)
    span = last_s - first_s
    if span <= 0:
        return None
    return (len(ticks) - 1) / span


def parse_chart_nps(path):
    """Same as parse_chart_nps_from_text, reading from a file path. Missing
    or unreadable files return None rather than raising."""
    try:
        with open(path, encoding='utf-8-sig', errors='replace') as f:
            text = f.read()
    except OSError as e:
        log.warning('Could not read chart %s: %s', path, e)
        return None
    return parse_chart_nps_from_text(text)


def parse_chart_note_count_from_text(text):
    """Total note count (chords count each note individually) for the same
    Expert-section selection parse_chart_nps uses -- the two are meant to be
    read together (\"1247 notes, 7.3 NPS\"), so they must describe the same
    track. None when no Expert section has any notes."""
    ticks = _select_expert_note_ticks(text, minimum=1)
    return len(ticks) if ticks is not None else None


def parse_chart_note_count(path):
    """Same as parse_chart_note_count_from_text, reading from a file path.
    Missing or unreadable files return None rather than raising."""
    try:
        with open(path, encoding='utf-8-sig', errors='replace') as f:
            text = f.read()
    except OSError as e:
        log.warning('Could not read chart %s: %s', path, e)
        return None
    return parse_chart_note_count_from_text(text)
