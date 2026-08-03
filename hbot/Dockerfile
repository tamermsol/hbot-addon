ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19
FROM $BUILD_FROM

# Small Python + requests + paho-mqtt; bashio comes from the HA base image for reading options/services.
# zeroconf enables mDNS auto-discovery of Tasmota devices; requests+paho for HTTP/MQTT.
RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir --break-system-packages requests paho-mqtt zeroconf

COPY run.sh /run.sh
COPY hbot_bridge.py /hbot_bridge.py
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
