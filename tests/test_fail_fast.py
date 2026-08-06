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
