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

# Stable per-install identity. hbot-connect requires (code, ha_id) to match on /claim/status, so an
# eavesdropper who saw the code alone still can't collect the token. Reuse across reboots.
if [[ -s "$HAID_FILE" ]]; then
  HA_ID="$(cat "$HAID_FILE")"
else
  HA_ID="ha-$( (cat /etc/machine-id 2>/dev/null || head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n') | head -c 24)-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  printf '%s' "$HA_ID" > "$HAID_FILE"
fi

# Short, human-readable code shown on the HA screen (also a bearer secret → made unguessable by the
# random suffix). Reuse a persisted code so a restart mid-pairing keeps the same code the user sees.
if [[ -s "$CODE_FILE" ]]; then
  CODE="$(cat "$CODE_FILE")"
else
  SUFFIX="$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n' | tr 'a-f' 'A-F' | head -c 8)"
  CODE="HBOT${SUFFIX}"
  printf '%s' "$CODE" > "$CODE_FILE"
fi

# Register (idempotent per code+ha_id). Non-fatal — retried each boot until the app approves.
register() {
  curl -fsS -X POST "${HBOT_CONNECT_URL}/claim/register" \
    -H 'Content-Type: application/json' \
    --data "{\"code\":\"${CODE}\",\"ha_id\":\"${HA_ID}\"}" >/dev/null 2>&1 || return 1
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
