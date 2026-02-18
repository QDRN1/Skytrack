"""SkyTrack GPS provider — abstraction over gpsd, ModemManager, and static config.

Priority:
  1. gpsd (if the daemon is reachable)
  2. ModemManager via ``mmcli`` (cellular modem GPS)
  3. Static lat/lon from config.yaml

Returns a dict:
  lat, lon, accuracy_m, source, last_fix_utc

Never crashes — returns None-safe fallback when no fix is available.
"""

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

logger = logging.getLogger('skytrack.gps')

# Default cache path — overridden by GPSService using config['geo_data_dir']
_DEFAULT_CACHE_DIR = '/var/lib/skytrack/geo'


def _try_gpsd():
    """Attempt to get a fix from gpsd via its JSON interface."""
    try:
        import socket as _sock
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        s.settimeout(3)
        s.connect(('127.0.0.1', 2947))
        s.sendall(b'?WATCH={"enable":true,"json":true}\n')
        buf = b''
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            for line in buf.split(b'\n'):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if obj.get('class') == 'TPV' and obj.get('lat') and obj.get('lon'):
                    s.close()
                    return {
                        'lat': float(obj['lat']),
                        'lon': float(obj['lon']),
                        'accuracy_m': float(obj.get('epx', obj.get('epy', 50))),
                        'source': 'gpsd',
                        'last_fix_utc': obj.get('time', datetime.now(timezone.utc).isoformat()),
                    }
        s.close()
    except Exception as exc:
        logger.debug('gpsd unavailable: %s', exc)
    return None


def _try_modemmanager():
    """Attempt to get a fix from ModemManager via mmcli."""
    if not shutil.which('mmcli'):
        return None
    try:
        # List modems
        result = subprocess.run(
            ['mmcli', '-L'], capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None

        # Find first modem index
        modem_idx = None
        for line in result.stdout.splitlines():
            if '/Modem/' in line:
                parts = line.strip().split('/Modem/')
                if len(parts) >= 2:
                    modem_idx = parts[1].split()[0].strip()
                    break
        if modem_idx is None:
            return None

        # Enable GPS on modem (idempotent)
        subprocess.run(
            ['mmcli', '-m', modem_idx, '--location-enable-gps-raw',
             '--location-enable-gps-nmea'],
            capture_output=True, timeout=5,
        )

        # Get location
        result = subprocess.run(
            ['mmcli', '-m', modem_idx, '--location-get'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None

        lat = lon = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if 'latitude' in line.lower():
                try:
                    lat = float(line.split(':')[-1].strip())
                except ValueError:
                    pass
            if 'longitude' in line.lower():
                try:
                    lon = float(line.split(':')[-1].strip())
                except ValueError:
                    pass

        if lat is not None and lon is not None and (lat != 0 or lon != 0):
            return {
                'lat': lat,
                'lon': lon,
                'accuracy_m': 100.0,
                'source': 'modemmanager',
                'last_fix_utc': datetime.now(timezone.utc).isoformat(),
            }
    except Exception as exc:
        logger.debug('ModemManager GPS unavailable: %s', exc)
    return None


def _load_cache(cache_path):
    """Load last known GPS position from cache file."""
    try:
        if os.path.exists(cache_path):
            with open(cache_path, 'r') as f:
                data = json.load(f)
            if data.get('lat') and data.get('lon'):
                data['source'] = data.get('source', 'cached') + ' (cached)'
                return data
    except Exception as exc:
        logger.debug('GPS cache read error: %s', exc)
    return None


def _save_cache(fix, cache_path):
    """Persist GPS fix to disk."""
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(fix, f)
    except Exception as exc:
        logger.debug('GPS cache write error: %s', exc)


class GPSService:
    """Provides GPS location with provider fallback chain."""

    def __init__(self, config):
        self._config = config
        self._last_fix = None
        cache_dir = config.get('geo_data_dir', _DEFAULT_CACHE_DIR)
        self._cache_path = os.path.join(cache_dir, 'last_gps.json')

    def get_fix(self):
        """Return the best available GPS fix dict, or None."""
        fix = _try_gpsd()
        if fix:
            self._last_fix = fix
            _save_cache(fix, self._cache_path)
            return fix

        fix = _try_modemmanager()
        if fix:
            self._last_fix = fix
            _save_cache(fix, self._cache_path)
            return fix

        # Fallback: static config
        lat = self._config.get('latitude')
        lon = self._config.get('longitude')
        if lat and lon:
            fix = {
                'lat': float(lat),
                'lon': float(lon),
                'accuracy_m': None,
                'source': 'config',
                'last_fix_utc': datetime.now(timezone.utc).isoformat(),
            }
            self._last_fix = fix
            return fix

        # Absolute fallback: cached value
        cached = _load_cache(self._cache_path)
        if cached:
            self._last_fix = cached
            return cached

        return None

    def selfcheck(self):
        """Quick diagnostic."""
        fix = self.get_fix()
        if fix:
            return {'ok': True, 'message': f'GPS via {fix["source"]}: {fix["lat"]:.4f}, {fix["lon"]:.4f}'}
        return {'ok': False, 'message': 'No GPS fix available'}
