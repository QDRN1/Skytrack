"""Settings blueprint — touch-friendly v2 with 11 sections.

Sections (URL-friendly slugs):
  general        timezone, units, theme, default time filter
  display        rotation, brightness, sleep, animations, fullscreen
  access         admin password / PIN / session timeout / lockout
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

import logging
import os
import shutil
import subprocess
import tarfile
import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request

import auth as auth_lib
import device_id
import enrich
import logs_svc
import network_svc
from config import save_user_config

logger = logging.getLogger('skytrack.settings_bp')

settings_bp = Blueprint('settings', __name__)

ADMIN = auth_lib.login_required(auth_lib.ROLE_ADMIN)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@settings_bp.route('/settings')
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def index():
    cfg = current_app.skytrack_config
    identity = current_app.config.get('DEVICE_RECORD', {})
    aeroapi_usage = enrich.usage_summary('aeroapi')
    return render_template(
        'settings/index.html',
        identity=identity,
        radar_url=device_id.radar_url(identity) if identity else '',
        usage=aeroapi_usage,  # back-compat alias for legacy template
        usage_aeroapi=aeroapi_usage,
        usage_opensky=enrich.usage_summary('opensky'),
        hotspot_default_ssid=_default_hotspot_ssid(cfg, identity),
    )


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


def _shell(args, timeout=10):
    """Run a command and return (ok, message). Never raises."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
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
            'app_version': '2.0.0',
        })
    payload = request.get_json(silent=True) or {}
    updates = _take(payload, _GENERAL_KEYS)
    cfg.update(updates)
    _persist(updates)
    logs_svc.log_portal('admin', 'settings_general_update', updates)
    return _ok({'config': {k: cfg.get(k) for k in _GENERAL_KEYS}})


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
    logs_svc.log_portal('admin', 'settings_display_update', updates)
    return _ok({
        'config': {k: cfg.get(k) for k in _DISPLAY_KEYS},
        'note': 'Some display settings apply on next reboot.',
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
        'has_pin': auth_lib.has_pin(),
        'config': {k: cfg.get(k) for k in _ACCESS_KEYS},
    })


@settings_bp.route('/api/settings/access/admin', methods=['POST'])
@ADMIN
def api_change_admin_password():
    payload = request.get_json(silent=True) or {}
    try:
        auth_lib.change_admin_password(payload.get('password') or '')
    except ValueError as e:
        return _err(str(e))
    logs_svc.log_portal('admin', 'change_admin_password', {})
    return _ok()


@settings_bp.route('/api/settings/access/pin', methods=['POST'])
@ADMIN
def api_change_pin():
    """Set/clear the optional PIN. Empty string removes the PIN."""
    payload = request.get_json(silent=True) or {}
    try:
        auth_lib.change_admin_pin((payload.get('pin') or '').strip())
    except ValueError as e:
        return _err(str(e))
    logs_svc.log_portal('admin', 'change_pin', {'has_pin': auth_lib.has_pin()})
    return _ok({'has_pin': auth_lib.has_pin()})


@settings_bp.route('/api/settings/access/session', methods=['POST'])
@ADMIN
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
    'cellular_enabled', 'wifi_client_enabled', 'hotspot_auto_start',
    'time_sync_source', 'metered_connection',
)
_VALID_TIME_SOURCES = {'ntp', 'cellular', 'gps'}


@settings_bp.route('/api/settings/network', methods=['GET', 'POST'])
@ADMIN
def api_network():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        rec = auth_lib.read_auth()
        return jsonify({
            'config': {k: cfg.get(k) for k in _NETWORK_KEYS},
            'hotspot': {
                'ssid': cfg.get('hotspot_ssid'),
                'gateway': cfg.get('hotspot_gateway'),
                'subnet': cfg.get('hotspot_subnet'),
                'password': rec.get('hotspot_password'),
                'auto_start': cfg.get('hotspot_auto_start', True),
            },
            'status': network_svc.get_network_status(cfg),
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
    if 'hotspot_ssid' in payload:
        updates['hotspot_ssid'] = str(payload['hotspot_ssid']).strip() \
            or cfg.get('hotspot_ssid')
    cfg.update(updates)
    _persist(updates)
    logs_svc.log_portal('admin', 'settings_network_update', updates)
    return _ok({'config': {k: cfg.get(k) for k in _NETWORK_KEYS}})


@settings_bp.route('/api/settings/network/hotspot/password', methods=['POST'])
@ADMIN
def api_hotspot_password():
    """Regenerate or set the hotspot WPA2 password."""
    payload = request.get_json(silent=True) or {}
    new_pw = payload.get('password')
    pw = auth_lib.set_hotspot_password(new_pw or None)
    logs_svc.log_network('hotspot_password_set', {'len': len(pw)})
    return _ok({'password': pw})


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
# 5. INTEGRATIONS
# ===========================================================================

_INTEGRATION_FLAGS = (
    'aeroapi_enabled', 'aeroapi_calls_per_hour', 'aeroapi_calls_per_day',
    'aeroapi_cost_per_call', 'opensky_enabled', 'opensky_poll_minutes',
    'enrichment_ttl_hours', 'weather_provider',
)
_INTEGRATION_SECRETS = (
    'aeroapi_key', 'opensky_user', 'opensky_pass', 'weather_api_key',
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
        wx = current_app.weather_svc.fetch()
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


# ===========================================================================
# 7. DATA
# ===========================================================================

_DATA_KEYS = (
    'default_dashboard_time_filter', 'sightings_retention_days',
    'logs_retention_days', 'max_records', 'ignore_helicopters',
    'ignore_ground_targets', 'min_altitude_ft', 'signal_threshold_dbm',
)


@settings_bp.route('/api/settings/data', methods=['GET', 'POST'])
@ADMIN
def api_data():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({'config': {k: cfg.get(k) for k in _DATA_KEYS}})
    payload = request.get_json(silent=True) or {}
    updates = _take(payload, _DATA_KEYS)
    for k in ('ignore_helicopters', 'ignore_ground_targets'):
        if k in updates:
            updates[k] = bool(updates[k])
    for k in ('sightings_retention_days', 'logs_retention_days', 'max_records',
              'min_altitude_ft', 'signal_threshold_dbm'):
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
)


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
    for k in ('buzzer_enabled', 'alert_cell_disconnect', 'alert_wifi_disconnect'):
        if k in updates:
            updates[k] = bool(updates[k])
    for k in ('buzzer_volume', 'alert_api_budget_pct', 'alert_low_storage_pct'):
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
    for k in ('buzzer_threshold_temp_f', 'buzzer_threshold_hum'):
        if k in updates:
            try:
                updates[k] = float(updates[k])
            except (TypeError, ValueError):
                return _err(f'{k} must be a number')
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
    result = current_app.buzzer.test()
    logs_svc.log_portal('admin', 'buzzer_test', result)
    return jsonify(result)


# ===========================================================================
# 9. UPDATES & BACKUP
# ===========================================================================

_UPDATE_KEYS = (
    'update_check_enabled', 'update_allow_cellular', 'auto_backup_frequency',
    'ota_remote', 'ota_branch',
)
_VALID_BACKUP_FREQ = {'off', 'daily', 'weekly', 'monthly'}


def _backup_dir() -> Path:
    cfg = current_app.skytrack_config
    p = Path(cfg.get('backup_dir', '/var/lib/skytrack/backups'))
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


@settings_bp.route('/api/settings/updates', methods=['GET', 'POST'])
@ADMIN
def api_updates():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({
            'config': {k: cfg.get(k) for k in _UPDATE_KEYS},
            'app_version': '2.0.0',
            'backups': _list_backups(),
        })
    payload = request.get_json(silent=True) or {}
    updates = _take(payload, _UPDATE_KEYS)
    if 'auto_backup_frequency' in updates and \
            updates['auto_backup_frequency'] not in _VALID_BACKUP_FREQ:
        return _err('auto_backup_frequency must be off/daily/weekly/monthly')
    for k in ('update_check_enabled', 'update_allow_cellular'):
        if k in updates:
            updates[k] = bool(updates[k])
    cfg.update(updates)
    _persist(updates)
    logs_svc.log_portal('admin', 'settings_updates_update', updates)
    return _ok({'config': {k: cfg.get(k) for k in _UPDATE_KEYS}})


@settings_bp.route('/api/settings/updates/check', methods=['POST'])
@ADMIN
def api_updates_check():
    cfg = current_app.skytrack_config
    cwd = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + '/..')
    ok, msg = _shell(['git', 'fetch', cfg.get('ota_remote', 'origin')],
                     timeout=20)
    return jsonify({'ok': ok, 'message': msg[-400:]})


@settings_bp.route('/api/settings/updates/apply', methods=['POST'])
@ADMIN
def api_updates_apply():
    cfg = current_app.skytrack_config
    ok, msg = _shell(
        ['git', 'pull', cfg.get('ota_remote', 'origin'), cfg.get('ota_branch', 'main')],
        timeout=60,
    )
    logs_svc.log_portal('admin', 'ota_apply', {'ok': ok, 'tail': msg[-200:]})
    return jsonify({'ok': ok, 'message': msg[-400:]})


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
    logs_svc.log_portal('admin', 'backup_restore_requested', {'name': name})
    return _ok({
        'note': 'Restore must be performed manually via SSH for safety. '
                'Backup file is preserved at ' + str(p),
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
    logs_svc.log_portal('admin', 'restart_app_requested', {})
    ok, msg = _shell(['systemctl', 'restart', 'skytrack-app'], timeout=15)
    return jsonify({'ok': ok, 'message': msg or 'restart issued'})


@settings_bp.route('/api/settings/diagnostics/restart-network', methods=['POST'])
@ADMIN
def api_restart_network():
    logs_svc.log_portal('admin', 'restart_network_requested', {})
    ok, msg = _shell(['systemctl', 'restart', 'skytrack-network'], timeout=15)
    return jsonify({'ok': ok, 'message': msg or 'restart issued'})


@settings_bp.route('/api/settings/diagnostics/reboot', methods=['POST'])
@ADMIN
def api_reboot():
    logs_svc.log_portal('admin', 'reboot_requested', {})
    ok, msg = _shell(['/sbin/shutdown', '-r', '+1'], timeout=5)
    if not ok:
        ok, msg = _shell(['shutdown', '-r', '+1'], timeout=5)
    return jsonify({'ok': ok, 'message': msg or 'reboot scheduled in 1 minute'})


@settings_bp.route('/api/settings/diagnostics/shutdown', methods=['POST'])
@ADMIN
def api_shutdown():
    logs_svc.log_portal('admin', 'shutdown_requested', {})
    ok, msg = _shell(['/sbin/shutdown', '-h', '+1'], timeout=5)
    if not ok:
        ok, msg = _shell(['shutdown', '-h', '+1'], timeout=5)
    return jsonify({'ok': ok, 'message': msg or 'shutdown scheduled in 1 minute'})


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
