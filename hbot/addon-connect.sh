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
HAID_FILE="/data/ha_id"
CODE_FILE="/data/claim_code"
STATE_FILE="/data/tunnel_url"
HBOT_CONNECT_URL="${HBOT_CONNECT_URL:?set HBOT_CONNECT_URL}"

# ── SELF-HEAL (v1.4.17): don't depend on the claim POLL subshell having written the files ──────────────
# ROOT CAUSE of "approved but nothing happens": addon-claim.sh persisted /data/{home_id,provisioning_token}
# ONLY inside its 15-min in-process poll. If that subshell died (add-on UPDATE/restart mid-pairing) or the
# approval landed after the loop, the files were never written → this script's old "not paired → skip" gate
# skipped connect PERMANENTLY, even though the server already had the token. We now fetch the token
# OURSELVES from the persisted STABLE (code, ha_id): a single restart completes pairing.
if [[ ! -s "$HOME_ID_FILE" || ! -s "$TOKEN_FILE" ]]; then
  # Recover the stable ha_id + code the same deterministic way addon-claim.sh derives them (machine-id),
  # so this works even if /data was partially wiped. A persisted file takes precedence.
  MID="$(cat /etc/machine-id 2>/dev/null || cat /data/.mid 2>/dev/null || true)"
  HA_ID="$(cat "$HAID_FILE" 2>/dev/null || true)"
  [[ -z "$HA_ID" && -n "$MID" ]] && HA_ID="ha-$(printf '%s' "$MID" | sha256sum | cut -c1-24)"
  CODE="$(cat "$CODE_FILE" 2>/dev/null || true)"
  [[ -z "$CODE" && -n "$MID" ]] && CODE="HBOT$(printf 'hbot-claim:%s' "$MID" | sha256sum | tr 'a-f' 'A-F' | head -c 8)"
  if [[ -z "$CODE" || -z "$HA_ID" ]]; then
    echo "[hbot-connect] not paired yet (no token files and no derivable claim code/ha_id) — skipping."
    exit 0
  fi
  echo "[hbot-connect] token files missing — self-fetching via /claim/status (code ${CODE})…"
  RESP="$(curl -fsS -m 15 -X POST "${HBOT_CONNECT_URL}/claim/status" \
    -H 'Content-Type: application/json' \
    --data "{\"code\":\"${CODE}\",\"ha_id\":\"${HA_ID}\"}" 2>/dev/null || echo '')"
  STATUS="$(echo "$RESP" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')"
  if [[ "$STATUS" == "approved" ]]; then
    T="$(echo "$RESP" | sed -n 's/.*"provisioning_token":"\([^"]*\)".*/\1/p')"
    H="$(echo "$RESP" | sed -n 's/.*"home_id":"\([^"]*\)".*/\1/p')"
    if [[ -n "$T" && -n "$H" ]]; then
      mkdir -p /data
      printf '%s' "$T" > "$TOKEN_FILE"
      printf '%s' "$H" > "$HOME_ID_FILE"
      echo "[hbot-connect] self-heal: fetched approved token for home ${H} — proceeding to mint + connect."
    else
      echo "[hbot-connect] /claim/status approved but missing token/home in response — awaiting; exiting 0 (retry next boot)."
      exit 0
    fi
  else
    echo "[hbot-connect] awaiting approval (status=${STATUS:-none}) — LAN-only for now; a later boot retries."
    exit 0
  fi
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

# ── Mint the HA access token the app will use — ROBUST, NEVER SILENT (v1.4.16).
#
# CRITICAL (HA docs, developers.home-assistant.io/docs/add-ons/communication): the SUPERVISOR_TOKEN is
# ONLY valid via the internal proxy http://supervisor/core/api/ — it is NOT accepted by Core's DIRECT
# external API at homeassistant.local:8123/api/, which is exactly what the phone app calls. Writing it as
# the app's access_token therefore yields a 401 and the app never connects (even after restart). So we
# MINT A REAL long-lived access token (LLAT, a JWT eyJ...) via Core's WebSocket auth (mint_llat.py, which
# authenticates the WS with SUPERVISOR_TOKEN and calls auth/long_lived_access_token). That JWT is tied to
# the add-on's Core user and IS accepted by the direct /api/.
#
# We VERIFY the minted token against the DIRECT api the same way the app will (LAN_URL/api/, NOT the
# supervisor proxy). This is the AUTONOMY-CRITICAL step: a real operator box left ha_connections.access_token
# NULL because the mint failed SILENTLY and we POSTed an empty token — so the app had base_url but no token
# and "nothing happened". We now:
#   • LOOP the mint+verify up to MINT_TRIES over ~2 min (Core may not be fully up on first boot);
#   • on success, write base_url + token TOGETHER (order: mint FIRST, then /provision);
#   • on total failure, DO NOT post an empty token — instead record a mint_error DIAGNOSTIC server-side so
#     the box never looks "paired" while silently tokenless, and log the reason prominently.
HA_TOKEN=""
MINT_ERROR=""
MINT_TRIES="${MINT_TRIES:-10}"       # up to ~10 attempts…
MINT_SLEEP="${MINT_SLEEP:-12}"       # …× ~12s ≈ 2 min total
verify_direct() {
  # Verify $1 against Core's DIRECT /api/ (the app's path). 200 = a valid token; sets HA_TOKEN + LAN_URL.
  local tok="$1" code alt
  code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${tok}" "${LAN_URL}/api/" 2>/dev/null || echo 000)"
  if [[ "$code" == "200" ]]; then
    HA_TOKEN="$tok"; echo "[hbot-connect] minted LLAT verified against Core DIRECT api (${LAN_URL}/api/ → 200)."; return 0
  fi
  # Fall back to the mDNS host if the primary-IP direct probe didn't reach Core (some routers block it).
  alt="$(curl -s -m 10 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${tok}" "http://homeassistant.local:8123/api/" 2>/dev/null || echo 000)"
  if [[ "$alt" == "200" ]]; then
    HA_TOKEN="$tok"; LAN_URL="http://homeassistant.local:8123"
    echo "[hbot-connect] minted LLAT verified against Core DIRECT api (homeassistant.local → 200)."; return 0
  fi
  MINT_ERROR="minted token did not authenticate against Core DIRECT /api/ (${LAN_URL}/api/ → ${code}, homeassistant.local → ${alt})"
  return 1
}
i=1
while [[ $i -le $MINT_TRIES ]]; do
  MINTED="$(SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}" LLAT_CLIENT_NAME="HBot App" \
            python3 /mint_llat.py 2>/tmp/mint_llat.err || true)"
  if [[ -n "$MINTED" ]]; then
    case "$MINTED" in eyJ*) : ;; *) echo "[hbot-connect] note: minted token is not a JWT (unexpected) — verifying directly anyway." ;; esac
    if verify_direct "$MINTED"; then break; fi
  else
    MINT_ERROR="mint_llat.py returned no token: $(tail -n1 /tmp/mint_llat.err 2>/dev/null)"
  fi
  echo "[hbot-connect] token not ready (attempt ${i}/${MINT_TRIES}): ${MINT_ERROR}"
  i=$((i + 1))
  [[ $i -le $MINT_TRIES ]] && sleep "$MINT_SLEEP"
done

if [[ -z "$HA_TOKEN" ]]; then
  # NEVER post an empty token (that's the autonomy-breaker: base_url written, token NULL → app stuck).
  # Record the failure server-side so we can SEE why this box failed without Supervisor access, and make
  # the reason loud in the add-on log. The full mint stderr is in /tmp/mint_llat.err.
  MINT_ERROR="${MINT_ERROR:-unknown mint failure}"
  MINT_DETAIL="$(tr '\n' ' ' </tmp/mint_llat.err 2>/dev/null | tail -c 600)"
  echo "[hbot-connect] ================ TOKEN MINT FAILED ================"
  echo "[hbot-connect] Could not mint+verify a Home Assistant token after ${MINT_TRIES} attempts."
  echo "[hbot-connect] reason: ${MINT_ERROR}"
  echo "[hbot-connect] mint_llat.py stderr: ${MINT_DETAIL}"
  echo "[hbot-connect] NOT writing a token-less connection. Reporting a diagnostic to the server so the"
  echo "[hbot-connect] app does NOT appear paired-but-tokenless. Common cause: the add-on's Core user is"
  echo "[hbot-connect] not an owner/admin (HA refuses auth/long_lived_access_token for non-admins)."
  echo "[hbot-connect] ==================================================="
  # POST the diagnostic (no token). The server records mint_error and will NOT write base_url without a
  # token (so the row never looks 'connected'). JSON-escape the reason (quotes/backslashes) crudely.
  ESC_ERR="$(printf '%s' "${MINT_ERROR} | ${MINT_DETAIL}" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n\r')"
  curl -fsS -m 15 -X POST "${HBOT_CONNECT_URL}/provision" \
    -H 'Content-Type: application/json' \
    -H "X-Hbot-Provision-Token: ${PROV_TOKEN}" \
    --data "{\"home_id\":\"${HOME_ID}\",\"mint_error\":\"${ESC_ERR}\"}" \
    >/dev/null 2>&1 \
    && echo "[hbot-connect] mint_error diagnostic recorded server-side." \
    || echo "[hbot-connect] warn: could not record mint_error diagnostic (network?)."
  # Keep the add-on alive for auto-update + the tunnel is pointless without Core auth; exit non-fatally so
  # the Supervisor doesn't crash-loop us. A later boot (Core fully up / user made the add-on owner) retries.
  exit 0
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
