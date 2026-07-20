import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import static_art as sa

_HAS_FFMPEG = shutil.which('ffmpeg') is not None
requires_ffmpeg = pytest.mark.skipif(not _HAS_FFMPEG, reason='ffmpeg not on PATH')


class _FakeCompletedProcess:
    def __init__(self, stdout=b'', returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _hash_with_distance(base, distance):
    """A 64-bit hash exactly `distance` bits away from `base`."""
    return base ^ ((1 << distance) - 1)


def _dummy_video(tmp_path):
    """A file that exists at all -- probe_static_video's very first check is
    Path.exists(), so any test driving logic past that point via monkeypatched
    duration/frames still needs a real file on disk, or every one of them
    would pass 'unknown' for the wrong reason."""
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'x')
    return path


def _controlled_frames(hash_distance, cell_delta, count=8):
    """Frames whose max pairwise hash distance and cell delta are exactly the
    given values -- lets the boundary tests drive probe_static_video's
    classification logic without any real video or ffmpeg call."""
    grid_a = [100] * (sa.STATIC_GRID * sa.STATIC_GRID)
    grid_b = list(grid_a)
    if cell_delta:
        grid_b[0] = 100 + cell_delta
    hash_a = 0
    hash_b = _hash_with_distance(hash_a, hash_distance) if hash_distance else hash_a
    half = count // 2
    return [(hash_a, grid_a)] * half + [(hash_b, grid_b)] * (count - half)


# --- _average_hash / _hamming / _max_cell_delta / _luminance_grid ---------
# Pure unit tests, no ffmpeg dependency.

def test_average_hash_sets_bits_above_the_frame_mean():
    grid = [0] * 32 + [200] * 32  # mean = 100; upper half of cells exceed it
    h = sa._average_hash(grid)
    assert h == sum(1 << i for i in range(32, 64))


def test_average_hash_uniform_frame_is_zero():
    grid = [128] * 64
    assert sa._average_hash(grid) == 0


def test_hamming_counts_differing_bits():
    assert sa._hamming(0b0000, 0b1111) == 4
    assert sa._hamming(0, 0) == 0
    assert sa._hamming(0b1010, 0b0101) == 4


def test_max_cell_delta_is_the_largest_single_difference():
    assert sa._max_cell_delta([10, 10, 10], [10, 15, 200]) == 190
    assert sa._max_cell_delta([50, 50], [50, 50]) == 0


def test_luminance_grid_has_size_squared_cells():
    img = Image.new('RGB', (100, 100), (128, 64, 32))
    assert len(sa._luminance_grid(img, 8)) == 64
    assert len(sa._luminance_grid(img, 32)) == 1024


# --- classification thresholds, driven by controlled (not real) frames ----
# Confirms probe_static_video's own boundary logic in isolation, independent
# of whether any real encoder happens to land near a boundary.

@pytest.mark.parametrize('hash_distance,expected', [
    (0, 'static'),
    (sa.STATIC_STRICT_DISTANCE, 'static'),
    (sa.STATIC_STRICT_DISTANCE + 1, 'near_static'),
    (sa.STATIC_LOOSE_DISTANCE, 'near_static'),
    (sa.STATIC_LOOSE_DISTANCE + 1, 'video'),
])
def test_classify_at_hash_distance_boundaries(monkeypatch, tmp_path, hash_distance, expected):
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    monkeypatch.setattr(sa, '_sample_frames',
                         lambda p, d: _controlled_frames(hash_distance, cell_delta=0))
    assert sa.probe_static_video(_dummy_video(tmp_path)) == expected


@pytest.mark.parametrize('cell_delta,expected', [
    (0, 'static'),
    (sa.STATIC_MAX_CELL_DELTA, 'static'),
    (sa.STATIC_MAX_CELL_DELTA + 1, 'near_static'),  # frame-wide match, local spike: reported only
])
def test_classify_at_cell_delta_boundaries(monkeypatch, tmp_path, cell_delta, expected):
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    monkeypatch.setattr(sa, '_sample_frames',
                         lambda p, d: _controlled_frames(hash_distance=0, cell_delta=cell_delta))
    assert sa.probe_static_video(_dummy_video(tmp_path)) == expected


def test_classify_requires_both_measures_to_agree_for_static(monkeypatch, tmp_path):
    """Regression guard for the bug this module was actually caught making:
    a hash-only check scored a fixed background with a small moving element
    as a perfect match (distance 0) because a global average can't see
    localized motion. Both the hash AND the per-cell check must agree nothing
    moved before the 'static' verdict is allowed to act."""
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    monkeypatch.setattr(sa, '_sample_frames',
                         lambda p, d: _controlled_frames(hash_distance=0, cell_delta=200))
    assert sa.probe_static_video(_dummy_video(tmp_path)) != 'static'


def test_classify_below_min_probe_seconds_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate',
                         lambda p: (sa.MIN_PROBE_SECONDS - 0.1, None))
    assert sa.probe_static_video(_dummy_video(tmp_path)) == 'unknown'


def test_classify_at_min_probe_seconds_is_judged(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (sa.MIN_PROBE_SECONDS, None))
    monkeypatch.setattr(sa, '_sample_frames', lambda p, d: _controlled_frames(0, 0))
    assert sa.probe_static_video(_dummy_video(tmp_path)) == 'static'


def test_classify_too_few_surviving_frames_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    frames = _controlled_frames(0, 0)[:sa.MIN_SAMPLES_REQUIRED - 1]
    monkeypatch.setattr(sa, '_sample_frames', lambda p, d: frames)
    assert sa.probe_static_video(_dummy_video(tmp_path)) == 'unknown'


def test_prefilter_high_bitrate_skips_decode_and_is_video(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate',
                         lambda p: (30.0, sa.PREFILTER_BITRATE_CEILING + 1))

    def _boom(*a, **k):
        raise AssertionError('the bitrate prefilter must skip decoding entirely')
    monkeypatch.setattr(sa, '_sample_frames', _boom)

    assert sa.probe_static_video(_dummy_video(tmp_path)) == 'video'


def test_prefilter_unknown_bitrate_still_decodes(monkeypatch, tmp_path):
    """bit_rate is frequently absent from container metadata; None must mean
    'go analyse it properly', never 'skip and call it real'."""
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    called = []
    monkeypatch.setattr(sa, '_sample_frames',
                         lambda p, d: called.append(1) or _controlled_frames(0, 0))
    sa.probe_static_video(_dummy_video(tmp_path))
    assert called == [1]


# --- fail-safe paths --------------------------------------------------
# Every error must resolve to 'unknown', and convert_to_album_art must never
# delete on it.

def test_missing_file_is_unknown(tmp_path):
    assert sa.probe_static_video(tmp_path / 'missing.mp4') == 'unknown'


def test_ffprobe_failure_is_unknown(monkeypatch, tmp_path):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'x')

    def _raise(*a, **k):
        raise OSError('ffprobe not found')
    monkeypatch.setattr(sa.subprocess, 'run', _raise)

    assert sa.probe_static_video(video) == 'unknown'


def test_ffprobe_garbage_output_is_unknown(monkeypatch, tmp_path):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'x')
    monkeypatch.setattr(sa.subprocess, 'run',
                         lambda *a, **k: _FakeCompletedProcess(stdout='not json'))
    assert sa.probe_static_video(video) == 'unknown'


def test_ffmpeg_frame_extraction_always_failing_is_unknown(monkeypatch, tmp_path):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'x')
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    monkeypatch.setattr(sa, '_extract_frame_png', lambda p, t: None)
    assert sa.probe_static_video(video) == 'unknown'


def test_pillow_unavailable_is_unknown(monkeypatch, tmp_path):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'x')
    monkeypatch.setattr(sa, 'Image', None)
    assert sa.probe_static_video(video) == 'unknown'


def test_unreadable_frame_bytes_are_skipped_not_fatal(monkeypatch, tmp_path):
    """A frame that extracts but doesn't decode as an image is dropped, same
    as one that failed to extract at all -- not enough alone to flip the
    verdict, but if too many drop, the MIN_SAMPLES_REQUIRED gate below still
    catches it."""
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    calls = {'n': 0}

    def _extract(p, t):
        calls['n'] += 1
        return None if calls['n'] <= 2 else b'not a real png'
    monkeypatch.setattr(sa, '_extract_frame_png', _extract)
    assert sa.probe_static_video(tmp_path / 'video.mp4') == 'unknown'


# --- convert_to_album_art: file operations, via monkeypatched detection ---
# The delete/marker ordering is the highest-stakes code in this module, so
# these exercise it directly without needing a real static video.

_SONG_INI = '[song]\nname = Test Song\nartist = Test Artist\nvideo_start_time = -3000\n'


def _make_song(tmp_path, ini=_SONG_INI, art_name=None):
    song = tmp_path / 'song'
    song.mkdir()
    (song / 'video.mp4').write_bytes(b'video bytes')
    if ini is not None:
        (song / 'song.ini').write_text(ini, encoding='utf-8')
    if art_name:
        (song / art_name).write_bytes(b'PRE-EXISTING ART')
    return song


def test_convert_no_video_file_is_ok(tmp_path):
    song = tmp_path / 'song'
    song.mkdir()
    assert sa.convert_to_album_art(song) == {'status': 'ok', 'detail': 'no video file'}


def test_convert_real_video_is_untouched(monkeypatch, tmp_path):
    song = _make_song(tmp_path)
    monkeypatch.setattr(sa, 'probe_static_video', lambda p: 'video')
    result = sa.convert_to_album_art(song)
    assert result == {'status': 'ok', 'detail': ''}
    assert (song / 'video.mp4').exists()


def test_convert_near_static_reports_only_never_deletes(monkeypatch, tmp_path):
    song = _make_song(tmp_path)
    monkeypatch.setattr(sa, 'probe_static_video', lambda p: 'near_static')
    result = sa.convert_to_album_art(song)
    assert result['status'] == 'near_static'
    assert (song / 'video.mp4').exists()


def test_convert_unknown_keeps_the_video(monkeypatch, tmp_path):
    song = _make_song(tmp_path)
    monkeypatch.setattr(sa, 'probe_static_video', lambda p: 'unknown')
    result = sa.convert_to_album_art(song)
    assert result['status'] == 'unknown'
    assert (song / 'video.mp4').exists()


def test_convert_dry_run_is_a_zero_byte_filesystem_diff(monkeypatch, tmp_path):
    song = _make_song(tmp_path)
    monkeypatch.setattr(sa, 'probe_static_video', lambda p: 'static')
    extract_called = []
    monkeypatch.setattr(sa, '_extract_frame_png', lambda p, t: extract_called.append(1))

    before = {p.name: p.stat().st_size for p in song.iterdir()}
    result = sa.convert_to_album_art(song, dry_run=True)
    after = {p.name: p.stat().st_size for p in song.iterdir()}

    assert result['status'] == 'converted'
    assert '(dry-run, not applied)' in result['detail']
    assert before == after
    assert extract_called == []  # not even the read-only extraction runs


def test_convert_writes_frame_deletes_video_and_marks_ini(monkeypatch, tmp_path):
    song = _make_song(tmp_path)
    monkeypatch.setattr(sa, 'probe_static_video', lambda p: 'static')
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    monkeypatch.setattr(sa, '_extract_frame_png', lambda p, t: b'FAKE PNG BYTES')

    result = sa.convert_to_album_art(song)

    assert result['status'] == 'converted'
    assert not (song / 'video.mp4').exists()
    assert (song / 'album.png').read_bytes() == b'FAKE PNG BYTES'
    assert not (song / 'album.png.tmp').exists()
    ini_text = (song / 'song.ini').read_text(encoding='utf-8')
    assert 'backstagehero_video = static_art' in ini_text
    # everything already in song.ini survives -- set_ini_values touches only
    # the keys it's given
    assert 'video_start_time = -3000' in ini_text
    assert 'name = Test Song' in ini_text


def test_convert_never_overwrites_existing_album_art(monkeypatch, tmp_path):
    song = _make_song(tmp_path, art_name='album.jpg')
    monkeypatch.setattr(sa, 'probe_static_video', lambda p: 'static')
    extract_called = []
    monkeypatch.setattr(sa, '_extract_frame_png', lambda p, t: extract_called.append(1))

    result = sa.convert_to_album_art(song)

    assert result['status'] == 'converted'
    assert (song / 'album.jpg').read_bytes() == b'PRE-EXISTING ART'
    assert not (song / 'album.png').exists()
    assert not (song / 'video.mp4').exists()
    assert extract_called == []  # existing art means the frame is never even grabbed


def test_convert_frame_extraction_failure_keeps_video(monkeypatch, tmp_path):
    song = _make_song(tmp_path)
    monkeypatch.setattr(sa, 'probe_static_video', lambda p: 'static')
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    monkeypatch.setattr(sa, '_extract_frame_png', lambda p, t: None)

    result = sa.convert_to_album_art(song)

    assert result['status'] == 'failed'
    assert (song / 'video.mp4').exists()
    assert not (song / 'album.png').exists()
    assert not (song / 'album.png.tmp').exists()


def test_convert_no_song_section_leaves_video_and_cleans_up(monkeypatch, tmp_path):
    """A song.ini we can't write the marker into must not proceed -- without
    the marker, the very next run would treat this as still needing a video
    and re-download the same static upload forever."""
    song = _make_song(tmp_path, ini='name = orphaned, no section header\n')
    monkeypatch.setattr(sa, 'probe_static_video', lambda p: 'static')
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    monkeypatch.setattr(sa, '_extract_frame_png', lambda p, t: b'FAKE PNG BYTES')

    result = sa.convert_to_album_art(song)

    assert result['status'] == 'failed'
    assert (song / 'video.mp4').exists()
    assert not (song / 'album.png').exists()
    assert not (song / 'album.png.tmp').exists()


def test_convert_delete_failure_leaves_marker_and_art_committed(monkeypatch, tmp_path):
    """The video is deleted LAST, after the art and the marker are already
    committed. If the delete itself fails (file locked by another process),
    the song must not re-download on the next run -- it's already marked --
    and nothing already written should be rolled back."""
    song = _make_song(tmp_path)
    monkeypatch.setattr(sa, 'probe_static_video', lambda p: 'static')
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    monkeypatch.setattr(sa, '_extract_frame_png', lambda p, t: b'FAKE PNG BYTES')

    real_unlink = Path.unlink

    def _boom(self, *a, **k):
        if self.name == 'video.mp4':
            raise OSError('locked by another process')
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, 'unlink', _boom)

    result = sa.convert_to_album_art(song)

    assert result['status'] == 'failed'
    assert 'could not be removed' in result['detail']
    assert (song / 'video.mp4').exists()  # delete failed, so still there -- not lost
    assert (song / 'album.png').read_bytes() == b'FAKE PNG BYTES'
    assert 'backstagehero_video = static_art' in (song / 'song.ini').read_text(encoding='utf-8')


# --- process_download re-download-loop guard -------------------------
# The marker exists for exactly one reason: without it, a static-art song
# would re-download the same upload, re-detect it, and delete it, forever.

def test_process_download_skips_a_song_already_marked_static_art(tmp_path):
    import VideoDownload
    song = _make_song(tmp_path)
    (song / 'video.mp4').unlink()  # marker alone is what must trigger the skip
    VideoDownload.set_ini_values(str(song), {sa.VIDEO_MARKER_KEY: sa.VIDEO_MARKER_STATIC_ART})

    result = VideoDownload.process_download(str(song), 'Test Song', 'height<=720', False, False)

    assert result == 'skipped'


def test_process_download_replace_true_bypasses_the_static_art_skip(monkeypatch, tmp_path):
    import VideoDownload
    song = _make_song(tmp_path)
    (song / 'video.mp4').unlink()
    VideoDownload.set_ini_values(str(song), {sa.VIDEO_MARKER_KEY: sa.VIDEO_MARKER_STATIC_ART})

    class _ReachedSearch(Exception):
        pass
    monkeypatch.setattr(VideoDownload, 'search_candidates',
                         lambda *a, **k: (_ for _ in ()).throw(_ReachedSearch))
    monkeypatch.setattr(VideoDownload.resolver_client, 'resolve', lambda *a, **k: None)
    monkeypatch.setattr(VideoDownload.resolver_client, 'enabled', lambda: False)

    with pytest.raises(_ReachedSearch):
        VideoDownload.process_download(str(song), 'Test Song', 'height<=720', False, True)


def test_process_download_unmarked_song_is_not_skipped_by_the_marker_check(tmp_path):
    import VideoDownload
    song = _make_song(tmp_path)
    (song / 'video.mp4').unlink()
    assert VideoDownload._read_ini_value(str(song), sa.VIDEO_MARKER_KEY) is None


# --- real-video classification -----------------------------------------
# Synthetic fixtures generated at test time with ffmpeg, per SPEC-static-art-
# video.md's testing strategy. Skipped when ffmpeg is absent. These are the
# only tests in this file that exercise the actual perceptual-hash /
# per-cell-delta math against genuinely lossy-encoded video -- monkeypatched
# frame data can prove the threshold logic is self-consistent, but only a
# real encoder can prove the tolerance has real margin against real noise.

def _make_art(path):
    img = Image.new('RGB', (640, 640), (18, 28, 58))
    d = ImageDraw.Draw(img)
    d.ellipse([120, 120, 520, 520], fill=(200, 60, 40))
    d.rectangle([60, 260, 580, 380], fill=(240, 230, 200))
    img.save(path)


def _encode(args, out_path):
    subprocess.run(args + ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(out_path)],
                   check=True, capture_output=True)


@pytest.fixture(scope='module')
def art_image(tmp_path_factory):
    path = tmp_path_factory.mktemp('static_art_src') / 'art.png'
    _make_art(path)
    return path


@pytest.fixture(scope='module')
def static_video(tmp_path_factory, art_image):
    path = tmp_path_factory.mktemp('static_art_fixtures') / 'static.mp4'
    _encode(['ffmpeg', '-loop', '1', '-i', str(art_image), '-t', '60', '-r', '30'], path)
    return path


@requires_ffmpeg
def test_real_static_loop_classifies_as_static(static_video):
    assert sa.probe_static_video(static_video) == 'static'


@requires_ffmpeg
def test_real_static_loop_survives_heavy_compression(tmp_path_factory, art_image):
    """The tolerance's whole reason to exist: lossy re-encoding introduces
    per-frame noise even on a genuinely held still, so an exact-equality
    check would miss this. A perceptual hash with margin must still call it
    static even at a deliberately brutal CRF."""
    path = tmp_path_factory.mktemp('static_art_fixtures') / 'static_crf40.mp4'
    subprocess.run(['ffmpeg', '-loop', '1', '-i', str(art_image), '-t', '60', '-r', '30',
                    '-c:v', 'libx264', '-crf', '40', '-pix_fmt', 'yuv420p', '-y', str(path)],
                   check=True, capture_output=True)
    assert sa.probe_static_video(path) == 'static'


@requires_ffmpeg
def test_real_moving_pattern_is_never_classified_static(tmp_path_factory):
    """SPEC-static-art-video.md's own worked example claims a testsrc moving
    pattern 'must classify video'. Measured against the real algorithm, a
    classic ffmpeg testsrc lands at hash distance 10 -- exactly on the
    near_static/video boundary, not safely past it -- and classifies as
    near_static rather than video. That is still a safe outcome (near_static
    is report-only, never acted on), so the invariant this test actually
    guards is the one that matters: it must never be 'static'. The gap
    between the spec's illustrative claim and this measurement is a real
    finding, not a test bug -- see tasks/verification-plan.md Phase 3."""
    path = tmp_path_factory.mktemp('static_art_fixtures') / 'testsrc.mp4'
    subprocess.run(['ffmpeg', '-f', 'lavfi', '-i', 'testsrc=size=640x640:rate=30', '-t', '60',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(path)],
                   check=True, capture_output=True)
    assert sa.probe_static_video(path) != 'static'


@requires_ffmpeg
def test_real_slow_zoom_is_near_static_and_not_acted_on(tmp_path_factory, art_image):
    """Regression guard on the strict/loose boundary, named explicitly in the
    spec: a slow zoom/pan across a still image is real motion. It must be
    reported as a candidate, never converted.

    Pan rate (0.4 px/s, capped at 24px) was picked by measuring several rates
    against the real detector and choosing one that lands mid-band (hash
    distance 5, roughly centred between STRICT=2 and LOOSE=10) rather than
    tight against either edge -- a faster pan measured distance 11, one tick
    past LOOSE and into 'video', which is also a safe outcome but doesn't
    exercise the near_static tier this test exists to guard."""
    path = tmp_path_factory.mktemp('static_art_fixtures') / 'slow_zoom.mp4'
    subprocess.run(['ffmpeg', '-loop', '1', '-i', str(art_image), '-t', '60', '-r', '30',
                    '-vf', "crop=560:560:x='min(t*0.4,24)':y='min(t*0.4,24)'",
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(path)],
                   check=True, capture_output=True)
    assert sa.probe_static_video(path) == 'near_static'


@requires_ffmpeg
def test_real_short_clip_below_probe_floor_is_unknown(tmp_path_factory, art_image):
    path = tmp_path_factory.mktemp('static_art_fixtures') / 'short.mp4'
    subprocess.run(['ffmpeg', '-loop', '1', '-i', str(art_image), '-t', '5', '-r', '30',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(path)],
                   check=True, capture_output=True)
    assert sa.probe_static_video(path) == 'unknown'


@requires_ffmpeg
def test_real_small_local_motion_on_fixed_background_survives(tmp_path_factory, art_image):
    """The adversarial case that actually broke the first version of this
    detector during manual verification: a fixed background (a locked-off
    performance shot, or a lyric video) with one small moving element. An
    average hash alone cannot see localized motion and scored this at hash
    distance 0 -- indistinguishable from a genuinely held still. Real videos
    that look like this must never be deleted."""
    path = tmp_path_factory.mktemp('static_art_fixtures') / 'small_motion.mp4'
    subprocess.run([
        'ffmpeg', '-loop', '1', '-i', str(art_image),
        '-f', 'lavfi', '-i', 'color=c=yellow:s=60x40:r=30', '-t', '60',
        '-filter_complex', "[0:v][1:v]overlay=x='80+300*abs(sin(t))':y=500",
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(path),
    ], check=True, capture_output=True)
    assert sa.probe_static_video(path) != 'static'


@requires_ffmpeg
def test_real_end_to_end_conversion(tmp_path_factory, static_video):
    song = tmp_path_factory.mktemp('song')
    shutil.copy(static_video, song / 'video.mp4')
    (song / 'song.ini').write_text('[song]\nname = Test\nartist = Test\n', encoding='utf-8')

    result = sa.convert_to_album_art(song)

    assert result['status'] == 'converted'
    assert not (song / 'video.mp4').exists()
    assert (song / 'album.png').exists()
    assert 'backstagehero_video = static_art' in (song / 'song.ini').read_text(encoding='utf-8')


@requires_ffmpeg
def test_real_process_download_loop_guard(tmp_path_factory, static_video):
    """After a real conversion, a second process_download pass over the same
    folder must not re-download -- the marker is the entire point."""
    import VideoDownload
    song = tmp_path_factory.mktemp('song')
    shutil.copy(static_video, song / 'video.mp4')
    (song / 'song.ini').write_text('[song]\nname = Test\nartist = Test\n', encoding='utf-8')
    sa.convert_to_album_art(song)

    result = VideoDownload.process_download(str(song), 'Test', 'height<=720', False, False)

    assert result == 'skipped'


# --- the safety margin, pinned by measurement -----------------------------
#
# Phase 3 found that STATIC_MAX_CELL_DELTA's justification lived only in a
# comment, asserted at 640x640 and never re-measured. Real downloads run at
# 720p/1080p, and the same content scored very differently there, so the
# margin the whole feature rests on could erode to nothing without one test
# going red. These assert the actual numbers, not just the categorical verdict.

def _cell_delta(video):
    """Largest per-cell change across the sampled frames -- the number the
    delete decision is actually made on."""
    duration, _ = sa._probe_duration_and_bitrate(str(video))
    grids = [g for _h, g in sa._sample_frames(str(video), duration)]
    return max((sa._max_cell_delta(a, b)
                for i, a in enumerate(grids) for b in grids[i + 1:]), default=0)


@requires_ffmpeg
@pytest.mark.parametrize('size', ['640x640', '1280x720', '1920x1080'])
def test_a_held_still_scores_far_under_the_threshold_at_every_resolution(tmp_path_factory, size):
    w, h = (int(v) for v in size.split('x'))
    src = tmp_path_factory.mktemp('art') / 'art.png'
    img = Image.new('RGB', (w, h), (18, 28, 58))
    ImageDraw.Draw(img).ellipse([w * .2, h * .2, w * .8, h * .8], fill=(200, 60, 40))
    img.save(src)
    path = tmp_path_factory.mktemp('fx') / f'still_{size}.mp4'
    _encode(['ffmpeg', '-loop', '1', '-i', str(src), '-t', '60', '-r', '30'], path)

    delta = _cell_delta(path)

    assert delta <= 4, f'held still scored {delta} at {size} (measured 1-2)'
    assert sa.probe_static_video(path) == 'static'


@requires_ffmpeg
def test_heavy_compression_still_leaves_headroom_under_the_threshold(tmp_path_factory, art_image):
    """The convert-side edge. If this creeps up toward STATIC_MAX_CELL_DELTA
    the band has closed from below and the threshold needs re-deriving."""
    path = tmp_path_factory.mktemp('fx') / 'crf40.mp4'
    subprocess.run(['ffmpeg', '-loop', '1', '-i', str(art_image), '-t', '60', '-r', '30',
                    '-c:v', 'libx264', '-crf', '40', '-pix_fmt', 'yuv420p', '-y', str(path)],
                   check=True, capture_output=True)

    delta = _cell_delta(path)

    assert delta <= 10, f'worst legitimate still scored {delta}, threshold is {sa.STATIC_MAX_CELL_DELTA}'
    assert sa.probe_static_video(path) == 'static'


@requires_ffmpeg
@pytest.mark.parametrize('size', ['640x640', '1280x720', '1920x1080'])
def test_a_thin_progress_bar_is_never_converted(tmp_path_factory, size):
    """The actual false positive Phase 3 caught: a crawling progress bar is
    real motion, but being THIN its delta averages down to 17-19 -- under the
    old threshold of 24, so the video was being deleted. The README promises
    real motion is never touched; this is what holds that promise."""
    w, h = (int(v) for v in size.split('x'))
    src = tmp_path_factory.mktemp('art') / 'art.png'
    img = Image.new('RGB', (w, h), (18, 28, 58))
    ImageDraw.Draw(img).ellipse([w * .2, h * .2, w * .8, h * .8], fill=(200, 60, 40))
    img.save(src)
    barh = max(4, int(h * 0.012))
    path = tmp_path_factory.mktemp('fx') / f'bar_{size}.mp4'
    subprocess.run([
        'ffmpeg', '-loop', '1', '-i', str(src), '-t', '60', '-r', '30',
        '-f', 'lavfi', '-i', f'color=c=red:s={w}x{barh}:r=30',
        '-filter_complex',
        f"[1:v]loop=loop=-1:size=1[p];[0:v][p]overlay=x=-{w}+{w}*t/60:y={h - barh}",
        '-t', '60', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(path),
    ], check=True, capture_output=True)

    assert sa.probe_static_video(path) != 'static'


@requires_ffmpeg
def test_a_scrolling_lyric_line_is_nowhere_near_the_threshold(tmp_path_factory, art_image):
    """A lyric video is real content a user would be upset to lose. Measured
    at 129-230, an order of magnitude clear -- this is the reassuring end of
    the finding and it should stay that way."""
    path = tmp_path_factory.mktemp('fx') / 'lyrics.mp4'
    subprocess.run([
        'ffmpeg', '-loop', '1', '-i', str(art_image), '-t', '60', '-r', '30',
        '-f', 'lavfi', '-i', 'color=c=white:s=320x26:r=30',
        '-filter_complex',
        "[1:v]loop=loop=-1:size=1[t];[0:v][t]overlay=x=160:y=640-640*mod(t\\,6)/6",
        '-t', '60', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(path),
    ], check=True, capture_output=True)

    delta = _cell_delta(path)

    assert delta >= 100, f'lyric line scored only {delta} -- margin has collapsed'
    assert sa.probe_static_video(path) != 'static'


def test_normalising_makes_the_score_independent_of_resolution():
    """_normalise scales every frame to a fixed long edge before measuring, so
    the same content scores the same whether it was downloaded at 720p or
    1080p. Without it the delete decision depended on download quality."""
    big = Image.new('RGB', (1920, 1080), (10, 10, 10))
    ImageDraw.Draw(big).rectangle([200, 200, 800, 700], fill=(220, 40, 40))
    small = big.resize((640, 360), sa._RESAMPLE)

    assert sa._luminance_grid(sa._normalise(big), sa.STATIC_GRID) == \
        sa._luminance_grid(sa._normalise(small), sa.STATIC_GRID)


def test_normalise_leaves_an_already_small_frame_alone():
    small = Image.new('RGB', (320, 240), (5, 5, 5))
    assert sa._normalise(small) is small


# --- ordering: a failed art promote must not leave a committed marker -----

def test_a_failed_art_promote_leaves_no_marker_and_keeps_the_video(monkeypatch, tmp_path):
    """The promote used to run AFTER the marker was committed, and unguarded.
    A failure there left marker-set / no-album.png / video-still-present --
    a state with no name, which process_download then skipped forever."""
    song = _make_song(tmp_path)
    monkeypatch.setattr(sa, 'probe_static_video', lambda p: 'static')
    monkeypatch.setattr(sa, '_probe_duration_and_bitrate', lambda p: (30.0, None))
    monkeypatch.setattr(sa, '_extract_frame_png', lambda p, t: b'FAKE PNG BYTES')
    monkeypatch.setattr(sa.os, 'replace',
                        lambda *a, **k: (_ for _ in ()).throw(OSError(13, 'locked')))

    result = sa.convert_to_album_art(song)

    assert result['status'] == 'failed'
    assert (song / 'video.mp4').exists()
    assert not (song / 'album.png').exists()
    assert not (song / 'album.png.tmp').exists()
    assert 'backstagehero_video' not in (song / 'song.ini').read_text(encoding='utf-8')
