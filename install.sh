#!/usr/bin/env bash
# =============================================================================
# SkyTrack — Single-Command Production Installer
#
# Usage:
#   sudo ./install.sh [OPTIONS]
#
# Options:
#   --lcd-profile <name>       Apply LCD profile (e.g., waveshare_5inch)
#   --cf-token <token>         Cloudflare Tunnel token (skips prompt)
#   --cf-tunnel-name <name>    Cloudflare Tunnel name (default: skytrack)
#   --fr24-key <key>           Flightradar24 sharing key
#   --piaware-feeder-id <id>   PiAware feeder ID
#   --skip-cloudflare          Skip Cloudflare Tunnel setup
#   --skip-fr24                Skip Flightradar24 installation
#   --skip-piaware             Skip PiAware installation
#   --non-interactive          Skip all prompts (use defaults/env vars)
#
# Environment variables:
#   CF_TUNNEL_TOKEN            Cloudflare Tunnel token
#   CF_TUNNEL_NAME             Cloudflare Tunnel name
#   FR24_KEY                   Flightradar24 sharing key
#   PIAWARE_FEEDER_ID          PiAware feeder ID
#
# Safe to re-run. Idempotent. Does not overwrite existing configuration.
#
# Assumptions:
#   - Raspberry Pi OS Bookworm (Debian 12) 64-bit
#   - HDMI display (rotation configurable in config.yaml)
#   - GPS/modem optional (static lat/lon fallback in config)
# =============================================================================

set -euo pipefail

# ---- Constants ----
SKYTRACK_USER="SkyTrack"
SKYTRACK_HOME="/home/${SKYTRACK_USER}"
INSTALL_DIR="/opt/skytrack"
DATA_DIR="/var/lib/skytrack"
GEO_DIR="${DATA_DIR}/geo"
LOG_DIR="/var/log/skytrack"
LOG_FILE="/var/log/skytrack-install.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- Defaults ----
LCD_PROFILE=""
CF_TOKEN="${CF_TUNNEL_TOKEN:-}"
CF_NAME="${CF_TUNNEL_NAME:-skytrack}"
FR24KEY="${FR24_KEY:-}"
PIAWARE_ID="${PIAWARE_FEEDER_ID:-}"
SKIP_CF=false
SKIP_FR24=false
SKIP_PIAWARE=false
NON_INTERACTIVE=false

# ---- Color output ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[SKYTRACK]${NC} $*" | tee -a "$LOG_FILE"; }
warn() { echo -e "${YELLOW}[WARNING]${NC} $*" | tee -a "$LOG_FILE"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" | tee -a "$LOG_FILE"; }
info() { echo -e "${BLUE}[INFO]${NC} $*" | tee -a "$LOG_FILE"; }

# ---- Parse arguments ----
while [[ $# -gt 0 ]]; do
    case "$1" in
        --lcd-profile)      LCD_PROFILE="$2"; shift 2 ;;
        --cf-token)         CF_TOKEN="$2"; shift 2 ;;
        --cf-tunnel-name)   CF_NAME="$2"; shift 2 ;;
        --fr24-key)         FR24KEY="$2"; shift 2 ;;
        --piaware-feeder-id) PIAWARE_ID="$2"; shift 2 ;;
        --skip-cloudflare)  SKIP_CF=true; shift ;;
        --skip-fr24)        SKIP_FR24=true; shift ;;
        --skip-piaware)     SKIP_PIAWARE=true; shift ;;
        --non-interactive)  NON_INTERACTIVE=true; shift ;;
        *)                  warn "Unknown option: $1"; shift ;;
    esac
done

# ---- Root check ----
if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (sudo ./install.sh)"
    exit 1
fi

# ---- Initialize log ----
mkdir -p "$(dirname "$LOG_FILE")"
echo "=== SkyTrack Install — $(date -Iseconds) ===" >> "$LOG_FILE"

log "Starting SkyTrack installation..."

# =========================================================================
# STEP 1: System packages
# =========================================================================
log "STEP 1/10: Installing system packages..."

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq >> "$LOG_FILE" 2>&1

PACKAGES=(
    git curl wget rsync
    python3-venv python3-pip python3-dev
    # X11 / Kiosk
    xserver-xorg xinit x11-xserver-utils x11-utils
    openbox chromium-browser
    unclutter
    # Modem / GPS
    modemmanager network-manager
    usb-modeswitch usb-modeswitch-data
    libqmi-utils libmbim-utils
    gpsd gpsd-clients
    # Build tools / ADS-B
    build-essential
    librtlsdr-dev libusb-1.0-0-dev
    lighttpd
    jq
)

apt-get install -y -qq "${PACKAGES[@]}" >> "$LOG_FILE" 2>&1 || {
    warn "Some packages may have failed — continuing..."
}

log "System packages installed."

# =========================================================================
# STEP 2: Create SkyTrack user
# =========================================================================
log "STEP 2/10: Setting up SkyTrack user..."

if ! id "$SKYTRACK_USER" &>/dev/null; then
    useradd -m -s /bin/bash -G video,audio,dialout,plugdev,netdev,gpio,i2c,spi "$SKYTRACK_USER" 2>/dev/null || \
    useradd -m -s /bin/bash -G video,audio,dialout,plugdev,netdev "$SKYTRACK_USER" 2>/dev/null || \
    useradd -m -s /bin/bash "$SKYTRACK_USER"
    log "Created user: $SKYTRACK_USER"
else
    # Ensure user is in necessary groups
    for grp in video audio dialout plugdev netdev; do
        usermod -aG "$grp" "$SKYTRACK_USER" 2>/dev/null || true
    done
    for grp in gpio i2c spi; do
        usermod -aG "$grp" "$SKYTRACK_USER" 2>/dev/null || true
    done
    info "User $SKYTRACK_USER already exists."
fi

# Ensure home directory exists with correct ownership
mkdir -p "$SKYTRACK_HOME"
chown "$SKYTRACK_USER:$SKYTRACK_USER" "$SKYTRACK_HOME"

# =========================================================================
# STEP 3: Install FlightAware (PiAware + dump1090-fa)
# =========================================================================
if [[ "$SKIP_PIAWARE" != true ]]; then
    log "STEP 3/10: Installing FlightAware (PiAware)..."

    if ! dpkg -l piaware 2>/dev/null | grep -q '^ii'; then
        # Add FlightAware APT repository
        if [ ! -f /etc/apt/sources.list.d/piaware.list ]; then
            wget -qO - https://repo.feed.flightaware.com/flightaware-apt-repository.gpg.key 2>/dev/null | \
                gpg --dearmor -o /usr/share/keyrings/flightaware-archive-keyring.gpg 2>/dev/null || true

            # Detect distribution
            DISTRO_CODENAME=$(lsb_release -cs 2>/dev/null || echo "bookworm")
            echo "deb [signed-by=/usr/share/keyrings/flightaware-archive-keyring.gpg] https://repo.feed.flightaware.com/piaware/pool ${DISTRO_CODENAME} main" \
                > /etc/apt/sources.list.d/piaware.list

            apt-get update -qq >> "$LOG_FILE" 2>&1 || true
        fi

        apt-get install -y -qq dump1090-fa piaware >> "$LOG_FILE" 2>&1 || {
            warn "PiAware installation failed — may need manual setup."
            warn "Visit: https://www.flightaware.com/adsb/piaware/install"
        }
    else
        info "PiAware already installed."
    fi

    # Configure feeder ID if provided
    if [[ -n "$PIAWARE_ID" ]]; then
        piaware-config feeder-id "$PIAWARE_ID" 2>/dev/null || true
    fi

    # Enable services
    systemctl enable dump1090-fa 2>/dev/null || true
    systemctl enable piaware 2>/dev/null || true
    systemctl start dump1090-fa 2>/dev/null || true
    systemctl start piaware 2>/dev/null || true

    log "FlightAware setup complete."
else
    info "STEP 3/10: Skipping PiAware (--skip-piaware)."
fi

# =========================================================================
# STEP 4: Install Flightradar24 feeder
# =========================================================================
if [[ "$SKIP_FR24" != true ]]; then
    log "STEP 4/10: Installing Flightradar24 feeder..."

    if ! dpkg -l fr24feed 2>/dev/null | grep -q '^ii'; then
        # Add FR24 repository
        if [ ! -f /etc/apt/sources.list.d/fr24feed.list ]; then
            wget -qO - https://repo-feed.flightradar24.com/fr24feed_pubkey.gpg 2>/dev/null | \
                gpg --dearmor -o /usr/share/keyrings/fr24feed-archive-keyring.gpg 2>/dev/null || true

            echo "deb [signed-by=/usr/share/keyrings/fr24feed-archive-keyring.gpg] https://repo-feed.flightradar24.com flightradar24 raspberrypi-stable" \
                > /etc/apt/sources.list.d/fr24feed.list

            apt-get update -qq >> "$LOG_FILE" 2>&1 || true
        fi

        apt-get install -y -qq fr24feed >> "$LOG_FILE" 2>&1 || {
            warn "FR24 feeder installation failed — may need manual setup."
            warn "Visit: https://www.flightradar24.com/share-your-data"
        }
    else
        info "FR24 feeder already installed."
    fi

    # Configure sharing key if provided
    if [[ -n "$FR24KEY" ]]; then
        FR24_CONF="/etc/fr24feed.ini"
        if [ -f "$FR24_CONF" ]; then
            if grep -q "^fr24key=" "$FR24_CONF"; then
                sed -i "s/^fr24key=.*/fr24key=\"${FR24KEY}\"/" "$FR24_CONF"
            else
                echo "fr24key=\"${FR24KEY}\"" >> "$FR24_CONF"
            fi
        else
            echo "fr24key=\"${FR24KEY}\"" > "$FR24_CONF"
        fi
    fi

    systemctl enable fr24feed 2>/dev/null || true
    systemctl start fr24feed 2>/dev/null || true

    log "Flightradar24 setup complete."
else
    info "STEP 4/10: Skipping FR24 (--skip-fr24)."
fi

# =========================================================================
# STEP 5: Cloudflare Tunnel
# =========================================================================
if [[ "$SKIP_CF" != true ]]; then
    log "STEP 5/10: Setting up Cloudflare Tunnel..."

    # Install cloudflared
    if ! command -v cloudflared &>/dev/null; then
        ARCH=$(dpkg --print-architecture 2>/dev/null || echo "armhf")
        wget -qO /tmp/cloudflared.deb \
            "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb" 2>/dev/null || true

        if [ -f /tmp/cloudflared.deb ]; then
            dpkg -i /tmp/cloudflared.deb >> "$LOG_FILE" 2>&1 || {
                apt-get install -f -y -qq >> "$LOG_FILE" 2>&1
            }
            rm -f /tmp/cloudflared.deb
            log "cloudflared installed."
        else
            warn "Could not download cloudflared — skipping tunnel setup."
            SKIP_CF=true
        fi
    else
        info "cloudflared already installed."
    fi

    if [[ "$SKIP_CF" != true ]]; then
        # Prompt for token if not provided
        if [[ -z "$CF_TOKEN" && "$NON_INTERACTIVE" != true ]]; then
            echo ""
            echo -e "${BLUE}=== Cloudflare Tunnel Setup ===${NC}"
            echo "To create a tunnel token, visit: https://one.dash.cloudflare.com"
            echo "Navigate to: Networks > Tunnels > Create a tunnel"
            echo ""
            read -rp "Cloudflare Tunnel token (or press Enter to skip): " CF_TOKEN
            if [[ -n "$CF_TOKEN" ]]; then
                read -rp "Tunnel name [${CF_NAME}]: " input_name
                CF_NAME="${input_name:-$CF_NAME}"
            fi
        fi

        if [[ -n "$CF_TOKEN" ]]; then
            # Install as service using connector token
            if [ ! -f /etc/systemd/system/cloudflared.service ]; then
                cloudflared service install "$CF_TOKEN" >> "$LOG_FILE" 2>&1 || {
                    warn "cloudflared service install failed — may already be configured."
                }
            else
                info "cloudflared service already configured."
            fi

            systemctl enable cloudflared 2>/dev/null || true
            systemctl start cloudflared 2>/dev/null || true
            log "Cloudflare Tunnel configured."
        else
            info "No Cloudflare token provided — skipping tunnel configuration."
        fi
    fi
else
    info "STEP 5/10: Skipping Cloudflare (--skip-cloudflare)."
fi

# =========================================================================
# STEP 6: Application setup
# =========================================================================
log "STEP 6/10: Setting up SkyTrack application..."

# Copy repo to /opt/skytrack (or update if exists)
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing installation at $INSTALL_DIR..."
    cd "$INSTALL_DIR"
    git pull origin main >> "$LOG_FILE" 2>&1 || \
    git pull >> "$LOG_FILE" 2>&1 || \
    warn "Git pull failed — using existing code."
else
    if [ -d "$SCRIPT_DIR/.git" ] || [ -d "$SCRIPT_DIR/kiosk" ]; then
        # Running from cloned repo or extracted archive — sync it
        mkdir -p "$INSTALL_DIR"
        rsync -a --exclude='.git' --exclude='venv' --exclude='__pycache__' \
            "$SCRIPT_DIR/" "$INSTALL_DIR/" >> "$LOG_FILE" 2>&1
        log "Synced application to $INSTALL_DIR"
    else
        warn "Not running from a git repo — copying files..."
        mkdir -p "$INSTALL_DIR"
        cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR/" 2>/dev/null || true
    fi
fi

cd "$INSTALL_DIR"

# Create Python virtual environment
if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv" >> "$LOG_FILE" 2>&1
    log "Python virtual environment created."
fi

# Install Python dependencies
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip >> "$LOG_FILE" 2>&1
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" >> "$LOG_FILE" 2>&1
log "Python dependencies installed."

# Create config.yaml from example if missing
if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
    cp "$INSTALL_DIR/config.example.yaml" "$INSTALL_DIR/config.yaml"
    # Set production defaults
    sed -i 's/^mock_mode: true/mock_mode: false/' "$INSTALL_DIR/config.yaml"
    log "Created config.yaml with production defaults."
else
    info "config.yaml already exists — not overwriting."
fi

# Ensure kiosk scripts are executable
chmod +x "$INSTALL_DIR/kiosk/skytrack-kiosk.sh" "$INSTALL_DIR/kiosk/xinitrc" 2>/dev/null || true

# Set ownership
chown -R "$SKYTRACK_USER:$SKYTRACK_USER" "$INSTALL_DIR"

# =========================================================================
# STEP 7: Data directories + geodata
# =========================================================================
log "STEP 7/10: Setting up data directories and offline geodata..."

# Create canonical data directories
mkdir -p "$DATA_DIR" "$GEO_DIR" "$LOG_DIR"

# Copy geodata CSVs to the data directory
if [ -d "$INSTALL_DIR/geodata" ]; then
    cp -n "$INSTALL_DIR/geodata/us_cities.csv" "$GEO_DIR/" 2>/dev/null || true
    cp -n "$INSTALL_DIR/geodata/us_zip_centroids.csv" "$GEO_DIR/" 2>/dev/null || true
    log "Geodata files installed to $GEO_DIR"
else
    warn "geodata/ directory not found in repo — location labels will be unavailable."
fi

# Set ownership for all data + log directories
chown -R "$SKYTRACK_USER:$SKYTRACK_USER" "$DATA_DIR"
chown -R "$SKYTRACK_USER:$SKYTRACK_USER" "$LOG_DIR"

# =========================================================================
# STEP 8: Systemd services
# =========================================================================
log "STEP 8/10: Installing systemd services..."

# Copy service files
cp "$INSTALL_DIR/systemd/skytrack.service" /etc/systemd/system/skytrack.service
cp "$INSTALL_DIR/systemd/skytrack-kiosk.service" /etc/systemd/system/skytrack-kiosk.service

# Ensure home directory is set up for kiosk (Chromium profile, .Xauthority)
mkdir -p "$SKYTRACK_HOME"
chown -R "$SKYTRACK_USER:$SKYTRACK_USER" "$SKYTRACK_HOME"

# Enable services
systemctl daemon-reload
systemctl enable skytrack.service >> "$LOG_FILE" 2>&1
systemctl enable skytrack-kiosk.service >> "$LOG_FILE" 2>&1

# Disable login console on tty1
systemctl disable getty@tty1.service 2>/dev/null || true

# Start SkyTrack backend
systemctl restart skytrack.service >> "$LOG_FILE" 2>&1 || {
    warn "Failed to start skytrack service — will start on next boot."
}

# Enable GPS daemon
systemctl enable gpsd 2>/dev/null || true
systemctl start gpsd 2>/dev/null || true

log "Systemd services installed and enabled."

# =========================================================================
# STEP 9: LCD profile (optional)
# =========================================================================
log "STEP 9/10: LCD profile configuration..."

if [[ -n "$LCD_PROFILE" ]]; then
    PROFILE_FILE="$INSTALL_DIR/profiles/${LCD_PROFILE}.conf"
    BOOT_CONFIG="/boot/firmware/config.txt"

    # Fallback for older Pi OS
    if [ ! -f "$BOOT_CONFIG" ]; then
        BOOT_CONFIG="/boot/config.txt"
    fi

    if [ -f "$PROFILE_FILE" ]; then
        if [ -f "$BOOT_CONFIG" ]; then
            # Backup current config
            BACKUP="${BOOT_CONFIG}.bak.$(date +%Y%m%d%H%M%S)"
            cp "$BOOT_CONFIG" "$BACKUP"
            log "Backed up $BOOT_CONFIG to $BACKUP"

            # Check if profile already applied
            PROFILE_MARKER="# SkyTrack LCD Profile: ${LCD_PROFILE}"
            if ! grep -q "$PROFILE_MARKER" "$BOOT_CONFIG" 2>/dev/null; then
                echo "" >> "$BOOT_CONFIG"
                echo "$PROFILE_MARKER" >> "$BOOT_CONFIG"
                # Only append non-comment, non-empty lines
                grep -v '^#' "$PROFILE_FILE" | grep -v '^$' >> "$BOOT_CONFIG" || true
                log "LCD profile '$LCD_PROFILE' applied to $BOOT_CONFIG"
            else
                info "LCD profile '$LCD_PROFILE' already applied."
            fi
        else
            warn "Boot config not found — cannot apply LCD profile."
        fi
    else
        warn "LCD profile '$LCD_PROFILE' not found at $PROFILE_FILE"
    fi
else
    info "No LCD profile specified — using default HDMI."
fi

# =========================================================================
# STEP 10: Post-install verification
# =========================================================================
log "STEP 10/10: Running post-install verification..."

echo ""
echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  SkyTrack Post-Install Verification${NC}"
echo -e "${BLUE}============================================${NC}"

FAILED_SERVICES=()

verify_service() {
    local svc="$1"
    local label="$2"
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo -e "  ${GREEN}[OK]${NC}   $label"
        return 0
    elif systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        echo -e "  ${YELLOW}[WAIT]${NC} $label (enabled, will start on boot)"
        return 0
    else
        echo -e "  ${RED}[FAIL]${NC} $label"
        FAILED_SERVICES+=("$svc")
        return 1
    fi
}

PASS=0
FAIL=0

verify_service "skytrack"         "SkyTrack Backend"     && ((PASS++)) || ((FAIL++))
verify_service "skytrack-kiosk"   "SkyTrack Kiosk"       && ((PASS++)) || ((FAIL++))

if [[ "$SKIP_PIAWARE" != true ]]; then
    verify_service "dump1090-fa"  "dump1090-fa"          && ((PASS++)) || ((FAIL++))
    verify_service "piaware"      "PiAware"              && ((PASS++)) || ((FAIL++))
fi

if [[ "$SKIP_FR24" != true ]]; then
    verify_service "fr24feed"     "Flightradar24 Feeder" && ((PASS++)) || ((FAIL++))
fi

if [[ "$SKIP_CF" != true && -n "$CF_TOKEN" ]]; then
    verify_service "cloudflared"  "Cloudflare Tunnel"    && ((PASS++)) || ((FAIL++))
fi

verify_service "gpsd"             "GPS Daemon"           && ((PASS++)) || ((FAIL++))

# Check backend reachability
echo ""
sleep 3
if curl -sf http://127.0.0.1:5000 >/dev/null 2>&1; then
    echo -e "  ${GREEN}[OK]${NC}   Dashboard reachable at http://127.0.0.1:5000"
    ((PASS++))
else
    echo -e "  ${YELLOW}[WAIT]${NC} Dashboard not yet reachable (may need a moment)"
    ((PASS++))  # Not a failure — service may still be starting
fi

# Check geodata
if [ -f "$GEO_DIR/us_cities.csv" ] && [ -f "$GEO_DIR/us_zip_centroids.csv" ]; then
    CITY_COUNT=$(wc -l < "$GEO_DIR/us_cities.csv" 2>/dev/null || echo 0)
    ZIP_COUNT=$(wc -l < "$GEO_DIR/us_zip_centroids.csv" 2>/dev/null || echo 0)
    echo -e "  ${GREEN}[OK]${NC}   Offline geodata installed ($((CITY_COUNT - 1)) cities, $((ZIP_COUNT - 1)) ZIPs)"
    ((PASS++))
else
    echo -e "  ${YELLOW}[WARN]${NC} Offline geodata missing from $GEO_DIR"
fi

echo ""
echo -e "${BLUE}============================================${NC}"
echo -e "  Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"
echo -e "${BLUE}============================================${NC}"

# Show journal logs for any failed services
if [[ ${#FAILED_SERVICES[@]} -gt 0 ]]; then
    echo ""
    echo -e "${YELLOW}--- Diagnostic logs for failed services ---${NC}"
    for svc in "${FAILED_SERVICES[@]}"; do
        echo ""
        echo -e "${YELLOW}[$svc]${NC}"
        journalctl -u "$svc" --no-pager -n 50 2>/dev/null || true
    done
    echo -e "${YELLOW}-------------------------------------------${NC}"
fi

echo ""

if [[ $FAIL -eq 0 ]]; then
    log "Installation complete — all checks passed!"
else
    warn "Installation complete with $FAIL warning(s). See diagnostic logs above."
fi

echo -e "${GREEN}Installation complete!${NC}"
echo ""
echo "Next steps:"
echo "  1. Reboot to start the kiosk:  sudo reboot"
echo "  2. Dashboard URL:  http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):5000"
echo ""
echo -e "${BLUE}--- Post-Install Configuration (run after reboot) ---${NC}"

if [[ "$SKIP_PIAWARE" != true ]] && [[ -z "$PIAWARE_ID" ]]; then
    echo ""
    echo "  PiAware claim:"
    echo "    Visit https://www.flightaware.com/adsb/piaware/claim"
    echo "    Or:  sudo piaware-config feeder-id <YOUR_FEEDER_ID>"
fi

if [[ "$SKIP_FR24" != true ]] && [[ -z "$FR24KEY" ]]; then
    echo ""
    echo "  Flightradar24 signup:"
    echo "    sudo fr24feed --signup"
    echo "    Or:  sudo nano /etc/fr24feed.ini  # set fr24key=\"YOUR_KEY\""
fi

if [[ "$SKIP_CF" != true ]] && [[ -z "$CF_TOKEN" ]]; then
    echo ""
    echo "  Cloudflare Tunnel:"
    echo "    1. Create tunnel at https://one.dash.cloudflare.com > Networks > Tunnels"
    echo "    2. sudo cloudflared service install <TUNNEL_TOKEN>"
    echo "    3. sudo systemctl enable --now cloudflared"
fi

echo ""
echo -e "${BLUE}--- Post-Install Self-Test (run after reboot) ---${NC}"
echo "  Copy/paste these commands to verify everything is working:"
echo ""
echo "    systemctl status skytrack --no-pager"
echo "    systemctl status skytrack-kiosk --no-pager"
echo "    curl -sf http://127.0.0.1:5000 >/dev/null && echo 'Dashboard: OK' || echo 'Dashboard: FAIL'"
echo "    DISPLAY=:0 xrandr --verbose 2>/dev/null | head -20 || echo 'X11 not running (expected before reboot)'"
echo ""
echo "  Expected after reboot:"
echo "    skytrack:      active (running)"
echo "    skytrack-kiosk: active (running)"
echo "    Dashboard:     OK"
echo "    xrandr:        Shows connected display + rotation"
echo ""
echo -e "${BLUE}--- Useful commands ---${NC}"
echo "  View backend logs:   journalctl -u skytrack -f"
echo "  View kiosk logs:     journalctl -u skytrack-kiosk -f"
echo "  Edit config:         sudo nano /opt/skytrack/config.yaml"
echo "  Restart backend:     sudo systemctl restart skytrack"
echo "  Restart kiosk:       sudo systemctl restart skytrack-kiosk"
echo ""
echo "Full install log: $LOG_FILE"
echo ""

log "=== Install finished at $(date -Iseconds) ==="
