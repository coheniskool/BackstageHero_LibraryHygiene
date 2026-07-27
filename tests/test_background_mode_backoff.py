# next_resume_at() is the pure-function core of background mode's long
# backoff (SPEC-background-mode.md / tasks/plan-background-mode.md, Task 7).
# It is layered ABOVE the existing short per-song retry (BOT_BACKOFF_SECONDS/
# run_song_with_backoff) -- these tests only cover the new long-backoff math,
# never the short retry, which stays unmodified (see
# test_candidate_kind.py / test_process_resync.py for that regression
# coverage).

import VideoDownload as vd


def test_schedule_escalates_through_each_step():
    now = 1_000_000
    assert vd.next_resume_at(0, now) == now + 3600     # 1h
    assert vd.next_resume_at(1, now) == now + 14400    # 4h
    assert vd.next_resume_at(2, now) == now + 43200    # 12h
    assert vd.next_resume_at(3, now) == now + 86400    # 24h


def test_schedule_clamps_to_the_last_value_indefinitely():
    """throttle_count running past the schedule's length must never raise
    IndexError -- background mode retries forever, never gives up on its
    own."""
    now = 1_000_000
    assert vd.next_resume_at(10, now) == now + 86400
    assert vd.next_resume_at(1000, now) == now + 86400


def test_custom_schedule_override_is_used_instead_of_the_module_default():
    now = 1_000_000
    custom = [10, 20, 30]
    assert vd.next_resume_at(0, now, schedule=custom) == now + 10
    assert vd.next_resume_at(1, now, schedule=custom) == now + 20
    assert vd.next_resume_at(2, now, schedule=custom) == now + 30
    # clamps to the custom schedule's own last value, not the module default's
    assert vd.next_resume_at(5, now, schedule=custom) == now + 30


def test_now_boundary_has_no_off_by_one_error():
    """now=0 (or any exact boundary value) is handled the same as any other
    'now' -- no special-casing that could introduce an off-by-one."""
    assert vd.next_resume_at(0, 0) == 3600
    assert vd.next_resume_at(0, 0, schedule=[5]) == 5


def test_long_backoff_schedule_is_the_documented_1h_4h_12h_24h_shape():
    assert vd.LONG_BACKOFF_SECONDS == [3600, 14400, 43200, 86400]


def test_bot_backoff_seconds_and_run_song_with_backoff_are_unchanged():
    """Regression guard: Task 7 must not touch the existing short per-song
    retry at all."""
    assert vd.BOT_BACKOFF_SECONDS == [60, 180, 420]
    assert callable(vd.run_song_with_backoff)
