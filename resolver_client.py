# Community resolver client.
# Looks up whether the community has already confirmed a video for this chart,
# and reports back confident matches so others benefit too.
# Completely optional - if the resolver URL isn't set, everything here does nothing.

import hashlib
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import uuid

import updater

log = logging.getLogger('backstagehero')

_DEFAULT_RESOLVER = 'https://backstage.jimmyproton.co.uk'
RESOLVER_BASE = os.environ.get('BACKSTAGEHERO_RESOLVER', _DEFAULT_RESOLVER).rstrip('/')

# notes files are the same bytes for everyone who downloaded the same chart pack,
# so hashing them gives us a shared key. song.ini excluded - it has per-user stuff.
_CHART_FILES = ('notes.chart', 'notes.mid', 'notes.eof')

_RESOLVE_TIMEOUT = 3
_REPORT_TIMEOUT  = 3
_PING_TIMEOUT    = 3
_UA = 'BackstageHero-Client'

# share back to the pool or not. look-ups still work either way, this only stops
# the outbound /report and /ping. GUI sets it from the saved setting; on by
# default. BACKSTAGEHERO_NO_SHARE=1 forces it off if you're not using the GUI.
_sharing = os.environ.get('BACKSTAGEHERO_NO_SHARE', '') not in ('1', 'true', 'yes')


def set_sharing(on):
    global _sharing
    _sharing = bool(on)


def sharing_enabled():
    return _sharing


def enabled():
    return bool(RESOLVER_BASE)


def chart_hash(folder):
    """Stable hash identifying this chart across all users, or None.

    Hashes the raw bytes of the chart's notes file, so two users who downloaded
    the same chart produce the same hash and therefore share one mapping. A
    'ch1:' prefix lets the scheme change later without colliding with old data.
    """
    try:
        for name in _CHART_FILES:
            path = os.path.join(folder, name)
            if os.path.exists(path):
                h = hashlib.sha256()
                with open(path, 'rb') as f:
                    for block in iter(lambda: f.read(1 << 20), b''):
                        h.update(block)
                return 'ch1:' + h.hexdigest()
    except Exception:
        pass
    return None


def _client_id():
    """Random anonymous ID stored per machine so the server can count distinct voters."""
    try:
        path = os.path.join(updater.data_dir(), 'client_id')
        if os.path.exists(path):
            with open(path) as f:
                val = f.read().strip()
                if val:
                    return val
        val = uuid.uuid4().hex
        with open(path, 'w') as f:
            f.write(val)
        return val
    except Exception:
        return 'anon'


# what a YouTube video id looks like. everything the server hands back goes
# through this before it touches a URL or song.ini - the server validates on
# write too, but the client shouldn't have to trust that.
_VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')


def resolve(ch):
    """Look up the community-confirmed video for this chart. Returns the mapping dict or None."""
    if not RESOLVER_BASE or not ch:
        return None
    try:
        url = RESOLVER_BASE + '/resolve?hash=' + urllib.parse.quote(ch)
        req = urllib.request.Request(url, headers={'User-Agent': _UA})
        with urllib.request.urlopen(req, timeout=_RESOLVE_TIMEOUT) as resp:
            data = json.loads(resp.read(1 << 20).decode('utf-8', 'replace'))
        if data.get('status') != 'approved':
            return None
        if not _VIDEO_ID_RE.match(str(data.get('video_id') or '')):
            log.warning('resolver returned a malformed video id; ignoring')
            return None
        start = data.get('start_ms')
        if start is not None:
            start = int(start)
            if not -3_600_000 <= start <= 3_600_000:
                return None
            data['start_ms'] = start
        return data
    except Exception:
        log.debug('resolve() failed', exc_info=True)
    return None


def ping(sharing=True, app_version=''):
    """Heartbeat on startup so the server can count active users. Only goes out
    when sharing is on, since it carries the per-machine UUID. Ignore failures."""
    if not RESOLVER_BASE or not _sharing:
        return
    try:
        body = json.dumps({
            'client_id':   _client_id(),
            'sharing':     bool(sharing),
            'app_version': app_version,
        }).encode('utf-8')
        req = urllib.request.Request(
            RESOLVER_BASE + '/ping', data=body,
            headers={'User-Agent': _UA, 'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=_PING_TIMEOUT).close()
    except Exception:
        log.debug('ping() failed', exc_info=True)


def report(ch, video_id, start_ms, confidence, artist=None, title=None):
    """Send a vote for this chart->video mapping. Only called on confident
    fingerprint matches, and only when the user has sharing enabled."""
    if not RESOLVER_BASE or not _sharing or not ch or not video_id:
        return
    try:
        body = json.dumps({
            'hash': ch,
            'video_id': video_id,
            'start_ms': int(start_ms),
            'client_id': _client_id(),
            'confidence': float(confidence),
            'artist': artist or '',
            'title': title or '',
        }).encode('utf-8')
        req = urllib.request.Request(
            RESOLVER_BASE + '/report', data=body,
            headers={'User-Agent': _UA, 'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=_REPORT_TIMEOUT).close()
    except Exception:
        log.debug('report() failed', exc_info=True)
