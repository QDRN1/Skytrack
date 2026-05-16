"""On-demand flight enrichment via AeroAPI / OpenSky.

Strict rules:
  - Never poll continuously. Only fetch when the dashboard explicitly asks.
  - Always check the local enrichments cache first.
  - Always honor the per-hour and per-day budgets configured by the user.
  - Always cache responses with a TTL.
  - Fail open: if the API is disabled, no key, or budget exceeded, return None.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import db
from auth import get_secret

logger = logging.getLogger('skytrack.enrich')


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def usage_summary(provider: str = 'aeroapi') -> dict:
    """Return how many calls were made in the last hour, day, and month."""
    conn = db.get_conn()
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN ts > datetime('now', '-1 hour')  THEN 1 ELSE 0 END) AS hour,
          SUM(CASE WHEN ts > datetime('now', '-1 day')   THEN 1 ELSE 0 END) AS day,
          SUM(CASE WHEN ts > datetime('now', '-30 days') THEN 1 ELSE 0 END) AS month,
          IFNULL(SUM(CASE WHEN ts > datetime('now', '-30 days') THEN cost ELSE 0 END), 0) AS cost_30d
        FROM api_usage
        WHERE provider = ?
        """,
        (provider,),
    ).fetchone()
    return {
        'provider': provider,
        'hour': row['hour'] or 0,
        'day': row['day'] or 0,
        'month': row['month'] or 0,
        'cost_30d': float(row['cost_30d'] or 0),
    }


def _record_call(provider: str, endpoint: str, cost: float = 0) -> None:
    conn = db.get_conn()
    conn.execute(
        'INSERT INTO api_usage (provider, endpoint, cost) VALUES (?, ?, ?)',
        (provider, endpoint, cost),
    )
    conn.commit()


def _can_call_aeroapi(config) -> bool:
    if not config.get('aeroapi_enabled'):
        return False
    if not get_secret('aeroapi_key'):
        return False
    usage = usage_summary('aeroapi')
    if usage['hour'] >= int(config.get('aeroapi_calls_per_hour', 10)):
        return False
    if usage['day'] >= int(config.get('aeroapi_calls_per_day', 200)):
        return False
    return True


def _can_call_opensky(config) -> bool:
    if not config.get('opensky_enabled'):
        return False
    return True


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_lookup(icao: str) -> Optional[dict]:
    conn = db.get_conn()
    row = conn.execute(
        """
        SELECT icao, callsign, origin, destination, airline, aircraft_type,
               source, fetched_at, expires_at
        FROM enrichments
        WHERE icao = ? AND expires_at > datetime('now')
        """,
        (icao,),
    ).fetchone()
    return dict(row) if row else None


def _cache_store(record: dict, ttl_hours: int) -> None:
    expires = _now() + timedelta(hours=ttl_hours)
    conn = db.get_conn()
    conn.execute(
        """
        INSERT OR REPLACE INTO enrichments
          (icao, callsign, origin, destination, airline, aircraft_type,
           source, fetched_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record['icao'],
            record.get('callsign'),
            record.get('origin'),
            record.get('destination'),
            record.get('airline'),
            record.get('aircraft_type'),
            record.get('source'),
            _now().isoformat(timespec='seconds'),
            expires.isoformat(timespec='seconds'),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_flight(icao: str, callsign: Optional[str], config: dict) -> Optional[dict]:
    """Return enrichment for one flight. Cache-first, budgeted, never raises."""
    if not icao:
        return None
    icao = icao.lower()

    cached = _cache_lookup(icao)
    if cached:
        return cached

    ttl_hours = int(config.get('enrichment_ttl_hours', 24))

    # AeroAPI first (richer data)
    if _can_call_aeroapi(config):
        try:
            record = _fetch_aeroapi(icao, callsign)
            if record:
                cost = float(config.get('aeroapi_cost_per_call', 0.01))
                _record_call('aeroapi', f'flights/{icao}', cost=cost)
                _cache_store(record, ttl_hours)
                return record
        except Exception as e:
            logger.debug('AeroAPI fetch failed: %s', e)

    # OpenSky fallback
    if _can_call_opensky(config):
        try:
            record = _fetch_opensky(icao, callsign)
            if record:
                _record_call('opensky', f'states/{icao}', cost=0)
                _cache_store(record, ttl_hours)
                return record
        except Exception as e:
            logger.debug('OpenSky fetch failed: %s', e)

    return None


# ---------------------------------------------------------------------------
# Provider clients
# ---------------------------------------------------------------------------

def _fetch_aeroapi(icao: str, callsign: Optional[str]) -> Optional[dict]:
    import requests
    api_key = get_secret('aeroapi_key')
    if not api_key:
        return None

    ident = (callsign or icao).strip()
    url = f'https://aeroapi.flightaware.com/aeroapi/flights/{ident}'
    resp = requests.get(
        url,
        headers={'x-apikey': api_key},
        timeout=8,
    )
    if resp.status_code != 200:
        logger.debug('AeroAPI %s -> %s', ident, resp.status_code)
        return None
    data = resp.json() or {}
    flights = data.get('flights') or []
    if not flights:
        return None
    flight = flights[0]
    return {
        'icao': icao,
        'callsign': flight.get('ident'),
        'origin': (flight.get('origin') or {}).get('code'),
        'destination': (flight.get('destination') or {}).get('code'),
        'airline': flight.get('operator'),
        'aircraft_type': flight.get('aircraft_type'),
        'source': 'aeroapi',
    }


def _fetch_opensky(icao: str, callsign: Optional[str]) -> Optional[dict]:
    import requests
    user = get_secret('opensky_username')
    pw = get_secret('opensky_password')
    auth = (user, pw) if user and pw else None
    url = f'https://opensky-network.org/api/states/all?icao24={icao}'
    resp = requests.get(url, auth=auth, timeout=8)
    if resp.status_code != 200:
        return None
    data = resp.json() or {}
    states = data.get('states') or []
    if not states:
        return None
    s = states[0]
    return {
        'icao': icao,
        'callsign': (s[1] or '').strip() if len(s) > 1 else callsign,
        'origin': None,
        'destination': None,
        'airline': None,
        'aircraft_type': None,
        'source': 'opensky',
    }


# ---------------------------------------------------------------------------
# Connection test (for Settings → Integrations → Test buttons)
# ---------------------------------------------------------------------------

def test_aeroapi() -> dict:
    api_key = get_secret('aeroapi_key')
    if not api_key:
        return {'ok': False, 'message': 'No AeroAPI key configured'}
    try:
        import requests
        resp = requests.get(
            'https://aeroapi.flightaware.com/aeroapi/airports/KSFO',
            headers={'x-apikey': api_key},
            timeout=6,
        )
        if resp.status_code == 200:
            return {'ok': True, 'message': 'AeroAPI reachable'}
        return {'ok': False, 'message': f'HTTP {resp.status_code}'}
    except Exception as e:
        return {'ok': False, 'message': str(e)}


def test_opensky() -> dict:
    try:
        import requests
        user = get_secret('opensky_user')
        pw = get_secret('opensky_pass')
        auth = (user, pw) if user and pw else None
        resp = requests.get(
            'https://opensky-network.org/api/states/all?lamin=44&lomin=-93&lamax=45&lomax=-92',
            auth=auth,
            timeout=6,
        )
        if resp.status_code == 200:
            return {'ok': True, 'message': 'OpenSky reachable'}
        return {'ok': False, 'message': f'HTTP {resp.status_code}'}
    except Exception as e:
        return {'ok': False, 'message': str(e)}
