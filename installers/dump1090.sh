#!/bin/bash
# Install dump1090-fa (FlightAware's fork) on Debian/Raspbian.
#
# Called by skytrack-installer wrapper — runs as root.
# Idempotent: re-running is safe and updates to latest available version.
#
# On Trixie (Debian 13) the Bookworm binary packages have unmet library
# dependencies (liblimesuite22.09 vs 23.x). We build from source using
# FlightAware's own prepare-build.sh, which is the community-recommended
# approach. On Bookworm and older we use the APT repo as normal.

set -euo pipefail

echo "=== dump1090-fa installer ==="

if dpkg -s dump1090-fa >/dev/null 2>&1; then
  echo "dump1090-fa is already installed."
  CURRENT_VER="$(dpkg -s dump1090-fa | grep '^Version:' | awk '{print $2}')"
  echo "  current version: ${CURRENT_VER}"
fi

REAL_CODENAME="$(lsb_release -cs 2>/dev/null || echo bookworm)"

# ── Trixie: build from source ──
if [[ "$REAL_CODENAME" == "trixie" ]]; then
  echo "Trixie detected — building dump1090-fa from FlightAware source…"

  echo "Installing build dependencies…"
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential debhelper dh-sysuser \
    librtlsdr-dev libbladerf-dev libhackrf-dev \
    liblimesuite-dev libsoapysdr-dev \
    libusb-1.0-0-dev libncurses-dev \
    pkg-config git lighttpd \
    2>&1

  BUILD_DIR="/tmp/dump1090-build-$$"
  rm -rf "$BUILD_DIR"

  echo "Cloning FlightAware dump1090 (dev branch)…"
  git clone --depth 1 -b dev https://github.com/flightaware/dump1090 "$BUILD_DIR"

  cd "$BUILD_DIR"

  echo "Preparing Trixie build…"
  bash ./prepare-build.sh trixie

  cd package-trixie

  echo "Building .deb package (this may take a few minutes)…"
  dpkg-buildpackage -b --no-sign 2>&1

  cd "$BUILD_DIR"

  echo "Installing built package…"
  dpkg -i dump1090-fa_*_*.deb || true
  apt-get install -f -y 2>&1

  # Clean up
  rm -rf "$BUILD_DIR"

  echo "Enabling and starting dump1090-fa service…"
  systemctl enable dump1090-fa || true
  systemctl restart dump1090-fa || true

  if systemctl is-active --quiet dump1090-fa; then
    echo "dump1090-fa is running."
  else
    echo "Warning: dump1090-fa installed but service not yet active."
  fi

  echo "=== dump1090-fa installation complete ==="
  exit 0
fi

# ── Bookworm / Bullseye / Buster: use APT repo ──
CODENAME="$REAL_CODENAME"
case "$CODENAME" in
  buster|bullseye|bookworm) ;;
  *) CODENAME="bookworm" ;;
esac

# GPG key via the official repo .deb
if ! dpkg -s flightaware-apt-repository >/dev/null 2>&1; then
  echo "Installing FlightAware repository package…"
  FA_DEB=""
  for path in \
    "pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.2_all.deb" \
    "pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.1_all.deb"; do
    url="https://www.flightaware.com/adsb/piaware/files/packages/${path}"
    if curl -fsSL "$url" -o /tmp/fa-repo.deb 2>/dev/null; then
      FA_DEB="/tmp/fa-repo.deb"
      break
    fi
  done
  if [[ -n "$FA_DEB" ]]; then
    dpkg -i "$FA_DEB" && rm -f "$FA_DEB" \
      || echo "Warning: dpkg -i of FA repo package failed"
  fi
fi

FA_LIST="/etc/apt/sources.list.d/flightaware-apt-repository.list"
FA_LINE="deb [signed-by=/usr/share/keyrings/flightaware-archive-keyring.gpg] https://www.flightaware.com/adsb/piaware/files/packages ${CODENAME} piaware"
echo "Setting FlightAware repo: ${CODENAME} / piaware"
echo "$FA_LINE" > "$FA_LIST"

for stale in /etc/apt/sources.list.d/flightaware.list; do
  [[ -f "$stale" ]] && rm -f "$stale"
done

echo "Updating package lists…"
apt-get update

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
