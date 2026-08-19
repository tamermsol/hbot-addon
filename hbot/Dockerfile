ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19
FROM $BUILD_FROM

# Small Python + requests + paho-mqtt; bashio comes from the HA base image for reading options/services.
# zeroconf enables mDNS auto-discovery of Tasmota devices; requests+paho for HTTP/MQTT.
RUN apk add --no-cache python3 py3-pip curl \
    && pip3 install --no-cache-dir --break-system-packages requests paho-mqtt zeroconf

# cloudflared for the optional remote-access tunnel (multi-arch: pick the binary for this build arch).
ARG BUILD_ARCH=amd64
RUN case "${BUILD_ARCH}" in \
      aarch64) CF_ARCH=arm64 ;; \
      amd64)   CF_ARCH=amd64 ;; \
      armv7|armhf) CF_ARCH=arm ;; \
      i386)    CF_ARCH=386 ;; \
      *)       CF_ARCH=amd64 ;; \
    esac \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
       -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared

COPY run.sh /run.sh
COPY hbot_bridge.py /hbot_bridge.py
COPY addon-connect.sh /addon-connect.sh
RUN chmod a+x /run.sh /addon-connect.sh

CMD [ "/run.sh" ]
