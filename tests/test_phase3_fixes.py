# Regression coverage for the Phase 3 adversarial findings.
#
# Every test here corresponds to a finding that was verified by execution
# before being fixed -- both review agents ran without an execution tool, so
# their reports were static traces, and re-running them changed several
# verdicts. These pin the fixed behaviour so it cannot regress silently.

import glob as globmod
import io
import os
import sys
from pathlib import Path

import pytest

import library_common
import chart_rename
import video_repair
import metadata_enrichment
import static_art
import dedupe_report


def _song(folder, name='Song', ini=True, chart=True):
    folder.mkdir(parents=True, exist_ok=True)
    if ini:
        (folder / 'song.ini').write_text(
            f'[song]\nname = {name}\nartist = Someone\n', encoding='utf-8')
    if chart:
        (folder / 'notes.chart').write_text(
            f'[Song]\n{{\n  Name = "{name}"\n  Artist = "Someone"\n}}\n', encoding='utf-8')
    return folder


# --- P3-M3: the pythonw stdout=None guard is now reachable by a test -------
#
# It used to be import-time code in gui.py, structurally unreachable under
# pytest (pytest never sets sys.stdout to None). Deleting it would not have
# failed a single test, yet the suite count was cited as evidence it worked.

def test_ensure_stdio_not_none_replaces_a_none_stdout(monkeypatch):
    monkeypatch.setattr(sys, 'stdout', None)
    monkeypatch.setattr(sys, 'stderr', None)

    opened = library_common.ensure_stdio_not_none()

    assert sorted(opened) == ['stderr', 'stdout']
    assert sys.stdout is not None and sys.stderr is not None
    print('this would have crashed the whole app before the guard')
    sys.stdout.close()
    sys.stderr.close()


def test_ensure_stdio_not_none_leaves_a_real_stdout_alone(monkeypatch):
    sentinel = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', sentinel)

    assert library_common.ensure_stdio_not_none() == []
    assert sys.stdout is sentinel


def test_the_replacement_stream_survives_non_cp1252_text(monkeypatch):
    """The devnull sink used to be opened with the platform default encoding,
    so the very guard meant to keep the app alive could itself raise on a
    song title cp1252 cannot encode."""
    monkeypatch.setattr(sys, 'stdout', None)
    library_common.ensure_stdio_not_none()
    try:
        print('Kryptonite ♥ 東京')          # must not raise
    finally:
        sys.stdout.close()


# --- P3-H1: legacy nested review folders ----------------------------------

def test_a_song_in_an_old_nested_review_folder_is_invisible_to_repair_but_live_to_the_app(tmp_path):
    """The state the migration exists to end, pinned so the asymmetry that
    makes it dangerous stays visible: no repair scan can reach these songs,
    while the app's own song list, downloader and Clone Hero all still do."""
    home = tmp_path / 'Songs'
    home.mkdir()
    _song(home / 'Healthy', 'Healthy')
    _song(home / '_needs_review' / 'Kryptonite', 'Kryptonite')

    found = [p.name for p in library_common.iter_song_folders(home)]
    assert found == ['Healthy']                       # repair cannot see it

    app_sees = [os.path.basename(os.path.dirname(p)) for p in globmod.iglob(
        os.path.join(globmod.escape(str(home)), '**', 'song.ini'), recursive=True)]
    assert 'Kryptonite' in app_sees                   # but the app still does


def test_migration_moves_legacy_folders_out_to_a_sibling(tmp_path):
    home = tmp_path / 'Songs'
    home.mkdir()
    _song(home / 'Healthy', 'Healthy')
    _song(home / '_needs_review' / 'Kryptonite', 'Kryptonite')
    _song(home / '_duplicates_review' / 'Dupe', 'Dupe')

    counts = library_common.migrate_legacy_review_folders(home)

    assert counts.get('moved') == 2
    assert (tmp_path / 'Songs_needs_review' / 'Kryptonite' / 'song.ini').exists()
    assert (tmp_path / 'Songs_duplicates_review' / 'Dupe' / 'song.ini').exists()
    assert not (home / '_needs_review').exists()
    assert not (home / '_duplicates_review').exists()
    assert (home / 'Healthy' / 'song.ini').exists()   # untouched


def test_migration_dry_run_moves_nothing(tmp_path):
    home = tmp_path / 'Songs'
    home.mkdir()
    _song(home / '_needs_review' / 'Kryptonite', 'Kryptonite')
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))

    counts = library_common.migrate_legacy_review_folders(home, dry_run=True)

    assert counts.get('would_move') == 1
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before


def test_migration_never_overwrites_a_name_that_already_exists(tmp_path):
    """Merging into an existing sibling review folder must not clobber a
    same-named song already sitting there."""
    home = tmp_path / 'Songs'
    home.mkdir()
    _song(home / '_needs_review' / 'Kryptonite', 'Nested copy')
    existing = tmp_path / 'Songs_needs_review' / 'Kryptonite'
    _song(existing, 'Already there')

    counts = library_common.migrate_legacy_review_folders(home)

    assert counts.get('conflict') == 1
    assert counts.get('moved') is None
    assert 'Already there' in (existing / 'song.ini').read_text(encoding='utf-8')
    assert (home / '_needs_review' / 'Kryptonite').exists()   # left in place


def test_migration_is_a_no_op_on_a_clean_library(tmp_path):
    home = tmp_path / 'Songs'
    home.mkdir()
    _song(home / 'Healthy', 'Healthy')
    assert library_common.migrate_legacy_review_folders(home) == {}


def test_migration_finds_the_predecessors_camelcase_review_folder(tmp_path):
    """Found in a real library during Phase 4: this project's predecessor
    (clonehero-video-downloader) named it '_NeedsReview', so an exact-literal
    match on '_needs_review' walked straight past three genuinely stranded
    songs -- two of which the app cannot even see, because their .ini is
    ID-suffixed and the **/song.ini glob never matches it."""
    home = tmp_path / 'Songs'
    home.mkdir()
    legacy = home / '_NeedsReview'
    _song(legacy / 'Styx - Mr. Roboto', 'Mr. Roboto', ini=False, chart=False)
    (legacy / 'Styx - Mr. Roboto' / 'song_819.ini').write_text(
        '[song]\nname = Mr. Roboto\n', encoding='utf-8')
    (legacy / 'Styx - Mr. Roboto' / 'notes_454.chart').write_text('[Song]\n{\n}\n', encoding='utf-8')
    (legacy / 'Styx - Mr. Roboto' / 'video_meta.json').write_text('{}', encoding='utf-8')

    assert library_common.find_legacy_review_folders(home) == [legacy]

    counts = library_common.migrate_legacy_review_folders(home)

    assert counts.get('moved') == 1
    assert (tmp_path / 'Songs_needs_review' / 'Styx - Mr. Roboto' / 'song_819.ini').exists()
    assert not legacy.exists()


def test_camelcase_and_snake_case_land_in_the_same_sibling_folder(tmp_path):
    """Two tools' naming for one concept must not produce two review folders
    for the user to hunt through."""
    home = tmp_path / 'Songs'
    home.mkdir()
    _song(home / '_NeedsReview' / 'From Old Tool', 'A')
    _song(home / '_needs_review' / 'From This Tool', 'B')

    library_common.migrate_legacy_review_folders(home)

    assert (tmp_path / 'Songs_needs_review' / 'From Old Tool' / 'song.ini').exists()
    assert (tmp_path / 'Songs_needs_review' / 'From This Tool' / 'song.ini').exists()
    assert not (tmp_path / 'Songs_NeedsReview').exists()


def test_migration_ignores_a_users_own_underscore_folder(tmp_path):
    """Only the two literal legacy names are migrated -- a folder that merely
    starts with '_' is the user's own business."""
    home = tmp_path / 'Songs'
    home.mkdir()
    mine = home / '_my stuff'
    _song(mine / 'Something', 'Something')

    library_common.migrate_legacy_review_folders(home)

    assert (mine / 'Something' / 'song.ini').exists()


# --- P3-M1/M2: every scan is recursive, and none dies on a unicode name ----

SCANS = [
    ('video_repair', video_repair.scan_and_repair_video_library),
    ('metadata_enrichment', metadata_enrichment.enrich_song_ini_metadata_library),
    ('static_art', static_art.scan_and_convert_static_art_library),
    ('chart_rename', chart_rename.scan_and_fix_chart_library),
    ('dedupe_report', dedupe_report.generate_dedupe_report),
]


@pytest.mark.parametrize('name,scan', SCANS, ids=[s[0] for s in SCANS])
def test_no_scan_relocates_a_pack_folder_on_a_nested_library(tmp_path, name, scan, monkeypatch):
    """A pack folder is a container, not a broken song. dedupe in particular
    used to fuzzy-match two similarly-named packs into a duplicate group on
    folder name alone, with no song.ini in either."""
    monkeypatch.setattr(dedupe_report.chorus_client, 'search_by_artist_title',
                        lambda artist, title: None)
    home = tmp_path / 'Songs'
    home.mkdir()
    _song(home / 'Guitar Hero III' / 'Song A', 'Song A')
    _song(home / 'Guitar Hero III (2)' / 'Song A', 'Song A')

    scan(home, dry_run=True)

    assert (home / 'Guitar Hero III' / 'Song A' / 'song.ini').exists()
    assert (home / 'Guitar Hero III (2)' / 'Song A' / 'song.ini').exists()
    assert not (tmp_path / 'Songs_duplicates_review').exists()
    assert not (tmp_path / 'Songs_needs_review').exists()


@pytest.mark.parametrize('name,scan', SCANS, ids=[s[0] for s in SCANS])
def test_no_scan_dies_on_a_song_name_cp1252_cannot_encode(tmp_path, name, scan, monkeypatch):
    """The fix was originally wired into chart_rename only, while three
    sibling tools print folder names identically from the same dialog."""
    monkeypatch.setattr(dedupe_report.chorus_client, 'search_by_artist_title',
                        lambda artist, title: None)
    monkeypatch.setattr(metadata_enrichment.chorus_client, 'search_by_artist_title',
                        lambda artist, title: None)
    home = tmp_path / 'Songs'
    home.mkdir()
    for title in ('Kryptonite ♥', '東京ソング'):
        folder = _song(home / title, 'x', chart=False)
        (folder / 'video.mp4').write_bytes(b'not a real video')

    real = sys.stdout
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='strict')
    try:
        scan(home, dry_run=True)          # must not raise UnicodeEncodeError
    finally:
        sys.stdout = real


def test_no_ffmpeg_call_decodes_child_output_with_the_locale_codec():
    """subprocess's text=True decodes with the LOCALE encoding -- cp1252 on a
    default Windows box. ffprobe echoes the file path in its messages, so a
    song folder named with a heart or CJK text produced bytes cp1252 could not
    decode, and the failure happened on subprocess's internal reader thread
    where no caller could catch it: the probe just came back empty.

    Surfaced as a PytestUnhandledThreadExceptionWarning during the unicode
    scan tests, which is easy to scroll past -- hence this test rather than a
    reliance on reading warning output.
    """
    import re
    sources = ['VideoDownload.py', 'video_repair.py', 'static_art.py',
               'library_common.py', 'chart_rename.py', 'dedupe_report.py']
    offenders = []
    for name in sources:
        path = Path(library_common.__file__).parent / name
        if not path.exists():
            continue
        for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith('#'):        # prose about the bug, not the bug
                continue
            if re.search(r'\btext\s*=\s*True', line) and 'TEXT_UTF8' not in line:
                offenders.append(f'{name}:{n}: {stripped}')
    assert not offenders, (
        'these decode child output with the locale codec; use '
        '**library_common.TEXT_UTF8 instead:\n  ' + '\n  '.join(offenders))


def test_text_utf8_replaces_undecodable_bytes_instead_of_raising():
    import subprocess
    result = subprocess.run(
        [sys.executable, '-c',
         r'import sys; sys.stdout.buffer.write(b"ok \x9d\xff end")'],
        capture_output=True, **library_common.TEXT_UTF8)
    assert 'ok' in result.stdout and 'end' in result.stdout


def test_a_folder_holding_only_a_video_still_counts_as_a_song_folder(tmp_path):
    """video_repair's whole job is folders that have a video, including ones
    whose chart files are missing or misnamed."""
    home = tmp_path / 'Songs'
    home.mkdir()
    bare = home / 'Just A Video'
    bare.mkdir()
    (bare / 'video.mp4').write_bytes(b'x')

    assert [p.name for p in library_common.iter_song_folders(home)] == ['Just A Video']
