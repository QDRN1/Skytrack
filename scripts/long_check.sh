#!/usr/bin/env bash
# =============================================================================
# SkyTrack Long Check — operator-friendly troubleshooting dump
#
#   /opt/skytrack/scripts/long_check.sh
#   skytrack-long-check                   (symlink in /usr/local/bin)
#
# Sectioned, human-readable diagnostics used when something is off and
# you want every relevant data point in one place. Goes deeper than
# quick_check.sh:
#
#   1. Version + uptime
#   2. systemd unit status (skytrack-app, display, ModemManager,
#      NetworkManager, hostapd, dnsmasq if installed)
#   3. /healthz output
#   4. Onboarding state — full JSON
#   5. Network state — full normalized JSON
#   6. Hardware summary — full JSON
#   7. Modem listing (mmcli -L)
#   8. Active modem details (mmcli -m <id>)
#   9. NetworkManager active connections
#  10. Default route table
#  11. Per-interface IPv4 addresses
#  12. Hotspot session (ssid + password presence + countdown)
#  13. Quick log tail (last 20 lines from skytrack-app)
#
# Read-only — never mutates persistent state.
# =============================================================================

set -uo pipefail

REPO_DIR="${SKYTRACK_REPO_DIR:-/opt/skytrack}"
HOST="${SKYTRACK_HOST:-127.0.0.1}"
PORT="${SKYTRACK_PORT:-8080}"
BASE="http://${HOST}:${PORT}"

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'
  C_CYAN=$'\033[36m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_CYAN=""
fi

hdr() {
  printf "\n${C_BOLD}${C_CYAN}== %s ==${C_RESET}\n\n" "$*"
}

note() { printf "  ${C_DIM}%s${C_RESET}\n" "$*"; }

# ---------------------------------------------------------------------------
# Pre-flight: jq required for pretty-printing the JSON sections.
# ---------------------------------------------------------------------------
if ! command -v jq >/dev/null 2>&1; then
  if [[ $EUID -eq 0 ]] && command -v apt-get >/dev/null 2>&1; then
    echo "${C_DIM}long_check: installing missing 'jq' once...${C_RESET}" >&2
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq jq >/dev/null 2>&1 || true
  fi
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "${C_RED}ERROR${C_RESET}: 'jq' is not installed. Install it with:" >&2
  echo "  sudo apt-get install -y jq" >&2
  exit 2
fi

api() {
  curl -fsS --max-time 3 "${BASE}$1" 2>/dev/null || true
}

pretty() {
  # Pretty-print stdin as JSON; if it isn't valid JSON, print it raw with
  # a marker so the operator can see what came back instead.
  local body
  body="$(cat)"
  if [[ -z "$body" ]]; then
    echo "  (empty / endpoint unreachable)"
    return
  fi
  if echo "$body" | jq . >/dev/null 2>&1; then
    echo "$body" | jq .
  else
    echo "  (non-JSON response)"
    echo "$body" | sed 's/^/  /'
  fi
}

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
APP_VERSION="$(grep -oE '__version__\s*=\s*"[^"]+"' "$REPO_DIR/_version.py" 2>/dev/null \
                | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
[[ -z "$APP_VERSION" ]] && APP_VERSION="unknown"

UPTIME="$(uptime -p 2>/dev/null || uptime || echo "unknown")"
HOST_NAME="$(hostname 2>/dev/null || echo "unknown")"

printf "\n${C_BOLD}## SkyTrack Long Check${C_RESET}  ${C_DIM}(%s)${C_RESET}\n" "$(date '+%Y-%m-%d %H:%M:%S')"
printf "  host    : %s\n"  "$HOST_NAME"
printf "  version : %s\n"  "$APP_VERSION"
printf "  uptime  : %s\n"  "$UPTIME"

# ---------------------------------------------------------------------------
# 1. systemd units
# ---------------------------------------------------------------------------
hdr "systemd units"
UNITS=(
  skytrack-app.service
  skytrack-display.service
  skytrack-network.service
  skytrack-ingest.service
  skytrack-hardware.service
  skytrack-firstboot.service
  skytrack-hotspot.service
  ModemManager.service
  NetworkManager.service
  hostapd.service
  dnsmasq.service
)
for unit in "${UNITS[@]}"; do
  # NB: systemctl prints "not-found"/"inactive"/"disabled" to stdout AND
  # exits non-zero in those states. Use plain capture (no `||` fallback)
  # so we don't end up with both the real word AND our placeholder.
  state="$(systemctl is-active  "$unit" 2>/dev/null)"
  enabled="$(systemctl is-enabled "$unit" 2>/dev/null)"
  [[ -z "$state"   ]] && state="unknown"
  [[ -z "$enabled" ]] && enabled="—"
  case "$state" in
    active)   color="$C_GREEN" ;;
    inactive) color="$C_DIM" ;;
    failed)   color="$C_RED" ;;
    *)        color="$C_YELLOW" ;;
  esac
  printf "  %-36s %b%-10s%b  [%s]\n" "$unit" "$color" "$state" "$C_RESET" "$enabled"
done

# ---------------------------------------------------------------------------
# 2. /healthz
# ---------------------------------------------------------------------------
hdr "/healthz"
api /healthz | pretty

# ---------------------------------------------------------------------------
# 3. Onboarding (full JSON)
# ---------------------------------------------------------------------------
hdr "onboarding state"
api /api/onboarding/state | pretty

# ---------------------------------------------------------------------------
# 4. Network state (full normalized JSON)
# ---------------------------------------------------------------------------
hdr "network state (normalized)"
api /api/network/state | pretty

# ---------------------------------------------------------------------------
# 5. Hardware summary
# ---------------------------------------------------------------------------
hdr "hardware summary"
api /api/hardware/summary | pretty

# ---------------------------------------------------------------------------
# 6. Modem listing
# ---------------------------------------------------------------------------
hdr "modem listing (mmcli -L)"
if command -v mmcli >/dev/null 2>&1; then
  mmcli -L 2>&1 | sed 's/^/  /'
else
  note "mmcli not installed"
fi

# ---------------------------------------------------------------------------
# 7. Active modem details
# ---------------------------------------------------------------------------
hdr "active modem details"
if command -v mmcli >/dev/null 2>&1; then
  # First numeric path component of the first /Modem/N line
  modem_id="$(mmcli -L 2>/dev/null \
              | grep -oE '/Modem/[0-9]+' \
              | head -1 \
              | grep -oE '[0-9]+$' || true)"
  if [[ -n "$modem_id" ]]; then
    mmcli -m "$modem_id" 2>&1 | sed 's/^/  /'
  else
    note "no modem detected"
  fi
else
  note "mmcli not installed"
fi

# ---------------------------------------------------------------------------
# 8. NetworkManager active connections
# ---------------------------------------------------------------------------
hdr "NetworkManager active connections"
if command -v nmcli >/dev/null 2>&1; then
  nmcli -t -f NAME,UUID,TYPE,DEVICE,STATE connection show --active 2>&1 | sed 's/^/  /'
else
  note "nmcli not installed"
fi

# ---------------------------------------------------------------------------
# 9. Default route table
# ---------------------------------------------------------------------------
hdr "default routes"
if command -v ip >/dev/null 2>&1; then
  ip route show default 2>&1 | sed 's/^/  /'
else
  note "iproute2 not installed"
fi

# ---------------------------------------------------------------------------
# 10. Per-interface IPv4 addresses
# ---------------------------------------------------------------------------
hdr "interface IPv4 addresses"
for iface in wlan0 wwan0 eth0; do
  if command -v ip >/dev/null 2>&1; then
    addr="$(ip -4 -o addr show dev "$iface" 2>/dev/null \
            | awk '{print $4}' | tr '\n' ' ')"
    if [[ -n "$addr" ]]; then
      printf "  %-7s %s\n" "$iface" "$addr"
    else
      printf "  %-7s ${C_DIM}(no IPv4)${C_RESET}\n" "$iface"
    fi
  fi
done

# ---------------------------------------------------------------------------
# 11. Hotspot session
# ---------------------------------------------------------------------------
hdr "hotspot session"
if [[ -n "$(api /api/network/state)" ]]; then
  api /api/network/state | jq -r '
    .hotspot |
    "  enabled         : \(.enabled // false)\n" +
    "  configured      : \(.configured // false)\n" +
    "  service_running : \(.service_running // false)\n" +
    "  dhcp_active     : \(.dhcp_active // false)\n" +
    "  dhcp_installed  : \(.dhcp_installed // false)\n" +
    "  actually_usable : \(.actually_usable // false)\n" +
    "  ssid            : \(.ssid // "—")\n" +
    "  local_url       : \(.local_url // "—")\n" +
    "  gateway         : \(.gateway // "—")\n" +
    "  seconds_left    : \(.seconds_remaining // "—")"' 2>/dev/null
else
  note "/api/network/state unreachable"
fi

# ---------------------------------------------------------------------------
# 12. Recent skytrack-app logs (last 20 lines)
# ---------------------------------------------------------------------------
hdr "recent skytrack-app log"
if command -v journalctl >/dev/null 2>&1; then
  journalctl -u skytrack-app.service -n 20 --no-pager 2>/dev/null | sed 's/^/  /' \
    || note "journalctl returned an error"
else
  note "journalctl not installed"
fi

echo
printf "${C_DIM}long_check complete — paste this whole block when reporting an issue.${C_RESET}\n\n"
