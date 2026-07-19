# Auto-sync ("process_resync") should prefer the audio it can extract from
# the video already on disk over re-fetching from YouTube -- the video
# hasn't changed, only the stored timing needs a recheck, so the common
# case should cost zero network requests. Falls back to the stored
# YouTube source, then a fresh search, unchanged from before -- see
# VideoDownload.process_resync's docstring.

import os

import VideoDownload as vd


def _make_song_folder(tmp_path, with_video=True):
    (tmp_path / 'song.ini').write_text('[song]\nname = Test\nartist = Someone\n', encoding='utf-8')
    if with_video:
        (tmp_path / 'video.mp4').write_bytes(b'fake mp4 bytes')
    return tmp_path


def _unexpected(*args, **kwargs):
    raise AssertionError('this should not have been called')


def test_process_resync_prefers_local_video_no_network(tmp_path, monkeypatch):
    folder = _make_song_folder(tmp_path, with_video=True)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms', lambda folder_, probe: (1234, 'matched locally', 0.9))
    monkeypatch.setattr(vd, 'set_ini_values', lambda f, values: True)
    monkeypatch.setattr(vd, 'fetch_audio', _unexpected)  # network path must never be reached

    vd.process_resync(str(folder), 'Test Song', sync_ready=True)


def test_process_resync_passes_the_real_local_video_path(tmp_path, monkeypatch):
    folder = _make_song_folder(tmp_path, with_video=True)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)

    calls = []

    def _fake_compute(folder_, probe):
        calls.append(probe)
        return (1234, 'matched', 0.9)

    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms', _fake_compute)
    monkeypatch.setattr(vd, 'set_ini_values', lambda f, values: True)

    vd.process_resync(str(folder), 'Test Song', sync_ready=True)

    assert calls == [os.path.join(str(folder), 'video.mp4')]


def test_process_resync_writes_the_computed_offset(tmp_path, monkeypatch):
    folder = _make_song_folder(tmp_path, with_video=True)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms', lambda folder_, probe: (-2500, 'matched', 0.9))

    written = {}
    monkeypatch.setattr(vd, 'set_ini_values', lambda f, values: written.update(values) or True)

    vd.process_resync(str(folder), 'Test Song', sync_ready=True)

    # the offset was genuinely fingerprint-measured here, so it must be recorded
    # as such -- a resync is exactly how a stale 'guess' gets upgraded
    assert written == {'video_start_time': '-2500',
                       'backstagehero_sync': vd.SYNC_MEASURED}


def test_process_resync_falls_back_to_known_source_when_local_video_inconclusive(tmp_path, monkeypatch):
    folder = _make_song_folder(tmp_path, with_video=True)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd, 'get_stored_source', lambda f: 'dQw4w9WgXcQ')

    probes = []

    def _fake_compute(folder_, probe):
        probes.append(probe)
        if probe == os.path.join(str(folder), 'video.mp4'):
            return (None, 'no confident match', 0.0)  # local video inconclusive
        return (5678, 'matched from network fetch', 0.8)

    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms', _fake_compute)
    monkeypatch.setattr(vd, 'fetch_audio', lambda f, url: ('fake_audio_path', 0, None))
    monkeypatch.setattr(vd, 'cleanup_temp_files', lambda f: None)
    monkeypatch.setattr(vd, 'set_ini_values', lambda f, values: True)
    monkeypatch.setattr(vd, '_probe_and_store_resolution', lambda f: None)

    vd.process_resync(str(folder), 'Test Song', sync_ready=True)

    assert probes == [os.path.join(str(folder), 'video.mp4'), 'fake_audio_path']


def test_process_resync_no_local_video_behaves_exactly_as_before(tmp_path, monkeypatch):
    """Regression: a folder with no video.mp4 must skip the new local-audio
    step entirely and go straight to the original source-lookup/search
    fallback chain, unchanged."""
    folder = _make_song_folder(tmp_path, with_video=False)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd, 'get_stored_source', lambda f: 'dQw4w9WgXcQ')

    probes = []

    def _fake_compute(folder_, probe):
        probes.append(probe)
        return (999, 'ok', 0.7)

    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms', _fake_compute)
    monkeypatch.setattr(vd, 'fetch_audio', lambda f, url: ('fake_audio_path', 0, None))
    monkeypatch.setattr(vd, 'cleanup_temp_files', lambda f: None)
    monkeypatch.setattr(vd, 'set_ini_values', lambda f, values: True)
    monkeypatch.setattr(vd, '_probe_and_store_resolution', lambda f: None)

    vd.process_resync(str(folder), 'Test Song', sync_ready=True)

    assert probes == ['fake_audio_path']  # only the network path ran, never a local video.mp4


def test_process_resync_skips_when_sync_not_ready(tmp_path, monkeypatch):
    folder = _make_song_folder(tmp_path, with_video=True)
    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms', _unexpected)

    vd.process_resync(str(folder), 'Test Song', sync_ready=False)


def test_process_resync_skips_converted_folders(tmp_path, monkeypatch):
    folder = _make_song_folder(tmp_path, with_video=True)
    monkeypatch.setattr(vd, 'is_converted', lambda f: True)
    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms', _unexpected)

    vd.process_resync(str(folder), 'Test Song', sync_ready=True)
