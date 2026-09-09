#!/usr/bin/env bash
# Configuration arrives on stdin as a serialized systemd EnvironmentFile.
set -euo pipefail
: "${GATEWAY_IP:?}" "${GARDENA_SSH_KEY:?}" "${MQTT_BINARY:?}" "${MQTT_SERVICE:?}"
GW="root@${GATEWAY_IP}"
SSH_OPTS=(-i "${GARDENA_SSH_KEY}" -o IdentitiesOnly=yes -o BatchMode=yes
    -o StrictHostKeyChecking=accept-new
    -o "UserKnownHostsFile=${GARDENA_KNOWN_HOSTS:-/data/ssh/known_hosts_gardena}"
    -o GlobalKnownHostsFile=/dev/null -o ConnectTimeout=15)
# Consume stdin only here. User data is never parsed as shell program text.
ssh "${SSH_OPTS[@]}" "$GW" 'set -eu
umask 077
mkdir -p /etc/gardena-matter
chmod 700 /etc/gardena-matter
tmp=$(mktemp /etc/gardena-matter/mqtt.env.XXXXXX)
trap "rm -f $tmp" EXIT
cat > "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" /etc/gardena-matter/mqtt.env'
ssh -n "${SSH_OPTS[@]}" "$GW" 'mkdir -p /usr/local/lib/gardena-matter'
scp -O "${SSH_OPTS[@]}" "$MQTT_BINARY" "$GW:/usr/local/lib/gardena-matter/gardena-mqtt-publisher.new"
scp -O "${SSH_OPTS[@]}" "$MQTT_SERVICE" "$GW:/etc/systemd/system/gardena-mqtt-publisher.service"
ssh -n "${SSH_OPTS[@]}" "$GW" 'set -eu
chmod 755 /usr/local/lib/gardena-matter/gardena-mqtt-publisher.new
mv -f /usr/local/lib/gardena-matter/gardena-mqtt-publisher.new /usr/local/lib/gardena-matter/gardena-mqtt-publisher
systemctl daemon-reload'
