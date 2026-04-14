# SkyTrack Portal v2

A self-contained ADS-B flight tracker and connectivity monitor for the Raspberry Pi,
built around a local web portal you reach over a built-in Wi-Fi hotspot.

Each device boots up as `SkyTrack-Portal` Wi-Fi, hands the operator a captive
portal at `http://10.4.26.89/`, walks them through a one-time setup wizard, and
then exposes a full dashboard, settings panel, network panel, log viewer, and
super-user console — all on a single Pi, with no cloud account required.

## Highlights

- **Single-radio hotspot.** `SkyTrack-Portal` SSID on `wlan0` (channel 6),
  `10.4.26.89/24` gateway, DHCP `10.4.26.100-200`, captive-portal DNS that
  resolves every name to the gateway so phones land on the wizard.
- **Cellular-first networking.** ModemManager + NetworkManager supervise an
  optional LTE modem; the dashboard reports signal, operator, APN, and bytes.
- **Deterministic device identity.** `QDRN-SkyTrack-XXXXX` derived from the
  Pi serial via HMAC-SHA256 over an unambiguous 5-char alphabet.
- **3-tier auth.** PIN → Admin password → SuperUser. Default super-user
  `collin / collin123` until you change it.
- **SQLite-backed.** WAL mode, per-thread connections, schema migrations,
  retention pruning. No PostgreSQL, no Redis.
- **Budget-aware enrichment.** AeroAPI / OpenSky lookups go through an
  in-memory queue worker; daily budget caps prevent surprise bills.
- **Hardware-friendly.** DHT22 on GPIO 21, buzzer on GPIO 18, mock fallbacks
  via `DEV_MODE=1` for laptop development.
- **Five+ systemd services.** App, ingest, network, hardware, first-boot,
  hotspot, hotspot watchdog, and an optional kiosk display.

## Install (Raspberry Pi)

Tested on Raspberry Pi OS Bookworm 64-bit. **Do the install over Ethernet or
the console** — the installer will refuse to bring up the hotspot if it
detects you're SSH'd in over `wlan0`.

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/QDRN1/Skytrack.git /tmp/skytrack-src
cd /tmp/skytrack-src
sudo ./install.sh
```

That runs an idempotent 11-step bring-up:

1. apt installs (`hostapd`, `dnsmasq`, `dhcpcd5`, `modemmanager`, build tools…)
2. creates the `skytrack` system user
3. syncs the repo to `/opt/skytrack`
4. builds a venv and installs `requirements.txt`
5. drops `/etc/skytrack/skytrack.env`
6. creates `/var/lib/skytrack` and `/var/log/skytrack`
7. generates the device ID and migrates the database
8. fetches vendor JS into `static/vendor/`
9. installs and enables the systemd units
10. brings up the wlan0 hotspot
11. starts `skytrack-app` and runs `scripts/install_verify.sh`

After it finishes:

1. Connect a phone or laptop to the **SkyTrack-Portal** Wi-Fi (open during
   first boot — the wizard sets the WPA2 passphrase).
2. Browse to `http://10.4.26.89/`. You'll be redirected to `/setup`.
3. Set the PIN (4–8 digits), the admin password, and the hotspot password.
4. The device locks the hotspot, hands you a login screen, and you're in.

### Installer flags

```
--skip-piaware     don't install dump1090-fa / piaware
--skip-fr24        don't install fr24feed
--skip-hotspot     don't bring up the SkyTrack-Portal hotspot
--dev              local-dev install: no systemd, no hotspot, no feeders
--non-interactive  never prompt
```

## Local Development

```bash
git clone https://github.com/QDRN1/Skytrack.git
cd Skytrack
./install.sh --dev          # creates venv, fetches vendor JS, migrates db
DEV_MODE=1 venv/bin/python app.py
```

Then open `http://127.0.0.1:8080/`. With `DEV_MODE=1` the app uses mock
sensors and mock aircraft, ignores GPIO entirely, and points its data
directories at `$SKYTRACK_DATA_DIR` (defaults to `/var/lib/skytrack`).

The first boot will redirect you straight to `/setup`; complete it once
and the wizard hands off to a normal login.

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Raspberry Pi (Bookworm)                                           │
│                                                                    │
│  hostapd ─ dnsmasq ─ dhcpcd ──▶ wlan0  (SkyTrack-Portal AP)        │
│                                                                    │
│  ModemManager ─ NetworkManager ─ usb0 / ppp0  (cellular upstream)  │
│                                                                    │
│  ┌──── skytrack-app.service ───────────────────────────────────┐   │
│  │  Flask + Flask-SocketIO  ::  app.create_app()               │   │
│  │  Blueprints: auth, dashboard, settings, logs, network,      │   │
│  │              super, api                                     │   │
│  │  Daemon threads: sensor, cellular, enrichment, prune, tick  │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                    │
│  skytrack-ingest.service   ── dump1090 → SQLite                    │
│  skytrack-network.service  ── cellular/wifi supervisor             │
│  skytrack-hardware.service ── DHT22 + buzzer driver                │
│  skytrack-firstboot.svc    ── one-shot bootstrap                   │
│  skytrack-display.service  ── chromium kiosk (optional)            │
│                                                                    │
│  /var/lib/skytrack/skytrack.db  (WAL, schema v1)                   │
│  /var/log/skytrack/skytrack.log                                    │
└────────────────────────────────────────────────────────────────────┘
```

## Repository layout

```
app.py                   Flask application factory (create_app)
auth.py                  PIN + admin + super-user logic, lockouts, sessions
config.py                config.yaml load/save
db.py                    SQLite connection, schema, migrate, prune
device_id.py             QDRN-SkyTrack-XXXXX derivation from Pi serial
ingest.py                dump1090 → SQLite ingest pipeline
enrich.py                AeroAPI / OpenSky enrichment with budget caps
buzzer.py                DHT22 + buzzer hardware driver
network_svc.py           cellular + wifi + hotspot status
hotspot.py               hostapd / dnsmasq helpers
dashboard_svc.py         dashboard query helpers
logs_svc.py              journald + portal log accessors
blueprints/
  auth.py                /, /healthz, /setup, /login, /logout, /superuser
  dashboard.py           /dashboard + /api/dashboard/*
  settings.py            /settings (9 tabs) + /api/settings/*
  logs.py                /logs + /api/logs/*
  network.py             /network + /api/network/*
  super.py               /super + /api/super/*
  api.py                 /api/{device,health,sensor,weather}
templates/               Jinja2: setup wizard, login, dashboard, settings…
static/                  CSS, JS, vendor/ (fetched, not committed)
scripts/
  fetch_vendor.sh        downloads socket.io / chart.js / leaflet
  firstboot.sh           one-shot bootstrap (creates dirs, migrates db)
  hotspot_apply.sh       brings up the SkyTrack-Portal AP
  hotspot_rollback.sh    restores prior config and bounces services
  db_migrate.py          standalone DB migration runner
  install_verify.sh      post-install acceptance test
config_templates/        hostapd / dnsmasq / dhcpcd / NM / env templates
systemd/                 8 unit files (app/network/ingest/hardware/…)
legacy/                  v1 files preserved for reference
```

## Operating notes

- **Re-applying the hotspot.** `sudo /opt/skytrack/scripts/hotspot_apply.sh --reapply`
  bounces hostapd / dnsmasq without rewriting configs.
- **Rolling it back.** `sudo /opt/skytrack/scripts/hotspot_rollback.sh` restores
  the most recent timestamped backups and re-enables NetworkManager / wpa_supplicant.
  A 90-second watchdog inside `hotspot_apply.sh` triggers this automatically if
  hostapd doesn't come up.
- **SSH safety.** `hotspot_apply.sh` aborts with exit 2 if `$SSH_CONNECTION`
  arrives over `wlan0`, so you can't accidentally lock yourself out.
- **Factory reset.** Super-user → "Factory reset" wipes `auth.json`,
  `device_id.json`, and the SQLite database. The next boot re-runs first-boot.
- **Verifying an install.** `sudo /opt/skytrack/scripts/install_verify.sh`
  runs ~30 read-only checks and exits non-zero on any failure.
- **DEV_MODE.** Set `DEV_MODE=1` in `/etc/skytrack/skytrack.env` (or the shell)
  to force mock sensors and aircraft. Useful when running on a non-Pi host.

## Default credentials

| Role       | Username        | Password    |
|------------|-----------------|-------------|
| SuperUser  | `collin`        | `collin123` |
| Admin      | (set in setup)  | (set in setup) |
| PIN        | (set in setup)  | (set in setup) |

Change the super-user password from `/super` immediately after install.

## License

Internal QDRNow project. All rights reserved.
