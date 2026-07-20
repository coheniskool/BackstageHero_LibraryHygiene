# Empirically locks audiosync.compute_offset_ms()'s sign convention before
# any hygiene code (or the metadata-enrichment/dedupe features) is built on
# top of it -- same discipline as clonehero-video-downloader's
# test_compute_offset_sign.py (plan.md Checkpoint 1 / Task 3).
#
# Instead of a real ffmpeg "-itsoffset" shell delay, the known delay is
# injected by prepending exact silence to a synthetic reference signal --
# equally deterministic, and avoids ffmpeg CLI quoting/format edge cases.
# ffmpeg is still exercised for real: audiosync._decode() shells out to it to
# turn these WAV fixtures into the PCM it fingerprints.
#
# The reference signal is seeded broadband noise, not a tone (an earlier
# attempt at a multi-frequency tone-burst fixture was tried and rejected: a
# landmark fingerprinter's own peak-frequency at a fixed pitch repeats
# identically across every frame within a burst, which is exactly the kind
# of self-similar content audiosync.py's ambiguity/score gates are built to
# resist -- it produced a broad correlation ridge instead of a sharp spike
# and never cleared MIN_SCORE. Broadband noise's per-frame spectral peaks
# vary continuously, giving the fingerprinter unique time-frequency anchors
# and a single sharp, confidently-gated correlation spike (empirically
# verified at 16s/score ~137, comfortably clear of MIN_SCORE=50).

import wave

import numpy as np
import pytest

import audiosync

pytestmark = pytest.mark.skipif(not audiosync.is_available(), reason='numpy not available')

_DURATION_SECONDS = 16.0
_SR = 22050
_DELAY_SECONDS = 3.25
_SEED = 12345


def _noise_signal(duration_seconds, sr, seed, amplitude=0.3):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(duration_seconds * sr)).astype(np.float32) * amplitude)


def _write_wav(path, signal, sr):
    pcm = np.clip(signal * 32767, -32768, 32767).astype('<i2')
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def test_compute_offset_ms_sign_and_magnitude(tmp_path):
    reference = _noise_signal(_DURATION_SECONDS, _SR, _SEED)
    silence = np.zeros(int(_DELAY_SECONDS * _SR), dtype=np.float32)
    delayed = np.concatenate([silence, reference])  # simulates the video's audio having a lead-in

    song_dir = tmp_path / 'Test Song'
    song_dir.mkdir()
    _write_wav(song_dir / 'song.wav', reference, _SR)

    probe_path = tmp_path / 'probe.wav'
    _write_wav(probe_path, delayed, _SR)

    ms, info, conf = audiosync.compute_offset_ms(str(song_dir), str(probe_path))

    assert ms is not None, f'expected a confident match, got: {info}'
    # docstring convention: audio starting L seconds into the probe (video)
    # gives video_start_time = -L*1000 -- a lead-in is reported as negative.
    expected_ms = -round(_DELAY_SECONDS * 1000)
    assert abs(ms - expected_ms) <= 100, f'expected ~{expected_ms}ms, got {ms}ms ({info})'
    assert conf > 0


def test_compute_offset_ms_no_probe_audio_returns_none(tmp_path):
    song_dir = tmp_path / 'Test Song'
    song_dir.mkdir()
    _write_wav(song_dir / 'song.wav', _noise_signal(_DURATION_SECONDS, _SR, _SEED), _SR)

    ms, info, conf = audiosync.compute_offset_ms(str(song_dir), str(tmp_path / 'does_not_exist.wav'))

    assert ms is None
    assert conf == 0.0
