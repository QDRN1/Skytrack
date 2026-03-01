# QDRNow SkyTrack

ADS-B flight tracker and status monitor for Raspberry Pi with a local web dashboard and kiosk mode.

## Fresh Install (Raspberry Pi)

**Tested on:** Raspberry Pi OS Bookworm (Debian 12) 64-bit. HDMI display.

```bash
# Flash Pi OS Lite (64-bit), enable SSH + Wi-Fi in Imager, then:
sudo apt update && sudo apt install -y git
git clone https://github.com/QDRN1/Skytrack.git /tmp/skytrack-src
cd /tmp/skytrack-src
sudo ./install.sh --non-interactive
sudo nano /opt/skytrack/config.yaml   # set latitude, longitude, weather_api_key
sudo reboot
```

After reboot, the kiosk launches on HDMI and the dashboard is at `http://<pi-ip>:5000`.

**Verify after reboot:**
```bash
systemctl status skytrack --no-pager          # expect: active (running)
systemctl status skytrack-kiosk --no-pager    # expect: active (running)
curl -sf http://127.0.0.1:5000 && echo OK     # expect: OK
```

### Installer Flags

```
--skip-piaware       Skip FlightAware PiAware installation
--skip-fr24          Skip Flightradar24 feeder installation
--skip-cloudflare    Skip Cloudflare Tunnel setup
--non-interactive    No prompts — use defaults and env vars
--lcd-profile NAME   Apply an LCD panel profile to /boot/firmware/config.txt
--fr24-key KEY       Pre-configure FR24 sharing key
--piaware-feeder-id ID   Pre-configure PiAware feeder ID
--cf-token TOKEN     Pre-configure Cloudflare tunnel token
```

### Post-Install Configuration

After the first boot, configure external services:

```bash
# PiAware — claim your feeder:
# Visit https://www.flightaware.com/adsb/piaware/claim
# Or: sudo piaware-config feeder-id <YOUR_FEEDER_ID>

# Flightradar24 — sign up:
# sudo fr24feed --signup

# Cloudflare Tunnel — connect:
# sudo cloudflared service install <YOUR_TUNNEL_TOKEN>
# sudo systemctl enable --now cloudflared
```

## Paths

| Path | Purpose |
|------|---------|
| `/opt/skytrack` | Application code + Python venv |
| `/opt/skytrack/config.yaml` | Runtime configuration |
| `/opt/skytrack/venv` | Python virtual environment |
| `/var/lib/skytrack/geo` | Geodata CSVs, GPS cache, location cache |
| `/var/log/skytrack` | Application logs |
| `/home/SkyTrack` | Service user home (Chromium profile, .Xauthority) |

## Display Rotation

Set `display_rotation` in `/opt/skytrack/config.yaml`:

```yaml
display_rotation: 0      # 0, 90, 180, or 270 (degrees)
display_output: "HDMI-1"  # xrandr output name
```

This applies rotation via `xrandr` after X starts. For boot-level rotation
(needed by some LCD panels or for console rotation), manually edit
`/boot/firmware/config.txt`:

```
display_hdmi_rotate=1   # 1=90°, 2=180°, 3=270°
```

## Quick Start (Development / Any Machine)

```bash
cd Skytrack
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python app.py
```

Open **http://localhost:5000**. Runs in mock mode by default (no hardware needed).

## Self-Check

```bash
python app.py --selfcheck
```

Runs diagnostics and exits with status 0 (pass) or 1 (fail).

## Configuration

All settings are in `config.yaml` (see `config.example.yaml` for full docs).
Environment variables with `SKYTRACK_` prefix override file settings:

| Env Variable | Config Key | Type |
|---|---|---|
| `SKYTRACK_PORT` | `port` | int |
| `SKYTRACK_HOST` | `host` | str |
| `SKYTRACK_MOCK` | `mock_mode` | bool |
| `SKYTRACK_WEATHER_API_KEY` | `weather_api_key` | str |
| `SKYTRACK_LAT` | `latitude` | float |
| `SKYTRACK_LON` | `longitude` | float |
| `SKYTRACK_DHT_PIN` | `dht_pin` | int |
| `SKYTRACK_DEBUG` | `debug` | bool |
| `SKYTRACK_DISPLAY_ROTATION` | `display_rotation` | str |

## Services

| Service | Description | Logs |
|---------|-------------|------|
| `skytrack` | Flask backend (port 5000) | `journalctl -u skytrack -f` |
| `skytrack-kiosk` | X11 + Chromium kiosk | `journalctl -u skytrack-kiosk -f` |
| `dump1090-fa` | ADS-B decoder (FlightAware) | `journalctl -u dump1090-fa` |
| `piaware` | FlightAware feeder | `journalctl -u piaware` |
| `fr24feed` | Flightradar24 feeder | `journalctl -u fr24feed` |

## Architecture

```
app.py              Main Flask + SocketIO server
config.py           Config loader (YAML + env vars)
weather.py          WeatherAPI.com client with caching
aircraft.py         dump1090 aircraft data reader + daily stats
sensors.py          DHT22 temperature/humidity sensor
gps.py              GPS provider (gpsd / ModemManager / static config)
geocode_offline.py  Offline reverse geocoder (CSV nearest-neighbor)
health.py           System health (uptime, disk, CPU, network)
alerts.py           Humidity alert system (SMTP email)
kiosk/              X11 kiosk launcher scripts
systemd/            Service unit files
geodata/            Offline city/ZIP CSV datasets
templates/          Jinja2 HTML templates
static/             CSS, JS, images, video assets
```

## Assumptions

- **OS:** Raspberry Pi OS Bookworm (Debian 12) 64-bit. May work on Bullseye but untested.
- **Display:** HDMI (standard or micro). Rotation via software (xrandr). LCD panels supported via `--lcd-profile`.
- **GPS:** Optional. Uses gpsd if available, falls back to ModemManager (cellular modem GPS), then static lat/lon from config.
- **Network:** Wi-Fi or Ethernet. Cellular (USB modem) supported via ModemManager/NetworkManager.
- **RTL-SDR:** Required for live ADS-B reception. Without it, aircraft data comes from mock mode.

## License

Proprietary — QDRNow
