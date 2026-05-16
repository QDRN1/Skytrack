"""Settings blueprint — touch-friendly v2 with 11 sections.

Sections (URL-friendly slugs):
  general        timezone, units, theme, default time filter
  display        rotation, brightness, sleep, animations, fullscreen
  access         admin PIN / session timeout / lockout
  network        cellular / wifi client / hotspot / time sync
  integrations   AeroAPI, OpenSky, weather provider + budget controls
  feeders        FlightAware, FR24, feed-over-cellular toggle
  data           retention, filters, default time filter
  alerts         buzzer, temp/hum threshold, no-aircraft, API budget warn
  updates        OTA + backup + auto-backup
  diagnostics    log level, self-check, restart/reboot, view logs
  radar          publish toggle (display-only in phase 1)

The page itself (`/settings`) is admin-protected. So is every API
endpoint defined here. The dashboard remains public.

Backups + restart/reboot/shutdown shell out to local commands; on a dev
box without the right binaries they return a clear error instead of
crashing.
"""

import json
import logging
import os
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request

import auth as auth_lib
import device_id
import enrich
import git_auth
import hotspot
import logs_svc
import network_svc
from _version import __version__ as SKYTRACK_VERSION, __version_base__
from config import save_user_config

logger = logging.getLogger('skytrack.settings_bp')

settings_bp = Blueprint('settings', __name__)

ADMIN = auth_lib.login_required(auth_lib.ROLE_ADMIN)
SUPER = auth_lib.login_required(auth_lib.ROLE_SUPER)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@settings_bp.route('/settings')
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def index():
    cfg = current_app.skytrack_config
    identity = current_app.config.get('DEVICE_RECORD', {})
    aeroapi_usage = enrich.usage_summary('aeroapi')
    secrets_set = {
        # Integrations
        'aeroapi_key':        bool(auth_lib.get_secret('aeroapi_key')),
        'opensky_username':   bool(auth_lib.get_secret('opensky_username')),
        'opensky_password':   bool(auth_lib.get_secret('opensky_password')),
        'openweather_api_key':bool(auth_lib.get_secret('openweather_api_key')),
        # Feeders
        'fr24_key':           bool(auth_lib.get_secret('fr24_key')),
        'piaware_feeder_id':  bool(auth_lib.get_secret('piaware_feeder_id')),
        'flightaware_id':     bool(auth_lib.get_secret('flightaware_id')),
    }
    return render_template(
        'settings/index.html',
        identity=identity,
        radar_url=device_id.radar_url(identity) if identity else '',
        usage=aeroapi_usage,  # back-compat alias for legacy template
        usage_aeroapi=aeroapi_usage,
        usage_opensky=enrich.usage_summary('opensky'),
        hotspot_default_ssid=_default_hotspot_ssid(cfg, identity),
        timezone_groups=_timezone_groups(),
        secrets_set=secrets_set,
    )


# Common IANA zones surfaced at the top of the dropdown. Everything else lives
# under "All timezones" alphabetically. Operators rarely need more than this.
_TIMEZONE_COMMON = (
    ('US / Eastern',     'America/New_York'),
    ('US / Central',     'America/Chicago'),
    ('US / Mountain',    'America/Denver'),
    ('US / Arizona',     'America/Phoenix'),
    ('US / Pacific',     'America/Los_Angeles'),
    ('US / Alaska',      'America/Anchorage'),
    ('US / Hawaii',      'Pacific/Honolulu'),
    ('Canada / Toronto', 'America/Toronto'),
    ('Canada / Winnipeg','America/Winnipeg'),
    ('Canada / Edmonton','America/Edmonton'),
    ('Canada / Vancouver','America/Vancouver'),
    ('UK / London',      'Europe/London'),
    ('Ireland / Dublin', 'Europe/Dublin'),
    ('Central Europe',   'Europe/Berlin'),
    ('UTC',              'UTC'),
)


def _timezone_groups():
    """Return (common, all) zone lists for the Settings → General dropdown.

    `common` is a short, labeled list for the top of the menu. `all` is
    every IANA zone available on the OS, alphabetized.
    """
    try:
        from zoneinfo import available_timezones  # py 3.9+
        all_zones = sorted(available_timezones())
    except Exception:
        all_zones = [z for _, z in _TIMEZONE_COMMON]
    return {
        'common': list(_TIMEZONE_COMMON),
        'all':    all_zones,
    }


def _default_hotspot_ssid(cfg, identity) -> str:
    """Default 'QDRN Skytrack <Device ID>' if not yet set in config."""
    ssid = cfg.get('hotspot_ssid')
    if ssid and ssid != 'SkyTrack-Portal':
        return ssid
    did = (identity or {}).get('device_id') or current_app.config.get('DEVICE_ID', '')
    return f'QDRN Skytrack {did}' if did else 'SkyTrack-Portal'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _persist(updates: dict) -> None:
    if not updates:
        return
    try:
        path = os.environ.get(
            'SKYTRACK_CONFIG',
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '..', 'config.yaml'),
        )
        save_user_config(os.path.abspath(path), updates)
    except Exception as e:
        logger.warning('config persist failed: %s', e)


def _take(payload: dict, keys) -> dict:
    return {k: payload[k] for k in keys if k in payload}


def _ok(extra: dict = None) -> tuple:
    body = {'ok': True}
    if extra:
        body.update(extra)
    return jsonify(body)


def _err(msg: str, status: int = 400):
    return jsonify({'ok': False, 'error': msg}), status


def _write_display_env(cfg: dict) -> None:
    """Write <data_dir>/display.env so xinitrc can pick up rotation/output
    on the next display-service restart (or reboot). Lives in /var/lib so
    the skytrack service user can write it without root or polkit.
    """
    try:
        rotation = str(cfg.get('display_rotation') or '0')
        output   = str(cfg.get('display_output') or 'HDMI-1')
        data_dir = cfg.get('data_dir') or '/var/lib/skytrack'
        path = os.path.join(data_dir, 'display.env')
        os.makedirs(data_dir, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            f.write('# Written by skytrack settings — do not edit by hand.\n')
            f.write(f'SKYTRACK_DISPLAY_ROTATION={rotation}\n')
            f.write(f'SKYTRACK_DISPLAY_OUTPUT={output}\n')
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o644)
        except Exception:
            pass
    except Exception as e:
        logger.debug('display.env write skipped: %s', e)


def _shell(args, timeout=10, env=None, cwd=None):
    """Run a command and return (ok, message). Never raises."""
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            env=env, cwd=cwd,
        )
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out
    except FileNotFoundError:
        return False, f'{args[0]} not available on this system'
    except subprocess.TimeoutExpired:
        return False, f'{args[0]} timed out'
    except Exception as e:
        return False, str(e)


# ===========================================================================
# 1. GENERAL
# ===========================================================================

_GENERAL_KEYS = (
    'device_name_prefix', 'timezone', 'latitude', 'longitude', 'map_zoom',
    'units_temperature', 'units_speed', 'clock_format', 'default_theme',
    'default_dashboard_time_filter',
)


@settings_bp.route('/api/settings/general', methods=['GET', 'POST'])
@ADMIN
def api_general():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({
            'config': {k: cfg.get(k) for k in _GENERAL_KEYS},
            'device_id': current_app.config.get('DEVICE_ID', ''),
            'app_version': SKYTRACK_VERSION,
        })
    payload = request.get_json(silent=True) or {}
    updates = _take(payload, _GENERAL_KEYS)

    if 'timezone' in updates:
        tz = str(updates['timezone'] or '').strip() or 'auto'
        if tz != 'auto':
            try:
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                ZoneInfo(tz)
            except Exception:
                return _err('Unknown timezone. Use "auto" or an IANA name like America/Chicago.')
        updates['timezone'] = tz

    for coord_key in ('latitude', 'longitude'):
        if coord_key in updates:
            try:
                updates[coord_key] = float(updates[coord_key])
            except (TypeError, ValueError):
                return _err(f'{coord_key} must be a number')
    if 'map_zoom' in updates:
        try:
            updates['map_zoom'] = int(updates['map_zoom'])
        except (TypeError, ValueError):
            return _err('map_zoom must be an integer')

    cfg.update(updates)
    _persist(updates)
    logs_svc.log_portal('admin', 'settings_general_update', updates)
    return _ok({'config': {k: cfg.get(k) for k in _GENERAL_KEYS}})


# ===========================================================================
# 1b. LOCATION — GPS vs manual address override
# ===========================================================================

_LOCATION_KEYS = (
    'latitude', 'longitude', 'map_zoom', 'location_source', 'location_address',
)


@settings_bp.route('/api/settings/location', methods=['GET', 'POST'])
@ADMIN
def api_location():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        with current_app.gps_state_lock:
            gps_snap = dict(current_app.gps_state)
        src = (gps_snap.get('source') or '').lower()
        gps_snap['hardware_available'] = src in (
            'modemmanager', 'gpsd',
        )
        return jsonify({
            'config': {k: cfg.get(k) for k in _LOCATION_KEYS},
            'gps': gps_snap,
        })

    payload = request.get_json(silent=True) or {}
    updates = _take(payload, _LOCATION_KEYS)

    if 'location_source' in updates:
        if updates['location_source'] not in ('gps', 'manual'):
            return _err('location_source must be gps or manual')

    for coord in ('latitude', 'longitude'):
        if coord in updates:
            try:
                updates[coord] = float(updates[coord])
            except (TypeError, ValueError):
                return _err(f'Invalid {coord}')

    if 'map_zoom' in updates:
        try:
            updates['map_zoom'] = max(1, min(18, int(updates['map_zoom'])))
        except (TypeError, ValueError):
            return _err('Invalid map_zoom')

    cfg.update(updates)
    _persist(updates)
    logs_svc.log_portal('admin', 'settings_location_update', updates)
    return _ok({'config': {k: cfg.get(k) for k in _LOCATION_KEYS}})


@settings_bp.route('/api/settings/location/geocode', methods=['POST'])
@ADMIN
def api_geocode():
    """Geocode an address string to lat/lon using Nominatim (free, no key).

    Handles midwest rural fire-number addresses (W7048, N1234, etc.) by
    progressively simplifying the query until Nominatim finds a match.
    """
    import re
    import urllib.request
    import urllib.parse
    import json as _json

    payload = request.get_json(silent=True) or {}
    address = (payload.get('address') or '').strip()
    if not address:
        return _err('address is required')

    base = 'https://nominatim.openstreetmap.org/search?'
    headers = {'User-Agent': 'SkyTrack-Appliance/1.0'}

    def _nom(params):
        params.update({'format': 'json', 'limit': '1', 'countrycodes': 'us'})
        url = base + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            r = _json.loads(resp.read())
        return r if r else None

    # Normalize whitespace, preserve commas for the first try.
    norm = re.sub(r'\s+', ' ', address).strip()

    # 1. Try the address exactly as typed.
    results = None
    try:
        results = _nom({'q': norm})
    except Exception:
        pass

    if not results:
        # Flatten commas so we can tokenize cleanly.
        flat = re.sub(r',', ' ', norm)
        flat = re.sub(r'\s+', ' ', flat).strip()

        # Strip midwest rural fire-number prefix (W7048, N1234, etc.).
        flat = re.sub(r'^[NSEWnsew]\d+\s+', '', flat)

        # Extract zip and state from the tail: "... WI 54723"
        tail = re.search(r'\b([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?\s*$', flat)
        state = tail.group(1).upper() if tail else ''
        zipcode = tail.group(2) if tail else ''
        before_tail = flat[:tail.start()].strip() if tail else flat

        # Progressive word-drop: try the full stripped string, then drop
        # one word from the front each time.  This peels off the street
        # portion until only the city name remains.
        # e.g. "170th ave bay city" → "ave bay city" → "bay city" → "city"
        words = before_tail.split()
        for i in range(len(words)):
            candidate = ' '.join(words[i:])
            suffix = (', ' + state + ' ' + zipcode) if state else ''
            try:
                results = _nom({'q': candidate + suffix})
                if results:
                    break
            except Exception:
                pass

        # Last resort: just the zip code.
        if not results and zipcode:
            try:
                results = _nom({'q': zipcode})
            except Exception:
                pass

    if not results:
        return _err('No results found for that address')

    hit = results[0]
    lat = float(hit['lat'])
    lon = float(hit['lon'])
    display = hit.get('display_name', address)

    cfg = current_app.skytrack_config
    cfg['latitude'] = lat
    cfg['longitude'] = lon
    cfg['location_address'] = display
    cfg['location_source'] = 'manual'
    _persist({'latitude': lat, 'longitude': lon,
              'location_address': display, 'location_source': 'manual'})
    logs_svc.log_portal('admin', 'settings_location_geocode', {
        'address': address, 'lat': lat, 'lon': lon, 'display': display,
    })
    return _ok({'lat': lat, 'lon': lon, 'display_name': display})


@settings_bp.route('/api/settings/kiosk-cards', methods=['GET', 'POST'])
@ADMIN
def api_kiosk_cards():
    """Get or set which cards the kiosk carousel shows."""
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({
            'cards': cfg.get('kiosk_cards', []),
            'interval': cfg.get('kiosk_carousel_interval', 8),
            'map_interval': cfg.get('kiosk_map_interval', 15),
            'show_map': cfg.get('kiosk_show_map', True),
        })

    payload = request.get_json(silent=True) or {}
    updates = {}
    if 'cards' in payload:
        try:
            updates['kiosk_cards'] = list(payload['cards'])
        except (TypeError, ValueError):
            return _err('cards must be a list')
    if 'interval' in payload:
        try:
            updates['kiosk_carousel_interval'] = max(3, min(30, int(payload['interval'])))
        except (TypeError, ValueError):
            return _err('interval must be a number')
    if 'map_interval' in payload:
        try:
            updates['kiosk_map_interval'] = max(5, min(60, int(payload['map_interval'])))
        except (TypeError, ValueError):
            return _err('map_interval must be a number')
    if 'show_map' in payload:
        updates['kiosk_show_map'] = bool(payload['show_map'])
    cfg.update(updates)
    _persist(updates)
    logs_svc.log_portal('admin', 'settings_kiosk_cards_update', updates)
    return _ok(updates)


# ===========================================================================
# 2. DISPLAY
# ===========================================================================

_DISPLAY_KEYS = (
    'display_rotation', 'display_brightness', 'display_sleep_minutes',
    'display_animation_level', 'display_particle_density',
    'display_fullscreen_on_boot',
)

_VALID_ROTATIONS = {'0', '90', '180', '270'}
_VALID_ANIM = {'full', 'reduced', 'off'}


@settings_bp.route('/api/settings/display', methods=['GET', 'POST'])
@ADMIN
def api_display():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({'config': {k: cfg.get(k) for k in _DISPLAY_KEYS}})

    payload = request.get_json(silent=True) or {}
    updates = _take(payload, _DISPLAY_KEYS)

    if 'display_rotation' in updates:
        rot = str(updates['display_rotation'])
        if rot not in _VALID_ROTATIONS:
            return _err('display_rotation must be 0/90/180/270')
        updates['display_rotation'] = rot
    if 'display_animation_level' in updates and \
            updates['display_animation_level'] not in _VALID_ANIM:
        return _err('display_animation_level must be full/reduced/off')
    for k in ('display_brightness', 'display_particle_density'):
        if k in updates:
            try:
                updates[k] = max(0, min(100, int(updates[k])))
            except (TypeError, ValueError):
                return _err(f'{k} must be 0-100')
    if 'display_sleep_minutes' in updates:
        try:
            updates['display_sleep_minutes'] = max(0, int(updates['display_sleep_minutes']))
        except (TypeError, ValueError):
            return _err('display_sleep_minutes must be a non-negative integer')
    if 'display_fullscreen_on_boot' in updates:
        updates['display_fullscreen_on_boot'] = bool(updates['display_fullscreen_on_boot'])

    cfg.update(updates)
    _persist(updates)
    # Mirror rotation/output to /etc/skytrack/display.env so xinitrc picks
    # it up on the next display-service restart or reboot.
    if 'display_rotation' in updates:
        _write_display_env(cfg)
    logs_svc.log_portal('admin', 'settings_display_update', updates)
    return _ok({
        'config': {k: cfg.get(k) for k in _DISPLAY_KEYS},
        'note': 'Rotation applies on next reboot.',
    })


# ===========================================================================
# 3. ACCESS
# ===========================================================================

_ACCESS_KEYS = (
    'session_timeout_hours', 'lockout_max_attempts', 'lockout_window_seconds',
)


@settings_bp.route('/api/settings/access', methods=['GET'])
@ADMIN
def api_access_status():
    cfg = current_app.skytrack_config
    return jsonify({
        'config': {k: cfg.get(k) for k in _ACCESS_KEYS},
    })


@settings_bp.route('/api/settings/access/pin', methods=['POST'])
@ADMIN
def api_change_pin():
    """Replace the admin PIN. PIN is required — empty values are rejected."""
    payload = request.get_json(silent=True) or {}
    try:
        auth_lib.change_admin_pin((payload.get('pin') or '').strip())
    except ValueError as e:
        return _err(str(e))
    logs_svc.log_portal('admin', 'change_pin', {})
    return _ok()


@settings_bp.route('/api/settings/access/session', methods=['POST'])
@SUPER
def api_access_session():
    cfg = current_app.skytrack_config
    payload = request.get_json(silent=True) or {}
    updates = _take(payload, _ACCESS_KEYS)
    for k, lo, hi in (
        ('session_timeout_hours', 1, 72),
        ('lockout_max_attempts', 1, 50),
        ('lockout_window_seconds', 30, 86400),
    ):
        if k in updates:
            try:
                updates[k] = max(lo, min(hi, int(updates[k])))
            except (TypeError, ValueError):
                return _err(f'{k} must be {lo}-{hi}')
    cfg.update(updates)
    _persist(updates)
    logs_svc.log_portal('admin', 'settings_access_update', updates)
    return _ok({'config': {k: cfg.get(k) for k in _ACCESS_KEYS}})


# ===========================================================================
# 4. NETWORK
# ===========================================================================

_NETWORK_KEYS = (
    'cellular_enabled', 'cellular_apn', 'wifi_client_enabled',
    'hotspot_auto_start', 'time_sync_source', 'metered_connection',
)
_VALID_TIME_SOURCES = {'ntp', 'cellular', 'gps'}


@settings_bp.route('/api/settings/network', methods=['GET', 'POST'])
@ADMIN
def api_network():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        rec = auth_lib.read_auth() or {}
        live = network_svc.get_network_status(cfg) or {}
        # Surface the live APN value (read straight off the gsm connection)
        # so the input field in Settings → Network reflects whatever NM is
        # actually using, not just whatever happens to be persisted to YAML.
        live_apn = (live.get('cellular') or {}).get('apn') or ''
        cfg_view = {k: cfg.get(k) for k in _NETWORK_KEYS}
        if live_apn:
            cfg_view['cellular_apn'] = live_apn
        return jsonify({
            'config': cfg_view,
            'hotspot': {
                'ssid': cfg.get('hotspot_ssid'),
                'gateway': cfg.get('hotspot_gateway'),
                'subnet': cfg.get('hotspot_subnet'),
                'password': rec.get('hotspot_password'),
                'auto_start': cfg.get('hotspot_auto_start', True),
            },
            'status': live,
            'saved_wifi': cfg.get('saved_wifi_networks', []),
        })

    payload = request.get_json(silent=True) or {}
    updates = _take(payload, _NETWORK_KEYS)
    if 'time_sync_source' in updates and updates['time_sync_source'] not in _VALID_TIME_SOURCES:
        return _err('time_sync_source must be ntp/cellular/gps')
    for k in ('cellular_enabled', 'wifi_client_enabled', 'hotspot_auto_start',
              'metered_connection'):
        if k in updates:
            updates[k] = bool(updates[k])
    if 'cellular_apn' in updates:
        updates['cellular_apn'] = str(updates['cellular_apn'] or '').strip()
    if 'hotspot_ssid' in payload:
        updates['hotspot_ssid'] = str(payload['hotspot_ssid']).strip() \
            or cfg.get('hotspot_ssid')
    apn_changed = (
        'cellular_apn' in updates
        and updates['cellular_apn']
        and updates['cellular_apn'] != (cfg.get('cellular_apn') or '')
    )
    wifi_changed = (
        'wifi_client_enabled' in updates
        and bool(updates['wifi_client_enabled']) != bool(cfg.get('wifi_client_enabled'))
    )
    cell_changed = (
        'cellular_enabled' in updates
        and bool(updates['cellular_enabled']) != bool(cfg.get('cellular_enabled'))
    )
    cfg.update(updates)
    _persist(updates)
    # Push APN to NetworkManager alongside config persistence so operators
    # don't have to hit a second button just to have the modem pick it up.
    apn_result = None
    if apn_changed:
        apn_result = network_svc.set_cellular_apn(updates['cellular_apn'])
        logs_svc.log_network('cellular_apn_set', {
            'apn': updates['cellular_apn'],
            'ok': bool(apn_result.get('ok')),
            'backend': apn_result.get('backend'),
        })
    if wifi_changed:
        network_svc.set_wifi_radio(bool(updates['wifi_client_enabled']))
        logs_svc.log_network('wifi_radio_toggle', {
            'enabled': bool(updates['wifi_client_enabled']),
        })
    if cell_changed:
        network_svc.set_cellular_radio(bool(updates['cellular_enabled']))
        logs_svc.log_network('cellular_radio_toggle', {
            'enabled': bool(updates['cellular_enabled']),
        })
    logs_svc.log_portal('admin', 'settings_network_update', updates)
    return _ok({
        'config': {k: cfg.get(k) for k in _NETWORK_KEYS},
        'apn_result': apn_result,
    })


def _hotspot_lockout_guard():
    """Refuse a rotate/restart if the caller is connected via the hotspot.

    Phase 2.2: see blueprints/network.py::_lockout_guard for the full
    rationale. This is the settings-blueprint copy so both legacy
    (/network) and canonical (/settings) paths enforce the same rule.
    """
    cfg = current_app.skytrack_config
    if not hotspot.caller_on_hotspot(request.remote_addr, cfg):
        return None
    forced = request.args.get('force') == '1'
    if not forced:
        body = (request.get_json(silent=True) or {}) if request.is_json else {}
        forced = bool(body.get('force'))
    if forced:
        return None
    return (jsonify({
        'ok': False,
        'error': 'hotspot_lockout_guard',
        'message': ('You are connected via the SkyTrack hotspot. '
                    'Restarting hostapd will disconnect you before '
                    'the response arrives. Reconnect over eth0 or '
                    'cellular, or pass force=1 to override.'),
        'remote_addr': request.remote_addr,
    }), 409)


@settings_bp.route('/api/settings/network/hotspot/password', methods=['POST'])
@ADMIN
def api_hotspot_password():
    """Regenerate or set the hotspot WPA2 password.

    Phase 2.2: atomic — the password is written to auth.json AND the
    full hotspot_apply.sh re-render is invoked in the same request so
    /etc/hostapd/hostapd.conf picks up the new passphrase and hostapd
    is bounced. Previously the password went into auth.json and stayed
    there until the next reboot, which is the 2.5.x bug this fixes.
    """
    blocked = _hotspot_lockout_guard()
    if blocked:
        return blocked
    payload = request.get_json(silent=True) or {}
    new_pw = payload.get('password')
    pw = auth_lib.set_hotspot_password(new_pw or None)
    apply_result = hotspot.rotate_hotspot_apply()
    logs_svc.log_network('hotspot_password_set',
                         {'len': len(pw or ''), 'apply_ok': apply_result.get('ok', False)})
    if not apply_result.get('ok', False):
        return _ok({'password': pw, 'apply': apply_result,
                    'warning': 'password stored but hostapd re-apply failed'})
    return _ok({'password': pw, 'apply': apply_result})


@settings_bp.route('/api/settings/network/wifi', methods=['POST'])
@ADMIN
def api_wifi_add():
    """Save a WiFi network. Phase 1 just persists to config; nmcli wiring
    is wired in by the network watchdog when the radio is free."""
    payload = request.get_json(silent=True) or {}
    ssid = (payload.get('ssid') or '').strip()
    psk = (payload.get('password') or '').strip()
    if not ssid:
        return _err('ssid required')
    cfg = current_app.skytrack_config
    saved = list(cfg.get('saved_wifi_networks') or [])
    saved = [n for n in saved if n.get('ssid') != ssid]
    saved.append({'ssid': ssid, 'has_password': bool(psk)})
    cfg['saved_wifi_networks'] = saved
    _persist({'saved_wifi_networks': saved})
    if psk:
        auth_lib.set_secret(f'wifi_psk_{ssid}', psk)
    logs_svc.log_network('wifi_saved', {'ssid': ssid})
    return _ok({'saved_wifi': saved})


@settings_bp.route('/api/settings/network/wifi/remove', methods=['POST'])
@ADMIN
def api_wifi_remove():
    payload = request.get_json(silent=True) or {}
    ssid = (payload.get('ssid') or '').strip()
    if not ssid:
        return _err('ssid required')
    cfg = current_app.skytrack_config
    saved = [n for n in (cfg.get('saved_wifi_networks') or []) if n.get('ssid') != ssid]
    cfg['saved_wifi_networks'] = saved
    _persist({'saved_wifi_networks': saved})
    auth_lib.set_secret(f'wifi_psk_{ssid}', None)
    logs_svc.log_network('wifi_removed', {'ssid': ssid})
    return _ok({'saved_wifi': saved})


# ===========================================================================
# 4b. CLOUDFLARE TUNNEL
# ===========================================================================

_TUNNEL_TOKEN_PATH = '/etc/skytrack/tunnel_token'
_TUNNEL_ID_PATH = '/etc/skytrack/tunnel_id'
_TUNNEL_CONFIG_PATH = '/etc/cloudflared/config.yml'

@settings_bp.route('/api/settings/tunnel', methods=['GET'])
@ADMIN
def api_tunnel_status():
    """Return tunnel configuration and live status."""
    cfg = current_app.skytrack_config
    installed = shutil.which('cloudflared') is not None
    running = False
    if installed:
        try:
            result = subprocess.run(
                ['systemctl', 'is-active', 'cloudflared'],
                capture_output=True, text=True, timeout=5,
            )
            running = result.stdout.strip() == 'active'
        except Exception:
            pass
    token_set = (os.path.exists(_TUNNEL_TOKEN_PATH)
                 or os.path.exists(_TUNNEL_ID_PATH)
                 or os.path.exists(_TUNNEL_CONFIG_PATH))
    return jsonify({
        'installed': installed,
        'running': running,
        'enabled': cfg.get('cloudflared_enabled', False),
        'token_set': token_set,
        'tunnel_name': cfg.get('cloudflared_tunnel_name', ''),
        'hostname': cfg.get('cloudflared_hostname', ''),
    })


@settings_bp.route('/api/settings/tunnel/install', methods=['POST'])
@ADMIN
def api_tunnel_install():
    """Install a tunnel with a connector token from the CF dashboard."""
    payload = request.get_json(silent=True) or {}
    token = (payload.get('token') or '').strip()
    if not token:
        return _err('Connector token is required')

    if not shutil.which('cloudflared'):
        return _err('cloudflared binary not installed — run install.sh first')

    try:
        subprocess.run(['systemctl', 'stop', 'cloudflared'],
                       capture_output=True, timeout=10)
        subprocess.run(['systemctl', 'disable', 'cloudflared'],
                       capture_output=True, timeout=10)
    except Exception:
        pass

    try:
        result = subprocess.run(
            ['cloudflared', 'service', 'install', token],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout or '').strip()
            if 'already' not in err_msg.lower():
                return _err(f'cloudflared service install failed: {err_msg}')
    except subprocess.TimeoutExpired:
        return _err('cloudflared service install timed out')
    except Exception as e:
        return _err(f'cloudflared service install error: {e}')

    try:
        subprocess.run(['systemctl', 'daemon-reload'], capture_output=True, timeout=10)
        subprocess.run(['systemctl', 'enable', 'cloudflared'], capture_output=True, timeout=10)
        subprocess.run(['systemctl', 'start', 'cloudflared'], capture_output=True, timeout=10)
    except Exception:
        pass

    os.makedirs('/etc/skytrack', exist_ok=True)
    try:
        with open(_TUNNEL_TOKEN_PATH, 'w') as f:
            f.write(token)
        os.chmod(_TUNNEL_TOKEN_PATH, 0o600)
    except Exception:
        pass

    tunnel_name = (payload.get('tunnel_name') or '').strip()
    hostname = (payload.get('hostname') or '').strip()
    cfg = current_app.skytrack_config
    cfg['cloudflared_enabled'] = True
    updates = {'cloudflared_enabled': True}
    if tunnel_name:
        cfg['cloudflared_tunnel_name'] = tunnel_name
        updates['cloudflared_tunnel_name'] = tunnel_name
    if hostname:
        cfg['cloudflared_hostname'] = hostname
        updates['cloudflared_hostname'] = hostname
    _persist(updates)

    logs_svc.log_portal('admin', 'tunnel_installed', {
        'tunnel_name': tunnel_name,
        'hostname': hostname,
    })

    time.sleep(2)
    running = False
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', 'cloudflared'],
            capture_output=True, text=True, timeout=5,
        )
        running = result.stdout.strip() == 'active'
    except Exception:
        pass

    return _ok({'running': running, 'tunnel_name': tunnel_name})


@settings_bp.route('/api/settings/tunnel/toggle', methods=['POST'])
@ADMIN
def api_tunnel_toggle():
    """Start or stop the tunnel service."""
    payload = request.get_json(silent=True) or {}
    enable = bool(payload.get('enabled', True))

    action = 'start' if enable else 'stop'
    try:
        subprocess.run(
            ['systemctl', action, 'cloudflared'],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return _err(f'Failed to {action} cloudflared: {e}')

    cfg = current_app.skytrack_config
    cfg['cloudflared_enabled'] = enable
    _persist({'cloudflared_enabled': enable})
    label = 'started' if enable else 'stopped'
    logs_svc.log_portal('admin', f'tunnel_{label}', {})
    return _ok({'enabled': enable})


# ===========================================================================
# 5. INTEGRATIONS
# ===========================================================================

_INTEGRATION_FLAGS = (
    'aeroapi_enabled', 'aeroapi_calls_per_hour', 'aeroapi_calls_per_day',
    'aeroapi_cost_per_call', 'aeroapi_monthly_budget_usd',
    'opensky_enabled', 'opensky_poll_minutes',
    'enrichment_ttl_hours', 'weather_enabled', 'weather_provider',
)
_INTEGRATION_SECRETS = (
    'aeroapi_key', 'opensky_username', 'opensky_password', 'openweather_api_key',
)


@settings_bp.route('/api/settings/integrations', methods=['GET', 'POST'])
@ADMIN
def api_integrations():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        usage = enrich.usage_summary('aeroapi')
        per_call = float(cfg.get('aeroapi_cost_per_call') or 0)
        est_month_calls = int(cfg.get('aeroapi_calls_per_day') or 0) * 30
        est_month_cost = est_month_calls * per_call
        return jsonify({
            'config': {k: cfg.get(k) for k in _INTEGRATION_FLAGS},
            'secrets_set': {k: bool(auth_lib.get_secret(k)) for k in _INTEGRATION_SECRETS},
            'usage_aeroapi': usage,
            'usage_opensky': enrich.usage_summary('opensky'),
            'estimated_monthly_calls': est_month_calls,
            'estimated_monthly_cost': est_month_cost,
        })

    payload = request.get_json(silent=True) or {}
    flag_updates = _take(payload, _INTEGRATION_FLAGS)
    cfg.update(flag_updates)
    if flag_updates:
        _persist(flag_updates)
    secret_updates = payload.get('secrets') or {}
    for key in _INTEGRATION_SECRETS:
        if key in secret_updates:
            auth_lib.set_secret(key, secret_updates[key])
    logs_svc.log_portal('admin', 'settings_integrations_update',
                        {'flags': list(flag_updates), 'secrets': list(secret_updates)})
    return _ok()


@settings_bp.route('/api/settings/integrations/test/aeroapi', methods=['POST'])
@ADMIN
def api_test_aeroapi():
    return jsonify(enrich.test_aeroapi())


@settings_bp.route('/api/settings/integrations/test/opensky', methods=['POST'])
@ADMIN
def api_test_opensky():
    return jsonify(enrich.test_opensky())


@settings_bp.route('/api/settings/integrations/test/weather', methods=['POST'])
@ADMIN
def api_test_weather():
    try:
        wx = current_app.weather_svc.get_weather()
        return jsonify({'ok': bool(wx), 'message': 'OK' if wx else 'no data',
                        'sample': wx})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 200


# ===========================================================================
# 6. FEEDERS
# ===========================================================================

_FEEDER_FLAGS = ('feed_over_cellular', 'feed_over_wifi_only')
_FEEDER_SECRETS = ('fr24_key', 'piaware_feeder_id', 'flightaware_id')


@settings_bp.route('/api/settings/feeders', methods=['GET', 'POST'])
@ADMIN
def api_feeders():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({
            'config': {k: cfg.get(k) for k in _FEEDER_FLAGS},
            'secrets_set': {k: bool(auth_lib.get_secret(k)) for k in _FEEDER_SECRETS},
            'flightaware_active': _systemd_active('piaware'),
            'fr24_active': _systemd_active('fr24feed'),
        })
    payload = request.get_json(silent=True) or {}
    flag_updates = _take(payload, _FEEDER_FLAGS)
    for k in _FEEDER_FLAGS:
        if k in flag_updates:
            flag_updates[k] = bool(flag_updates[k])
    cfg.update(flag_updates)
    if flag_updates:
        _persist(flag_updates)
    secret_updates = (payload.get('secrets') or {})
    for key in _FEEDER_SECRETS:
        if key in secret_updates:
            auth_lib.set_secret(key, secret_updates[key])
    logs_svc.log_portal('admin', 'settings_feeders_update',
                        {'flags': list(flag_updates), 'secrets': list(secret_updates)})
    return _ok()


@settings_bp.route('/api/settings/feeders/restart', methods=['POST'])
@ADMIN
def api_feeders_restart():
    results = {}
    for unit in ('piaware', 'fr24feed'):
        ok, msg = _shell(['systemctl', 'restart', unit], timeout=20)
        results[unit] = {'ok': ok, 'message': msg}
    logs_svc.log_portal('admin', 'feeders_restart', results)
    return jsonify({'ok': any(r['ok'] for r in results.values()),
                    'results': results})


def _systemd_active(unit: str) -> bool:
    try:
        r = subprocess.run(['systemctl', 'is-active', unit],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() == 'active'
    except Exception:
        return False


def _systemd_presence(unit: str) -> dict:
    """Richer 3-state answer: not-installed / installed-but-stopped / active.

    Uses `systemctl show` so we get a single round-trip and the LoadState
    field, which tells us whether the unit file even exists.
    """
    out = {'installed': False, 'active': False, 'state': 'missing'}
    try:
        r = subprocess.run(
            ['systemctl', 'show', unit,
             '--property=LoadState', '--property=ActiveState'],
            capture_output=True, text=True, timeout=3,
        )
        fields = {}
        for line in (r.stdout or '').splitlines():
            if '=' in line:
                k, _, v = line.partition('=')
                fields[k.strip()] = v.strip()
        load = fields.get('LoadState', '')
        active = fields.get('ActiveState', '')
        if load == 'loaded':
            out['installed'] = True
            out['active'] = (active == 'active')
            out['state']  = active or 'unknown'
        else:
            out['state'] = 'missing'  # unit not installed
    except FileNotFoundError:
        out['state'] = 'no-systemd'
    except Exception as e:
        out['state'] = f'error: {e}'
    return out


# ===========================================================================
# 7. DATA
# ===========================================================================

_DATA_KEYS = (
    'default_dashboard_time_filter', 'sightings_retention_days',
    'logs_retention_days', 'max_records', 'ignore_helicopters',
    'ignore_ground_targets', 'min_altitude_ft', 'signal_threshold_dbm',
    # --- New touch-shell keys ---
    'data_retention_days', 'data_max_rows', 'data_autovacuum',
)


@settings_bp.route('/api/settings/data', methods=['GET', 'POST'])
@ADMIN
def api_data():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({'config': {k: cfg.get(k) for k in _DATA_KEYS}})
    payload = request.get_json(silent=True) or {}
    updates = _take(payload, _DATA_KEYS)
    for k in ('ignore_helicopters', 'ignore_ground_targets', 'data_autovacuum'):
        if k in updates:
            updates[k] = bool(updates[k])
    for k in ('sightings_retention_days', 'logs_retention_days', 'max_records',
              'min_altitude_ft', 'signal_threshold_dbm',
              'data_retention_days', 'data_max_rows'):
        if k in updates:
            try:
                updates[k] = int(updates[k])
            except (TypeError, ValueError):
                return _err(f'{k} must be an integer')
    cfg.update(updates)
    _persist(updates)
    logs_svc.log_portal('admin', 'settings_data_update', updates)
    return _ok({'config': {k: cfg.get(k) for k in _DATA_KEYS}})


# ===========================================================================
# 8. ALERTS  (includes buzzer + thresholds)
# ===========================================================================

_ALERT_KEYS = (
    'buzzer_enabled', 'buzzer_volume', 'buzzer_threshold_temp_f',
    'buzzer_threshold_hum', 'alert_no_aircraft_minutes', 'alert_cell_disconnect',
    'alert_wifi_disconnect', 'alert_api_budget_pct', 'alert_low_storage_pct',
    # --- New touch-shell alert keys ---
    'alerts_enabled', 'alert_military', 'alert_emergency', 'alert_heavy',
    'alert_watchlist', 'alert_radius_nm', 'alert_cooldown_minutes',
    'alert_delivery', 'alert_volume', 'device_temp_alarm_c',
)
_VALID_ALERT_DELIVERY = {'toast', 'sound', 'both', 'off'}


@settings_bp.route('/api/settings/alerts', methods=['GET', 'POST'])
@ADMIN
def api_alerts():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({
            'config': {k: cfg.get(k) for k in _ALERT_KEYS},
            'buzzer_available': current_app.buzzer.available(),
        })
    payload = request.get_json(silent=True) or {}
    updates = _take(payload, _ALERT_KEYS)
    bool_keys = ('buzzer_enabled', 'alert_cell_disconnect', 'alert_wifi_disconnect',
                 'alerts_enabled', 'alert_military', 'alert_emergency',
                 'alert_heavy', 'alert_watchlist')
    for k in bool_keys:
        if k in updates:
            updates[k] = bool(updates[k])
    for k in ('buzzer_volume', 'alert_api_budget_pct', 'alert_low_storage_pct',
              'alert_volume'):
        if k in updates:
            try:
                updates[k] = max(0, min(100, int(updates[k])))
            except (TypeError, ValueError):
                return _err(f'{k} must be 0-100')
    if 'alert_no_aircraft_minutes' in updates:
        try:
            updates['alert_no_aircraft_minutes'] = max(0, int(updates['alert_no_aircraft_minutes']))
        except (TypeError, ValueError):
            return _err('alert_no_aircraft_minutes must be an integer')
    if 'alert_radius_nm' in updates:
        try:
            updates['alert_radius_nm'] = max(1, min(250, int(updates['alert_radius_nm'])))
        except (TypeError, ValueError):
            return _err('alert_radius_nm must be 1-250')
    if 'alert_cooldown_minutes' in updates:
        try:
            updates['alert_cooldown_minutes'] = max(1, min(1440, int(updates['alert_cooldown_minutes'])))
        except (TypeError, ValueError):
            return _err('alert_cooldown_minutes must be 1-1440')
    if 'alert_delivery' in updates and updates['alert_delivery'] not in _VALID_ALERT_DELIVERY:
        return _err('alert_delivery must be toast/sound/both/off')
    for k in ('buzzer_threshold_temp_f', 'buzzer_threshold_hum'):
        if k in updates:
            try:
                updates[k] = float(updates[k])
            except (TypeError, ValueError):
                return _err(f'{k} must be a number')
    if 'device_temp_alarm_c' in updates:
        try:
            updates['device_temp_alarm_c'] = max(50, min(100, int(updates['device_temp_alarm_c'])))
        except (TypeError, ValueError):
            return _err('device_temp_alarm_c must be 50-100')
    cfg.update(updates)
    if 'buzzer_volume' in updates:
        current_app.buzzer.set_volume(int(updates['buzzer_volume']))
    if 'buzzer_enabled' in updates:
        current_app.buzzer.set_enabled(bool(updates['buzzer_enabled']))
    _persist(updates)
    logs_svc.log_portal('admin', 'settings_alerts_update', updates)
    return _ok({'config': {k: cfg.get(k) for k in _ALERT_KEYS}})


@settings_bp.route('/api/settings/alerts/buzzer/test', methods=['POST'])
@ADMIN
def api_buzzer_test():
    """Sound the on-board buzzer for a quick three-beep audible check.

    Always returns a structured result the UI can toast verbatim:
        {ok, available, enabled, last_error, message}
    `ok=False` is returned (200) when the GPIO is missing or the
    operator has the buzzer disabled, so the UI can show the actual
    reason instead of a generic failure.
    """
    bz = current_app.buzzer
    result = bz.test()
    result.setdefault('available', bool(getattr(bz, '_available', False)))
    result['enabled'] = bool(getattr(bz, 'enabled', False))
    result['last_error'] = getattr(bz, 'last_error', '') or ''
    logs_svc.log_portal('admin', 'buzzer_test', {
        'ok': bool(result.get('ok')),
        'available': bool(result.get('available')),
        'enabled': bool(result.get('enabled')),
    })
    return jsonify(result)


@settings_bp.route('/api/settings/alerts/buzzer/status', methods=['GET'])
@ADMIN
def api_buzzer_status():
    bz = current_app.buzzer
    return jsonify({
        'available':  bool(bz.available()),
        'enabled':    bool(getattr(bz, 'enabled', False)),
        'volume':     int(getattr(bz, 'volume', 0)),
        'pin':        int(getattr(bz, 'pin', 0)),
        'last_error': getattr(bz, 'last_error', '') or '',
    })


@settings_bp.route('/api/settings/hardware/status', methods=['GET'])
@ADMIN
def api_hardware_status():
    """Combined sensor + buzzer truthfulness panel for Settings → Alerts.

    Returns the actual init outcome of both subsystems so the operator
    can tell at a glance whether the temperature in the topbar is real
    DHT22 data or mock, and whether the buzzer test will actually beep.
    """
    sensor_reading = current_app.sensor_svc.read() or {}
    bz = current_app.buzzer
    return jsonify({
        'sensor': {
            'available':     bool(getattr(current_app.sensor_svc, 'available', False)),
            'source':        sensor_reading.get('source') or 'mock',
            'mock':          bool(sensor_reading.get('mock')),
            'pin':           sensor_reading.get('pin'),
            'temperature_f': sensor_reading.get('temperature_f'),
            'temperature_c': sensor_reading.get('temperature_c'),
            'humidity':      sensor_reading.get('humidity'),
            'last_error':    sensor_reading.get('last_error') or getattr(current_app.sensor_svc, 'last_error', '') or '',
            'timestamp':     sensor_reading.get('timestamp'),
        },
        'buzzer': {
            'available':  bool(bz.available()),
            'enabled':    bool(getattr(bz, 'enabled', False)),
            'volume':     int(getattr(bz, 'volume', 0)),
            'pin':        int(getattr(bz, 'pin', 0)),
            'last_error': getattr(bz, 'last_error', '') or '',
        },
    })


# ===========================================================================
# 9. UPDATES & BACKUP
# ===========================================================================

_UPDATE_KEYS = (
    'update_check_enabled', 'update_allow_cellular', 'auto_backup_frequency',
    'ota_remote', 'ota_branch',
    # --- New touch-shell update keys ---
    'ota_auto_check', 'ota_channel', 'backup_frequency', 'backup_keep',
)
_VALID_BACKUP_FREQ = {'off', 'daily', 'weekly', 'monthly'}
_VALID_OTA_CHANNELS = {'stable', 'beta'}


def _backup_dir() -> Path:
    cfg = current_app.skytrack_config
    p = Path(cfg.get('backup_dir', '/var/lib/skytrack/backups'))
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


_RELEASES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'releases.json')


def _load_releases(path=None):
    """Load releases.json, return list of release dicts (newest first)."""
    p = path or _RELEASES_PATH
    try:
        with open(p, 'r') as f:
            return json.load(f)
    except Exception:
        return []


def _current_release():
    """Return the release entry matching the running version base."""
    for r in _load_releases():
        if r.get('version') == __version_base__:
            return r
    return None


@settings_bp.route('/api/settings/updates', methods=['GET', 'POST'])
@ADMIN
def api_updates():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({
            'config': {k: cfg.get(k) for k in _UPDATE_KEYS},
            'app_version': SKYTRACK_VERSION,
            'app_version_base': __version_base__,
            'current_release': _current_release(),
            'backups': _list_backups(),
        })
    payload = request.get_json(silent=True) or {}
    updates = _take(payload, _UPDATE_KEYS)
    if 'auto_backup_frequency' in updates and \
            updates['auto_backup_frequency'] not in _VALID_BACKUP_FREQ:
        return _err('auto_backup_frequency must be off/daily/weekly/monthly')
    if 'backup_frequency' in updates and \
            updates['backup_frequency'] not in _VALID_BACKUP_FREQ:
        return _err('backup_frequency must be off/daily/weekly/monthly')
    if 'ota_channel' in updates and updates['ota_channel'] not in _VALID_OTA_CHANNELS:
        return _err('ota_channel must be stable/beta')
    for k in ('update_check_enabled', 'update_allow_cellular', 'ota_auto_check'):
        if k in updates:
            updates[k] = bool(updates[k])
    if 'backup_keep' in updates:
        try:
            updates['backup_keep'] = max(1, min(50, int(updates['backup_keep'])))
        except (TypeError, ValueError):
            return _err('backup_keep must be an integer 1-50')
    cfg.update(updates)
    _persist(updates)
    logs_svc.log_portal('admin', 'settings_updates_update', updates)
    return _ok({'config': {k: cfg.get(k) for k in _UPDATE_KEYS}})


# ---------------------------------------------------------------------------
# OTA workspace helpers
#
# /opt/skytrack is a plain rsync target — the installer excludes .git, so we
# cannot run git commands there. Instead, OTA clones the upstream repo once
# into /var/lib/skytrack/ota-workspace/ (owned by the skytrack service user),
# fetches/merges there, then rsyncs the workspace on top of /opt/skytrack and
# restarts skytrack-app. This also gives git a writable HOME + known_hosts
# outside the service user's real home, so SSH-based remotes no longer fail
# on "Host key verification failed" or "could not create /.ssh".
# ---------------------------------------------------------------------------


def _ota_home() -> str:
    """Directory git should treat as HOME — owns .ssh, .gitconfig, etc."""
    cfg = current_app.skytrack_config
    ws = os.path.abspath(
        cfg.get('ota_workspace_dir', '/var/lib/skytrack/ota-workspace')
    )
    home = os.path.dirname(ws) or '/var/lib/skytrack'
    try:
        os.makedirs(home, mode=0o755, exist_ok=True)
    except Exception:
        pass
    return home


def _ota_workspace() -> str:
    cfg = current_app.skytrack_config
    return os.path.abspath(
        cfg.get('ota_workspace_dir', '/var/lib/skytrack/ota-workspace')
    )


def _ota_repo_url() -> str:
    """Return the OTA upstream URL, with credentials injected for the
    configured `ota_auth_mode` (ssh / https_none / https_token).

    For ssh and https_none this is the raw URL. For https_token we splice
    the token from auth.json into the netloc at command time — it never
    lands in the workspace's .git/config. All of that logic lives in
    `git_auth` so swapping away from deploy keys is a config change, not
    a code change.
    """
    cfg = current_app.skytrack_config
    url = git_auth.resolve_remote_url(cfg)
    if not url:
        return 'https://github.com/QDRN1/Skytrack.git'
    return url


def _ota_env() -> dict:
    """Return a copy of os.environ tuned for non-interactive git.

    Delegates to `git_auth.build_env`, which picks the right set of
    GIT_* / SSH variables based on the current `ota_auth_mode`. See
    git_auth.py for the full contract.
    """
    cfg = current_app.skytrack_config
    return git_auth.build_env(cfg, _ota_home())


def _ensure_ota_workspace() -> tuple:
    """Make sure the OTA workspace is a working git clone. Idempotent.

    Returns (ok, message). On first call, clones the repo shallow; on
    subsequent calls verifies the clone is intact. Never touches
    /opt/skytrack itself.
    """
    ws = _ota_workspace()
    parent = os.path.dirname(ws)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            return False, f'cannot create {parent}: {e}'
    git_dir = os.path.join(ws, '.git')
    if os.path.isdir(git_dir):
        return True, 'workspace ready'
    url = _ota_repo_url()
    if not url:
        return False, 'no ota_repo_url configured'
    env = _ota_env()
    ok, msg = _shell(
        ['git', 'clone', '--depth', '50', url, ws],
        timeout=180, env=env,
    )
    if not ok:
        return False, f'clone failed: {msg[-400:]}'
    return True, 'cloned'


def _git_in_workspace(args, timeout=60) -> tuple:
    ws = _ota_workspace()
    env = _ota_env()
    return _shell(['git', '-C', ws] + list(args), timeout=timeout, env=env)


@settings_bp.route('/api/settings/updates/check', methods=['POST'])
@ADMIN
def api_updates_check():
    cfg = current_app.skytrack_config
    ok, msg = _ensure_ota_workspace()
    if not ok:
        logs_svc.log_portal('admin', 'ota_check',
                            {'ok': False, 'phase': 'workspace', 'tail': msg[-200:]})
        return jsonify({'ok': False, 'message': msg[-400:]})
    remote = cfg.get('ota_remote', 'origin') or 'origin'
    branch = cfg.get('ota_branch', 'claude/skytrack-adsb-tracker-N8p6u') or 'claude/skytrack-adsb-tracker-N8p6u'
    ok, msg = _git_in_workspace(
        ['fetch', '--depth', '50', remote, branch], timeout=90,
    )
    if not ok:
        logs_svc.log_portal('admin', 'ota_check',
                            {'ok': False, 'phase': 'fetch', 'tail': msg[-200:]})
        return jsonify({'ok': False, 'message': msg[-400:] or 'fetch failed'})
    behind = None
    ok2, ahead_msg = _git_in_workspace(
        ['rev-list', '--count', f'HEAD..{remote}/{branch}'], timeout=15,
    )
    if ok2:
        try:
            behind = int(ahead_msg.strip())
        except (TypeError, ValueError):
            behind = None

    remote_version = None
    remote_version_base = None
    remote_releases = []
    ref = f'{remote}/{branch}'
    ok3, ver_raw = _git_in_workspace(
        ['show', f'{ref}:_version.py'], timeout=10,
    )
    if ok3 and ver_raw:
        for line in ver_raw.splitlines():
            if line.strip().startswith('__version_base__'):
                try:
                    remote_version_base = line.split('=', 1)[1].strip().strip('"').strip("'")
                except Exception:
                    pass
    ok4, rel_raw = _git_in_workspace(
        ['show', f'{ref}:releases.json'], timeout=10,
    )
    if ok4 and rel_raw:
        try:
            remote_releases = json.loads(rel_raw)
        except Exception:
            remote_releases = []

    latest_release = None
    if remote_releases:
        latest_release = remote_releases[0]
        remote_version = latest_release.get('version')

    ok5, date_raw = _git_in_workspace(
        ['log', '-1', '--format=%aI', ref, '--'], timeout=10,
    )
    latest_date = date_raw.strip() if ok5 and date_raw.strip() else None

    stamp = datetime.utcnow().isoformat() + 'Z'
    cfg['ota_last_check'] = stamp
    _persist({'ota_last_check': stamp})
    logs_svc.log_portal('admin', 'ota_check',
                        {'ok': True, 'behind': behind,
                         'remote_version': remote_version})
    return jsonify({
        'ok': True,
        'message': msg[-400:] or ('up to date' if behind == 0 else 'fetched'),
        'behind': behind,
        'checked_at': stamp,
        'remote_version': remote_version or remote_version_base,
        'remote_date': latest_date,
        'latest_release': latest_release,
        'all_releases': remote_releases,
    })


@settings_bp.route('/api/settings/updates/apply', methods=['POST'])
@ADMIN
def api_updates_apply():
    cfg = current_app.skytrack_config
    ok, msg = _ensure_ota_workspace()
    if not ok:
        logs_svc.log_portal('admin', 'ota_apply',
                            {'ok': False, 'phase': 'workspace', 'tail': msg[-200:]})
        return jsonify({'ok': False, 'message': msg[-400:]})
    remote = cfg.get('ota_remote', 'origin') or 'origin'
    branch = cfg.get('ota_branch', 'claude/skytrack-adsb-tracker-N8p6u') or 'claude/skytrack-adsb-tracker-N8p6u'
    # Fetch then hard-reset — never merge, never leave stray files behind
    # from an aborted apply.
    ok, msg = _git_in_workspace(
        ['fetch', '--depth', '50', remote, branch], timeout=180,
    )
    if not ok:
        logs_svc.log_portal('admin', 'ota_apply',
                            {'ok': False, 'phase': 'fetch', 'tail': msg[-200:]})
        return jsonify({'ok': False, 'message': msg[-400:] or 'fetch failed'})
    ok, msg = _git_in_workspace(
        ['reset', '--hard', f'{remote}/{branch}'], timeout=30,
    )
    if not ok:
        logs_svc.log_portal('admin', 'ota_apply',
                            {'ok': False, 'phase': 'reset', 'tail': msg[-200:]})
        return jsonify({'ok': False, 'message': msg[-400:] or 'reset failed'})
    # Sync workspace → /opt/skytrack. Preserve the venv, user config, DB,
    # and anything else that isn't part of the source tree.
    repo_dir = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + '/..')
    ws = _ota_workspace()
    rsync_args = [
        'rsync', '-a', '--delete',
        '--exclude=.git/',
        '--exclude=.venv/',
        '--exclude=venv/',
        '--exclude=__pycache__/',
        '--exclude=*.pyc',
        '--exclude=config.yaml',
        '--exclude=static/vendor/',
        '--exclude=legacy/',
        f'{ws.rstrip("/")}/', f'{repo_dir.rstrip("/")}/',
    ]
    ok, rsync_msg = _shell(rsync_args, timeout=180)
    if not ok:
        logs_svc.log_portal('admin', 'ota_apply',
                            {'ok': False, 'phase': 'rsync', 'tail': rsync_msg[-200:]})
        return jsonify({'ok': False, 'message': rsync_msg[-400:] or 'rsync failed'})
    # Best-effort restart. The client will lose this connection mid-response;
    # the UI catches that and polls /healthz until the new version answers.
    _shell(['systemctl', 'restart', 'skytrack-app.service'], timeout=20)
    logs_svc.log_portal('admin', 'ota_apply', {'ok': True})
    return jsonify({
        'ok': True,
        'message': 'Update applied. The service is restarting — the page '
                   'will reload in a few seconds.',
    })


def _list_backups() -> list:
    p = _backup_dir()
    out = []
    try:
        for f in sorted(p.glob('*.tar.gz'), key=lambda x: x.stat().st_mtime, reverse=True):
            st = f.stat()
            out.append({
                'name': f.name,
                'size': st.st_size,
                'created_at': datetime.utcfromtimestamp(st.st_mtime).isoformat() + 'Z',
            })
    except Exception:
        pass
    return out


@settings_bp.route('/api/settings/updates/backup/create', methods=['POST'])
@ADMIN
def api_backup_create():
    cfg = current_app.skytrack_config
    out_dir = _backup_dir()
    name = f'skytrack-backup-{int(time.time())}.tar.gz'
    out_path = out_dir / name
    targets = []
    for p in (cfg.get('auth_path'), cfg.get('db_path'), cfg.get('device_id_path')):
        if p and os.path.exists(p):
            targets.append(p)
    yaml_path = os.environ.get('SKYTRACK_CONFIG',
                               os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            '..', 'config.yaml'))
    if os.path.exists(yaml_path):
        targets.append(yaml_path)
    if not targets:
        return _err('no backup sources found', 500)
    try:
        with tarfile.open(out_path, 'w:gz') as tf:
            for t in targets:
                tf.add(t, arcname=os.path.basename(t))
    except Exception as e:
        return _err(f'backup failed: {e}', 500)
    logs_svc.log_portal('admin', 'backup_create', {'name': name, 'size': out_path.stat().st_size})
    return _ok({'backup': {
        'name': name,
        'size': out_path.stat().st_size,
        'created_at': datetime.utcnow().isoformat() + 'Z',
    }})


@settings_bp.route('/api/settings/updates/backup/restore', methods=['POST'])
@ADMIN
def api_backup_restore():
    payload = request.get_json(silent=True) or {}
    name = (payload.get('name') or '').strip()
    if not name or '/' in name or '..' in name:
        return _err('invalid backup name')
    p = _backup_dir() / name
    if not p.exists():
        return _err('backup not found', 404)

    cfg = current_app.skytrack_config
    restore_targets = {
        'config.yaml': os.environ.get(
            'SKYTRACK_CONFIG',
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.yaml'),
        ),
        'auth.json': cfg.get('auth_path', '/etc/skytrack/auth.json'),
    }
    if cfg.get('db_path'):
        restore_targets[os.path.basename(cfg['db_path'])] = cfg['db_path']
    if cfg.get('device_id_path'):
        restore_targets[os.path.basename(cfg['device_id_path'])] = cfg['device_id_path']

    restored = []
    try:
        with tarfile.open(p, 'r:gz') as tf:
            for member in tf.getmembers():
                basename = os.path.basename(member.name)
                if basename in restore_targets:
                    dest = restore_targets[basename]
                    dest_dir = os.path.dirname(os.path.abspath(dest))
                    os.makedirs(dest_dir, exist_ok=True)
                    bak = dest + '.pre-restore'
                    if os.path.exists(dest):
                        shutil.copy2(dest, bak)
                    with tf.extractfile(member) as src:
                        if src:
                            with open(dest, 'wb') as dst:
                                dst.write(src.read())
                            restored.append(basename)
    except Exception as e:
        return _err(f'restore failed: {e}', 500)

    logs_svc.log_portal('admin', 'backup_restore', {
        'name': name, 'restored': restored,
    })
    return _ok({
        'restored': restored,
        'message': f'Restored {len(restored)} file(s). Restart the app to apply.',
    })


# ===========================================================================
# 10. DIAGNOSTICS
# ===========================================================================

_VALID_LOG_LEVELS = {'DEBUG', 'INFO', 'WARNING', 'ERROR'}


@settings_bp.route('/api/settings/diagnostics', methods=['GET'])
@ADMIN
def api_diagnostics():
    cfg = current_app.skytrack_config
    services = {}
    for unit in ('skytrack-app', 'skytrack-network', 'skytrack-hotspot',
                 'skytrack-ingest', 'skytrack-display', 'hostapd', 'dnsmasq'):
        services[unit] = _systemd_active(unit)
    storage = {}
    try:
        st = shutil.disk_usage(cfg.get('data_dir', '/var/lib/skytrack'))
        storage = {
            'total': st.total, 'used': st.used, 'free': st.free,
            'percent': round(100 * st.used / st.total, 1),
        }
    except Exception as e:
        storage = {'error': str(e)}
    return jsonify({
        'config': {
            'log_level': cfg.get('log_level'),
            'dht_pin': cfg.get('dht_pin'),
            'sensor_interval': cfg.get('sensor_interval'),
        },
        'services': services,
        'storage': storage,
        'sensor': current_app.sensor_svc.read(),
    })


@settings_bp.route('/api/settings/diagnostics/log-level', methods=['POST'])
@ADMIN
def api_log_level():
    payload = request.get_json(silent=True) or {}
    level = (payload.get('log_level') or '').upper().strip()
    if level not in _VALID_LOG_LEVELS:
        return _err('log_level must be DEBUG/INFO/WARNING/ERROR')
    cfg = current_app.skytrack_config
    cfg['log_level'] = level
    _persist({'log_level': level})
    logging.getLogger().setLevel(getattr(logging, level))
    logs_svc.log_portal('admin', 'log_level_change', {'level': level})
    return _ok({'log_level': level})


@settings_bp.route('/api/settings/diagnostics/selfcheck', methods=['POST'])
@ADMIN
def api_selfcheck():
    cfg = current_app.skytrack_config
    import db as _db
    import hotspot as _hs
    checks = {
        'db': {'ok': _db.current_version() >= 1, 'message': f'schema v{_db.current_version()}'},
        'device_id': {'ok': bool(current_app.config.get('DEVICE_ID')),
                      'message': current_app.config.get('DEVICE_ID', '')},
        'sensors': current_app.sensor_svc.selfcheck(),
        'weather': current_app.weather_svc.selfcheck(),
        'health': current_app.health_svc.selfcheck(),
        'ingest': current_app.ingest.selfcheck(),
        'hotspot': _hs.selfcheck(cfg),
        'aeroapi': enrich.test_aeroapi(),
    }
    overall = all(c.get('ok') for c in checks.values())
    return jsonify({'ok': overall, 'checks': checks})


@settings_bp.route('/api/settings/diagnostics/restart-app', methods=['POST'])
@ADMIN
def api_restart_app():
    """Restart the SkyTrack app via systemd, returning verified status.

    `--no-block` is critical here: restarting our own unit would otherwise
    SIGTERM us mid-response, the HTTP socket would die, and the operator
    would see a generic "fetch failed" toast even though the restart
    actually worked. With --no-block, systemd just queues the job and we
    flush the response cleanly before the restart fires.
    """
    logs_svc.log_portal('admin', 'restart_app_requested', {})
    ok, msg = _shell(['systemctl', '--no-block', 'restart', 'skytrack-app'], timeout=10)
    if ok:
        return jsonify({'ok': True, 'message': 'App restart queued'})
    return jsonify({
        'ok': False,
        'message': (msg or 'restart failed').strip()[:300],
    }), 500


@settings_bp.route('/api/settings/diagnostics/restart-network', methods=['POST'])
@ADMIN
def api_restart_network():
    """Restart the network helper unit via systemd, returning verified status."""
    logs_svc.log_portal('admin', 'restart_network_requested', {})
    ok, msg = _shell(['systemctl', '--no-block', 'restart', 'skytrack-network'], timeout=10)
    if ok:
        return jsonify({'ok': True, 'message': 'Network restart queued'})
    return jsonify({
        'ok': False,
        'message': (msg or 'restart failed').strip()[:300],
    }), 500


def _power_action(verb: str, label: str):
    """Run a login1 power action via systemctl --no-block and verify it
    was accepted by the OS before claiming success to the UI.

    `--no-block` returns the moment the request lands in dbus, which is
    long before the actual reboot fires, so the HTTP response always
    flushes. If polkit rejects the call we get a non-zero exit AND a
    visible error string we can surface in a toast — no more silent
    "rebooting…" ghost.

    Falls back to `shutdown -r +1 / -h +1` only if the systemctl call
    itself failed for an unrelated reason (e.g. dbus down). Both verbs
    are explicitly granted to the skytrack user in
    config_templates/skytrack.polkit.rules.
    """
    logs_svc.log_portal('admin', f'{verb}_requested', {})
    cmd_systemctl = ['systemctl', '--no-block', verb]
    ok, msg = _shell(cmd_systemctl, timeout=10)
    if ok:
        return jsonify({'ok': True, 'message': f'{label} queued — system going down'})
    fallback = ['shutdown', '-r' if verb == 'reboot' else '-h', '+1']
    ok2, msg2 = _shell(fallback, timeout=10)
    if ok2:
        return jsonify({
            'ok': True,
            'message': f'{label} scheduled in 1 minute (systemctl path returned: {msg.strip()[:200]})',
        })
    detail = (msg or msg2 or '').strip()[:300] or 'unknown failure'
    return jsonify({
        'ok': False,
        'message': f'{label} rejected by the OS: {detail}',
    }), 500


@settings_bp.route('/api/settings/diagnostics/reboot', methods=['POST'])
@ADMIN
def api_reboot():
    """Immediate reboot via systemd-logind, with verified queueing.

    Uses `systemctl --no-block reboot`, which talks to logind through
    dbus and returns success only if the polkit grant for
    `org.freedesktop.login1.reboot` resolves. The grant for the
    `skytrack` service user is installed by install.sh (see
    config_templates/skytrack.polkit.rules).
    """
    return _power_action('reboot', 'Reboot')


@settings_bp.route('/api/settings/diagnostics/shutdown', methods=['POST'])
@ADMIN
def api_shutdown():
    """Immediate power-off — same verified path as api_reboot."""
    return _power_action('poweroff', 'Shutdown')


# ===========================================================================
# 11. RADAR  (display-only in phase 1)
# ===========================================================================

@settings_bp.route('/api/settings/radar', methods=['GET', 'POST'])
@ADMIN
def api_radar():
    cfg = current_app.skytrack_config
    identity = current_app.config.get('DEVICE_RECORD', {})
    if request.method == 'GET':
        return jsonify({
            'radar_publish_enabled': bool(cfg.get('radar_publish_enabled', False)),
            'radar_url': device_id.radar_url(identity) if identity else '',
        })
    payload = request.get_json(silent=True) or {}
    if 'radar_publish_enabled' in payload:
        cfg['radar_publish_enabled'] = bool(payload['radar_publish_enabled'])
        _persist({'radar_publish_enabled': cfg['radar_publish_enabled']})
    return _ok()


# ===========================================================================
# 12. BRIDGE ENDPOINTS — match the touch-shell settings.js client
#
# The UI client calls cleaner, more RESTful paths (e.g. /wifi/forget,
# /data/vacuum, /diagnostics/system).  These handlers either wrap the
# existing implementations above or provide best-effort stubs that fail
# gracefully on dev boxes without nmcli / systemctl / psutil.
# ===========================================================================

# ---------- Network ---------------------------------------------------------

@settings_bp.route('/api/settings/network/status', methods=['GET'])
@ADMIN
def api_network_status():
    """Touch-shell status tiles for /settings → Network.

    Reads network_svc.get_network_status() and translates the service-level
    keys into the small {state, detail, ...} shape the settings.js tiles
    expect. Also surfaces which interface is currently carrying the default
    route so the UI can mark it as the active WAN.
    """
    cfg = current_app.skytrack_config
    try:
        base = network_svc.get_network_status(cfg) or {}
    except Exception as e:
        logger.warning('network status read failed: %s', e)
        base = {}

    cellular = base.get('cellular') or {}
    wifi     = base.get('wifi')     or {}
    hs       = base.get('hotspot')  or {}
    online   = bool(base.get('online') or base.get('internet'))
    primary  = base.get('primary') or 'none'
    primary_iface = base.get('primary_interface') or ''

    # --- Cellular tile ----------------------------------------------------
    cell_up = bool(cellular.get('detected')) and (
        'connected' in (cellular.get('state') or '').lower()
        or (cellular.get('signal_pct') or 0) > 0
    )
    cell_detail_bits = []
    if cellular.get('carrier'):
        cell_detail_bits.append(cellular['carrier'])
    if cellular.get('access_tech'):
        cell_detail_bits.append(cellular['access_tech'])
    if cellular.get('signal_pct'):
        cell_detail_bits.append(f"{cellular['signal_pct']}%")
    if not cell_up and cellular.get('detected'):
        cell_detail_bits.append(cellular.get('state') or 'detected, not connected')
    if not cellular.get('detected'):
        cell_detail_bits.append('no modem')

    # --- WiFi client tile -------------------------------------------------
    wifi_up = bool(wifi.get('connected'))
    if hs.get('enabled'):
        wifi_detail = 'disabled — hotspot is using wlan0'
    elif wifi_up:
        wifi_detail = wifi.get('ssid') or 'connected'
    else:
        wifi_detail = 'not connected'

    # --- Hotspot tile -----------------------------------------------------
    hs_up   = bool(hs.get('enabled'))
    clients = hs.get('clients') or []
    if hs_up:
        hs_detail = f"{len(clients)} client" + ('' if len(clients) == 1 else 's')
    else:
        hs_detail = 'off'

    # --- Internet tile ----------------------------------------------------
    if online:
        inet_detail = f"via {primary_iface}" if primary_iface else 'reachable'
    else:
        inet_detail = 'no route'

    return jsonify({
        'cellular': {
            'state':       'on' if cell_up else 'off',
            'detail':      ' · '.join(cell_detail_bits) or '—',
            'active':      primary == 'cellular',
            'apn':         cellular.get('apn') or '',
            'apn_source':  cellular.get('apn_source') or '',
            'carrier':     cellular.get('carrier') or '',
            'signal_pct':  int(cellular.get('signal_pct') or 0),
            'access_tech': cellular.get('access_tech') or '',
            'detected':    bool(cellular.get('detected')),
        },
        'wifi': {
            'state':      'on' if wifi_up else 'off',
            'ssid':       wifi.get('ssid') or '',
            'detail':     wifi_detail,
            'active':     primary == 'wifi' and not hs_up,
            'signal_pct': int(wifi.get('signal_pct') or 0),
        },
        'hotspot': {
            'state':   'on' if hs_up else 'off',
            'ssid':    hs.get('ssid') or cfg.get('hotspot_ssid') or '',
            'clients': len(clients),
            'detail':  hs_detail,
        },
        'internet': {
            'state':  'online' if online else 'offline',
            'detail': inet_detail,
        },
        'primary':            primary,
        'primary_interface':  primary_iface,
    })


@settings_bp.route('/api/settings/network/wifi/saved', methods=['GET'])
@ADMIN
def api_wifi_saved():
    cfg = current_app.skytrack_config
    saved = list(cfg.get('saved_wifi_networks') or [])
    saved_ssids = {n.get('ssid') for n in saved}

    # Merge in any NM-known connections not already in config (e.g. networks
    # joined via nmcli directly or before the saved-list feature existed).
    try:
        nm = network_svc.wifi_saved()
        for nm_net in (nm.get('networks') or []):
            ssid = nm_net.get('ssid')
            if ssid and ssid not in saved_ssids:
                saved.append({'ssid': ssid, 'has_password': True})
                saved_ssids.add(ssid)
    except Exception:
        pass

    connected_ssid = None
    try:
        st = network_svc.get_network_status(cfg) or {}
        connected_ssid = (st.get('wifi') or {}).get('ssid')
    except Exception:
        pass
    for n in saved:
        n['connected'] = (n.get('ssid') == connected_ssid)
    return jsonify({'networks': saved})


@settings_bp.route('/api/settings/network/hotspot/password', methods=['GET'])
@ADMIN
def api_hotspot_password_get():
    rec = auth_lib.read_auth() or {}
    return jsonify({'password': rec.get('hotspot_password') or ''})


@settings_bp.route('/api/settings/network/hotspot/regenerate', methods=['POST'])
@ADMIN
def api_hotspot_regenerate():
    """Regenerate a random WPA2 password and apply it atomically."""
    blocked = _hotspot_lockout_guard()
    if blocked:
        return blocked
    pw = auth_lib.set_hotspot_password(None)
    apply_result = hotspot.rotate_hotspot_apply()
    logs_svc.log_network('hotspot_password_regen',
                         {'len': len(pw or ''), 'apply_ok': apply_result.get('ok', False)})
    if not apply_result.get('ok', False):
        return _ok({'password': pw, 'apply': apply_result,
                    'warning': 'password stored but hostapd re-apply failed'})
    return _ok({'password': pw, 'apply': apply_result})


@settings_bp.route('/api/settings/network/hotspot/restart', methods=['POST'])
@ADMIN
def api_hotspot_restart():
    """Cheap bounce — does NOT pick up a new password (use regenerate for that)."""
    blocked = _hotspot_lockout_guard()
    if blocked:
        return blocked
    logs_svc.log_portal('admin', 'hotspot_restart', {})
    # Route through hotspot.restart_hotspot() so every caller uses the
    # same `hotspot_apply.sh --reapply` path. Keeps per-unit systemctl
    # out of the blueprint (where it never had the full picture —
    # hostapd can be "active" briefly while dnsmasq and the wlan0 IP
    # both need to come back too, and only the helper script knows
    # that).
    result = hotspot.restart_hotspot()
    return jsonify(result)


@settings_bp.route('/api/settings/network/wifi/add', methods=['POST'])
@ADMIN
def api_wifi_add_alias():
    return api_wifi_add()


@settings_bp.route('/api/settings/network/wifi/forget', methods=['POST'])
@ADMIN
def api_wifi_forget():
    return api_wifi_remove()


@settings_bp.route('/api/settings/network/wifi/connect', methods=['POST'])
@ADMIN
def api_wifi_connect():
    """Join a WiFi network via NetworkManager, falling back to the
    direct-tool path on boxes without nmcli. Delegates to
    `network_svc.wifi_connect`, which returns a structured
    `{ok, message, backend}` dict we pass straight through.
    """
    payload = request.get_json(silent=True) or {}
    ssid = (payload.get('ssid') or '').strip()
    password = payload.get('password') or None
    if not ssid:
        return _err('ssid required')
    result = network_svc.wifi_connect(ssid, password)
    logs_svc.log_network('wifi_connect', {
        'ssid': ssid, 'ok': bool(result.get('ok')),
        'backend': result.get('backend'),
    })
    # Persist a remembered entry so Settings → Saved lists it consistently
    # with the direct-config path. The PSK itself is stored in auth.json.
    if result.get('ok'):
        cfg = current_app.skytrack_config
        saved = [n for n in (cfg.get('saved_wifi_networks') or [])
                 if n.get('ssid') != ssid]
        saved.append({'ssid': ssid, 'has_password': bool(password)})
        cfg['saved_wifi_networks'] = saved
        _persist({'saved_wifi_networks': saved})
        if password:
            auth_lib.set_secret(f'wifi_psk_{ssid}', password)
    return jsonify(result)


@settings_bp.route('/api/settings/network/wifi/scan', methods=['GET', 'POST'])
@ADMIN
def api_wifi_scan():
    """Return a list of WiFi networks visible to the radio right now.

    Uses `nmcli device wifi list --rescan yes` via network_svc when
    NetworkManager is available, and falls back to `iwlist` otherwise.
    The list is sorted strongest-first with duplicate SSIDs collapsed so
    the UI can render it directly.
    """
    result = network_svc.wifi_scan()
    return jsonify(result)


@settings_bp.route('/api/settings/network/cellular/apn', methods=['POST'])
@ADMIN
def api_cellular_apn():
    """Set the APN on the active gsm connection and persist it into
    config.yaml. The backend re-activates the connection so the new APN
    takes effect on the live cellular link, not just the next modem cycle.

    Failure handling: if NetworkManager rejects the change (most often
    because the polkit grant is missing), we surface the actual nmcli
    stderr verbatim AND return a non-200 status so the JS client toasts
    a real error instead of a green checkmark on a no-op.
    """
    payload = request.get_json(silent=True) or {}
    apn = (payload.get('apn') or '').strip()
    if not apn:
        return _err('apn required')
    cfg = current_app.skytrack_config
    cfg['cellular_apn'] = apn
    _persist({'cellular_apn': apn})
    result = network_svc.set_cellular_apn(apn)
    logs_svc.log_network('cellular_apn_set', {
        'apn': apn,
        'ok': bool(result.get('ok')),
        'backend': result.get('backend'),
        'applied': bool(result.get('applied')),
    })
    payload_out = {
        'ok':         bool(result.get('ok')),
        'apn':        apn,
        'backend':    result.get('backend'),
        'connection': result.get('connection'),
        'applied':    bool(result.get('applied')),
        'message':    result.get('message') or 'APN updated',
    }
    if not result.get('ok'):
        return jsonify(payload_out), 500
    return jsonify(payload_out)


@settings_bp.route('/api/settings/network/speedtest', methods=['POST'])
@ADMIN
def api_speedtest():
    """Run a basic download/upload speed test against Cloudflare."""
    import urllib.request
    import urllib.error
    import socket

    results = {'ok': False, 'download_mbps': None, 'upload_mbps': None, 'ping_ms': None}

    # Detect active interface
    iface = None
    ip_addr = None
    try:
        r = subprocess.run(
            ['ip', 'route', 'get', '8.8.8.8'],
            capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            parts = r.stdout.split()
            for i, tok in enumerate(parts):
                if tok == 'dev' and i + 1 < len(parts):
                    iface = parts[i + 1]
                if tok == 'src' and i + 1 < len(parts):
                    ip_addr = parts[i + 1]
    except Exception:
        pass
    results['interface'] = iface
    results['ip'] = ip_addr

    # Ping (TCP connect latency to 1.1.1.1:443)
    try:
        t0 = time.time()
        s = socket.create_connection(('1.1.1.1', 443), timeout=5)
        ping_ms = (time.time() - t0) * 1000
        s.close()
        results['ping_ms'] = round(ping_ms, 1)
    except Exception:
        pass

    # Download test (~2 MB from Cloudflare)
    try:
        url = 'https://speed.cloudflare.com/__down?bytes=2000000'
        req = urllib.request.Request(url, headers={'User-Agent': 'SkyTrack-Speedtest/1.0'})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        elapsed = time.time() - t0
        if elapsed > 0:
            mbps = (len(data) * 8) / (elapsed * 1_000_000)
            results['download_mbps'] = round(mbps, 2)
            results['download_bytes'] = len(data)
            results['download_seconds'] = round(elapsed, 2)
    except Exception as e:
        results['download_error'] = str(e)

    # Upload test (~500 KB to Cloudflare)
    try:
        url = 'https://speed.cloudflare.com/__up'
        payload = b'\x00' * 500_000
        req = urllib.request.Request(
            url, data=payload, method='POST',
            headers={'User-Agent': 'SkyTrack-Speedtest/1.0', 'Content-Type': 'application/octet-stream'},
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        elapsed = time.time() - t0
        if elapsed > 0:
            mbps = (len(payload) * 8) / (elapsed * 1_000_000)
            results['upload_mbps'] = round(mbps, 2)
            results['upload_bytes'] = len(payload)
            results['upload_seconds'] = round(elapsed, 2)
    except Exception as e:
        results['upload_error'] = str(e)

    results['ok'] = results['download_mbps'] is not None
    logs_svc.log_network('speedtest', {
        'download_mbps': results.get('download_mbps'),
        'upload_mbps': results.get('upload_mbps'),
        'ping_ms': results.get('ping_ms'),
        'interface': iface,
    })

    # Persist to SQLite (keep last 10)
    try:
        import db as _db
        conn = _db.get_conn()
        conn.execute('''CREATE TABLE IF NOT EXISTS speed_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            interface TEXT,
            ip TEXT,
            download_mbps REAL,
            upload_mbps REAL,
            ping_ms REAL
        )''')
        conn.execute(
            'INSERT INTO speed_tests (interface, ip, download_mbps, upload_mbps, ping_ms) VALUES (?,?,?,?,?)',
            (iface, ip_addr, results.get('download_mbps'), results.get('upload_mbps'), results.get('ping_ms')))
        conn.execute('''DELETE FROM speed_tests WHERE id NOT IN (
            SELECT id FROM speed_tests ORDER BY id DESC LIMIT 10)''')
        conn.commit()
    except Exception as e:
        logger.warning('speed test db write failed: %s', e)

    return jsonify(results)


@settings_bp.route('/api/settings/network/speedtest/history', methods=['GET'])
@ADMIN
def api_speedtest_history():
    """Return the last 10 speed test results."""
    try:
        import db as _db
        conn = _db.get_conn()
        conn.execute('''CREATE TABLE IF NOT EXISTS speed_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            interface TEXT, ip TEXT,
            download_mbps REAL, upload_mbps REAL, ping_ms REAL
        )''')
        rows = conn.execute(
            'SELECT ts, interface, ip, download_mbps, upload_mbps, ping_ms FROM speed_tests ORDER BY id DESC LIMIT 10'
        ).fetchall()
        return jsonify({'ok': True, 'results': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'results': []})


@settings_bp.route('/api/settings/network/backend', methods=['GET'])
@ADMIN
def api_network_backend():
    """Report which network backend is live (nm / direct) and what it
    can actually do. Settings → Network uses this to hide buttons whose
    underlying tool is missing instead of letting them 500 on click.
    """
    return jsonify(network_svc.backend_info())


@settings_bp.route('/api/settings/updates/git_auth', methods=['GET'])
@ADMIN
def api_updates_git_auth():
    """Return a display-safe summary of the current OTA git auth mode.

    This is what Settings → Updates shows so operators can see at a
    glance whether they're on deploy keys (ssh) or a token, and what
    remote URL we'd hand to git. Token is NOT included in the response.
    """
    return jsonify(git_auth.describe(current_app.skytrack_config))


# ---------- Feeders ---------------------------------------------------------

@settings_bp.route('/api/settings/feeders/status', methods=['GET'])
@ADMIN
def api_feeders_status():
    """Honest 3-state tiles for Settings → Feeders.

    Separates "not installed" (wasn't selected at install time) from
    "installed but stopped" from "running". The UI uses this to avoid
    shaming users for something they opted out of.
    """
    def tile(unit):
        p = _systemd_presence(unit)
        if not p['installed']:
            return {
                'state':     'missing',
                'installed': False,
                'active':    False,
                'detail':    'not installed',
            }
        if p['active']:
            return {
                'state':     'on',
                'installed': True,
                'active':    True,
                'detail':    f'{unit} running',
            }
        return {
            'state':     'off',
            'installed': True,
            'active':    False,
            'detail':    p['state'] or 'stopped',
        }
    return jsonify({
        'dump1090': tile('dump1090-fa'),
        'fr24':     tile('fr24feed'),
        'piaware':  tile('piaware'),
    })


@settings_bp.route('/api/settings/feeders/dump1090/restart', methods=['POST'])
@ADMIN
def api_dump1090_restart():
    logs_svc.log_portal('admin', 'dump1090_restart', {})
    ok, msg = _shell(['systemctl', 'restart', 'dump1090-fa'], timeout=15)
    return jsonify({'ok': ok, 'message': msg or 'restart issued'})


_INSTALL_WHITELIST = {'dump1090', 'fr24feed', 'piaware'}
_install_jobs = {}  # job_id -> dict
_install_lock = threading.Lock()


def _run_installer(package):
    """Run a whitelisted package install (blocking — call from worker thread).

    Prefers the deployed wrapper at /usr/local/bin/skytrack-installer.
    Falls back to running the bundled installer script directly when the
    wrapper hasn't been deployed yet (first deploy before install.sh).
    """
    if package not in _INSTALL_WHITELIST:
        return False, f'Package {package!r} is not in the install whitelist.'

    wrapper = '/usr/local/bin/skytrack-installer'
    if os.path.isfile(wrapper):
        logger.info('Running installer wrapper: %s %s', wrapper, package)
        ok, output = _shell(['sudo', wrapper, package], timeout=900)
        logger.info('Installer wrapper returned ok=%s, len=%d', ok, len(output or ''))
        return ok, output

    script = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          'installers', f'{package}.sh')
    if not os.path.isfile(script):
        logger.warning('No installer script found: %s', script)
        return False, f'No installer script found for {package!r}.'
    logger.info('Running installer script directly: %s', script)
    ok, output = _shell(['sudo', 'bash', script], timeout=900)
    logger.info('Installer script returned ok=%s, len=%d', ok, len(output or ''))
    return ok, output


def _install_worker(job_id, packages):
    """Background worker — runs installers sequentially, updates job dict."""
    job = _install_jobs[job_id]
    all_ok = True
    for pkg in packages:
        job['current'] = pkg
        logger.info('Install job %s: starting %s', job_id, pkg)
        try:
            ok, out = _run_installer(pkg)
            job['output'] += (out or '') + '\n'
            if not ok:
                all_ok = False
                logger.warning('Install job %s: %s failed', job_id, pkg)
        except Exception as e:
            logger.exception('Install job %s: %s crashed', job_id, pkg)
            job['output'] += f'\n{pkg} crashed: {e}\n'
            all_ok = False
    job['ok'] = all_ok
    job['status'] = 'done' if all_ok else 'failed'
    job['finished'] = time.time()
    job['current'] = None
    logger.info('Install job %s finished: ok=%s', job_id, all_ok)


def _start_install_job(label, packages):
    """Start an async install job. Returns (job_id, is_new)."""
    with _install_lock:
        for jid, job in _install_jobs.items():
            if job.get('label') == label and job['status'] == 'running':
                return jid, False
        job_id = uuid.uuid4().hex[:8]
        _install_jobs[job_id] = {
            'label': label,
            'status': 'running',
            'output': '',
            'ok': None,
            'current': None,
            'started': time.time(),
            'finished': None,
        }
    threading.Thread(
        target=_install_worker, args=(job_id, packages),
        name=f'install-{label}', daemon=True,
    ).start()
    return job_id, True


@settings_bp.route('/api/settings/feeders/dump1090/install', methods=['POST'])
@ADMIN
def api_dump1090_install():
    """Start async ADS-B stack install (dump1090-fa + piaware)."""
    logger.info('ADS-B install endpoint called')
    logs_svc.log_portal('admin', 'adsb_install_requested', {})
    job_id, is_new = _start_install_job('adsb', ['dump1090', 'piaware'])
    if not is_new:
        logger.info('ADS-B install already running: %s', job_id)
    return jsonify({'job_id': job_id, 'status': 'running'})


@settings_bp.route('/api/settings/feeders/fr24/install', methods=['POST'])
@ADMIN
def api_fr24_install():
    """Start async FR24 feeder install."""
    logs_svc.log_portal('admin', 'fr24_install_requested', {})
    job_id, is_new = _start_install_job('fr24', ['fr24feed'])
    if not is_new:
        logger.info('FR24 install already running: %s', job_id)
    return jsonify({'job_id': job_id, 'status': 'running'})


@settings_bp.route('/api/settings/install/status/<job_id>', methods=['GET'])
@ADMIN
def api_install_status(job_id):
    """Poll install job progress."""
    job = _install_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Unknown job', 'status': 'unknown'}), 404
    elapsed = int(time.time() - job['started'])
    return jsonify({
        'job_id': job_id,
        'status': job['status'],
        'output': job['output'],
        'ok': job['ok'],
        'current': job['current'],
        'elapsed': elapsed,
    })


@settings_bp.route('/api/settings/software/install', methods=['POST'])
@ADMIN
def api_software_install():
    """Generic package install via the secure installer framework."""
    package = (request.get_json(silent=True) or {}).get('package', '').strip()
    if not package:
        return _err('No package specified', 400)
    if package not in _INSTALL_WHITELIST:
        return _err(f'Package {package!r} not allowed', 400)
    logs_svc.log_portal('admin', 'software_install_requested', {'package': package})
    job_id, _ = _start_install_job(package, [package])
    return jsonify({'job_id': job_id, 'status': 'running'})


# ---------- Data ------------------------------------------------------------

@settings_bp.route('/api/settings/data/usage', methods=['GET'])
@ADMIN
def api_data_usage():
    cfg = current_app.skytrack_config
    db_path = cfg.get('db_path') or ''
    out = {'db_path': db_path}
    try:
        out['db_size'] = os.path.getsize(db_path) if db_path and os.path.exists(db_path) else 0
    except Exception:
        out['db_size'] = 0
    try:
        st = shutil.disk_usage(cfg.get('data_dir') or '/var/lib/skytrack')
        out['disk_total'] = st.total
        out['disk_used']  = st.used
        out['disk_free']  = st.free
    except Exception:
        pass
    return jsonify(out)


def _with_db(fn):
    try:
        import db as _db
        return fn(_db)
    except Exception as e:
        return _err(f'db error: {e}', 500)


@settings_bp.route('/api/settings/data/vacuum', methods=['POST'])
@ADMIN
def api_data_vacuum():
    def run(_db):
        conn = _db.get_conn()
        conn.execute('VACUUM')
        conn.commit()
        logs_svc.log_portal('admin', 'db_vacuum', {})
        return _ok({'message': 'vacuum complete'})
    return _with_db(run)


@settings_bp.route('/api/settings/data/prune', methods=['POST'])
@ADMIN
def api_data_prune():
    cfg = current_app.skytrack_config
    try:
        days = int(cfg.get('data_retention_days') or cfg.get('sightings_retention_days') or 30)
    except (TypeError, ValueError):
        days = 30
    def run(_db):
        conn = _db.get_conn()
        removed = 0
        for tbl, col in (('sightings','ts'), ('enrichments','fetched_at'), ('api_usage','ts'), ('auth_attempts','ts')):
            try:
                cur = conn.execute(
                    f"DELETE FROM {tbl} WHERE {col} < datetime('now', ?)",
                    (f'-{days} days',),
                )
                removed += cur.rowcount or 0
            except Exception:
                pass
        conn.commit()
        logs_svc.log_portal('admin', 'db_prune', {'days': days, 'removed': removed})
        return _ok({'message': f'pruned {removed} rows older than {days}d', 'removed': removed})
    return _with_db(run)


@settings_bp.route('/api/settings/data/wipe', methods=['POST'])
@ADMIN
def api_data_wipe():
    def run(_db):
        conn = _db.get_conn()
        wiped = []
        for tbl in ('sightings', 'enrichments', 'api_usage', 'auth_attempts', 'aircraft_log'):
            try:
                conn.execute(f'DELETE FROM {tbl}')
                wiped.append(tbl)
            except Exception:
                pass
        conn.commit()
        logs_svc.log_portal('admin', 'db_wipe', {'tables': wiped})
        return _ok({'message': f'wiped {len(wiped)} tables', 'tables': wiped})
    return _with_db(run)


# ---------- Alerts / watchlist ---------------------------------------------

def _watchlist() -> list:
    return list(current_app.skytrack_config.get('watchlist') or [])


@settings_bp.route('/api/settings/alerts/watchlist', methods=['GET'])
@ADMIN
def api_watchlist_list():
    return jsonify({'items': _watchlist()})


@settings_bp.route('/api/settings/alerts/watchlist/add', methods=['POST'])
@ADMIN
def api_watchlist_add():
    payload = request.get_json(silent=True) or {}
    ident = (payload.get('ident') or '').strip().upper()
    note  = (payload.get('note')  or '').strip()
    if not ident:
        return _err('ident required')
    cfg = current_app.skytrack_config
    items = [i for i in _watchlist() if i.get('ident') != ident]
    items.append({'ident': ident, 'note': note})
    cfg['watchlist'] = items
    _persist({'watchlist': items})
    logs_svc.log_portal('admin', 'watchlist_add', {'ident': ident})
    return _ok({'items': items})


@settings_bp.route('/api/settings/alerts/watchlist/remove', methods=['POST'])
@ADMIN
def api_watchlist_remove():
    payload = request.get_json(silent=True) or {}
    ident = (payload.get('ident') or '').strip().upper()
    cfg = current_app.skytrack_config
    items = [i for i in _watchlist() if i.get('ident') != ident]
    cfg['watchlist'] = items
    _persist({'watchlist': items})
    logs_svc.log_portal('admin', 'watchlist_remove', {'ident': ident})
    return _ok({'items': items})


# ---------- Updates: backups + power aliases -------------------------------

@settings_bp.route('/api/settings/updates/backups', methods=['GET'])
@ADMIN
def api_updates_backups():
    items = _list_backups()
    for b in items:
        b.setdefault('created', b.pop('created_at', ''))
    return jsonify({'backups': items})


@settings_bp.route('/api/settings/updates/backup', methods=['POST'])
@ADMIN
def api_updates_backup_alias():
    return api_backup_create()


@settings_bp.route('/api/settings/updates/backup/delete', methods=['POST'])
@ADMIN
def api_backup_delete():
    payload = request.get_json(silent=True) or {}
    name = (payload.get('name') or '').strip()
    if not name or '/' in name or '..' in name:
        return _err('invalid backup name')
    path = _backup_dir() / name
    if not path.exists():
        return _err('not found', 404)
    try:
        path.unlink()
    except Exception as e:
        return _err(f'delete failed: {e}', 500)
    logs_svc.log_portal('admin', 'backup_delete', {'name': name})
    return _ok()


@settings_bp.route('/api/settings/updates/restart-app', methods=['POST'])
@ADMIN
def api_updates_restart_app():
    return api_restart_app()


@settings_bp.route('/api/settings/updates/restart-network', methods=['POST'])
@ADMIN
def api_updates_restart_network():
    return api_restart_network()


@settings_bp.route('/api/settings/updates/reboot', methods=['POST'])
@ADMIN
def api_updates_reboot():
    return api_reboot()


@settings_bp.route('/api/settings/updates/shutdown', methods=['POST'])
@ADMIN
def api_updates_shutdown():
    return api_shutdown()


# ---------- System status banner ---------------------------------------------

@settings_bp.route('/api/settings/system-status', methods=['GET'])
@ADMIN
def api_system_status():
    """Priority-based health summary for the General page status banner."""
    checks = []

    # CPU temperature
    cpu_temp = None
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            cpu_temp = int(f.read().strip()) / 1000.0
    except Exception:
        pass
    if cpu_temp is not None:
        if cpu_temp > 80:
            checks.append({'name': 'CPU temp', 'level': 'red',
                           'detail': f'{cpu_temp:.0f}°C — critical'})
        elif cpu_temp > 70:
            checks.append({'name': 'CPU temp', 'level': 'amber',
                           'detail': f'{cpu_temp:.0f}°C — warm'})
        else:
            checks.append({'name': 'CPU temp', 'level': 'green',
                           'detail': f'{cpu_temp:.0f}°C'})

    # Disk usage
    try:
        st = os.statvfs('/opt/skytrack')
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used_pct = round(100 * (1 - free / total), 1) if total else 0
        if used_pct > 95:
            checks.append({'name': 'Disk', 'level': 'red',
                           'detail': f'{used_pct}% used'})
        elif used_pct > 85:
            checks.append({'name': 'Disk', 'level': 'amber',
                           'detail': f'{used_pct}% used'})
        else:
            checks.append({'name': 'Disk', 'level': 'green',
                           'detail': f'{used_pct}% used'})
    except Exception:
        checks.append({'name': 'Disk', 'level': 'amber', 'detail': 'unknown'})

    # Memory
    try:
        info = {}
        with open('/proc/meminfo') as f:
            for line in f:
                k, _, v = line.partition(':')
                info[k.strip()] = int(v.strip().split()[0]) * 1024
        total = info.get('MemTotal', 0)
        avail = info.get('MemAvailable', info.get('MemFree', 0))
        used_pct = round(100 * (total - avail) / total, 1) if total else 0
        if used_pct > 90:
            checks.append({'name': 'Memory', 'level': 'red',
                           'detail': f'{used_pct}% used'})
        elif used_pct > 80:
            checks.append({'name': 'Memory', 'level': 'amber',
                           'detail': f'{used_pct}% used'})
        else:
            checks.append({'name': 'Memory', 'level': 'green',
                           'detail': f'{used_pct}% used'})
    except Exception:
        checks.append({'name': 'Memory', 'level': 'amber', 'detail': 'unknown'})

    # Internet
    try:
        r = subprocess.run(
            ['ip', 'route', 'get', '8.8.8.8'],
            capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            checks.append({'name': 'Internet', 'level': 'green',
                           'detail': 'reachable'})
        else:
            checks.append({'name': 'Internet', 'level': 'red',
                           'detail': 'no route'})
    except Exception:
        checks.append({'name': 'Internet', 'level': 'red',
                       'detail': 'check failed'})

    # dump1090
    dump_json = Path('/run/dump1090-fa/aircraft.json')
    if dump_json.exists():
        checks.append({'name': 'dump1090', 'level': 'green',
                       'detail': 'running'})
    elif Path('/usr/bin/dump1090-fa').exists():
        checks.append({'name': 'dump1090', 'level': 'amber',
                       'detail': 'installed but no data'})
    else:
        checks.append({'name': 'dump1090', 'level': 'amber',
                       'detail': 'not installed'})

    # GPS
    with current_app.gps_state_lock:
        gps = dict(current_app.gps_state)
    gps_state = gps.get('state', 'no_fix')
    if gps_state == 'fix_acquired':
        checks.append({'name': 'GPS', 'level': 'green', 'detail': 'fix acquired'})
    elif current_app.skytrack_config.get('location_source') == 'manual':
        checks.append({'name': 'GPS', 'level': 'green', 'detail': 'manual location set'})
    else:
        checks.append({'name': 'GPS', 'level': 'amber', 'detail': 'no fix'})

    # Overall level: worst of all checks
    levels = [c['level'] for c in checks]
    if 'red' in levels:
        overall = 'red'
    elif 'amber' in levels:
        overall = 'amber'
    else:
        overall = 'green'

    return jsonify({'overall': overall, 'checks': checks})


# ---------- Diagnostics -----------------------------------------------------

@settings_bp.route('/api/settings/diagnostics/system', methods=['GET'])
@ADMIN
def api_diag_system():
    import platform
    out = {
        'hostname': platform.node(),
        'kernel':   platform.release(),
    }
    # CPU load via os.getloadavg
    try:
        load1, _, _ = os.getloadavg()
        out['cpu_load'] = round(load1, 2)
    except Exception:
        out['cpu_load'] = None
    out['cpu_count'] = os.cpu_count() or 1

    # Memory via /proc/meminfo (Linux-specific but cheap)
    try:
        info = {}
        with open('/proc/meminfo') as f:
            for line in f:
                k, _, v = line.partition(':')
                info[k.strip()] = int(v.strip().split()[0]) * 1024
        total = info.get('MemTotal', 0)
        avail = info.get('MemAvailable', info.get('MemFree', 0))
        out['mem_total'] = total
        out['mem_used']  = max(0, total - avail)
        out['mem_used_pct'] = round(100 * (total - avail) / total, 1) if total else None
    except Exception:
        out['mem_total'] = out['mem_used'] = None
        out['mem_used_pct'] = None

    # SoC temperature (Pi)
    out['cpu_temp_c'] = None
    for p in ('/sys/class/thermal/thermal_zone0/temp',):
        try:
            with open(p) as f:
                out['cpu_temp_c'] = int(f.read().strip()) / 1000.0
            break
        except Exception:
            pass

    # Uptime
    try:
        with open('/proc/uptime') as f:
            out['uptime'] = float(f.read().split()[0])
    except Exception:
        out['uptime'] = None

    return jsonify(out)


@settings_bp.route('/api/settings/diagnostics/services', methods=['GET'])
@ADMIN
def api_diag_services():
    units = ('skytrack-app', 'skytrack-ingest', 'skytrack-network',
             'skytrack-hotspot', 'skytrack-display',
             'hostapd', 'dnsmasq', 'dump1090-fa')
    items = []
    for unit in units:
        try:
            r = subprocess.run(['systemctl', 'is-active', unit],
                               capture_output=True, text=True, timeout=3)
            state = r.stdout.strip() or 'unknown'
        except Exception:
            state = 'unknown'
        items.append({'name': unit, 'state': state})
    return jsonify({'services': items})


@settings_bp.route('/api/settings/diagnostics/log-tail', methods=['GET'])
@ADMIN
def api_diag_log_tail():
    cfg = current_app.skytrack_config
    log_dir = cfg.get('log_dir') or '/var/log/skytrack'
    log_path = os.path.join(log_dir, 'skytrack.log')
    lines = []
    if os.path.exists(log_path):
        try:
            with open(log_path, 'rb') as f:
                try:
                    f.seek(-8192, os.SEEK_END)
                except OSError:
                    f.seek(0)
                data = f.read().decode('utf-8', errors='replace')
                lines = data.splitlines()[-80:]
        except Exception as e:
            lines = [f'(could not read log: {e})']
    else:
        # Fall back to journalctl if we have it.
        ok, out = _shell(['journalctl', '-u', 'skytrack-app', '-n', '80', '--no-pager'], timeout=5)
        if ok:
            lines = out.splitlines()
    return jsonify({'lines': lines})


@settings_bp.route('/api/settings/diagnostics/loglevel', methods=['POST'])
@ADMIN
def api_diag_loglevel_alias():
    return api_log_level()
