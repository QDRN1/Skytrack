"""Authentication — PIN, Admin password, Super User credentials.

Storage: /var/lib/skytrack/auth.json (mode 0600)
Shape:
  {
    "configured": true/false,           # first-boot wizard completed
    "pin_hash": "pbkdf2:...",
    "admin_hash": "pbkdf2:...",
    "super_username": "collin",
    "super_hash": "pbkdf2:...",
    "secrets": {
      "aeroapi_key": "...",
      "opensky_user": "...",
      "opensky_pass": "...",
      "fr24_key": "...",
      "piaware_feeder_id": "...",
      "weather_api_key": "..."
    },
    "hotspot_password": "random-16-char",
    "updated_at": "2026-04-14T12:00:00Z"
  }
"""

import json
import logging
import os
import secrets
import string
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger('skytrack.auth')

DEFAULT_AUTH_PATH = Path('/var/lib/skytrack/auth.json')

ROLE_PIN = 'pin'
ROLE_ADMIN = 'admin'
ROLE_SUPER = 'super'

_HIERARCHY = {ROLE_PIN: 1, ROLE_ADMIN: 2, ROLE_SUPER: 3}

# Lockout policy
LOCKOUT_MAX_ATTEMPTS = 5
LOCKOUT_WINDOW_SEC = 300  # 5 minutes

# Default super-user seed (editable in Settings → Access)
DEFAULT_SUPER_USERNAME = 'collin'
DEFAULT_SUPER_PASSWORD = 'collin123'


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _auth_path() -> Path:
    return Path(os.environ.get('SKYTRACK_AUTH_PATH', str(DEFAULT_AUTH_PATH)))


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _empty_record() -> dict:
    return {
        'configured': False,
        'pin_hash': None,
        'admin_hash': None,
        'super_username': DEFAULT_SUPER_USERNAME,
        'super_hash': generate_password_hash(DEFAULT_SUPER_PASSWORD),
        'secrets': {},
        'hotspot_password': None,
        'updated_at': _now_iso(),
    }


def read_auth() -> dict:
    """Read auth.json, seeding defaults if missing. Never raises."""
    path = _auth_path()
    if not path.exists():
        rec = _empty_record()
        write_auth(rec)
        return rec
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning('Auth file unreadable at %s (%s); re-seeding', path, e)
        rec = _empty_record()
        write_auth(rec)
        return rec


def write_auth(record: dict) -> None:
    """Atomic write at mode 0600."""
    record['updated_at'] = _now_iso()
    path = _auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(record, indent=2))
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def is_configured() -> bool:
    rec = read_auth()
    return bool(rec.get('configured'))


# ---------------------------------------------------------------------------
# First-boot setup
# ---------------------------------------------------------------------------

def complete_setup(pin: str, admin_password: str,
                   secrets_overrides: Optional[dict] = None) -> dict:
    """Finalize first-boot wizard. Sets PIN, admin password, generates
    a hotspot password, and marks the device configured."""
    if not pin or not pin.isdigit() or not (4 <= len(pin) <= 8):
        raise ValueError('PIN must be 4–8 digits')
    if not admin_password or len(admin_password) < 6:
        raise ValueError('Admin password must be at least 6 characters')

    rec = read_auth()
    rec['pin_hash'] = generate_password_hash(pin)
    rec['admin_hash'] = generate_password_hash(admin_password)
    rec['hotspot_password'] = _random_hotspot_password()
    if secrets_overrides:
        rec.setdefault('secrets', {}).update(
            {k: v for k, v in secrets_overrides.items() if v}
        )
    rec['configured'] = True
    write_auth(rec)
    logger.info('First-boot setup completed; device is now configured')
    return rec


def _random_hotspot_password(length: int = 12) -> str:
    """Generate an easy-to-read WPA2 password (no ambiguous chars)."""
    alphabet = 'ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# Credential mutation
# ---------------------------------------------------------------------------

def change_pin(new_pin: str) -> None:
    if not new_pin.isdigit() or not (4 <= len(new_pin) <= 8):
        raise ValueError('PIN must be 4–8 digits')
    rec = read_auth()
    rec['pin_hash'] = generate_password_hash(new_pin)
    write_auth(rec)


def change_admin_password(new_password: str) -> None:
    if len(new_password) < 6:
        raise ValueError('Admin password must be at least 6 characters')
    rec = read_auth()
    rec['admin_hash'] = generate_password_hash(new_password)
    write_auth(rec)


def change_super_credentials(new_username: str, new_password: str) -> None:
    if not new_username or len(new_password) < 6:
        raise ValueError('Super credentials invalid')
    rec = read_auth()
    rec['super_username'] = new_username
    rec['super_hash'] = generate_password_hash(new_password)
    write_auth(rec)


def set_secret(key: str, value: Optional[str]) -> None:
    rec = read_auth()
    rec.setdefault('secrets', {})
    if value is None or value == '':
        rec['secrets'].pop(key, None)
    else:
        rec['secrets'][key] = value
    write_auth(rec)


def get_secret(key: str, default: Optional[str] = None) -> Optional[str]:
    rec = read_auth()
    return rec.get('secrets', {}).get(key, default)


def set_hotspot_password(password: Optional[str] = None) -> str:
    """Set or regenerate the hotspot WPA2 password. Returns the new password."""
    rec = read_auth()
    rec['hotspot_password'] = password or _random_hotspot_password()
    write_auth(rec)
    return rec['hotspot_password']


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_pin(pin: str) -> bool:
    rec = read_auth()
    h = rec.get('pin_hash')
    return bool(h) and check_password_hash(h, pin or '')


def verify_admin(password: str) -> bool:
    rec = read_auth()
    h = rec.get('admin_hash')
    return bool(h) and check_password_hash(h, password or '')


def verify_super(username: str, password: str) -> bool:
    rec = read_auth()
    if (username or '') != rec.get('super_username', DEFAULT_SUPER_USERNAME):
        return False
    h = rec.get('super_hash')
    return bool(h) and check_password_hash(h, password or '')


# ---------------------------------------------------------------------------
# Lockout — records failed attempts in auth_attempts (in db.py)
# ---------------------------------------------------------------------------

def record_attempt(ip: str, role: str, success: bool) -> None:
    try:
        from db import get_conn
        conn = get_conn()
        conn.execute(
            'INSERT INTO auth_attempts (ip, role, success) VALUES (?, ?, ?)',
            (ip or '', role, 1 if success else 0),
        )
        conn.commit()
    except Exception as e:
        logger.debug('record_attempt failed: %s', e)


def is_locked_out(ip: str, role: str) -> bool:
    """5 failed attempts within LOCKOUT_WINDOW_SEC → locked."""
    try:
        from db import get_conn
        conn = get_conn()
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM auth_attempts
            WHERE ip = ? AND role = ? AND success = 0
              AND ts > datetime('now', ?)
            """,
            (ip or '', role, f'-{LOCKOUT_WINDOW_SEC} seconds'),
        ).fetchone()
        return (row['n'] if row else 0) >= LOCKOUT_MAX_ATTEMPTS
    except Exception as e:
        logger.debug('is_locked_out check failed: %s', e)
        return False


# ---------------------------------------------------------------------------
# Session / decorator
# ---------------------------------------------------------------------------

def _session_touch():
    session['last_seen'] = int(time.time())


def _session_expired(timeout_seconds: int) -> bool:
    last = session.get('last_seen', 0)
    return (int(time.time()) - int(last)) > timeout_seconds


def login_required(role: str = ROLE_PIN):
    """Decorator: require at least `role` (hierarchical)."""
    min_level = _HIERARCHY[role]

    def wrap(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            # First-boot wizard gate: if device not configured, redirect to /setup
            if not is_configured():
                if request.endpoint not in (
                    'auth.setup_page', 'auth.setup_submit',
                    'auth.superuser_page', 'auth.superuser_login',
                    'auth.healthz', 'static',
                ):
                    return redirect(url_for('auth.setup_page'))

            user_role = session.get('role')
            level = _HIERARCHY.get(user_role, 0)

            # Session timeout
            timeout = int(session.get('session_timeout_seconds', 28800))  # 8h
            if user_role and _session_expired(timeout):
                session.clear()
                return redirect(url_for('auth.login_page'))

            if level < min_level:
                return redirect(url_for('auth.login_page'))

            _session_touch()
            return fn(*args, **kwargs)

        return inner

    return wrap


def current_role() -> Optional[str]:
    return session.get('role')


def login_as(role: str, timeout_seconds: int = 28800) -> None:
    session.clear()
    session['role'] = role
    session['session_timeout_seconds'] = int(timeout_seconds)
    session['last_seen'] = int(time.time())
    session.permanent = True


def logout() -> None:
    session.clear()
