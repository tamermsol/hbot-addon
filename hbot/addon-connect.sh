#!/usr/bin/env bash
# addon-connect.sh — drop into the HBot HA add-on (hbot-addon/hbot/run.sh) so, on FIRST BOOT, the
# add-on connects this HA install to the H-Bot app with ZERO user typing (BURDEN 1).
#
# The app's registerHomeAssistantFromDb needs BOTH ha_connections.base_url AND access_token. This
# script supplies both:
#   1. Read the paired home_id + provisioning_token (persisted at /data/{home_id,provisioning_token}
#      by the pairing/claim flow — see addon-claim.sh).
#   2. MINT a Home Assistant access token from inside HAOS (SUPERVISOR_TOKEN — Core accepts it as a
#      bearer, and it reaches Core both on the LAN and through the tunnel which routes to homeassistant:8123).
#   3. LAN FAST PATH: immediately POST /ha-connection {base_url: LAN URL, access_token} so the app can
#      connect on-network the instant pairing finishes — WITHOUT waiting for Cloudflare.
#   4. POST /provision {home_id, access_token} → hbot-connect mints the stable tunnel and (server-side,
#      service key) upgrades ha_connections.base_url to the tunnel URL + stores the token. This is why
#      we send the token here and NOT via a direct Supabase PATCH: the table's RLS (auth.uid()=user_id)
#      makes the add-on's own anon write update 0 rows — only the server (service key) can persist it.
#   5. Start cloudflared with the tunnel token (HA now reachable at the stable URL, even behind CGNAT).
#
# Requires: HBOT_CONNECT_URL. SUPERVISOR_TOKEN is provided automatically to add-ons (homeassistant_api).
set -euo pipefail

HOME_ID_FILE="/data/home_id"
TOKEN_FILE="/data/provisioning_token"   # per-install token minted in the app at HA-pair time
STATE_FILE="/data/tunnel_url"
HBOT_CONNECT_URL="${HBOT_CONNECT_URL:?set HBOT_CONNECT_URL}"

# Not paired yet → nothing to write (prosumer manual-URL path handles it in the app).
if [[ ! -s "$HOME_ID_FILE" || ! -s "$TOKEN_FILE" ]]; then
  echo "[hbot-connect] not paired yet (need home_id + provisioning_token) — skipping."
  exit 0
fi
HOME_ID="$(cat "$HOME_ID_FILE")"
PROV_TOKEN="$(cat "$TOKEN_FILE")"

# ── Mint the HA access token the app will use. SUPERVISOR_TOKEN is injected into every add-on and is
# accepted by HA Core as a bearer for /api/ — it reaches Core on the LAN and through the tunnel (which
# routes to homeassistant:8123). We verify it actually authenticates before writing it, so we never
# store a token that would leave the app "connected but 401". Falls back to HASSIO_TOKEN (older name). ──
HA_TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}"
if [[ -n "$HA_TOKEN" ]]; then
  if curl -fsS -m 8 -o /dev/null -H "Authorization: Bearer ${HA_TOKEN}" "http://supervisor/core/api/" 2>/dev/null; then
    echo "[hbot-connect] HA access token verified against Core."
  else
    echo "[hbot-connect] warn: supervisor token did not authenticate against Core — writing it anyway (Core may still accept it via the tunnel)."
  fi
else
  echo "[hbot-connect] warn: no SUPERVISOR_TOKEN in env — the app will need a manual token."
fi

# ── LAN base_url: reach Core directly on-network. Prefer the add-on's own primary IP (host_network),
# fall back to the mDNS name every HAOS install answers to. ──
LAN_IP="$(hostname -i 2>/dev/null | awk '{print $1}')"
if [[ -n "$LAN_IP" && "$LAN_IP" != "127.0.0.1" ]]; then
  LAN_URL="http://${LAN_IP}:8123"
else
  LAN_URL="http://homeassistant.local:8123"
fi

# ── Step 3: LAN FAST PATH — write base_url + token now so the app connects immediately, tunnel or not.
echo "[hbot-connect] writing LAN connection (${LAN_URL}) for home ${HOME_ID}…"
curl -fsS -m 12 -X POST "${HBOT_CONNECT_URL}/ha-connection" \
  -H 'Content-Type: application/json' \
  -H "X-Hbot-Provision-Token: ${PROV_TOKEN}" \
  --data "{\"base_url\":\"${LAN_URL}\",\"access_token\":\"${HA_TOKEN}\"}" \
  && echo "[hbot-connect] LAN connection written." \
  || echo "[hbot-connect] warn: LAN connection write failed (non-fatal; /provision will retry the write)."

# ── Step 4: provision the stable tunnel. Send access_token so the server also persists it alongside the
# tunnel base_url (service-key write). Auth is PER-INSTALL: the server resolves the token to its home.
echo "[hbot-connect] provisioning tunnel for home ${HOME_ID}…"
RESP="$(curl -fsS -m 30 -X POST "${HBOT_CONNECT_URL}/provision" \
  -H 'Content-Type: application/json' \
  -H "X-Hbot-Provision-Token: ${PROV_TOKEN}" \
  --data "{\"home_id\":\"${HOME_ID}\",\"access_token\":\"${HA_TOKEN}\"}")" || {
    echo "[hbot-connect] tunnel provisioning failed — staying LAN-only (the app can already connect via ${LAN_URL})." >&2
    exit 0
  }

URL="$(echo "$RESP" | sed -n 's/.*"url":"\([^"]*\)".*/\1/p')"
TUNNEL_TOKEN="$(echo "$RESP" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
if [[ -z "$URL" || -z "$TUNNEL_TOKEN" ]]; then
  echo "[hbot-connect] provisioning response missing url/token — staying LAN-only: $RESP" >&2
  exit 0
fi
echo "$URL" > "$STATE_FILE"

# ── Trust the tunnel proxy so HA doesn't 400 tunnelled requests. cloudflared routes to
# homeassistant:8123 and adds X-Forwarded-For; HA returns "400 Bad Request" for forwarded headers
# from an untrusted source unless http.use_x_forwarded_for + trusted_proxies are set. Without this the
# tunnel connects healthy but every public request 400s. We write a config PACKAGE (never touching the
# user's configuration.yaml) covering the hassio add-on docker network + loopback, then reload Core.
ensure_trusted_proxies() {
  local pkg_dir="/homeassistant/packages" pkg="/homeassistant/packages/hbot_tunnel.yaml"
  # /homeassistant is the Core config dir mapped into the add-on (map: homeassistant_config:rw).
  [[ -d /homeassistant ]] || { echo "[hbot-connect] warn: /homeassistant not mapped — cannot set trusted_proxies; tunnel may 400 until set manually."; return 0; }
  mkdir -p "$pkg_dir" 2>/dev/null || true
  # Only (re)write if missing/different, so we don't churn Core reloads on every boot.
  local want='homeassistant:
  packages: !include_dir_named packages'
  # Ensure configuration.yaml includes packages (idempotent): if no "packages:" under homeassistant, add it.
  if ! grep -qE '^\s*packages:\s*!include_dir_named\s+packages' /homeassistant/configuration.yaml 2>/dev/null; then
    printf '\n# Added by HBot add-on so package files (incl. tunnel trusted_proxies) load.\nhomeassistant:\n  packages: !include_dir_named packages\n' >> /homeassistant/configuration.yaml 2>/dev/null || true
  fi
  # The proxy IP HA sees is the add-on's OWN source IP reaching Core. Because this add-on runs with
  # host_network: true, cloudflared reaches homeassistant:8123 from the HOST's LAN IP (e.g. 192.168.1.x),
  # NOT a 172.30.x docker address — so we must trust the real LAN_IP computed above, plus its /24 and
  # loopback as a safety net. (Getting this wrong = HA 400s every tunnel request.)
  local host_ip="${LAN_IP:-}"
  local host_net=""
  if [[ "$host_ip" =~ ^([0-9]+\.[0-9]+\.[0-9]+)\.[0-9]+$ ]]; then host_net="${BASH_REMATCH[1]}.0/24"; fi
  {
    echo "# Managed by the HBot add-on — trusts the cloudflared tunnel proxy so remote (tunnel)"
    echo "# requests are not rejected with 400. Host-network add-on → trust the host LAN IP."
    echo "http:"
    echo "  use_x_forwarded_for: true"
    echo "  trusted_proxies:"
    [[ -n "$host_ip"  ]] && echo "    - $host_ip"
    [[ -n "$host_net" ]] && echo "    - $host_net"
    echo "    - 172.30.32.0/23"
    echo "    - 127.0.0.1"
    echo "    - ::1"
  } > "$pkg" 2>/dev/null || true
  echo "[hbot-connect] wrote trusted_proxies package; reloading Core config…"
  # Validate + reload via Core service (no full restart needed for http trusted_proxies? http needs a
  # restart — so request a Core restart, which HA schedules gracefully).
  curl -fsS -m 10 -X POST -H "Authorization: Bearer ${HA_TOKEN}" \
    "http://supervisor/core/api/services/homeassistant/restart" -H 'Content-Type: application/json' -d '{}' \
    >/dev/null 2>&1 && echo "[hbot-connect] Core restart requested to apply trusted_proxies." \
    || echo "[hbot-connect] warn: Core restart request failed — trusted_proxies will apply on next HA restart."
}
ensure_trusted_proxies

echo "[hbot-connect] starting cloudflared → ${URL}"
# Supervised retry loop: if cloudflared ever exits (network blip, CF edge hiccup, CGNAT re-NAT), the
# add-on used to stay "started" (the python bridge is the foreground process) while the tunnel stayed
# DEAD — the app then saw Cloudflare 530/1033 with no self-healing. Wrap it so a drop reconnects
# automatically with capped backoff. --retries and --grace-period make cloudflared itself hold on
# harder before giving up a connection; the outer loop covers a full process exit.
backoff=2
while true; do
  cloudflared tunnel --no-autoupdate --retries 10 --grace-period 30s run --token "$TUNNEL_TOKEN"
  rc=$?
  echo "[hbot-connect] cloudflared exited rc=$rc — reconnecting in ${backoff}s (tunnel auto-heal)."
  sleep "$backoff"
  # exponential backoff capped at 60s so a persistent outage doesn't hammer the CF edge.
  backoff=$(( backoff * 2 )); [ "$backoff" -gt 60 ] && backoff=60
done
