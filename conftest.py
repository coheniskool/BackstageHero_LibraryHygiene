import os
import shutil
import sys
import tempfile

# Point the whole test session at a throwaway data dir before anything can
# import a project module.
#
# VideoDownload._setup_logging() attaches a RotatingFileHandler to
# %LOCALAPPDATA%\BackstageHero\log.txt at *import* time, and nearly every module
# under test imports VideoDownload -- so without this, a test run appends its own
# fixture output ("boom", pytest tmp paths, deliberately broken-yt_dlp
# tracebacks) straight into the real user's diagnostics log. That log rotates at
# 512KB x 3 files and a single run writes enough to force a rotation, which
# permanently deletes the oldest file. Real run history is the evidence base this
# project actually diagnoses from -- SPEC-cookie-fallback-fix.md and
# SPEC-fail-fast-preconditions.md both reconstruct incidents out of log.txt and
# its rotated backups -- so letting the suite overwrite it is expensive.
#
# Redirecting LOCALAPPDATA rather than patching the log handler covers every
# updater.data_dir() consumer at once: settings.json, songs_path.txt,
# background_state.json, throttle_history.json and client_id are all read and
# written from there too, and tests have no business touching the real ones
# either. It also survives the subprocess round-trip in the enricher CLI tests,
# since os.environ is inherited. This generalises what individual tests were
# already doing by hand (see test_resolver_client._stub_client_id).
_DATA_DIR = tempfile.mkdtemp(prefix='backstagehero-tests-')
os.environ['LOCALAPPDATA'] = _DATA_DIR

# Repo root isn't on sys.path by default under pytest's "prepend" import mode
# (only the tests/ directory is, since it has no __init__.py above it). The
# modules under test (VideoDownload, resolver_client, updater, audiosync,
# library_common) live at the repo root, so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def pytest_sessionfinish(session, exitstatus):
    # Best effort: the rotating log handler still holds log.txt open, and
    # Windows won't unlink an open file, so leaving the directory behind is an
    # acceptable outcome -- it's under %TEMP% either way.
    import logging
    logging.shutdown()
    shutil.rmtree(_DATA_DIR, ignore_errors=True)
