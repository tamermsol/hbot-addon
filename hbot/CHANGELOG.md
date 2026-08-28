# HBot Add-on Changelog

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
