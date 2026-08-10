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
    # (SPEC-cookie-fallback-fix.md) and _BROKEN_COOKIE_BROWSERS
    # (SPEC-cookie-chain-and-pacing.md) are deliberately never reset by
    # configure_cookies() itself -- they must be reset here instead, or the
    # first fallback test to run leaks its state into every later test in
    # this file. The set is the more dangerous of the two: a leaked 'chrome'
    # silently changes which browser a later test's chain starts on.
    vd.configure_cookies(False, None)
    vd._COOKIES_BROKEN = False
    vd._BROKEN_COOKIE_BROWSERS.clear()


def teardown_function(_func):
    vd.configure_cookies(False, None)
    vd._COOKIES_BROKEN = False
    vd._BROKEN_COOKIE_BROWSERS.clear()


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

# The second browser-cookie failure this app actually hits, copied verbatim from
# a 2026-08-05 overnight run's log. yt_dlp/cookies.py turns a Windows
# PermissionError (errno 13 -- Chrome is running and holding its Cookies file
# open) into a bare DownloadError, so nothing but the message text survives to
# identify it. The doubled 'ERROR: ERROR:' and the double spaces around the URL
# are real: cookies.py logs the message and YoutubeDL.report_error prefixes it
# again on the way out.
_LOCKED_CHROME_DB_ERROR_TEXT = (
    'ERROR: ERROR: Could not copy Chrome cookie database. See  '
    'https://github.com/yt-dlp/yt-dlp/issues/7271  for more info')


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


def test_is_cookie_decrypt_error_matches_locked_chrome_cookie_db():
    # Same failure class as DPAPI -- the browser's cookie store cannot be read,
    # so the only useful response is to carry on without it. Different message
    # though, and the original one killed every song in a 7441-song run.
    assert vd._is_cookie_decrypt_error(Exception(_LOCKED_CHROME_DB_ERROR_TEXT))
    assert vd._is_cookie_decrypt_error(Exception('COULD NOT COPY CHROME COOKIE DATABASE'))


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


def test_search_candidates_advances_to_the_next_browser_after_dpapi_failure(monkeypatch):
    # Behavior change from SPEC-cookie-chain-and-pacing.md: this used to assert
    # the retry went cookie-FREE. With a chain configured, a chrome DPAPI
    # failure now falls through to firefox and the same song keeps its cookies.
    vd.configure_cookies(True, 'chrome')
    good = {'entries': [{'id': 'abc123', 'title': 'A Song', 'duration': 180}]}
    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT), good])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    candidates = vd.search_candidates('some query', n=1)

    assert candidates == [('https://www.youtube.com/watch?v=abc123', 'A Song', 180)]
    # Chain is not exhausted -- firefox worked, so the run still has cookies.
    assert vd._COOKIES_BROKEN is False
    assert FakeYDL.calls[0]['cookiesfrombrowser'] == ('chrome',)
    assert FakeYDL.calls[1]['cookiesfrombrowser'] == ('firefox',)


def test_search_candidates_advances_to_the_next_browser_after_locked_chrome_db(monkeypatch):
    # End-to-end shape of the 2026-08-05 failure: Chrome was open, every song
    # died on the first construction. The retry has to rescue the same song --
    # now by moving to the next browser rather than dropping cookies.
    vd.configure_cookies(True, 'chrome')
    good = {'entries': [{'id': 'abc123', 'title': 'A Song', 'duration': 180}]}
    FakeYDL = _make_fake_ydl_class([Exception(_LOCKED_CHROME_DB_ERROR_TEXT), good])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    candidates = vd.search_candidates('some query', n=1)

    assert candidates == [('https://www.youtube.com/watch?v=abc123', 'A Song', 180)]
    assert vd._COOKIES_BROKEN is False
    assert FakeYDL.calls[0]['cookiesfrombrowser'] == ('chrome',)
    assert FakeYDL.calls[1]['cookiesfrombrowser'] == ('firefox',)


def test_search_candidates_goes_cookie_free_once_every_browser_fails(monkeypatch):
    # The 2026-08-09 machine, exactly: chrome and edge both DPAPI-fail. Only
    # after the whole chain is exhausted does the run drop cookies -- and that
    # is the one case where the old cookie-free behavior still applies.
    vd.configure_cookies(True, 'chrome')
    good = {'entries': [{'id': 'abc123', 'title': 'A Song', 'duration': 180}]}
    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT)] * 3 + [good])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    candidates = vd.search_candidates('some query', n=1)

    assert candidates == [('https://www.youtube.com/watch?v=abc123', 'A Song', 180)]
    assert vd._COOKIES_BROKEN is True
    assert [c.get('cookiesfrombrowser') for c in FakeYDL.calls] == [
        ('chrome',), ('firefox',), ('edge',), None]


def test_search_candidates_bot_error_is_not_treated_as_a_cookie_failure(monkeypatch):
    vd.configure_cookies(True, 'chrome')
    bot_error = Exception("Sign in to confirm you're not a bot")
    FakeYDL = _make_fake_ydl_class([bot_error])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    with pytest.raises(vd.BotDetected):
        vd.search_candidates('some query', n=1)

    assert vd._COOKIES_BROKEN is False
    assert len(FakeYDL.calls) == 1


def test_fetch_audio_recovers_after_dpapi_failure(tmp_path, monkeypatch):
    vd.configure_cookies(True, 'chrome')
    monkeypatch.setattr(vd, 'cleanup_temp_files', lambda folder: None)
    (tmp_path / 'video.sync.opus').write_bytes(b'fake audio')
    good_info = {'formats': [{'height': 480}, {'height': 240}]}
    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT), good_info])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    path, max_h, info = vd.fetch_audio(str(tmp_path), 'https://youtu.be/x')

    assert path == str(tmp_path / 'video.sync.opus')
    assert max_h == 480
    assert info == good_info
    # Chain advanced chrome -> firefox rather than dropping cookies.
    assert vd._COOKIES_BROKEN is False
    assert FakeYDL.calls[0]['cookiesfrombrowser'] == ('chrome',)
    assert FakeYDL.calls[1]['cookiesfrombrowser'] == ('firefox',)


def test_fetch_audio_still_swallows_non_cookie_non_bot_errors(tmp_path, monkeypatch):
    # Unrelated failure (e.g. network unreachable) after cookies are already
    # disabled -- fetch_audio's existing swallow-and-return contract must
    # still hold, unchanged by this fix.
    vd.configure_cookies(False, None)
    monkeypatch.setattr(vd, 'cleanup_temp_files', lambda folder: None)
    FakeYDL = _make_fake_ydl_class([Exception('network unreachable')])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    path, max_h, info = vd.fetch_audio(str(tmp_path), 'https://youtu.be/x')

    assert (path, max_h, info) == (None, 0, None)
    assert vd._COOKIES_BROKEN is False


def test_download_video_retries_without_cookies_after_dpapi_failure(tmp_path, monkeypatch):
    vd.configure_cookies(True, 'chrome')
    monkeypatch.setattr(vd, 'cleanup_temp_files', lambda folder: None)
    monkeypatch.setattr(vd, 'ffmpegAvailable', False)  # skip the remux subprocess path
    folder = tmp_path

    def _make_download_file():
        (folder / 'video.download.mp4').write_bytes(b'fake video')

    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT), _make_download_file])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    vd.download_video(str(folder), 'https://youtu.be/x', 'height<=720')

    assert (folder / 'video.mp4').exists()
    # Chain advanced chrome -> firefox rather than dropping cookies.
    assert vd._COOKIES_BROKEN is False
    assert FakeYDL.calls[0]['cookiesfrombrowser'] == ('chrome',)
    assert FakeYDL.calls[1]['cookiesfrombrowser'] == ('firefox',)


def test_download_video_bot_error_is_not_treated_as_a_cookie_failure(tmp_path, monkeypatch):
    vd.configure_cookies(True, 'chrome')
    monkeypatch.setattr(vd, 'cleanup_temp_files', lambda folder: None)
    bot_error = Exception('HTTP Error 429: Too Many Requests')
    FakeYDL = _make_fake_ydl_class([bot_error])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    with pytest.raises(vd.BotDetected):
        vd.download_video(str(tmp_path), 'https://youtu.be/x', 'height<=720')

    assert vd._COOKIES_BROKEN is False
    assert len(FakeYDL.calls) == 1


# --- SPEC-cookie-chain-and-pacing.md Part A: the fallback CHAIN
#
# The 2026-08-09 run went 9.5 hours cookie-free because Chrome's App-Bound
# Encryption made its store unreadable, while a signed-in Firefox store sat
# on the same machine untouched. _COOKIES_BROKEN was process-wide, so the
# first failure ended cookies for the whole run. Now only an exhausted chain
# does that.

def test_chain_puts_the_preferred_browser_first_then_the_defaults():
    vd.configure_cookies(True, 'chrome')
    assert vd._cookie_chain() == ['chrome', 'firefox', 'edge']


def test_chain_does_not_repeat_the_preferred_browser():
    # firefox is both the preference and the head of _COOKIE_CHAIN_ORDER --
    # it must appear exactly once or it would be retried after failing.
    vd.configure_cookies(True, 'firefox')
    assert vd._cookie_chain() == ['firefox', 'edge', 'chrome']
    assert vd._cookie_chain().count('firefox') == 1


def test_chain_is_empty_when_cookies_are_off():
    vd.configure_cookies(False, None)
    assert vd._cookie_chain() == []
    assert vd._active_cookie_browser() is None


def test_chain_filters_unsupported_browser_names(monkeypatch):
    # Defense in depth, mirroring configure_cookies' own guard: a bad name in
    # the fallback order must never reach yt-dlp.
    monkeypatch.setattr(vd, '_COOKIE_CHAIN_ORDER', ('firefox', 'notabrowser'))
    vd.configure_cookies(True, 'chrome')
    assert vd._cookie_chain() == ['chrome', 'firefox']


def test_dpapi_failure_advances_to_the_next_browser_and_the_same_call_returns(monkeypatch):
    """The contract that matters: the song that hits the wall is the song that
    recovers -- one _run_ytdlp_with_cookie_fallback call, one return value."""
    vd.configure_cookies(True, 'chrome')
    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT), 'recovered'])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    result = vd._run_ytdlp_with_cookie_fallback(
        vd._base_opts(), lambda ydl: ydl.extract_info())

    assert result == 'recovered'
    assert FakeYDL.calls[0]['cookiesfrombrowser'] == ('chrome',)
    assert FakeYDL.calls[1]['cookiesfrombrowser'] == ('firefox',)
    assert vd._BROKEN_COOKIE_BROWSERS == {'chrome'}
    assert vd._COOKIES_BROKEN is False


def test_exhausting_every_browser_ends_cookie_free(monkeypatch):
    vd.configure_cookies(True, 'chrome')
    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT)] * 3 + ['ok'])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    result = vd._run_ytdlp_with_cookie_fallback(
        vd._base_opts(), lambda ydl: ydl.extract_info())

    assert result == 'ok'
    assert [c.get('cookiesfrombrowser') for c in FakeYDL.calls] == [
        ('chrome',), ('firefox',), ('edge',), None]
    assert vd._BROKEN_COOKIE_BROWSERS == {'chrome', 'firefox', 'edge'}
    assert vd._COOKIES_BROKEN is True
    # And every later call skips the dead chain entirely.
    assert 'cookiesfrombrowser' not in vd._base_opts()


@pytest.mark.parametrize('sign_text', [
    'ERROR: Failed to decrypt with DPAPI. See issues/10927',
    'ERROR: Failed to load cookies',
    'ERROR: ERROR: Could not copy Chrome cookie database. See issues/7271',
])
def test_each_cookie_error_sign_advances_the_chain(monkeypatch, sign_text):
    vd.configure_cookies(True, 'chrome')
    FakeYDL = _make_fake_ydl_class([Exception(sign_text), 'recovered'])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    result = vd._run_ytdlp_with_cookie_fallback(
        vd._base_opts(), lambda ydl: ydl.extract_info())

    assert result == 'recovered'
    assert FakeYDL.calls[1]['cookiesfrombrowser'] == ('firefox',)


def test_non_cookie_error_propagates_without_advancing(monkeypatch):
    vd.configure_cookies(True, 'chrome')
    FakeYDL = _make_fake_ydl_class([Exception('network unreachable')])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    with pytest.raises(Exception, match='network unreachable'):
        vd._run_ytdlp_with_cookie_fallback(
            vd._base_opts(), lambda ydl: ydl.extract_info())

    assert len(FakeYDL.calls) == 1
    assert vd._BROKEN_COOKIE_BROWSERS == set()
    assert vd._COOKIES_BROKEN is False


def test_a_broken_browser_is_not_retried_on_a_later_call(monkeypatch):
    """Per-process stickiness at finer grain: re-paying a known failure once
    per song is the cost SPEC-cookie-fallback-fix.md existed to remove."""
    vd.configure_cookies(True, 'chrome')
    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT), 'first', 'second'])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    vd._run_ytdlp_with_cookie_fallback(vd._base_opts(), lambda ydl: ydl.extract_info())
    # A fresh _base_opts() (what the next song does) must already start on
    # firefox -- chrome is never offered again.
    second = vd._run_ytdlp_with_cookie_fallback(
        vd._base_opts(), lambda ydl: ydl.extract_info())

    assert second == 'second'
    assert len(FakeYDL.calls) == 3
    assert FakeYDL.calls[2]['cookiesfrombrowser'] == ('firefox',)


def test_one_warning_per_transition_not_one_per_song(monkeypatch, caplog):
    """A 7,000-song run must not log a cookie warning per song. Each browser
    gets exactly one line, no matter how many calls hit the same wall."""
    vd.configure_cookies(True, 'chrome')
    FakeYDL = _make_fake_ydl_class(
        [Exception(_DPAPI_ERROR_TEXT), 'a', 'b', 'c', 'd'])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    with caplog.at_level('WARNING'):
        for _ in range(4):
            vd._run_ytdlp_with_cookie_fallback(
                vd._base_opts(), lambda ydl: ydl.extract_info())

    cookie_warnings = [r for r in caplog.records if 'cookie' in r.message.lower()]
    assert len(cookie_warnings) == 1
    assert 'trying firefox next' in cookie_warnings[0].message


def test_chain_exhausted_warning_is_logged_once(monkeypatch, caplog):
    vd.configure_cookies(True, 'chrome')
    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT)] * 3 + ['ok', 'ok2'])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    with caplog.at_level('WARNING'):
        vd._run_ytdlp_with_cookie_fallback(vd._base_opts(), lambda ydl: ydl.extract_info())
        vd._run_ytdlp_with_cookie_fallback(vd._base_opts(), lambda ydl: ydl.extract_info())

    exhausted = [r for r in caplog.records
                 if 'without browser cookies' in r.message]
    assert len(exhausted) == 1


def test_no_cookie_value_reaches_the_logs(monkeypatch, caplog):
    """Only browser NAMES ever pass through this module -- the property the
    original cookie feature was built around must survive the chain."""
    vd.configure_cookies(True, 'chrome')
    FakeYDL = _make_fake_ydl_class([Exception(_DPAPI_ERROR_TEXT), 'ok'])
    monkeypatch.setattr(vd.yt_dlp, 'YoutubeDL', FakeYDL)

    with caplog.at_level('WARNING'):
        vd._run_ytdlp_with_cookie_fallback(
            vd._base_opts(), lambda ydl: ydl.extract_info())

    for record in caplog.records:
        assert 'SAPISID' not in record.message
        assert 'sessionid' not in record.message.lower()


def test_base_opts_is_byte_identical_when_cookies_are_off():
    """The central regression this file exists to prove, restated for the
    chain: with the feature off, nothing about the chain is observable."""
    vd.configure_cookies(False, None)
    assert vd._base_opts() == {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': 1,
        'sleep_interval_requests': 1,
    }
