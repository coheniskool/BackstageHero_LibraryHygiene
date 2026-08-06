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
    exactly why the caller could not tell a dud song from a broken run.

    An exhausted script keeps returning 'ok' rather than raising, so a test
    only has to script the part it cares about. Tests that depend on how many
    songs were attempted assert on the song_start count instead."""
    def fake(folder, label, quality, sync_ready, replace, resync,
             errored, stop_evt, events):
        entry = script.pop(0) if script else 'ok'
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


# --- Part B: the same breaker in background mode ----------------------------
#
# Background mode never gives up on its own, so a trip here backs off on the
# long escalating schedule and retries rather than ending the run. The two
# things that must NOT happen are the subject of their own tests below: the
# streak must not consume throttle escalation steps, and it must not be
# recorded as a throttle episode. A cookie failure is not evidence about how
# long YouTube throttles for, and feeding it into that dataset would corrupt
# the adaptive schedule SPEC-background-mode.md built.

SCHED = [3600, 14400, 43200, 86400]


@pytest.fixture
def background(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, '_BACKGROUND_STATE_FILE',
                        str(tmp_path / 'background_state.json'))
    calls = {'next_resume_at': [], 'episodes': []}

    def fake_next_resume_at(count, now, schedule=None):
        calls['next_resume_at'].append(count)
        return now + SCHED[min(count, len(SCHED) - 1)]

    def fake_record_episode(started_at, resolved_at, steps, schedule=None):
        calls['episodes'].append((started_at, resolved_at, steps))

    monkeypatch.setattr(gui, 'next_resume_at', fake_next_resume_at)
    monkeypatch.setattr(gui, 'get_active_schedule', lambda: list(SCHED))
    monkeypatch.setattr(gui, 'record_throttle_episode', fake_record_episode)
    monkeypatch.setattr(gui, '_run_library_tool', lambda folder, key, dry: {})
    monkeypatch.setattr(gui, '_format_tool_summary',
                        lambda key, counts, dry: 'summary')
    monkeypatch.setattr(gui, 'get_stored_resolution', lambda folder: None)

    app = _bare_app()
    app._stop_evt = _FakeStopEvent()

    class _NS:
        pass
    ns = _NS()
    ns.app, ns.calls, ns.state_file = app, calls, tmp_path / 'background_state.json'
    return ns


def test_background_trip_backs_off_instead_of_ending_the_run(background, monkeypatch):
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    monkeypatch.setattr(gui, 'run_song_with_backoff',
                        _script_runner([('error', 'boom')] * limit))

    background.app._dl_thread(_songs(limit), 'q', replace=False, resync=False,
                              background_mode=True)

    kinds = _kinds(_drain(background.app))
    assert 'background_error_streak' in kinds
    # Backed off on step 0 of the schedule, and actually waited.
    assert 3600 in background.app._stop_evt.waits
    # The run did not end at the trip -- background mode retries indefinitely.
    assert 'background_done' in kinds


def test_background_trip_persists_state_before_waiting(background, monkeypatch):
    """A crash during an hours-long wait must not lose where the run got to, so
    the state has to be on disk *before* the wait starts -- not merely by the
    end of the run. Snapshotting inside wait() is the only way to tell the
    difference; checking after _dl_thread returns would pass even if the save
    happened last, and in fact finds nothing at all, since a completed run
    clears the state file on its way out."""
    import json
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    seen = []

    class _SnapshotStopEvent(_FakeStopEvent):
        def wait(self, timeout=None):
            if background.state_file.exists():
                seen.append(json.loads(
                    background.state_file.read_text(encoding='utf-8')))
            return super().wait(timeout)

    background.app._stop_evt = _SnapshotStopEvent()
    monkeypatch.setattr(gui, 'run_song_with_backoff',
                        _script_runner([('error', 'boom')] * limit))

    background.app._dl_thread(_songs(limit), 'q', replace=False, resync=False,
                              background_mode=True)

    persisted = [st for st in seen if st.get('resume_at')]
    assert persisted, 'nothing was on disk when the backoff wait began'
    assert persisted[-1]['phase'] == 'downloading'
    # And it recorded what is still to do, not just when to wake up.
    assert persisted[-1]['remaining_folders']


def test_background_trip_does_not_record_a_throttle_episode(background, monkeypatch):
    """A run that trips the breaker and then hits one real throttle must record
    exactly one episode -- the throttle's. If the streak recorded one too, the
    adaptive schedule would be learning from a cookie failure."""
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    script = [('error', 'boom')] * limit + ['stop', 'ok']
    monkeypatch.setattr(gui, 'run_song_with_backoff', _script_runner(script))

    background.app._dl_thread(_songs(limit), 'q', replace=False, resync=False,
                              background_mode=True)

    assert len(background.calls['episodes']) == 1


def test_background_trip_leaves_throttle_count_alone(background, monkeypatch):
    """The streak has its own escalation counter. A throttle that happens after
    a streak backoff must still start at step 0, not inherit the streak's."""
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    script = [('error', 'boom')] * limit + ['stop', 'ok']
    monkeypatch.setattr(gui, 'run_song_with_backoff', _script_runner(script))

    background.app._dl_thread(_songs(limit), 'q', replace=False, resync=False,
                              background_mode=True)

    # First call is the streak's own escalation, second is the throttle's --
    # both at step 0 because they count independently.
    assert background.calls['next_resume_at'] == [0, 0]


def test_background_trip_wait_is_cancellable_by_stop(background, monkeypatch):
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    monkeypatch.setattr(gui, 'run_song_with_backoff',
                        _script_runner([('error', 'boom')] * limit))
    # Cancel on the wait that follows the per-song delays: a Stop pressed during
    # an hours-long backoff must not sit there until it elapses.
    background.app._stop_evt = _FakeStopEvent(cancel_on=range(limit - 1, limit + 2))

    background.app._dl_thread(_songs(limit), 'q', replace=False, resync=False,
                              background_mode=True)

    kinds = _kinds(_drain(background.app))
    assert 'background_stopped' in kinds
    assert 'background_done' not in kinds


def test_counter_resets_after_a_background_backoff(background, monkeypatch):
    """One trip, then a near-miss run of failures, must not trip again --
    otherwise every song after the first trip costs another long backoff."""
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    script = [('error', 'boom')] * limit + [('error', 'boom')] * (limit - 1)
    monkeypatch.setattr(gui, 'run_song_with_backoff', _script_runner(script))

    background.app._dl_thread(_songs(3 * limit), 'q', replace=False,
                              resync=False, background_mode=True)

    kinds = _kinds(_drain(background.app))
    assert kinds.count('background_error_streak') == 1


# --- Part B: the same breaker in the source-run CLI loop --------------------
#
# main() is the `python VideoDownload.py` path (the frozen build launches the
# GUI instead). No long backoff here: the escalating wait is background-mode-
# only by design, and an interactive run should not silently sit for an hour.

def _cli_library(tmp_path, count):
    songs = tmp_path / 'songs'
    for n in range(count):
        folder = songs / ('S%d' % n)
        folder.mkdir(parents=True)
        (folder / 'song.ini').write_text('[song]\nname = S%d\n' % n,
                                         encoding='utf-8')
    return songs


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Drive main() far enough to reach its download loop: real song.ini files
    on disk, scripted answers to its two input() prompts, no real sleeping."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(vd.updater, 'run_startup_updates', lambda *a, **k: None)
    monkeypatch.setattr('time.sleep', lambda seconds: None)
    answers = iter(['1'])          # '1' = default 720p; then Enter-to-exit
    monkeypatch.setattr('builtins.input', lambda *a, **k: next(answers, ''))

    attempted = []

    def make_runner(script):
        def fake(folder, song_name, quality, sync_ready, replace, resync,
                 errored):
            attempted.append(song_name)
            entry = script.pop(0) if script else 'ok'
            if entry == 'error':
                errored.append('boom')
                return 'ok'
            return entry
        return fake

    class _NS:
        pass
    ns = _NS()
    ns.tmp_path, ns.attempted, ns.make_runner = tmp_path, attempted, make_runner
    return ns


def test_cli_loop_trips_after_limit_consecutive_errors(cli, monkeypatch):
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    _cli_library(cli.tmp_path, limit + 3)
    monkeypatch.setattr(vd, 'run_song_with_backoff',
                        cli.make_runner(['error'] * (limit + 3)))

    vd.main()

    # Stopped at the limit instead of working through the remaining three.
    assert len(cli.attempted) == limit


def test_cli_loop_success_resets_the_count(cli, monkeypatch):
    limit = vd.CONSECUTIVE_ERROR_LIMIT
    total = 2 * limit - 1
    _cli_library(cli.tmp_path, total)
    script = ['error'] * (limit - 1) + ['ok'] + ['error'] * (limit - 1)
    monkeypatch.setattr(vd, 'run_song_with_backoff', cli.make_runner(script))

    vd.main()

    # Never limit-in-a-row, so every song got its turn.
    assert len(cli.attempted) == total
