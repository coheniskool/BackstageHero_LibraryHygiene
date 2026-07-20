# Coverage for `backstagehero_sync`, the provenance marker written alongside
# video_start_time.
#
# The problem it solves: DEFAULT_START_TIME (-3000) written as a pure guess is
# byte-identical in song.ini to a real measurement that happens to land near
# -3000. A song reported as out of sync turned out to have never been matched
# at all -- its offset was the fallback constant, and nothing on disk said so.
# These tests pin the distinction: every write path must say how it got there.

import VideoDownload as vd


def _song(tmp_path):
    (tmp_path / 'song.ini').write_text('[song]\nname = Test\nartist = Someone\n', encoding='utf-8')
    return tmp_path


def _capture_ini(monkeypatch):
    """Record what process_download/process_resync write to song.ini."""
    written = {}
    monkeypatch.setattr(vd, 'set_ini_values', lambda f, values: written.update(values) or True)
    return written


def _no_resolver(monkeypatch):
    monkeypatch.setattr(vd.resolver_client, 'enabled', lambda: False)
    monkeypatch.setattr(vd.resolver_client, 'resolve', lambda ch: None)
    monkeypatch.setattr(vd.resolver_client, 'report', lambda *a, **k: None)


def _quiet_download(monkeypatch, folder):
    monkeypatch.setattr(vd, 'download_with_fallback',
                        lambda folder_, url, candidates, quality, info=None: url)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd, '_probe_and_store_resolution', lambda f: None)


def test_unmeasured_offset_is_recorded_as_a_guess(tmp_path, monkeypatch):
    """The whole point: an unconfirmed match still gets the useful -3000 guess,
    but is now labelled, so it can never again be mistaken for a measurement."""
    folder = _song(tmp_path)
    _no_resolver(monkeypatch)
    _quiet_download(monkeypatch, folder)
    monkeypatch.setattr(vd, 'search_candidates', lambda q: [('https://youtube.com/watch?v=abc', 'A', 200)])
    monkeypatch.setattr(vd, 'select_video',
                        lambda f, c, s, target_h=0:
                        ('https://youtube.com/watch?v=abc', 'A', vd.DEFAULT_START_TIME, False, 0.0, None))
    written = _capture_ini(monkeypatch)

    vd.process_download(str(folder), 'Test Song', vd.quality_format(720),
                        sync_ready=True, replace=False)

    assert written['video_start_time'] == str(vd.DEFAULT_START_TIME)   # guess preserved
    assert written['backstagehero_sync'] == vd.SYNC_GUESS              # ...and declared


def test_fingerprint_matched_offset_is_recorded_as_measured(tmp_path, monkeypatch):
    folder = _song(tmp_path)
    _no_resolver(monkeypatch)
    _quiet_download(monkeypatch, folder)
    monkeypatch.setattr(vd, 'search_candidates', lambda q: [('https://youtube.com/watch?v=abc', 'A', 200)])
    monkeypatch.setattr(vd, 'select_video',
                        lambda f, c, s, target_h=0:
                        ('https://youtube.com/watch?v=abc', 'A', 4005, True, 0.93, None))
    written = _capture_ini(monkeypatch)

    vd.process_download(str(folder), 'Test Song', vd.quality_format(720),
                        sync_ready=True, replace=False)

    assert written['video_start_time'] == '4005'
    assert written['backstagehero_sync'] == vd.SYNC_MEASURED


def test_a_measurement_landing_on_the_default_value_is_still_measured(tmp_path, monkeypatch):
    """The exact ambiguity this feature exists to remove: audiosync genuinely
    computing -3000 must NOT look like the -3000 fallback guess."""
    folder = _song(tmp_path)
    _no_resolver(monkeypatch)
    _quiet_download(monkeypatch, folder)
    monkeypatch.setattr(vd, 'search_candidates', lambda q: [('https://youtube.com/watch?v=abc', 'A', 200)])
    monkeypatch.setattr(vd, 'select_video',
                        lambda f, c, s, target_h=0:
                        ('https://youtube.com/watch?v=abc', 'A', vd.DEFAULT_START_TIME, True, 0.91, None))
    written = _capture_ini(monkeypatch)

    vd.process_download(str(folder), 'Test Song', vd.quality_format(720),
                        sync_ready=True, replace=False)

    assert written['video_start_time'] == str(vd.DEFAULT_START_TIME)
    assert written['backstagehero_sync'] == vd.SYNC_MEASURED   # identical value, different provenance


def test_fallback_to_a_different_video_downgrades_measured_to_guess(tmp_path, monkeypatch):
    """When a fallback candidate downloads instead of the one that was measured,
    the offset belongs to a different video and is dropped. The marker has to
    follow it down -- otherwise a discarded measurement still reads as 'measured'."""
    folder = _song(tmp_path)
    _no_resolver(monkeypatch)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd, '_probe_and_store_resolution', lambda f: None)
    # measured against ...=abc, but ...=xyz is what actually downloaded
    monkeypatch.setattr(vd, 'download_with_fallback',
                        lambda folder_, url, candidates, quality, info=None: 'https://youtube.com/watch?v=xyz')
    monkeypatch.setattr(vd, 'search_candidates', lambda q: [('https://youtube.com/watch?v=abc', 'A', 200)])
    monkeypatch.setattr(vd, 'select_video',
                        lambda f, c, s, target_h=0:
                        ('https://youtube.com/watch?v=abc', 'A', 4005, True, 0.93, None))
    written = _capture_ini(monkeypatch)

    vd.process_download(str(folder), 'Test Song', vd.quality_format(720),
                        sync_ready=True, replace=False)

    assert written['video_start_time'] == str(vd.DEFAULT_START_TIME)
    assert written['backstagehero_sync'] == vd.SYNC_GUESS


def test_community_offset_is_distinguished_from_a_community_video_with_no_offset(tmp_path, monkeypatch):
    """A resolver hit carrying a start_ms is a real community measurement; a hit
    without one is a trusted video on a guessed timing. Different provenance."""
    folder = _song(tmp_path)
    monkeypatch.setattr(vd.resolver_client, 'enabled', lambda: True)
    monkeypatch.setattr(vd.resolver_client, 'chart_hash', lambda f: 'hash123')
    monkeypatch.setattr(vd.resolver_client, 'report', lambda *a, **k: None)
    monkeypatch.setattr(vd, 'download_video', lambda folder_, url, quality: None)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd, '_probe_and_store_resolution', lambda f: None)

    monkeypatch.setattr(vd.resolver_client, 'resolve',
                        lambda ch: {'video_id': 'abc123', 'start_ms': 2750})
    written = _capture_ini(monkeypatch)
    vd.process_download(str(folder), 'Test Song', vd.quality_format(720),
                        sync_ready=True, replace=False)
    assert written['video_start_time'] == '2750'
    assert written['backstagehero_sync'] == vd.SYNC_COMMUNITY

    monkeypatch.setattr(vd.resolver_client, 'resolve',
                        lambda ch: {'video_id': 'abc123'})       # known video, unknown timing
    written = _capture_ini(monkeypatch)
    vd.process_download(str(folder), 'Test Song', vd.quality_format(720),
                        sync_ready=True, replace=False)
    assert written['video_start_time'] == str(vd.DEFAULT_START_TIME)
    assert written['backstagehero_sync'] == vd.SYNC_GUESS


# --- SYNC_MANUAL is read, not just written -------------------------------
#
# The marker was defined and written by the sync editor from the start, but
# nothing ever read it back, so "Auto-sync" over a checked library silently
# overwrote every hand-set offset -- by definition the songs the user had
# already fixed because audiosync got them wrong the first time.

def _manual_song(tmp_path):
    (tmp_path / 'song.ini').write_text(
        '[song]\nname = Test\nartist = Someone\n'
        'video_start_time = 1234\n'
        f'backstagehero_sync = {vd.SYNC_MANUAL}\n', encoding='utf-8')
    return tmp_path


def _explode_if_called(monkeypatch, *names):
    """Any of these running means the manual guard didn't stop the pass."""
    for name in names:
        monkeypatch.setattr(vd, name, lambda *a, **k: (_ for _ in ()).throw(
            AssertionError(f'{name} ran on a manually-synced song')))


def test_manual_offset_survives_an_automatic_resync(tmp_path, monkeypatch):
    """The core of the fix: a hand-set offset outranks anything automatic."""
    folder = _manual_song(tmp_path)
    written = _capture_ini(monkeypatch)
    _explode_if_called(monkeypatch, 'search_candidates', 'select_video', 'fetch_audio')

    result = vd.process_resync(str(folder), 'Test Song', sync_ready=True)

    assert result == 'skipped'
    assert written == {}                                   # nothing touched song.ini
    on_disk = (folder / 'song.ini').read_text(encoding='utf-8')
    assert 'video_start_time = 1234' in on_disk            # the user's value, intact
    assert f'backstagehero_sync = {vd.SYNC_MANUAL}' in on_disk


def test_a_non_manual_song_is_still_resynced(tmp_path, monkeypatch):
    """Guard against over-correcting: only `manual` is protected, not every
    marker. A `guess` song is exactly what Auto-sync exists to improve."""
    (tmp_path / 'song.ini').write_text(
        '[song]\nname = Test\nartist = Someone\n'
        f'backstagehero_sync = {vd.SYNC_GUESS}\n', encoding='utf-8')
    (tmp_path / 'video.mp4').write_bytes(b'not really a video')
    # a stub file has no decodable audio stream, so the local-video path is
    # correctly skipped -- say which case this test is exercising rather than
    # letting it silently drift onto the network fallback
    monkeypatch.setattr(vd, '_has_audio_stream', lambda path: True)
    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms',
                        lambda f, a: (7200, 'matched', 0.95))
    written = _capture_ini(monkeypatch)

    result = vd.process_resync(str(tmp_path), 'Test Song', sync_ready=True)

    assert result != 'skipped'
    assert written['video_start_time'] == '7200'
    assert written['backstagehero_sync'] == vd.SYNC_MEASURED


def test_manual_skip_is_reported_as_skipped_by_the_batch_runner(tmp_path, monkeypatch):
    """process_resync's return value used to be discarded, so even once the
    guard existed the GUI would have counted a protected song as re-synced."""
    folder = _manual_song(tmp_path)
    _capture_ini(monkeypatch)

    assert vd.run_song_with_backoff(
        str(folder), 'Test Song', vd.quality_format(720), sync_ready=True,
        replace=False, resync=True, errored=[]) == 'skipped'


# --- the resync search fallback must not write another video's offset -----

def test_resync_fallback_search_leaves_timing_unchanged(tmp_path, monkeypatch):
    """We only reach the search fallback because the video ON DISK failed to
    match. A fresh candidate matching the chart says nothing about that file's
    timing, so writing its offset would store a measurement of a video the user
    doesn't have -- and stamp it SYNC_MEASURED, the highest-trust marker."""
    folder = _song(tmp_path)
    (folder / 'video.mp4').write_bytes(b'a video that no longer matches')
    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms',
                        lambda f, a: (None, 'no match', 0.0))   # local video fails
    monkeypatch.setattr(vd, 'get_stored_source', lambda f: None)
    monkeypatch.setattr(vd, 'search_candidates',
                        lambda q: [('https://youtube.com/watch?v=other', 'Other upload', 200)])
    # a DIFFERENT upload fingerprint-matches the chart
    monkeypatch.setattr(vd, 'select_video',
                        lambda f, c, s, target_h=0:
                        ('https://youtube.com/watch?v=other', 'Other upload', 8800, True, 0.97, None))
    written = _capture_ini(monkeypatch)

    vd.process_resync(str(folder), 'Test Song', sync_ready=True)

    assert written == {}, 'wrote an offset measured against a video not on disk'
