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
