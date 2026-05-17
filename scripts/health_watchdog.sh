#!/usr/bin/env bash
# SkyTrack unified health watchdog.
#
# Runs every 2 minutes. Checks three layers and takes escalating action:
#
#   1. Flask app (localhost:8080/healthz)
#      → 3 consecutive failures (~6 min) → restart skytrack-app.service
#
#   2. Cloudflare tunnel (systemctl is-active cloudflared)
#      → 3 consecutive failures (~6 min) → restart cloudflared
#      Also: if process is alive but tunnel is broken (app works
#      locally but not through the tunnel), restart cloudflared.
#
#   3. Internet connectivity (TCP to 1.1.1.1 or 8.8.8.8)
#      → 30 minutes continuous failure → reboot the Pi
#
# All state is in /tmp so a reboot clears everything.

set -euo pipefail

STAMP_DIR="/tmp/skytrack-watchdog"
mkdir -p "$STAMP_DIR"

MAX_FAILURES=3
REBOOT_AFTER_SEC=1800

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ---------------------------------------------------------------
# Check 1: Flask app
# ---------------------------------------------------------------
APP_STAMP="$STAMP_DIR/app"

if curl -sf --max-time 10 -o /dev/null http://127.0.0.1:8080/healthz 2>/dev/null; then
  [ -f "$APP_STAMP" ] && rm -f "$APP_STAMP"
  APP_OK=true
else
  APP_OK=false
  F=$(cat "$APP_STAMP" 2>/dev/null || echo 0)
  F=$((F + 1))
  echo "$F" > "$APP_STAMP"
  log "app healthz failed ($F/$MAX_FAILURES)"
  if [ "$F" -ge "$MAX_FAILURES" ]; then
    log "restarting skytrack-app.service"
    rm -f "$APP_STAMP"
    systemctl restart skytrack-app.service
  fi
fi

# ---------------------------------------------------------------
# Check 2: Cloudflared tunnel
# ---------------------------------------------------------------
CF_STAMP="$STAMP_DIR/cf"
CF_TUNNEL_STAMP="$STAMP_DIR/cf-tunnel"

if systemctl is-active --quiet cloudflared 2>/dev/null; then
  [ -f "$CF_STAMP" ] && rm -f "$CF_STAMP"

  # Process alive — but is the tunnel actually working?
  # If the app is healthy locally, test the full path through CF.
  if [ "$APP_OK" = true ]; then
    CF_HOST=$(grep 'hostname:' /etc/cloudflared/config.yml 2>/dev/null \
      | grep -v ssh | head -1 | awk '{print $NF}' || true)
    if [ -n "$CF_HOST" ]; then
      if curl -sf --max-time 15 -o /dev/null "https://$CF_HOST/healthz" 2>/dev/null; then
        [ -f "$CF_TUNNEL_STAMP" ] && rm -f "$CF_TUNNEL_STAMP"
      else
        F=$(cat "$CF_TUNNEL_STAMP" 2>/dev/null || echo 0)
        F=$((F + 1))
        echo "$F" > "$CF_TUNNEL_STAMP"
        log "tunnel unreachable via $CF_HOST ($F/$MAX_FAILURES)"
        if [ "$F" -ge "$MAX_FAILURES" ]; then
          log "app works locally but tunnel is stuck — restarting cloudflared"
          rm -f "$CF_TUNNEL_STAMP"
          systemctl restart cloudflared
        fi
      fi
    fi
  fi
else
  [ -f "$CF_TUNNEL_STAMP" ] && rm -f "$CF_TUNNEL_STAMP"
  F=$(cat "$CF_STAMP" 2>/dev/null || echo 0)
  F=$((F + 1))
  echo "$F" > "$CF_STAMP"
  log "cloudflared not active ($F/$MAX_FAILURES)"
  if [ "$F" -ge "$MAX_FAILURES" ]; then
    log "restarting cloudflared"
    rm -f "$CF_STAMP"
    systemctl restart cloudflared
  fi
fi

# ---------------------------------------------------------------
# Check 3: Internet connectivity
# ---------------------------------------------------------------
CONN_STAMP="$STAMP_DIR/conn"

check_net() {
  timeout 5 bash -c 'echo >/dev/tcp/1.1.1.1/53' 2>/dev/null && return 0
  timeout 5 bash -c 'echo >/dev/tcp/8.8.8.8/53' 2>/dev/null && return 0
  return 1
}

if check_net; then
  if [ -f "$CONN_STAMP" ]; then
    rm -f "$CONN_STAMP"
    log "connectivity restored"
  fi
else
  log "connectivity check failed"
  if [ ! -f "$CONN_STAMP" ]; then
    date +%s > "$CONN_STAMP"
    log "first failure — reboot after 30m"
  else
    FIRST=$(cat "$CONN_STAMP" 2>/dev/null || echo 0)
    ELAPSED=$(($(date +%s) - FIRST))
    if [ "$ELAPSED" -ge "$REBOOT_AFTER_SEC" ]; then
      log "offline ${ELAPSED}s — rebooting"
      rm -f "$CONN_STAMP"
      /sbin/reboot
    else
      log "offline ${ELAPSED}s — reboot in ~$(( (REBOOT_AFTER_SEC - ELAPSED) / 60 ))m"
    fi
  fi
fi

exit 0
