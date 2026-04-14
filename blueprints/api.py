"""Catch-all read APIs that don't fit any specific tab.

These are intentionally thin — most data lives behind the dashboard or
settings blueprints. This blueprint is for cross-cutting endpoints that
the front-end shell needs (topbar identity, weather, health, sensor
heartbeat) without coupling them to any one page.
"""

import logging

from flask import Blueprint, current_app, jsonify

import auth as auth_lib
import device_id

logger = logging.getLogger('skytrack.api_bp')

api_bp = Blueprint('api', __name__)


@api_bp.route('/api/device')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_device():
    identity = current_app.config.get('DEVICE_RECORD', {})
    return jsonify({
        'device': identity,
        'radar_url': device_id.radar_url(identity) if identity else '',
    })


@api_bp.route('/api/health')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_health():
    return jsonify(current_app.health_svc.get_status())


@api_bp.route('/api/sensor')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_sensor():
    reading = current_app.sensor_svc.read()
    eval_result = current_app.buzzer.evaluate(reading)
    return jsonify({'reading': reading, 'buzzer': eval_result})


@api_bp.route('/api/weather')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_weather():
    return jsonify(current_app.weather_svc.get_weather())
