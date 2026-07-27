# Gap logging + adaptive backoff recompute (Task 8 of SPEC-background-mode.md).
#
# Every test here runs on FABRICATED throttle_history.json-shaped data -- never
# real logged episodes, which don't exist until the user runs background mode
# against a real library for days. Per the spec's testing strategy we assert
# the recompute *direction* (grew / shrank / no-op) and that the
# crash-prevention clamp holds, NOT exact output numbers: the formula itself is
# expected to be tuned once real, right-censored data comes in.

import os

import VideoDownload as vd


# --- helpers ----------------------------------------------------------------

def _episodes(count, escalation_steps_used, gap=1800):
    """`count` fabricated episodes that each cleared at the given escalation
    depth. gap (resolved_at - started_at) is incidental -- the recompute keys
    off escalation depth, not the gap (see maybe_recompute_schedule's docstring
    on right-censoring), so we only vary it in the degenerate-input test."""
    return [
        {'started_at': 1000.0, 'resolved_at': 1000.0 + gap,
         'escalation_steps_used': escalation_steps_used}
        for _ in range(count)
    ]


DEFAULT = vd.LONG_BACKOFF_SECONDS


# --- below-threshold no-op --------------------------------------------------

def test_fewer_than_five_records_is_a_noop():
    for n in range(0, 5):
        assert vd.maybe_recompute_schedule(_episodes(n, 0)) is None, \
            f'{n} records should not trigger a recompute'


def test_exactly_five_records_triggers_a_recompute():
    result = vd.maybe_recompute_schedule(_episodes(5, 0))
    assert result is not None
    assert len(result) == len(DEFAULT)


# --- shrink direction -------------------------------------------------------

def test_all_first_step_successes_shrink_the_schedule():
    """Every episode cleared on step 0 -> we're consistently over-waiting ->
    every step should come out shorter than the default."""
    result = vd.maybe_recompute_schedule(_episodes(6, 0))
    assert all(new < old for new, old in zip(result, DEFAULT)), \
        f'expected shrink below {DEFAULT}, got {result}'


def test_shrink_is_allowed_below_the_declined_one_hour_floor():
    """No policy floor: if the data says clear-on-step-0, the first step is
    allowed below the 1h (3600s) the user explicitly declined. Only the
    crash-prevention clamp applies."""
    result = vd.maybe_recompute_schedule(_episodes(6, 0))
    assert result[0] < 3600
    assert result[0] >= vd._MIN_BACKOFF_SECONDS


def test_shrink_never_dips_below_the_crash_prevention_clamp():
    result = vd.maybe_recompute_schedule(_episodes(8, 0))
    assert all(step >= vd._MIN_BACKOFF_SECONDS for step in result)


# --- grow direction ---------------------------------------------------------

def test_all_full_escalation_grows_or_caps_the_schedule():
    """Every episode needed the last step -> real block is longer than our top
    -> schedule grows (never shrinks)."""
    max_index = len(DEFAULT) - 1
    result = vd.maybe_recompute_schedule(_episodes(6, max_index))
    assert all(new >= old for new, old in zip(result, DEFAULT)), \
        f'expected grow/hold vs {DEFAULT}, got {result}'
    assert any(new > old for new, old in zip(result, DEFAULT))


def test_growth_is_bounded_not_unbounded():
    """Episodes that repeated at the top thousands of times must not explode
    the schedule -- the depth clamp caps a single recompute's growth."""
    huge = vd.maybe_recompute_schedule(_episodes(6, 100000))
    at_top = vd.maybe_recompute_schedule(_episodes(6, len(DEFAULT) - 1))
    # A runaway repeat count reads as one-step-past-the-end, so its growth is
    # bounded and only modestly beyond "cleared at the top step".
    assert huge is not None
    for step in huge:
        assert step <= 3 * max(DEFAULT)  # nowhere near unbounded
    # and it's still at least as long as the plain top-step result
    assert all(h >= t for h, t in zip(huge, at_top))


# --- degenerate / corrupt input ---------------------------------------------

def test_negative_gap_from_clock_skew_never_breaks_or_busy_loops():
    """resolved_at before started_at (clock skew). The recompute keys off
    escalation depth not the gap, but it must still never raise and never
    produce a sub-clamp step."""
    skewed = _episodes(6, 0, gap=-99999)
    result = vd.maybe_recompute_schedule(skewed)
    assert result is not None
    assert all(step >= vd._MIN_BACKOFF_SECONDS for step in result)


def test_escalation_larger_than_schedule_length_is_a_grow_not_a_crash():
    result = vd.maybe_recompute_schedule(_episodes(6, len(DEFAULT) + 50))
    assert result is not None
    assert all(step >= vd._MIN_BACKOFF_SECONDS for step in result)


def test_garbage_escalation_values_never_raise_or_dip_below_clamp():
    junk = [
        {'started_at': 0, 'resolved_at': 0, 'escalation_steps_used': None},
        {'started_at': 0, 'resolved_at': 0, 'escalation_steps_used': 'oops'},
        {'started_at': 0, 'resolved_at': 0, 'escalation_steps_used': -5},
        {},  # missing key entirely
        {'escalation_steps_used': 2},
    ]
    result = vd.maybe_recompute_schedule(junk)
    assert result is not None
    assert all(step >= vd._MIN_BACKOFF_SECONDS for step in result)


def test_clamp_binds_on_a_tiny_custom_schedule():
    """Force the crash-prevention clamp to be the binding constraint: a tiny
    custom schedule shrunk by all-step-0 history would go near-zero without the
    clamp."""
    result = vd.maybe_recompute_schedule(_episodes(6, 0), schedule=[10, 20])
    assert result == [vd._MIN_BACKOFF_SECONDS, vd._MIN_BACKOFF_SECONDS]


def test_empty_schedule_is_a_noop_not_a_crash():
    assert vd.maybe_recompute_schedule(_episodes(6, 0), schedule=[]) is None


# --- record_throttle_episode + file round-trip ------------------------------

def _point_history_file(tmp_path, monkeypatch):
    path = str(tmp_path / 'throttle_history.json')
    monkeypatch.setattr(vd, '_THROTTLE_HISTORY_FILE', path)
    return path


def test_record_episode_appends_and_reloads(tmp_path, monkeypatch):
    path = _point_history_file(tmp_path, monkeypatch)

    vd.record_throttle_episode(100.0, 3700.0, 0)
    vd.record_throttle_episode(200.0, 4000.0, 2)

    data = vd._load_throttle_data()
    assert len(data['episodes']) == 2
    assert data['episodes'][0] == {
        'started_at': 100.0, 'resolved_at': 3700.0, 'escalation_steps_used': 0}
    assert data['episodes'][1]['escalation_steps_used'] == 2
    assert os.path.exists(path)
    assert not os.path.exists(path + '.tmp')


def test_recording_five_episodes_persists_a_recomputed_schedule(tmp_path, monkeypatch):
    _point_history_file(tmp_path, monkeypatch)

    for _ in range(5):
        vd.record_throttle_episode(0.0, 60.0, 0)  # all first-step successes

    # The earned schedule survives a "restart": get_active_schedule reads it
    # back from disk and it's the shrunk one, not the default guess.
    active = vd.get_active_schedule()
    assert active != vd.LONG_BACKOFF_SECONDS
    assert all(new < old for new, old in zip(active, vd.LONG_BACKOFF_SECONDS))


def test_repeated_recompute_with_stable_signal_converges_not_compounds(tmp_path, monkeypatch):
    """Regression for the compounding-drift bug: gui.py's _dl_thread used to
    call record_throttle_episode(..., schedule=get_active_schedule()), which
    feeds each recompute the PREVIOUS recompute's output instead of the fixed
    default (LONG_BACKOFF_SECONDS). Under a perfectly stable signal (escalation
    depth never changes) that made the schedule shrink further on every single
    call, collapsing to the crash-prevention floor within a handful of cycles
    even though nothing about real-world throttling behavior had changed.
    Calling record_throttle_episode with no schedule= kwarg (its default,
    LONG_BACKOFF_SECONDS) is the fix: recompute is idempotent for a stable
    history, so once the schedule converges it must stay put."""
    _point_history_file(tmp_path, monkeypatch)

    seen_schedules = []
    for i in range(15):
        # No schedule= kwarg -- exercises the fixed-default path.
        vd.record_throttle_episode(float(i), float(i) + 10, 0)
        seen_schedules.append(vd.get_active_schedule())

    # The window only holds the most recent _THROTTLE_HISTORY_MAX episodes,
    # but that's well above 15, so every call here sees a growing, always-
    # step-0 history -- a stable signal by construction.
    first_recompute_index = vd._RECOMPUTE_THRESHOLD - 1  # first non-default value
    first_recompute = seen_schedules[first_recompute_index]
    assert first_recompute != vd.LONG_BACKOFF_SECONDS

    # After the first recompute, the schedule must not keep changing on every
    # subsequent call under a stable signal -- it should stabilize.
    for later in seen_schedules[first_recompute_index:]:
        assert later == first_recompute, (
            f"schedule kept changing under a stable signal: "
            f"{first_recompute} -> {later} (compounding drift regression)")


def test_get_active_schedule_falls_back_to_default_when_no_history(tmp_path, monkeypatch):
    _point_history_file(tmp_path, monkeypatch)
    assert vd.get_active_schedule() == vd.LONG_BACKOFF_SECONDS


def test_history_is_capped_to_the_most_recent_window(tmp_path, monkeypatch):
    _point_history_file(tmp_path, monkeypatch)

    for i in range(vd._THROTTLE_HISTORY_MAX + 20):
        vd.record_throttle_episode(float(i), float(i) + 10, 0)

    data = vd._load_throttle_data()
    assert len(data['episodes']) == vd._THROTTLE_HISTORY_MAX
    # the oldest were dropped; the newest survives
    assert data['episodes'][-1]['started_at'] == float(vd._THROTTLE_HISTORY_MAX + 19)


# --- defensive loading ------------------------------------------------------

def test_missing_file_loads_as_empty_shape(tmp_path, monkeypatch):
    _point_history_file(tmp_path, monkeypatch)
    assert vd._load_throttle_data() == {'episodes': [], 'schedule': None}


def test_corrupt_json_loads_as_empty_shape_not_raise(tmp_path, monkeypatch):
    path = _point_history_file(tmp_path, monkeypatch)
    with open(path, 'wb') as f:
        f.write(b'\x00not json{{{')
    assert vd._load_throttle_data() == {'episodes': [], 'schedule': None}


def test_bare_list_legacy_format_is_tolerated(tmp_path, monkeypatch):
    path = _point_history_file(tmp_path, monkeypatch)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('[{"started_at": 1, "resolved_at": 2, "escalation_steps_used": 0}]')
    data = vd._load_throttle_data()
    assert data['schedule'] is None
    assert len(data['episodes']) == 1


# --- atomic-write guarantee under a simulated crash -------------------------

def test_crash_mid_write_leaves_original_file_untouched(tmp_path, monkeypatch):
    """Mirrors test_background_state.py's atomic-write test: making os.replace
    raise simulates a crash between the temp write and the rename. The
    pre-existing valid history must survive unmodified."""
    _point_history_file(tmp_path, monkeypatch)

    vd.record_throttle_episode(1.0, 2.0, 0)
    before = vd._load_throttle_data()

    real_replace = os.replace

    def boom(src, dst):
        raise OSError('simulated crash mid-write')

    monkeypatch.setattr(vd.os, 'replace', boom)
    vd.record_throttle_episode(3.0, 4.0, 1)  # must not raise

    monkeypatch.setattr(vd.os, 'replace', real_replace)
    after = vd._load_throttle_data()
    assert after == before, 'a failed atomic write corrupted the previous history'


def test_crash_mid_write_with_no_original_file_leaves_nothing(tmp_path, monkeypatch):
    path = _point_history_file(tmp_path, monkeypatch)
    monkeypatch.setattr(vd.os, 'replace', lambda src, dst: (_ for _ in ()).throw(
        OSError('simulated crash mid-write')))

    vd.record_throttle_episode(1.0, 2.0, 0)  # must not raise

    assert not os.path.exists(path)


# --- schedule adjustment logging -----------------------------------------------

def test_schedule_adjustment_logs_old_new_and_direction(tmp_path, monkeypatch, caplog):
    """When enough episodes accumulate to trigger a recompute that produces a
    new schedule (not None), log.info is called with the old schedule, new
    schedule, and the direction (grew/shrank/unchanged)."""
    _point_history_file(tmp_path, monkeypatch)

    # Record 6 episodes that all clear on step 0 -> should shrink the schedule
    for i in range(6):
        vd.record_throttle_episode(1000.0 + i, 1010.0 + i, 0)

    with caplog.at_level('INFO', logger='backstagehero'):
        # The 6th record triggers the recompute; we need to add one more to
        # see the log line (since record_throttle_episode logs when new_schedule
        # is not None, which happens on the 7th record when len(episodes) >= 5)
        vd.record_throttle_episode(1100.0, 1110.0, 0)

    # Assert the log contains the schedule adjustment message
    assert any('adaptive backoff schedule' in record.message and
               'old=' in record.message and 'new=' in record.message
               for record in caplog.records), \
        f"Expected schedule adjustment log message in {[r.message for r in caplog.records]}"


def test_no_op_recompute_does_not_log(tmp_path, monkeypatch, caplog):
    """When fewer than 5 episodes exist, maybe_recompute_schedule returns None
    (no-op), and nothing should be logged."""
    _point_history_file(tmp_path, monkeypatch)

    with caplog.at_level('INFO', logger='backstagehero'):
        # Record only 4 episodes - not enough to trigger a recompute
        for i in range(4):
            vd.record_throttle_episode(1000.0 + i, 1010.0 + i, 0)

    # Assert no schedule adjustment log was written
    assert not any('adaptive backoff schedule' in record.message
                   for record in caplog.records), \
        f"Expected no schedule adjustment log, but got {[r.message for r in caplog.records]}"
