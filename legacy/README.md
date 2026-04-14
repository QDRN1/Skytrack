# Legacy v1 Files

These files were the original SkyTrack v1 implementation. They are preserved
here for reference and rollback only — they are **not loaded** by v2.

| File | Replaced by |
|---|---|
| `app.py` | `app.py` (v2 factory) + `blueprints/*` |
| `aircraft.py` | `ingest.py` + `dashboard_svc.py` (SQLite-backed) |
| `config.py` | `config.py` (v2, additional keys) |
| `alerts.py` | `hardware_svc.py` (buzzer-based local alerts) + `logs_svc.py` |
| `templates/dashboard.html` | `templates/dashboard.html` (v2 layout) |
| `static/css/style.css` | `static/css/portal.css` |
| `static/js/dashboard.js` | `static/js/pages/dashboard.js` + core modules |

## What's still reused at the repo root (not legacy)

- `gps.py` — provider chain (gpsd → mmcli → static → cache)
- `geocode_offline.py` — haversine nearest-neighbor lookup
- `sensors.py` — DHT22 reader with mock fallback
- `health.py` — system health probe (extended in place)
- `weather.py` — WeatherAPI.com client with cache and mock
- `geodata/` — offline city + ZIP centroid CSVs
- `kiosk/` — Chromium kiosk launcher and xinitrc
- `profiles/` — LCD HDMI profiles
- `static/video/` — boot and refresh scene MP4s

## Removing legacy

These files have no runtime impact. To remove them entirely:

    git rm -r legacy/
    git commit -m "remove legacy v1 archive"
