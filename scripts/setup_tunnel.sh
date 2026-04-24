#!/usr/bin/env bash
# scripts/setup_tunnel.sh — install and configure a Cloudflare Tunnel
#
# Usage:
#   sudo bash scripts/setup_tunnel.sh <connector-token>
#
# The connector token comes from the Cloudflare Zero Trust dashboard:
#   Zero Trust → Networks → Tunnels → Create → Cloudflared → copy token
#
# What this script does:
#   1. Validates cloudflared is installed
#   2. Stops any existing cloudflared service
#   3. Installs the tunnel as a systemd service using the token
#   4. Enables and starts the service
#   5. Writes the token to /etc/skytrack/tunnel_token for persistence
#   6. Updates SkyTrack config to set cloudflared_enabled: true
#
# The tunnel routes traffic from your Cloudflare domain to
# http://localhost:8080 on the device. Configure the public hostname
# in the Cloudflare dashboard (e.g. skytrack-baycity.yourdomain.com).

set -euo pipefail

TOKEN="${1:-}"

if [[ -z "$TOKEN" ]]; then
  echo "Usage: sudo bash $0 <connector-token>"
  echo ""
  echo "Get the token from:"
  echo "  Cloudflare Zero Trust → Networks → Tunnels → Create"
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root (sudo)."
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed. Run install.sh first or install manually:"
  echo "  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg"
  echo "  echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared \$(lsb_release -cs) main' > /etc/apt/sources.list.d/cloudflared.list"
  echo "  apt-get update && apt-get install cloudflared"
  exit 1
fi

echo "[tunnel] stopping existing cloudflared service (if any)..."
systemctl stop cloudflared 2>/dev/null || true
systemctl disable cloudflared 2>/dev/null || true

# cloudflared service install creates /etc/systemd/system/cloudflared.service
# and writes the token into the unit file's ExecStart.
echo "[tunnel] installing cloudflared service with token..."
cloudflared service install "$TOKEN"

echo "[tunnel] enabling and starting cloudflared..."
systemctl daemon-reload
systemctl enable cloudflared
systemctl start cloudflared

# Persist the token so we can reinstall/update without re-entering it
mkdir -p /etc/skytrack
echo "$TOKEN" > /etc/skytrack/tunnel_token
chmod 0600 /etc/skytrack/tunnel_token
echo "[tunnel] token saved to /etc/skytrack/tunnel_token"

# Update SkyTrack config
REPO_DIR="${REPO_DIR:-/opt/skytrack}"
CONFIG_FILE="$REPO_DIR/config.yaml"
if [[ -f "$CONFIG_FILE" ]]; then
  if grep -q 'cloudflared_enabled' "$CONFIG_FILE"; then
    sed -i 's/cloudflared_enabled:.*/cloudflared_enabled: true/' "$CONFIG_FILE"
  else
    echo "cloudflared_enabled: true" >> "$CONFIG_FILE"
  fi
  echo "[tunnel] updated config.yaml: cloudflared_enabled: true"
fi

# Quick health check
sleep 2
if systemctl is-active cloudflared >/dev/null 2>&1; then
  echo ""
  echo "[tunnel] cloudflared is running!"
  echo ""
  echo "Next steps:"
  echo "  1. Go to Cloudflare Zero Trust → Networks → Tunnels"
  echo "  2. Click on your tunnel → Public Hostname → Add"
  echo "  3. Set:"
  echo "       Subdomain: skytrack-baycity (or whatever you want)"
  echo "       Domain:    yourdomain.com"
  echo "       Service:   HTTP://localhost:8080"
  echo "  4. Save. Your device is now reachable at https://skytrack-baycity.yourdomain.com"
  echo ""
  cloudflared --version 2>/dev/null || true
else
  echo "[tunnel] WARNING: cloudflared service did not start. Check:"
  echo "  sudo systemctl status cloudflared"
  echo "  sudo journalctl -u cloudflared --no-pager -n 30"
fi
