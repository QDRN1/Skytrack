#!/usr/bin/env bash
# SkyTrack connectivity watchdog.
#
# Called every 5 minutes by skytrack-connectivity-watchdog.timer.
# Tracks consecutive failures in a stamp file. If connectivity has
# been down for REBOOT_AFTER_MIN minutes, reboots the Pi so
# NetworkManager, the modem, and the tunnel all get a clean start.
#
# "Connectivity" = can reach 1.1.1.1 OR 8.8.8.8 on port 53.
# We try two targets so a single upstream blip doesn't count.

set -euo pipefail

STAMP="/tmp/skytrack-conn-watchdog-first-fail"
REBOOT_AFTER_MIN="${SKYTRACK_CONN_REBOOT_MIN:-30}"
REBOOT_AFTER_SEC=$((REBOOT_AFTER_MIN * 60))

check_connectivity() {
  timeout 5 bash -c 'echo >/dev/tcp/1.1.1.1/53' 2>/dev/null && return 0
  timeout 5 bash -c 'echo >/dev/tcp/8.8.8.8/53' 2>/dev/null && return 0
  return 1
}

if check_connectivity; then
  [ -f "$STAMP" ] && rm -f "$STAMP" && echo "connectivity restored — cleared failure stamp"
  exit 0
fi

echo "connectivity check failed"

if [ ! -f "$STAMP" ]; then
  date +%s > "$STAMP"
  echo "first failure recorded — will reboot after ${REBOOT_AFTER_MIN}m of continuous failure"
  exit 0
fi

FIRST_FAIL=$(cat "$STAMP" 2>/dev/null || echo 0)
NOW=$(date +%s)
ELAPSED=$((NOW - FIRST_FAIL))

if [ "$ELAPSED" -ge "$REBOOT_AFTER_SEC" ]; then
  echo "offline for ${ELAPSED}s (>= ${REBOOT_AFTER_SEC}s) — rebooting"
  rm -f "$STAMP"
  /sbin/reboot
else
  REMAINING=$(( (REBOOT_AFTER_SEC - ELAPSED) / 60 ))
  echo "offline for ${ELAPSED}s — reboot in ~${REMAINING}m"
fi
