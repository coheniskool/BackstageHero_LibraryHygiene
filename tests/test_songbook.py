import colorsys
import statistics
from pathlib import Path

import pytest
from PIL import Image

import songbook as sb


class _FakeSong:
    """Duck-typed stand-in for gui.py's Song dataclass -- only .folder is read."""
    def __init__(self, folder):
        self.folder = folder


def _write_song(tmp_path, name, artist, title):
    folder = tmp_path / name
    folder.mkdir()
    (folder / 'song.ini').write_text(
        f'[song]\nartist = {artist}\nname = {title}\n', encoding='utf-8')
    return folder


# --- parse_entries -----------------------------------------------------------

def test_parse_entries_from_song_list(tmp_path):
    f1 = _write_song(tmp_path, 'a', 'Sublime', 'Santeria')
    f2 = _write_song(tmp_path, 'b', 'Sum 41', 'Fat Lip')
    songs = [_FakeSong(str(f1)), _FakeSong(str(f2))]
    assert parse_result(sb.parse_entries(songs)) == [
        ('Sublime', 'Santeria'), ('Sum 41', 'Fat Lip')]


def test_parse_entries_from_folder_path(tmp_path):
    _write_song(tmp_path, 'a', 'Sublime', 'Santeria')
    _write_song(tmp_path, 'b', 'Sum 41', 'Fat Lip')
    assert parse_result(sb.parse_entries(str(tmp_path))) == [
        ('Sublime', 'Santeria'), ('Sum 41', 'Fat Lip')]


def test_parse_entries_skips_missing_artist(tmp_path):
    folder = tmp_path / 'no_artist'
    folder.mkdir()
    (folder / 'song.ini').write_text('[song]\nname = Some Title\n', encoding='utf-8')
    assert sb.parse_entries([_FakeSong(str(folder))]) == []


def test_parse_entries_trims_whitespace(tmp_path):
    folder = tmp_path / 'a'
    folder.mkdir()
    (folder / 'song.ini').write_text(
        '[song]\nartist =   Sublime  \nname =  Santeria \n', encoding='utf-8')
    assert sb.parse_entries([_FakeSong(str(folder))]) == [('Sublime', 'Santeria')]


def parse_result(entries):
    return entries


# --- dedupe_and_sort ----------------------------------------------------------

def test_dedupe_merges_artist_casing_keeps_first_seen():
    entries = [('sublime', 'Santeria'), ('Sublime', 'What I Got')]
    result = sb.dedupe_and_sort(entries)
    assert result == [('sublime', ['Santeria', 'What I Got'])]


def test_dedupe_drops_case_insensitive_duplicate_pairs():
    entries = [('Sublime', 'Santeria'), ('sublime', 'santeria'), ('Sublime', 'What I Got')]
    result = sb.dedupe_and_sort(entries)
    assert result == [('Sublime', ['Santeria', 'What I Got'])]


def test_dedupe_sorts_artists_stripping_leading_quotes():
    # real fixture from sample-library.csv: '"Weird Al" Yankovic' must sort
    # under W, not under the leading quote character.
    entries = [
        ('Sum 41', 'Fat Lip'),
        ('"Weird Al" Yankovic', 'Bob'),
        ('!I!', '!I'),
        ('Aerosmith', 'Dream On'),
    ]
    result = sb.dedupe_and_sort(entries)
    artists_in_order = [artist for artist, _ in result]
    assert artists_in_order == ['!I!', 'Aerosmith', 'Sum 41', '"Weird Al" Yankovic']


def test_dedupe_sorts_songs_within_artist_case_insensitively():
    entries = [('Sublime', 'santeria'), ('Sublime', 'What I Got'), ('Sublime', 'Badfish')]
    result = sb.dedupe_and_sort(entries)
    assert result == [('Sublime', ['Badfish', 'santeria', 'What I Got'])]


# --- bucket_by_letter ----------------------------------------------------------

def test_bucket_groups_by_first_letter():
    sorted_entries = [
        ('Aerosmith', ['Dream On']),
        ('Sublime', ['Santeria']),
        ('Sum 41', ['Fat Lip']),
    ]
    buckets = sb.bucket_by_letter(sorted_entries)
    letters = [g['letter'] for g in buckets['letters']]
    assert letters == ['A', 'S']
    a_group = buckets['letters'][0]
    assert a_group['artists'] == [{'name': 'Aerosmith', 'songs': ['Dream On']}]
    s_group = buckets['letters'][1]
    assert [a['name'] for a in s_group['artists']] == ['Sublime', 'Sum 41']


def test_bucket_uses_first_alphanumeric_char_not_leading_symbol():
    # '!I!' has an alphanumeric char ('I') once the leading '!' is skipped,
    # so it buckets under I, not '#' -- '#' is reserved for names with NO
    # alphanumeric char at all, or a leading digit (see next two tests).
    sorted_entries = [('!I!', ['!I']), ('"Weird Al" Yankovic', ['Bob'])]
    buckets = sb.bucket_by_letter(sorted_entries)
    letters = [g['letter'] for g in buckets['letters']]
    assert letters == ['I', 'W']


def test_bucket_digit_leading_name_goes_to_hash():
    sorted_entries = [('3 Doors Down', ['Kryptonite']), ('Aerosmith', ['Dream On'])]
    buckets = sb.bucket_by_letter(sorted_entries)
    letters = [g['letter'] for g in buckets['letters']]
    assert letters == ['A', '#']


def test_bucket_hash_always_last_regardless_of_input_order():
    # '#!' has no alphanumeric char at all, so it falls all the way through
    # to '#' -- and even though it sorts before 'A' alphabetically by raw
    # ASCII, the '#' bucket must still render last, like a book's
    # symbols/numbers section, not wherever it happens to land in sort order.
    sorted_entries = [('#!', ['Noise']), ('Aerosmith', ['Dream On'])]
    buckets = sb.bucket_by_letter(sorted_entries)
    letters = [g['letter'] for g in buckets['letters']]
    assert letters == ['A', '#']


# --- compute_stats_and_toc -----------------------------------------------------

def test_stats_exact_numbers():
    # 4 artists with song counts 1, 1, 1, 9 -> mean=3, pstdev=3.4641016...
    buckets = {'letters': [
        {'letter': 'A', 'artists': [
            {'name': 'Artist One', 'songs': ['S1']},
            {'name': 'Artist Two', 'songs': ['S1']},
            {'name': 'Artist Three', 'songs': ['S1']},
            {'name': 'Prolific Artist', 'songs': [f'S{i}' for i in range(9)]},
        ]},
    ]}
    stats, toc = sb.compute_stats_and_toc(buckets)
    counts = [1, 1, 1, 9]
    expected_mean = statistics.mean(counts)
    expected_stdev = statistics.pstdev(counts)
    expected_threshold = expected_mean + 1.5 * expected_stdev
    assert stats['totalArtists'] == 4
    assert stats['totalSongs'] == 12
    assert stats['mean'] == expected_mean
    assert stats['stdev'] == expected_stdev
    assert stats['threshold'] == expected_threshold
    assert stats['stdevMultiplier'] == sb.DEFAULT_STDEV_MULTIPLIER
    assert toc == [{'name': 'Prolific Artist', 'count': 9}]


def test_stats_custom_stdev_multiplier_changes_threshold_and_toc_membership():
    # Same 1/1/1/9 fixture as test_stats_exact_numbers, but a much larger
    # multiplier should push the threshold high enough that even the
    # 9-song artist no longer qualifies for the TOC.
    buckets = {'letters': [
        {'letter': 'A', 'artists': [
            {'name': 'Artist One', 'songs': ['S1']},
            {'name': 'Artist Two', 'songs': ['S1']},
            {'name': 'Artist Three', 'songs': ['S1']},
            {'name': 'Prolific Artist', 'songs': [f'S{i}' for i in range(9)]},
        ]},
    ]}
    counts = [1, 1, 1, 9]
    mean, stdev = statistics.mean(counts), statistics.pstdev(counts)

    stats, toc = sb.compute_stats_and_toc(buckets, stdev_multiplier=3.0)

    assert stats['threshold'] == mean + 3.0 * stdev
    assert stats['stdevMultiplier'] == 3.0
    assert toc == [], 'threshold*3 should exceed even the 9-song artist'


def test_toc_sorted_alphabetically_not_by_count():
    quiet = [{'name': f'Quiet Artist {i}', 'songs': ['S1']} for i in range(10)]
    buckets = {'letters': [
        {'letter': 'A', 'artists': quiet + [
            {'name': 'Zeta Prolific', 'songs': [f'S{i}' for i in range(20)]},
            {'name': 'Alpha Prolific', 'songs': [f'S{i}' for i in range(15)]},
        ]},
    ]}
    _, toc = sb.compute_stats_and_toc(buckets)
    names = [e['name'] for e in toc]
    assert names == ['Alpha Prolific', 'Zeta Prolific']


def test_stats_empty_library():
    stats, toc = sb.compute_stats_and_toc({'letters': []})
    assert stats == {'totalSongs': 0, 'totalArtists': 0, 'mean': 0.0,
                      'stdev': 0.0, 'threshold': 0.0,
                      'stdevMultiplier': sb.DEFAULT_STDEV_MULTIPLIER}
    assert toc == []


# --- pagination ----------------------------------------------------------------
#
# The state-machine tests below inject a synthetic measurer so line counts are
# exact and font-independent: the risk in this port is the page/column state
# machine, not glyph metrics, and a test whose behavior silently changed on a
# machine without Courier New would hide the very bug it exists to catch.
# Measurement against the real fonts is covered separately at the bottom.

def _one_line(text, kind, max_width):
    return 1


def _buckets(*groups):
    return {'letters': [
        {'letter': letter,
         'artists': [{'name': n, 'songs': list(songs)} for n, songs in artists]}
        for letter, artists in groups]}


def _items(page, col):
    """Flat [(kind, label)] for one column, for readable assertions."""
    out = []
    for item in page['columns'][col]:
        if item['isLetter']:
            out.append(('letter', item['letter']))
        elif item['isArtist']:
            out.append(('artist', item['name']))
        else:
            out.append(('song', item['title']))
    return out


def test_paginate_column_width_math():
    # contentWidth = 816 - 0.9*96 - 52.8 = 676.8; (676.8 - 20*(n-1)) / n, floored
    for cols, expected in ((2, 328), (3, 212), (4, 154)):
        result = sb.paginate(_buckets(('A', [('X', ['S'])])), [],
                             column_count=cols, measure=_one_line)
        assert result['colWidth'] == expected, f'{cols} columns'


def test_letter_section_starts_a_fresh_page_not_just_a_column():
    buckets = _buckets(
        ('A', [('Artist A', ['Song A1'])]),
        ('B', [('Artist B', ['Song B1'])]),
    )
    result = sb.paginate(buckets, [], measure=_one_line)

    assert len(result['pages']) == 2
    first, second = result['pages']
    assert first['pageNumber'] == 3
    assert second['pageNumber'] == 4
    assert _items(first, 0) == [
        ('letter', 'A'), ('artist', 'Artist A'), ('song', 'Song A1')]
    # Columns 1 and 2 stay empty despite ~820px of unused room in each --
    # this is what makes it a PAGE break rather than a column break.
    assert first['columns'][1] == []
    assert first['columns'][2] == []
    assert _items(second, 0) == [
        ('letter', 'B'), ('artist', 'Artist B'), ('song', 'Song B1')]


def test_first_letter_does_not_emit_a_leading_blank_page():
    # forceNewPage() must no-op when already at the top of a fresh page, or
    # every book would open on an empty sheet.
    result = sb.paginate(_buckets(('A', [('X', ['S'])])), [], measure=_one_line)
    assert len(result['pages']) == 1
    assert result['pages'][0]['pageNumber'] == 3


def test_orphan_control_moves_artist_and_first_song_together():
    # Heights with the synthetic measurer: letter 78, artist 26, song 16.
    # After the letter (900-78=822) and the filler artist (=796), 48 filler
    # songs leave 28px. The next artist alone WOULD still fit in that 28px --
    # and would then be stranded, because its first song needs 16 more than
    # remains. Orphan control must push the pair to the next column instead.
    filler_songs = [f'Filler Song {i:02d}' for i in range(48)]
    buckets = _buckets(('A', [
        ('Filler Artist', filler_songs),
        ('Orphan Artist', ['Orphan Song']),
    ]))
    result = sb.paginate(buckets, [], measure=_one_line)

    page = result['pages'][0]
    col0, col1 = _items(page, 0), _items(page, 1)
    assert col0[-1] == ('song', 'Filler Song 47'), 'artist name must not be stranded'
    assert ('artist', 'Orphan Artist') not in col0
    assert col1 == [('artist', 'Orphan Artist'), ('song', 'Orphan Song')]


def test_orphan_control_skipped_when_pair_cannot_fit_any_column():
    # An artist name so tall the pair exceeds a whole column (1029+16 > 900).
    # The rule must NOT fire -- firing would advance() forever hunting for
    # room that cannot exist. The pair splits instead: the documented
    # pathological case, not a bug.
    def measure(text, kind, max_width):
        return 60 if text == 'Enormous Artist' else 1

    buckets = _buckets(('A', [('Enormous Artist', ['Its Song'])]))
    result = sb.paginate(buckets, [], measure=measure)

    page = result['pages'][0]
    assert ('artist', 'Enormous Artist') in _items(page, 1)
    assert _items(page, 2) == [('song', 'Its Song')]


def test_advance_rolls_to_a_new_page_after_the_last_column():
    # 3 columns, sized so exactly one artist+song pair (26*17+9 + 16 = 467 of
    # 900) fits per column. The 4th pair therefore has to roll onto a second
    # page rather than a fourth column.
    def measure(text, kind, max_width):
        return 26 if kind == 'artist' else 1

    buckets = _buckets(('A', [(f'Artist {i}', ['S']) for i in range(4)]))
    result = sb.paginate(buckets, [], measure=measure)

    assert len(result['pages']) == 2
    assert result['pages'][1]['pageNumber'] == 4
    assert _items(result['pages'][1], 0) == [('artist', 'Artist 3'), ('song', 'S')]


def test_toc_page_numbers_come_from_pagination():
    buckets = _buckets(
        ('A', [('Artist A', ['Song A1'])]),
        ('B', [('Artist B', ['Song B1'])]),
    )
    toc = [{'name': 'Artist A', 'count': 1}, {'name': 'Artist B', 'count': 1}]
    result = sb.paginate(buckets, toc, measure=_one_line)

    assert result['toc'] == [
        {'name': 'Artist A', 'count': 1, 'page': 3},
        {'name': 'Artist B', 'count': 1, 'page': 4},
    ]


def test_toc_entry_with_no_matching_artist_falls_back_to_first_page():
    result = sb.paginate(_buckets(('A', [('Artist A', ['S'])])),
                         [{'name': 'Ghost Artist', 'count': 99}], measure=_one_line)
    assert result['toc'] == [{'name': 'Ghost Artist', 'count': 99, 'page': 3}]


def test_paginate_empty_library():
    result = sb.paginate({'letters': []}, [], measure=_one_line)
    assert result['toc'] == []
    assert len(result['pages']) == 1
    assert result['pages'][0]['columns'] == [[], [], []]


def test_paginate_is_deterministic():
    # Spec success criterion: the same library twice must give the same page
    # count and the same TOC page numbers, with no run-to-run drift.
    buckets = _buckets(
        ('A', [(f'Artist A{i}', [f'S{j}' for j in range(9)]) for i in range(12)]),
        ('B', [(f'Artist B{i}', [f'S{j}' for j in range(7)]) for i in range(9)]),
    )
    toc = [{'name': 'Artist A3', 'count': 9}, {'name': 'Artist B5', 'count': 7}]
    assert sb.paginate(buckets, toc, measure=_one_line) == \
        sb.paginate(buckets, toc, measure=_one_line)


# --- text measurement against the real fonts -------------------------------------

def _skip_without_courier():
    if sb._font_path('song') is None:
        import pytest
        pytest.skip('Courier New not installed')


def test_measure_width_matches_canvas_monospace_metrics():
    # Courier New advances 0.6em/char, so canvas measureText yields len*6.3
    # at 10.5px. The port must agree closely: naive PIL measurement rounds
    # each glyph to a whole pixel and lands ~11% high, which would inflate
    # wrap counts and silently shift every page number in the book.
    _skip_without_courier()
    title = 'White & Nerdy (Parody of Ridin)'
    assert abs(sb._measure_width(title, 'song') - len(title) * 6.3) < 0.5


def test_measure_width_matches_canvas_for_bold_artist_font():
    _skip_without_courier()
    name = 'A Day To Remember'
    assert abs(sb._measure_width(name, 'artist') - len(name) * 7.5) < 0.5


def test_measure_lines_wraps_long_text():
    _skip_without_courier()
    assert sb._measure_lines('Short', 'song', 200) == 1
    assert sb._measure_lines('x' * 400, 'song', 200) > 1


def test_measure_lines_never_returns_zero_for_empty_text():
    _skip_without_courier()
    assert sb._measure_lines('', 'song', 200) == 1


# --- render_html ---------------------------------------------------------------

def _tiny_paginated():
    buckets = sb.bucket_by_letter([('Aerosmith', ['Dream On', 'Sweet Emotion'])])
    toc = [{'name': 'Aerosmith', 'count': 2}]
    return sb.paginate(buckets, toc, measure=_one_line)


def test_render_html_contains_cover_stats():
    paginated = _tiny_paginated()
    stats = {'totalSongs': 2, 'totalArtists': 1, 'mean': 2.0, 'stdev': 0.0, 'threshold': 2.0}
    html = sb.render_html(paginated, stats)
    assert '1 ARTISTS' in html
    assert '2 SONGS' in html


def test_render_html_contains_toc_entry_with_page_and_count():
    paginated = _tiny_paginated()
    stats = {'totalSongs': 2, 'totalArtists': 1, 'mean': 2.0, 'stdev': 0.0, 'threshold': 2.0}
    html = sb.render_html(paginated, stats)
    assert 'AEROSMITH' in html or 'Aerosmith' in html
    assert 'p.3' in html
    assert '>2<' in html  # the TOC count badge


def test_render_html_contains_letter_banner_and_song_lines():
    paginated = _tiny_paginated()
    stats = {'totalSongs': 2, 'totalArtists': 1, 'mean': 2.0, 'stdev': 0.0, 'threshold': 2.0}
    html = sb.render_html(paginated, stats)
    assert '>A<' in html
    assert 'Dream On' in html
    assert 'Sweet Emotion' in html


def test_render_html_uses_requested_colors():
    paginated = _tiny_paginated()
    stats = {'totalSongs': 2, 'totalArtists': 1, 'mean': 2.0, 'stdev': 0.0, 'threshold': 2.0}
    html = sb.render_html(paginated, stats, accent_color='#3B5998', cover_color='#B5A642')
    assert '#3B5998' in html
    assert '#B5A642' in html


def test_render_html_toc_subhead_shows_the_actual_stdev_multiplier():
    paginated = _tiny_paginated()
    stats = {'totalSongs': 2, 'totalArtists': 1, 'mean': 2.0, 'stdev': 0.0,
             'threshold': 2.0, 'stdevMultiplier': 2.5}
    html = sb.render_html(paginated, stats)
    assert '2.5&sigma;' in html
    assert '1.5&sigma;' not in html


def test_render_html_toc_subhead_defaults_multiplier_when_stats_omits_it():
    # Older callers (or hand-built test stats dicts) that never set
    # stdevMultiplier must still render something sane, not a KeyError.
    paginated = _tiny_paginated()
    stats = {'totalSongs': 2, 'totalArtists': 1, 'mean': 2.0, 'stdev': 0.0, 'threshold': 2.0}
    html = sb.render_html(paginated, stats)
    assert f'{sb.DEFAULT_STDEV_MULTIPLIER}' in html


def test_render_html_escapes_html_in_titles_so_layout_matches_measurement():
    # Real library data contains literal '<i>' etc in titles (e.g. "Parody of
    # <i>Beat It</i>"). paginate() measured those characters as plain text, so
    # the rendered page must show them as literal text too, not interpret them
    # as markup -- otherwise the wrap point Chrome renders no longer matches
    # the wrap point the pagination math computed.
    buckets = sb.bucket_by_letter([('Weird Al', ['<script>alert(1)</script> Song'])])
    paginated = sb.paginate(buckets, [], measure=_one_line)
    stats = {'totalSongs': 1, 'totalArtists': 1, 'mean': 1.0, 'stdev': 0.0, 'threshold': 1.0}
    html = sb.render_html(paginated, stats)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


def test_render_html_no_page_number_on_cover_or_toc():
    # Only content pages get a footer page number; this doubles as a coarse
    # structural check that exactly one content page was rendered.
    paginated = _tiny_paginated()
    stats = {'totalSongs': 2, 'totalArtists': 1, 'mean': 2.0, 'stdev': 0.0, 'threshold': 2.0}
    html = sb.render_html(paginated, stats)
    assert html.count('class="page"') == 3  # cover + toc + 1 content page


# --- Chrome/Edge discovery + PDF export -----------------------------------------

def test_find_browser_prefers_which_over_hardcoded_paths(monkeypatch):
    monkeypatch.setattr(sb.shutil, 'which',
                        lambda name: r'C:\fake\chrome.exe' if name == 'chrome' else None)
    assert sb._find_browser() == r'C:\fake\chrome.exe'


def test_find_browser_falls_back_to_edge(monkeypatch):
    monkeypatch.setattr(sb.shutil, 'which',
                        lambda name: r'C:\fake\msedge.exe' if name == 'msedge' else None)
    assert sb._find_browser() == r'C:\fake\msedge.exe'


def test_find_browser_raises_specific_error_when_neither_found(monkeypatch):
    monkeypatch.setattr(sb.shutil, 'which', lambda name: None)
    monkeypatch.setattr(sb.os.path, 'exists', lambda path: False)
    import pytest
    with pytest.raises(sb.BrowserNotFoundError):
        sb._find_browser()


def test_render_pdf_writes_html_sibling_and_invokes_browser(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        # Simulate the browser actually producing the PDF file, like a real
        # `--print-to-pdf` invocation would.
        out_arg = next(a for a in cmd if a.startswith('--print-to-pdf='))
        Path(out_arg.split('=', 1)[1]).write_bytes(b'%PDF-fake')
        calls.append(cmd)
        return sb.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(sb, '_find_browser', lambda: r'C:\fake\chrome.exe')
    monkeypatch.setattr(sb.subprocess, 'run', fake_run)

    out_pdf = tmp_path / 'Clone Hero Songbook.pdf'
    result_path = sb.render_pdf('<html><body>hi</body></html>', out_pdf)

    assert result_path == out_pdf
    assert out_pdf.exists()
    assert out_pdf.with_suffix('.html').exists()
    assert out_pdf.with_suffix('.html').read_text(encoding='utf-8') == \
        '<html><body>hi</body></html>'
    assert len(calls) == 1
    assert '--headless' in calls[0]
    assert any(a.startswith('--print-to-pdf=') for a in calls[0])


def test_render_pdf_raises_when_browser_missing(tmp_path, monkeypatch):
    def boom():
        raise sb.BrowserNotFoundError('no browser')
    monkeypatch.setattr(sb, '_find_browser', boom)
    import pytest
    with pytest.raises(sb.BrowserNotFoundError):
        sb.render_pdf('<html></html>', tmp_path / 'out.pdf')


# --- generate_songbook orchestrator + CLI ---------------------------------------

def _library(tmp_path):
    _write_song(tmp_path, 'a', 'Sublime', 'Santeria')
    _write_song(tmp_path, 'b', 'Sublime', 'What I Got')
    _write_song(tmp_path, 'c', 'Sum 41', 'Fat Lip')
    return tmp_path


def test_generate_songbook_from_folder_path_cli_mode(tmp_path):
    _library(tmp_path)
    result = sb.generate_songbook(str(tmp_path))

    assert result['pdf_path'] == tmp_path / 'Clone Hero Songbook.pdf'
    assert result['html_path'] == tmp_path / 'Clone Hero Songbook.html'
    assert result['html_path'].exists()
    assert result['stats']['totalArtists'] == 2
    assert result['stats']['totalSongs'] == 3
    assert result['page_count'] >= 1
    try:
        sb._find_browser()
    except sb.BrowserNotFoundError:
        pytest.skip('no Chrome/Edge installed on this machine to assert the PDF itself')
    assert result['pdf_path'].exists()


def test_generate_songbook_from_song_list_gui_mode_skips_rescan(tmp_path):
    _library(tmp_path)
    songs = [_FakeSong(str(p)) for p in tmp_path.iterdir() if p.is_dir()]
    result = sb.generate_songbook(str(tmp_path), songs=songs)
    assert result['stats']['totalArtists'] == 2
    assert result['html_path'].exists()


def test_generate_songbook_passes_through_layout_and_color_options(tmp_path):
    _library(tmp_path)
    result = sb.generate_songbook(str(tmp_path), column_count=2,
                                  binding_margin=1.1, accent_color='#3B5998',
                                  cover_color='#B5A642')
    html_text = result['html_path'].read_text(encoding='utf-8')
    assert '#3B5998' in html_text
    assert '#B5A642' in html_text


def test_generate_songbook_raises_on_empty_library(tmp_path):
    with pytest.raises(sb.EmptyLibraryError):
        sb.generate_songbook(str(tmp_path))


def test_generate_songbook_passes_through_stdev_multiplier(tmp_path):
    _library(tmp_path)
    result = sb.generate_songbook(str(tmp_path), stdev_multiplier=2.5)
    assert result['stats']['stdevMultiplier'] == 2.5
    html_text = result['html_path'].read_text(encoding='utf-8')
    assert '2.5&sigma;' in html_text


def test_parse_args_defaults():
    args = sb.parse_args(['--library-path', 'C:/Songs'])
    assert args.library_path == 'C:/Songs'
    assert args.columns == 3
    assert args.binding_margin == sb.DEFAULT_BINDING_MARGIN
    assert args.accent == 'denim'
    assert args.cover == 'red'
    assert args.stdev_multiplier == sb.DEFAULT_STDEV_MULTIPLIER
    assert args.out is None


def test_parse_args_overrides():
    args = sb.parse_args([
        '--library-path', 'C:/Songs', '--columns', '4', '--binding-margin', '1.1',
        '--accent', 'red', '--cover', 'olive', '--stdev-multiplier', '2.0',
        '--out', 'C:/out.pdf'])
    assert args.stdev_multiplier == 2.0
    assert args.columns == 4
    assert args.binding_margin == 1.1
    assert args.accent == 'red'
    assert args.cover == 'olive'
    assert args.out == 'C:/out.pdf'


# --- album art: palette extraction + legibility clamping ------------------------

def _solid(tmp_path, name, rgb, size=(100, 100)):
    path = tmp_path / name
    Image.new('RGB', size, rgb).save(path)
    return path


def _two_region_image(tmp_path, name, bg_rgb, fg_rgb, fg_fraction, size=(100, 100)):
    """bg_rgb fills the whole canvas; fg_rgb fills a left-hand strip covering
    roughly fg_fraction of the width (and therefore the area, at fixed height)."""
    img = Image.new('RGB', size, bg_rgb)
    fg_width = int(size[0] * fg_fraction)
    for x in range(fg_width):
        for y in range(size[1]):
            img.putpixel((x, y), fg_rgb)
    path = tmp_path / name
    img.save(path)
    return path


def _hls(hexcolor):
    r = int(hexcolor[1:3], 16) / 255
    g = int(hexcolor[3:5], 16) / 255
    b = int(hexcolor[5:7], 16) / 255
    return colorsys.rgb_to_hls(r, g, b)


def _hue_degrees(hexcolor):
    return _hls(hexcolor)[0] * 360


# --- _load_weighted_palette / _score_swatch --------------------------------------

def test_score_swatch_prefers_saturation_over_raw_pixel_count():
    # A vivid color with fewer pixels should still outscore a much larger but
    # totally desaturated (gray) region -- otherwise a large plain background
    # would always win, defeating the point of "dominant COLOR" extraction.
    gray_score = sb._score_swatch(9000, (128, 128, 128))
    red_score = sb._score_swatch(1000, (220, 30, 30))
    assert red_score > gray_score


def test_score_swatch_is_monotonic_in_count_for_equal_saturation():
    low = sb._score_swatch(100, (200, 40, 40))
    high = sb._score_swatch(500, (200, 40, 40))
    assert high > low


def test_load_weighted_palette_finds_both_regions(tmp_path):
    path = _two_region_image(tmp_path, 'two.png', (128, 128, 128), (220, 30, 30), 0.2)
    palette = sb._load_weighted_palette(str(path))
    colors = [rgb for _count, rgb in palette]
    # allow for quantization rounding -- just check something red-ish and
    # something gray-ish both made it into the palette
    assert any(r > 180 and g < 80 and b < 80 for r, g, b in colors), colors
    assert any(abs(r - g) < 20 and abs(g - b) < 20 for r, g, b in colors), colors


# --- _pick_primary_and_accent -----------------------------------------------------

def test_pick_primary_is_the_highest_scored_swatch():
    scored = [(500, (30, 30, 220)), (2000, (220, 30, 30))]  # red scores higher
    primary, accent = sb._pick_primary_and_accent(scored)
    assert primary == (220, 30, 30)
    assert accent == (30, 30, 220)


def test_pick_accent_skips_a_close_hue_third_place_swatch():
    # red (primary), orange (2nd by score, hue close to red -- must be
    # skipped), blue (3rd by score, hue far from red -- must be chosen).
    red = (220, 30, 30)
    orange = (220, 120, 30)     # ~20 degrees of hue from red
    blue = (30, 30, 220)        # ~140 degrees of hue from red
    scored = [(3000, red), (2000, orange), (500, blue)]
    primary, accent = sb._pick_primary_and_accent(scored)
    assert primary == red
    assert accent == blue, 'a hue-close 2nd place must not win over a hue-distant 3rd'


def test_pick_accent_falls_back_to_second_place_when_nothing_clears_the_hue_bar():
    red = (220, 30, 30)
    orange = (220, 120, 30)     # the ONLY other swatch -- hue-close, but must still be used
    scored = [(3000, red), (500, orange)]
    primary, accent = sb._pick_primary_and_accent(scored)
    assert primary == red
    assert accent == orange


def test_pick_accent_from_a_single_swatch_image_reuses_primary():
    scored = [(10000, (100, 150, 200))]
    primary, accent = sb._pick_primary_and_accent(scored)
    assert primary == accent == (100, 150, 200)


# --- _clamp_for_legibility ---------------------------------------------------------

def test_clamp_pure_white_lands_in_band_for_both_roles():
    for role in ('cover', 'accent'):
        clamped = sb._clamp_for_legibility((255, 255, 255), role)
        h, l, s = _hls(clamped)
        min_l, max_l, min_s = sb._LEGIBILITY_BANDS[role]
        assert min_l - 6e-3 <= l <= max_l + 6e-3, (role, clamped, l)
        assert s >= min_s - 6e-3, (role, clamped, s)


def test_clamp_pure_black_lands_in_band_for_both_roles():
    for role in ('cover', 'accent'):
        clamped = sb._clamp_for_legibility((0, 0, 0), role)
        h, l, s = _hls(clamped)
        min_l, max_l, min_s = sb._LEGIBILITY_BANDS[role]
        assert min_l - 6e-3 <= l <= max_l + 6e-3, (role, clamped, l)
        assert s >= min_s - 6e-3, (role, clamped, s)


def test_clamp_desaturated_gray_gets_saturation_raised():
    clamped = sb._clamp_for_legibility((140, 140, 140), 'accent')
    _, _, s = _hls(clamped)
    _, _, min_s = sb._LEGIBILITY_BANDS['accent']
    assert s >= min_s - 6e-3


def test_clamp_already_in_band_color_is_left_close_to_unchanged():
    min_l, max_l, min_s = sb._LEGIBILITY_BANDS['cover']
    mid_l = (min_l + max_l) / 2
    r, g, b = colorsys.hls_to_rgb(0.55, mid_l, max(min_s, 0.6))
    rgb = (round(r * 255), round(g * 255), round(b * 255))
    clamped = sb._clamp_for_legibility(rgb, 'cover')
    ch, cl, cs = _hls(clamped)
    # allow small drift from the 8-bit rgb round-trip, not exact equality
    assert abs(cl - mid_l) < 0.03
    assert cs >= min_s - 6e-3


def test_accent_band_is_darker_and_more_saturated_than_cover_band():
    # accent must work as white-on-accent AND as accent-colored text on cream,
    # a stricter bar than cover just needing to not fight fixed dark text.
    cover_min_l, cover_max_l, cover_min_s = sb._LEGIBILITY_BANDS['cover']
    accent_min_l, accent_max_l, accent_min_s = sb._LEGIBILITY_BANDS['accent']
    assert accent_max_l <= cover_max_l
    assert accent_min_s >= cover_min_s


# --- extract_cover_and_accent_colors (orchestration) ------------------------------

def test_extract_colors_from_a_two_color_image_returns_legible_hex(tmp_path):
    path = _two_region_image(tmp_path, 'art.png', (40, 40, 40), (220, 30, 30), 0.3)
    cover_hex, accent_hex = sb.extract_cover_and_accent_colors(str(path))
    assert cover_hex.startswith('#') and len(cover_hex) == 7
    assert accent_hex.startswith('#') and len(accent_hex) == 7
    for hexcolor, role in ((cover_hex, 'cover'), (accent_hex, 'accent')):
        _, l, s = _hls(hexcolor)
        min_l, max_l, min_s = sb._LEGIBILITY_BANDS[role]
        assert min_l - 6e-3 <= l <= max_l + 6e-3
        assert s >= min_s - 6e-3


def test_extract_colors_is_deterministic(tmp_path):
    path = _two_region_image(tmp_path, 'art.png', (40, 40, 40), (220, 30, 30), 0.3)
    first = sb.extract_cover_and_accent_colors(str(path))
    second = sb.extract_cover_and_accent_colors(str(path))
    assert first == second


def test_extract_colors_raises_album_art_error_on_bad_file(tmp_path):
    bad = tmp_path / 'not_an_image.png'
    bad.write_bytes(b'this is not image data')
    with pytest.raises(sb.AlbumArtError):
        sb.extract_cover_and_accent_colors(str(bad))


def test_extract_colors_raises_album_art_error_on_missing_file(tmp_path):
    with pytest.raises(sb.AlbumArtError):
        sb.extract_cover_and_accent_colors(str(tmp_path / 'nope.png'))
