#!/usr/bin/env bash
# addon-claim.sh — FIRST-BOOT zero-typing pairing for the HBot HA add-on.
#
# The client installs this add-on on their own Home Assistant. They have NOT typed a token. This
# script announces the add-on to hbot-connect as a PENDING claim with a short human code, SHOWS that
# code in the add-on log, then polls until the H-Bot app (which owns the home's provisioning_token)
# APPROVES it. On approval hbot-connect hands back the provisioning_token, which we persist so the
# existing addon-connect.sh opens the Cloudflare tunnel exactly as before.
#
# Contract (ha-integration/hbot-connect/server.js):
#   POST /claim/register {code, ha_id}          -> {status:"pending"}
#   POST /claim/status   {code, ha_id}          -> {status:"pending"} | {status:"approved", provisioning_token, home_id}
#
# Idempotent: once /data/provisioning_token + /data/home_id exist we do nothing (already paired).
set -euo pipefail

HOME_ID_FILE="/data/home_id"
TOKEN_FILE="/data/provisioning_token"
HAID_FILE="/data/ha_id"
CODE_FILE="/data/claim_code"
HBOT_CONNECT_URL="${HBOT_CONNECT_URL:?set HBOT_CONNECT_URL}"

log() { echo "[hbot-claim] $*"; }

# Already paired (manual token, or a prior successful claim) → nothing to do.
if [[ -s "$TOKEN_FILE" && -s "$HOME_ID_FILE" ]]; then
  log "already paired (home $(cat "$HOME_ID_FILE")) — skipping claim."
  exit 0
fi

# Stable per-install identity. hbot-connect requires (code, ha_id) to match on /claim/status, so a
# RESTART MUST reuse the SAME code+ha_id — otherwise the add-on polls a code the app never approved and
# pairing deadlocks (the "new code every restart" bug). We derive BOTH deterministically from the HA
# machine-id so they're identical across restarts even if /data was wiped; /data is just a cache.
#
# machine-id is stable for the life of the HAOS install. Fall back to a persisted random id only if it
# is somehow unavailable (then /data must persist for stability — it normally does for add-ons).
MID="$(cat /etc/machine-id 2>/dev/null || cat /data/.mid 2>/dev/null || true)"
if [[ -z "$MID" ]]; then
  MID="$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  printf '%s' "$MID" > /data/.mid 2>/dev/null || true
fi

# ha_id = deterministic from machine-id (persist a copy for readability/debug).
if [[ -s "$HAID_FILE" ]]; then
  HA_ID="$(cat "$HAID_FILE")"
else
  HA_ID="ha-$(printf '%s' "$MID" | sha256sum | cut -c1-24)"
  printf '%s' "$HA_ID" > "$HAID_FILE" 2>/dev/null || true
fi

# code = deterministic from machine-id too, so the SAME code regenerates after a restart even if the
# /data cache is gone. Still unguessable (derived from a per-install secret via SHA-256), and matches
# the app-visible format HBOT + 8 uppercase hex. A persisted CODE_FILE takes precedence (if the app is
# mid-pairing on a code the user already sees, never change it).
if [[ -s "$CODE_FILE" ]]; then
  CODE="$(cat "$CODE_FILE")"
else
  SUFFIX="$(printf 'hbot-claim:%s' "$MID" | sha256sum | tr 'a-f' 'A-F' | head -c 8)"
  CODE="HBOT${SUFFIX}"
  printf '%s' "$CODE" > "$CODE_FILE" 2>/dev/null || true
fi

# The base_url the app would independently DISCOVER this HA at (default: HA's in-network hostname).
# hbot-connect uses it to safely auto-bind a fresh UNBOUND claim ONLY when the host matches the URL the
# app discovered (see server.js pickUnboundAutoBind) — this is what makes the true 0-typing flow work
# for a brand-new client that never told the add-on its home. Overridable for non-default HA ports.
HA_DISCOVER_URL="${HA_DISCOVER_URL:-http://homeassistant:8123}"

# Register (idempotent per code+ha_id). Non-fatal — retried each boot until the app approves. We declare
# base_url so the server's host-match gate can auto-bind the single discovered HA with zero typing.
register() {
  curl -fsS -X POST "${HBOT_CONNECT_URL}/claim/register" \
    -H 'Content-Type: application/json' \
    --data "{\"code\":\"${CODE}\",\"ha_id\":\"${HA_ID}\",\"base_url\":\"${HA_DISCOVER_URL}\"}" >/dev/null 2>&1 || return 1
}

if ! register; then
  log "could not reach ${HBOT_CONNECT_URL} to register — will retry next boot; LAN-only for now."
  exit 0
fi

# Human display code: HBOT12AB34CD → HBOT-12AB (short prefix is what the user reads/types in the app).
SHOW="HBOT-${CODE:4:4}"
log "======================================================================"
log " PAIR THIS HOME ASSISTANT"
log " In the H-Bot app: Settings -> Home Assistant -> Open my Home Assistant."
log " If it doesn't pair automatically, choose \"Pair with a code\" and enter:"
log "     ${SHOW}   (full code: ${CODE})"
log "======================================================================"

# Poll /claim/status until approved (~15 min TTL server-side). Re-register periodically so a server
# restart mid-pairing doesn't strand us. Fully non-fatal: on timeout we exit 0 and stay LAN-only.
DEADLINE=$(( $(date +%s) + 15*60 ))
while [[ $(date +%s) -lt $DEADLINE ]]; do
  RESP="$(curl -fsS -X POST "${HBOT_CONNECT_URL}/claim/status" \
    -H 'Content-Type: application/json' \
    --data "{\"code\":\"${CODE}\",\"ha_id\":\"${HA_ID}\"}" 2>/dev/null || echo '')"
  STATUS="$(echo "$RESP" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')"
  if [[ "$STATUS" == "approved" ]]; then
    TOKEN="$(echo "$RESP" | sed -n 's/.*"provisioning_token":"\([^"]*\)".*/\1/p')"
    HID="$(echo "$RESP" | sed -n 's/.*"home_id":"\([^"]*\)".*/\1/p')"
    if [[ -n "$TOKEN" && -n "$HID" ]]; then
      mkdir -p /data
      printf '%s' "$TOKEN" > "$TOKEN_FILE"
      printf '%s' "$HID" > "$HOME_ID_FILE"
      rm -f "$CODE_FILE"
      log "paired to home ${HID}. Remote access will come up shortly."
      exit 0
    fi
  fi
  if [[ "$STATUS" == "unknown" ]]; then register || true; fi   # claim expired/swept → re-announce
  sleep 5
done

log "pairing not completed within 15 min — staying LAN-only. Restart the add-on to try again."
exit 0
