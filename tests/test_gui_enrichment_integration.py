# tests/test_gui_enrichment_integration.py
# Covers gui.App's "Enrich after scan" integration -- Task 3.1 of the
# library-enrichment plan. See tasks/plan-library-enrichment.md.
#
# Follows tests/test_offset_range_and_csv.py's established convention:
# object.__new__(gui.App) + only the attributes the method under test
# touches, never the real __init__ (which builds actual CTk widgets and
# starts update checks).

import threading

import pytest

ctk = pytest.importorskip('customtkinter')
import gui


class _FakeVar:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


def _app_with(songs_folder, enrich_on=True):
    app = object.__new__(gui.App)
    app._songs_folder = songs_folder
    app._enrich_var = _FakeVar(enrich_on)
    return app


def test_maybe_start_enrichment_does_nothing_when_checkbox_off(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(threading, 'Thread', lambda *a, **k: calls.append((a, k)) or object())
    app = _app_with(str(tmp_path), enrich_on=False)
    app._maybe_start_enrichment()
    assert calls == []


def test_maybe_start_enrichment_does_nothing_without_a_library_folder(monkeypatch):
    calls = []
    monkeypatch.setattr(threading, 'Thread', lambda *a, **k: calls.append((a, k)) or object())
    app = _app_with(None, enrich_on=True)
    app._maybe_start_enrichment()
    assert calls == []


def test_maybe_start_enrichment_spawns_a_background_thread_when_enabled(tmp_path, monkeypatch):
    started = []

    class _FakeThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(threading, 'Thread', _FakeThread)
    app = _app_with(str(tmp_path), enrich_on=True)
    app._maybe_start_enrichment()

    assert started == [app._run_enrichment]


def test_run_enrichment_calls_enrich_library_with_the_songs_folder(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(gui.library_enrichment, 'enrich_library',
                         lambda library_path: captured.setdefault('path', library_path))
    app = _app_with(str(tmp_path))
    app._run_enrichment()
    assert captured['path'] == str(tmp_path)


def test_run_enrichment_never_raises_on_failure(tmp_path, monkeypatch):
    """A background thread that raises crashes silently with no way for the
    user to see it -- _run_enrichment must catch and log, never propagate,
    matching _export_library_csv's own philosophy for optional-feature
    failures."""
    def boom(library_path):
        raise RuntimeError('disk full')
    monkeypatch.setattr(gui.library_enrichment, 'enrich_library', boom)
    app = _app_with(str(tmp_path))
    app._run_enrichment()  # must not raise


def test_run_enrichment_touches_no_tkinter_widget(tmp_path, monkeypatch):
    """Regression guard for the cross-thread widget-access hazard: this
    method runs off the main thread, so it must never call .configure()/
    .set() on any CTk widget directly -- only self._queue (unused here) is
    safe to touch from a background thread in this app's architecture."""
    monkeypatch.setattr(gui.library_enrichment, 'enrich_library', lambda library_path: None)
    app = _app_with(str(tmp_path))

    class _ExplodingWidget:
        def __getattr__(self, name):
            raise AssertionError(f'_run_enrichment touched a widget attribute: {name}')

    app._status_lbl = _ExplodingWidget()
    app._progress = _ExplodingWidget()
    app._run_enrichment()  # must not touch _status_lbl/_progress at all
