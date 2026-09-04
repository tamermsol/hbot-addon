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

# ── LAN base_url: reach Core directly on-network. Computed EARLY because we verify the minted token
# against this exact DIRECT endpoint (the one the app calls), not the supervisor proxy. Prefer the
# add-on's own primary IP (host_network), fall back to the mDNS name every HAOS install answers to. ──
LAN_IP="$(hostname -i 2>/dev/null | awk '{print $1}')"
if [[ -n "$LAN_IP" && "$LAN_IP" != "127.0.0.1" ]]; then
  LAN_URL="http://${LAN_IP}:8123"
else
  LAN_URL="http://homeassistant.local:8123"
fi

# ── Mint the HA access token the app will use.
#
# CRITICAL (HA docs, developers.home-assistant.io/docs/add-ons/communication): the SUPERVISOR_TOKEN is
# ONLY valid via the internal proxy http://supervisor/core/api/ — it is NOT accepted by Core's DIRECT
# external API at homeassistant.local:8123/api/, which is exactly what the phone app calls. Writing it as
# the app's access_token therefore yields a 401 and the app never connects (even after restart). So we
# MINT A REAL long-lived access token (LLAT, a JWT eyJ...) via Core's WebSocket auth (mint_llat.py, which
# authenticates the WS with SUPERVISOR_TOKEN and calls auth/long_lived_access_token). That JWT is tied to
# the add-on's Core user and IS accepted by the direct /api/.
#
# We then VERIFY the minted token against the DIRECT api the same way the app will (LAN_URL/api/, NOT the
# supervisor proxy). Only a token that authenticates there is written; otherwise we write an EMPTY token
# and log clearly, so the app shows "finish / needs token" instead of a silently-broken 401 connection.
HA_TOKEN=""
MINTED="$(SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}" LLAT_CLIENT_NAME="HBot App" \
          python3 /mint_llat.py 2>/tmp/mint_llat.err || true)"
if [[ -z "$MINTED" ]]; then
  echo "[hbot-connect] warn: could not mint a long-lived token: $(tail -n1 /tmp/mint_llat.err 2>/dev/null)"
  echo "[hbot-connect] writing an EMPTY token — the app will show 'finish setup / needs token'."
else
  case "$MINTED" in
    eyJ*) : ;; # looks like a JWT (real LLAT), good
    *)    echo "[hbot-connect] warn: minted token is not a JWT (unexpected) — will still verify it directly." ;;
  esac
  # VERIFY against Core's DIRECT api (the app's path). HTTP 200 (authorized) or 401 both mean the endpoint
  # is Core and the bearer was evaluated; a VALID token returns 200 on /api/. We require 200 here.
  DIRECT_CODE="$(curl -s -m 10 -o /dev/null -w '%{http_code}' \
                 -H "Authorization: Bearer ${MINTED}" "${LAN_URL}/api/" 2>/dev/null || echo 000)"
  if [[ "$DIRECT_CODE" == "200" ]]; then
    HA_TOKEN="$MINTED"
    echo "[hbot-connect] minted LLAT verified against Core DIRECT api (${LAN_URL}/api/ → 200)."
  else
    # Fall back to the mDNS host if the primary-IP direct probe didn't reach Core (some routers block it).
    ALT_CODE="$(curl -s -m 10 -o /dev/null -w '%{http_code}' \
                -H "Authorization: Bearer ${MINTED}" "http://homeassistant.local:8123/api/" 2>/dev/null || echo 000)"
    if [[ "$ALT_CODE" == "200" ]]; then
      HA_TOKEN="$MINTED"
      LAN_URL="http://homeassistant.local:8123"
      echo "[hbot-connect] minted LLAT verified against Core DIRECT api (homeassistant.local → 200)."
    else
      echo "[hbot-connect] warn: minted token did NOT authenticate against Core DIRECT api (${LAN_URL}/api/ → ${DIRECT_CODE}, homeassistant.local → ${ALT_CODE})."
      echo "[hbot-connect] writing an EMPTY token — the app will show 'finish setup / needs token' rather than a broken 401 connection."
    fi
  fi
fi

# ── Zero-touch updates: turn ON the Supervisor auto-update for THIS add-on so non-technical clients
# receive every future fix automatically (no terminal, no GitHub). Idempotent — runs on every boot,
# so if the setting is ever cleared it self-heals. Uses SUPERVISOR_TOKEN DIRECTLY (the supervisor API is
# the proxy path where that token is valid — NOT the app's HA_TOKEN, which is now a Core LLAT that is
# deliberately NOT authorized for /api/hassio/). Non-fatal. (v1.4.4) ──
SUP_TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}"
if [[ -n "$SUP_TOKEN" ]]; then
  if curl -fsS -m 10 -X POST -H "Authorization: Bearer ${SUP_TOKEN}" \
       -H 'Content-Type: application/json' -d '{"auto_update": true}' \
       "http://supervisor/addons/self/options" >/dev/null 2>&1; then
    echo "[hbot-connect] auto-update ENABLED for this add-on — future versions install themselves."
  else
    echo "[hbot-connect] note: could not set auto-update automatically (leave it on in the add-on page). Continuing."
  fi
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
  # Ensure configuration.yaml loads packages, WITHOUT ever writing a second top-level `homeassistant:`
  # block (duplicate YAML keys make Core refuse to boot — config-corruption). Three cases:
  #   (a) a `packages:` include already present anywhere → nothing to do.
  #   (b) NO top-level `homeassistant:` key at all → safe to append our own block.
  #   (c) a `homeassistant:` block EXISTS but has no packages include → we must NOT append a second
  #       block; log clearly and rely on the package file still being read only if the user adds the
  #       include. (We refuse to blindly splice under an existing block from a shell script — that risks
  #       corrupting hand-authored YAML. The tunnel simply 400s until the include is added, which the
  #       log explains, rather than bricking Core.)
  local cfg="/homeassistant/configuration.yaml"
  local has_pkg_include has_ha_key
  has_pkg_include="$(grep -cE '^\s*packages:\s*!include_dir_named\s+packages' "$cfg" 2>/dev/null || echo 0)"
  has_ha_key="$(grep -cE '^homeassistant:\s*$' "$cfg" 2>/dev/null || echo 0)"
  if [[ "$has_pkg_include" == "0" ]]; then
    if [[ "$has_ha_key" == "0" ]]; then
      printf '\n# Added by HBot add-on so package files (incl. tunnel trusted_proxies) load.\nhomeassistant:\n  packages: !include_dir_named packages\n' >> "$cfg" 2>/dev/null || true
      echo "[hbot-connect] added 'homeassistant: packages: !include_dir_named packages' to configuration.yaml."
    else
      echo "[hbot-connect] warn: configuration.yaml already has a 'homeassistant:' block but no packages include — NOT appending a duplicate key (would break Core). Add 'packages: !include_dir_named packages' under it to enable the tunnel trusted_proxies package."
    fi
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
  echo "[hbot-connect] wrote trusted_proxies package; validating Core config before restart…"
  # Validate the config via Core's check_config service BEFORE requesting a restart, so a malformed
  # configuration.yaml (whatever the cause) never gets a restart that would leave Core down. Uses the
  # Supervisor proxy path with SUPERVISOR_TOKEN (check_config is a Supervisor operation). If validation
  # fails or is unavailable, we do NOT restart — trusted_proxies simply applies on the next manual
  # restart, which is safe (tunnel 400s until then) rather than risking a boot failure.
  local check_code
  check_code="$(curl -s -o /dev/null -m 20 -w '%{http_code}' -X POST \
    -H "Authorization: Bearer ${SUP_TOKEN:-${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}}" \
    "http://supervisor/core/check" 2>/dev/null || echo 000)"
  if [[ "$check_code" == "200" ]]; then
    curl -fsS -m 10 -X POST -H "Authorization: Bearer ${HA_TOKEN}" \
      "http://supervisor/core/api/services/homeassistant/restart" -H 'Content-Type: application/json' -d '{}' \
      >/dev/null 2>&1 && echo "[hbot-connect] config valid — Core restart requested to apply trusted_proxies." \
      || echo "[hbot-connect] warn: Core restart request failed — trusted_proxies will apply on next HA restart."
  else
    echo "[hbot-connect] warn: config check returned ${check_code} (not 200) — NOT restarting Core; trusted_proxies will apply on the next manual restart."
  fi
}
ensure_trusted_proxies

echo "[hbot-connect] starting cloudflared → ${URL}"
# ── SUPERVISED TUNNEL (permanent self-heal — RESTORED into v1.4.26, 2026-09-04) ─────────────────────
# REGRESSION FIXED: the v1.4.26 reconcile (mint-LLAT + LAN fast-path) replaced the self-heal watchdog
# with a bare `while true; cloudflared … run` loop that ONLY restarts on PROCESS EXIT. When cloudflared
# de-registers its origin (network blip, CGNAT re-NAT, CF edge hiccup) the LOCAL PROCESS STAYS ALIVE
# while the public tunnel is DEAD — Cloudflare's edge serves HTTP 530 / "error code: 1033" and nothing
# respawns it, so every HA tile goes Offline until someone manually restarts the add-on. This has now
# recurred 6×. Restarting only on PROCESS EXIT is NOT enough: the process does not exit in this failure.
# The ONLY authoritative signal is an END-TO-END probe of the PUBLIC endpoint. So: run cloudflared in the
# background, probe ${URL}/api/ every 30s, and on 2 CONSECUTIVE public failures force-kill (-9) cloudflared
# and relaunch it. Loop forever while paired.  POSIX/bash, Alpine-safe: only kill, sleep, curl, date, builtins.

TS() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# Log the heartbeat back to hbot-connect on each probe state change (best-effort, non-fatal). This gives
# OFF-BOX observability that does NOT depend on this dying add-on process (backend GET /tunnel-health).
heartbeat() {
  # $1 = "ok" / "fail"
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
  cloudflared tunnel --no-autoupdate --retries 10 --grace-period 30s run --token "$TUNNEL_TOKEN" &
  CF_PID=$!
  echo "[hbot-connect] $(TS) cloudflared started pid=${CF_PID} → ${URL}"
}

# Probe the PUBLIC endpoint end-to-end. Returns 0 = healthy, 1 = failure.
# Healthy = any HTTP status that proves a live origin answered (200/401/403 — an HA auth challenge still
# means cloudflared delivered the request to Core). Failure = 530/502/000/timeout, OR a body containing
# "error code: 1033" (Cloudflare's no-origin page). We inspect the BODY, not only the code, because 1033
# is the definitive dead-origin tell.
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
