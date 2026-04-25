#!/bin/bash
# Install dump1090-fa (FlightAware's fork) on Debian/Raspbian.
#
# Called by skytrack-installer wrapper — runs as root.
# Idempotent: re-running is safe and updates to latest available version.

set -euo pipefail

echo "=== dump1090-fa installer ==="

# Check if already installed
if dpkg -s dump1090-fa >/dev/null 2>&1; then
  echo "dump1090-fa is already installed, upgrading if available…"
fi

# Add FlightAware repo if not present
FA_LIST="/etc/apt/sources.list.d/flightaware.list"
if [[ ! -f "$FA_LIST" ]]; then
  echo "Adding FlightAware APT repository…"
  # Detect distribution
  CODENAME="$(lsb_release -cs 2>/dev/null || echo bookworm)"
  echo "deb http://flightaware.com/adsb/piaware/files/packages/${CODENAME} ${CODENAME} flightaware" \
    > "$FA_LIST"

  # Import GPG key
  KEYRING="/usr/share/keyrings/flightaware-archive-keyring.gpg"
  if [[ ! -f "$KEYRING" ]]; then
    curl -fsSL https://flightaware.com/adsb/piaware/files/packages/pool/all/f/flightaware-apt-repository/flightaware-apt-repository_1.2_all.deb \
      -o /tmp/fa-repo.deb && dpkg -i /tmp/fa-repo.deb && rm -f /tmp/fa-repo.deb \
      || echo "Warning: could not add FlightAware GPG key automatically"
  fi
fi

echo "Updating package lists…"
apt-get update -qq

echo "Installing dump1090-fa…"
DEBIAN_FRONTEND=noninteractive apt-get install -y dump1090-fa

echo "Enabling and starting dump1090-fa service…"
systemctl enable dump1090-fa || true
systemctl restart dump1090-fa || true

# Verify
if systemctl is-active --quiet dump1090-fa; then
  echo "dump1090-fa is running."
else
  echo "Warning: dump1090-fa installed but service not yet active."
fi

echo "=== dump1090-fa installation complete ==="
