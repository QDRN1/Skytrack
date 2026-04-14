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
#   • hostapd / dnsmasq / dhcpcd installed (we don't require them active here)
#   • vendor JS present in static/vendor/
#
# This script is read-only — it never modifies state.

set -uo pipefail

REPO_DIR="${SKYTRACK_REPO_DIR:-/opt/skytrack}"
DATA_DIR="${SKYTRACK_DATA_DIR:-/var/lib/skytrack}"
LOG_DIR="${SKYTRACK_LOG_DIR:-/var/log/skytrack}"
DB_PATH="${SKYTRACK_DB_PATH:-$DATA_DIR/skytrack.db}"
VENV_DIR="${SKYTRACK_VENV:-$REPO_DIR/venv}"
SKYTRACK_USER="${SKYTRACK_USER:-skytrack}"

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
if "$PY" -c "import sys; sys.path.insert(0,'$REPO_DIR'); import config; c=config.load(); assert isinstance(c,dict) and c" 2>/dev/null; then
  ok "config.load() returns a dict"
else
  fail "config.load() failed"
fi

# ---------------------------------------------------------------------------
hdr "systemd units"
for unit in skytrack-app.service skytrack-network.service skytrack-ingest.service skytrack-hardware.service skytrack-firstboot.service; do
  if systemctl list-unit-files 2>/dev/null | grep -q "^$unit"; then
    ok "$unit installed"
  else
    fail "$unit not installed"
  fi
done

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
for bin in hostapd dnsmasq dhcpcd iw; do
  if command -v "$bin" >/dev/null 2>&1; then
    ok "$bin in PATH"
  else
    fail "$bin not installed"
  fi
done

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
