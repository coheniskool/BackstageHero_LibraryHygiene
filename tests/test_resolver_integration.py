# Confirms the resolver's actual payoff at the point it matters: a
# community-confirmed chart skips the YouTube search entirely
# (VideoDownload.process_download's resolver-hit branch).

import VideoDownload as vd


def _unexpected_search(*args, **kwargs):
    raise AssertionError('search_candidates should not be called on a resolver hit')


def test_resolver_hit_skips_youtube_search(tmp_path, monkeypatch):
    folder = tmp_path
    (folder / 'song.ini').write_text('[song]\nname = Test\nartist = Someone\n', encoding='utf-8')

    monkeypatch.setattr(vd.resolver_client, 'enabled', lambda: True)
    monkeypatch.setattr(vd.resolver_client, 'resolve',
                         lambda ch: {'video_id': 'dQw4w9WgXcQ', 'start_ms': -1500})
    monkeypatch.setattr(vd, 'download_video', lambda *a, **k: None)
    monkeypatch.setattr(vd, 'set_ini_values', lambda *a, **k: True)
    monkeypatch.setattr(vd, '_probe_resolution_value', lambda *a, **k: None)
    monkeypatch.setattr(vd, 'search_candidates', _unexpected_search)

    vd.process_download(str(folder), 'Test Song', vd.quality_format(720),
                         sync_ready=False, replace=False)
    # No assertion needed beyond "didn't raise" -- _unexpected_search raises
    # AssertionError if the search path were reached instead.
