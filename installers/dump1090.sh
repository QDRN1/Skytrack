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

# Resolve supported codename — FlightAware only publishes repos for
# buster, bullseye, and bookworm. Fall back to bookworm for anything else.
CODENAME="$(lsb_release -cs 2>/dev/null || echo bookworm)"
case "$CODENAME" in
  buster|bullseye|bookworm) ;;
  *) echo "Codename '$CODENAME' not supported by FlightAware repo, using bookworm."
     CODENAME="bookworm" ;;
esac

# Install the FlightAware repo package for GPG keys (if not already present).
# The .deb creates its own .list file, but we overwrite it below with the
# correct HTTPS URL and codename.
if ! dpkg -s flightaware-apt-repository >/dev/null 2>&1; then
  echo "Installing FlightAware APT repository package…"
  curl -fsSL "https://flightaware.com/adsb/piaware/files/packages/pool/all/f/flightaware-apt-repository/flightaware-apt-repository_1.2_all.deb" \
    -o /tmp/fa-repo.deb \
    && dpkg -i /tmp/fa-repo.deb \
    && rm -f /tmp/fa-repo.deb \
    || echo "Warning: could not install FlightAware repo package"
fi

# Write the repo source with HTTPS (HTTP is broken/unsigned) and the
# correct codename. Always overwrite to fix stale entries from prior runs.
FA_LIST="/etc/apt/sources.list.d/flightaware-apt-repository.list"
FA_LIST_ALT="/etc/apt/sources.list.d/flightaware.list"
FA_LINE="deb https://flightaware.com/adsb/piaware/files/packages/${CODENAME} ${CODENAME} flightaware"

echo "Setting FlightAware repo → HTTPS / ${CODENAME}"
echo "$FA_LINE" > "$FA_LIST"
# Remove any stale alternate-name list file from prior installer versions
[[ -f "$FA_LIST_ALT" ]] && rm -f "$FA_LIST_ALT"

# Fix Cloudflare repo if it references an unsupported codename
CF_LIST="/etc/apt/sources.list.d/cloudflared.list"
if [[ -f "$CF_LIST" ]] && grep -q 'trixie\|jammy\|noble' "$CF_LIST" 2>/dev/null; then
  echo "Fixing Cloudflare repo codename to bookworm…"
  sed -i "s/trixie/bookworm/g; s/jammy/bookworm/g; s/noble/bookworm/g" "$CF_LIST"
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
