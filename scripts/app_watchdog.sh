#!/usr/bin/env bash
# SkyTrack app liveness watchdog.
#
# Called every 2 minutes by skytrack-app-watchdog.timer.
# Hits localhost:8080/healthz with a tight timeout. If the app fails
# to respond for MAX_FAILURES consecutive checks (~6 minutes), restarts
# skytrack-app.service. This catches hangs that systemd's Restart=always
# can't see (process alive but Flask blocked on subprocess calls).
#
# The CF tunnel is independent — it stays up even when the app is dead,
# so the moment the app comes back, remote access is restored.

set -euo pipefail

HEALTH_URL="http://127.0.0.1:8080/healthz"
STAMP="/tmp/skytrack-app-watchdog-failures"
MAX_FAILURES=3
TIMEOUT=10

if curl -sf --max-time "$TIMEOUT" -o /dev/null "$HEALTH_URL" 2>/dev/null; then
  [ -f "$STAMP" ] && rm -f "$STAMP"
  exit 0
fi

# Health check failed — increment counter
FAILURES=0
[ -f "$STAMP" ] && FAILURES=$(cat "$STAMP" 2>/dev/null || echo 0)
FAILURES=$((FAILURES + 1))
echo "$FAILURES" > "$STAMP"

echo "app healthz failed ($FAILURES/$MAX_FAILURES)"

if [ "$FAILURES" -ge "$MAX_FAILURES" ]; then
  echo "app unresponsive for $FAILURES consecutive checks — restarting skytrack-app.service"
  rm -f "$STAMP"
  systemctl restart skytrack-app.service
fi
