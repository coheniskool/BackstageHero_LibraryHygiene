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
