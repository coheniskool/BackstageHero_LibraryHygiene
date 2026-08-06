# Fail-fast preconditions + consecutive-failure circuit breaker
# (SPEC-fail-fast-preconditions.md). Both halves exist because of the same
# 2026-08-05 run: a broken precondition failed thousands of songs in a row while
# the app reported itself as running normally. Nothing here fixes an underlying
# cause -- these tests pin the app's ability to *notice*.

import types
from pathlib import Path

import pytest

import library_common
import VideoDownload as vd

ctk = pytest.importorskip('customtkinter')
import gui

# Reuse the download-loop harness rather than growing a second copy of it --
# these tests drive the same _dl_thread, and two sets of fakes for one loop
# would drift apart the first time the loop changes.
from test_background_mode_controller import (
    _FakeSong, _FakeStopEvent, _bare_app, _drain, _kinds)


def _script_runner(script):
    """Fake run_song_with_backoff driven by a list of scripted outcomes:
    'ok' | 'skipped' | 'stop' | 'stopped' | ('error', message).

    ('error', msg) reproduces what the real function does on a generic
    failure (VideoDownload.py:1764-1770): append to `errored` and return
    'ok'. That asymmetry -- an error reported as a successful return -- is
    exactly why the caller could not tell a dud song from a broken run."""
    def fake(folder, label, quality, sync_ready, replace, resync,
             errored, stop_evt, events):
        entry = script.pop(0)
        if isinstance(entry, tuple) and entry[0] == 'error':
            errored.append(entry[1])
            return 'ok'
        return entry
    return fake


def _songs(count):
    return [_FakeSong('C:/Songs/S%d' % n, 'Song %d' % n) for n in range(count)]


@pytest.fixture
def foreground(monkeypatch):
    """A bare App wired for a default (non-background) download run."""
    monkeypatch.setattr(gui, 'get_stored_resolution', lambda folder: None)
    app = _bare_app()
    app._stop_evt = _FakeStopEvent()
    return app


def _module_missing_youtubedl(file_path=r'C:\somewhere\yt_dlp\__init__.py'):
    """A stand-in for the module the 2026-08-05 process actually got: imports
    fine, has a __file__, has no YoutubeDL on it."""
    mod = types.ModuleType('yt_dlp')
    mod.__file__ = file_path
    return mod


# --- Part A: import guard --------------------------------------------------

def test_assert_ytdlp_usable_raises_when_youtubedl_missing():
    with pytest.raises(ImportError):
        vd._assert_ytdlp_usable(_module_missing_youtubedl())


def test_assert_ytdlp_usable_message_names_the_resolved_path():
    # The failure mode is "imported the wrong thing", not "did not import", so
    # the path is the whole diagnostic -- without it the message is unactionable.
    ghost = r'C:\ghost\yt_dlp\__init__.py'
    with pytest.raises(ImportError) as exc:
        vd._assert_ytdlp_usable(_module_missing_youtubedl(ghost))
    assert ghost in str(exc.value)


def test_assert_ytdlp_usable_survives_a_module_with_no_file_at_all():
    # The exact 2026-08-05 shape: a namespace package (directory resolved,
    # __init__.py did not) has no __file__ attribute whatsoever. The guard must
    # still raise ImportError -- an AttributeError from inside the guard would
    # be its own confusing failure on top of the one being reported.
    nameless = types.ModuleType('yt_dlp')
    assert not hasattr(nameless, '__file__')
    with pytest.raises(ImportError):
        vd._assert_ytdlp_usable(nameless)


def test_assert_ytdlp_usable_passes_for_the_real_yt_dlp():
    # The module VideoDownload actually imported. If this ever fails, the app
    # genuinely cannot download and the guard is doing its job.
    assert vd._assert_ytdlp_usable(vd.yt_dlp) is None


def test_consecutive_error_limit_is_a_sane_positive_int():
    # Range rather than an exact value: the number is a judgement call
    # (SPEC-fail-fast-preconditions.md Open Question 1), so pinning it exactly
    # would make tuning it a test failure. The bounds are what actually matter
    # -- below ~5 a bad patch of dud videos trips it, above ~100 it stops being
    # a circuit breaker and becomes a formality.
    assert isinstance(vd.CONSECUTIVE_ERROR_LIMIT, int)
    assert 5 <= vd.CONSECUTIVE_ERROR_LIMIT <= 100


def test_videodownload_calls_the_guard_right_after_importing_yt_dlp():
    """The predicate is unit-testable; the *call* is import-time code that
    pytest can never reach, so nothing else would fail if it were deleted.
    Mirrors the source-presence check Task 1 of SPEC-cookie-fallback-fix.md
    used for make_console_encoding_safe()."""
    source = (Path(library_common.__file__).parent / 'VideoDownload.py').read_text(
        encoding='utf-8')
    assert '_assert_ytdlp_usable(yt_dlp)' in source
    # Order matters: guarding before the import would be checking the wrong thing.
    assert source.index('import yt_dlp\n') < source.index('_assert_ytdlp_usable(yt_dlp)')


# --- Part B: consecutive-failure circuit breaker, default (foreground) run ---

def test_one_short_of_the_limit_does_not_trip(foreground, monkeypatch):
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    monkeypatch.setattr(gui, 'run_song_with_backoff',
                        _script_runner([('error', 'boom')] * (limit - 1)))
    targets = _songs(limit - 1)

    foreground._dl_thread(targets, 'q', replace=False, resync=False,
                          background_mode=False)

    kinds = _kinds(_drain(foreground))
    assert 'error_streak' not in kinds
    assert 'finished' in kinds


def test_trips_at_exactly_the_limit_and_stops_advancing(foreground, monkeypatch):
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    # Three more songs and three more scripted errors than needed: if the
    # breaker were off by one in either direction, the song_start count below
    # catches it rather than the script running dry.
    monkeypatch.setattr(gui, 'run_song_with_backoff',
                        _script_runner([('error', 'boom')] * (limit + 3)))
    targets = _songs(limit + 3)

    foreground._dl_thread(targets, 'q', replace=False, resync=False,
                          background_mode=False)

    msgs = _drain(foreground)
    kinds = _kinds(msgs)
    assert 'error_streak' in kinds
    # It stopped digging: exactly `limit` songs were attempted, not limit + 3.
    assert kinds.count('song_start') == limit
    # The run ended at the trip, so the normal completion message never fires.
    assert 'finished' not in kinds


def test_foreground_trip_does_not_claim_a_rate_limit(foreground, monkeypatch):
    """'rate_limited' tells the user YouTube is throttling them. When the real
    cause is a broken cookie store or a dead downloader that is a lie, and it
    sends them off debugging the wrong thing."""
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    monkeypatch.setattr(gui, 'run_song_with_backoff',
                        _script_runner([('error', 'boom')] * limit))

    foreground._dl_thread(_songs(limit), 'q', replace=False, resync=False,
                          background_mode=False)

    msgs = _drain(foreground)
    assert 'rate_limited' not in _kinds(msgs)
    streak = [m for m in msgs if m[0] == 'error_streak']
    assert len(streak) == 1
    # The message carries the error that actually repeated, so the UI can name it.
    assert 'boom' in str(streak[0][-1])


def test_a_success_between_two_near_miss_runs_resets_the_count(foreground, monkeypatch):
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    script = ([('error', 'boom')] * (limit - 1) + ['ok']
              + [('error', 'boom')] * (limit - 1))
    monkeypatch.setattr(gui, 'run_song_with_backoff', _script_runner(script))

    foreground._dl_thread(_songs(2 * limit - 1), 'q', replace=False,
                          resync=False, background_mode=False)

    kinds = _kinds(_drain(foreground))
    assert 'error_streak' not in kinds
    assert 'finished' in kinds


def test_a_skipped_song_between_two_near_miss_runs_resets_the_count(foreground, monkeypatch):
    # A skip means the song already had its video -- the downloader demonstrably
    # reached a verdict, so it is as much a sign of health as a download.
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    script = ([('error', 'boom')] * (limit - 1) + ['skipped']
              + [('error', 'boom')] * (limit - 1))
    monkeypatch.setattr(gui, 'run_song_with_backoff', _script_runner(script))

    foreground._dl_thread(_songs(2 * limit - 1), 'q', replace=False,
                          resync=False, background_mode=False)

    kinds = _kinds(_drain(foreground))
    assert 'error_streak' not in kinds
    assert 'finished' in kinds
