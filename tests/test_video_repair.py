import json
from pathlib import Path

import video_repair as vr


class _FakeCompletedProcess:
    def __init__(self, stdout='', returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _ffprobe_rate_json(r_rate, avg_rate):
    return json.dumps({'streams': [{'r_frame_rate': r_rate, 'avg_frame_rate': avg_rate}]})


def _ffprobe_codec_json(codec_name):
    return json.dumps({'streams': [{'codec_name': codec_name}]})


# --- probe_frame_rate --------------------------------------------------

def test_probe_frame_rate_true_for_vfr(monkeypatch, tmp_path):
    monkeypatch.setattr(vr.subprocess, 'run',
                         lambda *a, **k: _FakeCompletedProcess(_ffprobe_rate_json('30/1', '29/1')))
    assert vr.probe_frame_rate(tmp_path / 'video.mp4') is True


def test_probe_frame_rate_false_for_cfr(monkeypatch, tmp_path):
    monkeypatch.setattr(vr.subprocess, 'run',
                         lambda *a, **k: _FakeCompletedProcess(_ffprobe_rate_json('30/1', '30/1')))
    assert vr.probe_frame_rate(tmp_path / 'video.mp4') is False


def test_probe_frame_rate_false_on_ffprobe_failure(monkeypatch, tmp_path):
    def _raise(*a, **k):
        raise OSError('ffprobe not found')
    monkeypatch.setattr(vr.subprocess, 'run', _raise)
    assert vr.probe_frame_rate(tmp_path / 'video.mp4') is False


# --- probe_video_codec --------------------------------------------------

def test_probe_video_codec_returns_codec_name(monkeypatch, tmp_path):
    monkeypatch.setattr(vr.subprocess, 'run', lambda *a, **k: _FakeCompletedProcess(_ffprobe_codec_json('vp9')))
    assert vr.probe_video_codec(tmp_path / 'video.webm') == 'vp9'


def test_probe_video_codec_none_on_failure(monkeypatch, tmp_path):
    def _raise(*a, **k):
        raise OSError('ffprobe not found')
    monkeypatch.setattr(vr.subprocess, 'run', _raise)
    assert vr.probe_video_codec(tmp_path / 'video.webm') is None


# --- reencode_to_cfr --------------------------------------------------

def test_reencode_to_cfr_success_replaces_file(monkeypatch, tmp_path):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'original bytes')

    def _fake_run(cmd, **kwargs):
        # simulate ffmpeg writing its output to the temp path it was given
        Path(cmd[-1]).write_bytes(b're-encoded bytes')
        return _FakeCompletedProcess()

    monkeypatch.setattr(vr.subprocess, 'run', _fake_run)

    ok = vr.reencode_to_cfr(video)

    assert ok is True
    assert video.read_bytes() == b're-encoded bytes'
    assert list(tmp_path.glob('*.mp4')) == [video]  # no leftover temp file


def test_reencode_to_cfr_failure_cleans_up_temp_and_leaves_original(monkeypatch, tmp_path):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'original bytes')

    def _raise(cmd, **kwargs):
        raise RuntimeError('ffmpeg crashed')

    monkeypatch.setattr(vr.subprocess, 'run', _raise)

    ok = vr.reencode_to_cfr(video)

    assert ok is False
    assert video.read_bytes() == b'original bytes'
    assert list(tmp_path.glob('*.mp4')) == [video]  # temp file cleaned up, not left behind


# --- ensure_playable --------------------------------------------------

def test_ensure_playable_no_file_returns_ok(tmp_path):
    result = vr.ensure_playable(tmp_path / 'missing.mp4')
    assert result == {'status': 'ok', 'detail': 'no video file'}


def test_ensure_playable_cfr_video_is_ok(monkeypatch, tmp_path):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'x')
    monkeypatch.setattr(vr, 'probe_frame_rate', lambda p: False)

    result = vr.ensure_playable(video)

    assert result['status'] == 'ok'


def test_ensure_playable_vfr_video_gets_reencoded(monkeypatch, tmp_path):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'x')
    monkeypatch.setattr(vr, 'probe_frame_rate', lambda p: True)
    monkeypatch.setattr(vr, 'reencode_to_cfr', lambda p: True)

    result = vr.ensure_playable(video)

    assert result['status'] == 'reencoded_cfr'


def test_ensure_playable_reencode_failure_is_reported(monkeypatch, tmp_path):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'x')
    monkeypatch.setattr(vr, 'probe_frame_rate', lambda p: True)
    monkeypatch.setattr(vr, 'reencode_to_cfr', lambda p: False)

    result = vr.ensure_playable(video)

    assert result['status'] == 'reencode_failed'


def test_ensure_playable_dry_run_vfr_never_calls_reencode(monkeypatch, tmp_path):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'original bytes')
    monkeypatch.setattr(vr, 'probe_frame_rate', lambda p: True)
    called = []
    monkeypatch.setattr(vr, 'reencode_to_cfr', lambda p: called.append(1) or True)

    result = vr.ensure_playable(video, dry_run=True)

    assert result['status'] == 'reencoded_cfr'
    assert '(dry-run, not applied)' in result['detail']
    assert called == []
    assert video.read_bytes() == b'original bytes'  # untouched


def test_ensure_playable_dry_run_never_removes_codec(monkeypatch, tmp_path):
    video = tmp_path / 'video.webm'
    video.write_bytes(b'x')
    monkeypatch.setattr(vr, 'probe_video_codec', lambda p: 'vp9')
    monkeypatch.setattr(vr, 'probe_frame_rate', lambda p: False)

    result = vr.ensure_playable(video, allow_codec_removal=True, dry_run=True)

    assert result['status'] == 'removed_unsupported_codec'
    assert '(dry-run, not applied)' in result['detail']
    assert video.exists()  # never actually removed


def test_ensure_playable_inline_mode_never_probes_codec(monkeypatch, tmp_path):
    """allow_codec_removal defaults to False -- the inline post-download hook
    must never delete a video (BackstageHero's own downloads are always
    remuxed AVC .mp4; codec removal is standalone-scan-only)."""
    video = tmp_path / 'video.webm'
    video.write_bytes(b'x')
    called = []
    monkeypatch.setattr(vr, 'probe_video_codec', lambda p: called.append(1) or 'vp9')
    monkeypatch.setattr(vr, 'probe_frame_rate', lambda p: False)

    result = vr.ensure_playable(video)  # allow_codec_removal defaults False

    assert called == []  # codec probe never even attempted
    assert video.exists()
    assert result['status'] == 'ok'


def test_ensure_playable_standalone_mode_removes_unsupported_webm_codec(monkeypatch, tmp_path):
    video = tmp_path / 'video.webm'
    video.write_bytes(b'x')
    monkeypatch.setattr(vr, 'probe_video_codec', lambda p: 'vp9')

    result = vr.ensure_playable(video, allow_codec_removal=True)

    assert result['status'] == 'removed_unsupported_codec'
    assert not video.exists()


def test_ensure_playable_standalone_mode_keeps_vp8_webm(monkeypatch, tmp_path):
    video = tmp_path / 'video.webm'
    video.write_bytes(b'x')
    monkeypatch.setattr(vr, 'probe_video_codec', lambda p: 'vp8')
    monkeypatch.setattr(vr, 'probe_frame_rate', lambda p: False)

    result = vr.ensure_playable(video, allow_codec_removal=True)

    assert result['status'] == 'ok'
    assert video.exists()


# --- scan_and_repair_video_library --------------------------------------------------

def test_scan_and_repair_video_library_processes_all_folders(monkeypatch, tmp_path):
    (tmp_path / 'Song A').mkdir()
    (tmp_path / 'Song A' / 'video.mp4').write_bytes(b'x')
    (tmp_path / 'Song B').mkdir()
    (tmp_path / 'Song B' / 'video.webm').write_bytes(b'x')
    (tmp_path / 'Song C').mkdir()  # no video at all
    (tmp_path / '_needs_review').mkdir()  # leading-underscore folder, must be skipped
    (tmp_path / '_needs_review' / 'video.mp4').write_bytes(b'x')

    calls = []

    def _fake_ensure_playable(video_path, *, allow_codec_removal=False, dry_run=False):
        calls.append((Path(video_path).parent.name, allow_codec_removal))
        if 'Song B' in str(video_path):
            return {'status': 'removed_unsupported_codec', 'detail': 'video.webm (vp9) removed'}
        return {'status': 'ok', 'detail': ''}

    monkeypatch.setattr(vr, 'ensure_playable', _fake_ensure_playable)

    counts = vr.scan_and_repair_video_library(tmp_path)

    assert ('Song A', True) in calls
    assert ('Song B', True) in calls
    assert all(name != '_needs_review' for name, _ in calls)
    assert counts == {'ok': 1, 'removed_unsupported_codec': 1}
    assert len(calls) == 2  # Song C has no video, never calls ensure_playable


def test_scan_and_repair_video_library_checks_every_video_in_a_folder(monkeypatch, tmp_path):
    """Regression: a folder with a good video.mp4 AND a stale video.webm must
    have BOTH inspected -- find_video_file() returns only the first match
    (mp4 shadows webm), which would leave a broken VP9 webm unexamined
    forever."""
    song = tmp_path / 'Song Both'
    song.mkdir()
    (song / 'video.mp4').write_bytes(b'x')
    (song / 'video.webm').write_bytes(b'x')

    seen = []
    monkeypatch.setattr(vr, 'ensure_playable',
                         lambda video_path, *, allow_codec_removal=False, dry_run=False:
                         seen.append(Path(video_path).name) or {'status': 'ok', 'detail': ''})

    vr.scan_and_repair_video_library(tmp_path)

    assert set(seen) == {'video.mp4', 'video.webm'}
