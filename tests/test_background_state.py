# background_state.json persistence (Task 9 of SPEC-background-mode.md).
#
# Unlike settings.json (flat UI prefs, written once in a while, plain
# open().write()), this file is rewritten frequently over a run that can span
# days and survive app/machine restarts -- so the one thing worth a dedicated
# test beyond a basic round-trip is the atomic-write guarantee: a crash
# mid-write must never leave a half-written file behind for the next resume
# to trust.

import os

import pytest

ctk = pytest.importorskip('customtkinter')
import gui


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = str(tmp_path / 'background_state.json')
    monkeypatch.setattr(gui, '_BACKGROUND_STATE_FILE', path)
    return path


SAMPLE_STATE = {
    'phase': 'downloading',
    'resume_at': 1234567890.5,
    'throttle_count': 2,
    'songs_folder': 'C:/Songs',
    'quality': '1080p',
    'replace': False,
    'resync': True,
    'remaining_folders': ['C:/Songs/Artist - Song A', 'C:/Songs/Artist - Song B'],
    'tool_dry_run': {
        'migrate_review_folders': True,
        'fix_chart_names': True,
        'repair_videos': False,
        'find_static_art': True,
        'enrich_metadata': True,
        'find_duplicates': False,
    },
}


# --- round trip -------------------------------------------------------------

def test_save_then_load_round_trips_exactly(state_file):
    gui._save_background_state(SAMPLE_STATE)

    assert gui._load_background_state() == SAMPLE_STATE


def test_save_writes_via_atomic_replace(state_file):
    """The real file on disk should be the final content, not a stray .tmp
    left behind once a save succeeds."""
    gui._save_background_state(SAMPLE_STATE)

    assert os.path.exists(state_file)
    assert not os.path.exists(state_file + '.tmp')


# --- defensive loading --------------------------------------------------

def test_missing_file_loads_as_empty_dict(state_file):
    assert not os.path.exists(state_file)

    assert gui._load_background_state() == {}


def test_empty_file_loads_as_empty_dict_not_raise(state_file):
    with open(state_file, 'w', encoding='utf-8') as f:
        f.write('')

    assert gui._load_background_state() == {}


def test_corrupt_json_loads_as_empty_dict_not_raise(state_file):
    with open(state_file, 'wb') as f:
        f.write(b'\x00\x01not json at all{{{')

    assert gui._load_background_state() == {}


# --- atomic-write guarantee under a simulated crash -------------------------

def test_crash_mid_write_leaves_original_file_untouched(state_file, monkeypatch):
    """Simulates a crash between the temp-file write and os.replace() by
    making os.replace raise. The pre-existing valid state file must survive
    unmodified -- a resume must never trust a half-written file."""
    original = {'phase': 'downloading', 'resume_at': 111, 'throttle_count': 0,
                'songs_folder': 'C:/Songs', 'quality': '720p', 'replace': False,
                'resync': False, 'remaining_folders': [], 'tool_dry_run': {}}
    gui._save_background_state(original)
    assert gui._load_background_state() == original

    real_replace = os.replace

    def boom(src, dst):
        raise OSError('simulated crash mid-write')

    monkeypatch.setattr(gui.os, 'replace', boom)

    new_state = dict(original, phase='library_tools', throttle_count=5)
    gui._save_background_state(new_state)  # must not raise

    monkeypatch.setattr(gui.os, 'replace', real_replace)
    assert gui._load_background_state() == original, \
        'a failed atomic write corrupted or replaced the previous valid state'


def test_crash_mid_write_when_no_original_file_existed(state_file, monkeypatch):
    """Same simulated crash, but with no pre-existing file -- must not create
    a half-written one either."""
    assert not os.path.exists(state_file)
    monkeypatch.setattr(gui.os, 'replace', lambda src, dst: (_ for _ in ()).throw(
        OSError('simulated crash mid-write')))

    gui._save_background_state({'phase': 'downloading'})  # must not raise

    assert not os.path.exists(state_file)


# --- clearing -----------------------------------------------------------

def test_clear_removes_the_state_file(state_file):
    gui._save_background_state(SAMPLE_STATE)
    assert os.path.exists(state_file)

    gui._clear_background_state()

    assert not os.path.exists(state_file)
    assert gui._load_background_state() == {}


def test_clear_when_no_file_exists_is_a_silent_no_op(state_file):
    assert not os.path.exists(state_file)

    gui._clear_background_state()  # must not raise

    assert gui._load_background_state() == {}
