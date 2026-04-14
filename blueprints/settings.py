"""Settings blueprint — 9 tabs.

  General        device name, location, units, theme, tz
  Access         change PIN / admin pw, super-user link (admin)
  Network        WiFi/Cellular/Hotspot status (+ Restart Hotspot)   → see network_bp
  Integrations   AeroAPI / OpenSky / Weather keys + budget controls
  Feeders        FR24 / PiAware / Flightradar24 sharing IDs
  Radar          radar publishing (display-only in phase 1)
  Hardware       DHT22 pin, buzzer pin/volume/threshold + Test btn
  Updates        OTA git pull, version info
  Logs           system logs viewer                                 → see logs_bp

Network and Logs are split into their own blueprints per the v2 layout.
The settings page itself renders all 9 tabs via partials.

All routes here require admin (password OR PIN). The dashboard is
public; settings are not.
"""

import logging
import os

from flask import Blueprint, current_app, jsonify, render_template, request

import auth as auth_lib
import device_id
import enrich
import logs_svc
from config import save_user_config

logger = logging.getLogger('skytrack.settings_bp')

settings_bp = Blueprint('settings', __name__)


# ---------------------------------------------------------------------------
# Page (renders all 9 tabs as partials inside templates/settings/index.html)
# ---------------------------------------------------------------------------

@settings_bp.route('/settings')
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def index():
    cfg = current_app.skytrack_config
    return render_template(
        'settings/index.html',
        identity=current_app.config.get('DEVICE_RECORD', {}),
        radar_url=device_id.radar_url(current_app.config.get('DEVICE_RECORD', {})),
        usage=enrich.usage_summary('aeroapi'),
    )


# ---------------------------------------------------------------------------
# General tab
# ---------------------------------------------------------------------------

_GENERAL_KEYS = (
    'device_name_prefix', 'timezone', 'latitude', 'longitude', 'map_zoom',
    'units_temperature', 'units_speed', 'clock_format', 'default_theme',
)


@settings_bp.route('/api/settings/general', methods=['GET', 'POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_general():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({k: cfg.get(k) for k in _GENERAL_KEYS})

    payload = request.get_json(silent=True) or {}
    updates = {k: payload[k] for k in _GENERAL_KEYS if k in payload}
    cfg.update(updates)
    _persist(updates)
    logs_svc.log_portal(auth_lib.current_role() or 'admin', 'settings_general_update', updates)
    return jsonify({'ok': True, 'config': {k: cfg.get(k) for k in _GENERAL_KEYS}})


# ---------------------------------------------------------------------------
# Access tab — change PIN / admin password / hotspot password
# ---------------------------------------------------------------------------

@settings_bp.route('/api/settings/access/pin', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_change_pin():
    """Set/clear the optional PIN. Empty string removes the PIN."""
    payload = request.get_json(silent=True) or {}
    try:
        auth_lib.change_admin_pin((payload.get('pin') or '').strip())
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    logs_svc.log_portal('admin', 'change_pin', {'has_pin': auth_lib.has_pin()})
    return jsonify({'ok': True, 'has_pin': auth_lib.has_pin()})


@settings_bp.route('/api/settings/access/admin', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_change_admin_password():
    payload = request.get_json(silent=True) or {}
    try:
        auth_lib.change_admin_password(payload.get('password') or '')
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    logs_svc.log_portal('admin', 'change_admin_password', {})
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Integrations tab — AeroAPI / OpenSky / Weather keys + budget controls
# ---------------------------------------------------------------------------

_INTEGRATION_FLAGS = (
    'aeroapi_enabled', 'aeroapi_calls_per_hour', 'aeroapi_calls_per_day',
    'aeroapi_cost_per_call', 'opensky_enabled', 'opensky_poll_minutes',
    'enrichment_ttl_hours',
)

_INTEGRATION_SECRETS = (
    'aeroapi_key', 'opensky_user', 'opensky_pass', 'weather_api_key',
)


@settings_bp.route('/api/settings/integrations', methods=['GET', 'POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_integrations():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({
            'flags': {k: cfg.get(k) for k in _INTEGRATION_FLAGS},
            'secrets_set': {k: bool(auth_lib.get_secret(k)) for k in _INTEGRATION_SECRETS},
            'usage_aeroapi': enrich.usage_summary('aeroapi'),
            'usage_opensky': enrich.usage_summary('opensky'),
        })

    payload = request.get_json(silent=True) or {}
    flag_updates = {k: payload[k] for k in _INTEGRATION_FLAGS if k in payload}
    cfg.update(flag_updates)
    if flag_updates:
        _persist(flag_updates)

    secret_updates = payload.get('secrets') or {}
    for key in _INTEGRATION_SECRETS:
        if key in secret_updates:
            auth_lib.set_secret(key, secret_updates[key])

    logs_svc.log_portal(auth_lib.current_role() or 'admin', 'settings_integrations_update',
                        {'flags': list(flag_updates), 'secrets': list(secret_updates)})
    return jsonify({'ok': True})


@settings_bp.route('/api/settings/integrations/test/aeroapi', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_test_aeroapi():
    return jsonify(enrich.test_aeroapi())


@settings_bp.route('/api/settings/integrations/test/opensky', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_test_opensky():
    return jsonify(enrich.test_opensky())


# ---------------------------------------------------------------------------
# Feeders tab — FR24, PiAware, etc.
# ---------------------------------------------------------------------------

_FEEDER_SECRETS = ('fr24_key', 'piaware_feeder_id')


@settings_bp.route('/api/settings/feeders', methods=['GET', 'POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_feeders():
    if request.method == 'GET':
        return jsonify({
            'secrets_set': {k: bool(auth_lib.get_secret(k)) for k in _FEEDER_SECRETS},
        })
    payload = (request.get_json(silent=True) or {}).get('secrets') or {}
    for key in _FEEDER_SECRETS:
        if key in payload:
            auth_lib.set_secret(key, payload[key])
    logs_svc.log_portal(auth_lib.current_role() or 'admin', 'settings_feeders_update',
                        {'keys': list(payload)})
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Radar tab — display-only in phase 1
# ---------------------------------------------------------------------------

@settings_bp.route('/api/settings/radar', methods=['GET', 'POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_radar():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({
            'radar_publish_enabled': cfg.get('radar_publish_enabled', False),
            'radar_url': device_id.radar_url(current_app.config.get('DEVICE_RECORD', {})),
        })
    payload = request.get_json(silent=True) or {}
    if 'radar_publish_enabled' in payload:
        cfg['radar_publish_enabled'] = bool(payload['radar_publish_enabled'])
        _persist({'radar_publish_enabled': cfg['radar_publish_enabled']})
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Hardware tab — DHT pin, buzzer controls + test
# ---------------------------------------------------------------------------

_HARDWARE_KEYS = (
    'dht_pin', 'buzzer_pin', 'buzzer_enabled', 'buzzer_volume',
    'buzzer_threshold_temp_f', 'buzzer_threshold_hum', 'sensor_interval',
)


@settings_bp.route('/api/settings/hardware', methods=['GET', 'POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_hardware():
    cfg = current_app.skytrack_config
    if request.method == 'GET':
        return jsonify({
            'config': {k: cfg.get(k) for k in _HARDWARE_KEYS},
            'sensor': current_app.sensor_svc.read(),
            'buzzer_available': current_app.buzzer.available(),
        })
    payload = request.get_json(silent=True) or {}
    updates = {k: payload[k] for k in _HARDWARE_KEYS if k in payload}
    cfg.update(updates)
    if 'buzzer_volume' in updates:
        current_app.buzzer.set_volume(int(updates['buzzer_volume']))
    if 'buzzer_enabled' in updates:
        current_app.buzzer.set_enabled(bool(updates['buzzer_enabled']))
    if updates:
        _persist(updates)
    logs_svc.log_portal(auth_lib.current_role() or 'admin', 'settings_hardware_update', updates)
    return jsonify({'ok': True, 'config': {k: cfg.get(k) for k in _HARDWARE_KEYS}})


@settings_bp.route('/api/settings/hardware/buzzer/test', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_buzzer_test():
    result = current_app.buzzer.test()
    logs_svc.log_portal(auth_lib.current_role() or 'admin', 'buzzer_test', result)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Updates tab — OTA git pull (admin)
# ---------------------------------------------------------------------------

@settings_bp.route('/api/settings/updates/check', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_updates_check():
    import subprocess
    try:
        result = subprocess.run(
            ['git', 'fetch', current_app.skytrack_config.get('ota_remote', 'origin')],
            capture_output=True, text=True, timeout=20,
            cwd=os.path.dirname(os.path.abspath(__file__)) + '/..',
        )
        return jsonify({
            'ok': result.returncode == 0,
            'message': (result.stdout + result.stderr).strip()[-400:],
        })
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 500


@settings_bp.route('/api/settings/updates/apply', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_updates_apply():
    import subprocess
    cfg = current_app.skytrack_config
    try:
        result = subprocess.run(
            ['git', 'pull', cfg.get('ota_remote', 'origin'), cfg.get('ota_branch', 'main')],
            capture_output=True, text=True, timeout=60,
            cwd=os.path.dirname(os.path.abspath(__file__)) + '/..',
        )
        logs_svc.log_portal('admin', 'ota_apply', {
            'returncode': result.returncode,
            'stdout_tail': result.stdout[-200:],
        })
        return jsonify({
            'ok': result.returncode == 0,
            'message': (result.stdout + result.stderr).strip()[-400:],
        })
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _persist(updates: dict) -> None:
    """Persist a partial config update back to config.yaml."""
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
