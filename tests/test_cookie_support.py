# Optional, opt-in browser-cookie support for yt-dlp (SPEC-background-mode.md,
# Task 6). Off by default -- the central regression this file exists to prove
# is that _base_opts()'s output is byte-identical to before this feature
# existed whenever the setting is off, including when configure_cookies() is
# never called at all (a fresh install's module-level defaults).

import VideoDownload as vd


def setup_function(_func):
    # Every test starts from the untouched default, regardless of what an
    # earlier test in this (or another) module left behind.
    vd.configure_cookies(False, None)


def teardown_function(_func):
    vd.configure_cookies(False, None)


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
