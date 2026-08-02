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

import argparse
import html
import math
import os
import shutil
import statistics
import subprocess
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


# --- HTML rendering ---------------------------------------------------------------
#
# Ports the reference prototype's markup/CSS near-verbatim into plain Python
# string templates (the design is final, not a starting point -- see
# SPEC-karaoke-songbook.md's Boundaries). The reference relied on a generic
# <doc-page> web component for page-box sizing/print CSS; that harness is not
# Clone-Hero-specific, so it is reimplemented directly here instead of ported.

# The reference source disagreed with itself on these: renderVals()'s own
# `??` fallback says accent=#8C2727/cover=#6B8E23, but the DC props schema
# declares accent=#3B5998/cover=#8C2727. Empirically, screenshots/01-cover.png
# and 02-most-requested.png (the design tool's own rendered captures) are red
# and denim-blue respectively -- i.e. they match the *props schema* defaults,
# not renderVals()'s internal fallback. Confirmed by directly comparing this
# port's Task 3 checkpoint render against both screenshots; verified visually,
# not assumed.
DEFAULT_ACCENT_COLOR = '#3B5998'
DEFAULT_COVER_COLOR = '#8C2727'

# Named swatches, matching the reference's DC props schema `options` lists --
# used by the CLI (Task 4) and GUI (Task 5) to offer the same choices the
# design tool did, without exposing raw hex to the user.
ACCENT_COLOR_CHOICES = {'red': '#8C2727', 'olive': '#B5A642', 'denim': '#3B5998'}
COVER_COLOR_CHOICES = {'olive': '#6B8E23', 'denim': '#3B5998', 'red': '#8C2727', 'yellow': '#B5A642'}

_PAGE_WIDTH_IN = 8.5
_PAGE_HEIGHT_IN = 11


def _esc(text):
    return html.escape(text or '', quote=True)


def _page_shell_css(binding_margin_in):
    # Each .page is sized to exactly one physical US Letter sheet and forced
    # onto its own printed page -- both the modern `break-after` and the
    # older `page-break-after` are set since headless Chrome's print-to-pdf
    # path is what actually consumes this, not a specific browser's newest
    # engine version.
    return f"""
    * {{ margin: 0; box-sizing: border-box; }}
    body {{ margin: 0; background: #2E2E2E; }}
    @page {{ size: {_PAGE_WIDTH_IN}in {_PAGE_HEIGHT_IN}in; margin: 0; }}
    .page {{
      width: {_PAGE_WIDTH_IN}in; height: {_PAGE_HEIGHT_IN}in;
      break-after: page; page-break-after: always;
      position: relative; overflow: hidden;
    }}
    .page:last-child {{ break-after: auto; page-break-after: auto; }}
    """


def _render_cover_page(stats, cover_color, binding_margin_in, synced_label):
    total_artists = stats.get('totalArtists', 0)
    total_songs = stats.get('totalSongs', 0)
    return f"""
    <section class="page" style="padding: 0.55in 0.55in 0.55in {binding_margin_in}; background: #232120; color: #E9E1D4;">
      <div style="position: absolute; inset: 0; background-image: radial-gradient(rgba(233,225,212,0.05) 1px, transparent 1px); background-size: 3px 3px; opacity: 0.6;"></div>
      <div style="position: absolute; inset: 0; background: repeating-linear-gradient(115deg, rgba(0,0,0,0.18) 0px, transparent 2px, transparent 6px); mix-blend-mode: multiply;"></div>
      <div style="position: absolute; top: -40px; right: -60px; width: 320px; height: 320px; border-radius: 50%; background: radial-gradient(circle, rgba(140,39,39,0.35), transparent 70%); filter: blur(2px);"></div>

      <div style="height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; position: relative;">
        <div style="position: relative; width: 92%; height: 90%; display: flex; flex-direction: column; background: {cover_color}; color: #232120; padding: 40px 32px; box-sizing: border-box; transform: rotate(-0.6deg); box-shadow: 0 14px 34px rgba(0,0,0,0.6);">
          <div style="position: absolute; inset: 0; background: repeating-linear-gradient(0deg, rgba(0,0,0,0.08) 0px, transparent 3px, transparent 7px);"></div>
          <div style="position: absolute; inset: 0; background: repeating-linear-gradient(90deg, rgba(0,0,0,0.06) 0px, transparent 3px, transparent 9px);"></div>

          <div style="position: absolute; top: -16px; left: 24px; width: 60px; height: 18px; background: repeating-linear-gradient(45deg, #9c9584, #9c9584 6px, #7a7468 6px, #7a7468 12px); transform: rotate(-30deg); box-shadow: 0 2px 5px rgba(0,0,0,0.5); z-index: 3;"></div>
          <div style="position: absolute; top: -14px; right: 30px; width: 56px; height: 18px; background: repeating-linear-gradient(45deg, #9c9584, #9c9584 6px, #7a7468 6px, #7a7468 12px); transform: rotate(24deg); box-shadow: 0 2px 5px rgba(0,0,0,0.5); z-index: 3;"></div>
          <div style="position: absolute; bottom: -14px; left: 40%; width: 56px; height: 18px; background: repeating-linear-gradient(45deg, #9c9584, #9c9584 6px, #7a7468 6px, #7a7468 12px); transform: rotate(-8deg); box-shadow: 0 2px 5px rgba(0,0,0,0.5); z-index: 3;"></div>

          <div style="position: relative; font-family: 'Courier New', monospace; font-size: 11px; font-weight: 700; letter-spacing: 3px; text-transform: uppercase;">VOL. 1 &middot; A LIBRARY OF NOISE</div>

          <div style="position: relative; margin-top: 26px; font-family: 'Courier New', monospace; font-weight: 700; font-size: 58px; line-height: 0.88; text-transform: uppercase; color: #E9E1D4; text-shadow: 3px 3px 0 #232120;">Clone<br>Hero</div>
          <div style="position: relative; margin-top: 8px; font-family: 'Courier New', monospace; font-weight: 700; font-size: 58px; line-height: 0.88; text-transform: uppercase; color: #232120; -webkit-text-stroke: 2px #E9E1D4;">Songbook</div>

          <div style="position: relative; margin-top: 34px; display: inline-block; background: #232120; color: #E9E1D4; padding: 9px 20px; font-family: 'Courier New', monospace; font-size: 15px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; transform: rotate(-1deg);">Every Song. Every Screamer.</div>

          <div style="position: relative; flex: 1; margin: 10px -32px 10px 0;"></div>

          <div style="position: relative; margin-top: auto; display: flex; justify-content: space-between; border-top: 3px dashed #232120; padding-top: 14px; font-family: 'Courier New', monospace; font-size: 15px; font-weight: 700;">
            <span>{total_artists} ARTISTS</span><span>{total_songs} SONGS</span>
          </div>
          <div style="position: relative; margin-top: 6px; text-align: right; font-family: 'Courier New', monospace; font-size: 10px; font-weight: 700; letter-spacing: 0.5px; opacity: 0.75;">LAST SYNCED: {_esc(synced_label)}</div>
        </div>
      </div>
    </section>
    """


def _render_toc_page(toc, stats, accent_color, binding_margin_in):
    threshold = stats.get('threshold', 0)
    entries = ''.join(f"""
          <div style="display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 6px 0; border-bottom: 1px dashed #b8b0a0; break-inside: avoid;">
            <span style="font-family: 'Courier New', monospace; font-weight: 700; font-size: 13px; text-transform: uppercase; color: #2E2E2E;">{_esc(entry['name'])}</span>
            <span style="display: flex; align-items: baseline; gap: 6px; flex: none;">
              <span style="font-family: 'Courier New', monospace; font-size: 11px; font-weight: 700; color: #2E2E2E;">p.{entry['page']}</span>
              <span style="font-family: 'Courier New', monospace; font-size: 11px; font-weight: 700; color: #E9E1D4; background: {accent_color}; padding: 2px 7px;">{entry['count']}</span>
            </span>
          </div>""" for entry in toc)
    return f"""
    <section class="page" style="padding: 0.55in 0.55in 0.55in {binding_margin_in}; background: #E9E1D4;">
      <div style="height: 100%; display: flex; flex-direction: column;">
        <div style="display: flex; align-items: baseline; gap: 14px; border-bottom: 5px solid #2E2E2E; padding-bottom: 10px; margin-bottom: 6px; flex: none;">
          <h2 style="font-family: 'Courier New', monospace; font-weight: 700; font-size: 34px; margin: 0; text-transform: uppercase; color: #2E2E2E; letter-spacing: 1px;">Most Requested</h2>
          <span style="font-family: 'Courier New', monospace; font-size: 12px; color: {accent_color}; font-weight: 700;">HEAVY HITTERS</span>
        </div>
        <p style="font-family: 'Courier New', monospace; font-size: 11px; color: #5a5550; margin: 0 0 18px; flex: none;">Artists with more than average + 1.5&sigma; songs in the library ({threshold:.2f}+). Sorted A&ndash;Z; page shows where the artist's songs begin.</p>
        <div style="column-count: 2; column-gap: 36px; flex: 1; overflow: hidden;">{entries}
        </div>
      </div>
    </section>
    """


def _render_item(item):
    if item['isLetter']:
        return f"""
                <div style="background: #2E2E2E; color: #E9E1D4; height: 64px; margin-bottom: 14px; display: flex; align-items: center; padding-left: 12px; clip-path: polygon(0% 0%, 100% 0%, 100% 78%, 92% 100%, 84% 78%, 76% 100%, 68% 78%, 60% 100%, 52% 78%, 44% 100%, 36% 78%, 28% 100%, 20% 78%, 12% 100%, 4% 78%, 0% 100%);">
                  <span style="font-family: 'Courier New', monospace; font-weight: 700; font-size: 32px;">{_esc(item['letter'])}</span>
                </div>"""
    if item['isArtist']:
        return f"""
                <div style="font-family: 'Courier New', monospace; font-weight: 700; font-size: 12.5px; text-transform: uppercase; color: #2E2E2E; margin-top: 9px;">{_esc(item['name'])}</div>"""
    return f"""
                <div style="font-family: 'Courier New', monospace; font-size: 10.5px; color: #4a453e; padding-left: 25px; text-indent: -13px; line-height: 1.5;">&mdash; {_esc(item['title'])}</div>"""


def _render_content_page(page, col_width_css, binding_margin_in):
    columns = ''.join(
        f'<div style="width: {col_width_css};">{"".join(_render_item(item) for item in col)}</div>'
        for col in page['columns'])
    return f"""
    <section class="page" style="padding: 0.55in 0.55in 0.4in {binding_margin_in};background: #E9E1D4;">
      <div style="height: 100%; display: flex; flex-direction: column;">
        <div style="display: flex; gap: 20px; flex: 1; overflow: hidden;">{columns}</div>
        <div style="flex: none; text-align: center; font-family: 'Courier New', monospace; font-size: 11px; color: #2E2E2E; font-weight: 700; padding-top: 8px;">{page['pageNumber']}</div>
      </div>
    </section>
    """


def render_html(paginated, stats, accent_color=DEFAULT_ACCENT_COLOR,
                 cover_color=DEFAULT_COVER_COLOR, binding_margin=DEFAULT_BINDING_MARGIN,
                 synced_label=''):
    """Full standalone HTML document string for one songbook: cover, "Most
    Requested" TOC, then one section per paginate() page. `paginated` is a
    paginate() result; `stats` is a compute_stats_and_toc() stats dict.
    """
    binding_margin_in = f'{binding_margin}in'
    col_width_css = f"{paginated['colWidth']}px"

    body = (
        _render_cover_page(stats, cover_color, binding_margin_in, synced_label)
        + _render_toc_page(paginated['toc'], stats, accent_color, binding_margin_in)
        + ''.join(_render_content_page(page, col_width_css, binding_margin_in)
                  for page in paginated['pages'])
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Clone Hero Songbook</title>
<style>{_page_shell_css(binding_margin_in)}</style>
</head>
<body>
{body}
</body>
</html>
"""


# --- PDF export ---------------------------------------------------------------
#
# No Puppeteer/Node/new pip dependency: shell out to whatever Chrome or Edge
# is already installed and use its own headless print-to-pdf, the same
# rendering engine (and same @page/print CSS support) a user would get
# printing the fallback .html file manually.

class BrowserNotFoundError(RuntimeError):
    """Raised when neither Chrome nor Edge can be located for PDF export."""


_BROWSER_EXECUTABLES = ('chrome', 'msedge')

_BROWSER_INSTALL_PATHS = tuple(
    os.path.join(program_files, vendor, 'Application', exe)
    for program_files in (r'C:\Program Files', r'C:\Program Files (x86)')
    for vendor, exe in (('Google\\Chrome', 'chrome.exe'), ('Microsoft\\Edge', 'msedge.exe'))
)


def _find_browser():
    """Absolute path to an installed Chrome or Edge, or raise
    BrowserNotFoundError -- generation must fail loudly here, never silently
    produce an empty or missing PDF (see SPEC-karaoke-songbook.md Boundaries).
    """
    for name in _BROWSER_EXECUTABLES:
        found = shutil.which(name)
        if found:
            return found
    for path in _BROWSER_INSTALL_PATHS:
        if os.path.exists(path):
            return path
    raise BrowserNotFoundError(
        'Could not find Chrome or Edge (checked PATH and the standard '
        'Program Files locations). Install either browser to generate a PDF, '
        'or open the .html file that was written alongside this error and '
        'use its Print dialog to save a PDF manually.')


def render_pdf(html_str, out_pdf_path):
    """Write out_pdf_path's sibling .html (kept as a manual-print fallback and
    for debugging pagination without re-invoking the browser), then shell out
    to headless Chrome/Edge to print it to out_pdf_path. Returns out_pdf_path
    on success; raises BrowserNotFoundError or subprocess.CalledProcessError
    on failure -- never silently produces a missing/empty file.
    """
    out_pdf_path = Path(out_pdf_path)
    out_html_path = out_pdf_path.with_suffix('.html')

    tmp_html = out_html_path.with_suffix('.html.tmp')
    tmp_html.write_text(html_str, encoding='utf-8')
    os.replace(tmp_html, out_html_path)

    browser = _find_browser()
    tmp_pdf = out_pdf_path.with_suffix('.pdf.tmp')
    subprocess.run([
        browser, '--headless', '--disable-gpu',
        f'--print-to-pdf={tmp_pdf}', '--no-pdf-header-footer',
        out_html_path.resolve().as_uri(),
    ], check=True, capture_output=True)
    os.replace(tmp_pdf, out_pdf_path)
    return out_pdf_path


# --- orchestrator + CLI -----------------------------------------------------------
#
# The single entry point gui.py's button calls, mirroring _run_library_tool's
# module-level-dispatch reasoning: it works identically with or without a GUI
# open, so the CLI below is a thin argument-parsing shell around it.

OUTPUT_BASENAME = 'Clone Hero Songbook'


class EmptyLibraryError(RuntimeError):
    """Raised when a library has no usable (artist, title) pairs to print."""


def generate_songbook(songs_folder, songs=None, column_count=DEFAULT_COLUMN_COUNT,
                       binding_margin=DEFAULT_BINDING_MARGIN,
                       accent_color=DEFAULT_ACCENT_COLOR, cover_color=DEFAULT_COVER_COLOR,
                       synced_label='', out_path=None):
    """Generate a songbook PDF (+ sibling .html) for a library.

    `songs`, when given, is used directly (the GUI's already-scanned in-memory
    list) -- no folder re-walk. When omitted, songs_folder itself is walked
    (the standalone CLI path). Regenerates fully every call, like
    gui.py's _export_library_csv() -- never an incremental update.

    Returns {'pdf_path', 'html_path', 'page_count', 'stats'}. Raises
    EmptyLibraryError if there is nothing to print, or BrowserNotFoundError
    if render_pdf() can't find Chrome/Edge -- both left uncaught, since a
    dialog-less caller and a GUI dialog want to handle them differently.
    """
    entries = parse_entries(songs if songs is not None else songs_folder)
    if not entries:
        raise EmptyLibraryError(
            f'No songs with both an artist and a title were found in {songs_folder!r}.')

    buckets = bucket_by_letter(dedupe_and_sort(entries))
    stats, toc = compute_stats_and_toc(buckets)
    paginated = paginate(buckets, toc, column_count=column_count,
                         binding_margin=binding_margin)
    html_str = render_html(paginated, stats, accent_color=accent_color,
                           cover_color=cover_color, binding_margin=binding_margin,
                           synced_label=synced_label)

    pdf_path = Path(out_path) if out_path else Path(songs_folder) / f'{OUTPUT_BASENAME}.pdf'
    result_path = render_pdf(html_str, pdf_path)

    return {
        'pdf_path': result_path,
        'html_path': result_path.with_suffix('.html'),
        'page_count': len(paginated['pages']) + 2,  # + cover + TOC
        'stats': stats,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Generate a printable Clone Hero karaoke songbook PDF from a library folder.')
    parser.add_argument('--library-path', type=str, required=True,
                        help='Path to your Clone Hero songs library folder.')
    parser.add_argument('--columns', type=int, default=DEFAULT_COLUMN_COUNT, choices=(2, 3, 4),
                        help='Song-list columns per page (default: 3).')
    parser.add_argument('--binding-margin', type=float, default=DEFAULT_BINDING_MARGIN,
                        help='Spine-side page margin in inches, 0.6-1.3 (default: 0.9).')
    parser.add_argument('--accent', choices=sorted(ACCENT_COLOR_CHOICES), default='denim',
                        help='Accent color for the TOC/badges (default: denim).')
    parser.add_argument('--cover', choices=sorted(COVER_COLOR_CHOICES), default='red',
                        help='Cover poster color (default: red).')
    parser.add_argument('--out', type=str, default=None,
                        help='Output PDF path (default: <library-path>/Clone Hero Songbook.pdf).')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    result = generate_songbook(
        args.library_path, column_count=args.columns, binding_margin=args.binding_margin,
        accent_color=ACCENT_COLOR_CHOICES[args.accent], cover_color=COVER_COLOR_CHOICES[args.cover],
        out_path=args.out)
    print(f"Wrote {result['pdf_path']} ({result['page_count']} pages, "
          f"{result['stats']['totalArtists']} artists, {result['stats']['totalSongs']} songs).")


if __name__ == '__main__':
    main()
