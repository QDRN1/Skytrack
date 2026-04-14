#!/usr/bin/env bash
# =============================================================================
# SkyTrack Quick Check — operator-friendly daily sanity check
#
#   /opt/skytrack/scripts/quick_check.sh
#   skytrack-quick-check                  (symlink in /usr/local/bin)
#
# Compact, scannable summary of the appliance's most important truth
# values. Designed to be readable in 2-5 seconds. Uses the public
# /api/onboarding/state, /api/network/state, /api/hardware/summary and
# /healthz endpoints — no admin authentication required.
#
# Exit codes:
#   0  every check looked healthy
#   1  one or more checks were unhealthy or unreachable
#
# This script is read-only — it never mutates persistent state.
# =============================================================================

set -uo pipefail

REPO_DIR="${SKYTRACK_REPO_DIR:-/opt/skytrack}"
HOST="${SKYTRACK_HOST:-127.0.0.1}"
PORT="${SKYTRACK_PORT:-8080}"
BASE="http://${HOST}:${PORT}"

# ---------------------------------------------------------------------------
# Color helpers — minimal, off when stdout isn't a tty
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_GREEN=""; C_YELLOW=""; C_RED=""
fi

ok()    { printf "%s%s%s "  "$C_GREEN"  "OK"   "$C_RESET"; }
warn()  { printf "%s%s%s "  "$C_YELLOW" "WARN" "$C_RESET"; }
bad()   { printf "%s%s%s "  "$C_RED"    "FAIL" "$C_RESET"; }

UNHEALTHY=0
mark_bad()  { UNHEALTHY=1; }

# ---------------------------------------------------------------------------
# Pre-flight: jq must be installed. We try once to install it ourselves if
# we're root and apt is available, otherwise we tell the operator how.
# ---------------------------------------------------------------------------
if ! command -v jq >/dev/null 2>&1; then
  if [[ $EUID -eq 0 ]] && command -v apt-get >/dev/null 2>&1; then
    echo "${C_DIM}quick_check: installing missing 'jq' once...${C_RESET}" >&2
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq jq >/dev/null 2>&1 || true
  fi
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "${C_RED}ERROR${C_RESET}: 'jq' is not installed. Install it with:" >&2
  echo "  sudo apt-get install -y jq" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# HTTP helper — short timeout, returns empty string on failure (never errors).
# ---------------------------------------------------------------------------
api() {
  local path="$1"
  curl -fsS --max-time 2 "${BASE}${path}" 2>/dev/null || true
}

# Pull JSON once, reuse with jq — saves three round-trips.
HEALTHZ_JSON="$(api /healthz)"
ONBOARD_JSON="$(api /api/onboarding/state)"
NETSTATE_JSON="$(api /api/network/state)"
HARDWARE_JSON="$(api /api/hardware/summary)"

# Read app version from _version.py if reachable; fall back to /healthz.
APP_VERSION="$(grep -oE '__version__\s*=\s*"[^"]+"' "$REPO_DIR/_version.py" 2>/dev/null \
                | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
if [[ -z "$APP_VERSION" && -n "$HEALTHZ_JSON" ]]; then
  APP_VERSION="$(echo "$HEALTHZ_JSON" | jq -r '.version // empty' 2>/dev/null)"
fi
[[ -z "$APP_VERSION" ]] && APP_VERSION="unknown"

# ---------------------------------------------------------------------------
# 1. App service state
# ---------------------------------------------------------------------------
APP_STATE="unknown"
if command -v systemctl >/dev/null 2>&1; then
  # systemctl is-active prints "active"/"inactive"/etc. to stdout AND
  # exits non-zero for anything that isn't "active". Capture stdout
  # without the OR-fallback so we don't get a doubled-up value.
  APP_STATE="$(systemctl is-active skytrack-app.service 2>/dev/null)"
  [[ -z "$APP_STATE" ]] && APP_STATE="unknown"
fi

if [[ -z "$HEALTHZ_JSON" ]]; then
  APP_STATE_LABEL="${C_RED}down${C_RESET} (/healthz unreachable)"
  mark_bad
else
  # /healthz answered, so the app IS running. systemctl just adds detail
  # about HOW it's running (under skytrack-app.service vs. ad-hoc).
  case "$APP_STATE" in
    active)            APP_STATE_LABEL="${C_GREEN}running${C_RESET}" ;;
    inactive|failed)   APP_STATE_LABEL="${C_GREEN}running${C_RESET} ${C_DIM}(unit ${APP_STATE})${C_RESET}" ;;
    unknown|not-found) APP_STATE_LABEL="${C_GREEN}running${C_RESET} ${C_DIM}(no systemd unit)${C_RESET}" ;;
    *)                 APP_STATE_LABEL="${C_GREEN}running${C_RESET} ${C_DIM}(${APP_STATE})${C_RESET}" ;;
  esac
fi

# ---------------------------------------------------------------------------
# 2. Onboarding
# ---------------------------------------------------------------------------
if [[ -n "$ONBOARD_JSON" ]]; then
  ONB_STAGE="$(   echo "$ONBOARD_JSON" | jq -r '.stage // "unknown"')"
  ONB_NEXT="$(    echo "$ONBOARD_JSON" | jq -r '.next_stage // "—"')"
  ONB_PIN="$(     echo "$ONBOARD_JSON" | jq -r 'if .pin_set    then "yes" else "no" end')"
  ONB_CONFIG="$(  echo "$ONBOARD_JSON" | jq -r 'if .configured then "yes" else "no" end')"
else
  ONB_STAGE="unreachable"; ONB_NEXT="—"; ONB_PIN="?"; ONB_CONFIG="?"
  mark_bad
fi

# ---------------------------------------------------------------------------
# 3. Network
# ---------------------------------------------------------------------------
if [[ -n "$NETSTATE_JSON" ]]; then
  WIFI_CONNECTED="$(echo "$NETSTATE_JSON" | jq -r 'if .wifi.connected then "yes" else "no" end')"
  WIFI_SSID="$(    echo "$NETSTATE_JSON" | jq -r '.wifi.ssid // ""')"
  CELL_BEARER="$(  echo "$NETSTATE_JSON" | jq -r 'if .cellular.bearer_connected then "yes" else "no" end')"
  CELL_ROUTE="$(   echo "$NETSTATE_JSON" | jq -r 'if .cellular.route_active   then "yes" else "no" end')"
  HOTSPOT_ON="$(   echo "$NETSTATE_JSON" | jq -r 'if .hotspot.enabled         then "yes" else "no" end')"
  PRIMARY="$(      echo "$NETSTATE_JSON" | jq -r '.routing.primary // "—"')"
  PRIMARY_IFACE="$(echo "$NETSTATE_JSON" | jq -r '.routing.primary_interface // ""')"
  ONLINE="$(       echo "$NETSTATE_JSON" | jq -r 'if .routing.internet_reachable then "yes" else "no" end')"
else
  WIFI_CONNECTED="?"; WIFI_SSID=""
  CELL_BEARER="?";    CELL_ROUTE="?"
  HOTSPOT_ON="?";     PRIMARY="—"; PRIMARY_IFACE=""; ONLINE="?"
  mark_bad
fi

WIFI_LABEL="$WIFI_CONNECTED"
[[ "$WIFI_CONNECTED" == "yes" && -n "$WIFI_SSID" ]] && WIFI_LABEL="connected (${WIFI_SSID})"
[[ "$WIFI_CONNECTED" == "no" ]] && WIFI_LABEL="off"

CELL_LABEL="$CELL_BEARER"
if [[ "$CELL_BEARER" == "yes" && "$CELL_ROUTE" == "yes" ]]; then
  CELL_LABEL="connected (route active)"
elif [[ "$CELL_BEARER" == "yes" ]]; then
  CELL_LABEL="bearer up (no route)"
elif [[ "$CELL_BEARER" == "no" ]]; then
  CELL_LABEL="off"
fi

ROUTE_LABEL="$PRIMARY"
[[ -n "$PRIMARY_IFACE" ]] && ROUTE_LABEL="${PRIMARY} (${PRIMARY_IFACE})"

# ---------------------------------------------------------------------------
# 4. Hardware (sensor + buzzer + GPS placeholder)
# ---------------------------------------------------------------------------
if [[ -n "$HARDWARE_JSON" ]]; then
  SENSOR_SRC="$(   echo "$HARDWARE_JSON" | jq -r '.sensor.source // "unknown"')"
  SENSOR_TEMP="$(  echo "$HARDWARE_JSON" | jq -r '.sensor.temperature_f // empty')"
  BUZZER_AVAIL="$( echo "$HARDWARE_JSON" | jq -r 'if .buzzer.available then "yes" else "no" end')"
  GPS_STATE="$(    echo "$HARDWARE_JSON" | jq -r '.gps.state // "unknown"')"
else
  SENSOR_SRC="?"; SENSOR_TEMP=""; BUZZER_AVAIL="?"; GPS_STATE="?"
fi

SENSOR_LABEL="$SENSOR_SRC"
if [[ -n "$SENSOR_TEMP" && "$SENSOR_TEMP" != "null" ]]; then
  SENSOR_LABEL="${SENSOR_SRC} (${SENSOR_TEMP}°F)"
fi

# Color the sensor source so 'mock' on a deployed unit jumps out.
case "$SENSOR_SRC" in
  real)            SENSOR_COLOR="$C_GREEN" ;;
  cached)          SENSOR_COLOR="$C_YELLOW" ;;
  mock)            SENSOR_COLOR="$C_YELLOW" ;;
  error|unknown|?) SENSOR_COLOR="$C_RED"; mark_bad ;;
  *)               SENSOR_COLOR="" ;;
esac

case "$BUZZER_AVAIL" in
  yes) BUZZER_COLOR="$C_GREEN" ;;
  no)  BUZZER_COLOR="$C_YELLOW" ;;
  *)   BUZZER_COLOR="$C_RED" ;;
esac

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
printf "\n${C_BOLD}## SkyTrack Quick Check${C_RESET}  ${C_DIM}(%s)${C_RESET}\n\n" "$(date '+%Y-%m-%d %H:%M:%S')"

printf "  Version       : %s\n" "$APP_VERSION"
printf "  App           : %b\n" "$APP_STATE_LABEL"
printf "  Onboarding    : %s -> %s\n" "$ONB_STAGE" "$ONB_NEXT"
printf "  PIN Set       : %s\n" "$ONB_PIN"
printf "  Configured    : %s\n" "$ONB_CONFIG"
printf "  WiFi          : %s\n" "$WIFI_LABEL"
printf "  Cellular      : %s\n" "$CELL_LABEL"
printf "  Hotspot       : %s\n" "$HOTSPOT_ON"
printf "  Primary Route : %s\n" "$ROUTE_LABEL"
printf "  Internet      : %s\n" "$ONLINE"
printf "  GPS           : %s\n" "$GPS_STATE"
printf "  Sensor        : %b%s%b\n" "$SENSOR_COLOR" "$SENSOR_LABEL" "$C_RESET"
printf "  Buzzer        : %b%s%b\n" "$BUZZER_COLOR" "$BUZZER_AVAIL" "$C_RESET"
echo

if (( UNHEALTHY != 0 )); then
  printf "  ${C_YELLOW}!! one or more checks were unhealthy — run skytrack-long-check for details${C_RESET}\n\n"
  exit 1
fi

exit 0
