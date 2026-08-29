#!/usr/bin/env bash
# addon-connect.sh — drop into the HBot HA add-on (hbot-addon/hbot/run.sh) so, on FIRST BOOT, the
# add-on obtains a stable public URL for this HA install with ZERO user typing (BURDEN 1).
#
# ⚑ SOURCE OF TRUTH: THIS file (hbotv2/ha-integration/hbot-connect/addon-connect.sh) is the ONE
#   canonical copy. The operator's HA box installs the add-on from the GitHub repo tamermsol/hbot-addon;
#   whoever cuts a new add-on version MUST copy this file into that repo's hbot/addon-connect.sh so the
#   fix actually reaches the box. See SYNC-ADDON.md in this directory. Do NOT hand-edit a separate copy
#   (the split between this and hbot-addon/hbot/addon-connect.sh is exactly why the tunnel watchdog kept
#   regressing — fixes landed in one copy but the box shipped the other).
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

# ── SUPERVISED TUNNEL (permanent self-heal, 2026-08-29) ────────────────────────────────────────────
# The previous line was a terminal `exec cloudflared … run` with NO supervision. When cloudflared
# de-registers its origin (network blip, CGNAT re-NAT, CF edge hiccup) the LOCAL PROCESS STAYS ALIVE
# while the public tunnel is DEAD — Cloudflare's edge then serves HTTP 530 / "error code: 1033" and
# nothing respawns it, so every HA tile goes Offline until the operator manually restarts the add-on.
# This has recurred 5×. Restarting only on PROCESS EXIT (the old while-true loop in the legacy copy) is
# NOT enough: the process does not exit in this failure. The ONLY authoritative signal is an END-TO-END
# probe of the PUBLIC endpoint. So: run cloudflared in the background, probe ${URL}/api/ every 30s, and
# on 2 CONSECUTIVE public failures force-kill (-9) cloudflared and relaunch it. Loop forever while paired.
#
# POSIX/bash, Alpine-safe: only kill, sleep, curl, date, and shell builtins.

TS() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# Log the heartbeat back to hbot-connect on each HEALTHY probe (best-effort, non-fatal). This gives us
# OFF-BOX observability that does NOT depend on this dying add-on process (backend GET /tunnel-health).
heartbeat() {
  # $1 = http status observed on the public probe
  [ -n "${HBOT_CONNECT_URL:-}" ] || return 0
  curl -fsS -m 8 -X POST "${HBOT_CONNECT_URL}/tunnel-health" \
    -H 'Content-Type: application/json' \
    -H "X-Hbot-Provision-Token: ${PROV_TOKEN}" \
    --data "{\"home_id\":\"${HOME_ID}\",\"status\":\"${1}\",\"url\":\"${URL}\"}" \
    >/dev/null 2>&1 || true
}

start_cloudflared() {
  # --retries/--grace-period make cloudflared hold onto a connection harder before giving up; the outer
  # public-probe watchdog covers the case where it silently keeps a dead origin.
  cloudflared tunnel --no-autoupdate --retries 10 --grace-period 30s run --token "$TOKEN" &
  CF_PID=$!
  echo "[hbot-connect] $(TS) cloudflared started pid=${CF_PID} → ${URL}"
}

# Probe the PUBLIC endpoint end-to-end. Returns 0 = healthy, 1 = failure.
# Healthy = any HTTP status that proves a live origin answered (200 or 401 — HA auth challenge both mean
# cloudflared delivered the request to Core). Failure = 530/502/000/timeout, OR a body containing
# "error code: 1033" (Cloudflare's no-origin page, which can arrive with a 530 or occasionally a 200-ish
# edge response). We must inspect the BODY, not only the code, because 1033 is the definitive dead-origin tell.
probe_public() {
  _body="$(curl -s -m 12 "${URL}/api/" 2>/dev/null)"
  _code="$(curl -s -o /dev/null -m 12 -w '%{http_code}' "${URL}/api/" 2>/dev/null || echo 000)"
  case "$_body" in
    *"error code: 1033"*) return 1 ;;   # Cloudflare "no origin" — definitive tunnel death
  esac
  case "$_code" in
    200|401|403) return 0 ;;             # a live origin answered (auth-gated is fine)
    *) return 1 ;;                       # 530/502/000/timeout/etc = origin not reachable via edge
  esac
}

start_cloudflared
fails=0
last_reported=""   # "ok" / "fail" — heartbeat ONLY on state change (keeps backend traffic tiny fleet-wide)
# Give cloudflared a moment to register its first connection before the first public probe.
sleep 20
while true; do
  if probe_public; then
    if [ "$fails" -ne 0 ]; then echo "[hbot-connect] $(TS) public probe recovered (${URL}/api/ healthy)."; fi
    fails=0
    if [ "$last_reported" != "ok" ]; then heartbeat "ok"; last_reported="ok"; fi
  else
    fails=$(( fails + 1 ))
    echo "[hbot-connect] $(TS) public probe FAILED (${URL}/api/ → code=${_code}, 1033-body=$(case "$_body" in *'error code: 1033'*) echo yes;; *) echo no;; esac)) — consecutive=${fails}."
    if [ "$last_reported" != "fail" ]; then heartbeat "fail"; last_reported="fail"; fi
    if [ "$fails" -ge 2 ]; then
      # Do NOT trust the local PID being alive — the process stays 'up' while the origin is de-registered.
      # Only the public probe is authoritative, so on 2 consecutive public failures we force-kill + relaunch.
      echo "[hbot-connect] $(TS) 2 consecutive public failures — force-killing cloudflared pid=${CF_PID} and relaunching (tunnel auto-heal)."
      kill -9 "$CF_PID" 2>/dev/null || true
      wait "$CF_PID" 2>/dev/null || true
      # If cloudflared already died on its own, reap any stragglers too.
      pkill -9 -f 'cloudflared tunnel' 2>/dev/null || true
      sleep 2
      start_cloudflared
      fails=0
      sleep 20   # let the fresh process register before probing again
      continue
    fi
  fi
  # If cloudflared exited entirely (not just origin-dead), relaunch immediately regardless of probe count.
  if ! kill -0 "$CF_PID" 2>/dev/null; then
    echo "[hbot-connect] $(TS) cloudflared process exited — relaunching."
    pkill -9 -f 'cloudflared tunnel' 2>/dev/null || true
    sleep 1
    start_cloudflared
    fails=0
    sleep 20
    continue
  fi
  sleep 30
done
