"""dump1090 aircraft data reader with daily/all-time tracking and mock fallback."""

import os
import json
import math
import time
import random
import logging
from datetime import datetime, date

logger = logging.getLogger('skytrack.aircraft')

# Realistic mock airlines and hex prefixes
_MOCK_AIRLINES = ['DAL', 'UAL', 'AAL', 'SWA', 'JBU', 'FDX', 'UPS', 'SKW', 'RPA', 'ASA',
                  'NKS', 'FFT', 'EDV', 'ENY', 'GJS']


class AircraftService:
    def __init__(self, config):
        self.config = config
        self.center_lat = config.get('latitude', 44.602016)
        self.center_lon = config.get('longitude', -92.494604)
        self.dump1090_json = config.get('dump1090_json_path', '/run/dump1090-fa/aircraft.json')
        self.dump1090_url = config.get('dump1090_url', 'http://localhost:8080/data/aircraft.json')

        # Counters
        self._msg_today = {}       # hex -> message count today
        self._msg_alltime = {}     # hex -> message count all-time
        self._flights = {}         # hex -> callsign
        self._unique_today = set()
        self._today = date.today().isoformat()

        # Stable mock seed so planes don't teleport every update
        self._mock_planes = self._generate_mock_fleet()

    def get_data(self):
        self._check_day_rollover()

        if self.config.get('mock_mode'):
            return self._mock_data()

        return self._fetch_data()

    # ------------------------------------------------------------------
    # Real data
    # ------------------------------------------------------------------
    def _fetch_data(self):
        raw = self._read_dump1090()
        if raw is None:
            return self._build_response([])

        aircraft_list = raw.get('aircraft', [])
        result = []

        for ac in aircraft_list:
            hex_code = ac.get('hex', '').strip()
            if not hex_code:
                continue

            flight = ac.get('flight', '').strip()
            lat = ac.get('lat')
            lon = ac.get('lon')
            msgs = ac.get('messages', 1)

            self._msg_today[hex_code] = self._msg_today.get(hex_code, 0) + msgs
            self._msg_alltime[hex_code] = self._msg_alltime.get(hex_code, 0) + msgs
            self._unique_today.add(hex_code)
            if flight:
                self._flights[hex_code] = flight

            if lat is not None and lon is not None:
                result.append({
                    'hex': hex_code,
                    'flight': flight,
                    'lat': lat,
                    'lon': lon,
                    'altitude': ac.get('alt_baro', ac.get('altitude', 0)),
                    'speed': ac.get('gs', 0),
                    'track': ac.get('track', 0),
                    'messages': self._msg_today.get(hex_code, 0),
                })

        return self._build_response(result)

    def _read_dump1090(self):
        """Try local JSON file first, then HTTP fallback."""
        # Local file (fastest)
        if os.path.exists(self.dump1090_json):
            try:
                with open(self.dump1090_json, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f'dump1090 JSON read error: {e}')

        # HTTP fallback
        try:
            import requests
            resp = requests.get(self.dump1090_url, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f'dump1090 HTTP error: {e}')
            return None

    # ------------------------------------------------------------------
    # Mock data
    # ------------------------------------------------------------------
    def _generate_mock_fleet(self):
        """Create a stable fleet of mock planes."""
        rng = random.Random(42)
        fleet = []
        for i in range(25):
            hex_code = f'a{rng.randint(10000, 99999):05d}'
            airline = rng.choice(_MOCK_AIRLINES)
            flight_num = rng.randint(100, 9999)
            angle = rng.uniform(0, 2 * math.pi)
            dist = rng.uniform(0.05, 0.85)
            lat = self.center_lat + dist * math.sin(angle)
            lon = self.center_lon + dist * math.cos(angle) / max(math.cos(math.radians(self.center_lat)), 0.01)
            fleet.append({
                'hex': hex_code,
                'flight': f'{airline}{flight_num}',
                'lat': round(lat, 6),
                'lon': round(lon, 6),
                'altitude': rng.randint(5000, 41000),
                'speed': rng.randint(180, 520),
                'track': rng.randint(0, 359),
                'base_msgs': rng.randint(200, 8000),
            })
        return fleet

    def _mock_data(self):
        # Drift planes slightly each update for realism
        t = time.time()
        visible = []
        for plane in self._mock_planes:
            # Simulate ~70% visible at any time
            if (hash(plane['hex']) + int(t / 30)) % 10 < 7:
                drift = math.sin(t / 60 + hash(plane['hex'])) * 0.003
                p = dict(plane)
                p['lat'] = round(plane['lat'] + drift, 6)
                p['lon'] = round(plane['lon'] - drift * 0.5, 6)
                p['track'] = (plane['track'] + int(drift * 100)) % 360
                msgs = plane['base_msgs'] + int(t % 1000)
                p['messages'] = msgs

                self._msg_today[p['hex']] = msgs
                self._msg_alltime[p['hex']] = msgs * 12
                self._unique_today.add(p['hex'])
                self._flights[p['hex']] = p['flight']

                del p['base_msgs']
                visible.append(p)

        return self._build_response(visible)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _build_response(self, aircraft):
        return {
            'aircraft': aircraft,
            'total_today': len(self._unique_today),
            'most_frequent_today': self._most_frequent(self._msg_today),
            'most_frequent_alltime': self._most_frequent(self._msg_alltime),
            'timestamp': datetime.now().isoformat(),
        }

    def _most_frequent(self, counts):
        if not counts:
            return None
        hex_code = max(counts, key=counts.get)
        return {
            'hex': hex_code,
            'flight': self._flights.get(hex_code, ''),
            'messages': counts[hex_code],
        }

    def _check_day_rollover(self):
        today = date.today().isoformat()
        if today != self._today:
            self._today = today
            self._msg_today.clear()
            self._unique_today.clear()
            logger.info('Day rolled over, reset daily counters')

    def selfcheck(self):
        if self.config.get('mock_mode'):
            return {'ok': True, 'message': 'Mock mode active'}
        if os.path.exists(self.dump1090_json):
            return {'ok': True, 'message': f'dump1090 JSON found at {self.dump1090_json}'}
        try:
            import requests
            resp = requests.get(self.dump1090_url, timeout=5)
            return {'ok': resp.status_code == 200, 'message': f'dump1090 HTTP status {resp.status_code}'}
        except Exception as e:
            return {'ok': False, 'message': f'dump1090 unreachable: {e}'}
