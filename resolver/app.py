# BackstageHero resolver service (FastAPI).
# GET  /resolve?hash=  -> approved mapping or {"status":"none"}
# POST /report         -> record a vote
# GET  /healthz        -> liveness
# /admin/*             -> curator endpoints (Cloudflare Access + bearer token)

import json
import os
import re
import urllib.request
import urllib.parse

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import db
import suggest as suggest_mod

# video_id has to be a real YouTube id, hash is our chart-hash format. check them
# here so junk/markup never reaches the DB or the dashboard.
_VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')
_HASH_RE     = re.compile(r'^[A-Za-z0-9:._-]{1,128}$')


def _require_video_id(v):
    if not _VIDEO_ID_RE.match(v or ''):
        raise HTTPException(status_code=400, detail='invalid video_id')


def _require_hash(h):
    if not _HASH_RE.match(h or ''):
        raise HTTPException(status_code=400, detail='invalid hash')


def _client_ip(request):
    """Client IP, as best we can get it. Behind Cloudflare it's in
    CF-Connecting-IP, otherwise the socket peer. Only used to dedup the made-up
    client_ids so one machine can't stack a quorum."""
    return (request.headers.get('cf-connecting-ip')
            or (request.client.host if request.client else '')
            or '')

# Optional Cloudflare single-URL cache purge, so a curator change is served
# immediately instead of waiting out the edge TTL. All three must be set, else
# purging is skipped (the change still appears once the cache expires).
CF_API_TOKEN = os.environ.get('CF_API_TOKEN', '')
CF_ZONE_ID = os.environ.get('CF_ZONE_ID', '')
PUBLIC_URL = os.environ.get('BACKSTAGEHERO_PUBLIC_URL', '').rstrip('/')


def _purge_resolve(chart_hash):
    """Purge one /resolve URL from Cloudflare's edge cache. Skipped if env vars aren't set."""
    if not (CF_API_TOKEN and CF_ZONE_ID and PUBLIC_URL):
        return
    try:
        target = PUBLIC_URL + '/resolve?hash=' + urllib.parse.quote(chart_hash)
        body = json.dumps({'files': [target]}).encode('utf-8')
        req = urllib.request.Request(
            f'https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/purge_cache',
            data=body, method='POST',
            headers={'Authorization': f'Bearer {CF_API_TOKEN}',
                     'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:
        pass

DB_PATH = os.environ.get('BACKSTAGEHERO_DB', os.path.join('data', 'resolver.sqlite3'))
ADMIN_TOKEN = os.environ.get('BACKSTAGEHERO_ADMIN_TOKEN', '')

# How long Cloudflare/clients may cache a /resolve answer. Approved mappings
# rarely change, so a few hours of edge caching makes the home uplink a non-issue;
# a curator change purges the single affected URL (see the deploy runbook).
RESOLVE_CACHE_SECONDS = int(os.environ.get('BACKSTAGEHERO_RESOLVE_TTL', '21600'))

app = FastAPI(title='BackstageHero Resolver', version='1.0')

# Initialise at import so the schema exists no matter how the app is launched
# (uvicorn, gunicorn, or a test client that doesn't trigger lifespan events).
db.init_db(DB_PATH)


def _conn():
    conn = db.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def _require_admin(authorization: str = Header(default='')):
    """Defence in depth behind Cloudflare Access. If no token is configured the
    check is skipped (Access alone guards it); if one is set it must match."""
    if not ADMIN_TOKEN:
        return
    if authorization != f'Bearer {ADMIN_TOKEN}':
        raise HTTPException(status_code=401, detail='admin token required')


@app.get('/healthz')
def healthz():
    return {'ok': True}


@app.get('/resolve')
def resolve(hash: str, response: Response, conn=Depends(_conn)):
    result = db.resolve(conn, hash)
    if result.get('status') == 'approved':
        response.headers['Cache-Control'] = f'public, max-age={RESOLVE_CACHE_SECONDS}'
    else:
        # Don't cache misses for long - they flip to a hit as soon as a chart
        # reaches consensus, and we want that to surface quickly.
        response.headers['Cache-Control'] = 'public, max-age=60'
    return result


class ReportIn(BaseModel):
    hash: str = Field(max_length=128)
    video_id: str = Field(max_length=32)
    start_ms: int
    client_id: str = Field(max_length=64)
    confidence: float = 1.0
    artist: str = Field(default='', max_length=200)
    title: str = Field(default='', max_length=200)


class PingIn(BaseModel):
    client_id:   str = Field(max_length=64)
    sharing:     bool = False
    app_version: str  = Field(default='', max_length=32)


@app.post('/ping')
def ping(body: PingIn, request: Request, conn=Depends(_conn)):
    if not body.client_id:
        raise HTTPException(status_code=400, detail='client_id required')
    db.record_ping(conn, body.client_id, body.sharing, body.app_version)
    return {'ok': True}


@app.post('/report')
def report(body: ReportIn, request: Request, conn=Depends(_conn)):
    if not body.client_id:
        raise HTTPException(status_code=400, detail='client_id required')
    _require_hash(body.hash)
    _require_video_id(body.video_id)
    status = db.record_vote(
        conn, body.hash, body.video_id, body.start_ms, body.client_id,
        max(0.0, min(1.0, body.confidence)), body.artist, body.title,
        ip=_client_ip(request))
    return {'status': status}


@app.get('/admin/pending', dependencies=[Depends(_require_admin)])
def admin_pending(conn=Depends(_conn)):
    return {'pending': db.list_pending(conn)}


@app.get('/admin/stats', dependencies=[Depends(_require_admin)])
def admin_stats(conn=Depends(_conn)):
    return db.stats(conn)


class SetIn(BaseModel):
    hash: str = Field(max_length=128)
    video_id: str = Field(default='', max_length=32)
    status: str          # 'approved' | 'rejected' | 'pending'


@app.post('/admin/set', dependencies=[Depends(_require_admin)])
def admin_set(body: SetIn, conn=Depends(_conn)):
    if body.status not in ('approved', 'rejected', 'pending'):
        raise HTTPException(status_code=400, detail='bad status')
    _require_hash(body.hash)
    if body.video_id:                 # may be empty for a 'rejected' chart
        _require_video_id(body.video_id)
    db.set_status(conn, body.hash, body.video_id, body.status, lock=True)
    _purge_resolve(body.hash)
    return {'ok': True}


class OffsetIn(BaseModel):
    hash: str
    video_id: str
    start_ms: int


@app.post('/admin/offset', dependencies=[Depends(_require_admin)])
def admin_offset(body: OffsetIn, conn=Depends(_conn)):
    _require_hash(body.hash)
    _require_video_id(body.video_id)
    db.set_start_ms(conn, body.hash, body.video_id, body.start_ms)
    _purge_resolve(body.hash)
    return {'ok': True}


class SuggestIn(BaseModel):
    artist: str = Field(default='', max_length=200)
    title: str = Field(default='', max_length=200)


@app.post('/admin/suggest', dependencies=[Depends(_require_admin)])
def admin_suggest(body: SuggestIn):
    """YouTube candidates (and an optional AI pick) for one unresolved chart."""
    return suggest_mod.suggest(body.artist, body.title)


@app.get('/admin', response_class=HTMLResponse)
def admin_page():
    return _ADMIN_HTML


# Single-file curator dashboard. It is normally reached behind Cloudflare Access;
# if an admin token is also set it is entered once and sent on each API call.
_ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>BackstageHero - Curator</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font:14px system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 header{padding:14px 20px;background:#171a21;border-bottom:1px solid #2a2f3a;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
 h1{font-size:16px;margin:0}
 .stat{color:#9aa4b2}.stat b{color:#e6e6e6}
 main{padding:16px 20px}
 .card{background:#171a21;border:1px solid #2a2f3a;border-radius:8px;padding:12px 14px;margin-bottom:10px}
 .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .song{font-weight:600}.muted{color:#9aa4b2;font-size:12px}
 button{background:#2a2f3a;color:#e6e6e6;border:1px solid #3a4150;border-radius:6px;padding:6px 10px;cursor:pointer}
 button:hover{background:#343b48} button.ok{background:#1f6f43;border-color:#26824f} button.no{background:#7a2530;border-color:#92303c}
 input{background:#0f1115;color:#e6e6e6;border:1px solid #3a4150;border-radius:6px;padding:6px 8px}
 .cands{margin-top:10px;display:none} .cand{border-top:1px solid #2a2f3a;padding:8px 0}
 .rec{color:#7fd1a0;font-weight:600} a{color:#6ea8fe}
</style></head><body>
<header>
 <h1>BackstageHero - Curator</h1>
 <span class="stat" id="stats">loading...</span>
 <span style="flex:1"></span>
 <button id="refresh">Refresh</button>
</header>
<main id="list">loading...</main>
<script>
"use strict";
// everything here is built with createElement + textContent. nothing from a
// report (artist/title/video_id/chart_hash/channel) gets dropped into an HTML
// string or an onclick, so a junk payload can't run anything in the dashboard.
let TOKEN = sessionStorage.getItem('bh_token') || '';
function hdr(j){ return j?{'Content-Type':'application/json'}:{}; }
async function api(path, opts){
  opts = opts || {};
  const headers = Object.assign({}, opts.headers||{});
  if(TOKEN) headers['Authorization'] = 'Bearer '+TOKEN;
  const r = await fetch(path, Object.assign({}, opts, {headers}));
  if(r.status===401){
    TOKEN = prompt('Admin token:')||''; sessionStorage.setItem('bh_token',TOKEN);
    if(TOKEN) return api(path, opts);
  }
  return r;
}
async function post(path, body){ return api(path,{method:'POST',headers:hdr(true),body:JSON.stringify(body)}); }

function el(tag, opts){
  const e = document.createElement(tag);
  if(opts){
    if(opts.cls) e.className = opts.cls;
    if(opts.text != null) e.textContent = opts.text;   // textContent = no HTML injection
    if(opts.attrs) for(const k in opts.attrs) e.setAttribute(k, opts.attrs[k]);
    if(opts.on) for(const ev in opts.on) e.addEventListener(ev, opts.on[ev]);
  }
  return e;
}
function statNode(label, value){
  const span = el('span'); span.append(label+' ');
  span.append(el('b', {text:String(value)})); return span;
}

async function load(){
  const s = await (await api('/admin/stats',{headers:hdr(false)})).json();
  const stats = document.getElementById('stats');
  stats.textContent = '';
  const items = [['charts',s.charts],['approved',s.approved],['pending',s.pending],
    ['votes',s.votes],['| users 24h',s.active_24h],['/7d',s.active_7d],
    ['/ever',s.total_ever],['· sharing 24h',s.sharing_24h],['/7d',s.sharing_7d]];
  items.forEach(([l,v],i)=>{ if(i) stats.append('  '); stats.append(statNode(l,v)); });

  const data = await (await api('/admin/pending',{headers:hdr(false)})).json();
  const list = document.getElementById('list');
  list.textContent = '';
  if(!data.pending.length){
    list.append(el('div',{cls:'card',text:'Nothing pending. All caught up.'}));
    return;
  }
  data.pending.forEach(m => list.append(rowCard(m)));
}

function rowCard(m){
  const card = el('div',{cls:'card'});
  const name = (m.artist ? m.artist+' - ' : '') + (m.title || '(unknown title)');

  const top = el('div',{cls:'row'});
  top.append(el('span',{cls:'song',text:name}));
  top.append(el('span',{cls:'muted',
    text:`votes ${m.votes} · status ${m.status} · ` +
         (m.video_id ? 'vid '+m.video_id : 'no video yet')}));
  card.append(top);

  const actions = el('div',{cls:'row',attrs:{style:'margin-top:8px'}});
  actions.append(el('button',{cls:'ok',text:'Approve',
    on:{click:()=>setStatus(m.chart_hash,m.video_id,'approved')}}));
  actions.append(el('button',{cls:'no',text:'Reject',
    on:{click:()=>setStatus(m.chart_hash,m.video_id,'rejected')}}));
  const vidInput = el('input',{attrs:{placeholder:'set video id',size:'14'}});
  actions.append(vidInput);
  actions.append(el('button',{text:'Set & approve',
    on:{click:()=>setStatus(m.chart_hash, vidInput.value.trim(), 'approved')}}));
  const cands = el('div',{cls:'cands'});
  actions.append(el('button',{text:'Suggest',
    on:{click:()=>suggest(m.chart_hash, m.artist, m.title, cands)}}));
  card.append(actions);
  card.append(cands);
  return card;
}

async function setStatus(h,v,st){
  if(!v && st!=='rejected'){ alert('no video id; type one then Set & approve'); return; }
  await post('/admin/set',{hash:h,video_id:v,status:st}); load();
}

async function suggest(h, artist, title, box){
  box.style.display='block'; box.textContent='searching...';
  const r = await (await post('/admin/suggest',{artist,title})).json();
  box.textContent='';
  if(!r.search_enabled){ box.append(el('div',{cls:'muted',text:'Server-side search unavailable (yt-dlp not installed).'})); return; }
  if(!r.candidates.length){ box.append(el('div',{cls:'muted',text:'No candidates found.'})); return; }
  r.candidates.forEach((c,i)=>{
    const cand = el('div',{cls:'cand'});
    const line = el('div',{cls:'row'});
    if(i===r.recommended) line.append(el('span',{cls:'rec',text:'AI pick: '}));
    line.append(el('span',{text:c.title||''}));
    line.append(el('span',{cls:'muted',text:` ${c.channel||''} · ${c.duration||'?'}s`}));
    cand.append(line);
    const act = el('div',{cls:'row',attrs:{style:'margin-top:6px'}});
    const link = el('a',{text:'preview',attrs:{target:'_blank',rel:'noopener'}});
    link.href = 'https://www.youtube.com/watch?v=' + encodeURIComponent(c.id || '');
    act.append(link);
    act.append(el('button',{cls:'ok',text:'Approve this',
      on:{click:()=>setStatus(h, c.id, 'approved')}}));
    cand.append(act);
    box.append(cand);
  });
  if(!r.llm_enabled) box.append(el('div',{cls:'muted',attrs:{style:'margin-top:6px'},
    text:'(set BACKSTAGEHERO_LLM_KEY for an AI pick)'}));
}

document.getElementById('refresh').addEventListener('click', load);
load();
</script></body></html>"""
