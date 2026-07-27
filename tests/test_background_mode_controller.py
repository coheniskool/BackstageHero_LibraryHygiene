# Background-mode controller (Task 11 of SPEC-background-mode.md).
#
# The controller wraps the existing _dl_thread download loop: on a YouTube
# throttle ('stop' from run_song_with_backoff) it backs off on the long
# escalating schedule and RETRIES THE SAME SONG instead of ending the run, and
# on true download-phase completion it hands off to a single Library Tools pass.
#
# Everything here drives _dl_thread directly on a bare App instance (the
# object.__new__(gui.App) pattern from test_offset_range_and_csv.py) with:
#   - run_song_with_backoff mocked to a scripted 'stop'/'ok'/'skipped' sequence,
#   - a fake stop-event whose wait() returns immediately (never a real sleep)
#     while recording the durations it was asked to wait,
#   - next_resume_at / get_active_schedule / record_throttle_episode /
#     _run_library_tool all stubbed so no real files or hours-long waits happen.
#
# The single most important thing these prove: the DEFAULT (non-background)
# path's 'stop' -> 'rate_limited' -> end-the-run behavior is byte-for-byte
# unchanged (see the two test_default_path_* regressions at the bottom).

import queue

import pytest

ctk = pytest.importorskip('customtkinter')
import gui
import VideoDownload as vd


# --- fakes ------------------------------------------------------------------

class _FakeSong:
    def __init__(self, folder, label):
        self.folder, self.label = folder, label
        self.res = '-'
        self.has_video = False


class _FakeStopEvent:
    """A stand-in for threading.Event that never actually blocks. wait()
    records the timeout it was asked for and returns False (timeout elapsed),
    unless this wait-call's index is in `cancel_on`, in which case it behaves
    like a Stop pressed mid-wait: sets itself and returns True."""

    def __init__(self, cancel_on=()):
        self.waits = []
        self._set = False
        self._cancel_on = set(cancel_on)

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def clear(self):
        self._set = False

    def wait(self, timeout=None):
        idx = len(self.waits)
        self.waits.append(timeout)
        if idx in self._cancel_on:
            self._set = True
            return True
        return False


def _bare_app():
    app = object.__new__(gui.App)
    app._queue = queue.Queue()
    app._sync_ready = False
    app._songs_folder = 'C:/Songs'
    return app


def _drain(app):
    msgs = []
    try:
        while True:
            msgs.append(app._queue.get_nowait())
    except queue.Empty:
        pass
    return msgs


def _kinds(msgs):
    return [m[0] for m in msgs]


@pytest.fixture
def controller(tmp_path, monkeypatch):
    """Wire a bare App plus stubbed background-mode collaborators. Returns a
    small namespace the tests read call-records off of."""
    # Real state file, in a temp dir, so save/load/clear round-trip for real
    # and tests can inspect what got persisted.
    monkeypatch.setattr(gui, '_BACKGROUND_STATE_FILE',
                        str(tmp_path / 'background_state.json'))

    # Deterministic schedule: next_resume_at returns now + SCHEDULE[step], so a
    # recorded wait duration is exactly SCHEDULE[step] regardless of wall clock.
    schedule = [3600, 14400, 43200, 86400]

    calls = {
        'song': [],            # scripted run_song_with_backoff results (popped)
        'next_resume_at': [],  # throttle_count args seen
        'episodes': [],        # record_throttle_episode args
        'tools': [],           # (key, dry_run) for each _run_library_tool
        'tool_fail': set(),    # keys _run_library_tool should raise on
    }

    def fake_run_song(folder, label, quality, sync_ready,
                      replace, resync, errored, stop_evt, events):
        return calls['song'].pop(0)

    def fake_next_resume_at(throttle_count, now, schedule=None):
        calls['next_resume_at'].append(throttle_count)
        step = min(throttle_count, len(SCHED) - 1)
        return now + SCHED[step]

    SCHED = schedule

    def fake_record_episode(started_at, resolved_at, escalation_steps_used,
                            schedule=None):
        calls['episodes'].append(
            (started_at, resolved_at, escalation_steps_used))

    def fake_run_library_tool(songs_folder, key, dry_run):
        calls['tools'].append((key, dry_run))
        if key in calls['tool_fail']:
            raise RuntimeError(f'boom {key}')
        return {}

    monkeypatch.setattr(gui, 'run_song_with_backoff', fake_run_song)
    monkeypatch.setattr(gui, 'next_resume_at', fake_next_resume_at)
    monkeypatch.setattr(gui, 'get_active_schedule', lambda: list(SCHED))
    monkeypatch.setattr(gui, 'record_throttle_episode', fake_record_episode)
    monkeypatch.setattr(gui, '_run_library_tool', fake_run_library_tool)
    monkeypatch.setattr(gui, '_format_tool_summary',
                        lambda key, counts, dry_run: 'summary')
    monkeypatch.setattr(gui, 'get_stored_resolution', lambda folder: None)

    app = _bare_app()

    class _NS:
        pass
    ns = _NS()
    ns.app = app
    ns.calls = calls
    ns.schedule = schedule
    return ns


# --- one throttle then success ---------------------------------------------

def test_one_throttle_then_success_records_the_episode_and_finishes(controller):
    app = controller.app
    app._stop_evt = _FakeStopEvent()
    controller.calls['song'] = ['stop', 'ok']
    targets = [_FakeSong('C:/Songs/A', 'Song A')]

    app._dl_thread(targets, 'q', replace=False, resync=False, background_mode=True)

    # The long-backoff wait actually happened for the throttle...
    assert 3600 in app._stop_evt.waits
    # ...backing off on step 0 of the schedule.
    assert controller.calls['next_resume_at'] == [0]
    # The episode resolved on the retry -> recorded exactly once, cleared on
    # escalation step 0 (throttle_count - 1).
    assert len(controller.calls['episodes']) == 1
    assert controller.calls['episodes'][0][2] == 0
    # The run did NOT end at the throttle: it went on to the Library Tools pass.
    kinds = _kinds(_drain(app))
    assert 'background_throttled' in kinds
    assert 'song_done' in kinds
    assert 'background_done' in kinds
    assert 'rate_limited' not in kinds


# --- escalation across consecutive throttles on the same song ---------------

def test_throttle_count_escalates_across_consecutive_throttles(controller):
    app = controller.app
    app._stop_evt = _FakeStopEvent()
    controller.calls['song'] = ['stop', 'stop', 'stop', 'ok']
    targets = [_FakeSong('C:/Songs/A', 'Song A')]

    app._dl_thread(targets, 'q', replace=False, resync=False, background_mode=True)

    # Each consecutive throttle escalates one step further down the schedule.
    assert controller.calls['next_resume_at'] == [0, 1, 2]
    long_waits = [w for w in app._stop_evt.waits if w in controller.schedule]
    assert long_waits == [3600, 14400, 43200]
    # One episode, resolved on escalation step 2 (the third, 12h, wait).
    assert len(controller.calls['episodes']) == 1
    assert controller.calls['episodes'][0][2] == 2


# --- a Stop during a long-backoff wait ends the run cleanly -----------------

def test_stop_during_backoff_wait_ends_run_and_leaves_state_resumable(controller):
    app = controller.app
    # Cancel the very first wait (the long backoff -- no inter-song pace wait
    # precedes the first song).
    app._stop_evt = _FakeStopEvent(cancel_on={0})
    controller.calls['song'] = ['stop']
    targets = [_FakeSong('C:/Songs/A', 'Song A')]

    app._dl_thread(targets, 'q', replace=False, resync=False, background_mode=True)

    kinds = _kinds(_drain(app))
    assert 'background_stopped' in kinds
    assert 'background_done' not in kinds
    # No episode recorded (the episode never resolved -- the wait was cancelled).
    assert controller.calls['episodes'] == []
    # Library Tools must NOT run when a throttle stopped the run.
    assert controller.calls['tools'] == []
    # State is left in place and still mid-download with a pending resume_at --
    # a deliberate Stop is intentionally indistinguishable from an interrupted
    # run, so a future resume-on-launch could pick it up.
    state = gui._load_background_state()
    assert state.get('phase') == 'downloading'
    assert state.get('resume_at') is not None


# --- true completion triggers exactly one Library Tools pass ----------------

def test_completion_runs_library_tools_once_in_order_all_dry_run(controller):
    app = controller.app
    app._stop_evt = _FakeStopEvent()
    controller.calls['song'] = ['ok', 'ok', 'ok']
    targets = [_FakeSong(f'C:/Songs/{n}', n) for n in ('A', 'B', 'C')]

    app._dl_thread(targets, 'q', replace=False, resync=False, background_mode=True)

    # Every tool ran exactly once, in _RUN_ALL_ORDER, each defaulting to dry-run.
    assert controller.calls['tools'] == [(k, True) for k in gui._RUN_ALL_ORDER]
    # No throttle occurred, so no episode was recorded.
    assert controller.calls['episodes'] == []
    kinds = _kinds(_drain(app))
    assert kinds.count('background_library_tools') == 1
    assert 'background_done' in kinds
    # Reaching 'done' clears the persisted state entirely.
    assert gui._load_background_state() == {}


def test_library_tools_reads_captured_dry_run_snapshot_not_forced_live(controller):
    app = controller.app
    app._stop_evt = _FakeStopEvent()
    controller.calls['song'] = ['ok']
    # Simulate a launch snapshot where one tool was captured as LIVE (False)
    # and the rest dry-run -- the controller must honor exactly that, never
    # silently force everything live nor everything dry.
    captured = {k: True for k in gui._RUN_ALL_ORDER}
    captured['fix_chart_names'] = False
    gui._save_background_state({
        'phase': 'downloading', 'resume_at': None, 'throttle_count': 0,
        'songs_folder': 'C:/Songs', 'quality': '720p', 'replace': False,
        'resync': False, 'remaining_folders': [], 'tool_dry_run': captured,
    })
    targets = [_FakeSong('C:/Songs/A', 'Song A')]

    app._dl_thread(targets, 'q', replace=False, resync=False, background_mode=True)

    got = dict(controller.calls['tools'])
    assert got['fix_chart_names'] is False
    assert all(got[k] is True for k in gui._RUN_ALL_ORDER if k != 'fix_chart_names')


def test_one_failing_library_tool_does_not_abort_the_rest(controller):
    app = controller.app
    app._stop_evt = _FakeStopEvent()
    controller.calls['song'] = ['ok']
    controller.calls['tool_fail'] = {'repair_videos'}
    targets = [_FakeSong('C:/Songs/A', 'Song A')]

    app._dl_thread(targets, 'q', replace=False, resync=False, background_mode=True)

    # All six were still attempted despite one raising.
    assert [k for k, _ in controller.calls['tools']] == list(gui._RUN_ALL_ORDER)
    done = [m for m in _drain(app) if m[0] == 'background_done']
    assert len(done) == 1
    # background_done payload: (_, done, skipped, errors, tools_ok)
    assert done[0][4] == len(gui._RUN_ALL_ORDER) - 1


# --- throttle episode resolution logging ----------------------------------------

def test_throttle_episode_resolved_logs_escalation_steps(controller, caplog):
    """When a throttle episode is resolved (song succeeds after one or more
    throttles), log.info is called with the escalation step count."""
    import logging
    app = controller.app
    app._stop_evt = _FakeStopEvent()
    # Sequence: throttle, retry, succeed
    controller.calls['song'] = ['stop', 'ok']
    targets = [_FakeSong('C:/Songs/A', 'Song A')]

    with caplog.at_level(logging.INFO, logger='backstagehero'):
        app._dl_thread(targets, 'q', replace=False, resync=False, background_mode=True)

    # Assert the resolution log was written with the escalation step count
    assert any('throttle episode resolved' in record.message and
               'escalation step(s)' in record.message
               for record in caplog.records), \
        f"Expected throttle resolution log in {[r.message for r in caplog.records]}"


# --- REGRESSIONS: the default (non-background) path must be unchanged --------

def test_default_path_stop_still_ends_run_with_rate_limited(controller):
    """The single most important constraint: with background_mode omitted, a
    'stop' still posts 'rate_limited' and ends the run -- no long backoff, no
    Library Tools, no episode recording."""
    app = controller.app
    app._stop_evt = _FakeStopEvent()
    controller.calls['song'] = ['stop']
    targets = [_FakeSong('C:/Songs/A', 'Song A')]

    app._dl_thread(targets, 'q', replace=False, resync=False)  # default False

    msgs = _drain(app)
    kinds = _kinds(msgs)
    assert ('rate_limited', targets[0], 0, 1) in msgs
    assert 'background_throttled' not in kinds
    assert 'background_done' not in kinds
    assert controller.calls['tools'] == []
    assert controller.calls['episodes'] == []
    # No long-backoff wait was ever attempted.
    assert app._stop_evt.waits == []


def test_default_path_normal_run_still_posts_finished(controller):
    app = controller.app
    app._stop_evt = _FakeStopEvent()
    controller.calls['song'] = ['ok', 'skipped']
    targets = [_FakeSong('C:/Songs/A', 'A'), _FakeSong('C:/Songs/B', 'B')]

    app._dl_thread(targets, 'q', replace=False, resync=False)

    kinds = _kinds(_drain(app))
    assert 'finished' in kinds
    assert 'background_done' not in kinds
    assert 'background_library_tools' not in kinds
    assert controller.calls['tools'] == []


# --- end-to-end regression: real VideoDownload functions, not mocks ---------
#
# The `controller` fixture above mocks next_resume_at/get_active_schedule/
# record_throttle_episode entirely, so nothing above can catch a regression
# reintroduced at gui.py's actual call site. This test drives _dl_thread
# against the REAL VideoDownload.record_throttle_episode/get_active_schedule/
# next_resume_at (only the throttle_history.json path is redirected to a temp
# file) -- it is the one test in this suite that would fail if gui.py's
# record_throttle_episode(...) call ever again passed schedule=get_active_
# schedule() (the /review-found Critical bug: feeding the previously-
# recomputed schedule back in as the recompute's own baseline compounds every
# cycle instead of independently re-deriving from the fixed default, and
# collapses to the crash-prevention floor within a handful of cycles even
# under a perfectly stable signal).

def test_repeated_real_throttle_episodes_do_not_compound_the_schedule(
        tmp_path, monkeypatch):
    monkeypatch.setattr(gui, '_BACKGROUND_STATE_FILE',
                        str(tmp_path / 'background_state.json'))
    monkeypatch.setattr(vd, '_THROTTLE_HISTORY_FILE',
                        str(tmp_path / 'throttle_history.json'))
    monkeypatch.setattr(gui, '_run_library_tool', lambda *a, **k: {})
    monkeypatch.setattr(gui, '_format_tool_summary', lambda *a, **k: 'summary')
    monkeypatch.setattr(gui, 'get_stored_resolution', lambda folder: None)

    app = _bare_app()
    app._stop_evt = _FakeStopEvent()

    # One song per episode: throttles exactly once (escalation depth 0) then
    # succeeds -- a stable "clears immediately" signal, repeated across enough
    # songs to drive several real recompute cycles past _RECOMPUTE_THRESHOLD.
    n_songs = vd._RECOMPUTE_THRESHOLD + 6
    script = []
    for _ in range(n_songs):
        script += ['stop', 'ok']

    def fake_run_song(folder, label, quality, sync_ready, replace, resync,
                      errored, stop_evt, events):
        return script.pop(0)
    monkeypatch.setattr(gui, 'run_song_with_backoff', fake_run_song)

    targets = [_FakeSong(f'C:/Songs/{i}', f'Song {i}') for i in range(n_songs)]
    app._dl_thread(targets, 'q', replace=False, resync=False,
                   background_mode=True)

    schedule_after = vd.get_active_schedule()
    assert schedule_after != vd.LONG_BACKOFF_SECONDS, (
        'expected a real recompute to have happened by now')

    # The correctness property: recomputing ONCE from the complete, final
    # episode history against the fixed default must equal what the real run
    # (which recomputed once per episode) actually converged to. If gui.py's
    # call site were compounding (feeding get_active_schedule() back in as
    # the recompute's baseline instead of the fixed default), the real run's
    # repeated-recompute result would diverge from this single-shot
    # recompute over the same history -- this assertion is exactly the
    # idempotency property maybe_recompute_schedule's docstring promises and
    # gui.py's old call site violated.
    data = vd._load_throttle_data()
    assert len(data['episodes']) == n_songs
    single_shot = vd.maybe_recompute_schedule(
        data['episodes'], schedule=vd.LONG_BACKOFF_SECONDS)
    assert schedule_after == single_shot, (
        f'schedule diverged from a single correct recompute off the fixed '
        f'default ({single_shot}) -- got {schedule_after} instead, which '
        'indicates compounding drift regressed at the record_throttle_episode '
        'call site in gui.py')
