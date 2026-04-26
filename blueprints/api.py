"""Catch-all read APIs that don't fit any specific tab.

These are intentionally thin — most data lives behind the dashboard or
settings blueprints. This blueprint is for cross-cutting endpoints that
the front-end shell needs (topbar identity, weather, health, sensor
heartbeat) without coupling them to any one page.

All endpoints here are **public**. They feed the topbar and the
public dashboard, both of which must work without a login.
"""

import logging

from flask import Blueprint, current_app, jsonify

import device_id

logger = logging.getLogger('skytrack.api_bp')

api_bp = Blueprint('api', __name__)


@api_bp.route('/api/device')
def api_device():
    identity = current_app.config.get('DEVICE_RECORD', {})
    return jsonify({
        'device': identity,
        'radar_url': device_id.radar_url(identity) if identity else '',
    })


@api_bp.route('/api/health')
def api_health():
    return jsonify(current_app.health_svc.get_status())


@api_bp.route('/api/sensor')
def api_sensor():
    reading = current_app.sensor_svc.read()
    eval_result = current_app.buzzer.evaluate(reading)
    return jsonify({'reading': reading, 'buzzer': eval_result})


@api_bp.route('/api/hardware/summary')
def api_hardware_summary():
    """Public hardware-truth summary consumed by the kiosk diagnostics
    panel and by the operator CLI tools (`skytrack-quick-check` /
    `skytrack-long-check`).

    Returns *non-secret* fields only — no PINs, no API keys, no
    network credentials. Specifically:

      sensor.source         real | cached | mock | error
      sensor.state          alias of source for clarity
      sensor.available      bool — DHT22 init succeeded
      sensor.temperature_f  current reading (or null)
      sensor.humidity       current reading (or null)
      sensor.cache_age_sec  seconds since last good read (cached only)
      sensor.last_error     short string for the diagnostics panel

      buzzer.available      bool — PWM init succeeded
      buzzer.enabled        bool — operator hasn't muted it
      buzzer.last_error     short string

      gps.state             no_fix | fix_acquired | config
      gps.source            gpsd | modemmanager | config | cached
      gps.lat / gps.lon     current position (null when no_fix)

    Kept here in api.py instead of behind ADMIN so that:
      1. The kiosk page can render the hardware row pre-login.
      2. `quick_check.sh` can scrape it without an auth cookie.
    """
    sensor_reading = current_app.sensor_svc.read() or {}
    bz = current_app.buzzer

    with current_app.gps_state_lock:
        gps_snap = dict(current_app.gps_state)
    gps = {
        'state':       gps_snap.get('state', 'no_fix'),
        'source':      gps_snap.get('source'),
        'lat':         gps_snap.get('lat'),
        'lon':         gps_snap.get('lon'),
        'accuracy_m':  gps_snap.get('accuracy_m'),
        'last_fix_utc': gps_snap.get('last_fix_utc'),
    }

    src = sensor_reading.get('source') or sensor_reading.get('state') or 'mock'
    return jsonify({
        'sensor': {
            'available':     bool(getattr(current_app.sensor_svc, 'available', False)),
            'source':        src,
            'state':         sensor_reading.get('state') or src,
            'temperature_f': sensor_reading.get('temperature_f'),
            'humidity':      sensor_reading.get('humidity'),
            'cache_age_sec': sensor_reading.get('cache_age_sec'),
            'last_error':    sensor_reading.get('last_error')
                              or getattr(current_app.sensor_svc, 'last_error', '') or '',
        },
        'buzzer': {
            'available':  bool(bz.available()),
            'enabled':    bool(getattr(bz, 'enabled', False)),
            'last_error': getattr(bz, 'last_error', '') or '',
        },
        'gps': gps,
    })


@api_bp.route('/api/gps')
def api_gps():
    """Current location for the map. Respects location_source setting."""
    cfg = current_app.skytrack_config
    zoom = cfg.get('map_zoom', 9)
    if cfg.get('location_source') == 'manual':
        return jsonify({
            'state': 'manual',
            'source': 'manual',
            'lat': cfg.get('latitude'),
            'lon': cfg.get('longitude'),
            'map_zoom': zoom,
            'accuracy_m': None,
            'last_fix_utc': None,
        })
    with current_app.gps_state_lock:
        snap = dict(current_app.gps_state)
    if snap.get('state') in ('fix_acquired', 'static') and snap.get('lat'):
        snap['map_zoom'] = zoom
        return jsonify(snap)
    return jsonify({
        'state': 'no_fix',
        'source': 'config',
        'lat': cfg.get('latitude'),
        'lon': cfg.get('longitude'),
        'map_zoom': zoom,
        'accuracy_m': None,
        'last_fix_utc': None,
    })


@api_bp.route('/api/weather')
def api_weather():
    return jsonify(current_app.weather_svc.get_weather())


@api_bp.route('/api/device-temp-alarm')
def api_device_temp_alarm():
    """Public — kiosk needs this for the critical overlay."""
    alarm = getattr(current_app, 'device_temp_alarm', None)
    if alarm:
        return jsonify(alarm.status())
    return jsonify({'active': False, 'cpu_temp_c': None, 'threshold_c': 80})
