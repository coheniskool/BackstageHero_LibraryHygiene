import statistics

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
    assert toc == [{'name': 'Prolific Artist', 'count': 9}]


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
                      'stdev': 0.0, 'threshold': 0.0}
    assert toc == []
