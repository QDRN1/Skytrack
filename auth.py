"""Authentication — Admin (PIN-only) + hidden Super User.

Storage: /var/lib/skytrack/auth.json (mode 0600)
Shape:
  {
    "configured": true/false,           # first-boot wizard completed
    "pin_hash": "pbkdf2:...",           # admin PIN (required to be admin)
    "super_username": "collin",
    "super_hash": "pbkdf2:...",
    "secrets": {...},
    "hotspot_password": "random-12-char",
    "updated_at": "2026-04-14T12:00:00Z"
  }

Roles
-----
There are exactly two roles:

    ROLE_ADMIN  the everyday admin user. Authenticates with a numeric
                PIN (4–8 digits). There is NO admin password — the
                PIN is the only credential the owner of the device
                types in day-to-day.
    ROLE_SUPER  hidden override role. Reachable only via the 5-tap
                topbar shortcut, the long-press, or /superuser. Defaults
                to username "collin", password "collin123".

Auth gating
-----------
The portal is **public by default**. Only routes that change config or
expose privileged data wear `@login_required(ROLE_ADMIN)`. Page routes
that hit 401 render `auth_gate.html` (which auto-opens the login modal
and redirects on success). API routes that hit 401 return JSON, which
the front-end fetch wrapper intercepts to open the same modal.

Sessions are 2-hour idle, sliding window. The decorator touches the
session on every authorized request.
"""

import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import (jsonify, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger('skytrack.auth')

_auth_lock = threading.Lock()

DEFAULT_AUTH_PATH = Path('/var/lib/skytrack/auth.json')

# --- Roles ----------------------------------------------------------------
ROLE_ADMIN = 'admin'
ROLE_SUPER = 'super'

_HIERARCHY = {ROLE_ADMIN: 1, ROLE_SUPER: 2}

# --- Session policy -------------------------------------------------------
DEFAULT_SESSION_TIMEOUT = 7200  # 2 hours, sliding window

# --- Lockout policy -------------------------------------------------------
LOCKOUT_MAX_ATTEMPTS = 5
LOCKOUT_WINDOW_SEC = 300  # 5 minutes

# --- Default super-user seed (editable in /super) -------------------------
DEFAULT_SUPER_USERNAME = 'collin'
DEFAULT_SUPER_PASSWORD = 'collin'
_OLD_DEFAULT_SUPER_PASSWORD = 'collin123'


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _auth_path() -> Path:
    return Path(os.environ.get('SKYTRACK_AUTH_PATH', str(DEFAULT_AUTH_PATH)))


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _empty_record() -> dict:
    return {
        'configured': False,
        'pin_hash': None,
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
        rec = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning('Auth file unreadable at %s (%s); re-seeding', path, e)
        rec = _empty_record()
        write_auth(rec)
        return rec
    # Migrate: if super password is still the old default, update it.
    if (rec.get('super_username') == DEFAULT_SUPER_USERNAME
            and rec.get('super_hash')
            and check_password_hash(rec['super_hash'], _OLD_DEFAULT_SUPER_PASSWORD)):
        rec['super_hash'] = generate_password_hash(DEFAULT_SUPER_PASSWORD)
        write_auth(rec)
        logger.info('Migrated super-user password to new default')
    return rec


def write_auth(record: dict) -> None:
    """Atomic write at mode 0600."""
    with _auth_lock:
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

def complete_setup(pin: str,
                   secrets_overrides: Optional[dict] = None) -> dict:
    """Finalize the first-boot wizard.

    Required: admin PIN (4–8 digits). This is the only admin credential.

    Always auto-generates a fresh hotspot password and marks the device
    configured. Returns the full auth record so the caller can surface
    the hotspot password to the operator.
    """
    pin = (pin or '').strip()
    if not pin or not pin.isdigit() or not (4 <= len(pin) <= 8):
        raise ValueError('Admin PIN must be 4–8 digits')

    rec = read_auth()
    rec['pin_hash'] = generate_password_hash(pin)
    rec['hotspot_password'] = _random_hotspot_password()
    if secrets_overrides:
        rec.setdefault('secrets', {}).update(
            {k: v for k, v in secrets_overrides.items() if v}
        )
    rec['configured'] = True
    # Make absolutely sure no legacy admin password hash remains after an
    # upgrade from a pre-PIN-only release.
    rec.pop('admin_hash', None)
    write_auth(rec)
    logger.info('First-boot setup completed; device is now configured')
    return rec


# Easy-to-read WPA2 passphrase generator: two short aviation-flavored words
# plus a 2-digit number, e.g. "BlueSky-47". Still ≥ 8 chars (WPA2 minimum),
# but typeable on a phone without squinting. The list is small and curated —
# nothing rude, ambiguous, or hard to spell out over voice.
_HS_WORDS = (
    'Alpha',  'Bravo',  'Delta',  'Echo',   'Foxtrot',
    'Golf',   'Hotel',  'India',  'Juliet', 'Kilo',
    'Lima',   'Mike',   'November','Oscar', 'Papa',
    'Quebec', 'Romeo',  'Sierra', 'Tango',  'Uniform',
    'Victor', 'Whiskey','Xray',   'Yankee', 'Zulu',
    'Radar',  'Tower',  'Cloud',  'Sky',    'Wing',
    'Pilot',  'Runway', 'Cirrus', 'Nimbus', 'Zenith',
    'Beacon', 'Compass','Horizon','Orbit',  'Vector',
)


def _random_hotspot_password(length: int = 0) -> str:
    """Generate a memorable WPA2 passphrase: Word-Word-NN.

    `length` is accepted for signature compatibility with older callers but
    is ignored — the generated string is always 10-16 characters.
    """
    a = secrets.choice(_HS_WORDS)
    b = secrets.choice([w for w in _HS_WORDS if w != a])
    n = secrets.randbelow(90) + 10  # 10..99, always 2 digits
    return f'{a}-{b}-{n}'


# ---------------------------------------------------------------------------
# Credential mutation
# ---------------------------------------------------------------------------

def change_admin_pin(new_pin: str) -> None:
    """Replace the admin PIN. The PIN is the only admin credential — it
    is always required. Empty or non-numeric values are rejected."""
    new_pin = (new_pin or '').strip()
    if not new_pin or not new_pin.isdigit() or not (4 <= len(new_pin) <= 8):
        raise ValueError('Admin PIN must be 4–8 digits')
    rec = read_auth()
    rec['pin_hash'] = generate_password_hash(new_pin)
    rec.pop('admin_hash', None)
    write_auth(rec)


def has_pin() -> bool:
    """True once the device has a PIN set (i.e. first-boot setup is done)."""
    return bool(read_auth().get('pin_hash'))


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

def verify_admin_pin(pin: str) -> bool:
    """Constant-time verify of the admin PIN."""
    rec = read_auth()
    h = rec.get('pin_hash')
    return bool(h) and check_password_hash(h, pin or '')


def verify_admin_credential(pin: Optional[str] = None) -> bool:
    """Returns True if the PIN authenticates the admin role.

    Kept as a wrapper over verify_admin_pin so the blueprint-layer call
    sites don't have to chase renames. There is no password form of admin
    anymore — this is just the single point of truth for admin auth.
    """
    return bool(pin) and verify_admin_pin(pin)


def verify_super(username: str, password: str) -> bool:
    rec = read_auth()
    if (username or '') != rec.get('super_username', DEFAULT_SUPER_USERNAME):
        return False
    h = rec.get('super_hash')
    return bool(h) and check_password_hash(h, password or '')


# ---------------------------------------------------------------------------
# Lockout — records failed attempts in auth_attempts (db.py)
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
# Session helpers
# ---------------------------------------------------------------------------

def _session_touch():
    session['last_seen'] = int(time.time())


def _session_expired(timeout_seconds: int) -> bool:
    last = session.get('last_seen', 0)
    return (int(time.time()) - int(last)) > timeout_seconds


def current_role() -> Optional[str]:
    """Returns the active role, or None if the session is expired/empty."""
    role = session.get('role')
    if not role:
        return None
    timeout = int(session.get('session_timeout_seconds', DEFAULT_SESSION_TIMEOUT))
    if _session_expired(timeout):
        session.clear()
        return None
    return role


def is_admin() -> bool:
    """Convenience: True if the current session is admin or super."""
    role = current_role()
    return role in (ROLE_ADMIN, ROLE_SUPER)


def is_super() -> bool:
    return current_role() == ROLE_SUPER


def session_state() -> dict:
    """Compact snapshot for /api/auth/status."""
    role = current_role()
    last = int(session.get('last_seen', 0))
    timeout = int(session.get('session_timeout_seconds', DEFAULT_SESSION_TIMEOUT))
    return {
        'configured': is_configured(),
        'role': role,
        'is_admin': role in (ROLE_ADMIN, ROLE_SUPER),
        'is_super': role == ROLE_SUPER,
        'has_pin': has_pin(),
        'session_expires_in': max(0, (last + timeout) - int(time.time())) if role else 0,
        'session_timeout_seconds': timeout,
    }


def login_as(role: str, timeout_seconds: int = DEFAULT_SESSION_TIMEOUT) -> None:
    session.clear()
    session['role'] = role
    session['session_timeout_seconds'] = int(timeout_seconds)
    session['last_seen'] = int(time.time())
    session.permanent = True


def logout() -> None:
    session.clear()


# ---------------------------------------------------------------------------
# Auth decorator — only Admin and Super
# ---------------------------------------------------------------------------

def _wants_json() -> bool:
    """Best-effort: did this request come from XHR / fetch?"""
    if request.path.startswith('/api/'):
        return True
    if request.is_json:
        return True
    accept = (request.headers.get('Accept') or '')
    if 'application/json' in accept:
        return True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    return False


def _unauthorized_response(required_role: str):
    """Return a 401 in the right shape for the caller."""
    if _wants_json():
        return jsonify({
            'ok': False,
            'error': 'authentication required',
            'required_role': required_role,
            'configured': is_configured(),
        }), 401
    # Page navigation — render the auth gate (modal pre-opened).
    return render_template(
        'auth_gate.html',
        next=request.full_path if request.query_string else request.path,
        required_role=required_role,
        configured=is_configured(),
    ), 401


def login_required(role: str = ROLE_ADMIN):
    """Decorator: require at least `role` (admin or super, hierarchical)."""
    if role not in _HIERARCHY:
        raise ValueError(f'unknown role: {role}')
    min_level = _HIERARCHY[role]

    def wrap(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            user_role = current_role()
            level = _HIERARCHY.get(user_role or '', 0)
            if level < min_level:
                return _unauthorized_response(role)
            _session_touch()
            return fn(*args, **kwargs)

        return inner

    return wrap
