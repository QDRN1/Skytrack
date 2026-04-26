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

# ── GPG key import (always refresh) ──
FR24_KEY="/usr/share/keyrings/fr24feed-archive-keyring.gpg"
echo "Importing FlightRadar24 GPG signing keys…"

KEY_OK=false

# Method 1: download from FR24 repo and dearmor into keyring
if curl -fsSL "https://repo-feed.flightradar24.com/fr24feed_pubkey.gpg" -o /tmp/fr24.gpg 2>/dev/null; then
  # Detect if armored (text) or binary
  if file /tmp/fr24.gpg | grep -qi 'PGP\|ASCII'; then
    gpg --dearmor -o "$FR24_KEY" < /tmp/fr24.gpg 2>/dev/null && KEY_OK=true
  else
    cp /tmp/fr24.gpg "$FR24_KEY" && KEY_OK=true
  fi
  rm -f /tmp/fr24.gpg
fi

# Method 2: fetch keys by fingerprint from keyserver
if [[ "$KEY_OK" != "true" ]]; then
  echo "Direct download failed, trying keyserver…"
  for KEYID in 97A9DF822081EC2A 6F7703F65FA1BDAF; do
    gpg --no-default-keyring --keyring gnupg-ring:"$FR24_KEY" \
        --keyserver hkp://keyserver.ubuntu.com:80 \
        --recv-keys "$KEYID" 2>/dev/null || true
  done
  chmod 644 "$FR24_KEY" 2>/dev/null || true
  [[ -s "$FR24_KEY" ]] && KEY_OK=true
fi

# Method 3: legacy trusted.gpg.d fallback (works on all Debian)
if [[ "$KEY_OK" != "true" ]]; then
  echo "Keyring methods failed, trying legacy trusted.gpg.d…"
  apt-key adv --keyserver hkp://keyserver.ubuntu.com:80 \
    --recv-keys 97A9DF822081EC2A 6F7703F65FA1BDAF 2>/dev/null || true
fi

if [[ "$KEY_OK" == "true" ]]; then
  echo "GPG key installed: $FR24_KEY"
else
  echo "Warning: GPG key import may have failed — apt-get update will show if there's a problem."
fi

# ── APT source list ──
FR24_LIST="/etc/apt/sources.list.d/fr24feed.list"
if [[ -f "$FR24_KEY" && -s "$FR24_KEY" ]]; then
  echo "deb [signed-by=${FR24_KEY}] https://repo-feed.flightradar24.com flightradar24 raspberrypi-stable" > "$FR24_LIST"
else
  echo "deb https://repo-feed.flightradar24.com flightradar24 raspberrypi-stable" > "$FR24_LIST"
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
