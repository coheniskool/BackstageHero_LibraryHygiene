# Optional, opt-in browser-cookie support for yt-dlp (SPEC-background-mode.md,
# Task 6). Off by default -- the central regression this file exists to prove
# is that _base_opts()'s output is byte-identical to before this feature
# existed whenever the setting is off, including when configure_cookies() is
# never called at all (a fresh install's module-level defaults).

import pytest

import VideoDownload as vd


def setup_function(_func):
    # Every test starts from the untouched default, regardless of what an
    # earlier test in this (or another) module left behind. _COOKIES_BROKEN
    # (SPEC-cookie-fallback-fix.md) is deliberately never reset by
    # configure_cookies() itself -- it must be reset here instead, or the
    # first fallback test to run leaks True into every later test in this
    # file.
    vd.configure_cookies(False, None)
    vd._COOKIES_BROKEN = False


def teardown_function(_func):
    vd.configure_cookies(False, None)
    vd._COOKIES_BROKEN = False


def _make_fake_ydl_class(behaviors):
    """Stand-in for yt_dlp.YoutubeDL. `behaviors` is a list, one entry
    consumed per construction (mirrors _run_ytdlp_with_cookie_fallback's
    at-most-two `with yt_dlp.YoutubeDL(...)` constructions per call): each
    entry is either an Exception instance (raised when the fake's
    extract_info/download/process_ie_result is called) or a value/callable
    those methods should return."""
    calls = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            self._behavior = behaviors[len(calls)]
            calls.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def _resolve(self):
            if isinstance(self._behavior, Exception):
                raise self._behavior
            return self._behavior() if callable(self._behavior) else self._behavior

        def extract_info(self, *a, **kw):
            return self._resolve()

        def download(self, *a, **kw):
            return self._resolve()

        def process_ie_result(self, *a, **kw):
            return self._resolve()

    FakeYDL.calls = calls
    return FakeYDL


_DPAPI_ERROR_TEXT = (
    'ERROR: Failed to decrypt with DPAPI. See  '
    'https://github.com/yt-dlp/yt-dlp/issues/10927  for more info')


def test_base_opts_has_no_cookie_key_by_default():
    opts = vd._base_opts()
    assert 'cookiesfrombrowser' not in opts


def test_base_opts_omits_cookie_key_without_configure_cookies_ever_called():
    # Simulates a fresh install where gui.py's startup call never happened --
    # the module-level defaults alone must reproduce today's behavior.
    vd.USE_BROWSER_COOKIES = False
    vd.COOKIE_BROWSER = None
    opts = vd._base_opts()
    assert 'cookiesfrombrowser' not in opts


def test_configure_cookies_on_adds_cookiesfrombrowser():
    vd.configure_cookies(True, 'chrome')
    opts = vd._base_opts()
    assert opts['cookiesfrombrowser'] == ('chrome',)


def test_configure_cookies_off_removes_cookiesfrombrowser_again():
    vd.configure_cookies(True, 'firefox')
    assert vd._base_opts()['cookiesfrombrowser'] == ('firefox',)

    vd.configure_cookies(False, None)
    assert 'cookiesfrombrowser' not in vd._base_opts()


def test_configure_cookies_true_but_no_browser_omits_key():
    # Belt-and-suspenders: the toggle alone, with no browser picked, must
    # never send an incomplete/garbage cookiesfrombrowser value to yt-dlp.
    vd.configure_cookies(True, None)
    assert 'cookiesfrombrowser' not in vd._base_opts()


def test_other_base_opts_keys_unchanged_by_cookie_setting():
    vd.configure_cookies(False, None)
    off = vd._base_opts()

    vd.configure_cookies(True, 'edge')
    on = vd._base_opts()

    on_minus_cookie_key = {k: v for k, v in on.items() if k != 'cookiesfrombrowser'}
    assert off == on_minus_cookie_key


def test_configure_cookies_accepts_supported_browser():
    # A known-good browser name is accepted and reaches _base_opts() exactly
    # as before this validation was added.
    vd.configure_cookies(True, 'chrome')
    assert vd.USE_BROWSER_COOKIES is True
    assert vd.COOKIE_BROWSER == 'chrome'
    assert vd._base_opts()['cookiesfrombrowser'] == ('chrome',)


def test_configure_cookies_rejects_unsupported_browser(caplog):
    # An unsupported browser name must never reach yt-dlp: cookie support is
    # left disabled, no exception escapes, and a warning is logged so the
    # misconfiguration is diagnosable.
    with caplog.at_level('WARNING'):
        vd.configure_cookies(True, 'notabrowser')
    assert vd.USE_BROWSER_COOKIES is False
    assert vd.COOKIE_BROWSER is None
    assert 'cookiesfrombrowser' not in vd._base_opts()
    assert any('unsupported browser' in r.message for r in caplog.records)


def test_configure_cookies_accepts_mixed_case_browser():
    # A UI dropdown or config file could plausibly send 'Chrome' -- validation
    # and storage are both case-insensitive, normalizing to lowercase.
    vd.configure_cookies(True, 'Chrome')
    assert vd.USE_BROWSER_COOKIES is True
    assert vd.COOKIE_BROWSER == 'chrome'
    assert vd._base_opts()['cookiesfrombrowser'] == ('chrome',)


# --- SPEC-cookie-fallback-fix.md Root Cause 1: DPAPI cookie-decrypt fallback
#
# A real overnight run showed browser-cookie extraction failing on every
# single song (Windows DPAPI / Chrome App-Bound Encryption -- yt-dlp issue
# #10927), permanently killing the whole run since the failure wasn't
# recognized as a bot/throttle condition and so was never retried.

def test_is_cookie_decrypt_error_matches_dpapi_and_cookie_load_text():
    assert vd._is_cookie_decrypt_error(Exception(_DPAPI_ERROR_TEXT))
    assert vd._is_cookie_decrypt_error(Exception('failed to load cookies'))
    assert vd._is_cookie_decrypt_error(Exception('Failed To Decrypt With DPAPI'))


def test_is_cookie_decrypt_error_does_not_match_bot_or_unrelated_errors():
    assert not vd._is_cookie_decrypt_error(
        Exception("Sign in to confirm you're not a bot"))
    assert not vd._is_cookie_decrypt_error(Exception('HTTP Error 429: Too Many Requests'))
    assert not vd._is_cookie_decrypt_error(Exception('network unreachable'))


def test_base_opts_omits_cookies_for_the_next_call_once_broken():
    vd.configure_cookies(True, 'chrome')
    vd._COOKIES_BROKEN = True
    assert 'cookiesfrombrowser' not in vd._base_opts()


def test_run_ytdlp_with_cookie_fallback_succeeds_normally_with_one_construction(monkeypatch):
    FakeYDL = _make_fake_ydl_class(['ok'])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    result = vd._run_ytdlp_with_cookie_fallback(
        {'cookiesfrombrowser': ('chrome',)}, lambda ydl: ydl.extract_info())

    assert result == 'ok'
    assert len(FakeYDL.calls) == 1
    assert vd._COOKIES_BROKEN is False


def test_run_ytdlp_with_cookie_fallback_retries_once_cookie_free_on_dpapi_failure(monkeypatch, caplog):
    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT), 'recovered'])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)
    opts = {'cookiesfrombrowser': ('chrome',), 'quiet': True}

    with caplog.at_level('WARNING'):
        result = vd._run_ytdlp_with_cookie_fallback(opts, lambda ydl: ydl.extract_info())

    assert result == 'recovered'
    assert vd._COOKIES_BROKEN is True
    assert len(FakeYDL.calls) == 2
    assert 'cookiesfrombrowser' in FakeYDL.calls[0]
    assert 'cookiesfrombrowser' not in FakeYDL.calls[1]
    assert FakeYDL.calls[1]['quiet'] is True   # everything else preserved
    assert any('cookie' in r.message.lower() for r in caplog.records)


def test_run_ytdlp_with_cookie_fallback_reraises_non_cookie_errors_without_retry(monkeypatch):
    bot_error = Exception("Sign in to confirm you're not a bot")
    FakeYDL = _make_fake_ydl_class([bot_error])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)
    opts = {'cookiesfrombrowser': ('chrome',)}

    with pytest.raises(Exception, match="not a bot"):
        vd._run_ytdlp_with_cookie_fallback(opts, lambda ydl: ydl.extract_info())

    assert vd._COOKIES_BROKEN is False
    assert len(FakeYDL.calls) == 1


def test_run_ytdlp_with_cookie_fallback_ignores_dpapi_text_when_cookies_were_never_used(monkeypatch):
    # No 'cookiesfrombrowser' in opts -- a DPAPI-shaped message here would be
    # a coincidence, not this failure class, and must not trigger a retry.
    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT)])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    with pytest.raises(Exception, match='DPAPI'):
        vd._run_ytdlp_with_cookie_fallback({'quiet': True}, lambda ydl: ydl.extract_info())

    assert vd._COOKIES_BROKEN is False
    assert len(FakeYDL.calls) == 1


def test_cookie_fallback_logs_the_warning_exactly_once(monkeypatch, caplog):
    FakeYDL = _make_fake_ydl_class(
        [Exception(_DPAPI_ERROR_TEXT), 'ok1', Exception(_DPAPI_ERROR_TEXT), 'ok2'])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)
    opts = {'cookiesfrombrowser': ('chrome',)}

    with caplog.at_level('WARNING'):
        r1 = vd._run_ytdlp_with_cookie_fallback(dict(opts), lambda ydl: ydl.extract_info())
        r2 = vd._run_ytdlp_with_cookie_fallback(dict(opts), lambda ydl: ydl.extract_info())

    assert (r1, r2) == ('ok1', 'ok2')
    warnings = [r for r in caplog.records if 'cookie' in r.message.lower()]
    assert len(warnings) == 1


def test_search_candidates_retries_without_cookies_after_dpapi_failure(monkeypatch):
    vd.configure_cookies(True, 'chrome')
    good = {'entries': [{'id': 'abc123', 'title': 'A Song', 'duration': 180}]}
    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT), good])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    candidates = vd.search_candidates('some query', n=1)

    assert candidates == [('https://www.youtube.com/watch?v=abc123', 'A Song', 180)]
    assert vd._COOKIES_BROKEN is True
    assert 'cookiesfrombrowser' in FakeYDL.calls[0]
    assert 'cookiesfrombrowser' not in FakeYDL.calls[1]


def test_search_candidates_bot_error_is_not_treated_as_a_cookie_failure(monkeypatch):
    vd.configure_cookies(True, 'chrome')
    bot_error = Exception("Sign in to confirm you're not a bot")
    FakeYDL = _make_fake_ydl_class([bot_error])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    with pytest.raises(vd.BotDetected):
        vd.search_candidates('some query', n=1)

    assert vd._COOKIES_BROKEN is False
    assert len(FakeYDL.calls) == 1
