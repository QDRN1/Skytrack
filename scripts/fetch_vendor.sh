#!/usr/bin/env bash
# Fetch SkyTrack Portal vendor JS into static/vendor/.
# Idempotent — skips files that already exist.
set -euo pipefail

VENDOR_DIR="$(dirname "$(readlink -f "$0")")/../static/vendor"
mkdir -p "$VENDOR_DIR"

declare -A FILES=(
  [socket.io.min.js]="https://cdn.socket.io/4.7.5/socket.io.min.js"
  [chart.min.js]="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"
  [leaflet.js]="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
  [leaflet.css]="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
)

for name in "${!FILES[@]}"; do
  dest="$VENDOR_DIR/$name"
  if [[ -s "$dest" ]]; then
    echo "[ok] $name already present"
    continue
  fi
  url="${FILES[$name]}"
  echo "[..] downloading $name"
  if command -v curl >/dev/null; then
    curl -sSfL "$url" -o "$dest" || { echo "  failed: $url"; rm -f "$dest"; }
  elif command -v wget >/dev/null; then
    wget -q "$url" -O "$dest" || { echo "  failed: $url"; rm -f "$dest"; }
  else
    echo "  neither curl nor wget available; skipping"
  fi
done

echo "vendor fetch complete in $VENDOR_DIR"
