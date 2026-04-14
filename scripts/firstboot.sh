#!/usr/bin/env bash
# SkyTrack Portal v2 first-boot bootstrap.
# Runs once via the skytrack-firstboot.service oneshot.
#
# Steps:
#   1. Create /var/lib/skytrack and /var/log/skytrack with sane perms
#   2. Generate or load the device ID
#   3. Run database migrations (creates skytrack.db with schema v1)
#   4. Drop a marker file so this never reruns

set -euo pipefail

REPO_DIR="${SKYTRACK_REPO_DIR:-/opt/skytrack}"
DATA_DIR="${SKYTRACK_DATA_DIR:-/var/lib/skytrack}"
LOG_DIR="${SKYTRACK_LOG_DIR:-/var/log/skytrack}"
MARKER="$DATA_DIR/.firstboot-done"

if [[ -f "$MARKER" ]]; then
  echo "firstboot: already complete (marker present)"
  exit 0
fi

echo "firstboot: creating directories"
mkdir -p "$DATA_DIR" "$LOG_DIR" "$DATA_DIR/backups"
chown -R "${SKYTRACK_USER:-skytrack}":"${SKYTRACK_USER:-skytrack}" "$DATA_DIR" "$LOG_DIR" 2>/dev/null || true

echo "firstboot: bootstrapping device ID + database"
cd "$REPO_DIR"
python3 - <<'PY'
import device_id
import db
identity = device_id.get_or_create_device_id()
print(f"device_id: {identity['device_id']} ({identity.get('hw_source')})")
db.migrate()
print(f"schema: v{db.current_version()}")
PY

echo "firstboot: writing marker"
date -u +%FT%TZ > "$MARKER"
echo "firstboot: complete"
