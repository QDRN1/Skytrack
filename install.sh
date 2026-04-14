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
#   --skip-runtime     Do NOT start skytrack-display.service during verify
#                      (useful when installing over SSH from a shell that
#                      already owns tty1). Everything still gets enabled
#                      for the next reboot.
#   --non-interactive  Never prompt
#
# What it does (in order):
#   1.  apt installs (base + plymouth + kiosk stack + feeders)
#   2.  Purges desktop packages that would leak Linux UI (unless --keep-desktop)
#   3.  Creates the `skytrack` service user and the `skytrack-kiosk` kiosk user
#   4.  Syncs the repo to /opt/skytrack
#   5.  Creates the venv at /opt/skytrack/.venv and installs requirements.txt
#   6.  Renders /etc/skytrack/skytrack.env (idempotent)
#   7.  Creates /var/lib/skytrack and /var/log/skytrack with sane perms,
#       plus the OTA workspace parent and .ssh/known_hosts for the service user
#   8.  Generates the device ID, initializes the SQLite database, and writes
#       static/splash/device_id.js so the file:// splash shows the ID before
#       skytrack-app is listening
#   9.  Fetches vendor JS into static/vendor/
#  10.  Runs scripts/configure_appliance.sh (plymouth, cmdline, ttys,
#       openbox, Xwrapper, /usr/share/skytrack/splash-fallback.html)
#  11.  Installs the systemd units (including skytrack-display.service) +
#       polkit rule, then daemon-reloads and enables them
#  12.  Applies the wlan0 hotspot (unless --skip-hotspot or --dev)
#  13.  Starts skytrack-app + (by default) skytrack-display so the runtime
#       verify can observe X + Chromium + /healthz
#  14.  Runs scripts/install_verify.sh — EXITS NON-ZERO on any failure
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
SKIP_RUNTIME=false
NON_INTERACTIVE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-piaware)    SKIP_PIAWARE=true; shift ;;
    --skip-fr24)       SKIP_FR24=true; shift ;;
    --skip-hotspot)    SKIP_HOTSPOT=true; shift ;;
    --keep-desktop)    KEEP_DESKTOP=true; shift ;;
    --skip-runtime)    SKIP_RUNTIME=true; shift ;;
    --dev)             DEV_INSTALL=true; SKIP_HOTSPOT=true; SKIP_PIAWARE=true; SKIP_FR24=true; KEEP_DESKTOP=true; SKIP_RUNTIME=true; shift ;;
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    -h|--help)         sed -n '1,60p' "$0"; exit 0 ;;
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
  log "step 1/14: apt packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >> "$LOG_FILE" 2>&1 || warn "apt-get update failed"

  # Required packages — if any of these fail we're in trouble, but we still
  # continue (with a warning) so the rest of the installer can run and
  # install_verify.sh can tell the operator exactly what's missing.
  PKGS=(
    git curl wget rsync ca-certificates jq
    python3 python3-venv python3-pip python3-dev build-essential
    sqlite3
    # Hotspot + DNS. dhcpcd intentionally omitted — see PKGS_OPTIONAL below.
    hostapd dnsmasq iw wireless-tools rfkill
    # Cellular
    modemmanager network-manager usb-modeswitch usb-modeswitch-data
    libqmi-utils libmbim-utils
    # GPS
    gpsd gpsd-clients
    # GPIO / sensors
    libgpiod2 python3-libgpiod
    # Appliance kiosk stack (X11 only, no display manager).
    # chromium / chromium-browser: Raspberry Pi OS Bookworm ships the binary
    # under both names at different revisions. We install the package that
    # exists and the xinitrc auto-detects which binary to launch.
    xserver-xorg xserver-xorg-legacy xinit
    openbox unclutter
    # Branded boot splash
    plymouth plymouth-themes
    # Polkit — needed for the skytrack service user to bounce its own units
    policykit-1
  )
  apt-get install -y -qq "${PKGS[@]}" >> "$LOG_FILE" 2>&1 || warn "some packages failed (continuing)"

  # Optional packages — install one at a time so a single missing package
  # in Bookworm doesn't abort the whole apt run. dhcpcd5 is the classic
  # case: Raspberry Pi OS Bookworm dropped it in favor of NetworkManager,
  # so on a fresh Bookworm Lite image the package is "Unable to locate".
  # hotspot_apply.sh already falls back to `ip addr add` when dhcpcd is
  # absent, so this is strictly best-effort.
  PKGS_OPTIONAL=(
    dhcpcd5
  )
  for opt_pkg in "${PKGS_OPTIONAL[@]}"; do
    if apt-get install -y -qq "$opt_pkg" >> "$LOG_FILE" 2>&1; then
      info "optional package installed: $opt_pkg"
    else
      info "optional package unavailable (ok): $opt_pkg"
    fi
  done

  # Chromium — try both package names. Bookworm Lite: chromium-browser.
  # Newer / non-Pi Debian: chromium. Install whichever is available so the
  # binary ends up in /usr/bin/ under either name.
  if ! dpkg -s chromium-browser >/dev/null 2>&1 && ! dpkg -s chromium >/dev/null 2>&1; then
    apt-get install -y -qq chromium-browser >> "$LOG_FILE" 2>&1 || \
    apt-get install -y -qq chromium        >> "$LOG_FILE" 2>&1 || \
      warn "could not install chromium — kiosk will paint a dark screen until installed"
  fi

  # Hotspot-critical packages — verify they actually landed. The bulk
  # apt-get above runs with `|| warn` so a single failed package in the
  # batch (often dnsmasq getting masked by systemd-resolved on a fresh
  # Bookworm) silently leaves the hotspot stack incomplete. Re-install
  # any missing critical package one at a time so we get a real error per
  # package, then unmask + enable dnsmasq + hostapd unconditionally.
  for crit_pkg in hostapd dnsmasq; do
    if ! dpkg -s "$crit_pkg" >/dev/null 2>&1; then
      info "hotspot-critical package missing — installing $crit_pkg individually"
      apt-get install -y -qq "$crit_pkg" >> "$LOG_FILE" 2>&1 \
        || warn "failed to install $crit_pkg — hotspot will not be usable"
    fi
  done
  # Bookworm ships dnsmasq.service disabled (and sometimes masked when
  # systemd-resolved is bound to :53). Unmasking is safe and idempotent;
  # we leave the dnsmasq drop-in to scripts/hotspot_apply.sh.
  systemctl unmask hostapd >> "$LOG_FILE" 2>&1 || true
  systemctl unmask dnsmasq >> "$LOG_FILE" 2>&1 || true
fi

# ---------------------------------------------------------------------------
# 1b. Pi-only Python hardware libs (RPi.GPIO + DHT)
#
# requirements.txt commits to "minimal deps" for the dev box, so the Pi
# hardware libs are commented out there. They're real packages though,
# and without them Phase 2.5 Long Check shows:
#     buzzer.last_error = RPi.GPIO not installed: No module named 'RPi'
# We install them here, but ONLY when /proc/device-tree/model says we're
# actually on a Raspberry Pi — installing RPi.GPIO on a non-Pi dev box
# fails to compile against the wrong arch and there's nothing useful for
# it to talk to anyway.
#
# Each pkg is installed individually so a single transient failure (most
# common: gpiozero pulling lgpio on Bookworm) doesn't poison the others.
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  if grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null; then
    log "step 1b/14: Pi hardware Python libs (RPi.GPIO, gpiozero, DHT)"
    PI_PIP_PKGS=(
      "RPi.GPIO>=0.7"
      "gpiozero>=2.0"
      "adafruit-blinka>=8.0"
      "adafruit-circuitpython-dht>=4.0"
    )
    # We need the venv to exist before we can pip into it. The main venv
    # creation is at step 5/14 below, so just stash the package list in
    # an env var and install it then.
    SKYTRACK_PI_PIP="${PI_PIP_PKGS[*]}"
    export SKYTRACK_PI_PIP
    info "queued Pi pip packages for step 5: $SKYTRACK_PI_PIP"
  else
    info "step 1b/14: not on a Raspberry Pi (no /proc/device-tree/model match) — skipping GPIO libs"
  fi
fi

# ---------------------------------------------------------------------------
# 2. purge desktop leaks
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  if [[ "$KEEP_DESKTOP" == true ]]; then
    info "step 2/14: --keep-desktop — not purging raspberrypi-ui-mods / libreoffice*"
  else
    log "step 2/14: purging desktop packages that would leak Linux UI"
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
  log "step 3/14: users ($SKYTRACK_USER, $KIOSK_USER)"
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
  log "step 4/14: sync repo → $REPO_DIR"
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
log "step 5/14: python venv at $VENV_DIR"
if [[ -d "$REPO_DIR/venv" && ! -L "$REPO_DIR/venv" ]]; then
  warn "legacy $REPO_DIR/venv exists alongside new .venv — leaving in place (safe to rm)"
fi
if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
  python3 -m venv "$VENV_DIR" >> "$LOG_FILE" 2>&1
fi
"$VENV_DIR/bin/pip" install --upgrade pip >> "$LOG_FILE" 2>&1
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt" >> "$LOG_FILE" 2>&1 || warn "pip install had errors"

# Pi hardware Python libs queued by step 1b. Install one at a time so a
# single bad wheel (most common: lgpio failing to build on first boot
# because libgpiod headers landed late) doesn't take down the others.
if [[ -n "${SKYTRACK_PI_PIP:-}" ]]; then
  log "step 5b/14: installing Pi hardware Python libs into $VENV_DIR"
  for pi_pkg in $SKYTRACK_PI_PIP; do
    if "$VENV_DIR/bin/pip" install --quiet "$pi_pkg" >> "$LOG_FILE" 2>&1; then
      info "installed $pi_pkg"
    else
      warn "failed to install $pi_pkg — buzzer/sensor may stay in mock mode until fixed"
    fi
  done
fi

# ---------------------------------------------------------------------------
# 6. /etc/skytrack/skytrack.env
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 6/14: /etc/skytrack/skytrack.env"
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
# 7. data + log directories (+ OTA workspace home + .ssh/known_hosts)
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 7/14: $DATA_DIR / $LOG_DIR (+ OTA workspace, .ssh)"
  # OTA runs out of $DATA_DIR/ota-workspace. Pre-create the parent, the
  # workspace dir itself, and a writable .ssh so git's HOME lookup for
  # known_hosts never fails when the service user has no real home entry.
  mkdir -p "$DATA_DIR" "$DATA_DIR/backups" "$DATA_DIR/ota-workspace" "$LOG_DIR"
  install -d -m 0700 -o "$SKYTRACK_USER" -g "$SKYTRACK_USER" "$DATA_DIR/.ssh"
  if [[ ! -f "$DATA_DIR/.ssh/known_hosts" ]]; then
    : > "$DATA_DIR/.ssh/known_hosts"
  fi
  chown "$SKYTRACK_USER:$SKYTRACK_USER" "$DATA_DIR/.ssh/known_hosts"
  chmod 0600 "$DATA_DIR/.ssh/known_hosts"
  chown -R "$SKYTRACK_USER:$SKYTRACK_USER" "$DATA_DIR" "$LOG_DIR"
fi

# ---------------------------------------------------------------------------
# 8. device ID + DB + splash device_id.js
# ---------------------------------------------------------------------------
log "step 8/14: device ID + database migrate + splash device_id.js"
SUDO_AS=""
if [[ "$DEV_INSTALL" != true ]]; then
  SUDO_AS="sudo -u $SKYTRACK_USER -H"
fi
$SUDO_AS env SKYTRACK_REPO_DIR="$REPO_DIR" SKYTRACK_DATA_DIR="$DATA_DIR" \
  "$VENV_DIR/bin/python" "$REPO_DIR/scripts/db_migrate.py" >> "$LOG_FILE" 2>&1 \
  || warn "db_migrate had errors"

# Bootstrap the device ID AND write /opt/skytrack/static/splash/device_id.js
# so the file:// splash can render the device-ID pill before skytrack-app
# is listening. The file is otherwise written by firstboot.sh on next boot;
# doing it here means the splash already has it on the very first kiosk
# launch, without waiting for a reboot.
$SUDO_AS env SKYTRACK_REPO_DIR="$REPO_DIR" SKYTRACK_DATA_DIR="$DATA_DIR" \
  "$VENV_DIR/bin/python" - <<PY_EOF >> "$LOG_FILE" 2>&1 || warn "device_id init / splash js failed"
import json
import os
import sys

repo = os.environ.get('SKYTRACK_REPO_DIR', '/opt/skytrack')
sys.path.insert(0, repo)
import device_id  # noqa: E402
from _version import __version__ as SKYTRACK_VERSION  # noqa: E402

ident = device_id.get_or_create_device_id()
print('device_id:', ident['device_id'])
print('version:',   SKYTRACK_VERSION)

splash_js = os.path.join(repo, 'static', 'splash', 'device_id.js')
os.makedirs(os.path.dirname(splash_js), exist_ok=True)
with open(splash_js, 'w') as f:
    f.write(
        '// Generated by install.sh step 8 (also by scripts/firstboot.sh).\n'
        f'window.SKYTRACK_DEVICE_ID = {json.dumps(ident["device_id"])};\n'
        f'window.SKYTRACK_VERSION   = {json.dumps(SKYTRACK_VERSION)};\n'
    )
print('splash device_id.js:', splash_js)
PY_EOF

# The file was created by the skytrack user (or as root in dev mode); in
# either case make sure it's world-readable so skytrack-kiosk can load it
# via file:// from Chromium.
if [[ -f "$REPO_DIR/static/splash/device_id.js" ]]; then
  chmod 0644 "$REPO_DIR/static/splash/device_id.js" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 9. vendor JS
# ---------------------------------------------------------------------------
log "step 9/14: vendor JS"
"$REPO_DIR/scripts/fetch_vendor.sh" >> "$LOG_FILE" 2>&1 || warn "vendor fetch had errors"

# ---------------------------------------------------------------------------
# 10. appliance provisioning (plymouth + cmdline + ttys + openbox + Xwrapper
#     + /usr/share/skytrack/splash-fallback.html)
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 10/14: appliance provisioning"
  if [[ -x "$REPO_DIR/scripts/configure_appliance.sh" ]]; then
    SKYTRACK_REPO_DIR="$REPO_DIR" "$REPO_DIR/scripts/configure_appliance.sh" \
      >> "$LOG_FILE" 2>&1 || warn "configure_appliance had errors"
  else
    warn "scripts/configure_appliance.sh not executable — skipping"
  fi
fi

# ---------------------------------------------------------------------------
# 11. systemd units + polkit rule
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 11/14: systemd units"
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

  # Polkit rule — lets the unprivileged skytrack service user bounce its
  # own systemd units (Restart buttons in Settings, OTA self-restart,
  # hotspot apply/rollback). Without this, every restart endpoint fails
  # with "Interactive authentication required" on a real appliance.
  if [[ -f "$REPO_DIR/config_templates/skytrack.polkit.rules" ]] && \
     [[ -d /etc/polkit-1/rules.d ]]; then
    install -m 0644 -o root -g root \
      "$REPO_DIR/config_templates/skytrack.polkit.rules" \
      /etc/polkit-1/rules.d/50-skytrack.rules
    info "installed polkit rule → /etc/polkit-1/rules.d/50-skytrack.rules"
  elif [[ ! -d /etc/polkit-1/rules.d ]]; then
    warn "/etc/polkit-1/rules.d missing — restart buttons may fail until polkit is installed"
  fi
fi

# ---------------------------------------------------------------------------
# 12. hotspot bring-up
# ---------------------------------------------------------------------------
if [[ "$SKIP_HOTSPOT" != true && "$DEV_INSTALL" != true ]]; then
  log "step 12/14: applying wlan0 hotspot"
  # If we're on SSH over wlan0, hotspot_apply.sh will refuse — that's intentional.
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    warn "SSH session detected — hotspot will be applied on next boot via skytrack-hotspot.service"
  else
    "$REPO_DIR/scripts/hotspot_apply.sh" || warn "hotspot apply returned non-zero"
  fi
fi

# ---------------------------------------------------------------------------
# 12b. operator verification scripts — symlink to /usr/local/bin
#
# install.sh already pulls `jq` via PKGS above, so on a fresh install both
# scripts and jq are guaranteed present. This step just creates the two
# operator-friendly aliases:
#
#     skytrack-quick-check  → /opt/skytrack/scripts/quick_check.sh
#     skytrack-long-check   → /opt/skytrack/scripts/long_check.sh
#
# Idempotent: ln -sfn replaces a stale link, never errors on re-run.
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 12b/14: operator verification scripts"
  # Defensive: top-up jq in case an existing image was installed before jq
  # was added to PKGS. apt is idempotent so this is essentially free.
  if ! command -v jq >/dev/null 2>&1; then
    apt-get install -y -qq jq >> "$LOG_FILE" 2>&1 \
      || warn "could not install jq — quick_check / long_check will not format JSON"
  fi
  for s in quick_check long_check; do
    src="$REPO_DIR/scripts/${s}.sh"
    link="/usr/local/bin/skytrack-${s//_/-}"
    if [[ -f "$src" ]]; then
      chmod 0755 "$src" 2>/dev/null || true
      ln -sfn "$src" "$link" \
        && info "linked $link -> $src" \
        || warn "could not link $link"
    else
      warn "missing $src — skipping symlink"
    fi
  done
fi

# ---------------------------------------------------------------------------
# 13. start app + display (runtime smoke-test targets)
# ---------------------------------------------------------------------------
if [[ "$DEV_INSTALL" != true ]]; then
  log "step 13/14: starting services"
  systemctl restart skytrack-app.service 2>>"$LOG_FILE" || warn "skytrack-app failed to start"

  # Wait for /healthz to answer before declaring the backend ready. We give
  # it up to 20s — on a cold install the first request can take a second.
  if command -v curl >/dev/null 2>&1; then
    for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      if curl -fsS --max-time 2 http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
        info "skytrack-app /healthz is live"
        break
      fi
      sleep 1
    done
  fi

  if [[ "$SKIP_RUNTIME" != true ]]; then
    info "starting skytrack-display.service (takes over tty1)"
    systemctl start skytrack-display.service 2>>"$LOG_FILE" || \
      warn "skytrack-display.service failed to start"
    for _i in 1 2 3 4 5 6 7 8 9 10; do
      systemctl is-active --quiet skytrack-display.service && break
      sleep 1
    done
  else
    info "--skip-runtime — skytrack-display.service will start on next boot"
  fi
fi

# ---------------------------------------------------------------------------
# 14. run install_verify.sh — HARD FAIL on any issue
# ---------------------------------------------------------------------------
log "step 14/14: install_verify.sh"
if [[ "$DEV_INSTALL" != true ]]; then
  VERIFY_ARGS=()
  if [[ "$SKIP_RUNTIME" == true ]]; then
    VERIFY_ARGS+=("--no-runtime")
  fi
  if ! "$REPO_DIR/scripts/install_verify.sh" "${VERIFY_ARGS[@]}"; then
    err "install_verify FAILED — install is NOT complete. See the failures above."
    err "Fix the reported issues and re-run: sudo $REPO_DIR/install.sh"
    exit 1
  fi
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
  echo "Appliance install complete and verified."
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
