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

import statistics
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
