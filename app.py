#!/usr/bin/env python3
"""SkyTrack - ADS-B Flight Tracker & Status Monitor.

Main Flask + SocketIO application.
"""

import os
import sys
import logging
from datetime import datetime

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO

from config import load_config
from weather import WeatherService
from aircraft import AircraftService
from sensors import SensorService
from health import HealthService
from alerts import AlertService

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
config = load_config()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_dir = config.get('log_dir', '/var/log/skytrack')
try:
    os.makedirs(log_dir, exist_ok=True)
    log_handlers = [
        logging.FileHandler(os.path.join(log_dir, 'skytrack.log')),
        logging.StreamHandler(sys.stdout),
    ]
except PermissionError:
    # Can't write to /var/log — log to stdout only
    log_handlers = [logging.StreamHandler(sys.stdout)]

logging.basicConfig(
    level=getattr(logging, config.get('log_level', 'INFO').upper(), logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=log_handlers,
)
logger = logging.getLogger('skytrack')

# ---------------------------------------------------------------------------
# Flask + SocketIO
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = config.get('secret_key', 'skytrack-dev-key')
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
weather_svc = WeatherService(config)
aircraft_svc = AircraftService(config)
sensor_svc = SensorService(config)
health_svc = HealthService(config)
alert_svc = AlertService(config)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def dashboard():
    return render_template('dashboard.html', config=config)


@app.route('/api/health')
def api_health():
    return jsonify(health_svc.get_status())


@app.route('/api/weather')
def api_weather():
    return jsonify(weather_svc.get_weather())


@app.route('/api/aircraft')
def api_aircraft():
    return jsonify(aircraft_svc.get_data())


@app.route('/api/sensor')
def api_sensor():
    return jsonify(sensor_svc.read())


@app.route('/api/selfcheck')
def api_selfcheck():
    checks = {
        'weather': weather_svc.selfcheck(),
        'aircraft': aircraft_svc.selfcheck(),
        'sensors': sensor_svc.selfcheck(),
        'health': health_svc.selfcheck(),
        'alerts': alert_svc.selfcheck(),
    }
    all_ok = all(c['ok'] for c in checks.values())
    return jsonify({'ok': all_ok, 'checks': checks})


# ---------------------------------------------------------------------------
# WebSocket events
# ---------------------------------------------------------------------------

@socketio.on('connect')
def handle_connect():
    logger.info('Client connected')
    _push_all()


@socketio.on('request_update')
def handle_request_update(data):
    kind = data.get('type', 'all') if isinstance(data, dict) else 'all'
    if kind in ('weather', 'all'):
        socketio.emit('weather_update', weather_svc.get_weather())
    if kind in ('aircraft', 'all'):
        socketio.emit('aircraft_update', aircraft_svc.get_data())
    if kind in ('sensor', 'all'):
        socketio.emit('sensor_update', sensor_svc.read())
    if kind in ('health', 'all'):
        socketio.emit('health_update', health_svc.get_status())
    if kind in ('alert', 'all'):
        socketio.emit('alert_update', alert_svc.get_last_alert())


def _push_all():
    try:
        socketio.emit('weather_update', weather_svc.get_weather())
        socketio.emit('aircraft_update', aircraft_svc.get_data())
        socketio.emit('sensor_update', sensor_svc.read())
        socketio.emit('health_update', health_svc.get_status())
        socketio.emit('alert_update', alert_svc.get_last_alert())
    except Exception as e:
        logger.error(f'Initial push error: {e}')


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

def _weather_loop():
    while True:
        try:
            socketio.emit('weather_update', weather_svc.get_weather())
        except Exception as e:
            logger.error(f'Weather loop error: {e}')
        socketio.sleep(config.get('weather_interval', 1800))


def _aircraft_loop():
    while True:
        try:
            data = aircraft_svc.get_data()
            socketio.emit('aircraft_update', data)
        except Exception as e:
            logger.error(f'Aircraft loop error: {e}')
        socketio.sleep(config.get('aircraft_interval', 15))


def _sensor_loop():
    while True:
        try:
            data = sensor_svc.read()
            socketio.emit('sensor_update', data)
            # Humidity alert check
            hum = data.get('humidity')
            if hum is not None and hum > config.get('humidity_threshold', 80):
                triggered = alert_svc.trigger_humidity_alert(data)
                if triggered:
                    socketio.emit('alert_update', alert_svc.get_last_alert())
        except Exception as e:
            logger.error(f'Sensor loop error: {e}')
        socketio.sleep(config.get('sensor_interval', 30))


def _health_loop():
    while True:
        try:
            socketio.emit('health_update', health_svc.get_status())
        except Exception as e:
            logger.error(f'Health loop error: {e}')
        socketio.sleep(config.get('health_interval', 60))


def _start_background():
    socketio.start_background_task(_weather_loop)
    socketio.start_background_task(_aircraft_loop)
    socketio.start_background_task(_sensor_loop)
    socketio.start_background_task(_health_loop)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    logger.info('=' * 60)
    logger.info('  QDRNow-SkyTrack starting up')
    logger.info(f'  Mock mode: {config.get("mock_mode", True)}')
    logger.info('=' * 60)

    # Self-check mode
    if '--selfcheck' in sys.argv:
        with app.app_context():
            result = api_selfcheck().get_json()
        print('\nSkyTrack Self-Check')
        print('-' * 40)
        for name, check in result['checks'].items():
            status = 'OK' if check['ok'] else 'FAIL'
            print(f'  [{status:4s}] {name}: {check.get("message", "")}')
        print('-' * 40)
        print(f'Overall: {"PASS" if result["ok"] else "FAIL"}')
        sys.exit(0 if result['ok'] else 1)

    _start_background()

    host = config.get('host', '0.0.0.0')
    port = config.get('port', 5000)
    debug = config.get('debug', False)

    logger.info(f'Dashboard at http://{host}:{port}')
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
