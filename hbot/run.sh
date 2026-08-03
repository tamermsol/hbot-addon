#!/usr/bin/with-contenv bashio
# Read the add-on options + the MQTT service HA gives us, then hand them to the bridge as env vars.

# Devices list (optional user-entered IPs) → comma-separated for the python bridge.
DEVICES=$(bashio::config 'devices | join(",")')
SUBNETS=$(bashio::config 'subnets | join(",")')
AUTODISCOVER=$(bashio::config 'autodiscover')
POLL=$(bashio::config 'poll_interval')
PREFIX=$(bashio::config 'discovery_prefix')

# HA's built-in MQTT broker credentials (provided because we declared services: mqtt:need).
if bashio::services.available "mqtt"; then
  export MQTT_HOST="$(bashio::services mqtt 'host')"
  export MQTT_PORT="$(bashio::services mqtt 'port')"
  export MQTT_USER="$(bashio::services mqtt 'username')"
  export MQTT_PASS="$(bashio::services mqtt 'password')"
else
  bashio::log.fatal "No MQTT service available. Install the 'Mosquitto broker' add-on and try again."
  exit 1
fi

export HBOT_DEVICES="$DEVICES"
export HBOT_SUBNETS="$SUBNETS"
export HBOT_AUTODISCOVER="$AUTODISCOVER"
export HBOT_POLL="$POLL"
export HBOT_PREFIX="$PREFIX"

bashio::log.info "HBot starting — autodiscover: ${AUTODISCOVER}, manual devices: ${DEVICES:-none}, subnets: ${SUBNETS:-auto}, MQTT: ${MQTT_HOST}:${MQTT_PORT}, poll ${POLL}s"
exec python3 /hbot_bridge.py
