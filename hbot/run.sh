#!/usr/bin/with-contenv bashio
# Read the add-on options + the MQTT service HA gives us, then hand them to the bridge as env vars.

# Devices list (optional user-entered IPs) → comma-separated for the python bridge.
DEVICES=$(bashio::config 'devices | join(",")')
SUBNETS=$(bashio::config 'subnets | join(",")')
AUTODISCOVER=$(bashio::config 'autodiscover')
POLL=$(bashio::config 'poll_interval')
PREFIX=$(bashio::config 'discovery_prefix')
DEBUG=$(bashio::config 'debug')
ACCOUNT_EMAIL=$(bashio::config 'account_email')
ACCOUNT_PASSWORD=$(bashio::config 'account_password')

# HA's built-in MQTT broker credentials (provided because we declared services: mqtt:need).
# Do NOT `exit 1` if MQTT isn't ready yet — that made the Supervisor restart-loop us and the add-on
# looked like it 'stopped after ~20s'. Fall back to the default broker host and let the Python side
# retry the connection forever instead.
if bashio::services.available "mqtt"; then
  export MQTT_HOST="$(bashio::services mqtt 'host')"
  export MQTT_PORT="$(bashio::services mqtt 'port')"
  export MQTT_USER="$(bashio::services mqtt 'username')"
  export MQTT_PASS="$(bashio::services mqtt 'password')"
else
  bashio::log.warning "MQTT service not reported yet — falling back to core-mosquitto:1883 and retrying."
  export MQTT_HOST="core-mosquitto"
  export MQTT_PORT="1883"
fi

export HBOT_DEVICES="$DEVICES"
export HBOT_SUBNETS="$SUBNETS"
export HBOT_AUTODISCOVER="$AUTODISCOVER"
export HBOT_POLL="$POLL"
export HBOT_PREFIX="$PREFIX"
export HBOT_DEBUG="$DEBUG"
export HBOT_ACCOUNT_EMAIL="$ACCOUNT_EMAIL"
export HBOT_ACCOUNT_PASSWORD="$ACCOUNT_PASSWORD"

# ── Ensure <config>/www/ exists BEFORE Core scans it at startup (fresh-install /local fix) ──────────
# HA registers the /local static route ONLY at Core startup, and ONLY if <config>/www/ exists then. On a
# brand-new install www/ doesn't exist, so /local is never mounted and the add-on's pairing nonce (written
# post-boot) 404s forever — the root of the 99-commit pairing dead-end. Creating www/ here, before the
# bridge/pairing run, means on the NEXT Core start /local mounts natively. (addon-claim.sh additionally
# forces a reload/restart THIS boot, and the bridge serves the nonce at :8098 as a mount-independent
# fallback — belt and suspenders.) The config dir is mounted at /homeassistant (current) or /config.
for _cfg in /homeassistant /config; do
  if [[ -d "$_cfg" ]]; then
    mkdir -p "$_cfg/www" 2>/dev/null && bashio::log.info "ensured ${_cfg}/www exists so HA registers /local at Core startup." || true
    break
  fi
done

# ── Optional remote access: open a Cloudflare tunnel if this HA is paired to a home (BURDEN 1) ──
# Runs in the BACKGROUND and is fully non-fatal — if unpaired or provisioning fails, the add-on keeps
# working LAN-only. cloudflared holds the tunnel open; the bridge is the foreground process below.
HOME_ID=$(bashio::config 'home_id')
PROV_TOKEN=$(bashio::config 'provisioning_token')
export HBOT_CONNECT_URL="$(bashio::config 'hbot_connect_url')"
export SUPABASE_URL="$(bashio::config 'supabase_url')"
export SUPABASE_ANON_KEY="$(bashio::config 'supabase_anon_key')"
mkdir -p /data

# Legacy/manual path: the user pasted home_id + provisioning_token into the add-on options.
if [[ -n "$HOME_ID" && -n "$PROV_TOKEN" ]]; then
  printf '%s' "$HOME_ID" > /data/home_id
  printf '%s' "$PROV_TOKEN" > /data/provisioning_token
fi

# Zero-typing pairing (the default): if we aren't paired yet, announce a claim to hbot-connect and
# poll for the app to approve it. Runs in the BACKGROUND so the bridge (below) starts immediately and
# devices work on the LAN while the user pairs. On approval it persists /data/{home_id,provisioning_token}
# then opens the Cloudflare tunnel. Fully non-fatal — an unpaired HA just stays LAN-only.
if [[ -n "${HBOT_CONNECT_URL:-}" ]]; then
  (
    if [[ ! -s /data/provisioning_token || ! -s /data/home_id ]]; then
      /addon-claim.sh || bashio::log.warning "HBot pairing: claim flow errored — LAN-only for now."
    fi
    # ALWAYS attempt connect (v1.4.17): do NOT gate solely on the files the claim POLL wrote — that subshell
    # can die on an add-on update/restart mid-pairing, or the approval can land late, leaving connect skipped
    # forever even though the server already has the token. addon-connect.sh is now SELF-HEALING: if the
    # token files are missing but a stable claim_code/ha_id is derivable, it self-fetches the approved token
    # via /claim/status, then mints + writes ha_connections. It exits 0 cleanly if genuinely unpaired.
    if [[ -s /data/provisioning_token && -s /data/home_id ]]; then
      bashio::log.info "HBot remote access: paired (home $(cat /data/home_id)) — opening Cloudflare tunnel via ${HBOT_CONNECT_URL}"
    else
      bashio::log.info "HBot remote access: not paired via poll — running self-healing connect (fetch approved token if any) via ${HBOT_CONNECT_URL}"
    fi
    /addon-connect.sh || bashio::log.warning "HBot remote access: connect step failed — continuing LAN-only."
  ) &
else
  bashio::log.info "HBot remote access: hbot_connect_url unset — LAN-only."
fi

# ── Self-update pump (v1.4.9): make version bumps reach clients WITHOUT a manual Rebuild ────────────
# Root cause of "HA shows Up-to-date after a repo bump": the Supervisor caches the add-on repo git clone
# and only re-pulls on its own ~daily schedule, so a fresh commit is invisible for up to a day. From
# INSIDE the add-on (SUPERVISOR_TOKEN + hassio_role:manager) we can force it: POST /store/reload re-pulls
# every repository (this is exactly what `ha store reload` does — Supervisor api/store.py::reload bound to
# POST /store/reload), then GET /addons/self/info exposes update_available/version_latest, and if a newer
# version is now visible AND auto-update is on we self-update via POST /addons/self/update. Best-effort,
# non-fatal, backgrounded so it never delays the bridge/tunnel; a short delay lets Supervisor settle.
SUP_TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}"
if [[ -n "$SUP_TOKEN" ]]; then
  (
    sleep 20   # let the Supervisor finish bringing the add-on up before poking the store
    if curl -fsS -m 30 -X POST -H "Authorization: Bearer ${SUP_TOKEN}" \
         "http://supervisor/store/reload" >/dev/null 2>&1; then
      bashio::log.info "self-update: store reloaded (repo git clones re-pulled) — future version bumps now visible."
    else
      bashio::log.warning "self-update: store reload request failed (non-fatal; Supervisor will reload on its own schedule)."
    fi
    # After the reload, is a newer version of THIS add-on now visible? If so, install it (auto-update path).
    info="$(curl -fsS -m 15 -H "Authorization: Bearer ${SUP_TOKEN}" "http://supervisor/addons/self/info" 2>/dev/null || true)"
    upd="$(printf '%s' "$info" | sed -n 's/.*"update_available"[: ]*\(true\|false\).*/\1/p')"
    latest="$(printf '%s' "$info" | sed -n 's/.*"version_latest"[: ]*"\([^"]*\)".*/\1/p')"
    if [[ "$upd" == "true" ]]; then
      bashio::log.info "self-update: newer version ${latest:-?} available — requesting self-update…"
      if curl -fsS -m 120 -X POST -H "Authorization: Bearer ${SUP_TOKEN}" \
           "http://supervisor/addons/self/update" >/dev/null 2>&1; then
        bashio::log.info "self-update: update to ${latest:-latest} requested (Supervisor will install + restart the add-on)."
      else
        bashio::log.warning "self-update: self-update request failed (non-fatal; auto-update will retry)."
      fi
    else
      bashio::log.info "self-update: already on the latest visible version after store reload."
    fi
  ) &
else
  bashio::log.warning "self-update: no SUPERVISOR_TOKEN — cannot force a store reload; relying on Supervisor's own schedule."
fi

bashio::log.info "HBot starting — account: ${ACCOUNT_EMAIL:-none}, autodiscover: ${AUTODISCOVER}, manual devices: ${DEVICES:-none}, subnets: ${SUBNETS:-auto}, debug: ${DEBUG}, MQTT: ${MQTT_HOST}:${MQTT_PORT}, poll ${POLL}s"
# `exec` so signals reach python; if python ever exits, the trap logs it (should never happen).
exec python3 /hbot_bridge.py
