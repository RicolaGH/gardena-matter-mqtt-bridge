ARG BUILD_FROM=ghcr.io/hassio-addons/base:18.2.0
FROM ${BUILD_FROM}
RUN apk add --no-cache python3 openssh-client
WORKDIR /opt/gardena-local
COPY *.py ./
COPY LICENSE NOTICE config.yaml ./
COPY run.sh /run.sh
RUN chmod 755 /run.sh
ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH
LABEL io.hass.type="addon" io.hass.name="GARDENA Local MQTT" \
      io.hass.version="${BUILD_VERSION}" io.hass.arch="${BUILD_ARCH}"
CMD ["/run.sh"]
