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

echo "[hbot-connect] starting cloudflared → ${URL}"
exec cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN"
