# HBot Add-on Changelog

## 1.4.26
- **Fixes the fresh-box "paired but nothing happens / app stays Looking for your HA" dead-end.**
  Root cause: `addon-connect.sh` had regressed to a stale build that POSTed `/provision` with ONLY
  `{home_id}` (no HA token) and then tried a Supabase **PATCH** write-back with the anon key. On a
  brand-new home no `ha_connections` row exists, so the PATCH updated **zero rows** and no token was
  ever minted — hbot-connect's server-side write refuses a token-less `base_url` (it would look
  "paired but stuck"), so `ha_connections` stayed EMPTY and the app could never connect (claim binds
  in ~3s, but the connection row never lands). DB-proven on a fresh home (approved claim, `mint_error`
  NULL, `ha_connections` empty).
- **Fix:** `addon-connect.sh` now MINTS a real Home Assistant long-lived access token (LLAT, a JWT)
  from inside HAOS via `mint_llat.py` (Core WebSocket `auth/long_lived_access_token`), VERIFIES it
  against Core's DIRECT `/api/` (must return 200 — the exact path the app uses, where a
  SUPERVISOR_TOKEN would 401), then:
    - POSTs `/ha-connection {base_url: LAN URL, access_token}` immediately (LAN fast path — the app
      connects on-network the instant pairing finishes, before Cloudflare), and
    - POSTs `/provision {home_id, access_token}` so hbot-connect (service key) UPSERTS a complete
      `ha_connections` row (INSERTs it if absent) with `base_url` = the stable tunnel URL **and** the
      durable JWT token. No supervisor-token is ever written as the app's token.
  This restores the same connection path the working homes already use (183-char `eyJ…` JWT).

# HBot Add-on Changelog

## 1.4.25
- **Permanent fix for the fresh-box pairing dead-end (registered claim never binds → `ha_connections`
  stays empty → app shows "install the add-on" forever).** Root cause: Home Assistant registers the
  `/local` static route ONLY at Core startup and ONLY if `<config>/www/` exists then. On a brand-new
  install `www/` doesn't exist at boot, so `/local` is never mounted; the add-on writes the pairing
  nonce to `www/hbot_pair_nonce.txt` AFTER boot, so `GET /local/hbot_pair_nonce.txt` returns a plain 404
  indefinitely. The app can't read the nonce → can't echo it → hbot-connect's proof-of-LAN auto-bind
  (`sha256(echo) == nonce_hash`) never fires → the claim stays `pending`. Three independent fixes make a
  registered claim ALWAYS bindable:
  - `addon-claim.sh` now, after publishing the nonce, forces HA to expose `/local` this boot: it POSTs
    `homeassistant.reload_core_config` (via the add-on's `SUPERVISOR_TOKEN`) to re-register the static
    mount, verifies `GET …/local/hbot_pair_nonce.txt` returns 200 with the nonce, and if it's still 404
    falls back to a **guarded one-time** Core `restart` (a `/data` flag prevents any restart loop; the
    flag is cleared once pairing completes).
  - The bridge now serves the SAME nonce from its own already-running proxy on **:8098 at
    `/hbot_pair_nonce`**, INDEPENDENT of HA's `/local` mount, so the app can bind even when `/local` is
    dead. The app-side `readHaPairNonce` tries `/local` first, then this `:8098` fallback.
  - `run.sh` now creates `<config>/www/` at startup BEFORE Core scans it, so future fresh installs
    register `/local` natively on the next Core start.
  - Net effect: after the operator installs+Starts the add-on ONCE, pairing completes with zero further
    taps — the nonce is reachable at `/local` (post reload) OR at `:8098/hbot_pair_nonce` (proxy), the
    app echoes it, hbot-connect binds, and the `ha_connections` row is written.

## 1.4.24
- **Tunnel supervisor now uses the LOCAL edge-liveness probe as its primary trigger, PLUS the public
  probe as corroboration.** v1.4.22 switched to a public-only probe because in one failure mode
  cloudflared's `/ready` reported 200 while the public URL was dead. But the public probe alone is slow
  and noisy (CGNAT/DNS/CF-edge jitter can 530 a healthy box) and a 530 has causes other than a hung
  connector. v1.4.24 launches cloudflared with `--metrics 127.0.0.1:36429` and probes `GET /ready`
  locally: HTTP 200 = edge registered, **HTTP 503 = process alive but NO edge connection** — the exact
  dead-edge-but-alive failure the process-exit loop misses, now caught in milliseconds without touching
  the public URL (older builds without `/ready` fall back to scraping the `ha_connections` gauge from
  `/metrics`). A cycle is UNHEALTHY when the local probe says edge-not-registered **OR** the public
  `${tunnel}/api/` returns 530 / `error code: 1033`; on 2 consecutive unhealthy cycles the supervisor
  force-kills (`-9`) cloudflared and relaunches it with the same token so the edge re-registers within
  seconds. Every healthy↔unhealthy transition and every respawn is logged with a UTC timestamp. This
  combines the strengths of the v1.4.20 local probe and the v1.4.22 public probe (union of both), so
  neither a `/ready`-lies-healthy case nor a slow-public case can hide a dead tunnel.


## 1.4.22
- **Tunnel watchdog now probes the PUBLIC URL end-to-end.** The previous watchdog checked only
  cloudflared's LOCAL `/ready` + `/metrics`, which reflect the connector's own view. The recurring
  Cloudflare 530/1033 outage is the origin de-registering at the CF edge while the connector process
  stays alive (and can even report `/ready`=200) — so the local signals looked healthy while the
  customer's public URL was dead. The watchdog now GETs `${tunnel}/api/` over the internet every 30s;
  a real HA answer (401/403/200) = healthy origin, and 530/1033/502/000/timeout for 2 consecutive
  probes force-restarts cloudflared so the origin re-registers within seconds. Every detection and
  restart is logged with a UTC timestamp.

## 1.4.21
- Camera reliability: proxy retry + cached snapshots + synthesized MJPEG stream.

## 1.4.20
- Active health-watchdog self-heals dead-but-alive CF tunnel via local /ready + /metrics (1033).

## 1.4.19
- Core reverse-proxy on :8098 injects SUPERVISOR_TOKEN so the app reaches HA through the add-on
  (retires the mint-LLAT path). CF tunnel origin repointed to the proxy server-side.
