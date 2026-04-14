#!/usr/bin/env bash
# =============================================================================
# SkyTrack Portal v2 — appliance installer
#
#   sudo ./install.sh [OPTIONS]
#
# Options:
#   --skip-piaware     Don't install dump1090-fa / piaware
#   --skip-fr24        Don't install fr24feed
#   --skip-hotspot     Don't bring up the SkyTrack-Portal hotspot
#   --keep-desktop     Do NOT purge raspberrypi-ui-mods / libreoffice*
#   --dev              Local-dev install: no systemd, no hotspot, no feeders,
#                      no appliance lockdown
#   --non-interactive  Never prompt
#
# What it does (in order):
#   1. apt installs (base + plymouth + kiosk stack + feeders)
#   2. Purges desktop packages that would leak Linux UI (unless --keep-desktop)
#   3. Creates the `skytrack` service user and the `skytrack-kiosk` kiosk user
#   4. Syncs the repo to /opt/skytrack
#   5. Creates the venv at /opt/skytrack/.venv and installs requirements.txt
#   6. Renders /etc/skytrack/skytrack.env (idempotent)
#   7. Creates /var/lib/skytrack and /var/log/skytrack with sane perms
#   8. Generates the device ID + initializes the SQLite database
#   9. Fetches vendor JS into static/vendor/
#  10. Runs scripts/configure_appliance.sh (plymouth, cmdline, ttys, openbox)
#  11. Installs the systemd units (including skytrack-display.service) and
#      enables them
#  12. Applies the wlan0 hotspot (unless --skip-hotspot or --dev)
#  13. Runs scripts/install_verify.sh
#
# Idempotent: safe to re-run. Will not overwrite an existing config.yaml,
# auth.json, or /etc/skytrack/skytrack.env, will not regenerate the device ID,
# will not wipe the db. Never touches ssh.service.
#
# Recovery: if the appliance boots into a broken state, drop a file named
# `skytrack-safe-mode` into /boot/firmware/ (mount the SD card on another
# machine). On next boot, skytrack-display.service will be skipped and
# getty@tty1 will come up normally for console service work.
# =============================================================================

set -uo pipefail

REPO_DIR="${SKYTRACK_REPO_DIR:-/opt/skytrack}"
DATA_DIR="${SKYTRACK_DATA_DIR:-/var/lib/skytrack}"
LOG_DIR="${SKYTRACK_LOG_DIR:-/var/log/skytrack}"
ETC_DIR="/etc/skytrack"
VENV_DIR="$REPO_DIR/.venv"
SKYTRACK_USER="${SKYTRACK_USER:-skytrack}"
KIOSK_USER="skytrack-kiosk"
KIOSK_HOME="/home/${KIOSK_USER}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/var/log/skytrack-install.log"

SKIP_PIAWARE=false
SKIP_FR24=false
SKIP_HOTSPOT=false
KEEP_DESKTOP=false
DEV_INSTALL=false
NON_INTERACTIVE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-piaware)    SKIP_PIAWARE=true; shift ;;
    --skip-fr24)       SKIP_FR24=true; shift ;;
    --skip-hotspot)    SKIP_HOTSPOT=true; shift ;;
    --keep-desktop)    KEEP_DESKTOP=true; shift ;;
    --dev)             DEV_INSTALL=true; SKIP_HOTSPOT=true; SKIP_PIAWARE=true; SKIP_FR24=true; KEEP_DESKTOP=true; shift ;;
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    -h|--help)         sed -n '1,50p' "$0"; exit 0 ;;
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
  log "step 1/13: apt packages"
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
    # Appliance kiosk stack (X11 only, no display manager)
    chromium-browser
    xserver-xorg xserver-xorg-legacy xinit
    openbox unclutter
    # Branded boot splash
    plymouth plymouth-themes
  )
  apt-get install -y -qq "${PKGS[@]}" >> "$LOG_FILE" 2>&1 || warn "some packages failed (continuing)"
fi

# ---------------------------------------------------------------------------
# 2. purge desktop leaks
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  if [[ "$KEEP_DESKTOP" == true ]]; then
    info "step 2/13: --keep-desktop — not purging raspberrypi-ui-mods / libreoffice*"
  else
    log "step 2/13: purging desktop packages that would leak Linux UI"
    # raspberrypi-ui-mods pulls the PIXEL desktop; libreoffice* is ~1GB of
    # office app we don't want on an appliance.
    apt-get purge -y \
      raspberrypi-ui-mods \
      'libreoffice*' \
      lightdm lightdm-gtk-greeter \
      2>>"$LOG_FILE" || true
    apt-get autoremove -y >> "$LOG_FILE" 2>&1 || true
  fi
fi

# ---------------------------------------------------------------------------
# 3. users — hardened service user + dedicated kiosk session user
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 3/13: users ($SKYTRACK_USER, $KIOSK_USER)"
  # Service user — runs the backend, no GUI
  if ! id "$SKYTRACK_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$SKYTRACK_USER"
  fi
  for grp in dialout plugdev netdev gpio i2c spi video; do
    usermod -aG "$grp" "$SKYTRACK_USER" 2>/dev/null || true
  done

  # Kiosk session user — runs X+Chromium, password locked (only systemd/PAM
  # can log it in, never a human via a terminal).
  if ! id "$KIOSK_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$KIOSK_USER"
    passwd -l "$KIOSK_USER" >/dev/null 2>&1 || true
  fi
  for grp in tty video input audio plugdev dialout; do
    usermod -aG "$grp" "$KIOSK_USER" 2>/dev/null || true
  done
  mkdir -p "$KIOSK_HOME/.config"
  chown -R "$KIOSK_USER:$KIOSK_USER" "$KIOSK_HOME"
fi

# ---------------------------------------------------------------------------
# 4. sync repo to /opt/skytrack
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 4/13: sync repo → $REPO_DIR"
  mkdir -p "$REPO_DIR"
  rsync -a \
    --exclude='.git' --exclude='venv' --exclude='.venv' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='legacy/' \
    "$SCRIPT_DIR/" "$REPO_DIR/" >> "$LOG_FILE" 2>&1
  chown -R "$SKYTRACK_USER:$SKYTRACK_USER" "$REPO_DIR"
else
  REPO_DIR="$SCRIPT_DIR"
  VENV_DIR="$REPO_DIR/.venv"
  info "dev install: using $REPO_DIR in place"
fi

# ---------------------------------------------------------------------------
# 5. venv + python deps
# ---------------------------------------------------------------------------
log "step 5/13: python venv at $VENV_DIR"
if [[ -d "$REPO_DIR/venv" && ! -L "$REPO_DIR/venv" ]]; then
  warn "legacy $REPO_DIR/venv exists alongside new .venv — leaving in place (safe to rm)"
fi
if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
  python3 -m venv "$VENV_DIR" >> "$LOG_FILE" 2>&1
fi
"$VENV_DIR/bin/pip" install --upgrade pip >> "$LOG_FILE" 2>&1
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt" >> "$LOG_FILE" 2>&1 || warn "pip install had errors"

# ---------------------------------------------------------------------------
# 6. /etc/skytrack/skytrack.env
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 6/13: /etc/skytrack/skytrack.env"
  mkdir -p "$ETC_DIR"
  if [[ ! -f "$ETC_DIR/skytrack.env" ]]; then
    cp "$REPO_DIR/config_templates/skytrack.env.tmpl" "$ETC_DIR/skytrack.env"
    chown root:root "$ETC_DIR/skytrack.env"
    chmod 644 "$ETC_DIR/skytrack.env"
  else
    info "$ETC_DIR/skytrack.env exists — not overwriting"
    if ! grep -q '^SKYTRACK_PORT=8080' "$ETC_DIR/skytrack.env"; then
      warn "$ETC_DIR/skytrack.env does not set SKYTRACK_PORT=8080 — consider updating"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 7. data + log directories
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 7/13: $DATA_DIR / $LOG_DIR"
  mkdir -p "$DATA_DIR" "$DATA_DIR/backups" "$LOG_DIR"
  chown -R "$SKYTRACK_USER:$SKYTRACK_USER" "$DATA_DIR" "$LOG_DIR"
fi

# ---------------------------------------------------------------------------
# 8. device ID + DB
# ---------------------------------------------------------------------------
log "step 8/13: device ID + database migrate"
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
# 9. vendor JS
# ---------------------------------------------------------------------------
log "step 9/13: vendor JS"
"$REPO_DIR/scripts/fetch_vendor.sh" >> "$LOG_FILE" 2>&1 || warn "vendor fetch had errors"

# ---------------------------------------------------------------------------
# 10. appliance provisioning (plymouth + cmdline + ttys + openbox + Xwrapper)
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 10/13: appliance provisioning"
  if [[ -x "$REPO_DIR/scripts/configure_appliance.sh" ]]; then
    SKYTRACK_REPO_DIR="$REPO_DIR" "$REPO_DIR/scripts/configure_appliance.sh" \
      >> "$LOG_FILE" 2>&1 || warn "configure_appliance had errors"
  else
    warn "scripts/configure_appliance.sh not executable — skipping"
  fi
fi

# ---------------------------------------------------------------------------
# 11. systemd units
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 11/13: systemd units"
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
                   skytrack-hardware.service skytrack-display.service \
                   >> "$LOG_FILE" 2>&1 || true
  if [[ "$SKIP_HOTSPOT" != true ]]; then
    systemctl enable skytrack-hotspot.service skytrack-hotspot-watchdog.timer >> "$LOG_FILE" 2>&1 || true
  fi
fi

# ---------------------------------------------------------------------------
# 12. hotspot bring-up
# ---------------------------------------------------------------------------
if [[ "$SKIP_HOTSPOT" != true && "$DEV_INSTALL" != true ]]; then
  log "step 12/13: applying wlan0 hotspot"
  # If we're on SSH over wlan0, hotspot_apply.sh will refuse — that's intentional.
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    warn "SSH session detected — hotspot will be applied on next boot via skytrack-hotspot.service"
  else
    "$REPO_DIR/scripts/hotspot_apply.sh" || warn "hotspot apply returned non-zero"
  fi
fi

# ---------------------------------------------------------------------------
# 13. start app + verify
# ---------------------------------------------------------------------------
log "step 13/13: starting services and running verify"
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
  echo "Appliance install complete."
  echo
  echo -e "${YELLOW}Reboot now to enter appliance mode.${NC}"
  echo
  echo "On next boot the device will:"
  echo "  • Show the SkyTrack Plymouth splash"
  echo "  • Drop into Chromium kiosk on /kiosk (setup wizard or dashboard)"
  echo "  • Never expose the Linux desktop, terminal, or login screen"
  echo
  echo "Operator access:"
  echo "  • SSH stays enabled"
  echo "  • Safe-mode: touch /boot/firmware/skytrack-safe-mode and reboot"
  echo "    → skips kiosk takeover, getty@tty1 comes up for service work"
  echo "  • Logs:   journalctl -u skytrack-app -f"
  echo "  • Verify: sudo $REPO_DIR/scripts/install_verify.sh"
fi
echo
echo "Install log: $LOG_FILE"
