#!/usr/bin/env bash
# Roll back a SkyTrack hotspot bring-up.
#
# Strategy:
#   1. Restore the most recent timestamped .bak files for hostapd / dnsmasq /
#      dhcpcd if they exist.
#   2. Otherwise, remove the SkyTrack drop-ins entirely so the system goes
#      back to whatever NetworkManager / wpa_supplicant default it had.
#   3. Stop hostapd + dnsmasq, restart dhcpcd, and (if available) bring
#      NetworkManager back online so the operator regains a console.
#
# Safe to run repeatedly. Returns 0 even on partial failures.
#
# Triggered by:
#   • the 90-second watchdog in hotspot_apply.sh
#   • the operator manually (`sudo /opt/skytrack/scripts/hotspot_rollback.sh`)

set -uo pipefail

REPO_DIR="${SKYTRACK_REPO_DIR:-/opt/skytrack}"

log() { echo "[hotspot-rollback] $*"; }

restore_latest_backup() {
  local target="$1"
  local latest
  latest="$(ls -1t "${target}".*.bak 2>/dev/null | head -n 1 || true)"
  if [[ -n "$latest" && -f "$latest" ]]; then
    log "restoring $target from $latest"
    cp "$latest" "$target" || true
  else
    log "no backup found for $target"
  fi
}

log "starting rollback"

# 1. hostapd
if [[ -f /etc/hostapd/hostapd.conf ]]; then
  restore_latest_backup /etc/hostapd/hostapd.conf
fi

# 2. dnsmasq drop-in (delete outright if no backup)
if [[ -f /etc/dnsmasq.d/skytrack.conf ]]; then
  if ls /etc/dnsmasq.d/skytrack.conf.*.bak >/dev/null 2>&1; then
    restore_latest_backup /etc/dnsmasq.d/skytrack.conf
  else
    log "removing /etc/dnsmasq.d/skytrack.conf"
    rm -f /etc/dnsmasq.d/skytrack.conf
  fi
fi

# 3. dhcpcd: strip the skytrack-hotspot block
if grep -q "## skytrack-hotspot" /etc/dhcpcd.conf 2>/dev/null; then
  log "stripping skytrack-hotspot block from /etc/dhcpcd.conf"
  # delete from the marker to EOF
  sed -i '/## skytrack-hotspot/,$d' /etc/dhcpcd.conf || true
fi
restore_latest_backup /etc/dhcpcd.conf

# 4. Stop hotspot-side services
log "stopping hostapd and dnsmasq"
systemctl stop hostapd 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true
systemctl disable hostapd 2>/dev/null || true

# 5. Bounce dhcpcd so wlan0 returns to its previous state
log "restarting dhcpcd"
systemctl restart dhcpcd 2>/dev/null || true

# 6. If NetworkManager is installed, hand wlan0 back to it
if systemctl list-unit-files | grep -q '^NetworkManager\.service'; then
  log "restarting NetworkManager"
  systemctl restart NetworkManager 2>/dev/null || true
  if command -v nmcli >/dev/null 2>&1; then
    nmcli device set wlan0 managed yes 2>/dev/null || true
  fi
fi

# 7. If wpa_supplicant is the default, kick it
if systemctl list-unit-files | grep -q '^wpa_supplicant\.service'; then
  log "restarting wpa_supplicant"
  systemctl restart wpa_supplicant 2>/dev/null || true
fi

log "rollback complete"
exit 0
