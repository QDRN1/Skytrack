#!/usr/bin/env bash
# scripts/setup_tunnel.sh — create and run a Cloudflare Tunnel from the device
#
# Usage:
#   sudo bash scripts/setup_tunnel.sh <tunnel-name> <hostname>
#
# Example:
#   sudo bash scripts/setup_tunnel.sh skytrack-baycity skytrack-baycity.qdrn.com
#
# What this script does:
#   1. Authenticates with Cloudflare (opens a URL you visit on your phone)
#   2. Creates a named tunnel on the device
#   3. Writes the cloudflared config to route <hostname> → localhost:8080
#   4. Adds a DNS CNAME record for the hostname
#   5. Installs and starts cloudflared as a systemd service
#
# After this runs, the device is reachable at https://<hostname> from anywhere.
#
# Alternative — if you already have a connector token from the CF dashboard:
#   sudo bash scripts/setup_tunnel.sh --token <token>

set -euo pipefail

TUNNEL_NAME="${1:-}"
HOSTNAME="${2:-}"
TOKEN_MODE=false
TOKEN=""

# Handle --token mode (legacy / dashboard-created tunnels)
if [[ "$TUNNEL_NAME" == "--token" ]]; then
  TOKEN_MODE=true
  TOKEN="${HOSTNAME:-}"
  if [[ -z "$TOKEN" ]]; then
    echo "Usage: sudo bash $0 --token <connector-token>"
    exit 1
  fi
fi

if [[ "$TOKEN_MODE" == false ]] && [[ -z "$TUNNEL_NAME" ]]; then
  echo "Usage: sudo bash $0 <tunnel-name> <hostname>"
  echo ""
  echo "Examples:"
  echo "  sudo bash $0 skytrack-baycity skytrack-baycity.qdrn.com"
  echo "  sudo bash $0 --token eyJhIjoiNjQ3..."
  echo ""
  echo "The first form creates the tunnel from scratch (recommended)."
  echo "The --token form uses a pre-created token from the CF dashboard."
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root (sudo)."
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "[tunnel] cloudflared not installed. Downloading binary..."
  ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
  case "$ARCH" in
    arm64|aarch64) CF_ARCH="linux-arm64" ;;
    armhf|armv7l)  CF_ARCH="linux-arm"   ;;
    amd64|x86_64)  CF_ARCH="linux-amd64" ;;
    *)             CF_ARCH="linux-amd64"  ;;
  esac
  CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-${CF_ARCH}"
  echo "[tunnel] downloading $CF_URL"
  curl -fsSL -o /usr/local/bin/cloudflared "$CF_URL"
  chmod +x /usr/local/bin/cloudflared
  if ! command -v cloudflared >/dev/null 2>&1; then
    echo "[tunnel] FATAL: cloudflared install failed."
    exit 1
  fi
fi

echo "[tunnel] cloudflared version: $(cloudflared --version 2>/dev/null | head -1)"

# Stop any existing service
echo "[tunnel] stopping existing cloudflared service (if any)..."
systemctl stop cloudflared 2>/dev/null || true
systemctl disable cloudflared 2>/dev/null || true
# Clean up leftover service file from token-mode installs
rm -f /etc/systemd/system/cloudflared.service
systemctl daemon-reload 2>/dev/null || true

REPO_DIR="${REPO_DIR:-/opt/skytrack}"
CONFIG_FILE="$REPO_DIR/config.yaml"

# =========================================================================
# Token mode — simple: just install and run with the dashboard token
# =========================================================================
if [[ "$TOKEN_MODE" == true ]]; then
  echo "[tunnel] installing with connector token..."
  cloudflared service install "$TOKEN"
  systemctl daemon-reload
  systemctl enable cloudflared
  systemctl start cloudflared
  mkdir -p /etc/skytrack
  echo "$TOKEN" > /etc/skytrack/tunnel_token
  chmod 0600 /etc/skytrack/tunnel_token
  echo "[tunnel] token mode — tunnel running. Configure hostname in the CF dashboard."
  exit 0
fi

# =========================================================================
# Device-managed mode — full flow: login → create → config → DNS → run
# =========================================================================

CF_DIR="/root/.cloudflared"
CRED_DIR="/etc/cloudflared"
CF_CONFIG="$CRED_DIR/config.yml"

mkdir -p "$CRED_DIR"

# --- Step 1: Authenticate ---
# This prints a URL. The user opens it in any browser, picks their domain,
# and cloudflared gets a cert.pem written to $CF_DIR.
if [[ ! -f "$CF_DIR/cert.pem" ]]; then
  echo ""
  echo "================================================================"
  echo " CLOUDFLARE LOGIN REQUIRED"
  echo ""
  echo " cloudflared will print a URL below. Open it on your phone or"
  echo " any browser, log in to Cloudflare, and authorize this device."
  echo " The script will continue automatically once you approve."
  echo "================================================================"
  echo ""
  cloudflared tunnel login
  if [[ ! -f "$CF_DIR/cert.pem" ]]; then
    echo "[tunnel] FATAL: login did not produce cert.pem. Try again."
    exit 1
  fi
  echo "[tunnel] authenticated successfully."
else
  echo "[tunnel] already authenticated (cert.pem exists)."
fi

# --- Step 2: Create the tunnel ---
# If a tunnel with this name already exists, cloudflared returns non-zero
# but prints the UUID. We capture it either way.
echo "[tunnel] creating tunnel: $TUNNEL_NAME"
TUNNEL_UUID=""
CREATE_OUTPUT=$(cloudflared tunnel create "$TUNNEL_NAME" 2>&1) || true
echo "$CREATE_OUTPUT"

# Extract UUID from output. cloudflared prints it in various formats:
#   "Created tunnel <name> with id <uuid>"
#   "A]tunnel with name <name> already exists. ID: <uuid>"
TUNNEL_UUID=$(echo "$CREATE_OUTPUT" | grep -oP '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)

if [[ -z "$TUNNEL_UUID" ]]; then
  # Try listing to find it
  TUNNEL_UUID=$(cloudflared tunnel list -o json 2>/dev/null \
    | python3 -c "import sys,json; tunnels=json.load(sys.stdin); print(next((t['id'] for t in tunnels if t['name']=='$TUNNEL_NAME'),''))" 2>/dev/null || true)
fi

if [[ -z "$TUNNEL_UUID" ]]; then
  echo "[tunnel] FATAL: could not determine tunnel UUID."
  exit 1
fi

echo "[tunnel] tunnel UUID: $TUNNEL_UUID"

# --- Step 3: Find the credentials file ---
CRED_FILE="$CF_DIR/$TUNNEL_UUID.json"
if [[ ! -f "$CRED_FILE" ]]; then
  echo "[tunnel] WARNING: credentials file not at $CRED_FILE"
  # Try alternative location
  CRED_FILE="$CRED_DIR/$TUNNEL_UUID.json"
fi
# Copy to the service directory if not already there
if [[ -f "$CF_DIR/$TUNNEL_UUID.json" ]] && [[ ! -f "$CRED_DIR/$TUNNEL_UUID.json" ]]; then
  cp "$CF_DIR/$TUNNEL_UUID.json" "$CRED_DIR/$TUNNEL_UUID.json"
fi
CRED_FILE="$CRED_DIR/$TUNNEL_UUID.json"
echo "[tunnel] credentials: $CRED_FILE"

# --- Step 4: Write the config ---
cat > "$CF_CONFIG" <<CFEOF
tunnel: $TUNNEL_UUID
credentials-file: $CRED_FILE

ingress:
  - hostname: $HOSTNAME
    service: http://localhost:8080
  - service: http_status:404
CFEOF
echo "[tunnel] config written to $CF_CONFIG"
cat "$CF_CONFIG"

# --- Step 5: Route DNS ---
if [[ -n "$HOSTNAME" ]]; then
  echo "[tunnel] adding DNS route: $HOSTNAME → $TUNNEL_UUID"
  cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME" 2>&1 || \
    echo "[tunnel] DNS route may already exist (that's fine)."
fi

# --- Step 6: Install as systemd service ---
echo "[tunnel] installing systemd service..."

cat > /etc/systemd/system/cloudflared.service <<SVCEOF
[Unit]
Description=Cloudflare Tunnel — $TUNNEL_NAME
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$(command -v cloudflared) tunnel --config $CF_CONFIG run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable cloudflared
systemctl start cloudflared

# --- Step 7: Persist to SkyTrack config ---
mkdir -p /etc/skytrack
echo "$TUNNEL_UUID" > /etc/skytrack/tunnel_id
chmod 0600 /etc/skytrack/tunnel_id

if [[ -f "$CONFIG_FILE" ]]; then
  for key_val in "cloudflared_enabled: true" "cloudflared_tunnel_name: '$TUNNEL_NAME'" "cloudflared_hostname: '$HOSTNAME'"; do
    key="${key_val%%:*}"
    if grep -q "$key" "$CONFIG_FILE"; then
      sed -i "s|${key}:.*|${key_val}|" "$CONFIG_FILE"
    else
      echo "$key_val" >> "$CONFIG_FILE"
    fi
  done
  echo "[tunnel] updated config.yaml"
fi

# --- Done ---
sleep 2
echo ""
echo "================================================================"
if systemctl is-active cloudflared >/dev/null 2>&1; then
  echo " TUNNEL IS RUNNING!"
  echo ""
  echo " Name:     $TUNNEL_NAME"
  echo " UUID:     $TUNNEL_UUID"
  echo " Hostname: https://$HOSTNAME"
  echo ""
  echo " Your device is now reachable from anywhere at:"
  echo "   https://$HOSTNAME"
  echo ""
  echo " To check status:  sudo systemctl status cloudflared"
  echo " To view logs:     sudo journalctl -u cloudflared -f"
  echo " To stop:          sudo systemctl stop cloudflared"
else
  echo " TUNNEL INSTALLED but service may not be running yet."
  echo ""
  echo " Check: sudo systemctl status cloudflared"
  echo " Logs:  sudo journalctl -u cloudflared --no-pager -n 30"
fi
echo "================================================================"
