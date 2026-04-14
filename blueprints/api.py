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

      gps.state             disabled | searching | fix_acquired | stale | error
                            (placeholder while Phase 6 ships — currently
                            always 'not_implemented')

    Kept here in api.py instead of behind ADMIN so that:
      1. The kiosk page can render the hardware row pre-login.
      2. `quick_check.sh` can scrape it without an auth cookie.
    """
    sensor_reading = current_app.sensor_svc.read() or {}
    bz = current_app.buzzer

    # GPS isn't implemented yet (Phase 6) — return a stable shape so the
    # CLI doesn't have to special-case its absence.
    gps = {
        'state':       'not_implemented',
        'source':      None,
        'fix_age_sec': None,
        'satellites':  None,
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


@api_bp.route('/api/weather')
def api_weather():
    return jsonify(current_app.weather_svc.get_weather())
