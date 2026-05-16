"""SQLite connection helper, schema, and migrations.

Single source of truth for every DB interaction in SkyTrack v2.
WAL mode for concurrent ingest + dashboard reads. Per-thread connections.
"""

import os
import sqlite3
import threading
import logging
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger('skytrack.db')

DEFAULT_DB_PATH = '/var/lib/skytrack/skytrack.db'

_local = threading.local()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2

DDL = [
    # Versioning
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version    INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # ADS-B sightings (one row per ingest tick per visible aircraft)
    """
    CREATE TABLE IF NOT EXISTS sightings (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT    NOT NULL,
        icao         TEXT    NOT NULL,
        callsign     TEXT,
        altitude_ft  INTEGER,
        speed_kts    REAL,
        track        REAL,
        lat          REAL,
        lon          REAL,
        signal_db    REAL,
        origin       TEXT,
        destination  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sightings_ts ON sightings(ts)",
    "CREATE INDEX IF NOT EXISTS idx_sightings_icao ON sightings(icao)",
    "CREATE INDEX IF NOT EXISTS idx_sightings_ts_icao ON sightings(ts, icao)",
    # Portal audit log
    """
    CREATE TABLE IF NOT EXISTS portal_logs (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ts     TEXT NOT NULL DEFAULT (datetime('now')),
        actor  TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_portal_logs_ts ON portal_logs(ts)",
    # Network events
    """
    CREATE TABLE IF NOT EXISTS network_logs (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ts     TEXT NOT NULL DEFAULT (datetime('now')),
        event  TEXT NOT NULL,
        detail TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_network_logs_ts ON network_logs(ts)",
    # AeroAPI / OpenSky enrichment cache
    """
    CREATE TABLE IF NOT EXISTS enrichments (
        icao          TEXT PRIMARY KEY,
        callsign      TEXT,
        origin        TEXT,
        destination   TEXT,
        airline       TEXT,
        aircraft_type TEXT,
        source        TEXT,
        fetched_at    TEXT NOT NULL,
        expires_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_enrichments_expires ON enrichments(expires_at)",
    # API call counters (for budget enforcement)
    """
    CREATE TABLE IF NOT EXISTS api_usage (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        TEXT NOT NULL DEFAULT (datetime('now')),
        provider  TEXT NOT NULL,
        endpoint  TEXT,
        cost      REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_api_usage_ts_provider ON api_usage(ts, provider)",
    # Active sessions (for super-user kick + auditability)
    """
    CREATE TABLE IF NOT EXISTS sessions (
        sid        TEXT PRIMARY KEY,
        role       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen  TEXT NOT NULL DEFAULT (datetime('now')),
        ip         TEXT
    )
    """,
    # Failed login attempts (for PIN lockout)
    """
    CREATE TABLE IF NOT EXISTS auth_attempts (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ts     TEXT NOT NULL DEFAULT (datetime('now')),
        ip     TEXT,
        role   TEXT NOT NULL,
        success INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_auth_attempts_ts ON auth_attempts(ts)",
    # Persistent aircraft registry — never pruned by retention.
    # One row per unique ICAO hex code, accumulates sighting_count over time.
    """
    CREATE TABLE IF NOT EXISTS aircraft_log (
        icao           TEXT PRIMARY KEY,
        callsign       TEXT,
        airline        TEXT,
        first_seen     TEXT NOT NULL,
        last_seen      TEXT NOT NULL,
        sighting_count INTEGER NOT NULL DEFAULT 1,
        altitude_max   INTEGER,
        altitude_min   INTEGER,
        signal_best_db REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_aircraft_log_count ON aircraft_log(sighting_count DESC)",
    "CREATE INDEX IF NOT EXISTS idx_aircraft_log_last ON aircraft_log(last_seen DESC)",
]


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _db_path():
    return os.environ.get('SKYTRACK_DB_PATH', DEFAULT_DB_PATH)


def get_conn():
    """Per-thread connection cached in thread-local storage."""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        return conn

    path = _db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(
        path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
        timeout=10.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA foreign_keys=ON')
    _local.conn = conn
    return conn


@contextmanager
def transaction():
    """Context manager: commit on success, rollback on error."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close_conn():
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def migrate():
    """Apply all schema DDL. Idempotent (CREATE IF NOT EXISTS)."""
    conn = get_conn()
    for stmt in DDL:
        conn.execute(stmt)
    # Record schema version
    conn.execute(
        'INSERT OR IGNORE INTO schema_version (version) VALUES (?)',
        (SCHEMA_VERSION,),
    )
    # Normalize ISO-8601 timestamps (T separator + +00:00 suffix) to SQLite
    # native format so datetime('now', ...) comparisons work correctly.
    for tbl, col in [('sightings', 'ts'), ('aircraft_log', 'first_seen'),
                     ('aircraft_log', 'last_seen')]:
        conn.execute(f"""
            UPDATE {tbl}
            SET {col} = REPLACE(REPLACE({col}, 'T', ' '), '+00:00', '')
            WHERE {col} LIKE '%T%' OR {col} LIKE '%+00:00'
        """)
    conn.commit()
    logger.info('Schema migrated to version %d at %s', SCHEMA_VERSION, _db_path())


def current_version():
    conn = get_conn()
    row = conn.execute(
        'SELECT MAX(version) as v FROM schema_version'
    ).fetchone()
    return row['v'] if row and row['v'] is not None else 0


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def prune(sightings_days=7, logs_days=30):
    """Delete rows older than the retention windows. Safe to call repeatedly."""
    with transaction() as conn:
        conn.execute(
            "DELETE FROM sightings WHERE ts < datetime('now', ?)",
            (f'-{int(sightings_days)} days',),
        )
        conn.execute(
            "DELETE FROM portal_logs WHERE ts < datetime('now', ?)",
            (f'-{int(logs_days)} days',),
        )
        conn.execute(
            "DELETE FROM network_logs WHERE ts < datetime('now', ?)",
            (f'-{int(logs_days)} days',),
        )
        conn.execute(
            "DELETE FROM api_usage WHERE ts < datetime('now', ?)",
            (f'-{int(logs_days)} days',),
        )
        conn.execute(
            "DELETE FROM enrichments WHERE expires_at < datetime('now')"
        )
        conn.execute(
            "DELETE FROM auth_attempts WHERE ts < datetime('now', ?)",
            (f'-{int(logs_days)} days',),
        )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
