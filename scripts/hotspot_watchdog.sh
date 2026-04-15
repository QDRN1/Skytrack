#!/usr/bin/env bash
# SkyTrack hotspot post-apply watchdog.
#
# Runs once, ~90 seconds after hotspot_apply.sh finishes. Scheduled as a
# transient systemd unit (`skytrack-hotspot-watchdog.service`) via
# `systemd-run --on-active=90s` from the bottom of hotspot_apply.sh.
#
# Job: if hostapd never actually came up after an apply, roll back to
# the last-good config snapshot so the operator isn't stranded with a
# dead AP. Anything more ambitious than that (bouncing dnsmasq, fixing
# rfkill, etc.) belongs in the long-lived health probe, not here — a
# one-shot watchdog should never try to be clever about transient
# states it can't see the full history of.
#
# Safety:
#   • Idempotent — if hostapd is fine, we do nothing and exit 0.
#   • Refuses to roll back if the rollback script itself is missing,
#     so we don't wedge the system in a half-rolled-back state.
#   • All output goes to journal because this runs under systemd.

set -euo pipefail

REPO_DIR="${SKYTRACK_REPO_DIR:-/opt/skytrack}"
ROLLBACK="$REPO_DIR/scripts/hotspot_rollback.sh"

if systemctl is-active --quiet hostapd; then
  echo "hotspot watchdog: hostapd is active — nothing to do"
  exit 0
fi

echo "hotspot watchdog: hostapd NOT active 90s after apply — rolling back"

if [[ ! -x "$ROLLBACK" ]]; then
  echo "hotspot watchdog: $ROLLBACK not found/executable — refusing to roll back"
  exit 1
fi

"$ROLLBACK" || echo "hotspot watchdog: rollback script exited non-zero"
