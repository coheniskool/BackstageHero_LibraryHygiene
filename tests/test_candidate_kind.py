# The most important finding of the whole verification effort, from a real
# in-game playtest (2026-07-19):
#
#   "ALMOST ALL OF THE MEASURED VIDEOS ARE LYRIC VIDEOS, GUITAR HERO/ROCKBAND
#    VIDEOS"
#
# Fingerprinting confirms the AUDIO matches the chart and is completely blind
# to what is on screen. A lyric video, a Rock Band playthrough and the official
# music video carry identical audio, so audiosync confirms all three with equal
# confidence and stamps `measured` on each. The offsets were right. The videos
# were simply not what anyone wanted to watch.
#
# The candidate's title came back free with every search and was only ever
# printed. These pin it being used.

import VideoDownload as vd


# --- classification -------------------------------------------------------

def test_gameplay_footage_is_recognised():
    for title in (
        'Anthrax - Among the Living (Rock Band 3 Expert Guitar FC)',
        'Guitar Hero III - Through the Fire and Flames 100% FC',
        'Clone Hero | Some Song | Expert+ Playthrough',
        'Rocksmith 2014 - Some Song',
        'Beat Saber - Some Song',
        'Some Song [Custom Chart Preview]',
    ):
        assert vd.classify_candidate_title(title) == 'gameplay', title


def test_the_rock_band_network_naming_convention_is_recognised():
    """Real titles from the user's library. The first version of the marker
    list caught 8 of 42 fingerprint-confirmed videos, because it looked for
    the words a person would use to DESCRIBE gameplay ("playthrough",
    "gameplay") rather than the words these uploads actually use. The
    convention is "<Song> by <Artist> Full Band FC #123"."""
    for title in (
        'Crusader by AFD Shift Full Band FC #13',
        'Beautiful by Andy Kirk Full Band FC #3',
        "Automatic Doors by a'tris Full Band FC",
        'Secondary Gain by Abraham Nixon - Full Band',
        'One Step Behind by A Hero Next Door Full Band',
        'atom - To The Otherside RBN version',
        'A Call To Remain - Last Hope [RBN Gameplay]',
        'Team Pepe RB3- Reality Down by Active Knowledge',
        'Courage (V1) by Alien Ant Farm Expert Guitar',
    ):
        assert vd.classify_candidate_title(title) == 'gameplay', title


def test_short_tokens_do_not_fire_inside_ordinary_words():
    """'rbn', 'fc' and the console abbreviations are matched on word
    boundaries. Without that, an innocent title containing them as substrings
    would be demoted for no reason."""
    for title in (
        'Suburban Legends - Infatuation',      # contains 'rbn'
        'The Specials - Ghost Town',
        'Fcuk the Pain Away',                  # contains 'fc'
        'Rb Greaves - Take a Letter Maria',
    ):
        assert vd.classify_candidate_title(title) != 'gameplay', title


def test_lyric_and_karaoke_videos_are_recognised():
    for title in (
        'Alanis Morissette - Ironic (Lyrics)',
        'Alanis Morissette - Ironic [Lyric Video]',
        'Some Song - Karaoke Version',
        'Some Song (Sing Along)',
    ):
        assert vd.classify_candidate_title(title) == 'lyric', title


def test_audio_only_uploads_are_recognised():
    for title in ('Some Band - Some Song (Official Audio)',
                  'Some Song [Audio]',
                  'Some Band - Full Album'):
        assert vd.classify_candidate_title(title) == 'audio_only', title


def test_official_videos_are_recognised():
    assert vd.classify_candidate_title(
        'a-ha - Take On Me (Official Video)') == 'official'


def test_official_is_matched_by_pattern_not_by_enumerating_variants():
    """Real titles the substring list missed. Enumerating 'official hd video',
    'official 4k video', ... was the wrong shape -- the next variant would
    have been missed too."""
    for title in (
        'Alice In Chains - Rooster (Official HD Video)',
        '311 - Down (Official 4K Video)',
        'Some Band - Song (OFFICIAL HQ VIDEO)',
        'ANTHRAX - Madhouse (OFFICIAL LIVE CLIP)',
        'Some Band - Song (Official Music Video)',
    ):
        assert vd.classify_candidate_title(title) == 'official', title


def test_uploads_that_only_say_music_video_are_recognised():
    for title in (
        'Amberian Dawn - My Only Star (the music video)',
        'Freezepop- Doppelganger - Music Video',
        '311 - Beautiful Disaster (Bonus Music Video)',
        "Glitzy Glow 'Black And Sunny Day' Promovideo",
    ):
        assert vd.classify_candidate_title(title) == 'official', title


def test_a_lyric_video_is_not_promoted_by_the_word_official():
    """Order matters: the negative checks run first, so "OFFICIAL LYRIC
    VIDEO" stays a lyric video rather than being promoted over a real one."""
    for title in (
        'ALL SHALL PERISH - The Death Plague (OFFICIAL LYRIC VIDEO)',
        'All That Remains - The Waiting One (Official Lyric Video)',
    ):
        assert vd.classify_candidate_title(title) == 'lyric', title


def test_numbered_rock_band_network_tags_are_recognised():
    """\\brbn\\b could not match RBN2, which is one word. Both of these are
    real titles from the library."""
    assert vd.classify_candidate_title('RBN2 EA - Calling to Dance') == 'gameplay'
    assert vd.classify_candidate_title('(RBN1.0) C&O - We Are The Best') == 'gameplay'


def test_an_ordinary_upload_is_unknown_not_junk():
    """A plain upload of a real video usually says nothing special about
    itself, so 'unknown' must rank ABOVE anything self-declared as a lyric
    video -- not be lumped in with it."""
    assert vd.classify_candidate_title('Radiohead - Creep') == 'unknown'
    assert (vd.CANDIDATE_KIND_RANK['unknown']
            < vd.CANDIDATE_KIND_RANK['lyric']
            < vd.CANDIDATE_KIND_RANK['gameplay'])


def test_the_worst_signal_wins_when_a_title_claims_two_things():
    """'Official Lyric Video' is a lyric video. Erring toward keeping junk is
    exactly what this is meant to stop."""
    assert vd.classify_candidate_title(
        'Some Band - Some Song (Official Lyric Video)') == 'lyric'


def test_classification_survives_a_missing_title():
    assert vd.classify_candidate_title(None) == 'unknown'
    assert vd.classify_candidate_title('') == 'unknown'


# --- selection actually uses it -------------------------------------------

def _no_fingerprint(monkeypatch, chart_dur=200):
    """No audiosync, so selection falls through to ranking alone -- which is
    the path that was picking these videos in the first place."""
    monkeypatch.setattr(vd, 'audiosync', None)
    monkeypatch.setattr(vd, '_chart_duration', lambda folder: chart_dur)


def test_a_real_video_beats_a_lyric_video_that_ranked_higher_on_search(tmp_path, monkeypatch):
    """The reported failure, in miniature: YouTube's own relevance put the
    lyric video first, and nothing downstream ever disagreed."""
    _no_fingerprint(monkeypatch)
    (tmp_path / 'song.ini').write_text('[song]\nname = Ironic\n', encoding='utf-8')
    candidates = [
        ('https://youtube.com/watch?v=lyr', 'Alanis Morissette - Ironic (Lyrics)', 200),
        ('https://youtube.com/watch?v=real', 'Alanis Morissette - Ironic (Official Video)', 200),
    ]

    url, title, *_ = vd.select_video(str(tmp_path), candidates, sync_ready=True)

    assert url == 'https://youtube.com/watch?v=real'


def test_gameplay_footage_loses_to_anything_else(tmp_path, monkeypatch):
    _no_fingerprint(monkeypatch)
    (tmp_path / 'song.ini').write_text('[song]\nname = Among the Living\n', encoding='utf-8')
    candidates = [
        ('https://youtube.com/watch?v=rb', 'Among the Living (Rock Band Expert Guitar FC)', 200),
        ('https://youtube.com/watch?v=plain', 'Anthrax - Among the Living', 200),
    ]

    url, *_ = vd.select_video(str(tmp_path), candidates, sync_ready=True)

    assert url == 'https://youtube.com/watch?v=plain'


def test_a_wrong_length_official_video_still_loses_to_a_right_length_lyric_one(tmp_path, monkeypatch):
    """Duration outranks kind on purpose: a wrong-length result is the wrong
    SONG, which is a worse error than the wrong kind of video."""
    _no_fingerprint(monkeypatch)
    (tmp_path / 'song.ini').write_text('[song]\nname = x\n', encoding='utf-8')
    candidates = [
        ('https://youtube.com/watch?v=wrongsong', 'Some Band - Other Song (Official Video)', 12),
        ('https://youtube.com/watch?v=rightsong', 'Some Band - This Song (Lyrics)', 200),
    ]

    url, *_ = vd.select_video(str(tmp_path), candidates, sync_ready=True)

    assert url == 'https://youtube.com/watch?v=rightsong'


def test_a_lyric_video_is_still_used_when_it_is_all_that_exists(tmp_path, monkeypatch):
    """Demote, don't exclude. For an obscure custom chart a lyric video may be
    the only thing on YouTube, and this project's rule is that a video is worth
    having unless it is confidently wrong."""
    _no_fingerprint(monkeypatch)
    (tmp_path / 'song.ini').write_text('[song]\nname = x\n', encoding='utf-8')
    candidates = [('https://youtube.com/watch?v=lyr', 'Obscure Song (Lyrics)', 200)]

    url, *_ = vd.select_video(str(tmp_path), candidates, sync_ready=True)

    assert url == 'https://youtube.com/watch?v=lyr'


def test_kind_ranking_applies_even_with_no_chart_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(vd, 'audiosync', None)
    monkeypatch.setattr(vd, '_chart_duration', lambda folder: None)
    (tmp_path / 'song.ini').write_text('[song]\nname = x\n', encoding='utf-8')
    candidates = [
        ('https://youtube.com/watch?v=gh', 'Song (Guitar Hero Playthrough)', None),
        ('https://youtube.com/watch?v=real', 'Song (Official Video)', None),
    ]

    url, *_ = vd.select_video(str(tmp_path), candidates, sync_ready=True)

    assert url == 'https://youtube.com/watch?v=real'


# --- what got attached is now recorded ------------------------------------

def test_the_attached_videos_title_is_written_to_song_ini(tmp_path, monkeypatch):
    """Only the video ID was stored, so a library full of lyric videos looked
    exactly like a library of real ones without re-querying YouTube for every
    song."""
    (tmp_path / 'song.ini').write_text('[song]\nname = x\nartist = y\n', encoding='utf-8')
    monkeypatch.setattr(vd.resolver_client, 'enabled', lambda: False)
    monkeypatch.setattr(vd.resolver_client, 'resolve', lambda ch: None)
    monkeypatch.setattr(vd.resolver_client, 'report', lambda *a, **k: None)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd, '_probe_resolution_value', lambda f: None)
    monkeypatch.setattr(vd.static_art, 'probe_static_video', lambda p: 'video')
    monkeypatch.setattr(vd, 'search_candidates',
                        lambda q: [('https://youtube.com/watch?v=abc',
                                    'Band - Song (Lyrics)', 200)])
    monkeypatch.setattr(vd, 'select_video',
                        lambda f, c, s, target_h=0:
                        ('https://youtube.com/watch?v=abc', 'Band - Song (Lyrics)',
                         4005, True, 0.9, None))
    monkeypatch.setattr(vd, 'download_with_fallback',
                        lambda folder, url, candidates, quality, info=None: url)
    written = {}
    monkeypatch.setattr(vd, 'set_ini_values',
                        lambda f, values: written.update(values) or True)

    vd.process_download(str(tmp_path), 'Song', vd.quality_format(720),
                        sync_ready=True, replace=False)

    assert written['backstagehero_video_title'] == 'Band - Song (Lyrics)'


def test_a_fallback_download_records_the_title_it_actually_used(tmp_path, monkeypatch):
    """If a fallback candidate downloads instead, the stored title must be
    that one's -- otherwise the record describes a video the user doesn't have."""
    (tmp_path / 'song.ini').write_text('[song]\nname = x\nartist = y\n', encoding='utf-8')
    monkeypatch.setattr(vd.resolver_client, 'enabled', lambda: False)
    monkeypatch.setattr(vd.resolver_client, 'resolve', lambda ch: None)
    monkeypatch.setattr(vd.resolver_client, 'report', lambda *a, **k: None)
    monkeypatch.setattr(vd, 'is_converted', lambda f: False)
    monkeypatch.setattr(vd, '_probe_resolution_value', lambda f: None)
    monkeypatch.setattr(vd.static_art, 'probe_static_video', lambda p: 'video')
    monkeypatch.setattr(vd, 'search_candidates', lambda q: [
        ('https://youtube.com/watch?v=first', 'Band - Song (Official Video)', 200),
        ('https://youtube.com/watch?v=second', 'Band - Song (Lyrics)', 200),
    ])
    monkeypatch.setattr(vd, 'select_video',
                        lambda f, c, s, target_h=0:
                        ('https://youtube.com/watch?v=first',
                         'Band - Song (Official Video)', 4005, True, 0.9, None))
    # the first one fails to download; the second is what lands
    monkeypatch.setattr(vd, 'download_with_fallback',
                        lambda folder, url, candidates, quality, info=None:
                        'https://youtube.com/watch?v=second')
    written = {}
    monkeypatch.setattr(vd, 'set_ini_values',
                        lambda f, values: written.update(values) or True)

    vd.process_download(str(tmp_path), 'Song', vd.quality_format(720),
                        sync_ready=True, replace=False)

    assert written['backstagehero_video_title'] == 'Band - Song (Lyrics)'
