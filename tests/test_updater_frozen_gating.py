# Confirms both self-update channels stay dormant from a source run: the app
# self-update channel is meant to stay off (SPEC.md), and the yt-dlp PyPI
# channel is a documented no-op from source regardless (both are gated on
# the same _frozen() check -- see tasks/plan.md finding 1).

import sys

import updater


def test_check_app_update_is_noop_when_not_frozen(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    called = []
    monkeypatch.setattr(updater, '_get_json', lambda *a, **k: called.append(1))

    result = updater.check_app_update('1.0.0')

    assert result is None
    assert called == []  # no network call attempted


def test_maybe_update_ytdlp_is_noop_when_not_frozen(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    called = []
    monkeypatch.setattr(updater, '_wheel_url_and_version', lambda: called.append(1))

    result = updater.maybe_update_ytdlp('2024.1.1')

    assert result is None
    assert called == []
