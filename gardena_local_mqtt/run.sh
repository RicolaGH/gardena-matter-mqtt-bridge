#!/usr/bin/env bash
set -euo pipefail
umask 077
mkdir -p /data/ssh
chmod 700 /data/ssh
if [ ! -f /data/ssh/addon_ed25519 ]; then
    ssh-keygen -q -t ed25519 -N '' -C gardena-local-mqtt -f /data/ssh/addon_ed25519
fi
touch /data/ssh/known_hosts_gardena
chmod 600 /data/ssh/addon_ed25519 /data/ssh/known_hosts_gardena
exec python3 /opt/gardena-local/app.py
