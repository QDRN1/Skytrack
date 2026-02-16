# QDRNow SkyTrack

ADS-B flight tracker and status monitor for Raspberry Pi with a local web dashboard.

## Features

- **Live aircraft map** — Leaflet.js map reading dump1090/readsb data, centered on your location with a 60-mile radius
- **Weather** — Current conditions + 2-day forecast via WeatherAPI.com
- **Stats tiles** — Most-frequent plane (today / all-time) and total unique aircraft today
- **DHT22 sensor** — Temperature & humidity with configurable alert threshold
- **System health** — Uptime, disk usage, CPU temp, network & Cloudflare tunnel status
- **Burn-in prevention** — Micro-jitter every 5 min + full-screen refresh scene every 60 min
- **WebSocket push** — All data updates live via Socket.IO, no page refresh needed
- **Mock mode** — Runs with demo data out of the box (no hardware required)

## Quick Start (any machine)

```bash
# Clone and enter the project
cd Skytrack

# Create virtualenv & install
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Copy and edit config (optional — runs in mock mode by default)
cp config.example.yaml config.yaml

# Run
python app.py
```

Open **http://localhost:5000** in a browser.

## Raspberry Pi Install

```bash
# Install system deps
sudo apt update && sudo apt install -y python3-venv python3-dev libgpiod2

# Set up project
sudo mkdir -p /opt/skytrack
sudo cp -r . /opt/skytrack/
cd /opt/skytrack

# Virtualenv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# If using DHT22 sensor, also install:
pip install adafruit-circuitpython-dht RPi.GPIO

# Configure
cp config.example.yaml config.yaml
nano config.yaml   # set mock_mode: false, add weather_api_key, etc.

# Create log directory
sudo mkdir -p /var/log/skytrack
sudo chown pi:pi /var/log/skytrack

# Install systemd service
sudo cp systemd/skytrack.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable skytrack
sudo systemctl start skytrack
```

## Self-Check

```bash
python app.py --selfcheck
```

Runs a quick diagnostic of all subsystems and exits with status 0 (pass) or 1 (fail).

## Configuration

All settings are in `config.yaml` (see `config.example.yaml` for full documentation).
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

## Architecture

```
app.py              Main Flask + SocketIO server
config.py           Config loader (YAML + env vars)
weather.py          WeatherAPI.com client with caching
aircraft.py         dump1090 aircraft data reader + daily stats
sensors.py          DHT22 temperature/humidity sensor
health.py           System health (uptime, disk, CPU, network)
alerts.py           Humidity alert system (SMTP email)
templates/          Jinja2 HTML templates
static/css/         Stylesheets
static/js/          Client-side JavaScript
systemd/            systemd service file
```

## License

Proprietary — QDRNow
