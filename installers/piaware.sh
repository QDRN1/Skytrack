#!/bin/bash
# Install piaware (FlightAware feeder) on Debian/Raspbian.
#
# Called by skytrack-installer wrapper — runs as root.
# Idempotent: re-running is safe and updates to latest available version.
#
# On Trixie (Debian 13) builds from FlightAware source (same approach
# as dump1090-fa). On Bookworm and older uses the APT repo.

set -euo pipefail

echo "=== piaware installer ==="

if dpkg -s piaware >/dev/null 2>&1; then
  echo "piaware is already installed."
  CURRENT_VER="$(dpkg -s piaware | grep '^Version:' | awk '{print $2}')"
  echo "  current version: ${CURRENT_VER}"
fi

REAL_CODENAME="$(lsb_release -cs 2>/dev/null || echo bookworm)"

# ── Trixie: build from source ──
if [[ "$REAL_CODENAME" == "trixie" ]]; then
  echo "Trixie detected — building piaware from FlightAware source…"

  echo "Installing build dependencies…"
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential debhelper dh-sysuser \
    tcl8.6-dev tclx8.4 tcllib itcl3 \
    libboost-system-dev libboost-filesystem-dev libboost-program-options-dev \
    net-tools iproute2 procps \
    git patchelf \
    2>&1

  BUILD_DIR="/tmp/piaware-build-$$"
  rm -rf "$BUILD_DIR"

  echo "Cloning FlightAware piaware (dev branch)…"
  git clone --depth 1 -b dev https://github.com/flightaware/piaware_builder "$BUILD_DIR"

  cd "$BUILD_DIR"

  echo "Preparing Trixie build…"
  if [[ -f ./sensible-build.sh ]]; then
    bash ./sensible-build.sh trixie 2>&1
  elif [[ -f ./prepare-build.sh ]]; then
    bash ./prepare-build.sh trixie 2>&1
  else
    echo "ERROR: No build preparation script found in piaware_builder"
    exit 1
  fi

  if [[ -d package-trixie ]]; then
    cd package-trixie
  elif [[ -d package-bookworm ]]; then
    cd package-bookworm
  fi

  echo "Building .deb package (this may take several minutes)…"
  dpkg-buildpackage -b --no-sign 2>&1 || true

  cd "$BUILD_DIR"

  echo "Installing built packages…"
  dpkg -i piaware_*_*.deb 2>/dev/null || true
  dpkg -i piaware-web_*_*.deb 2>/dev/null || true
  apt-get install -f -y 2>&1

  rm -rf "$BUILD_DIR"

  echo "Enabling and starting piaware service…"
  systemctl enable piaware || true
  systemctl restart piaware || true

  if systemctl is-active --quiet piaware; then
    echo "piaware is running."
  else
    echo "Warning: piaware installed but service not yet active."
  fi

  echo "=== piaware installation complete ==="
  exit 0
fi

# ── Bookworm / Bullseye / Buster: use APT repo ──
CODENAME="$REAL_CODENAME"
case "$CODENAME" in
  buster|bullseye|bookworm) ;;
  *) CODENAME="bookworm" ;;
esac

# Ensure FlightAware APT repo is configured (dump1090 installer sets this up,
# but piaware may be installed independently).
FA_LIST="/etc/apt/sources.list.d/flightaware-apt-repository.list"
if [[ ! -f "$FA_LIST" ]] || ! grep -q 'piaware' "$FA_LIST" 2>/dev/null; then
  if ! dpkg -s flightaware-apt-repository >/dev/null 2>&1; then
    echo "Installing FlightAware repository package…"
    for path in \
      "pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.2_all.deb" \
      "pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.1_all.deb"; do
      url="https://www.flightaware.com/adsb/piaware/files/packages/${path}"
      if curl -fsSL "$url" -o /tmp/fa-repo.deb 2>/dev/null; then
        dpkg -i /tmp/fa-repo.deb && rm -f /tmp/fa-repo.deb
        break
      fi
    done
  fi

  FA_LINE="deb [signed-by=/usr/share/keyrings/flightaware-archive-keyring.gpg] https://www.flightaware.com/adsb/piaware/files/packages ${CODENAME} piaware"
  echo "$FA_LINE" > "$FA_LIST"
fi

echo "Updating package lists…"
apt-get update

echo "Installing piaware…"
DEBIAN_FRONTEND=noninteractive apt-get install -y piaware

echo "Installing piaware-web (optional)…"
DEBIAN_FRONTEND=noninteractive apt-get install -y piaware-web 2>/dev/null || true

echo "Enabling and starting piaware service…"
systemctl enable piaware || true
systemctl restart piaware || true

if systemctl is-active --quiet piaware; then
  echo "piaware is running."
else
  echo "Warning: piaware installed but service not yet active."
fi

echo "=== piaware installation complete ==="
