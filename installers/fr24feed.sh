#!/bin/bash
# Install fr24feed (FlightRadar24 feeder) on Debian/Raspbian.
#
# Called by skytrack-installer wrapper — runs as root.
# Idempotent: re-running is safe.

set -euo pipefail

echo "=== fr24feed installer ==="

if dpkg -s fr24feed >/dev/null 2>&1; then
  echo "fr24feed is already installed."
  CURRENT_VER="$(dpkg -s fr24feed | grep '^Version:' | awk '{print $2}')"
  echo "  current version: ${CURRENT_VER}"
fi

# FR24 provides their own repo and signing key
FR24_LIST="/etc/apt/sources.list.d/fr24feed.list"

if [[ ! -f "$FR24_LIST" ]]; then
  echo "Adding FlightRadar24 APT repository…"

  # Import GPG key
  FR24_KEY="/usr/share/keyrings/fr24feed-archive-keyring.gpg"
  if [[ ! -f "$FR24_KEY" ]]; then
    curl -fsSL "https://repo-feed.flightradar24.com/fr24feed_pubkey.gpg" \
      | gpg --dearmor -o "$FR24_KEY" 2>/dev/null \
      || curl -fsSL "https://repo-feed.flightradar24.com/fr24feed_pubkey.gpg" \
           > /etc/apt/trusted.gpg.d/fr24feed.gpg 2>/dev/null \
      || echo "Warning: could not add FR24 GPG key"
  fi

  ARCH="$(dpkg --print-architecture)"
  if [[ -f "$FR24_KEY" ]]; then
    echo "deb [signed-by=${FR24_KEY}] https://repo-feed.flightradar24.com flightradar24 raspberrypi-stable" > "$FR24_LIST"
  else
    echo "deb https://repo-feed.flightradar24.com flightradar24 raspberrypi-stable" > "$FR24_LIST"
  fi
fi

echo "Updating package lists…"
apt-get update

echo "Installing fr24feed…"
DEBIAN_FRONTEND=noninteractive apt-get install -y fr24feed

echo "Enabling and starting fr24feed service…"
systemctl enable fr24feed || true
systemctl restart fr24feed || true

if systemctl is-active --quiet fr24feed; then
  echo "fr24feed is running."
else
  echo "Warning: fr24feed installed but service not yet active."
  echo "You may need to run 'sudo fr24feed --signup' to configure it."
fi

echo "=== fr24feed installation complete ==="
