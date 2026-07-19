# Regression coverage for a real bug found via the user's own in-game
# testing (2026-07-18): with no fingerprint-confirmed candidate, the old
# final fallback used the RAW top search result with zero duration check
# at all -- and it attached a completely unrelated video (a different
# artist's static album-art upload, confirmed by extracting and viewing an
# actual frame) to a 3 Doors Down chart. Fixed in two parts: prefer the
# already-computed duration-ranked candidate over raw search order, and
# refuse to attach anything at all if even that one is implausible.

import VideoDownload as vd


def _never_confirms(folder, probe_audio):
    return None, 'no confident match', 0.0


def test_select_video_prefers_duration_plausible_candidate_in_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(vd, 'audiosync', type('_A', (), {
        'chart_stems': staticmethod(lambda folder: ['song.ogg']),
        'compute_offset_ms': staticmethod(_never_confirms),
    }))
    monkeypatch.setattr(vd, '_chart_duration', lambda folder: 200)  # chart is ~200s
    monkeypatch.setattr(vd, 'fetch_audio', lambda folder, url: ('fake_audio_path', 720, None))
    monkeypatch.setattr(vd, 'cleanup_temp_files', lambda folder: None)

    candidates = [
        ('https://youtube.com/watch?v=wrong', 'Unrelated short clip', 30),     # implausible
        ('https://youtube.com/watch?v=right', 'Plausible length video', 200),  # matches chart length
    ]

    url, title, ms, matched, conf, vinfo = vd.select_video(str(tmp_path), candidates, sync_ready=True)

    assert url == 'https://youtube.com/watch?v=right'  # NOT candidates[0] (the raw top result)
    assert matched is False
    assert ms == vd.DEFAULT_START_TIME


def test_select_video_refuses_to_attach_when_nothing_is_plausible(tmp_path, monkeypatch):
    monkeypatch.setattr(vd, 'audiosync', type('_A', (), {
        'chart_stems': staticmethod(lambda folder: ['song.ogg']),
        'compute_offset_ms': staticmethod(_never_confirms),
    }))
    monkeypatch.setattr(vd, '_chart_duration', lambda folder: 200)
    monkeypatch.setattr(vd, 'fetch_audio', lambda folder, url: ('fake_audio_path', 720, None))
    monkeypatch.setattr(vd, 'cleanup_temp_files', lambda folder: None)

    candidates = [
        ('https://youtube.com/watch?v=a', 'Way too short', 5),
        ('https://youtube.com/watch?v=b', 'Way too long', 6000),
    ]

    url, title, ms, matched, conf, vinfo = vd.select_video(str(tmp_path), candidates, sync_ready=True)

    assert url is None
    assert title is None
    assert matched is False


def test_select_video_no_chart_duration_falls_back_to_raw_top_result_unchanged(tmp_path, monkeypatch):
    """When there's no chart_dur signal at all (e.g. no reference audio to
    probe), the hard floor must not apply -- this preserves the original,
    pre-fix behavior for that case rather than refusing to ever attach a
    video when duration simply isn't knowable."""
    monkeypatch.setattr(vd, 'audiosync', type('_A', (), {
        'chart_stems': staticmethod(lambda folder: ['song.ogg']),
        'compute_offset_ms': staticmethod(_never_confirms),
    }))
    monkeypatch.setattr(vd, '_chart_duration', lambda folder: None)
    monkeypatch.setattr(vd, 'fetch_audio', lambda folder, url: ('fake_audio_path', 720, None))
    monkeypatch.setattr(vd, 'cleanup_temp_files', lambda folder: None)

    candidates = [
        ('https://youtube.com/watch?v=first', 'First raw result', None),
        ('https://youtube.com/watch?v=second', 'Second raw result', None),
    ]

    url, title, ms, matched, conf, vinfo = vd.select_video(str(tmp_path), candidates, sync_ready=True)

    assert url == 'https://youtube.com/watch?v=first'  # candidates[0], unranked, as before
    assert matched is False


def test_process_download_skips_cleanly_when_select_video_finds_nothing_plausible(tmp_path, monkeypatch):
    (tmp_path / 'song.ini').write_text('[song]\nname = Test\nartist = Someone\n', encoding='utf-8')

    monkeypatch.setattr(vd.resolver_client, 'enabled', lambda: False)
    monkeypatch.setattr(vd, 'search_candidates', lambda query: [('https://youtube.com/x', 'X', 10)])
    monkeypatch.setattr(vd, 'select_video',
                         lambda folder, candidates, sync_ready, target_h=0:
                         (None, None, vd.DEFAULT_START_TIME, False, 0.0, None))

    def _unexpected_download(*a, **k):
        raise AssertionError('download_with_fallback should not be called when nothing is plausible')
    monkeypatch.setattr(vd, 'download_with_fallback', _unexpected_download)

    vd.process_download(str(tmp_path), 'Test Song', vd.quality_format(720), sync_ready=True, replace=False)

    assert not (tmp_path / 'video.mp4').exists()
