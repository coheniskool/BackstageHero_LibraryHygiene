# Curator helper: find YouTube candidates for an unresolved chart.
# Returns search results via yt-dlp, plus an optional LLM-picked index if a key is set.
# Missing yt-dlp, no network, no LLM key - each fails silently and the rest still works.

import json
import os
import urllib.request

LLM_KEY = os.environ.get('BACKSTAGEHERO_LLM_KEY', '')
LLM_MODEL = os.environ.get('BACKSTAGEHERO_LLM_MODEL', 'claude-3-5-haiku-latest')
LLM_URL = 'https://api.anthropic.com/v1/messages'

try:
    import yt_dlp
except Exception:
    yt_dlp = None


def youtube_candidates(query, n=6):
    """Up to n YouTube search results for a query: [{id,title,duration,channel,url}]."""
    if not yt_dlp or not query:
        return []
    # Mirror the client's extractor config: the android_vr/android clients serve
    # results without a JS runtime, cookies, or PO tokens. A bare YoutubeDL hits
    # YouTube's JS-player/bot wall and returns nothing.
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': True,           # metadata only - no per-video extraction
        'noplaylist': 1,
        'extractor_args': {'youtube': {'player_client': ['android_vr', 'android']}},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f'ytsearch{n}:{query}', download=False)
    except Exception:
        return []
    out = []
    for entry in (info or {}).get('entries', []) or []:
        vid = entry.get('id')
        if not vid:
            continue
        out.append({
            'id': vid,
            'title': entry.get('title') or '',
            'duration': entry.get('duration'),
            'channel': entry.get('channel') or entry.get('uploader') or '',
            'url': f'https://www.youtube.com/watch?v={vid}',
        })
    return out


def llm_pick(artist, title, candidates):
    """Index of the best candidate per the LLM, or None on error/no key."""
    if not LLM_KEY or not candidates:
        return None
    listing = '\n'.join(
        f'{i}. {c["title"]}  [channel: {c["channel"]}, {c.get("duration") or "?"}s]'
        for i, c in enumerate(candidates))
    prompt = (
        f'A Clone Hero chart is "{artist} - {title}". Pick the YouTube result below '
        f'that is most likely the official music video for that exact song (not a '
        f'live version, cover, lyric video, or gameplay). Reply with ONLY the '
        f'number, or -1 if none is a confident match.\n\n{listing}')
    body = json.dumps({
        'model': LLM_MODEL,
        'max_tokens': 8,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode('utf-8')
    req = urllib.request.Request(LLM_URL, data=body, headers={
        'x-api-key': LLM_KEY,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8', 'replace'))
        text = ''.join(b.get('text', '') for b in data.get('content', []))
        idx = int(''.join(ch for ch in text if ch.isdigit() or ch == '-'))
        if 0 <= idx < len(candidates):
            return idx
    except Exception:
        pass
    return None


def suggest(artist, title):
    """Candidates plus an optional recommended index for one chart."""
    query = (f'{artist} {title}'.strip()) or title or ''
    candidates = youtube_candidates(query)
    return {
        'query': query,
        'candidates': candidates,
        'recommended': llm_pick(artist, title, candidates),
        'llm_enabled': bool(LLM_KEY),
        'search_enabled': yt_dlp is not None,
    }
