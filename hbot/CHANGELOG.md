# HBot Add-on Changelog

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
