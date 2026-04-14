# SkyTrack Hardware Acceptance Test

## 1. Starting State
- Fresh Raspberry Pi OS Bookworm Lite (64-bit) SD card
- SSH enabled, HDMI display connected **before power-on**
- Internet via Wi-Fi or Ethernet, nothing pre-installed

## 2. Deploy Commands
```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/QDRN1/Skytrack.git /tmp/skytrack-src
cd /tmp/skytrack-src
sudo ./install.sh --non-interactive
sudo nano /opt/skytrack/config.yaml   # set latitude, longitude, weather_api_key

# --- Pre-reboot verification (catch config errors before reboot) ---
sudo systemctl start skytrack
sudo systemctl status skytrack --no-pager
curl -sf http://127.0.0.1:8080/api/selfcheck | python3 -m json.tool
# expect: {"ok": true, "checks": {...}}  — if not, fix config.yaml now

sudo reboot
```

## 3. Success Criteria
| Check | Expected |
|-------|----------|
| `skytrack.service` | active (running) |
| `skytrack-kiosk.service` | active (running) |
| HDMI output | Full-screen Chromium showing SkyTrack dashboard, no cursor |
| TCP :8080 | HTTP 200 with HTML |
| `journalctl -u skytrack` | No tracebacks, shows startup messages |
| `journalctl -u skytrack-kiosk` | Shows "Backend is up", "Launching Chromium" |

## 4. Five Verification Commands
```bash
# 1. Backend healthy
curl -sf http://127.0.0.1:8080/api/selfcheck | python3 -m json.tool

# 2. Kiosk healthy — service active + Chromium actually rendering
systemctl is-active skytrack-kiosk && echo "SERVICE OK"
DISPLAY=:0 xrandr | grep '*'                # confirm active resolution
pgrep -af chromium | head -3                 # confirm Chromium processes exist

# 3. GPS functional (returns config fallback without hardware)
curl -sf http://127.0.0.1:8080/api/location | python3 -m json.tool
# expect: source="config", lat/lon from config.yaml

# 4. Geodata loaded
journalctl -u skytrack --no-pager | grep -i "geodata loaded"

# 5. No failed services
systemctl --failed --no-legend | grep -v 'dump1090\|piaware\|fr24' || echo "All SkyTrack services OK"
# dump1090/piaware/fr24 may fail without RTL-SDR — that's expected
```

## 5. Three Most Likely Failures + Recovery

### 1. Black screen / kiosk won't start
Cause: HDMI not detected (cable unplugged at boot) or wrong output name.
```bash
journalctl -u skytrack-kiosk -n 30 --no-pager
sudo nano /opt/skytrack/config.yaml            # try display_output: "HDMI-2"
sudo systemctl restart skytrack-kiosk
```

### 2. Backend crashes on start
Cause: Bad config.yaml syntax or missing Python dependency.
```bash
journalctl -u skytrack -n 50 --no-pager
sudo /opt/skytrack/.venv/bin/python /opt/skytrack/app.py --selfcheck
sudo systemctl restart skytrack
```

### 3. PiAware/dump1090-fa fails (no RTL-SDR)
Cause: No USB dongle plugged in. Expected without hardware — backend runs fine.
```bash
systemctl status dump1090-fa --no-pager
sudo nano /opt/skytrack/config.yaml            # set mock_mode: true if no dongle
sudo systemctl restart skytrack
```
