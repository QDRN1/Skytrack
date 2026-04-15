#!/usr/bin/env bash
# Bring up (or restart) the SkyTrack-Portal hotspot on wlan0.
#
#   hostapd     — WPA2 access point on channel 6
#   dnsmasq     — DHCP 10.4.26.100-200 + DNS hijack to 10.4.26.89
#   dhcpcd      — static address 10.4.26.89/24 on wlan0
#
# SAFETY:
#   • If invoked over SSH on wlan0, abort (the operator would lock themselves out).
#   • Snapshot existing config files before overwriting.
#   • Spawn a 90-second watchdog: if the new config doesn't bring the
#     interface up + dnsmasq up, roll back via hotspot_rollback.sh.
#
# This script is idempotent. Re-run with --reapply to bounce services
# without rewriting config files.

set -euo pipefail

REPO_DIR="${SKYTRACK_REPO_DIR:-/opt/skytrack}"
TMPL_DIR="$REPO_DIR/config_templates"
DATA_DIR="${SKYTRACK_DATA_DIR:-/var/lib/skytrack}"
AUTH_JSON="$DATA_DIR/auth.json"

SSID="${SKYTRACK_SSID:-SkyTrack-Portal}"
COUNTRY="${SKYTRACK_COUNTRY:-US}"
CHANNEL="${SKYTRACK_CHANNEL:-6}"
GATEWAY="${SKYTRACK_GATEWAY:-10.4.26.89}"
SUBNET="${SKYTRACK_SUBNET:-10.4.26.0/24}"
DHCP_START="${SKYTRACK_DHCP_START:-10.4.26.100}"
DHCP_END="${SKYTRACK_DHCP_END:-10.4.26.200}"

REAPPLY=0
[[ "${1:-}" == "--reapply" ]] && REAPPLY=1

# ---------------------------------------------------------------------------
# Safety: don't lock out an SSH session that's coming in over wlan0
# ---------------------------------------------------------------------------
if [[ -n "${SSH_CONNECTION:-}" ]]; then
  ssh_iface="$(ip -o route get "${SSH_CONNECTION%% *}" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')"
  if [[ "$ssh_iface" == "wlan0" ]]; then
    echo "ABORT: SSH session is on wlan0 — bringing the hotspot up would disconnect you."
    echo "       Re-run from a console or over eth0/cellular."
    exit 2
  fi
fi

# ---------------------------------------------------------------------------
# Render configs (only on full apply, not on --reapply)
# ---------------------------------------------------------------------------
if [[ $REAPPLY -eq 0 ]]; then
  if [[ -z "${SKYTRACK_HOTSPOT_PASSWORD:-}" ]]; then
    if [[ -f "$AUTH_JSON" ]]; then
      SKYTRACK_HOTSPOT_PASSWORD="$(python3 -c "import json,sys; print(json.load(open('$AUTH_JSON')).get('hotspot_password') or '', end='')")"
    fi
  fi
  if [[ -z "${SKYTRACK_HOTSPOT_PASSWORD:-}" ]]; then
    echo "first-boot mode: hotspot will be OPEN until the user finishes /setup"
    OPEN_HOTSPOT=1
  else
    OPEN_HOTSPOT=0
  fi

  ts="$(date +%Y%m%d-%H%M%S)"
  for f in /etc/hostapd/hostapd.conf /etc/dnsmasq.d/skytrack.conf /etc/dhcpcd.conf; do
    [[ -f "$f" ]] && cp "$f" "$f.$ts.bak" || true
  done

  echo "writing hostapd.conf"
  if [[ $OPEN_HOTSPOT -eq 1 ]]; then
    cat > /etc/hostapd/hostapd.conf <<EOF
country_code=$COUNTRY
interface=wlan0
ssid=$SSID
hw_mode=g
channel=$CHANNEL
auth_algs=1
ignore_broadcast_ssid=0
EOF
  else
    cat > /etc/hostapd/hostapd.conf <<EOF
country_code=$COUNTRY
interface=wlan0
ssid=$SSID
hw_mode=g
channel=$CHANNEL
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase=$SKYTRACK_HOTSPOT_PASSWORD
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
ignore_broadcast_ssid=0
EOF
  fi
  chmod 600 /etc/hostapd/hostapd.conf

  echo "writing dnsmasq skytrack drop-in"
  mkdir -p /etc/dnsmasq.d
  cat > /etc/dnsmasq.d/skytrack.conf <<EOF
interface=wlan0
bind-interfaces
dhcp-range=$DHCP_START,$DHCP_END,255.255.255.0,12h
dhcp-option=3,$GATEWAY
dhcp-option=6,$GATEWAY
domain-needed
bogus-priv
address=/skytrack.local/$GATEWAY
address=/setup.skytrack.local/$GATEWAY
EOF

  # dhcpcd block — only if dhcpcd is actually installed (Raspberry Pi OS
  # Bookworm replaced dhcpcd with NetworkManager, so /etc/dhcpcd.conf may
  # not exist). When absent, we assign the wlan0 static IP directly with
  # `ip addr add` below instead of teaching dhcpcd about it.
  if [[ -f /etc/dhcpcd.conf ]]; then
    echo "writing dhcpcd skytrack block"
    if ! grep -q "## skytrack-hotspot" /etc/dhcpcd.conf 2>/dev/null; then
      cat >> /etc/dhcpcd.conf <<EOF

## skytrack-hotspot
interface wlan0
static ip_address=$GATEWAY/24
nohook wpa_supplicant
EOF
    fi
  else
    echo "note: /etc/dhcpcd.conf absent (Bookworm / NetworkManager) — will use ip addr"
  fi
fi

# ---------------------------------------------------------------------------
# Bring services up
#
# The whole point of this script is "best-effort bring up". On a fresh
# Bookworm Lite image any of the following can be true at install time:
#
#   • dnsmasq.service is masked by the libnss-myhostname/systemd-resolved
#     stack and a plain `systemctl restart dnsmasq` exits non-zero
#   • hostapd.service is masked because it ships disabled by default
#   • dhcpcd.service doesn't exist at all (Bookworm uses NetworkManager)
#
# Under `set -e` ANY non-zero exit aborts the whole oneshot — which is
# exactly what was making `skytrack-hotspot.service` show as `failed` in
# Long Check even though the underlying problem was a single restart.
# Every restart below now: unmasks first, enables, then restarts with
# `|| true` so a single subsystem hiccup can't poison the whole apply.
# The watchdog at the bottom of this file is the real safety net — it
# detects "hostapd never came up" and rolls back.
# ---------------------------------------------------------------------------
echo "restarting dhcpcd / hostapd / dnsmasq"
systemctl unmask hostapd  2>/dev/null || true
systemctl unmask dnsmasq  2>/dev/null || true
systemctl enable hostapd  2>/dev/null || true
systemctl enable dnsmasq  2>/dev/null || true

# dhcpcd: only present on older Raspberry Pi OS / Debian. On Bookworm-Pi
# it's been replaced by NetworkManager — the unit file doesn't exist and
# `systemctl restart dhcpcd` would exit non-zero and abort the script
# under `set -e`. Detect and skip; assign the static gateway IP directly
# so hostapd has something to bind to.
if systemctl list-unit-files dhcpcd.service >/dev/null 2>&1 && \
   systemctl cat dhcpcd.service >/dev/null 2>&1; then
  systemctl restart dhcpcd || echo "warn: dhcpcd restart failed (continuing)"
else
  echo "note: dhcpcd.service not present — assigning $GATEWAY/24 to wlan0 directly"
  # NetworkManager may also be managing wlan0. Detach it so our static
  # address sticks. Silent-best-effort — NM may not be installed.
  nmcli dev set wlan0 managed no >/dev/null 2>&1 || true
  ip addr flush dev wlan0 2>/dev/null || true
  ip addr add "$GATEWAY/24" dev wlan0 2>/dev/null || true
  ip link set wlan0 up 2>/dev/null || true
fi
sleep 1

# hostapd is the layer that actually serves the wifi beacon. If this fails
# the watchdog at the bottom rolls everything back, so we DO want to know
# about it — but we still don't want `set -e` to short-circuit dnsmasq.
if ! systemctl restart hostapd; then
  echo "warn: hostapd restart failed (watchdog will roll back if it stays down)"
fi

# dnsmasq is the most common offender on a fresh image. We've already
# unmasked + enabled it above; if the restart still fails we report it
# loudly but DO NOT abort — the apply script has done its job (configs
# written, hostapd attempted), and Quick/Long Check will surface the
# DHCP-down state truthfully so the operator sees what's wrong.
if ! systemctl restart dnsmasq; then
  echo "warn: dnsmasq restart failed — hotspot will be 'enabled (DHCP down)' until fixed"
  systemctl status dnsmasq --no-pager 2>&1 | sed 's/^/  dnsmasq: /' || true
fi

# ---------------------------------------------------------------------------
# Watchdog rollback — phase 2.2
#
# Replaced an inline `(sleep 90; check) &` subshell with a transient
# systemd unit scheduled 90s in the future. Rationale:
#
#   • Orphaned subshells lose their parent's environment and can be
#     killed by systemd when the apply oneshot exits, making the
#     rollback unreliable precisely when it matters.
#   • A transient unit is visible in `systemctl list-timers` /
#     `journalctl -u skytrack-hotspot-watchdog.service` — one place
#     to see why a rollback happened (or didn't). The subshell was
#     invisible.
#   • On a dev box without systemd-run, we skip the schedule entirely
#     rather than fall back to a fragile subshell. The hotspot truth
#     probe on the kiosk covers that case truthfully.
# ---------------------------------------------------------------------------
if command -v systemd-run >/dev/null 2>&1; then
  # Cancel any pending watchdog from a prior apply so they don't stack.
  # `stop` on a non-existent transient unit is a no-op.
  systemctl stop skytrack-hotspot-watchdog.service 2>/dev/null || true
  systemd-run \
    --unit=skytrack-hotspot-watchdog.service \
    --on-active=90s \
    --description="SkyTrack hotspot post-apply watchdog" \
    "$REPO_DIR/scripts/hotspot_watchdog.sh" 2>/dev/null \
    || echo "warn: could not schedule hotspot watchdog (systemd-run failed)"
else
  echo "note: systemd-run not available — skipping watchdog schedule"
fi

echo "hotspot apply complete (SSID=$SSID gateway=$GATEWAY)"
