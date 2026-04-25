#!/bin/bash
# Install dump1090-fa (FlightAware's fork) on Debian/Raspbian.
#
# Called by skytrack-installer wrapper — runs as root.
# Idempotent: re-running is safe and updates to latest available version.
#
# Trixie (Debian 13) note: Debian switched from gpgv to sqv for APT
# signature verification and rejects SHA1 signatures since Feb 2026.
# FlightAware's repo key still uses SHA1, so we temporarily allow it
# during install, then remove the override.

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

# ── Trixie SHA1 workaround ──
# Debian 13 (Trixie) uses sqv which rejects SHA1 signatures by default.
# FlightAware's GPG key still uses SHA1. Temporarily allow it for install.
SHA1_POLICY="/etc/apt/apt.conf.d/99-flightaware-sha1"
REAL_CODENAME="$(lsb_release -cs 2>/dev/null || true)"
if [[ "$REAL_CODENAME" == "trixie" ]] || [[ -x /usr/bin/sqv ]]; then
  echo "Trixie detected — temporarily allowing SHA1 signatures for FlightAware repo…"
  cat > "$SHA1_POLICY" <<'APTCONF'
APT::Key::gpgvcommand "/usr/bin/gpgv";
APTCONF
fi

# ── GPG key via the official repo .deb ──
# Per https://www.flightaware.com/adsb/piaware/install
if ! dpkg -s flightaware-apt-repository >/dev/null 2>&1; then
  echo "Installing FlightAware repository package…"
  FA_DEB=""
  for path in \
    "pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.2_all.deb" \
    "pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.1_all.deb" \
    "pool/all/f/flightaware-apt-repository/flightaware-apt-repository_1.2_all.deb" \
    "pool/all/f/flightaware-apt-repository/flightaware-apt-repository_1.1_all.deb"; do
    url="https://www.flightaware.com/adsb/piaware/files/packages/${path}"
    if curl -fsSL "$url" -o /tmp/fa-repo.deb 2>/dev/null; then
      FA_DEB="/tmp/fa-repo.deb"
      echo "  downloaded: ${path}"
      break
    fi
  done
  if [[ -n "$FA_DEB" ]]; then
    dpkg -i "$FA_DEB" && rm -f "$FA_DEB" \
      || echo "Warning: dpkg -i of FA repo package failed"
  else
    echo "Warning: could not download FlightAware repo package"
  fi
fi

# ── Write the correct sources.list entry ──
# The .deb's postinst may regenerate this file, but it uses the native
# codename which won't exist on Trixie. Always overwrite with the correct
# format verified against the live Release file:
#   URL:       https://www.flightaware.com/adsb/piaware/files/packages
#   Dist:      bookworm  (no codename in URL path)
#   Component: piaware
FA_LIST="/etc/apt/sources.list.d/flightaware-apt-repository.list"
FA_LINE="deb [signed-by=/usr/share/keyrings/flightaware-archive-keyring.gpg] https://www.flightaware.com/adsb/piaware/files/packages ${CODENAME} piaware"

echo "Setting FlightAware repo: ${CODENAME} / piaware"
echo "$FA_LINE" > "$FA_LIST"

# Remove stale list files from prior installer versions
for stale in /etc/apt/sources.list.d/flightaware.list; do
  [[ -f "$stale" ]] && rm -f "$stale" && echo "Removed stale $stale"
done

# Fix Cloudflare repo if it references an unsupported codename
CF_LIST="/etc/apt/sources.list.d/cloudflared.list"
if [[ -f "$CF_LIST" ]] && grep -q 'trixie\|jammy\|noble' "$CF_LIST" 2>/dev/null; then
  echo "Fixing Cloudflare repo codename to bookworm…"
  sed -i "s/trixie/bookworm/g; s/jammy/bookworm/g; s/noble/bookworm/g" "$CF_LIST"
fi

echo "Updating package lists…"
apt-get update

echo "Installing dump1090-fa…"
DEBIAN_FRONTEND=noninteractive apt-get install -y dump1090-fa

# ── Clean up SHA1 workaround ──
if [[ -f "$SHA1_POLICY" ]]; then
  echo "Removing temporary SHA1 signature workaround…"
  rm -f "$SHA1_POLICY"
fi

echo "Enabling and starting dump1090-fa service…"
systemctl enable dump1090-fa || true
systemctl restart dump1090-fa || true

if systemctl is-active --quiet dump1090-fa; then
  echo "dump1090-fa is running."
else
  echo "Warning: dump1090-fa installed but service not yet active."
fi

echo "=== dump1090-fa installation complete ==="
