#!/usr/bin/env bash
#
# deploy-certs.sh — bridge Let's Encrypt certs into the mosquitto container.
#
# Let's Encrypt writes certs to /etc/letsencrypt/live/<domain>/ owned by root
# (privkey.pem is 0600 root). The eclipse-mosquitto container runs as uid/gid 1883
# and cannot read those. This script copies fullchain.pem + privkey.pem into ./certs/
# with ownership the container can read, then restarts the broker so it loads the
# (possibly renewed) cert.
#
# Run it TWICE in the lifecycle:
#   1. Once by hand after the first `certbot certonly`, to seed ./certs/ before
#      `docker compose up -d`.
#   2. Automatically on every renewal, as certbot's --deploy-hook (see README).
#
# Needs root (to read privkey.pem and chown). Run with sudo.
#
# Usage: sudo ./deploy-certs.sh [domain]   (default: toothless-telemetry.naveenapius.com)

set -euo pipefail

DOMAIN="${1:-toothless-telemetry.naveenapius.com}"
SRC="/etc/letsencrypt/live/${DOMAIN}"
DEST="$(cd "$(dirname "$0")" && pwd)/certs"
CONTAINER="toothless-mosquitto"
MOSQ_UID=1883   # uid/gid mosquitto runs as inside the eclipse-mosquitto image

if [ ! -d "$SRC" ]; then
  echo "ERROR: no cert at $SRC — run certbot for $DOMAIN first." >&2
  exit 1
fi

mkdir -p "$DEST"

# -L dereferences the LE symlinks (live/ -> archive/) so we copy the real files.
cp -L "$SRC/fullchain.pem" "$DEST/fullchain.pem"
cp -L "$SRC/privkey.pem"   "$DEST/privkey.pem"

chown "${MOSQ_UID}:${MOSQ_UID}" "$DEST/fullchain.pem" "$DEST/privkey.pem"
chmod 644 "$DEST/fullchain.pem"
chmod 600 "$DEST/privkey.pem"

echo "Certs for ${DOMAIN} copied to ${DEST} (owner ${MOSQ_UID}, key 0600)."

# Restart the broker so it picks up the new cert — but only if it's already running.
# (On first-time seeding the container doesn't exist yet; that's expected.)
if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  docker restart "$CONTAINER" >/dev/null
  echo "Restarted ${CONTAINER} to load the cert."
else
  echo "Container ${CONTAINER} not running yet — start it with 'docker compose up -d'."
fi
