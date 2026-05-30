# Two update channels: the app exe (from GitHub releases) and yt-dlp (from PyPI).
# yt-dlp updates separately because YouTube breaks it every few weeks, way more
# often than we cut releases. Both only run when frozen - source installs use pip.

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

GITHUB_REPO = 'jmb988/BackstageHero'
EXE_ASSET_NAME = 'BackstageHero.exe'

_API_LATEST = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'
_PYPI_YTDLP = 'https://pypi.org/pypi/yt-dlp/json'
_UA = 'BackstageHero-Updater'

_YTDLP_CHECK_INTERVAL = 20 * 3600  # ~daily
_META_TIMEOUT = 5    # short timeout for metadata fetches so a bad connection doesn't hang
_DL_TIMEOUT   = 120  # socket timeout for binary downloads (exe / wheel)


def _frozen():
    return getattr(sys, 'frozen', False)


def data_dir():
    """Per-user data folder for BackstageHero (cache, settings, etc)."""
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    path = os.path.join(base, 'BackstageHero')
    os.makedirs(path, exist_ok=True)
    return path


def _ytdlp_dir():
    """Where the cached yt-dlp package lives."""
    return os.path.join(data_dir(), 'ytdlp')


def _ver_tuple(v):
    """Version string to comparable tuple. Strips leading non-digits so 'v.1.0.3' works."""
    return tuple(int(n) for n in re.findall(r'\d+', v or ''))


def _get_json(url, timeout=_META_TIMEOUT):
    req = urllib.request.Request(url, headers={'User-Agent': _UA,
                                               'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8', 'replace'))


def prefer_cached_ytdlp():
    """Call before importing yt_dlp. Puts the cached newer version on sys.path if one exists."""
    if not _frozen():
        return
    try:
        cache = _ytdlp_dir()
        staged = cache + '.new'
        # promote staged update before yt_dlp imports - can't swap a live package mid-run
        if os.path.isdir(os.path.join(staged, 'yt_dlp')):
            _swap_dir(staged, cache)
        if os.path.isfile(os.path.join(cache, 'yt_dlp', '__init__.py')):
            sys.path.insert(0, cache)
    except Exception:
        pass


def _wheel_url_and_version():
    """(download_url, version) for the latest yt-dlp wheel on PyPI, or None."""
    info = _get_json(_PYPI_YTDLP)
    version = info['info']['version']
    for entry in info.get('urls', []):
        if entry.get('packagetype') == 'bdist_wheel':
            return entry['url'], version
    return None


def _swap_dir(staging, target):
    """Atomically replace directory `target` with `staging` (same volume)."""
    backup = target + '.bak'
    if os.path.exists(backup):
        shutil.rmtree(backup, ignore_errors=True)
    if os.path.exists(target):
        os.rename(target, backup)
    try:
        os.rename(staging, target)
    except Exception:
        if os.path.exists(backup):       # restore on failure
            os.rename(backup, target)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def maybe_update_ytdlp(current_version):
    """Check PyPI for a newer yt-dlp and stage it for the next launch. At most once a day.
    Returns the new version string if something was staged, else None."""
    if not _frozen():
        return None
    try:
        stamp = os.path.join(data_dir(), 'ytdlp_last_check')
        now = time.time()
        if os.path.exists(stamp) and now - os.path.getmtime(stamp) < _YTDLP_CHECK_INTERVAL:
            return None
        # write the stamp first so a failed check still waits a day before retrying
        with open(stamp, 'w') as f:
            f.write(str(int(now)))

        found = _wheel_url_and_version()
        if not found:
            return None
        url, latest = found
        if _ver_tuple(latest) <= _ver_tuple(current_version):
            return None

        work = tempfile.mkdtemp(prefix='bh_ytdlp_')
        try:
            wheel = os.path.join(work, 'ytdlp.whl')
            _download(url, wheel)
            staging = os.path.join(work, 'extract')
            os.makedirs(staging)
            with zipfile.ZipFile(wheel) as zf:
                for member in zf.namelist():
                    if member.startswith('yt_dlp/'):
                        zf.extract(member, staging)
            if not os.path.isdir(os.path.join(staging, 'yt_dlp')):
                return None
            # stage to .new - gets promoted on next launch before yt_dlp is imported
            _swap_dir(staging, _ytdlp_dir() + '.new')
            return latest
        finally:
            shutil.rmtree(work, ignore_errors=True)
    except Exception:
        return None


def _download(url, dest, progress=False):
    """Download a URL to a file, optionally printing progress."""
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=_DL_TIMEOUT) as resp, open(dest, 'wb') as out:
        total = int(resp.headers.get('Content-Length') or 0)
        got = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if progress and total:
                pct = got * 100 // total
                print(f'\r  Downloading update... {pct}%', end='', flush=True)
        if progress and total:
            print()


def _find_release_asset(release, name):
    for asset in release.get('assets', []):
        if asset.get('name') == name:
            return asset.get('browser_download_url')
    return None


def _expected_sha256(release):
    """Get the expected SHA-256 from a .sha256 sidecar or the release body. Returns None if not found."""
    try:
        side = _find_release_asset(release, EXE_ASSET_NAME + '.sha256')
        if side:
            req = urllib.request.Request(side, headers={'User-Agent': _UA})
            with urllib.request.urlopen(req, timeout=_META_TIMEOUT) as resp:
                text = resp.read().decode('utf-8', 'replace')
            m = re.search(r'[0-9a-fA-F]{64}', text)
            if m:
                return m.group(0).lower()
        m = re.search(r'[0-9a-fA-F]{64}', release.get('body') or '')
        if m:
            return m.group(0).lower()
    except Exception:
        pass
    return None


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _prompt_yes(message, timeout=5.0, default=True):
    """Prompt with a timeout. Returns default if the user doesn't respond in time."""
    print(message, end='', flush=True)
    box = [None]

    def _read():
        try:
            box[0] = sys.stdin.readline()
        except Exception:
            box[0] = ''

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    ans = box[0]
    if ans is None:          # timed out
        print()
        return default
    ans = ans.strip().lower()
    if ans == '':
        return default
    return ans in ('y', 'yes')


def _cleanup_old_exe():
    """Delete the previous exe left behind by an earlier self-update."""
    try:
        old = sys.executable + '.old'
        if os.path.exists(old):
            os.remove(old)
    except Exception:
        pass


def _apply_app_update(asset_url, expected_sha):
    """Download the new exe, verify the checksum, swap it in and relaunch.
    Returns False if anything fails - the current exe is left alone."""
    cur = sys.executable
    work = tempfile.mkdtemp(prefix='bh_app_')
    new = os.path.join(work, EXE_ASSET_NAME)
    try:
        _download(asset_url, new, progress=True)
        if expected_sha:
            actual = _sha256_of(new)
            if actual != expected_sha:
                print('  Update checksum did not match - keeping the current version.')
                return False

        old = cur + '.old'
        if os.path.exists(old):
            try:
                os.remove(old)
            except Exception:
                pass
        os.rename(cur, old)
        try:
            shutil.move(new, cur)
        except Exception:
            os.rename(old, cur)  # put it back if the move fails
            raise
    except Exception as e:
        print(f'  Could not install the update ({e}). Continuing with the current version.')
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print('  Update installed. Restarting...')
    try:
        subprocess.Popen([cur], cwd=os.getcwd(), close_fds=True)
    except Exception:
        pass
    return True  # caller is responsible for exiting the current process


def check_app_update(current_version):
    """Check GitHub for a newer release. Returns (latest_tag, asset_url, sha256) if one
    exists, or None if already current / offline / any error."""
    if not _frozen():
        return None
    try:
        release = _get_json(_API_LATEST)
        tag = release.get('tag_name') or ''
        if _ver_tuple(tag) <= _ver_tuple(current_version):
            return None
        asset = _find_release_asset(release, EXE_ASSET_NAME)
        if not asset:
            return None
        latest = re.sub(r'^\D*', '', tag) or tag
        return latest, asset, _expected_sha256(release)
    except Exception:
        return None


def apply_app_update(asset_url, expected_sha):
    """Download the new exe, verify it, swap it in place and relaunch.
    Returns False on any failure (current exe untouched)."""
    return _apply_app_update(asset_url, expected_sha)


def run_startup_updates(app_version, ytdlp_version):
    """Legacy entry-point kept for the CLI path. GUI uses check_app_update instead."""
    _cleanup_old_exe()
    info = check_app_update(app_version)
    if info:
        latest, asset, sha = info
        if _prompt_yes(
                f'\nUpdate available: v{app_version} -> v{latest}. '
                'Install now? [Y/n] '):
            _apply_app_update(asset, sha)
        else:
            print('  Skipping update for now.')
    staged = maybe_update_ytdlp(ytdlp_version)
    if staged:
        print(f'  Updated downloader (yt-dlp {staged}) - active next launch.')
