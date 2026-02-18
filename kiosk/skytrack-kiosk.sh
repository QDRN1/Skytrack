#!/usr/bin/env bash
# =============================================================================
# SkyTrack Kiosk Launcher
# Starts X11 + Openbox + Chromium in kiosk mode pointed at the SkyTrack dashboard.
# Designed for headless Raspberry Pi — no desktop environment required.
# =============================================================================

# Use -u for unset variable errors but NOT -e (we want the script to keep
# running even if individual commands fail — a kiosk must not bail out).
set -u

SKYTRACK_DIR="${SKYTRACK_DIR:-/opt/skytrack}"
SKYTRACK_URL="${SKYTRACK_URL:-http://localhost:5000}"
CONFIG_FILE="${SKYTRACK_DIR}/config.yaml"
LOG_TAG="[kiosk]"

log() { echo "${LOG_TAG} $(date '+%H:%M:%S') $*"; }

# ---------------------------------------------------------------------------
# Read display settings from config.yaml (fallback to safe defaults)
# ---------------------------------------------------------------------------
get_config() {
    local key="$1" default="$2"
    if command -v python3 >/dev/null 2>&1 && [ -f "$CONFIG_FILE" ]; then
        python3 -c "
import yaml, sys
try:
    c = yaml.safe_load(open('${CONFIG_FILE}'))
    print(c.get('${key}', '${default}'))
except:
    print('${default}')
" 2>/dev/null || echo "$default"
    else
        echo "$default"
    fi
}

ROTATION=$(get_config "display_rotation" "0")
DISPLAY_OUTPUT=$(get_config "display_output" "HDMI-1")

# ---------------------------------------------------------------------------
# Wait for network (up to 30 seconds) — not fatal if unavailable
# ---------------------------------------------------------------------------
log "Waiting for network..."
for i in $(seq 1 30); do
    if ip route get 1.1.1.1 >/dev/null 2>&1 || ip route show default >/dev/null 2>&1; then
        log "Network available."
        break
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# Wait for X11 to be ready (up to 15 seconds)
# ---------------------------------------------------------------------------
log "Waiting for X server..."
for i in $(seq 1 15); do
    if xdpyinfo >/dev/null 2>&1; then
        log "X server ready."
        break
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# Apply display rotation — supports both numeric (0/90/180/270) and named
# values (normal/left/right/inverted).
# ---------------------------------------------------------------------------
apply_rotation() {
    local rot="$1"
    local output="$2"
    local xrandr_rot="normal"

    case "$rot" in
        0|normal)       xrandr_rot="normal" ;;
        90|left)        xrandr_rot="left" ;;
        180|inverted)   xrandr_rot="inverted" ;;
        270|right)      xrandr_rot="right" ;;
        *)              log "Unknown rotation '$rot', defaulting to normal."
                        xrandr_rot="normal" ;;
    esac

    xrandr --output "$output" --rotate "$xrandr_rot" 2>/dev/null || {
        # If the specified output fails, try without --output (uses primary)
        log "xrandr failed for output '$output', trying default output..."
        xrandr --rotate "$xrandr_rot" 2>/dev/null || true
    }
    log "Rotation set to: $xrandr_rot (from config value '$rot') on $output"
}

if command -v xrandr >/dev/null 2>&1; then
    apply_rotation "$ROTATION" "$DISPLAY_OUTPUT"
fi

# ---------------------------------------------------------------------------
# Detect resolution
# ---------------------------------------------------------------------------
SCREEN_W=1920
SCREEN_H=1080

if command -v xrandr >/dev/null 2>&1; then
    RES=$(xrandr 2>/dev/null | grep '\*' | head -1 | awk '{print $1}')
    if [ -n "$RES" ]; then
        SCREEN_W=$(echo "$RES" | cut -d'x' -f1)
        SCREEN_H=$(echo "$RES" | cut -d'x' -f2)
    fi
fi
log "Screen resolution: ${SCREEN_W}x${SCREEN_H}"

# ---------------------------------------------------------------------------
# Disable screen blanking / DPMS
# ---------------------------------------------------------------------------
xset s off 2>/dev/null || true
xset -dpms 2>/dev/null || true
xset s noblank 2>/dev/null || true

# ---------------------------------------------------------------------------
# Hide cursor
# ---------------------------------------------------------------------------
if command -v unclutter >/dev/null 2>&1; then
    unclutter -idle 0.5 -root &
fi

# ---------------------------------------------------------------------------
# Wait for SkyTrack backend (up to 60 seconds)
# ---------------------------------------------------------------------------
log "Waiting for SkyTrack backend at $SKYTRACK_URL ..."
for i in $(seq 1 60); do
    if curl -sf "$SKYTRACK_URL" >/dev/null 2>&1; then
        log "Backend is up."
        break
    fi
    if [ "$i" -eq 60 ]; then
        log "Backend not responding after 60s — launching Chromium anyway."
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# Clean up Chromium crash flags (prevents "restore pages" dialog)
# ---------------------------------------------------------------------------
CHROMIUM_DIR="${HOME}/.config/chromium"
mkdir -p "$CHROMIUM_DIR/Default"
sed -i 's/"exited_cleanly":false/"exited_cleanly":true/' \
    "$CHROMIUM_DIR/Default/Preferences" 2>/dev/null || true
sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' \
    "$CHROMIUM_DIR/Default/Preferences" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Launch Chromium in kiosk mode
# ---------------------------------------------------------------------------
log "Launching Chromium kiosk: $SKYTRACK_URL"
exec chromium-browser \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-restore-session-state \
    --kiosk \
    --incognito \
    --window-size="${SCREEN_W},${SCREEN_H}" \
    --window-position=0,0 \
    --check-for-update-interval=31536000 \
    --disable-features=TranslateUI \
    --disable-component-update \
    --autoplay-policy=no-user-gesture-required \
    --disable-background-networking \
    --disable-sync \
    --no-first-run \
    --no-default-browser-check \
    "$SKYTRACK_URL"
