import json
import wave

import chart_rename as cr


def _write_wav(path, seconds, sr=8000):
    n = int(seconds * sr)
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b'\x00\x00' * n)


CHART_TEXT = '[Song]\n{\n  Name = "Kryptonite"\n  Artist = "3 Doors Down"\n}\n'


# --- scan_song_folder_chart_names --------------------------------------------------

def test_scan_chart_names_ok_when_literal(tmp_path):
    (tmp_path / 'song.ini').write_text('[song]\n', encoding='utf-8')
    (tmp_path / 'notes.chart').write_text(CHART_TEXT, encoding='utf-8')
    result = cr.scan_song_folder_chart_names(tmp_path)
    assert result['status'] == 'ok'


def test_scan_chart_names_id_suffixed(tmp_path):
    (tmp_path / 'song_2400.ini').write_text('[song]\n', encoding='utf-8')
    (tmp_path / 'notes_454.chart').write_text(CHART_TEXT, encoding='utf-8')
    result = cr.scan_song_folder_chart_names(tmp_path)
    assert result['status'] == 'id_suffixed'
    assert 'song_2400.ini' in result['detail']
    assert 'notes_454.chart' in result['detail']


def test_scan_chart_names_no_ini(tmp_path):
    (tmp_path / 'notes.chart').write_text(CHART_TEXT, encoding='utf-8')
    assert cr.scan_song_folder_chart_names(tmp_path)['status'] == 'no_ini'


def test_scan_chart_names_no_chart_file(tmp_path):
    (tmp_path / 'song.ini').write_text('[song]\n', encoding='utf-8')
    assert cr.scan_song_folder_chart_names(tmp_path)['status'] == 'no_chart_file'


def test_scan_chart_names_ambiguous_multiple_ini(tmp_path):
    (tmp_path / 'song.ini').write_text('[song]\n', encoding='utf-8')
    (tmp_path / 'song_2400.ini').write_text('[song]\n', encoding='utf-8')
    (tmp_path / 'notes.chart').write_text(CHART_TEXT, encoding='utf-8')
    assert cr.scan_song_folder_chart_names(tmp_path)['status'] == 'ambiguous'


def test_scan_chart_names_ambiguous_multiple_chart_candidates(tmp_path):
    (tmp_path / 'song.ini').write_text('[song]\n', encoding='utf-8')
    (tmp_path / 'notes.chart').write_text(CHART_TEXT, encoding='utf-8')
    (tmp_path / 'notes_99.chart').write_text(CHART_TEXT, encoding='utf-8')
    assert cr.scan_song_folder_chart_names(tmp_path)['status'] == 'ambiguous'


# --- verify_chart_content_match --------------------------------------------------

def test_verify_chart_content_match_passes_on_matching_name_and_artist(tmp_path):
    (tmp_path / 'notes.chart').write_text(CHART_TEXT, encoding='utf-8')
    matched, reason = cr.verify_chart_content_match(
        tmp_path, {'name': 'Kryptonite', 'artist': '3 Doors Down'})
    assert matched is True


def test_verify_chart_content_match_fails_when_name_mismatches_despite_artist_match(tmp_path):
    """The real Mr. Roboto case: Artist matches (Styx) but Name is a
    completely different song -- an OR check would wrongly pass this."""
    (tmp_path / 'notes.chart').write_text(
        '[Song]\n{\n  Name = "Rock & Roll Feeling"\n  Artist = "Styx"\n}\n', encoding='utf-8')
    matched, reason = cr.verify_chart_content_match(
        tmp_path, {'name': 'Mr. Roboto', 'artist': 'Styx'})
    assert matched is False
    assert 'name score' in reason


def test_verify_chart_content_match_mid_passes_within_duration_tolerance(tmp_path):
    (tmp_path / 'notes.mid').write_bytes(b'fake midi bytes')
    _write_wav(tmp_path / 'song.wav', seconds=180.0)
    matched, reason = cr.verify_chart_content_match(
        tmp_path, {'song_length': str(180 * 1000)})
    assert matched is True


def test_verify_chart_content_match_mid_fails_outside_duration_tolerance(tmp_path):
    (tmp_path / 'notes.mid').write_bytes(b'fake midi bytes')
    _write_wav(tmp_path / 'song.wav', seconds=180.0)
    matched, reason = cr.verify_chart_content_match(
        tmp_path, {'song_length': str(60 * 1000)})  # way off from the real 180s
    assert matched is False


def test_verify_chart_content_match_no_chart_or_mid_fails(tmp_path):
    matched, reason = cr.verify_chart_content_match(tmp_path, {})
    assert matched is False


# --- scan_song_folder_audio_stems --------------------------------------------------

def test_scan_audio_stems_ok_when_no_stems(tmp_path):
    assert cr.scan_song_folder_audio_stems(tmp_path)['status'] == 'ok'


def test_scan_audio_stems_ok_when_literally_named(tmp_path):
    (tmp_path / 'song.ogg').write_bytes(b'x')
    (tmp_path / 'guitar.ogg').write_bytes(b'x')
    assert cr.scan_song_folder_audio_stems(tmp_path)['status'] == 'ok'


def test_scan_audio_stems_rename_candidate_for_sole_id_suffixed_file(tmp_path):
    (tmp_path / 'song_1877.ogg').write_bytes(b'x')
    result = cr.scan_song_folder_audio_stems(tmp_path)
    assert result['status'] == 'rename_candidate'
    assert 'song' in result['detail']


def test_scan_audio_stems_needs_review_when_role_has_multiple_candidates(tmp_path):
    (tmp_path / 'guitar.ogg').write_bytes(b'x')
    (tmp_path / 'guitar_2.ogg').write_bytes(b'x')
    assert cr.scan_song_folder_audio_stems(tmp_path)['status'] == 'needs_review'


def test_scan_audio_stems_vocals_1_is_a_literal_role_not_id_suffixed_vocals(tmp_path):
    """vocals_1.ogg is a real harmony-vocals stem role, not vocals+ID '1'."""
    (tmp_path / 'vocals_1.ogg').write_bytes(b'x')
    assert cr.scan_song_folder_audio_stems(tmp_path)['status'] == 'ok'


# --- scan_song_folder_album_art --------------------------------------------------

def test_scan_album_art_ok_when_absent(tmp_path):
    assert cr.scan_song_folder_album_art(tmp_path)['status'] == 'ok'


def test_scan_album_art_ok_when_literal(tmp_path):
    (tmp_path / 'album.png').write_bytes(b'x')
    assert cr.scan_song_folder_album_art(tmp_path)['status'] == 'ok'


def test_scan_album_art_rename_candidate_when_sole_id_suffixed(tmp_path):
    (tmp_path / 'album_827.png').write_bytes(b'x')
    result = cr.scan_song_folder_album_art(tmp_path)
    assert result['status'] == 'rename_candidate'
    assert result['detail'] == 'album_827.png'


def test_scan_album_art_needs_review_when_multiple_candidates(tmp_path):
    (tmp_path / 'album_1.png').write_bytes(b'x')
    (tmp_path / 'album_2.jpg').write_bytes(b'x')
    assert cr.scan_song_folder_album_art(tmp_path)['status'] == 'needs_review'


# --- apply_stem_renames --------------------------------------------------
#
# Regression coverage for a real bug found on the user's actual library
# (2026-07-18): scan_song_folder_audio_stems()'s own docstring calls a sole
# ID-suffixed candidate "safe to rename", but nothing ever performed that
# rename -- the folder was relocated to needs_review instead, even with
# zero genuine ambiguity anywhere. apply_stem_renames() is the fix.

def test_apply_stem_renames_ok_when_nothing_to_do(tmp_path):
    result = cr.apply_stem_renames(tmp_path, {})
    assert result['status'] == 'ok'


def test_apply_stem_renames_renames_sole_non_song_stem_no_duration_check(tmp_path):
    (tmp_path / 'guitar_1760.ogg').write_bytes(b'x')

    result = cr.apply_stem_renames(tmp_path, {})

    assert result['status'] == 'ok'
    assert (tmp_path / 'guitar.ogg').exists()
    assert not (tmp_path / 'guitar_1760.ogg').exists()


def test_apply_stem_renames_song_role_renamed_within_duration_tolerance(tmp_path):
    _write_wav(tmp_path / 'song_1877.ogg', seconds=180.0)

    result = cr.apply_stem_renames(tmp_path, {'song_length': str(180 * 1000)})

    assert result['status'] == 'ok'
    assert (tmp_path / 'song.ogg').exists()


def test_apply_stem_renames_song_role_blocked_outside_duration_tolerance(tmp_path):
    _write_wav(tmp_path / 'song_1877.ogg', seconds=180.0)

    result = cr.apply_stem_renames(tmp_path, {'song_length': str(60 * 1000)})  # way off

    assert result['status'] == 'needs_review'
    assert (tmp_path / 'song_1877.ogg').exists()  # never renamed
    assert not (tmp_path / 'song.ogg').exists()


def test_apply_stem_renames_song_role_renamed_when_no_song_length_available(tmp_path):
    """Absence of song_length (or an unparseable value) doesn't block an
    otherwise-safe rename -- there's nothing to check against, so this
    falls through rather than treating "can't verify" as "must reject"."""
    _write_wav(tmp_path / 'song_1877.ogg', seconds=180.0)

    result = cr.apply_stem_renames(tmp_path, {})  # no song_length key at all

    assert result['status'] == 'ok'
    assert (tmp_path / 'song.ogg').exists()


def test_apply_stem_renames_delegates_genuine_ambiguity_untouched(tmp_path):
    (tmp_path / 'guitar_1760.ogg').write_bytes(b'x')
    (tmp_path / 'guitar_1846.ogg').write_bytes(b'x')

    result = cr.apply_stem_renames(tmp_path, {})

    assert result['status'] == 'needs_review'
    assert (tmp_path / 'guitar_1760.ogg').exists()
    assert (tmp_path / 'guitar_1846.ogg').exists()


def test_apply_stem_renames_literal_and_suffixed_coexisting_is_ambiguous(tmp_path):
    """A literal guitar.ogg coexisting with guitar_1760.ogg is caught by the
    same len(files) > 1 ambiguity check as two suffixed candidates -- both
    match role 'guitar' via _match_stem_role(), so this is the ambiguous
    branch, not the (separately tested) collision guard."""
    (tmp_path / 'guitar_1760.ogg').write_bytes(b'x')
    (tmp_path / 'guitar.ogg').write_bytes(b'already here')

    result = cr.apply_stem_renames(tmp_path, {})

    assert result['status'] == 'needs_review'
    assert (tmp_path / 'guitar_1760.ogg').exists()
    assert (tmp_path / 'guitar.ogg').read_bytes() == b'already here'


def test_apply_stem_renames_collision_guard_against_race(tmp_path, monkeypatch):
    """The collision check is a pure TOCTOU defense, not reachable via any
    static fixture: a literal target that already exists would always be
    grouped into the same by_role bucket as the ID-suffixed source (both
    match the same role via _match_stem_role()), tripping the ambiguity
    check first. Simulates the only way it can actually occur: the target
    appearing between classification and the rename itself."""
    (tmp_path / 'guitar_1760.ogg').write_bytes(b'x')
    target = tmp_path / 'guitar.ogg'
    real_exists = type(target).exists

    def _fake_exists(self, *a, **k):
        if self == target:
            return True  # simulate guitar.ogg appearing mid-call
        return real_exists(self, *a, **k)

    monkeypatch.setattr(type(target), 'exists', _fake_exists)

    result = cr.apply_stem_renames(tmp_path, {})

    assert result['status'] == 'needs_review'
    assert (tmp_path / 'guitar_1760.ogg').exists()  # source untouched


def test_apply_stem_renames_ambiguous_role_does_not_block_a_different_safe_role(tmp_path):
    """The real Kryptonite-shaped case, and the actual bug fix: an
    unambiguous, safely-renameable 'song' stem alongside a genuinely
    ambiguous 'rhythm' role. The two are independent -- song.ogg gets
    renamed regardless of rhythm's ambiguity, and the folder is still
    flagged needs_review for rhythm specifically."""
    _write_wav(tmp_path / 'song_1877.ogg', seconds=180.0)
    (tmp_path / 'rhythm_1315.ogg').write_bytes(b'x')
    (tmp_path / 'rhythm_647.ogg').write_bytes(b'x')

    result = cr.apply_stem_renames(tmp_path, {'song_length': str(180 * 1000)})

    assert result['status'] == 'needs_review'  # rhythm is still genuinely ambiguous
    assert 'rhythm' in result['detail']
    assert (tmp_path / 'song.ogg').exists()  # but song WAS renamed regardless
    assert not (tmp_path / 'song_1877.ogg').exists()


def test_apply_stem_renames_dry_run_touches_nothing(tmp_path):
    (tmp_path / 'guitar_1760.ogg').write_bytes(b'x')

    result = cr.apply_stem_renames(tmp_path, {}, dry_run=True)

    assert result['status'] == 'ok'
    assert '(dry-run, not applied)' in result['detail']
    assert (tmp_path / 'guitar_1760.ogg').exists()
    assert not (tmp_path / 'guitar.ogg').exists()


# --- apply_album_art_rename --------------------------------------------------

def test_apply_album_art_rename_ok_when_nothing_to_do(tmp_path):
    assert cr.apply_album_art_rename(tmp_path)['status'] == 'ok'


def test_apply_album_art_rename_renames_sole_candidate(tmp_path):
    (tmp_path / 'album_827.png').write_bytes(b'x')

    result = cr.apply_album_art_rename(tmp_path)

    assert result['status'] == 'ok'
    assert (tmp_path / 'album.png').exists()
    assert not (tmp_path / 'album_827.png').exists()


def test_apply_album_art_rename_delegates_genuine_ambiguity_untouched(tmp_path):
    (tmp_path / 'album_1.png').write_bytes(b'x')
    (tmp_path / 'album_2.jpg').write_bytes(b'x')

    result = cr.apply_album_art_rename(tmp_path)

    assert result['status'] == 'needs_review'
    assert (tmp_path / 'album_1.png').exists()
    assert (tmp_path / 'album_2.jpg').exists()


def test_apply_album_art_rename_literal_and_suffixed_coexisting_is_ambiguous(tmp_path):
    """A literal album.png coexisting with album_827.png is caught by
    scan_song_folder_album_art()'s own len(candidates) > 1 check -- both
    match the album-art pattern, so this is the ambiguous branch, not the
    (separately tested) collision guard."""
    (tmp_path / 'album_827.png').write_bytes(b'x')
    (tmp_path / 'album.png').write_bytes(b'already here')

    result = cr.apply_album_art_rename(tmp_path)

    assert result['status'] == 'needs_review'
    assert (tmp_path / 'album_827.png').exists()
    assert (tmp_path / 'album.png').read_bytes() == b'already here'


def test_apply_album_art_rename_collision_guard_against_race(tmp_path, monkeypatch):
    """The collision check is a pure TOCTOU defense: a literal target that
    already exists would always be picked up by
    scan_song_folder_album_art()'s own ambiguity check first (both match
    the same pattern). Simulates the only way it can actually occur: the
    target appearing between detection and the rename itself."""
    (tmp_path / 'album_827.png').write_bytes(b'x')
    target = tmp_path / 'album.png'
    real_exists = type(target).exists

    def _fake_exists(self, *a, **k):
        if self == target:
            return True  # simulate album.png appearing mid-call
        return real_exists(self, *a, **k)

    monkeypatch.setattr(type(target), 'exists', _fake_exists)

    result = cr.apply_album_art_rename(tmp_path)

    assert result['status'] == 'needs_review'
    assert (tmp_path / 'album_827.png').exists()  # source untouched


def test_apply_album_art_rename_dry_run_touches_nothing(tmp_path):
    (tmp_path / 'album_827.png').write_bytes(b'x')

    result = cr.apply_album_art_rename(tmp_path, dry_run=True)

    assert result['status'] == 'ok'
    assert '(dry-run, not applied)' in result['detail']
    assert (tmp_path / 'album_827.png').exists()
    assert not (tmp_path / 'album.png').exists()


# --- is_sng_packaged --------------------------------------------------

def test_is_sng_packaged_true(tmp_path):
    (tmp_path / 'chart.sng').write_bytes(b'x')
    assert cr.is_sng_packaged(tmp_path) is True


def test_is_sng_packaged_false(tmp_path):
    assert cr.is_sng_packaged(tmp_path) is False


# --- process_chart_folder_names --------------------------------------------------

def test_process_chart_folder_names_renames_after_verified_match(tmp_path):
    (tmp_path / 'song_2400.ini').write_text(
        '[song]\nname = Kryptonite\nartist = 3 Doors Down\n', encoding='utf-8')
    (tmp_path / 'notes_454.chart').write_text(CHART_TEXT, encoding='utf-8')

    result = cr.process_chart_folder_names(tmp_path)

    assert result['status'] == 'confirmed_ok'
    assert (tmp_path / 'song.ini').exists()
    assert (tmp_path / 'notes.chart').exists()
    assert not (tmp_path / 'song_2400.ini').exists()


def test_process_chart_folder_names_needs_review_on_verification_failure(tmp_path):
    (tmp_path / 'song_2400.ini').write_text(
        '[song]\nname = Totally Different Song\nartist = Someone Else\n', encoding='utf-8')
    (tmp_path / 'notes_454.chart').write_text(CHART_TEXT, encoding='utf-8')

    result = cr.process_chart_folder_names(tmp_path)

    assert result['status'] == 'needs_review'
    assert (tmp_path / 'song_2400.ini').exists()  # never renamed on unconfirmed content


def test_process_chart_folder_names_collision_guard(tmp_path, monkeypatch):
    """The collision guard is a pure TOCTOU defense, not reachable via any
    static fixture: scan_song_folder_chart_names()'s own ambiguous-detection
    (multiple *.ini on disk) always fires first if a colliding song.ini is
    physically present at call time -- process_chart_folder_names() never
    even reaches the id_suffixed/collision-guard branch in that case. This
    simulates the only way the guard's condition can actually occur: the
    target file appearing between the detection scan and the rename
    attempt, later in the same call."""
    (tmp_path / 'song_2400.ini').write_text(
        '[song]\nname = Kryptonite\nartist = 3 Doors Down\n', encoding='utf-8')
    (tmp_path / 'notes_454.chart').write_text(CHART_TEXT, encoding='utf-8')

    target_ini = tmp_path / 'song.ini'
    real_exists = type(target_ini).exists

    def _fake_exists(self, *a, **k):
        if self == target_ini:
            return True  # simulate song.ini appearing mid-call
        return real_exists(self, *a, **k)

    monkeypatch.setattr(type(target_ini), 'exists', _fake_exists)

    result = cr.process_chart_folder_names(tmp_path)

    assert result['status'] == 'needs_review'
    assert 'song.ini already exists' in result['detail']
    assert (tmp_path / 'song_2400.ini').exists()  # source left untouched


def test_process_chart_folder_names_dry_run_touches_nothing(tmp_path):
    (tmp_path / 'song_2400.ini').write_text(
        '[song]\nname = Kryptonite\nartist = 3 Doors Down\n', encoding='utf-8')
    (tmp_path / 'notes_454.chart').write_text(CHART_TEXT, encoding='utf-8')

    result = cr.process_chart_folder_names(tmp_path, dry_run=True)

    assert result['status'] == 'confirmed_ok'
    assert '(dry-run, not applied)' in result['detail']
    assert (tmp_path / 'song_2400.ini').exists()
    assert not (tmp_path / 'song.ini').exists()


def test_process_chart_folder_names_stray_chart_file_never_verified_or_renamed(tmp_path):
    """Regression: verify/rename must target the notes-pattern file detection
    selected, never a broad *.chart glob. A stray 'AAA.chart' sorts before
    'notes_454.chart' -- the old inherited code would have verified AAA's
    (deliberately wrong) content and either flagged the folder needs_review
    or renamed the wrong file to notes.chart."""
    (tmp_path / 'song_2400.ini').write_text(
        '[song]\nname = Kryptonite\nartist = 3 Doors Down\n', encoding='utf-8')
    (tmp_path / 'notes_454.chart').write_text(CHART_TEXT, encoding='utf-8')  # correct content
    (tmp_path / 'AAA.chart').write_text(
        '[Song]\n{\n  Name = "Wrong Song"\n  Artist = "Wrong Artist"\n}\n', encoding='utf-8')

    result = cr.process_chart_folder_names(tmp_path)

    assert result['status'] == 'confirmed_ok'
    assert 'notes_454.chart -> notes.chart' in result['detail']
    assert (tmp_path / 'notes.chart').read_text(encoding='utf-8') == CHART_TEXT  # the RIGHT file landed
    assert (tmp_path / 'AAA.chart').exists()  # stray file left untouched
    assert not (tmp_path / 'notes_454.chart').exists()


def test_process_chart_folder_names_skips_sng_packaged(tmp_path):
    (tmp_path / 'chart.sng').write_bytes(b'x')
    result = cr.process_chart_folder_names(tmp_path)
    assert result['status'] == 'skipped_sng'


# --- load/save_chart_rename_status --------------------------------------------------

def test_save_and_load_chart_rename_status_round_trips(tmp_path):
    cr.save_chart_rename_status(tmp_path, 'confirmed_ok', 'all good')
    assert cr.load_chart_rename_status(tmp_path) == 'confirmed_ok'


def test_load_chart_rename_status_none_when_absent(tmp_path):
    assert cr.load_chart_rename_status(tmp_path) is None


def test_save_chart_rename_status_merges_with_existing_metadata(tmp_path):
    metadata_path = tmp_path / cr.CHART_RENAME_METADATA_FILENAME
    metadata_path.write_text(json.dumps({'offset_ms': 1234}), encoding='utf-8')

    cr.save_chart_rename_status(tmp_path, 'confirmed_ok')

    data = json.loads(metadata_path.read_text(encoding='utf-8'))
    assert data['offset_ms'] == 1234  # untouched
    assert data['chart_rename_status'] == 'confirmed_ok'


# --- process_song_folder_for_chart_rename --------------------------------------------------

def test_process_song_folder_confirmed_ok_when_everything_passes(tmp_path):
    home = tmp_path
    song = home / 'Kryptonite'
    song.mkdir()
    (song / 'song.ini').write_text('[song]\nname = Kryptonite\nartist = 3 Doors Down\n', encoding='utf-8')
    (song / 'notes.chart').write_text(CHART_TEXT, encoding='utf-8')

    result = cr.process_song_folder_for_chart_rename(song, home)

    assert result['status'] == 'confirmed_ok'
    assert cr.load_chart_rename_status(song) == 'confirmed_ok'


def test_process_song_folder_real_kryptonite_shape_renames_safe_stems_but_still_flags_ambiguity(tmp_path):
    """Reproduces the exact real-library bug (2026-07-18): a folder whose
    .ini/.chart are already literal, with one unambiguous 'song' stem
    (safely renameable) alongside genuinely ambiguous 'guitar' (4
    candidates) and 'rhythm' (2 candidates) roles, plus a sole ID-suffixed
    album-art file. Before the fix, the unambiguous song/album-art files
    were never renamed and the whole folder was relocated untouched.
    Correct behavior: song.ogg and album.png get renamed in place, and the
    folder still relocates to review -- for the guitar/rhythm ambiguity
    only -- carrying the already-fixed files with it."""
    home = tmp_path / 'Library'
    home.mkdir()
    song = home / '3 Doors Down - Kryptonite'
    song.mkdir()
    (song / 'song.ini').write_text('[song]\nname = Kryptonite\nartist = 3 Doors Down\n', encoding='utf-8')
    (song / 'notes.mid').write_bytes(b'fake midi bytes')
    (song / 'song_1877.ogg').write_bytes(b'x')          # sole candidate -- safe to rename
    (song / 'album_827.png').write_bytes(b'x')           # sole candidate -- safe to rename
    (song / 'guitar_1760.ogg').write_bytes(b'x')         # 4-way ambiguous
    (song / 'guitar_1846.ogg').write_bytes(b'x')
    (song / 'guitar_2051.ogg').write_bytes(b'x')
    (song / 'guitar_925.ogg').write_bytes(b'x')
    (song / 'rhythm_1315.ogg').write_bytes(b'x')         # 2-way ambiguous
    (song / 'rhythm_647.ogg').write_bytes(b'x')

    result = cr.process_song_folder_for_chart_rename(song, home)

    assert result['status'] == 'needs_review'  # guitar/rhythm ambiguity is real, correctly flagged
    assert not song.exists()  # relocated

    relocated = tmp_path / 'Library_needs_review' / '3 Doors Down - Kryptonite'
    assert relocated.exists()
    # the unambiguous ones got fixed BEFORE relocation -- this is the bug fix
    assert (relocated / 'song.ogg').exists()
    assert (relocated / 'album.png').exists()
    assert not (relocated / 'song_1877.ogg').exists()
    assert not (relocated / 'album_827.png').exists()
    # the genuinely ambiguous ones are exactly why it still needed review
    assert (relocated / 'guitar_1760.ogg').exists()
    assert (relocated / 'rhythm_1315.ogg').exists()


def test_process_song_folder_relocates_on_failure(tmp_path):
    home = tmp_path / 'Library'
    home.mkdir()
    song = home / 'Some Song'
    song.mkdir()
    (song / 'song.ini').write_text('[song]\nname = Some Song\n', encoding='utf-8')
    # no chart/mid file at all -> no_chart_file -> needs_review

    result = cr.process_song_folder_for_chart_rename(song, home)

    assert result['status'] == 'needs_review'
    assert not song.exists()
    # relocation lands OUTSIDE home (a sibling), not nested inside it --
    # Clone Hero scans home recursively and doesn't know our naming
    # convention, so a nested review folder would still get loaded in-game
    assert (tmp_path / 'Library_needs_review' / 'Some Song').exists()
    assert not any(p.name == '_needs_review' for p in home.iterdir() if p.is_dir())


def test_process_song_folder_skipped_settled_on_rerun(tmp_path):
    home = tmp_path
    song = home / 'Kryptonite'
    song.mkdir()
    (song / 'song.ini').write_text('[song]\nname = Kryptonite\nartist = 3 Doors Down\n', encoding='utf-8')
    (song / 'notes.chart').write_text(CHART_TEXT, encoding='utf-8')

    first = cr.process_song_folder_for_chart_rename(song, home)
    assert first['status'] == 'confirmed_ok'

    second = cr.process_song_folder_for_chart_rename(song, home)
    assert second['status'] == 'skipped_settled'


def test_process_song_folder_dry_run_relocates_nothing(tmp_path):
    home = tmp_path
    song = home / 'Some Song'
    song.mkdir()
    (song / 'song.ini').write_text('[song]\nname = Some Song\n', encoding='utf-8')

    result = cr.process_song_folder_for_chart_rename(song, home, dry_run=True)

    assert result['status'] == 'needs_review'
    assert song.exists()  # never relocated in dry-run
    assert not (home / '_needs_review').exists()


# --- scan_and_fix_chart_library --------------------------------------------------

def test_scan_and_fix_chart_library_processes_all_folders_and_skips_underscored(tmp_path):
    home = tmp_path / 'Library'
    home.mkdir()
    ok_song = home / 'Kryptonite'
    ok_song.mkdir()
    (ok_song / 'song.ini').write_text('[song]\nname = Kryptonite\nartist = 3 Doors Down\n', encoding='utf-8')
    (ok_song / 'notes.chart').write_text(CHART_TEXT, encoding='utf-8')

    bad_song = home / 'Bad Song'
    bad_song.mkdir()
    (bad_song / 'song.ini').write_text('[song]\nname = Bad Song\n', encoding='utf-8')

    (home / '_needs_review').mkdir()  # a leftover underscore-prefixed folder inside
                                       # home itself must still never be scanned

    counts = cr.scan_and_fix_chart_library(home)

    assert cr.load_chart_rename_status(ok_song) == 'confirmed_ok'
    assert not bad_song.exists()
    # relocated OUTSIDE home (a sibling), not into the home/_needs_review
    # folder that happened to already exist -- that one is untouched
    assert (tmp_path / 'Library_needs_review' / 'Bad Song').exists()
    assert list((home / '_needs_review').iterdir()) == []
    assert counts == {'confirmed_ok': 1, 'needs_review': 1}
