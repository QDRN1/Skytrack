"""ADS-B sightings ingest worker.

Reads dump1090-fa aircraft.json (or HTTP fallback), normalizes each visible
aircraft into a row, and inserts batched into the `sightings` table. Mock
fleet is used when the dump1090 source is unreachable AND mock_mode is true.
"""

import json
import logging
import math
import os
import random
import threading
import time
from datetime import datetime, timezone

import db

logger = logging.getLogger('skytrack.ingest')


_MOCK_AIRLINES = ['DAL', 'UAL', 'AAL', 'SWA', 'JBU', 'FDX', 'UPS', 'SKW',
                  'RPA', 'ASA', 'NKS', 'FFT', 'EDV', 'ENY', 'GJS']


class SightingsIngest:
    def __init__(self, config):
        self.config = config
        self.dump1090_json = config.get(
            'dump1090_json_path', '/run/dump1090-fa/aircraft.json')
        # HTTP fallback is OPTIONAL. Leave empty by default — we must NOT
        # point it at SkyTrack itself (port 8080) or we'll self-loop and
        # log a 404 every ingest_interval seconds.
        self.dump1090_url = (config.get('dump1090_url') or '').strip()
        self.interval = max(2, int(config.get('ingest_interval', 5)))
        self.center_lat = config.get('latitude', 44.602016)
        self.center_lon = config.get('longitude', -92.494604)
        self.mock = bool(config.get('mock_mode'))

        self._stop = threading.Event()
        self._thread = None
        self._mock_fleet = self._generate_mock_fleet()
        self._last_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name='ingest', daemon=True)
        self._thread.start()
        logger.info('Sightings ingest started (interval=%ds)', self.interval)

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                logger.warning('ingest tick error: %s', e)
            self._stop.wait(self.interval)

    # ------------------------------------------------------------------
    # Single ingest cycle
    # ------------------------------------------------------------------
    def tick(self):
        rows = self._collect()
        if not rows:
            return 0
        ts = db.now_iso()
        with db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO sightings
                  (ts, icao, callsign, altitude_ft, speed_kts, track,
                   lat, lon, signal_db)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (ts, r['icao'], r.get('callsign'), r.get('altitude_ft'),
                     r.get('speed_kts'), r.get('track'), r.get('lat'),
                     r.get('lon'), r.get('signal_db'))
                    for r in rows
                ],
            )
            self._upsert_aircraft_log(conn, rows, ts)
        self._last_count = len(rows)
        return len(rows)

    @staticmethod
    def _upsert_aircraft_log(conn, rows, ts):
        """Batch upsert into the persistent aircraft registry."""
        for r in rows:
            icao = r['icao']
            cs = r.get('callsign')
            airline = cs[:3] if cs and len(cs) >= 3 else None
            alt = r.get('altitude_ft')
            sig = r.get('signal_db')
            conn.execute(
                """
                INSERT INTO aircraft_log
                    (icao, callsign, airline, first_seen, last_seen,
                     sighting_count, altitude_max, altitude_min, signal_best_db)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(icao) DO UPDATE SET
                    callsign       = COALESCE(excluded.callsign, aircraft_log.callsign),
                    airline        = COALESCE(excluded.airline, aircraft_log.airline),
                    last_seen      = excluded.last_seen,
                    sighting_count = aircraft_log.sighting_count + 1,
                    altitude_max   = MAX(COALESCE(aircraft_log.altitude_max, 0),
                                         COALESCE(excluded.altitude_max, 0)),
                    altitude_min   = CASE
                        WHEN aircraft_log.altitude_min IS NULL THEN excluded.altitude_min
                        WHEN excluded.altitude_min IS NULL THEN aircraft_log.altitude_min
                        ELSE MIN(aircraft_log.altitude_min, excluded.altitude_min)
                    END,
                    signal_best_db = CASE
                        WHEN aircraft_log.signal_best_db IS NULL THEN excluded.signal_best_db
                        WHEN excluded.signal_best_db IS NULL THEN aircraft_log.signal_best_db
                        ELSE MAX(aircraft_log.signal_best_db, excluded.signal_best_db)
                    END
                """,
                (icao, cs, airline, ts, ts, alt, alt, sig),
            )

    @property
    def last_count(self):
        return self._last_count

    # ------------------------------------------------------------------
    # Source readers
    # ------------------------------------------------------------------
    def _collect(self):
        raw = self._read_dump1090()
        if raw is None:
            if self.mock:
                return self._mock_tick()
            return []
        return self._normalize_dump1090(raw)

    def _read_dump1090(self):
        if os.path.exists(self.dump1090_json):
            try:
                with open(self.dump1090_json) as f:
                    return json.load(f)
            except Exception as e:
                logger.debug('dump1090 JSON read error: %s', e)
        # Only attempt HTTP fallback if an explicit URL is configured.
        # An empty string means "local file only, no remote probe" — this
        # prevents the historical self-loop bug where a default
        # http://localhost:8080/data/aircraft.json hit SkyTrack's own Flask
        # server every tick and spammed 404s.
        if self.dump1090_url:
            try:
                import requests
                resp = requests.get(self.dump1090_url, timeout=3)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.debug('dump1090 HTTP error: %s', e)
        return None

    @staticmethod
    def _normalize_dump1090(raw):
        out = []
        for ac in raw.get('aircraft', []):
            hex_code = (ac.get('hex') or '').strip().lower()
            if not hex_code:
                continue
            out.append({
                'icao': hex_code,
                'callsign': (ac.get('flight') or '').strip() or None,
                'altitude_ft': ac.get('alt_baro') or ac.get('altitude'),
                'speed_kts': ac.get('gs'),
                'track': ac.get('track'),
                'lat': ac.get('lat'),
                'lon': ac.get('lon'),
                'signal_db': ac.get('rssi'),
            })
        return out

    # ------------------------------------------------------------------
    # Mock fleet
    # ------------------------------------------------------------------
    def _generate_mock_fleet(self):
        rng = random.Random(42)
        fleet = []
        for _ in range(25):
            airline = rng.choice(_MOCK_AIRLINES)
            flight = f'{airline}{rng.randint(100, 9999)}'
            angle = rng.uniform(0, 2 * math.pi)
            dist = rng.uniform(0.05, 0.85)
            cos_lat = max(math.cos(math.radians(self.center_lat)), 0.01)
            fleet.append({
                'icao': f'a{rng.randint(0x100000, 0xFFFFFF):06x}',
                'callsign': flight,
                'altitude_ft': rng.randint(5000, 41000),
                'speed_kts': rng.randint(180, 520),
                'track': rng.randint(0, 359),
                'base_lat': self.center_lat + dist * math.sin(angle),
                'base_lon': self.center_lon + dist * math.cos(angle) / cos_lat,
                'signal_db': round(rng.uniform(-30, -5), 1),
            })
        return fleet

    def _mock_tick(self):
        t = time.time()
        out = []
        for plane in self._mock_fleet:
            # ~70% visible at any given tick
            if (hash(plane['icao']) + int(t / 30)) % 10 >= 7:
                continue
            drift = math.sin(t / 60 + hash(plane['icao'])) * 0.003
            out.append({
                'icao': plane['icao'],
                'callsign': plane['callsign'],
                'altitude_ft': plane['altitude_ft'],
                'speed_kts': plane['speed_kts'],
                'track': (plane['track'] + int(drift * 100)) % 360,
                'lat': round(plane['base_lat'] + drift, 6),
                'lon': round(plane['base_lon'] - drift * 0.5, 6),
                'signal_db': plane['signal_db'],
            })
        return out

    def selfcheck(self):
        if self.mock:
            return {'ok': True, 'message': 'Mock mode active'}
        if os.path.exists(self.dump1090_json):
            return {'ok': True, 'message': f'dump1090 JSON found at {self.dump1090_json}'}
        if self.dump1090_url:
            try:
                import requests
                resp = requests.get(self.dump1090_url, timeout=3)
                return {
                    'ok': resp.status_code == 200,
                    'message': f'dump1090 HTTP {resp.status_code}',
                }
            except Exception as e:
                return {'ok': False, 'message': f'dump1090 unreachable: {e}'}
        return {
            'ok': False,
            'message': f'dump1090 JSON not found at {self.dump1090_json} '
                       '(install dump1090-fa or set dump1090_url to a remote feed)',
        }


if __name__ == '__main__':
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    from config import load_config
    cfg = load_config()
    db.migrate()
    worker = SightingsIngest(cfg)
    worker.start()
    logger.info('Standalone ingest running (Ctrl-C to stop)')
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        worker.stop()
        sys.exit(0)
