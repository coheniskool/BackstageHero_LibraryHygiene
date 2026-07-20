# Auto-sync ("process_resync") uses the audio it can extract from the video
# already on disk instead of re-fetching from YouTube -- when that video HAS an
# audio track. Falls back to the stored YouTube source, then a fresh search --
# see VideoDownload.process_resync's docstring.
#
# The "with audio" part is not a detail. Measured against a real library:
# every video this app downloaded had NO audio track (quality_format asks for
# a video-only stream first), while every video left by the predecessor had
# one. So this optimisation applies to externally-sourced videos and never to
# the app's own. The original tests here all implied otherwise, because a
# stub video is neither -- these now say which case they are exercising.

import os

import VideoDownload as vd


def _make_song_folder(tmp_path, with_video=True, video_has_audio=True):
    (tmp_path / 'song.ini').write_text('[song]\nname = Test\nartist = Someone\n', encoding='utf-8')
    if with_video:
        (tmp_path / 'video.mp4').write_bytes(b'fake mp4 bytes')
    return tmp_path


def _local_video_has_audio(monkeypatch, present=True):
    """Stand in for the ffprobe audio-stream check.

    The stub video.mp4 above is not a real MP4, so the real probe would always
    say "no audio" and every local-path test would silently start exercising
    the network fallback instead of what it claims to test.
    """
    monkeypatch.setattr(vd, '_has_audio_stream', lambda path: present)


def _unexpected(*args, **kwargs):
    raise AssertionError('this should not have been called')


def test_process_resync_prefers_local_video_no_network(tmp_path, monkeypatch):
    folder = _make_song_folder(tmp_path, with_video=True)
    _local_video_has_audio(monkeypatch)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms', lambda folder_, probe: (1234, 'matched locally', 0.9))
    monkeypatch.setattr(vd, 'set_ini_values', lambda f, values: True)
    monkeypatch.setattr(vd, 'fetch_audio', _unexpected)  # network path must never be reached

    vd.process_resync(str(folder), 'Test Song', sync_ready=True)


def test_process_resync_passes_the_real_local_video_path(tmp_path, monkeypatch):
    folder = _make_song_folder(tmp_path, with_video=True)
    _local_video_has_audio(monkeypatch)
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
    _local_video_has_audio(monkeypatch)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms', lambda folder_, probe: (-2500, 'matched', 0.9))

    written = {}
    monkeypatch.setattr(vd, 'set_ini_values', lambda f, values: written.update(values) or True)

    vd.process_resync(str(folder), 'Test Song', sync_ready=True)

    # the offset was genuinely fingerprint-measured here, so it must be recorded
    # as such -- a resync is exactly how a stale 'guess' gets upgraded
    assert written == {'video_start_time': '-2500',
                       'backstagehero_sync': vd.SYNC_MEASURED}


def test_an_audioless_video_skips_straight_to_the_network_path(tmp_path, monkeypatch):
    """The real shape of an app-downloaded video: quality_format asks for a
    video-only stream first, so there is no audio track to fingerprint. All
    87 app-downloaded videos in the real library measured this way.

    compute_offset_ms must not be handed the video at all -- that decode is
    guaranteed to fail, and at library scale it means feeding ffmpeg a 60MB
    file per song to be told there is nothing in it."""
    folder = _make_song_folder(tmp_path, with_video=True)
    _local_video_has_audio(monkeypatch, present=False)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd, 'get_stored_source', lambda f: 'dQw4w9WgXcQ')
    monkeypatch.setattr(vd, 'fetch_audio',
                        lambda folder_, url: ('fetched_audio.opus', 720, None))
    monkeypatch.setattr(vd, 'cleanup_temp_files', lambda f: None)
    monkeypatch.setattr(vd, '_probe_and_store_resolution', lambda f: None)
    monkeypatch.setattr(vd, 'set_ini_values', lambda f, values: True)

    probes = []

    def _fake_compute(folder_, probe):
        probes.append(probe)
        return (4321, 'matched from network fetch', 0.8)

    monkeypatch.setattr(vd.audiosync, 'compute_offset_ms', _fake_compute)

    vd.process_resync(str(folder), 'Test Song', sync_ready=True)

    assert os.path.join(str(folder), 'video.mp4') not in probes
    assert probes == ['fetched_audio.opus']


def test_the_audio_check_fails_closed(tmp_path, monkeypatch):
    """A probe that errors must read as 'no audio', so the caller falls
    through to the network path that works rather than attempting a decode
    that is about to fail anyway."""
    (tmp_path / 'not_really.mp4').write_bytes(b'definitely not an mp4')
    assert vd._has_audio_stream(tmp_path / 'not_really.mp4') is False
    assert vd._has_audio_stream(tmp_path / 'does_not_exist.mp4') is False


def test_process_resync_falls_back_to_known_source_when_local_video_inconclusive(tmp_path, monkeypatch):
    folder = _make_song_folder(tmp_path, with_video=True)
    _local_video_has_audio(monkeypatch)
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
