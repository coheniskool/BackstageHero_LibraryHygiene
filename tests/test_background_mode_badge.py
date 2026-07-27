# Footer background-run badge (Task 5 of SPEC-background-mode-fixes.md).
#
# /review flagged self._background_mode as dead write-only state: set at 6
# places in App but never read anywhere. The fix wires it to a small visible
# status badge (App._background_badge_lbl) through one choke point,
# App._set_background_mode, so the badge can never drift out of sync with
# the flag. This module covers only that choke point.
#
# Follows tests/test_background_mode_gui_wiring.py's bare-instance
# convention: object.__new__(gui.App) with just the attributes the method
# under test touches, never a real ctk.CTk() window.

import pytest

ctk = pytest.importorskip('customtkinter')
import gui


class _FakeLabel:
    """Stand-in for a ctk.CTkLabel -- just records the last .configure(text=...) call."""

    def __init__(self):
        self.text = None

    def configure(self, **kwargs):
        if 'text' in kwargs:
            self.text = kwargs['text']


def _bare_app():
    app = object.__new__(gui.App)
    app._background_badge_lbl = _FakeLabel()
    return app


def test_set_background_mode_true_sets_flag_and_badge_text():
    app = _bare_app()

    app._set_background_mode(True)

    assert app._background_mode is True
    assert app._background_badge_lbl.text == '● Background'


def test_set_background_mode_false_clears_flag_and_badge_text():
    app = _bare_app()

    app._set_background_mode(False)

    assert app._background_mode is False
    assert app._background_badge_lbl.text == ''


def test_toggle_true_then_false_leaves_badge_empty_not_stale():
    app = _bare_app()

    app._set_background_mode(True)
    assert app._background_badge_lbl.text == '● Background'

    app._set_background_mode(False)

    assert app._background_mode is False
    assert app._background_badge_lbl.text == ''
