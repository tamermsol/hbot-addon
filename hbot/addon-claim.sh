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

# ── SELF-HEAL one-shot (v1.4.17) ──────────────────────────────────────────────────────────────────────
# The approval may have ALREADY happened server-side (e.g. the app approved while a PRIOR poll subshell was
# torn down by an add-on UPDATE/restart — which is exactly what stranded the operator: claim approved, token
# ready at /claim/status, but the files were never written so addon-connect.sh never ran). BEFORE we spend
# 15 min in the register+poll loop, ask the server ONCE with our stable (code, ha_id): if it already says
# approved, persist the files and exit 0 immediately (paired). This makes a single restart complete pairing.
persist_if_approved() {
  local resp status token hid
  resp="$(curl -fsS -X POST "${HBOT_CONNECT_URL}/claim/status" \
    -H 'Content-Type: application/json' \
    --data "{\"code\":\"${CODE}\",\"ha_id\":\"${HA_ID}\"}" 2>/dev/null || echo '')"
  status="$(echo "$resp" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')"
  [[ "$status" == "approved" ]] || return 1
  token="$(echo "$resp" | sed -n 's/.*"provisioning_token":"\([^"]*\)".*/\1/p')"
  hid="$(echo "$resp" | sed -n 's/.*"home_id":"\([^"]*\)".*/\1/p')"
  [[ -n "$token" && -n "$hid" ]] || return 1
  mkdir -p /data
  printf '%s' "$token" > "$TOKEN_FILE"
  printf '%s' "$hid" > "$HOME_ID_FILE"
  rm -f "$CODE_FILE"
  rm -f "$NONCE_FILE" /homeassistant/www/hbot_pair_nonce.txt /config/www/hbot_pair_nonce.txt 2>/dev/null || true
  rm -f /data/.local_mount_restarted 2>/dev/null || true  # reset the one-time restart guard so a future re-pair on this box can restart Core again if needed
  return 0
}
if persist_if_approved; then
  log "self-heal: claim ${CODE} was ALREADY approved server-side — persisted token for home $(cat "$HOME_ID_FILE"). Remote access will come up shortly."
  exit 0
fi

# The base_url the app would independently DISCOVER this HA at (default: HA's in-network hostname).
# hbot-connect uses it as a SECONDARY sanity co-check for the auto-bind (the primary gate is the nonce
# below). Overridable for non-default HA ports.
HA_DISCOVER_URL="${HA_DISCOVER_URL:-http://homeassistant:8123}"

# ── LAN proof-of-possession NONCE (true 0-typing auto-bind, hijack-safe) ──────────────────────────────
# A brand-new client never told the add-on its home, so hbot-connect must decide which unbound claim to
# bind to the app's home. Host-matching alone is an authorization-bypass (every HAOS advertises
# http://homeassistant:8123, so a remote attacker could assert it and steal a victim's unbound HA).
# Instead we mint a 32-byte random NONCE and PUBLISH it as a file HA serves UNAUTHENTICATED on the LAN at
#   <base_url>/local/hbot_pair_nonce.txt      (HA maps <config>/www/ → /local/, no token required)
# The app, once it has discovered+reached this HA, GETs that file (proving it is on THIS LAN), and echoes
# the raw nonce to /claim/pending. hbot-connect binds ONLY if sha256(echo) == the nonce_hash we register
# here. A non-LAN attacker can neither read the file nor guess 256 bits → cannot forge the proof.
NONCE_FILE="/data/pair_nonce"
if [[ -s "$NONCE_FILE" ]]; then
  NONCE="$(cat "$NONCE_FILE")"
else
  NONCE="$(head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')" # 64 hex chars = 256 bits
  printf '%s' "$NONCE" > "$NONCE_FILE" 2>/dev/null || true
fi
NONCE_HASH="$(printf '%s' "$NONCE" | sha256sum | cut -d' ' -f1)"

# Publish the nonce into HA's www/ so it is served at /local/hbot_pair_nonce.txt (no auth). The config dir
# is mounted at /homeassistant (current) or /config (legacy) via map: homeassistant_config:rw.
publish_nonce() {
  local d
  for d in /homeassistant /config; do
    if [[ -d "$d" ]]; then
      mkdir -p "$d/www" 2>/dev/null || true
      if printf '%s' "$NONCE" > "$d/www/hbot_pair_nonce.txt" 2>/dev/null; then
        log "published LAN pairing nonce at /local/hbot_pair_nonce.txt (config dir $d)"
        return 0
      fi
    fi
  done
  log "WARNING: could not write www/hbot_pair_nonce.txt — 0-tap auto-bind unavailable; visible-code path still works."
  return 1
}
publish_nonce || true

# ── Force HA to actually SERVE /local (the 99-commit invisible failure) ───────────────────────────────
# HA registers the /local StaticPathConfig ONLY at Core STARTUP, by scanning <config>/www/. On a FRESH
# install www/ does not exist at boot, so Core never mounts /local — and publish_nonce() above writes the
# file AFTER boot, so HA serves a plain 404 for /local/hbot_pair_nonce.txt (and for ANY /local/* path)
# forever. The app then can't read the nonce → can't echo it → hbot-connect never auto-binds → the claim
# stays pending → ha_connections stays empty. This stranded every fresh box. Fix: after writing www/, ask
# Core (via the add-on's SUPERVISOR_TOKEN — homeassistant_api:true) to reload its core config, which
# re-registers the static /local mount without a full restart. Verify by fetching the nonce; if it's still
# 404 after reload, fall back to a full Core restart, GUARDED to at most once per pairing so we never loop.
SUP_TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}"
NONCE_URL="${HA_DISCOVER_URL:-http://homeassistant:8123}/local/hbot_pair_nonce.txt"
RESTART_FLAG="/data/.local_mount_restarted"

nonce_served_200() {
  # Returns 0 iff /local/hbot_pair_nonce.txt returns 200 with the exact 64-hex nonce (proves /local is
  # mounted AND the file is served). Any non-200 / body mismatch → non-zero.
  local body
  body="$(curl -fsS -m 8 "$NONCE_URL" 2>/dev/null || echo '')"
  [[ "$body" == "$NONCE" ]]
}

ensure_local_mount() {
  # Already served? Nothing to do (e.g. www/ existed at boot, or a prior reload took).
  if nonce_served_200; then
    log "/local/hbot_pair_nonce.txt already served (200) — static mount live, no reload needed."
    return 0
  fi
  if [[ -z "$SUP_TOKEN" ]]; then
    log "WARNING: no SUPERVISOR_TOKEN — cannot reload Core to expose /local; relying on :8098 proxy nonce fallback."
    return 1
  fi
  # Step 1: reload_core_config — re-registers the static /local path handler without a restart.
  log "/local not served yet — POST reload_core_config to register the static /local mount…"
  curl -fsS -m 30 -X POST \
    -H "Authorization: Bearer ${SUP_TOKEN}" -H 'Content-Type: application/json' \
    "http://supervisor/core/api/services/homeassistant/reload_core_config" -d '{}' >/dev/null 2>&1 || \
    log "reload_core_config request errored (non-fatal) — will verify anyway."
  # HA needs a beat to re-register the aiohttp static route.
  local i
  for i in 1 2 3 4 5 6; do
    if nonce_served_200; then
      log "/local/hbot_pair_nonce.txt now served (200) after reload_core_config — 0-tap auto-bind ready."
      return 0
    fi
    sleep 2
  done
  # Step 2: guarded full Core restart (at most ONCE per pairing — a /data flag prevents a restart loop).
  if [[ -f "$RESTART_FLAG" ]]; then
    log "WARNING: /local still 404 after reload AND a Core restart was already attempted this pairing — not restarting again (loop guard). Falling back to :8098 proxy nonce."
    return 1
  fi
  printf '%s' "$(date -u +%FT%TZ)" > "$RESTART_FLAG" 2>/dev/null || true
  log "reload_core_config did not expose /local — falling back to a ONE-TIME Core restart to scan www/ at startup…"
  curl -fsS -m 30 -X POST \
    -H "Authorization: Bearer ${SUP_TOKEN}" -H 'Content-Type: application/json' \
    "http://supervisor/core/api/services/homeassistant/restart" -d '{}' >/dev/null 2>&1 || \
    log "Core restart request errored (non-fatal) — the :8098 proxy nonce fallback still lets the app bind."
  log "Core restart requested — /local will register on next boot; the app can also read the nonce from the add-on proxy at :8098/hbot_pair_nonce meanwhile."
  return 0
}
ensure_local_mount || true

# ── Tailscale IP (multi-HA tailnet disambiguation) ───────────────────────────────────────────────────
# On a tailnet with 2+ HAs both named "homeassistant", the bare MagicDNS name resolves to only ONE box,
# so the app can't reach THIS operator's HA by name. We detect this box's own Tailscale IPv4 and register
# it as an explicit tailnet_url; the app then probes it directly (extraTailnetHosts) alongside the bare
# name and nonce-selects the right one. No tailnet → skip cleanly (LAN-only box, no regression).
detect_tailnet_ip() {
  local ip
  # Preferred: the tailscale CLI (present when Tailscale runs on the host / as an add-on).
  ip="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  if [[ -z "$ip" ]]; then
    # Fallback: the tailscale0 interface address.
    ip="$(ip -4 addr show tailscale0 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -1 || true)"
  fi
  if [[ -z "$ip" ]]; then
    # Last resort: scan all IPv4 addrs for a 100.64.0.0/10 CGNAT address (Tailscale's range).
    ip="$(ip -4 addr 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' \
          | awk -F. '$1==100 && $2>=64 && $2<=127 {print; exit}' || true)"
  fi
  # Validate it really is a 100.64/10 CGNAT address before trusting it.
  if [[ "$ip" =~ ^100\.([0-9]+)\. ]] && (( BASH_REMATCH[1] >= 64 && BASH_REMATCH[1] <= 127 )); then
    printf '%s' "$ip"
  fi
}
TAILNET_IP="$(detect_tailnet_ip)"
TAILNET_URL=""
if [[ -n "$TAILNET_IP" ]]; then
  TAILNET_URL="http://${TAILNET_IP}:8123"
  log "detected Tailscale IP ${TAILNET_IP} — registering ${TAILNET_URL} so the app finds THIS HA on the tailnet."
fi

# Register (idempotent per code+ha_id). Non-fatal — retried each boot until the app approves. We declare
# base_url (secondary co-check) + nonce_hash (the primary LAN proof gate) so the server can auto-bind the
# single discovered HA with zero typing, safely. tailnet_url (when present) lets the app probe THIS box's
# tailnet IP directly, past a bare-name collision. Omitted from the body when there is no tailnet.
register() {
  local body
  body="{\"code\":\"${CODE}\",\"ha_id\":\"${HA_ID}\",\"base_url\":\"${HA_DISCOVER_URL}\",\"nonce_hash\":\"${NONCE_HASH}\""
  if [[ -n "$TAILNET_URL" ]]; then
    body="${body},\"tailnet_url\":\"${TAILNET_URL}\""
  fi
  body="${body}}"
  curl -fsS -X POST "${HBOT_CONNECT_URL}/claim/register" \
    -H 'Content-Type: application/json' \
    --data "$body" >/dev/null 2>&1 || return 1
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
      # Pairing done → retire the LAN nonce (remove the served file + cache) so it can't be reused.
      rm -f "$NONCE_FILE" /homeassistant/www/hbot_pair_nonce.txt /config/www/hbot_pair_nonce.txt 2>/dev/null || true
  rm -f /data/.local_mount_restarted 2>/dev/null || true  # reset the one-time restart guard so a future re-pair on this box can restart Core again if needed
      log "paired to home ${HID}. Remote access will come up shortly."
      exit 0
    fi
  fi
  if [[ "$STATUS" == "unknown" ]]; then register || true; fi   # claim expired/swept → re-announce
  sleep 5
done

log "pairing not completed within 15 min — staying LAN-only. Restart the add-on to try again."
exit 0
