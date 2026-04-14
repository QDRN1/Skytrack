#!/usr/bin/env bash
# Post-install acceptance test for SkyTrack Portal v2.
#
# Run on the Pi after install.sh completes. Returns 0 if every check
# passes, otherwise prints the failures and exits non-zero.
#
# Checks:
#   • repo + venv + key Python deps importable
#   • /var/lib/skytrack and /var/log/skytrack exist with right perms
#   • database file present, schema v1 applied
#   • device_id is QDRN-SkyTrack-XXXXX
#   • config.yaml renderable
#   • systemd units enabled (or at least loaded) for app/network/ingest/hardware/firstboot
#   • app responds to /healthz on 127.0.0.1:8080
#   • /kiosk responds with HTML
#   • hostapd / dnsmasq / dhcpcd installed (we don't require them active here)
#   • vendor JS present in static/vendor/
#   • appliance lockdown (plymouth, cmdline, ttys, safe-mode marker, chromium)
#   • splash integrity — primary + fallback + device_id.js + videos
#   • RUNTIME: skytrack-display.service active, Xorg running, Chromium running
#   • OTA workspace + SSH known_hosts set up for skytrack service user
#   • polkit rule installed
#
# This script is read-only — it never modifies persistent state. It WILL
# start skytrack-display.service if it's not active (so that the runtime
# checks can actually observe X and Chromium), which takes over tty1.
# Pass --no-runtime to skip that (e.g. when running over SSH in a dev shell
# where grabbing tty1 is undesired).

set -uo pipefail

REPO_DIR="${SKYTRACK_REPO_DIR:-/opt/skytrack}"
DATA_DIR="${SKYTRACK_DATA_DIR:-/var/lib/skytrack}"
LOG_DIR="${SKYTRACK_LOG_DIR:-/var/log/skytrack}"
DB_PATH="${SKYTRACK_DB_PATH:-$DATA_DIR/skytrack.db}"
VENV_DIR="${SKYTRACK_VENV:-$REPO_DIR/.venv}"
SKYTRACK_USER="${SKYTRACK_USER:-skytrack}"

RUN_RUNTIME=1
for arg in "$@"; do
  case "$arg" in
    --no-runtime) RUN_RUNTIME=0 ;;
  esac
done

PASS=0
FAIL=0
FAILED_CHECKS=()

ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); FAILED_CHECKS+=("$*"); }
hdr()  { echo; echo "== $* =="; }

# ---------------------------------------------------------------------------
hdr "filesystem layout"
[[ -d "$REPO_DIR" ]]               && ok "repo dir $REPO_DIR" || fail "missing $REPO_DIR"
[[ -d "$DATA_DIR" ]]               && ok "data dir $DATA_DIR" || fail "missing $DATA_DIR"
[[ -d "$LOG_DIR" ]]                && ok "log dir $LOG_DIR"   || fail "missing $LOG_DIR"
[[ -f "$REPO_DIR/app.py" ]]        && ok "app.py present"     || fail "missing $REPO_DIR/app.py"
[[ -f "$REPO_DIR/db.py" ]]         && ok "db.py present"      || fail "missing $REPO_DIR/db.py"
[[ -f "$REPO_DIR/device_id.py" ]]  && ok "device_id.py present" || fail "missing device_id.py"
[[ -f "$REPO_DIR/auth.py" ]]       && ok "auth.py present"    || fail "missing auth.py"

# ---------------------------------------------------------------------------
hdr "python venv"
if [[ -x "$VENV_DIR/bin/python3" ]]; then
  ok "venv python at $VENV_DIR/bin/python3"
  PY="$VENV_DIR/bin/python3"
else
  fail "venv python missing at $VENV_DIR/bin/python3"
  PY="python3"
fi

for mod in flask flask_socketio werkzeug yaml; do
  if "$PY" -c "import $mod" 2>/dev/null; then
    ok "import $mod"
  else
    fail "cannot import $mod"
  fi
done

# ---------------------------------------------------------------------------
hdr "database"
if [[ -f "$DB_PATH" ]]; then
  ok "db file present: $DB_PATH"
  ver="$("$PY" -c "import os,sys; sys.path.insert(0,'$REPO_DIR'); os.environ['SKYTRACK_DB_PATH']='$DB_PATH'; import db; print(db.current_version())" 2>/dev/null || echo 0)"
  if [[ "$ver" == "1" ]]; then
    ok "schema version v1 applied"
  else
    fail "schema version is '$ver' (expected 1) — run scripts/db_migrate.py"
  fi
else
  fail "db not initialized at $DB_PATH — run scripts/firstboot.sh"
fi

# ---------------------------------------------------------------------------
hdr "device identity"
did="$("$PY" -c "import os,sys; sys.path.insert(0,'$REPO_DIR'); os.environ.setdefault('SKYTRACK_DATA_DIR','$DATA_DIR'); import device_id; print(device_id.get_or_create_device_id()['device_id'])" 2>/dev/null || echo '')"
if [[ "$did" =~ ^QDRN-SkyTrack-[A-Z0-9]{5}$ ]]; then
  ok "device_id $did"
else
  fail "device_id malformed or missing: '$did'"
fi

# ---------------------------------------------------------------------------
hdr "config.yaml"
if "$PY" -c "import sys; sys.path.insert(0,'$REPO_DIR'); import config; c=config.load_config(); assert isinstance(c,dict) and c" 2>/dev/null; then
  ok "config.load_config() returns a dict"
else
  fail "config.load_config() failed"
fi

# ---------------------------------------------------------------------------
hdr "systemd units"
# Direct file-existence check. install.sh copies units to /etc/systemd/system/,
# and `systemctl list-unit-files | grep` is unreliable on Bookworm (systemd 252)
# because the output can include ANSI escapes, column padding, or pager state
# that breaks a simple anchored regex. The file on disk is the source of truth.
for unit in skytrack-app.service skytrack-network.service skytrack-ingest.service skytrack-hardware.service skytrack-firstboot.service skytrack-display.service; do
  if [[ -f "/etc/systemd/system/$unit" ]] || systemctl cat "$unit" >/dev/null 2>&1; then
    ok "$unit installed"
  else
    fail "$unit not installed"
  fi
done

# The display service MUST use Wants=skytrack-app.service (not Requires=),
# otherwise a backend failure takes the whole display offline → black screen.
DISPLAY_UNIT_FILE=/etc/systemd/system/skytrack-display.service
if [[ -f "$DISPLAY_UNIT_FILE" ]]; then
  if grep -qE '^Requires=skytrack-app\.service' "$DISPLAY_UNIT_FILE"; then
    fail "skytrack-display.service uses Requires=skytrack-app.service (should be Wants=)"
  else
    ok "skytrack-display.service does not hard-require skytrack-app"
  fi
  if grep -qE '^Wants=skytrack-app\.service' "$DISPLAY_UNIT_FILE"; then
    ok "skytrack-display.service Wants=skytrack-app.service"
  else
    fail "skytrack-display.service missing Wants=skytrack-app.service"
  fi
fi

# ---------------------------------------------------------------------------
hdr "app health"
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 3 http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
    ok "/healthz on :8080"
  else
    fail "/healthz unreachable on :8080 (is skytrack-app running?)"
  fi
else
  fail "curl not installed (cannot test /healthz)"
fi

# ---------------------------------------------------------------------------
hdr "hotspot prerequisites"
# dhcpcd is optional — Raspberry Pi OS Bookworm dropped it in favor of
# NetworkManager. When absent, hotspot_apply.sh falls back to `ip addr add`
# for the static wlan0 gateway assignment.
for bin in hostapd dnsmasq iw; do
  if command -v "$bin" >/dev/null 2>&1; then
    ok "$bin in PATH"
  else
    fail "$bin not installed"
  fi
done
if command -v dhcpcd >/dev/null 2>&1; then
  ok "dhcpcd in PATH (legacy static IP assignment)"
else
  ok "dhcpcd not installed (Bookworm — hotspot uses ip addr / NetworkManager)"
fi

# ---------------------------------------------------------------------------
hdr "appliance kiosk"
if id skytrack-kiosk >/dev/null 2>&1; then
  ok "user skytrack-kiosk exists"
else
  fail "user skytrack-kiosk missing (run install.sh)"
fi

if [[ -f /etc/systemd/system/skytrack-display.service ]] || systemctl cat skytrack-display.service >/dev/null 2>&1; then
  if systemctl is-enabled skytrack-display.service 2>/dev/null | grep -q '^enabled$'; then
    ok "skytrack-display.service enabled"
  else
    fail "skytrack-display.service not enabled"
  fi
else
  fail "skytrack-display.service not installed"
fi

if [[ -f /boot/firmware/skytrack-safe-mode ]] || [[ -f /boot/skytrack-safe-mode ]]; then
  ok "safe-mode marker present (kiosk takeover will be skipped)"
else
  ok "safe-mode marker absent (normal appliance boot)"
fi

if command -v chromium >/dev/null 2>&1; then
  ok "chromium in PATH ($(command -v chromium))"
elif command -v chromium-browser >/dev/null 2>&1; then
  ok "chromium-browser in PATH ($(command -v chromium-browser))"
else
  fail "no chromium binary found (neither 'chromium' nor 'chromium-browser')"
fi

# ---- Splash integrity — primary + fallback + device_id + videos ------------
if [[ -f "$REPO_DIR/static/splash/index.html" ]]; then
  ok "branded splash page present"
else
  fail "missing $REPO_DIR/static/splash/index.html"
fi
if [[ -f /usr/share/skytrack/splash-fallback.html ]]; then
  ok "system fallback splash present (/usr/share/skytrack/splash-fallback.html)"
else
  fail "missing /usr/share/skytrack/splash-fallback.html (run configure_appliance.sh)"
fi
if [[ -f "$REPO_DIR/static/splash/device_id.js" ]]; then
  if grep -q 'SKYTRACK_DEVICE_ID' "$REPO_DIR/static/splash/device_id.js"; then
    ok "splash device_id.js has device id"
  else
    fail "splash device_id.js exists but SKYTRACK_DEVICE_ID is missing"
  fi
  if grep -q 'SKYTRACK_VERSION' "$REPO_DIR/static/splash/device_id.js"; then
    ok "splash device_id.js has version"
  else
    fail "splash device_id.js exists but SKYTRACK_VERSION is missing (re-run install.sh step 8 or scripts/firstboot.sh)"
  fi
else
  fail "missing $REPO_DIR/static/splash/device_id.js"
fi
# All three splash videos must exist. The splash state machine has a
# per-state broken-flag fallback so a missing video won't black-screen the
# device, but we still fail the verifier — shipping without a video is a
# regression, not an emergency fallback.
for video in "SkyTrack Boot Screen-V1.mp4" "SkyTrack Setup Screen.mp4" "SkyTrack Service Unavail.mp4"; do
  if [[ -s "$REPO_DIR/static/video/$video" ]]; then
    ok "video: $video"
  else
    fail "missing $REPO_DIR/static/video/$video"
  fi
done
# And the splash index.html must reference each of them by name so a
# filename typo doesn't silently fall through to the emergency fallback.
for ref in "SkyTrack%20Boot%20Screen-V1\.mp4" \
           "SkyTrack%20Setup%20Screen\.mp4" \
           "SkyTrack%20Service%20Unavail\.mp4"; do
  if grep -q "$ref" "$REPO_DIR/static/splash/index.html" 2>/dev/null; then
    ok "splash references $(echo "$ref" | sed 's/\\\././g;s/%20/ /g')"
  else
    fail "splash missing reference to $(echo "$ref" | sed 's/\\\././g;s/%20/ /g')"
  fi
done

if [[ -f "$REPO_DIR/kiosk/xinitrc" ]]; then
  ok "kiosk xinitrc present"
else
  fail "missing $REPO_DIR/kiosk/xinitrc"
fi

for n in 2 3 4 5 6; do
  # `systemctl is-enabled` exits 1 for masked (and disabled) units, which
  # under `set -o pipefail` would propagate through `grep` and make the
  # check read false even when the unit IS masked. Capture stdout first
  # and then compare.
  state="$(systemctl is-enabled "getty@tty${n}.service" 2>/dev/null || true)"
  if [[ "$state" == "masked" ]]; then
    ok "getty@tty${n} masked"
  else
    fail "getty@tty${n} not masked (state='$state')"
  fi
done

for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
  if [[ -f "$f" ]]; then
    if grep -q 'quiet splash' "$f" && grep -q 'console=tty3' "$f"; then
      ok "$f locked down (quiet splash, console=tty3)"
    else
      fail "$f missing appliance cmdline params"
    fi
    break
  fi
done

# ---------------------------------------------------------------------------
hdr "OTA workspace"
OTA_WS="$DATA_DIR/ota-workspace"
if [[ -d "$OTA_WS" ]]; then
  ok "ota-workspace dir present"
  if [[ -d "$OTA_WS/.git" ]]; then
    ok "ota-workspace already cloned"
  else
    ok "ota-workspace empty (will clone on first check)"
  fi
else
  fail "ota-workspace missing at $OTA_WS — run install.sh step 7"
fi
if [[ -f "$DATA_DIR/.ssh/known_hosts" ]]; then
  ok "git known_hosts writable at $DATA_DIR/.ssh/known_hosts"
  kh_owner="$(stat -c '%U' "$DATA_DIR/.ssh/known_hosts" 2>/dev/null || echo '')"
  if [[ "$kh_owner" == "$SKYTRACK_USER" ]]; then
    ok "known_hosts owned by $SKYTRACK_USER"
  else
    fail "known_hosts owned by '$kh_owner' (expected $SKYTRACK_USER)"
  fi
  kh_mode="$(stat -c '%a' "$DATA_DIR/.ssh/known_hosts" 2>/dev/null || echo '')"
  if [[ "$kh_mode" == "600" ]]; then
    ok "known_hosts mode 600"
  else
    fail "known_hosts mode is '$kh_mode' (expected 600)"
  fi
else
  fail "$DATA_DIR/.ssh/known_hosts missing — OTA SSH remotes will fail"
fi
ssh_dir_owner="$(stat -c '%U' "$DATA_DIR/.ssh" 2>/dev/null || echo '')"
if [[ "$ssh_dir_owner" == "$SKYTRACK_USER" ]]; then
  ok ".ssh dir owned by $SKYTRACK_USER"
else
  fail ".ssh dir owned by '$ssh_dir_owner' (expected $SKYTRACK_USER)"
fi

# ---------------------------------------------------------------------------
hdr "operator verification scripts"
if command -v jq >/dev/null 2>&1; then
  ok "jq in PATH ($(command -v jq))"
else
  fail "jq not installed (quick_check/long_check cannot format JSON)"
fi
for s in quick_check long_check; do
  src="$REPO_DIR/scripts/${s}.sh"
  link="/usr/local/bin/skytrack-${s//_/-}"
  if [[ -x "$src" ]]; then
    ok "$src executable"
  else
    fail "$src missing or not executable"
  fi
  if [[ -L "$link" ]]; then
    ok "$link symlinked"
  else
    fail "$link symlink missing"
  fi
done

# ---------------------------------------------------------------------------
hdr "polkit (restart buttons)"
if [[ -f /etc/polkit-1/rules.d/50-skytrack.rules ]]; then
  ok "polkit rule 50-skytrack.rules installed"
else
  fail "polkit rule missing — Settings restart buttons will fail"
fi

# ---------------------------------------------------------------------------
hdr "frontend assets"
for f in static/js/particles.js static/js/dashboard.js static/js/settings.js \
         static/js/kiosk.js static/css/portal.css templates/dashboard.html \
         templates/base.html templates/kiosk.html; do
  if [[ -s "$REPO_DIR/$f" ]]; then
    ok "$f"
  else
    fail "$f missing or empty"
  fi
done

# Runtime watchdog must be wired into kiosk.js or a crashed backend never
# unsticks the display.
if grep -q 'HEALTH_FALLBACK_MS' "$REPO_DIR/static/js/kiosk.js" 2>/dev/null; then
  ok "kiosk.js has runtime healthz watchdog"
else
  fail "kiosk.js missing runtime healthz watchdog"
fi

if command -v plymouth-set-default-theme >/dev/null 2>&1; then
  if plymouth-set-default-theme 2>/dev/null | grep -q '^skytrack$'; then
    ok "plymouth theme = skytrack"
  else
    fail "plymouth theme is not skytrack"
  fi
fi

if dpkg -s raspberrypi-ui-mods >/dev/null 2>&1; then
  fail "raspberrypi-ui-mods still installed (run install.sh without --keep-desktop)"
else
  ok "raspberrypi-ui-mods not installed"
fi

if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 3 http://127.0.0.1:8080/kiosk 2>/dev/null | grep -qi '<html'; then
    ok "/kiosk returns HTML"
  else
    fail "/kiosk not reachable or not HTML (is skytrack-app running?)"
  fi
fi

# ---------------------------------------------------------------------------
hdr "vendor JS"
VENDOR="$REPO_DIR/static/vendor"
for f in socket.io.min.js chart.min.js leaflet.js leaflet.css; do
  if [[ -s "$VENDOR/$f" ]]; then
    ok "vendor/$f"
  else
    fail "vendor/$f missing or empty (run scripts/fetch_vendor.sh)"
  fi
done

# ---------------------------------------------------------------------------
# RUNTIME validation — X, Chromium, splash render path.
#
# Starts skytrack-display.service if it isn't already active, waits up to
# 20s for it to come up, and then asserts Xorg + Chromium are running. This
# is the real integration test: if any of the previous static checks is
# wrong in a way that breaks the appliance on reboot, these runtime checks
# catch it NOW at install time, not when the operator plugs in the HDMI.
# ---------------------------------------------------------------------------
if [[ "$RUN_RUNTIME" -eq 1 ]]; then
  hdr "runtime (X + Chromium + splash render)"

  if ! systemctl is-active --quiet skytrack-display.service; then
    echo "  [..] starting skytrack-display.service"
    systemctl start skytrack-display.service >/dev/null 2>&1 || true
    for _i in 1 2 3 4 5 6 7 8 9 10; do
      systemctl is-active --quiet skytrack-display.service && break
      sleep 1
    done
  fi

  if systemctl is-active --quiet skytrack-display.service; then
    ok "skytrack-display.service active"
  else
    fail "skytrack-display.service failed to start"
  fi

  # Give xinit + openbox + chromium a moment to come up after the unit
  # reaches 'active'. Chromium cold-starts in a couple of seconds.
  sleep 4

  if pgrep -x Xorg >/dev/null 2>&1 || pgrep -x X >/dev/null 2>&1; then
    ok "X server running"
  else
    fail "no X server process found (expected Xorg or X)"
  fi

  if pgrep -f 'chromium' >/dev/null 2>&1 || pgrep -f 'chromium-browser' >/dev/null 2>&1; then
    ok "Chromium running"
  else
    fail "no chromium process found"
  fi

  # /healthz must answer — this is what the splash polls to hand off.
  if command -v curl >/dev/null 2>&1; then
    hz_status=""
    for _i in 1 2 3 4 5; do
      hz_status="$(curl -o /dev/null -s -w '%{http_code}' --max-time 3 http://127.0.0.1:8080/healthz || true)"
      [[ "$hz_status" == "200" ]] && break
      sleep 1
    done
    if [[ "$hz_status" == "200" ]]; then
      ok "/healthz -> 200"
    else
      fail "/healthz did not respond 200 (got '$hz_status')"
    fi
  fi

  # Splash must reference device_id.js (the video refs are validated in
  # the appliance-kiosk section above so we don't double-report here).
  if grep -q 'device_id\.js' "$REPO_DIR/static/splash/index.html" 2>/dev/null; then
    ok "splash references device_id.js"
  else
    fail "splash missing device_id.js reference"
  fi
else
  hdr "runtime"
  ok "skipped (--no-runtime)"
fi

# ---------------------------------------------------------------------------
hdr "summary"
echo "  passed: $PASS"
echo "  failed: $FAIL"
if (( FAIL > 0 )); then
  echo
  echo "  failures:"
  for f in "${FAILED_CHECKS[@]}"; do
    echo "    - $f"
  done
  exit 1
fi
echo "  ALL CHECKS PASSED"
exit 0
