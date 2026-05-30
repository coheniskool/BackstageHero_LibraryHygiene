# BackstageHero resolver service (FastAPI).
# GET  /resolve?hash=  -> approved mapping or {"status":"none"}
# POST /report         -> record a vote
# GET  /healthz        -> liveness
# /admin/*             -> curator endpoints (Cloudflare Access + bearer token)

import json
import os
import urllib.request
import urllib.parse

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import db
import suggest as suggest_mod

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
    hash: str
    video_id: str
    start_ms: int
    client_id: str
    confidence: float = 1.0
    artist: str = ''
    title: str = ''


@app.post('/report')
def report(body: ReportIn, conn=Depends(_conn)):
    if not body.hash or not body.video_id or not body.client_id:
        raise HTTPException(status_code=400, detail='hash, video_id, client_id required')
    status = db.record_vote(
        conn, body.hash, body.video_id, body.start_ms, body.client_id,
        body.confidence, body.artist, body.title)
    return {'status': status}


@app.get('/admin/pending', dependencies=[Depends(_require_admin)])
def admin_pending(conn=Depends(_conn)):
    return {'pending': db.list_pending(conn)}


@app.get('/admin/stats', dependencies=[Depends(_require_admin)])
def admin_stats(conn=Depends(_conn)):
    return db.stats(conn)


class SetIn(BaseModel):
    hash: str
    video_id: str
    status: str          # 'approved' | 'rejected' | 'pending'


@app.post('/admin/set', dependencies=[Depends(_require_admin)])
def admin_set(body: SetIn, conn=Depends(_conn)):
    if body.status not in ('approved', 'rejected', 'pending'):
        raise HTTPException(status_code=400, detail='bad status')
    db.set_status(conn, body.hash, body.video_id, body.status, lock=True)
    _purge_resolve(body.hash)
    return {'ok': True}


class OffsetIn(BaseModel):
    hash: str
    video_id: str
    start_ms: int


@app.post('/admin/offset', dependencies=[Depends(_require_admin)])
def admin_offset(body: OffsetIn, conn=Depends(_conn)):
    db.set_start_ms(conn, body.hash, body.video_id, body.start_ms)
    _purge_resolve(body.hash)
    return {'ok': True}


class SuggestIn(BaseModel):
    artist: str = ''
    title: str = ''


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
 <button onclick="load()">Refresh</button>
</header>
<main id="list">loading...</main>
<script>
let TOKEN = sessionStorage.getItem('bh_token') || '';
function hdr(j){ return j?{'Content-Type':'application/json'}:{}; }
async function api(path, opts){
  // Inject the current token freshly on every call (including retries) so a
  // token entered at the prompt actually reaches the next request.
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
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

async function load(){
  const s = await (await api('/admin/stats',{headers:hdr(false)})).json();
  document.getElementById('stats').innerHTML =
    `charts <b>${s.charts}</b> &middot; approved <b>${s.approved}</b> &middot; pending <b>${s.pending}</b> &middot; votes <b>${s.votes}</b>`;
  const data = await (await api('/admin/pending',{headers:hdr(false)})).json();
  const list = document.getElementById('list');
  if(!data.pending.length){ list.innerHTML='<div class="card">Nothing pending. All caught up.</div>'; return; }
  list.innerHTML = data.pending.map(rowHtml).join('');
}

function rowHtml(m){
  const name = (esc(m.artist)? esc(m.artist)+' - ':'') + (esc(m.title)||'(unknown title)');
  const cid = 'c_'+Math.random().toString(36).slice(2);
  return `<div class="card" id="${cid}">
    <div class="row">
      <span class="song">${name}</span>
      <span class="muted">votes ${m.votes} &middot; status ${m.status} &middot; ${m.video_id?('vid '+esc(m.video_id)):'no video yet'}</span>
    </div>
    <div class="row" style="margin-top:8px">
      <button class="ok" onclick="setStatus('${m.chart_hash}','${esc(m.video_id)}','approved')">Approve</button>
      <button class="no" onclick="setStatus('${m.chart_hash}','${esc(m.video_id)}','rejected')">Reject</button>
      <input id="${cid}_vid" placeholder="set video id" size="14">
      <button onclick="setVid('${m.chart_hash}','${cid}')">Set &amp; approve</button>
      <button onclick="suggest('${m.chart_hash}','${cid}','${esc(m.artist)}','${esc(m.title)}')">Suggest</button>
    </div>
    <div class="cands" id="${cid}_cands"></div></div>`;
}

async function setStatus(h,v,st){ if(!v&&st!=='rejected'){alert('no video id; use Set & approve');return;} await post('/admin/set',{hash:h,video_id:v,status:st}); load(); }
async function setVid(h,cid){ const v=document.getElementById(cid+'_vid').value.trim(); if(!v)return; await post('/admin/set',{hash:h,video_id:v,status:'approved'}); load(); }

async function suggest(h,cid,artist,title){
  const box=document.getElementById(cid+'_cands'); box.style.display='block'; box.innerHTML='searching...';
  const r=await (await post('/admin/suggest',{artist,title})).json();
  if(!r.search_enabled){ box.innerHTML='<div class="muted">Server-side search unavailable (yt-dlp not installed).</div>'; return; }
  if(!r.candidates.length){ box.innerHTML='<div class="muted">No candidates found.</div>'; return; }
  box.innerHTML = r.candidates.map((c,i)=>`<div class="cand">
     <div class="row"><span>${i===r.recommended?'<span class="rec">AI pick &rarr; </span>':''}${esc(c.title)}</span>
       <span class="muted">${esc(c.channel)} &middot; ${c.duration||'?'}s</span></div>
     <div class="row" style="margin-top:6px">
       <a href="${c.url}" target="_blank">preview</a>
       <button class="ok" onclick="post('/admin/set',{hash:'${h}',video_id:'${c.id}',status:'approved'}).then(load)">Approve this</button>
     </div></div>`).join('') +
     (r.llm_enabled?'':'<div class="muted" style="margin-top:6px">(set BACKSTAGEHERO_LLM_KEY for an AI pick)</div>');
}
load();
</script></body></html>"""
