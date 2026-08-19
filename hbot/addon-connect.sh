#!/usr/bin/env bash
# addon-connect.sh — drop into the HBot HA add-on (hbot-addon/hbot/run.sh) so, on FIRST BOOT, the
# add-on obtains a stable public URL for this HA install with ZERO user typing (BURDEN 1).
#
# Flow:
#   1. Read the paired home_id (written when the user paired this HA in the app — same anon flow the
#      wall panel uses, memory panel-account-rebind). Persisted at /data/home_id.
#   2. POST /provision {home_id} to hbot-connect → { url, token }.
#   3. Start cloudflared with the token (HA now reachable at the stable URL, even behind CGNAT).
#   4. Write the URL back to Supabase ha_connections.base_url for this home, so the app reads it and
#      never asks the user for a URL. Token is stored in HA add-on options (never sent to the app).
#
# Requires (add-on options): HBOT_CONNECT_URL, SUPABASE_URL, SUPABASE_ANON_KEY.
set -euo pipefail

HOME_ID_FILE="/data/home_id"
TOKEN_FILE="/data/provisioning_token"   # per-install token minted in the app at HA-pair time
STATE_FILE="/data/tunnel_url"
HBOT_CONNECT_URL="${HBOT_CONNECT_URL:?set HBOT_CONNECT_URL}"

# Idempotent: if we already provisioned + still have cloudflared token, just (re)start the tunnel.
if [[ ! -f "$HOME_ID_FILE" || ! -f "$TOKEN_FILE" ]]; then
  echo "[hbot-connect] not paired yet (need home_id + provisioning_token) — skipping zero-config tunnel (prosumer manual-URL path)."
  exit 0
fi
HOME_ID="$(cat "$HOME_ID_FILE")"
PROV_TOKEN="$(cat "$TOKEN_FILE")"

echo "[hbot-connect] provisioning tunnel for home ${HOME_ID}…"
# Auth is PER-INSTALL: the server resolves the token to its home and provisions only that home. A
# compromised add-on can never provision another customer's home. Rotating the token revokes us.
RESP="$(curl -fsS -X POST "${HBOT_CONNECT_URL}/provision" \
  -H 'Content-Type: application/json' \
  -H "X-Hbot-Provision-Token: ${PROV_TOKEN}" \
  --data "{\"home_id\":\"${HOME_ID}\"}")"

URL="$(echo "$RESP" | sed -n 's/.*"url":"\([^"]*\)".*/\1/p')"
TOKEN="$(echo "$RESP" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
if [[ -z "$URL" || -z "$TOKEN" ]]; then
  echo "[hbot-connect] provisioning failed: $RESP" >&2
  exit 1
fi
echo "$URL" > "$STATE_FILE"

# Write the URL back to Supabase ha_connections for this home (PostgREST upsert with anon key; RLS
# permits the paired home to update its own row). The app's loadHaConnection() reads base_url — done.
if [[ -n "${SUPABASE_URL:-}" && -n "${SUPABASE_ANON_KEY:-}" ]]; then
  curl -fsS -X PATCH "${SUPABASE_URL}/rest/v1/ha_connections?home_id=eq.${HOME_ID}" \
    -H "apikey: ${SUPABASE_ANON_KEY}" \
    -H "Authorization: Bearer ${SUPABASE_ANON_KEY}" \
    -H 'Content-Type: application/json' \
    -H 'Prefer: return=minimal' \
    --data "{\"base_url\":\"${URL}\"}" || echo "[hbot-connect] warn: URL write-back failed (non-fatal)"
fi

echo "[hbot-connect] starting cloudflared → ${URL}"
exec cloudflared tunnel --no-autoupdate run --token "$TOKEN"
