"""Dashboard read service — SQL queries that power the 4 cards, recent
list, top airlines/routes, trend chart, and search."""

import logging
from datetime import datetime, timezone

from db import get_conn

logger = logging.getLogger('skytrack.dashboard')


_RANGE_TO_SECONDS = {
    'live': 300,        # last 5 minutes
    '1h': 3600,
    '6h': 21600,
    '24h': 86400,
    '7d': 604800,
}


def _seconds_for(range_key: str) -> int:
    return _RANGE_TO_SECONDS.get((range_key or '24h').lower(), 86400)


# ---------------------------------------------------------------------------
# 4 dashboard cards
# ---------------------------------------------------------------------------

def card_aircraft_now():
    """Unique aircraft seen in the last 5 minutes."""
    conn = get_conn()
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT icao) AS n
        FROM sightings
        WHERE ts > datetime('now', '-5 minutes')
        """
    ).fetchone()
    return {'count': row['n'] if row else 0, 'window': 'last 5 min'}


def card_aircraft_today():
    """Unique aircraft seen since local midnight (UTC stored, displayed as UTC day)."""
    conn = get_conn()
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT icao) AS n
        FROM sightings
        WHERE ts >= datetime('now', 'start of day')
        """
    ).fetchone()
    return {'count': row['n'] if row else 0, 'window': 'today (UTC)'}


def card_busiest_hour():
    """Hour of last 24h with the highest unique aircraft count."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT strftime('%H', ts) AS hour,
               COUNT(DISTINCT icao) AS n
        FROM sightings
        WHERE ts > datetime('now', '-24 hours')
        GROUP BY hour
        ORDER BY n DESC
        LIMIT 1
        """
    ).fetchone()
    if not rows or not rows['n']:
        return {'hour': None, 'count': 0, 'label': 'Busiest Hour (Last 24 Hours)'}
    return {
        'hour': int(rows['hour']),
        'count': rows['n'],
        'label': 'Busiest Hour (Last 24 Hours)',
    }


def card_last_aircraft():
    """Most recent unique aircraft to produce a position report in the last 5 min."""
    conn = get_conn()
    row = conn.execute(
        """
        SELECT icao, callsign, altitude_ft, speed_kts, ts
        FROM sightings
        WHERE ts > datetime('now', '-5 minutes')
          AND lat IS NOT NULL AND lon IS NOT NULL
        ORDER BY ts DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Activity section
# ---------------------------------------------------------------------------

def recent_aircraft(limit: int = 25):
    """Latest distinct aircraft, most recent first."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT icao,
               MAX(callsign)    AS callsign,
               MAX(altitude_ft) AS altitude_ft,
               MAX(speed_kts)   AS speed_kts,
               MAX(ts)          AS last_seen
        FROM sightings
        WHERE ts > datetime('now', '-1 hour')
        GROUP BY icao
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def top_airlines(range_key: str, limit: int = 5):
    """Top airlines by callsign prefix within the time window."""
    seconds = _seconds_for(range_key)
    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT substr(callsign, 1, 3) AS airline,
               COUNT(DISTINCT icao) AS n
        FROM sightings
        WHERE ts > datetime('now', '-{seconds} seconds')
          AND callsign IS NOT NULL
          AND length(callsign) >= 3
        GROUP BY airline
        ORDER BY n DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def top_routes(range_key: str, limit: int = 5):
    """Top origin → destination pairs from enrichments joined with sightings."""
    seconds = _seconds_for(range_key)
    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT s.origin, s.destination, COUNT(DISTINCT s.icao) AS n
        FROM sightings s
        WHERE s.ts > datetime('now', '-{seconds} seconds')
          AND s.origin IS NOT NULL
          AND s.destination IS NOT NULL
        GROUP BY s.origin, s.destination
        ORDER BY n DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def trend(range_key: str):
    """Bucketed unique-aircraft count for the trend graph."""
    seconds = _seconds_for(range_key)
    if seconds <= 3600:
        bucket = '5 minutes'
        bucket_secs = 300
    elif seconds <= 86400:
        bucket = '1 hour'
        bucket_secs = 3600
    else:
        bucket = '6 hours'
        bucket_secs = 21600

    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT (strftime('%s', ts) / {bucket_secs}) * {bucket_secs} AS bucket,
               COUNT(DISTINCT icao) AS n
        FROM sightings
        WHERE ts > datetime('now', '-{seconds} seconds')
        GROUP BY bucket
        ORDER BY bucket ASC
        """
    ).fetchall()
    return {
        'bucket_seconds': bucket_secs,
        'points': [
            {'t': r['bucket'], 'n': r['n']} for r in rows
        ],
    }


def search(query: str, range_key: str = '24h', limit: int = 50):
    """Search by ICAO hex, callsign, airline prefix, or origin/destination."""
    if not query:
        return []
    q = f'%{query.strip().lower()}%'
    seconds = _seconds_for(range_key)
    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT icao, callsign, altitude_ft, speed_kts, lat, lon,
               origin, destination, MAX(ts) AS last_seen
        FROM sightings
        WHERE ts > datetime('now', '-{seconds} seconds')
          AND (
                lower(icao) LIKE ?
             OR lower(IFNULL(callsign, '')) LIKE ?
             OR lower(IFNULL(origin, '')) LIKE ?
             OR lower(IFNULL(destination, '')) LIKE ?
          )
        GROUP BY icao
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (q, q, q, q, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def frequent_flyers(limit: int = 10):
    """Top aircraft by lifetime sighting count from the persistent log."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT icao, callsign, airline, sighting_count,
               first_seen, last_seen,
               altitude_max, altitude_min, signal_best_db
        FROM aircraft_log
        ORDER BY sighting_count DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def aircraft_log_list(sort: str = 'count', limit: int = 100, offset: int = 0):
    """Paginated aircraft log for the dashboard table."""
    order = {
        'count': 'sighting_count DESC',
        'recent': 'last_seen DESC',
        'first': 'first_seen ASC',
        'icao': 'icao ASC',
    }.get(sort, 'sighting_count DESC')
    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT icao, callsign, airline, sighting_count,
               first_seen, last_seen,
               altitude_max, altitude_min, signal_best_db
        FROM aircraft_log
        ORDER BY {order}
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    total = conn.execute('SELECT COUNT(*) AS n FROM aircraft_log').fetchone()
    return {
        'aircraft': [dict(r) for r in rows],
        'total': total['n'] if total else 0,
    }


def aircraft_log_stats():
    """Summary stats for the aircraft log."""
    conn = get_conn()
    row = conn.execute(
        """
        SELECT COUNT(*) AS total_aircraft,
               SUM(sighting_count) AS total_sightings,
               MIN(first_seen) AS earliest,
               MAX(last_seen) AS latest
        FROM aircraft_log
        """
    ).fetchone()
    if not row:
        return {'total_aircraft': 0, 'total_sightings': 0}
    return dict(row)


def reset_aircraft_log(icao: str = None):
    """Reset sighting counts. If icao is given, reset only that aircraft."""
    conn = get_conn()
    if icao:
        conn.execute('DELETE FROM aircraft_log WHERE icao = ?', (icao.lower(),))
    else:
        conn.execute('DELETE FROM aircraft_log')
    conn.commit()


def aircraft_now_positions():
    """Latest position per aircraft seen in the last 5 minutes (for map)."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT icao,
               callsign,
               altitude_ft,
               speed_kts,
               track,
               lat,
               lon,
               MAX(ts) AS last_seen
        FROM sightings
        WHERE ts > datetime('now', '-5 minutes')
          AND lat IS NOT NULL AND lon IS NOT NULL
        GROUP BY icao
        ORDER BY last_seen DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def aircraft_trails(minutes: int = 10):
    """Recent position history per aircraft for drawing flight trails."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT icao, lat, lon, track, ts
        FROM sightings
        WHERE ts > datetime('now', ? || ' minutes')
          AND lat IS NOT NULL AND lon IS NOT NULL
        ORDER BY icao, ts ASC
        """,
        (f'-{minutes}',),
    ).fetchall()
    trails = {}
    for r in rows:
        icao = r['icao']
        if icao not in trails:
            trails[icao] = []
        trails[icao].append([r['lat'], r['lon']])
    return trails
