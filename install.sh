#!/usr/bin/env bash
# =============================================================================
# SkyTrack Portal v2 — installer
#
#   sudo ./install.sh [OPTIONS]
#
# Options:
#   --skip-piaware     Don't install dump1090-fa / piaware
#   --skip-fr24        Don't install fr24feed
#   --skip-hotspot     Don't bring up the SkyTrack-Portal hotspot
#   --dev              Local-dev install: no systemd, no hotspot, no feeders
#   --non-interactive  Never prompt
#
# What it does (in order):
#   1. apt installs (Pi packages, hotspot stack, build tools, optional feeders)
#   2. Creates the `skytrack` user and home directory
#   3. Syncs the repo to /opt/skytrack
#   4. Creates the venv and installs requirements.txt
#   5. Renders /etc/skytrack/skytrack.env
#   6. Creates /var/lib/skytrack and /var/log/skytrack with sane perms
#   7. Generates the device ID + initializes the SQLite database
#   8. Fetches vendor JS into static/vendor/
#   9. Installs the systemd units and enables them
#  10. Applies the wlan0 hotspot (unless --skip-hotspot or --dev)
#  11. Runs scripts/install_verify.sh
#
# Idempotent: safe to re-run. Will not overwrite an existing config.yaml or
# auth.json, will not regenerate the device ID, will not wipe the db.
# =============================================================================

set -uo pipefail

REPO_DIR="${SKYTRACK_REPO_DIR:-/opt/skytrack}"
DATA_DIR="${SKYTRACK_DATA_DIR:-/var/lib/skytrack}"
LOG_DIR="${SKYTRACK_LOG_DIR:-/var/log/skytrack}"
ETC_DIR="/etc/skytrack"
VENV_DIR="$REPO_DIR/venv"
SKYTRACK_USER="${SKYTRACK_USER:-skytrack}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/var/log/skytrack-install.log"

SKIP_PIAWARE=false
SKIP_FR24=false
SKIP_HOTSPOT=false
DEV_INSTALL=false
NON_INTERACTIVE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-piaware)    SKIP_PIAWARE=true; shift ;;
    --skip-fr24)       SKIP_FR24=true; shift ;;
    --skip-hotspot)    SKIP_HOTSPOT=true; shift ;;
    --dev)             DEV_INSTALL=true; SKIP_HOTSPOT=true; SKIP_PIAWARE=true; SKIP_FR24=true; shift ;;
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    -h|--help)         sed -n '1,40p' "$0"; exit 0 ;;
    *)                 echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ "$DEV_INSTALL" != true && $EUID -ne 0 ]]; then
  echo "must run as root (sudo $0)" >&2
  exit 1
fi

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
echo "=== SkyTrack Portal v2 install $(date -Iseconds) ===" >> "$LOG_FILE" 2>/dev/null || true

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[skytrack]${NC} $*" | tee -a "$LOG_FILE" 2>/dev/null || echo "[skytrack] $*"; }
info() { echo -e "${BLUE}[info]${NC} $*"     | tee -a "$LOG_FILE" 2>/dev/null || echo "[info] $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"   | tee -a "$LOG_FILE" 2>/dev/null || echo "[warn] $*"; }
err()  { echo -e "${RED}[err]${NC} $*"       | tee -a "$LOG_FILE" 2>/dev/null || echo "[err] $*"; }

# ---------------------------------------------------------------------------
# 1. apt packages
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 1/11: apt packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >> "$LOG_FILE" 2>&1 || warn "apt-get update failed"

  PKGS=(
    git curl wget rsync ca-certificates jq
    python3 python3-venv python3-pip python3-dev build-essential
    sqlite3
    # Hotspot + DNS
    hostapd dnsmasq dhcpcd5 iw wireless-tools rfkill
    # Cellular
    modemmanager network-manager usb-modeswitch usb-modeswitch-data
    libqmi-utils libmbim-utils
    # GPS
    gpsd gpsd-clients
    # GPIO / sensors
    libgpiod2 python3-libgpiod
    # Optional kiosk
    chromium-browser xserver-xorg xinit openbox unclutter
  )
  apt-get install -y -qq "${PKGS[@]}" >> "$LOG_FILE" 2>&1 || warn "some packages failed (continuing)"
fi

# ---------------------------------------------------------------------------
# 2. user
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 2/11: user $SKYTRACK_USER"
  if ! id "$SKYTRACK_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$SKYTRACK_USER"
  fi
  for grp in dialout plugdev netdev gpio i2c spi video; do
    usermod -aG "$grp" "$SKYTRACK_USER" 2>/dev/null || true
  done
fi

# ---------------------------------------------------------------------------
# 3. sync repo to /opt/skytrack
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 3/11: sync repo → $REPO_DIR"
  mkdir -p "$REPO_DIR"
  rsync -a --exclude='.git' --exclude='venv' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='legacy/' \
    "$SCRIPT_DIR/" "$REPO_DIR/" >> "$LOG_FILE" 2>&1
  chown -R "$SKYTRACK_USER:$SKYTRACK_USER" "$REPO_DIR"
else
  REPO_DIR="$SCRIPT_DIR"
  info "dev install: using $REPO_DIR in place"
fi

# ---------------------------------------------------------------------------
# 4. venv + python deps
# ---------------------------------------------------------------------------
log "step 4/11: python venv"
VENV_DIR="$REPO_DIR/venv"
if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
  python3 -m venv "$VENV_DIR" >> "$LOG_FILE" 2>&1
fi
"$VENV_DIR/bin/pip" install --upgrade pip >> "$LOG_FILE" 2>&1
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt" >> "$LOG_FILE" 2>&1 || warn "pip install had errors"

# ---------------------------------------------------------------------------
# 5. /etc/skytrack/skytrack.env
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 5/11: /etc/skytrack/skytrack.env"
  mkdir -p "$ETC_DIR"
  if [[ ! -f "$ETC_DIR/skytrack.env" ]]; then
    cp "$REPO_DIR/config_templates/skytrack.env.tmpl" "$ETC_DIR/skytrack.env"
    chown root:root "$ETC_DIR/skytrack.env"
    chmod 644 "$ETC_DIR/skytrack.env"
  else
    info "$ETC_DIR/skytrack.env exists — not overwriting"
  fi
fi

# ---------------------------------------------------------------------------
# 6. data + log directories
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 6/11: $DATA_DIR / $LOG_DIR"
  mkdir -p "$DATA_DIR" "$DATA_DIR/backups" "$LOG_DIR"
  chown -R "$SKYTRACK_USER:$SKYTRACK_USER" "$DATA_DIR" "$LOG_DIR"
fi

# ---------------------------------------------------------------------------
# 7. device ID + DB
# ---------------------------------------------------------------------------
log "step 7/11: device ID + database migrate"
SUDO_AS=""
if [[ "$DEV_INSTALL" != true ]]; then
  SUDO_AS="sudo -u $SKYTRACK_USER -H"
fi
$SUDO_AS env SKYTRACK_REPO_DIR="$REPO_DIR" SKYTRACK_DATA_DIR="$DATA_DIR" \
  "$VENV_DIR/bin/python" "$REPO_DIR/scripts/db_migrate.py" >> "$LOG_FILE" 2>&1 \
  || warn "db_migrate had errors"

$SUDO_AS env SKYTRACK_REPO_DIR="$REPO_DIR" SKYTRACK_DATA_DIR="$DATA_DIR" \
  "$VENV_DIR/bin/python" -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import device_id
ident = device_id.get_or_create_device_id()
print('device_id:', ident['device_id'])
" >> "$LOG_FILE" 2>&1 || warn "device_id init failed"

# ---------------------------------------------------------------------------
# 8. vendor JS
# ---------------------------------------------------------------------------
log "step 8/11: vendor JS"
"$REPO_DIR/scripts/fetch_vendor.sh" >> "$LOG_FILE" 2>&1 || warn "vendor fetch had errors"

# ---------------------------------------------------------------------------
# 9. systemd units
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 9/11: systemd units"
  for unit in skytrack-firstboot.service skytrack-app.service skytrack-network.service \
              skytrack-ingest.service skytrack-hardware.service skytrack-display.service \
              skytrack-hotspot.service skytrack-hotspot-watchdog.service \
              skytrack-hotspot-watchdog.timer; do
    if [[ -f "$REPO_DIR/systemd/$unit" ]]; then
      cp "$REPO_DIR/systemd/$unit" "/etc/systemd/system/$unit"
      info "installed $unit"
    fi
  done
  systemctl daemon-reload
  systemctl enable skytrack-firstboot.service skytrack-app.service \
                   skytrack-network.service skytrack-ingest.service \
                   skytrack-hardware.service >> "$LOG_FILE" 2>&1 || true
  if [[ "$SKIP_HOTSPOT" != true ]]; then
    systemctl enable skytrack-hotspot.service skytrack-hotspot-watchdog.timer >> "$LOG_FILE" 2>&1 || true
  fi
fi

# ---------------------------------------------------------------------------
# 10. hotspot bring-up
# ---------------------------------------------------------------------------
if [[ "$SKIP_HOTSPOT" != true && "$DEV_INSTALL" != true ]]; then
  log "step 10/11: applying wlan0 hotspot"
  # If we're on SSH over wlan0, hotspot_apply.sh will refuse — that's intentional.
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    warn "SSH session detected — hotspot will be applied on next boot via skytrack-hotspot.service"
  else
    "$REPO_DIR/scripts/hotspot_apply.sh" || warn "hotspot apply returned non-zero"
  fi
fi

# ---------------------------------------------------------------------------
# 11. start app + verify
# ---------------------------------------------------------------------------
log "step 11/11: starting services and running verify"
if [[ "$DEV_INSTALL" != true ]]; then
  systemctl restart skytrack-app.service 2>>"$LOG_FILE" || warn "skytrack-app failed to start"
  sleep 2
  "$REPO_DIR/scripts/install_verify.sh" || warn "install_verify reported failures"
fi

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
echo
echo -e "${BLUE}=== SkyTrack Portal v2 install finished ===${NC}"
echo
if [[ "$DEV_INSTALL" == true ]]; then
  echo "Dev install complete."
  echo
  echo "Run the app:"
  echo "  cd $REPO_DIR && DEV_MODE=1 $VENV_DIR/bin/python app.py"
  echo
  echo "Then open http://127.0.0.1:8080/"
else
  echo "Production install complete."
  echo
  echo "Next steps:"
  echo "  • Connect a phone/laptop to the SkyTrack-Portal Wi-Fi"
  echo "  • Browse to http://10.4.26.89/ and complete the setup wizard"
  echo "  • Logs:        journalctl -u skytrack-app -f"
  echo "  • Verify:      sudo $REPO_DIR/scripts/install_verify.sh"
  echo "  • Reapply hot: sudo $REPO_DIR/scripts/hotspot_apply.sh --reapply"
fi
echo
echo "Install log: $LOG_FILE"
