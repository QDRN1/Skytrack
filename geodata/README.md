# SkyTrack Geodata — Offline Reverse Geocoding

This directory contains CSV datasets used for offline reverse geocoding (GPS coordinates to City, State ZIP).

## Files

| File | Rows | Description |
|------|------|-------------|
| `us_cities.csv` | ~770 | US cities: all state capitals + cities >100k population |
| `us_zip_centroids.csv` | ~4400 | ZIP code centroids spread across all 50 states + DC |

## Format

### us_cities.csv
```
city,state,lat,lon
Montgomery,AL,32.3792,-86.3077
```

### us_zip_centroids.csv
```
zip,state,lat,lon
01001,MA,42.3244,-71.0306
```

## How It Works

The `geocode_offline.py` module loads these files into memory and uses haversine distance to find the nearest city and ZIP centroid for a given lat/lon coordinate. The result is formatted as `City, ST ZIP`.

## Regenerating

Run the generator script:
```bash
python3 geodata/generate_geodata.py
```

## Installation

The `install.sh` script copies these files to `/var/lib/skytrack/geo/` on the target device.
