# "Dump this video" -- discarding a download that turned out to be the wrong
# thing entirely (an unrelated song, a lyric video, someone's bedroom cover).
#
# Deleting the file is the easy half and on its own it accomplishes nothing:
# the YouTube search is effectively deterministic, so the next run finds the
# same upload and downloads it again. The rejection memory is the feature.

import VideoDownload as vd


def _song(tmp_path, source='badvid123', extra=''):
    (tmp_path / 'song.ini').write_text(
        f'[song]\nname = Test\nartist = Someone\n'
        f'backstagehero_source = {source}\n{extra}', encoding='utf-8')
    (tmp_path / 'video.mp4').write_bytes(b'the wrong video entirely')
    return tmp_path


def test_dumping_removes_the_video_and_records_the_rejection(tmp_path):
    folder = _song(tmp_path)

    result = vd.dump_video(folder)

    assert result['status'] == 'dumped'
    assert not (folder / 'video.mp4').exists()
    assert vd.get_rejected_sources(folder) == {'badvid123'}
    assert vd.get_stored_source(folder) is None      # no longer this song's video


def test_dumping_twice_accumulates_rejections(tmp_path):
    """Each bad video the search offers has to be remembered, not just the
    most recent one -- otherwise it can alternate between two wrong uploads
    forever."""
    folder = _song(tmp_path, source='first')
    vd.dump_video(folder)

    (folder / 'video.mp4').write_bytes(b'a different wrong video')
    vd.set_ini_values(folder, {'backstagehero_source': 'second'})
    vd.dump_video(folder)

    assert vd.get_rejected_sources(folder) == {'first', 'second'}


def test_dumping_a_song_with_no_video_is_a_clean_no_op(tmp_path):
    (tmp_path / 'song.ini').write_text('[song]\nname = Test\n', encoding='utf-8')
    assert vd.dump_video(tmp_path)['status'] == 'nothing_to_dump'


def test_a_broken_song_ini_removes_nothing(tmp_path):
    """If the rejection can't be recorded, the video must stay. Deleting it
    without recording why is the one outcome that guarantees the same wrong
    video comes straight back."""
    (tmp_path / 'song.ini').write_text('name = no section header\n', encoding='utf-8')
    (tmp_path / 'video.mp4').write_bytes(b'x')

    result = vd.dump_video(tmp_path)

    assert result['status'] == 'failed'
    assert (tmp_path / 'video.mp4').exists()


def test_dumping_undoes_a_static_art_conversion(tmp_path):
    """The lyric-video case from the request: static-art detection turned the
    upload into album art, and the user doesn't want that picture. Dumping has
    to remove the art AND clear the marker, or the song stays permanently
    skipped by process_download."""
    (tmp_path / 'song.ini').write_text(
        f'[song]\nname = Test\nartist = Someone\n'
        f'backstagehero_source = lyricvid\n'
        f'{vd.static_art.VIDEO_MARKER_KEY} = {vd.static_art.VIDEO_MARKER_STATIC_ART}\n',
        encoding='utf-8')
    (tmp_path / 'album.png').write_bytes(b'extracted frame')

    result = vd.dump_video(tmp_path)

    assert result['status'] == 'dumped'
    assert not (tmp_path / 'album.png').exists()
    assert vd._read_ini_value(tmp_path, vd.static_art.VIDEO_MARKER_KEY) is None
    assert vd.get_rejected_sources(tmp_path) == {'lyricvid'}


def test_dumping_never_touches_album_art_the_app_did_not_create(tmp_path):
    """No static-art marker means the art is the user's own."""
    folder = _song(tmp_path)
    (folder / 'album.png').write_bytes(b'the users own artwork')

    vd.dump_video(folder)

    assert (folder / 'album.png').read_bytes() == b'the users own artwork'


def test_dump_video_parses_song_ini_once(tmp_path, monkeypatch):
    """dump_video needs the video marker, stored source, and rejected list --
    three keys that used to mean three separate opens+parses of song.ini."""
    folder = _song(tmp_path)
    calls = []
    real = vd._read_ini_section

    def _counting(f):
        calls.append(f)
        return real(f)

    monkeypatch.setattr(vd, '_read_ini_section', _counting)

    vd.dump_video(folder)

    assert len(calls) == 1


# --- the rejection has to actually change what gets downloaded ------------

def _sync_off(monkeypatch):
    monkeypatch.setattr(vd, 'audiosync', None)


def test_select_video_skips_a_rejected_upload(tmp_path, monkeypatch):
    _sync_off(monkeypatch)
    (tmp_path / 'song.ini').write_text(
        '[song]\nname = Test\nbackstagehero_rejected = badvid123\n', encoding='utf-8')
    candidates = [
        ('https://youtube.com/watch?v=badvid123', 'The wrong one', 200),
        ('https://youtube.com/watch?v=goodvid', 'Something else', 200),
    ]

    url, title, _, _, _, _ = vd.select_video(str(tmp_path), candidates, sync_ready=True)

    assert url == 'https://youtube.com/watch?v=goodvid'


def test_select_video_attaches_nothing_when_every_result_was_dumped(tmp_path, monkeypatch):
    """Better to leave the song without a video than to re-attach one the user
    has explicitly thrown away."""
    _sync_off(monkeypatch)
    (tmp_path / 'song.ini').write_text(
        '[song]\nname = Test\nbackstagehero_rejected = a,b\n', encoding='utf-8')
    candidates = [
        ('https://youtube.com/watch?v=a', 'Rejected 1', 200),
        ('https://youtube.com/watch?v=b', 'Rejected 2', 200),
    ]

    url, title, _, matched, _, _ = vd.select_video(str(tmp_path), candidates, sync_ready=True)

    assert url is None and title is None and matched is False


def test_a_dumped_video_is_not_reinstated_by_the_community_resolver(tmp_path, monkeypatch):
    """The pool's opinion must not override this user's explicit rejection --
    that would make dumping useless on exactly the songs it matters for."""
    (tmp_path / 'song.ini').write_text(
        '[song]\nname = Test\nartist = Someone\n'
        'backstagehero_rejected = poolfav\n', encoding='utf-8')
    monkeypatch.setattr(vd.resolver_client, 'enabled', lambda: True)
    monkeypatch.setattr(vd.resolver_client, 'chart_hash', lambda f: 'h')
    monkeypatch.setattr(vd.resolver_client, 'report', lambda *a, **k: None)
    monkeypatch.setattr(vd.resolver_client, 'resolve',
                        lambda ch: {'video_id': 'poolfav', 'start_ms': 1000})

    def _no_download(*a, **k):
        raise AssertionError('downloaded a video the user had dumped')
    monkeypatch.setattr(vd, 'download_video', _no_download)
    monkeypatch.setattr(vd, 'search_candidates', lambda q: [])
    monkeypatch.setattr(vd, 'select_video',
                        lambda *a, **k: (None, None, vd.DEFAULT_START_TIME, False, 0.0, None))

    vd.process_download(str(tmp_path), 'Test', vd.quality_format(720),
                        sync_ready=False, replace=False)
