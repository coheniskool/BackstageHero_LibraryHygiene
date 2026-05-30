# Deploying the BackstageHero resolver (your Unraid + existing tunnel)

A tiny metadata service: it tells clients which YouTube video a chart maps to and
how far into it the song starts. Video bytes never pass through it, so it is cheap
to host and most traffic is served straight from Cloudflare's edge cache.

This host (Tower) already runs everything we need:

- `Unraid-Cloudflared-Tunnel` - a token-managed Cloudflare tunnel (routing is in
  the Cloudflare dashboard, not a local file). This is the **external** path, and
  **we reuse it** - no second cloudflared.
- Domain `jimmyproton.co.uk` on Cloudflare.

(Nginx Proxy Manager handles **LAN-only** access to your services on local DNS;
it is not part of the external path and is not involved here. The resolver's
users are out on the internet, so it is served purely via the tunnel.)

So deploying is just: run one container, add one public-hostname entry to the
tunnel, and (optionally) a cache rule and an Access policy.

```
client ──HTTPS──> Cloudflare edge ──existing tunnel──> cloudflared
                       │                                      │
                  caches /resolve                 http://192.168.10.20:8099   (Unraid IP : published port)
                                                              │
                                                       resolver :8080 ──> SQLite (/mnt/user/appdata/backstagehero)
```

This is exactly how you already point the tunnel at Vaultwarden: a bridge
container that publishes a host port, with the tunnel hostname set to the **Unraid
IP and that port**. Vaultwarden publishes `4743` and you route it to
`192.168.10.20:4743`; the resolver publishes **8099** (free on Tower), routed to
`192.168.10.20:8099`. No macvlan, no NPM, no certificates to manage.

## 1. Run the resolver container

From the repo's `resolver/` folder on the host (or as a Portainer stack):

```bash
cp .env.example .env
#   edit .env: BACKSTAGEHERO_ADMIN_TOKEN, BACKSTAGEHERO_PUBLIC_URL
docker compose up -d --build
```

Equivalent plain `docker run` (Unraid "Add Container" advanced, or a User Script):

```bash
docker build -t backstagehero-resolver ./resolver
docker run -d --name backstagehero-resolver --restart unless-stopped \
  -p 8099:8080 \
  -v /mnt/user/appdata/backstagehero:/data \
  -e BACKSTAGEHERO_DB=/data/resolver.sqlite3 \
  -e BACKSTAGEHERO_ADMIN_TOKEN=<long-random-string> \
  -e BACKSTAGEHERO_PUBLIC_URL=https://backstage.jimmyproton.co.uk \
  backstagehero-resolver
```

Verify locally on the host:

```bash
curl http://localhost:8099/healthz                       # {"ok":true}
curl "http://localhost:8099/resolve?hash=ch1:test"       # {"status":"none"}
```

Data lives in `/mnt/user/appdata/backstagehero` and survives rebuilds.

## 2. Add the public hostname to the existing tunnel

Cloudflare **Zero Trust → Networks → Tunnels →** open your tunnel **→ Public
Hostname → Add a public hostname**:

- **Subdomain:** `backstage`  **Domain:** `jimmyproton.co.uk`
- **Type:** `HTTP`
- **URL:** `192.168.10.20:8099`   (the Unraid IP and the published port - same as
  your Vaultwarden entry)

That is the only routing change. TLS is terminated at Cloudflare's edge; the
internal hop is plain HTTP, so no certificate to manage.

> Optional: if you ever want `backstage.jimmyproton.co.uk` to resolve on the LAN
> too (via NPM, like your other local domains), add an NPM proxy host pointing at
> `http://192.168.10.20:8099`. That is purely for LAN access and is independent of
> the external tunnel route above.

Check from anywhere:

```bash
curl https://backstage.jimmyproton.co.uk/healthz
```

## 3. Cache rule (protects your 20 Mbps uplink)

Cloudflare dashboard → `jimmyproton.co.uk` → **Caching → Cache Rules → Create**:

- **When:** `URI Path` `starts with` `/resolve`
- **Then:** *Eligible for cache* → **Edge TTL: Respect origin** (the service sends
  `max-age=21600` for hits, `60` for misses) → **Browser TTL: Respect origin**.

The default cache key includes the query string, so each `?hash=` is cached
separately. `/report`, `/admin`, `/healthz` stay uncached (POST is never cached,
and admin must not be).

Optional - instant updates: set `CF_API_TOKEN` (token with **Zone → Cache Purge**
on this zone) and `CF_ZONE_ID` in `.env`. A curator change then purges just that
one `/resolve` URL, so it goes live immediately instead of after the TTL.

## 4. Protect the dashboard

Zero Trust → **Access → Applications → Add → Self-hosted**:

- **Domain:** `backstage.jimmyproton.co.uk`  **Path:** `/admin`
- **Policy:** allow your email (one-time code / Google / etc.).

`BACKSTAGEHERO_ADMIN_TOKEN` is a second layer: the dashboard asks for it once and
sends it on every API call, so the curator endpoints stay protected even if Access
is ever misconfigured.

### Do NOT bot-challenge the resolver

`/resolve` and `/report` are called by the BackstageHero app - a non-browser HTTP
client. Cloudflare **Bot Fight Mode / Super Bot Fight Mode** (and "Under Attack"
mode / a zone-wide Managed Challenge) will flag it as automated and challenge it;
the app can't solve a JS challenge, so those calls would be blocked. They fail
safe - the client just falls back to a normal YouTube search - but you lose the
whole point of the resolver. The `/admin` dashboard is unaffected (real browser,
behind Access).

- Not running bot protection on this zone? Nothing to do.
- **Super Bot Fight Mode (Pro+):** add a WAF **Skip/exception** for
  `hostname eq backstage.jimmyproton.co.uk` (optionally also matching
  `User-Agent: BackstageHero-Client`, which the app sends). Protection stays on
  for your other services.
- **Bot Fight Mode (Free):** it is zone-wide and cannot be scoped by host/path.
  If you need it elsewhere, keep the resolver on a domain/zone without it.

## 5. Point clients at it

Before building the exe, set `RESOLVER_BASE` in `resolver_client.py` to
`https://backstage.jimmyproton.co.uk` (or have users set the env var
`BACKSTAGEHERO_RESOLVER`). With it unset, the client behaves exactly as it did
before the resolver existed.

## 6. Curate

Open `https://backstage.jimmyproton.co.uk/admin`:

- The queue lists charts with no confirmed video yet.
- **Approve/Reject** a community-proposed mapping, paste a video id and **Set &
  approve**, or hit **Suggest** to search YouTube (and, with
  `BACKSTAGEHERO_LLM_KEY` set, see an AI pick of the likely official video).
- A locked curator decision overrides the vote count and reaches every user with
  that chart on their next run.

Charts auto-approve once `QUORUM` (default 3) different users report the same
mapping, so popular songs need no curation - you only spend time on the long tail.

## Updating the service later

```bash
git pull
docker compose up -d --build       # data volume is untouched
```
