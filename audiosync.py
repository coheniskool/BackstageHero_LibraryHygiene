"""
Audio alignment for Clone Hero background videos.

Given a downloaded video and the chart's own audio stems, this works out how far
into the video the song actually starts, so the video can be lined up with the
chart automatically instead of relying on a fixed guess.

The method is landmark-based audio fingerprinting (the same idea Shazam uses):
find robust peaks in each spectrogram, hash pairs of peaks into fingerprints,
then look for a consistent time offset between the two recordings. Because it
matches the *pattern* of peaks rather than the raw waveform, it survives the EQ,
loudness, and master differences you get when the YouTube result is not the exact
same file as the chart audio. When the match is weak (a live take, a remix, the
wrong song entirely) it reports no result and the caller falls back to the default.

Only numpy and ffmpeg are required.
"""

import os
import subprocess

# Suppress the console window ffmpeg would otherwise flash on a windowed build.
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

try:
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    _NUMPY = True
except Exception:
    _NUMPY = False


# Tunables

SAMPLE_RATE = 8000      # Hz. Plenty for fingerprinting; keeps the FFTs small.
ANALYZE_SECONDS = 240   # Only the first few minutes are needed to find the offset.

N_FFT = 1024            # ~128 ms window at 8 kHz.
HOP = 128               # ~16 ms between frames -> 16 ms offset resolution.

PEAK_TIME_RADIUS = 5    # Local-maximum neighbourhood, in frames...
PEAK_FREQ_RADIUS = 10   # ...and in frequency bins.
PEAK_QUANTILE = 0.97    # Per-frame magnitude cutoff before a point can be a peak.

FAN_OUT = 8             # Pair each anchor peak with up to this many later peaks.
DT_MIN = 1              # Minimum frame gap between paired peaks.
DT_MAX = 120            # Maximum frame gap (~2 s).

HIST_BIN = 3            # Offset histogram bin width, in frames (~48 ms).

# An alignment is accepted only when all three of these agree. They were
# calibrated on real Clone Hero charts aligned against the matching YouTube
# audio: a genuine same-recording match lands far above every threshold
# (thousands of votes, concentration above 0.2), while a wrong song or a
# different master sits near the noise floor (a handful of votes, concentration
# below 0.01). The gap between the two is enormous, so the only job here is to
# pick values that fall inside it.
MIN_MATCHES = 40        # Votes in the winning offset bin.
MIN_SCORE = 50.0        # Winning bin height relative to the average bin.
MIN_CONC = 0.05         # Share of all votes that land on the winning offset.

# Reject offsets outside a plausible window for a music-video lead-in.
MIN_LEAD_SECONDS = -30.0
MAX_LEAD_SECONDS = 90.0

# Audio stems that should not feed the fingerprint: a short preview clip is not
# the full song, and crowd noise is not in the studio video.
EXCLUDE_STEMS = {'preview', 'crowd'}
AUDIO_EXTS = {'.ogg', '.opus', '.mp3', '.wav', '.m4a', '.flac'}


def is_available():
    """True if auto-sync can run (numpy importable)."""
    return _NUMPY


# ffmpeg decoding

def _decode(inputs, sr, seconds):
    """Decode one or more audio files down to a single mono float array.

    Multiple inputs are mixed together with ffmpeg's amix so a chart split into
    instrument stems is treated as one full-song waveform. Returns None on any
    failure (missing ffmpeg, unreadable file, empty output).
    """
    cmd = ['ffmpeg', '-v', 'quiet']
    for path in inputs:
        cmd += ['-i', path]

    if len(inputs) > 1:
        mix = ''.join(f'[{i}:a]' for i in range(len(inputs)))
        mix += f'amix=inputs={len(inputs)}[a]'
        cmd += ['-filter_complex', mix, '-map', '[a]']

    cmd += ['-t', str(seconds), '-ac', '1', '-ar', str(sr), '-f', 's16le', '-']

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None

    samples = np.frombuffer(proc.stdout, dtype='<i2').astype(np.float32) / 32768.0
    return samples if samples.size >= N_FFT else None


def _chart_stems(folder, exclude=None):
    """A song folder's audio stems, minus preview/crowd and any excluded file.

    The excluded path is the throwaway audio fetched for syncing, which lives in
    the same folder and must not be mistaken for part of the chart."""
    exclude = os.path.abspath(exclude) if exclude else None
    stems = []
    for name in os.listdir(folder):
        base, ext = os.path.splitext(name)
        if ext.lower() not in AUDIO_EXTS or base.lower() in EXCLUDE_STEMS:
            continue
        path = os.path.join(folder, name)
        if exclude and os.path.abspath(path) == exclude:
            continue
        stems.append(path)
    return stems


# Fingerprinting

def _spectrogram(x):
    frames = sliding_window_view(x, N_FFT)[::HOP]
    frames = frames * np.hanning(N_FFT).astype(np.float32)
    return np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)


def _peaks(spec):
    """Spectral peaks as (time_frame, freq_bin) arrays.

    A point is kept if it is the maximum within its time/frequency rectangle and
    rises above a per-frame magnitude cutoff. A max filter over a rectangle is
    separable, so it is computed as a rolling max along each axis in turn.
    """
    padded = np.pad(spec, ((0, 0), (PEAK_FREQ_RADIUS, PEAK_FREQ_RADIUS)))
    fmax = sliding_window_view(padded, 2 * PEAK_FREQ_RADIUS + 1, axis=1).max(axis=-1)
    padded = np.pad(fmax, ((PEAK_TIME_RADIUS, PEAK_TIME_RADIUS), (0, 0)))
    local_max = sliding_window_view(padded, 2 * PEAK_TIME_RADIUS + 1, axis=0).max(axis=-1)

    floor = spec.max() * 1e-3
    frame_cut = np.quantile(spec, PEAK_QUANTILE, axis=1, keepdims=True)
    mask = (spec == local_max) & (spec >= frame_cut) & (spec > floor)

    t, f = np.nonzero(mask)   # np.nonzero returns time-ascending order
    return t, f


def _fingerprint(t, f):
    """Map each fingerprint hash to the anchor times that produced it.

    Peaks arrive sorted by time, so paired targets are simply the next few peaks
    within the allowed gap. Each hash packs the two frequency bins and the gap.
    """
    table = {}
    n = len(t)
    for i in range(n):
        ti, fi = t[i], f[i]
        paired = 0
        j = i + 1
        while j < n and paired < FAN_OUT:
            dt = t[j] - ti
            if dt < DT_MIN:
                j += 1
                continue
            if dt > DT_MAX:
                break
            h = (int(fi) & 0x3FF) | ((int(f[j]) & 0x3FF) << 10) | ((int(dt) & 0x7F) << 20)
            table.setdefault(h, []).append(int(ti))
            paired += 1
            j += 1
    return table


def _offset_seconds(ref_table, probe_table):
    """Most consistent time offset between probe (audio) and reference (chart).

    For every shared hash, the difference between the probe anchor time and the
    reference anchor time is one vote. The true lead-in shows up as a sharp spike
    in the vote histogram; everything else is spread thin. Returns
    (lead_seconds, votes, score, conc) or None when no spike clears the gates.
    """
    deltas = []
    for h, probe_times in probe_table.items():
        ref_times = ref_table.get(h)
        if not ref_times:
            continue
        for tp in probe_times:
            for tr in ref_times:
                deltas.append(tp - tr)   # audio time minus chart time = lead-in

    if len(deltas) < MIN_MATCHES:
        return None

    deltas = np.asarray(deltas, dtype=np.float64)
    bins = np.round(deltas / HIST_BIN).astype(np.int64)
    values, counts = np.unique(bins, return_counts=True)

    top = counts.argmax()
    votes = int(counts[top])
    score = votes / counts.mean()

    # Votes within one bin of the winner, as a share of all votes. A real match
    # piles most of its votes onto a single offset; noise stays spread out.
    near = np.abs(bins - values[top]) <= 1
    conc = float(near.sum()) / len(deltas)

    if votes < MIN_MATCHES or score < MIN_SCORE or conc < MIN_CONC:
        return None

    # Refine using the exact deltas in and next to the winning bin.
    lead = float(deltas[near].mean()) * HOP / SAMPLE_RATE
    if not (MIN_LEAD_SECONDS <= lead <= MAX_LEAD_SECONDS):
        return None
    return lead, votes, score, conc


# Public entry point

def compute_offset_ms(folder, probe_audio):
    """Work out video_start_time (ms) for one song folder.

    `probe_audio` is the audio track of the chosen YouTube result (the saved
    video.mp4 has no audio, so its audio is fetched separately for this). It is
    aligned against the chart's own stems.

    Returns (milliseconds, info_string) on success, or (None, reason_string) when
    auto-sync is unavailable or the match is not trustworthy. The sign follows the
    convention the original tool shipped: audio that starts L seconds into the
    video gets video_start_time = -L*1000 (so a 3 s lead-in reproduces -3000).
    """
    if not _NUMPY:
        return None, 'numpy not available'

    if not probe_audio or not os.path.exists(probe_audio):
        return None, 'no audio to sync'

    stems = _chart_stems(folder, exclude=probe_audio)
    if not stems:
        return None, 'no chart audio found'

    chart = _decode(stems, SAMPLE_RATE, ANALYZE_SECONDS)
    probe = _decode([probe_audio], SAMPLE_RATE, ANALYZE_SECONDS)
    if chart is None or probe is None:
        return None, 'could not decode audio'

    ref_table = _fingerprint(*_peaks(_spectrogram(chart)))
    probe_table = _fingerprint(*_peaks(_spectrogram(probe)))
    if not ref_table or not probe_table:
        return None, 'not enough audio detail'

    result = _offset_seconds(ref_table, probe_table)
    if result is None:
        return None, 'no confident match'

    lead, votes, score, conc = result
    ms = -int(round(lead * 1000))
    info = f'lead-in {lead:.2f}s, {votes} matches, {score:.0f}x confidence'
    return ms, info
