# SkyTrack Appliance — working notes for Claude

This file is durable context for any future Claude Code session that
touches the SkyTrack appliance. Update it when conventions change.

## Pi deployment — git must run as the `skytrack` user

The Pi uses **GitHub deployment keys** owned by the `skytrack` user.
Root does not have access to those keys, so anything that reaches
out to GitHub from root will prompt for interactive login/auth and
hang or fail.

**Never** use `sudo git ...` on the Pi. Always drop into the
deployment-key user for git operations:

```sh
sudo -u skytrack git fetch origin <branch>
sudo -u skytrack git pull origin <branch>
sudo -u skytrack git reset --hard origin/<branch>
sudo -u skytrack git status
sudo -u skytrack git log --oneline -5
```

`/opt/skytrack` is owned and managed through the `skytrack` user.

### What *does* still need `sudo` (root)

- `sudo systemctl restart|status|start|stop skytrack-*.service`
- `sudo journalctl -u skytrack-*.service ...`
- `sudo bash install.sh`
- `sudo bash scripts/install_verify.sh`
- Anything that writes to `/etc/skytrack/`, `/var/lib/skytrack/`,
  `/boot/firmware/`, or unit files under `/etc/systemd/system/`.

### Canonical deployment block template

Every Pi deployment block from here on follows this shape:

```sh
echo -e "\e[35m*********** START COPY ***********\e[0m"
cd /opt/skytrack && \
  sudo -u skytrack git fetch origin claude/skytrack-adsb-tracker-N8p6u && \
  sudo -u skytrack git reset --hard origin/claude/skytrack-adsb-tracker-N8p6u && \
  sudo systemctl restart skytrack-app.service && \
  sleep 2 && \
  sudo systemctl restart skytrack-display.service
echo -e "\e[35m*********** END COPY ***********\e[0m"
```

The magenta `START COPY` / `END COPY` delimiter wrapping is a hard
requirement for every Pi block (operator convention).

## Feature branch

All development goes to `claude/skytrack-adsb-tracker-N8p6u`.
Never push elsewhere without explicit user permission.

## Version bumping

`_version.py` is the single source of truth for `__version__`. Every
ship also touches `static/splash/device_id.js` which hard-codes the
same version string as a placeholder (installer overwrites it with
the real device ID + version at firstboot).

## Phased rewrite in progress

1. Startup / splash / onboarding chain        — shipped 2.5.2
2. Hotspot usability + truth model            — PENDING
3. Hardware truth (sensor + buzzer)           — PENDING
4. Full UI button/action audit + pink styling — PENDING
5. Header redesign (WiFi + cellular icons)    — PENDING
6. Device URL /devices/<short_id> pattern     — PENDING
7. Secondary polish                           — PENDING

Rules:
- Ship each phase as its own small, testable patch.
- Do not advance to phase N+1 until phase N is validated on the Pi.
- Each phase delivers: summary, files changed, full updated files,
  deployment block, validation block, expected results.

## Health watchdog — unified, shipped 2026-05-17

`scripts/health_watchdog.sh` is the unified watchdog that supersedes
the per-concern scripts. It checks Flask app health, Cloudflare tunnel
reachability, and internet connectivity in one pass and is triggered
every 2 minutes by `skytrack-health-watchdog.timer`.

The service is `Type=oneshot` / `static` (no `[Install]`) — only the
timer gets enabled. On the deployed Pi: `/etc/systemd/system/` holds
both the `.service` and the `.timer`, the timer is enabled, and the
unified watchdog is healthy (commit `b40401b`).

The per-concern watchdog scripts and unit files (`app_watchdog.sh`,
`connectivity_watchdog.sh`, and their `.service`+`.timer` pairs) are
deprecated by `health_watchdog.sh` but still in the repo. Open
question: delete them now or leave them in case a future install
wants a more granular check pattern. Default: delete on the next
cleanup pass since the deployed device is on the unified watchdog.

## Operational backlog (captured 2026-05-17)

Items below were captured after a prior planning session crashed
with an API 400 error and could not be recovered. Keep this list
in sync with active work — when an item ships, move it under the
phased rewrite above or delete it.

### Urgent — device in production at a remote site

- **SSH via PowerShell over Cloudflare Tunnel.** Add an
  `ssh.<host>.qdrn.io` ingress to `/etc/cloudflared/config.yml`
  on the Pi (the unified watchdog already greps the non-ssh
  hostname out of this file — design must keep that grep working),
  ensure `sshd` is up, and document the PowerShell `~/.ssh/config`
  entry that uses `cloudflared access ssh --hostname …` as
  `ProxyCommand`. Goal: copy-paste-friendly remote shell from
  Windows without the Super Admin web UI.
- ~~Finish health watchdog~~ — shipped 2026-05-17 (commit `b40401b`).

### Small patches (each is roughly one file)

- Split uptime in System Health: Pi uptime (kernel boot) **and**
  app uptime (`skytrack-app.service` started). Currently shown as
  one number.
- Watchdog trigger counter: log every watchdog action (app restart,
  `cloudflared` restart, reboot) to `/var/lib/skytrack/watchdog.log`
  with timestamp + reason, and surface a count + last-trigger time
  in System Health.
- Move noisy logs from the user dashboard view into the Super Admin
  panel. User view should be high-signal only.
- Location wording: rename "Override with manual address" → "Manual
  Address." When a GPS fix is active, warn before letting the user
  save a manual address that would clobber the GPS reading.
- FlightRadar24: install the FR24 feeder client by default in
  `install.sh`; setup wizard collects the sharing key only. Removes
  the need to use Super Admin to install it post-hoc.

### UX cleanups (medium)

- Super Admin shell: needs to be mobile-friendly. Per-line copy
  buttons, no horizontal scroll, larger tap targets, monospace.
  Currently painful to copy/paste from a phone.
- Onboarding/setup wording pass — clarify ambiguous terms.

### Architectural — design before code

- **Variant model** (counter device, stoplight device, future SKUs):
  single codebase, per-device feature flags in `skytrack.env` or a
  `device_profile` field. Counter variant is imminent (friend's
  install). Decide flag schema before adding the first variant
  feature so we don't accumulate ad-hoc conditionals.
- **Swappable hardware abstraction**: a `hardware.yml` (or similar)
  config that maps logical roles (display, fan, speaker, sensor) to
  drivers/commands, so swapping a part is a config edit, not a code
  change. Critical for keeping updates safe across SKUs and stock
  substitutions.
- **Fleet monitoring behind an "ops" gate**: each Pi pushes a
  heartbeat + health summary to a central endpoint. Need to choose
  Cloudflare Worker + D1 vs. a small VPS + Postgres. Auth via a
  per-device key. Page is ops-only, never user-visible.
- **PWA vs native mobile app**: recommend PWAifying the existing
  web UI first ("Add to Home Screen" covers most native-feeling
  needs, including hotspot setup). Native only if OS-level Wi-Fi
  APIs become necessary.

### Investigations — need data from deployed Pi

- Wi-Fi + cellular instability at the remote install. Once
  SSH-via-CF is up, pull `journalctl -u NetworkManager`,
  `journalctl -u ModemManager`, signal/RSSI trends, and
  `/var/log/syslog` and diagnose.

### Installer plan from prior session (paste, 2026-05-17)

The prior session also produced a detailed installer-rewrite plan
(Plymouth + xinit kiosk lockdown, port canonicalization to 8080,
safe-mode escape hatch via `/boot/firmware/skytrack-safe-mode`,
`skytrack-display.service` rewrite, `scripts/configure_appliance.sh`,
branded offline splash, `.venv` rename, etc.). The user pasted the
full plan into chat after recovery. **That plan is the spec for
finishing phase 1 hardening and parts of phases 4–5.** If you need
the full text, ask the user to re-paste; do not invent details.

Key callouts from that plan that contradict or override defaults
elsewhere in the repo:

- Canonical app port is **8080** everywhere (`config.py` default
  is to be changed from 80 → 8080; env override still supported
  but no longer load-bearing).
- Virtualenv path is `.venv` (not `venv`). Service ExecStarts
  must point at `.venv/bin/...`.
- SSH is **left untouched** by the installer.
- No Wayland, no display manager, no rollback path (recovery =
  reflash).
