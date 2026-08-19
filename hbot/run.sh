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
    if [[ -s /data/provisioning_token && -s /data/home_id ]]; then
      bashio::log.info "HBot remote access: paired (home $(cat /data/home_id)) — opening Cloudflare tunnel via ${HBOT_CONNECT_URL}"
      /addon-connect.sh || bashio::log.warning "HBot remote access: tunnel provisioning failed — continuing LAN-only."
    else
      bashio::log.info "HBot remote access: not paired yet — LAN-only. Pair this HA in the H-Bot app (Settings -> Home Assistant)."
    fi
  ) &
else
  bashio::log.info "HBot remote access: hbot_connect_url unset — LAN-only."
fi

bashio::log.info "HBot starting — account: ${ACCOUNT_EMAIL:-none}, autodiscover: ${AUTODISCOVER}, manual devices: ${DEVICES:-none}, subnets: ${SUBNETS:-auto}, debug: ${DEBUG}, MQTT: ${MQTT_HOST}:${MQTT_PORT}, poll ${POLL}s"
# `exec` so signals reach python; if python ever exits, the trap logs it (should never happen).
exec python3 /hbot_bridge.py
