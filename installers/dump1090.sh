#!/bin/bash
# Install dump1090-fa (FlightAware's fork) on Debian/Raspbian.
#
# Called by skytrack-installer wrapper — runs as root.
# Idempotent: re-running is safe and updates to latest available version.

set -euo pipefail

echo "=== dump1090-fa installer ==="

if dpkg -s dump1090-fa >/dev/null 2>&1; then
  echo "dump1090-fa is already installed, upgrading if available…"
fi

# Resolve supported codename — FlightAware publishes repos for
# buster, bullseye, and bookworm. Fall back to bookworm for anything else.
CODENAME="$(lsb_release -cs 2>/dev/null || echo bookworm)"
case "$CODENAME" in
  buster|bullseye|bookworm) ;;
  *) echo "Codename '$CODENAME' not in FlightAware repo, using bookworm."
     CODENAME="bookworm" ;;
esac

# ── GPG key via the official repo .deb ──
# Try both known pool paths; FlightAware has moved files between them.
if ! dpkg -s flightaware-apt-repository >/dev/null 2>&1; then
  echo "Installing FlightAware GPG key…"
  FA_DEB=""
  for path in \
    "pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.2_all.deb" \
    "pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.1_all.deb" \
    "pool/all/f/flightaware-apt-repository/flightaware-apt-repository_1.2_all.deb" \
    "pool/all/f/flightaware-apt-repository/flightaware-apt-repository_1.1_all.deb"; do
    url="https://www.flightaware.com/adsb/piaware/files/packages/${path}"
    if curl -fsSL "$url" -o /tmp/fa-repo.deb 2>/dev/null; then
      FA_DEB="/tmp/fa-repo.deb"
      break
    fi
  done
  if [[ -n "$FA_DEB" ]]; then
    dpkg -i "$FA_DEB" && rm -f "$FA_DEB" \
      || echo "Warning: dpkg -i of FA repo package failed"
  else
    echo "Warning: could not download FlightAware repo package (GPG key)"
  fi
fi

# ── Write the correct sources.list entry ──
# Correct format (verified against the live Release file):
#   URL:       https://www.flightaware.com/adsb/piaware/files/packages
#   Dist:      bookworm  (no codename in the URL path!)
#   Component: piaware   (not "flightaware")
FA_LIST="/etc/apt/sources.list.d/flightaware-apt-repository.list"
FA_LINE="deb https://www.flightaware.com/adsb/piaware/files/packages ${CODENAME} piaware"

echo "Setting FlightAware repo: ${FA_LINE}"
echo "$FA_LINE" > "$FA_LIST"

# Remove stale list files from prior installer versions
for stale in \
  /etc/apt/sources.list.d/flightaware.list; do
  [[ -f "$stale" ]] && rm -f "$stale" && echo "Removed stale $stale"
done

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

if systemctl is-active --quiet dump1090-fa; then
  echo "dump1090-fa is running."
else
  echo "Warning: dump1090-fa installed but service not yet active."
fi

echo "=== dump1090-fa installation complete ==="
