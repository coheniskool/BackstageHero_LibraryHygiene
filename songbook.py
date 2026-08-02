# songbook.py
# Generates a punk/grunge-styled, print-and-bind-ready PDF "karaoke songbook"
# from a Clone Hero library's Artist/Title data -- an alphabetical listing
# with letter-divider sections and a "Most Requested" table of contents for
# outlier-prolific artists. Ports the design/pagination logic from the
# handoff prototype at .../Karaoke Book Design System/design_handoff_karaoke_songbook/.
#
# GUI-import-free (no `import gui`), matching dedupe_report.py/chart_rename.py's
# existing separation -- callable from gui.py's button, from a standalone CLI,
# and from tests with no Tk/customtkinter involved.

import math
import os
import statistics
from functools import lru_cache
from pathlib import Path

import library_common
from VideoDownload import read_metadata

# Leading characters stripped before computing an artist's sort key, so
# '"Weird Al" Yankovic' sorts under W, not under the quote mark. Matches the
# design handoff README's exact rule (straight and curly quotes both count).
_LEADING_QUOTE_CHARS = '"\'‘’“”'


def _sort_key(text):
    return text.lstrip(_LEADING_QUOTE_CHARS).lower()


def _bucket_letter(sort_key):
    """First alphanumeric char of sort_key, uppercased; digits and anything
    with no alphanumeric char at all bucket into '#' (matches the design
    handoff README's letter-grouping rule)."""
    for ch in sort_key:
        if ch.isalnum():
            return '#' if ch.isdigit() else ch.upper()
    return '#'


def parse_entries(songs_or_folder):
    """list[(artist, title)] from either an in-memory list of Song-like
    objects (only `.folder` is read -- duck-typed so gui.py's Song dataclass
    works with no import needed here) or a raw library folder path (walked
    via library_common.iter_song_folders, for the standalone CLI path).

    Rows missing artist or title (after stripping whitespace) are skipped.
    read_metadata() falls back to the folder's own name for title, so in
    practice the only real-world skip case is a song with no artist tag at
    all -- title is rarely actually empty.
    """
    if isinstance(songs_or_folder, (str, Path)):
        folders = library_common.iter_song_folders(songs_or_folder)
    else:
        folders = (s.folder for s in songs_or_folder)

    entries = []
    for folder in folders:
        artist, title = read_metadata(folder)
        artist = (artist or '').strip()
        title = (title or '').strip()
        if not artist or not title:
            continue
        entries.append((artist, title))
    return entries


def dedupe_and_sort(entries):
    """list[(artist_display_name, sorted_song_titles)], case-insensitively
    deduped and alphabetically sorted per the design handoff README:
      - artists differing only in case are merged, keeping whichever casing
        was seen first as the display name
      - identical (artist, title) pairs are deduped case-insensitively
      - artists are sorted stripping leading quote chars first; each
        artist's own songs are sorted case-insensitively too
    """
    artist_display = {}   # artist_key -> first-seen display casing
    songs_by_artist = {}  # artist_key -> {song_key: first-seen display title}

    for artist, title in entries:
        artist_key = artist.lower()
        if artist_key not in artist_display:
            artist_display[artist_key] = artist
        songs = songs_by_artist.setdefault(artist_key, {})
        song_key = title.lower()
        if song_key not in songs:
            songs[song_key] = title

    result = []
    for artist_key in sorted(artist_display, key=_sort_key):
        songs = songs_by_artist[artist_key]
        sorted_titles = [songs[k] for k in sorted(songs, key=_sort_key)]
        result.append((artist_display[artist_key], sorted_titles))
    return result


def bucket_by_letter(sorted_entries):
    """{'letters': [{'letter': str, 'artists': [{'name', 'songs'}, ...]}]}
    grouped A-Z then '#' last -- '#' always renders at the end (like a
    book's symbols/numbers section) regardless of where those artists land
    in raw alphabetical sort order (e.g. '!I!' sorts before 'A' by ASCII,
    but still belongs in the trailing '#' section, not the front).
    """
    buckets = {}
    for artist, songs in sorted_entries:
        letter = _bucket_letter(_sort_key(artist))
        buckets.setdefault(letter, []).append({'name': artist, 'songs': songs})

    letter_order = [chr(c) for c in range(ord('A'), ord('Z') + 1)] + ['#']
    return {'letters': [{'letter': l, 'artists': buckets[l]}
                         for l in letter_order if l in buckets]}


def compute_stats_and_toc(buckets):
    """(stats_dict, toc_list) from a bucket_by_letter() result.

    stats = {totalSongs, totalArtists, mean, stdev, threshold} where
    threshold = mean + 1.5*stdev (songs-per-artist). toc lists artists over
    that threshold, sorted alphabetically (not by count), matching the
    "Most Requested" page's own copy ("average + 1.5 stdev").

    Uses population stdev (statistics.pstdev): this describes the exact,
    fully-known set of artists in the library, not a sample drawn from a
    larger population.
    """
    artists = [(group['name'], len(group['songs']))
               for letter_group in buckets['letters']
               for group in letter_group['artists']]

    total_artists = len(artists)
    total_songs = sum(count for _, count in artists)
    counts = [count for _, count in artists]
    mean = statistics.mean(counts) if counts else 0.0
    stdev = statistics.pstdev(counts) if counts else 0.0
    threshold = mean + 1.5 * stdev

    toc = sorted(
        ({'name': name, 'count': count} for name, count in artists if count > threshold),
        key=lambda e: _sort_key(e['name']))

    stats = {
        'totalSongs': total_songs,
        'totalArtists': total_artists,
        'mean': mean,
        'stdev': stdev,
        'threshold': threshold,
    }
    return stats, toc


# --- text measurement -----------------------------------------------------------
#
# The reference prototype measures text with a browser <canvas> 2D context's
# measureText(). PIL stands in for that here, but naively: PIL rounds every
# glyph advance to a whole pixel, so Courier New at 10.5px measures 7.0px per
# character instead of the true 6.3 (0.6em) -- 11% high. Left uncorrected that
# inflates wrap-line counts, which silently shifts page breaks and every
# "Most Requested" page number in the book.
#
# The fix is to measure at _MEASURE_SCALE x the requested size and divide back
# down, which amortises the rounding away. At scale 100 the two sizes this
# design actually uses land on whole-pixel advances exactly (10.5*100*0.6=630,
# 12.5*100*0.6=750), so the result matches canvas to the digit -- verified at
# zero error across 3000 real song titles from the sample library.
_MEASURE_SCALE = 100

# The reference multiplies every canvas measurement by this before dividing
# into lines. Kept verbatim: our measurement now equals canvas's, so the
# original's compensation applies unchanged.
_MEASURE_FUDGE = 1.08

# ('font file', px size) per text role, matching the reference's two CSS font
# strings: "700 12.5px 'Courier New'" for artists, "10.5px 'Courier New'" for
# song lines.
_FONTS = {
    'artist': ('courbd.ttf', 12.5),
    'song': ('cour.ttf', 10.5),
}

_FONT_DIRS = (
    os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'),
    '/usr/share/fonts/truetype/msttcorefonts',
    '/Library/Fonts',
)


class SongbookFontError(RuntimeError):
    """Raised when the fonts the layout is measured against are unavailable."""


def _font_path(kind):
    """Absolute path to the TTF for a text role, or None if not installed."""
    filename, _ = _FONTS[kind]
    for directory in _FONT_DIRS:
        candidate = os.path.join(directory, filename)
        if os.path.exists(candidate):
            return candidate
    return None


@lru_cache(maxsize=8)
def _load_font(kind):
    """Cached PIL font for a role, pre-scaled by _MEASURE_SCALE. Imported
    lazily so this module stays importable (and Task 1's data functions stay
    usable) on a machine with no pillow or no Courier New."""
    from PIL import ImageFont
    path = _font_path(kind)
    if path is None:
        raise SongbookFontError(
            f"Courier New ({_FONTS[kind][0]}) not found -- the songbook's layout "
            'is measured against it and cannot be computed without it.')
    _, size = _FONTS[kind]
    return ImageFont.truetype(path, size * _MEASURE_SCALE)


def _measure_width(text, kind):
    """Rendered width of text in px, equivalent to canvas measureText()."""
    return _load_font(kind).getlength(text) / _MEASURE_SCALE


def _measure_lines(text, kind, max_width):
    """How many wrapped lines text occupies in a max_width-px column."""
    width = _measure_width(text, kind) * _MEASURE_FUDGE
    return max(1, math.ceil(width / max_width))


# --- pagination -------------------------------------------------------------------
#
# Ported line-by-line from the reference prototype's _paginate(). This is a
# measure-and-flow layout: CSS column-count cannot express "900px of capacity
# per column, with a lookahead so an artist name is never stranded from its
# first song", so the whole flow is computed here before any HTML exists.

_COL_CAPACITY = 900          # px of vertical room per column
_COL_GAP = 20                # px between columns
_PAGE_WIDTH_PX = 816         # US Letter, 8.5in at 96dpi
_NON_BINDING_PADDING_PX = 52.8   # 0.55in of page padding on the non-spine side
_LETTER_BANNER_HEIGHT = 64 + 14  # banner box + its margin-bottom
_ARTIST_LINE_HEIGHT = 17
_ARTIST_MARGIN_TOP = 9
_SONG_LINE_HEIGHT = 16
_SONG_PAD_LEFT = 25          # hang-indent, subtracted from the wrap width
_FIRST_CONTENT_PAGE = 3      # page 1 is the cover, page 2 the "Most Requested" TOC

# The reference disagrees with itself on this default -- 0.85 inside
# _paginate() but 0.9 in renderVals(), which is what actually drives the
# rendered CSS padding. Following 0.85 would measure against a column 4.8px
# wider than the one really printed, so the two are reconciled on 0.9 here.
DEFAULT_BINDING_MARGIN = 0.9
DEFAULT_COLUMN_COUNT = 3


def _default_measure_lines(text, kind, max_width):
    return _measure_lines(text, kind, max_width)


def _flatten(buckets):
    """The letters/artists/songs tree as one linear item stream, which is what
    the flow algorithm walks."""
    flat = []
    for letter_group in buckets['letters']:
        flat.append({'type': 'letter', 'letter': letter_group['letter']})
        for artist in letter_group['artists']:
            flat.append({'type': 'artist', 'name': artist['name']})
            for song in artist['songs']:
                flat.append({'type': 'song', 'title': song})
    return flat


def paginate(buckets, toc, column_count=DEFAULT_COLUMN_COUNT,
             binding_margin=DEFAULT_BINDING_MARGIN, measure=None):
    """Flow a bucket_by_letter() tree into fixed-capacity page columns.

    Returns {'pages': [{'pageNumber', 'columns'}], 'toc': [...with 'page'],
    'colWidth': int}. `toc` entries are passed through with a 'page' added --
    those numbers are computed here, during the flow, because an artist's page
    is not knowable until everything before it has been placed.

    `measure` overrides line measurement (text, kind, max_width) -> line count;
    tests inject an exact one so the state machine is verified independently of
    installed fonts.
    """
    measure_lines = measure or _default_measure_lines
    num_cols = column_count

    content_width = _PAGE_WIDTH_PX - binding_margin * 96 - _NON_BINDING_PADDING_PX
    col_width = math.floor((content_width - _COL_GAP * (num_cols - 1)) / num_cols)
    song_width = col_width - _SONG_PAD_LEFT

    flat = _flatten(buckets)

    def fresh_page():
        return [[] for _ in range(num_cols)]

    pages = [fresh_page()]
    page_idx = 0
    col_idx = 0
    remaining = _COL_CAPACITY
    first_page = {}

    def advance():
        """Column break -- rolling onto a new page once the last column fills."""
        nonlocal page_idx, col_idx, remaining
        col_idx += 1
        if col_idx > num_cols - 1:
            col_idx = 0
            page_idx += 1
            # page_idx only ever increments, and never past len(pages), so
            # appending is equivalent to the reference's indexed assignment.
            pages.append(fresh_page())
        remaining = _COL_CAPACITY

    def force_new_page():
        """Page break for a letter section. A no-op when already parked at the
        top of an empty page, so the book never opens on a blank sheet and no
        blank page appears between back-to-back letters."""
        nonlocal page_idx, col_idx, remaining
        if col_idx != 0 or remaining < _COL_CAPACITY:
            page_idx += 1
            col_idx = 0
            remaining = _COL_CAPACITY
            pages.append(fresh_page())

    for i, item in enumerate(flat):
        kind = item['type']

        if kind == 'letter':
            force_new_page()
            height = _LETTER_BANNER_HEIGHT
        elif kind == 'artist':
            lines = measure_lines(item['name'], 'artist', col_width)
            height = lines * _ARTIST_LINE_HEIGHT + _ARTIST_MARGIN_TOP
            # Orphan control: look one item ahead, and if this artist's first
            # song cannot follow it in what is left of the column, move the
            # pair together. Skipped when the pair could not fit even a whole
            # empty column -- advancing then would just hunt forever for room
            # that does not exist.
            nxt = flat[i + 1] if i + 1 < len(flat) else None
            if nxt is not None and nxt['type'] == 'song':
                next_lines = measure_lines(nxt['title'], 'song', song_width)
                combined = height + next_lines * _SONG_LINE_HEIGHT
                if combined <= _COL_CAPACITY and combined > remaining:
                    advance()
        else:
            lines = measure_lines(item['title'], 'song', song_width)
            height = lines * _SONG_LINE_HEIGHT

        if height > remaining:
            advance()

        pages[page_idx][col_idx].append({
            'isLetter': kind == 'letter',
            'isArtist': kind == 'artist',
            'isSong': kind == 'song',
            'letter': item.get('letter'),
            'name': item.get('name'),
            'title': item.get('title'),
        })
        remaining -= height

        if kind == 'artist' and item['name'] not in first_page:
            first_page[item['name']] = page_idx

    return {
        'pages': [{'pageNumber': i + _FIRST_CONTENT_PAGE, 'columns': cols}
                  for i, cols in enumerate(pages)],
        'toc': [dict(entry, page=first_page.get(entry['name'], 0) + _FIRST_CONTENT_PAGE)
                for entry in (toc or [])],
        'colWidth': col_width,
    }
