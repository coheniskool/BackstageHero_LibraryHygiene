# Deploying the BackstageHero resolver

A tiny metadata service: it tells clients which YouTube video a chart maps to and
how far into it the song starts. Video bytes never pass through it, so it is cheap
to host and most traffic is served straight from a CDN/edge cache.

It is a single container that listens on port `8080`. You put it behind whatever
reverse proxy / tunnel you already use for HTTPS. Below uses Cloudflare as an
example, but anything that can terminate TLS and forward to an HTTP origin works
(Caddy, nginx, Traefik, a Cloudflare Tunnel, Tailscale Funnel, etc).

```
client ──HTTPS──> your edge / proxy ──HTTP──> resolver :8080 ──> SQLite (data volume)
                        │
                  caches /resolve
```

## 1. Run the resolver container

From the `resolver/` folder:

```bash
cp .env.example .env
#   edit .env: set BACKSTAGEHERO_ADMIN_TOKEN (a long random string) and
#              BACKSTAGEHERO_PUBLIC_URL (the public https URL you'll serve it on)
docker compose up -d --build
```

Equivalent plain `docker run`:

```bash
docker build -t backstagehero-resolver ./resolver
docker run -d --name backstagehero-resolver --restart unless-stopped \
  -p 8099:8080 \
  -v backstagehero-data:/data \
  -e BACKSTAGEHERO_DB=/data/resolver.sqlite3 \
  -e BACKSTAGEHERO_ADMIN_TOKEN="<long-random-string>" \
  -e BACKSTAGEHERO_PUBLIC_URL="https://resolver.example.com" \
  backstagehero-resolver
```

`-p 8099:8080` publishes the container's port 8080 on host port 8099, pick any
free host port and point your proxy at it. Verify locally:

```bash
curl http://localhost:8099/healthz                  # {"ok":true}
curl "http://localhost:8099/resolve?hash=ch1:test"  # {"status":"none"}
```

Data lives in the `/data` volume and survives rebuilds.

## 2. Put it behind HTTPS

Route a public hostname (e.g. `resolver.example.com`) to the host/port you
published above, with TLS terminated at your proxy/edge. The internal hop can be
plain HTTP.

```bash
curl https://resolver.example.com/healthz
```

## 3. Cache `/resolve` (optional but recommended)

`/resolve` answers are cacheable, the service sends `Cache-Control: max-age=21600`
for hits and `max-age=60` for misses. Add a cache rule on your edge for paths that
start with `/resolve`; keep the query string in the cache key (each `?hash=` is a
distinct entry). `/report`, `/admin`, and `/healthz` must stay uncached.

If your edge is Cloudflare and you set `CF_API_TOKEN` (scoped to **Zone > Cache
Purge**) and `CF_ZONE_ID` in `.env`, a curator change purges just that one
`/resolve` URL so it goes live immediately instead of after the TTL.

## 4. Protect the dashboard

`/admin` is gated two ways and you should use both:

- **An identity layer in front** (Cloudflare Access, an oauth2-proxy, basic auth,
  a VPN-only route, etc) so only you can reach `/admin`.
- **`BACKSTAGEHERO_ADMIN_TOKEN`**, the dashboard asks for it once and sends it on
  every API call, so the curator endpoints stay protected even if the front layer
  is ever misconfigured.

### Don't bot-challenge the API

`/resolve` and `/report` are called by the app, a non-browser HTTP client. A
JS/Managed Challenge (e.g. Cloudflare Bot Fight Mode / "Under Attack") will block
it, the app can't solve a JS challenge. The calls fail safe (the client just
falls back to a normal YouTube search), but you lose the point of the resolver.
If you run bot protection, add a skip/exception for the resolver hostname
(optionally also matching `User-Agent: BackstageHero-Client`, which the app
sends). `/admin` is a real browser and is unaffected.

## 5. Point clients at it

Before building the exe, set `RESOLVER_BASE` in `resolver_client.py` to your
public URL (or have users set the `BACKSTAGEHERO_RESOLVER` env var). With it unset,
the client behaves exactly as it did before the resolver existed.

## 6. Curate

Open `https://resolver.example.com/admin`:

- The queue lists charts with no confirmed video yet.
- **Approve/Reject** a community-proposed mapping, paste a video id and **Set &
  approve**, or hit **Suggest** to search YouTube (and, with
  `BACKSTAGEHERO_LLM_KEY` set, see an AI pick of the likely official video).
- A locked curator decision overrides the vote count and reaches every user with
  that chart on their next run.

Charts auto-approve once `QUORUM` (default 3) different sources report the same
mapping, so popular songs need no curation, you only spend time on the long tail.

## Updating the service later

```bash
git pull
docker compose up -d --build       # data volume is untouched
```
