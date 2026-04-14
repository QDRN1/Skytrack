#!/usr/bin/env python3
"""SkyTrack Portal v2 — Flask application factory.

Implements the strict v2 startup contract:

  Strict initialization order
  ---------------------------
      1. Load configuration                (config.load_config)
      2. Generate or load device ID        (device_id.get_or_create_device_id)
      3. Initialize database               (db.migrate)
      4. Initialize authentication system  (auth.read_auth)
      5. Start background services        (daemon threads, non-blocking)
      6. Register blueprints               (auth, dashboard, settings, logs, network, …)

  Background services (each runs in its own daemon=True thread)
  ------------------------------------------------------------
      • ADS-B sightings ingest loop
      • DHT22 temperature/humidity sensor polling loop
      • Cellular modem status monitor
      • API enrichment worker (drains an in-memory queue, never polls)
      • Database / log retention cleanup

  DEV_MODE
  --------
      When the `DEV_MODE` env var is set to a truthy value:
          - mock_mode is forced on (mock sensors, mock aircraft fleet)
          - GPIO is bypassed (buzzer disabled, DHT22 mocked)
          - Hardware dependencies become no-ops

  Device ID
  ---------
      Stored in `app.config["DEVICE_ID"]` once during create_app() and
      then never recomputed for the life of the process.
"""

import logging
import os
import queue
import sys
import threading
import time
from typing import Optional

from flask import Flask, render_template, request
from flask_socketio import SocketIO

from _version import __version__ as SKYTRACK_VERSION

# NOTE: backend modules are imported lazily inside create_app() so that
# unit tests can import this module without paying the full hardware
# initialization cost.

logger = logging.getLogger('skytrack')


# ---------------------------------------------------------------------------
# Module-level singletons reused across worker threads
# ---------------------------------------------------------------------------

socketio = SocketIO(
    cors_allowed_origins='*',
    async_mode='threading',
    logger=False,
    engineio_logger=False,
)

_workers_started = False
_worker_lock = threading.Lock()
_enrichment_queue: 'queue.Queue' = queue.Queue()
_cellular_state: dict = {'state': 'unknown'}
_cellular_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(level_name: str = 'INFO') -> None:
    """Configure root logging for the entire SkyTrack process."""
    handlers = [logging.StreamHandler(sys.stdout)]
    log_dir = '/var/log/skytrack'
    try:
        os.makedirs(log_dir, exist_ok=True)
        handlers.insert(0, logging.FileHandler(os.path.join(log_dir, 'skytrack.log')))
    except (PermissionError, OSError):
        # Dev box without /var/log/skytrack — stdout only
        pass

    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=handlers,
        force=True,
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(config_overrides: Optional[dict] = None) -> Flask:
    """Build and return a fully wired SkyTrack Portal v2 Flask app.

    Steps follow the strict initialization order documented at the top
    of this file. Every long-running task is started as a daemon thread
    so that Flask startup never blocks.
    """

    # Logging must come up first so all subsequent steps are visible
    _setup_logging()

    # ------------------------------------------------------------------
    # Step 1: Load configuration
    # ------------------------------------------------------------------
    from config import load_config
    config = load_config()
    if config_overrides:
        config.update(config_overrides)

    # DEV_MODE — env override that forces mock mode and disables GPIO
    dev_mode = os.environ.get('DEV_MODE', '').lower() in ('1', 'true', 'yes', 'on')
    if dev_mode:
        config['mock_mode'] = True
        config['buzzer_enabled'] = False
        logger.info('DEV_MODE active — mock sensors + mock aircraft, GPIO bypassed')

    # Re-apply log level from config now that we know it
    logging.getLogger().setLevel(
        getattr(logging, (config.get('log_level') or 'INFO').upper(), logging.INFO)
    )

    # Make sure the data directory exists before any module touches it
    try:
        os.makedirs(config.get('data_dir', '/var/lib/skytrack'), exist_ok=True)
    except OSError as e:
        logger.warning('Could not create data_dir: %s', e)

    # Propagate path config to the modules that read environment variables
    if config.get('db_path'):
        os.environ.setdefault('SKYTRACK_DB_PATH', config['db_path'])
    if config.get('auth_path'):
        os.environ.setdefault('SKYTRACK_AUTH_PATH', config['auth_path'])
    if config.get('device_id_path'):
        os.environ.setdefault('SKYTRACK_DEVICE_ID_PATH', config['device_id_path'])

    # ------------------------------------------------------------------
    # Step 2: Generate or load device ID
    # ------------------------------------------------------------------
    import device_id
    identity = device_id.get_or_create_device_id()
    logger.info('Device identity: %s (hw=%s)',
                identity['device_id'], identity.get('hw_source'))

    # ------------------------------------------------------------------
    # Step 3: Initialize database
    # ------------------------------------------------------------------
    import db
    db.migrate()
    logger.info('Database ready (schema v%d)', db.current_version())

    # ------------------------------------------------------------------
    # Step 4: Initialize authentication system
    # ------------------------------------------------------------------
    import auth as auth_lib
    auth_lib.read_auth()  # seeds /var/lib/skytrack/auth.json on first run
    logger.info('Auth subsystem ready (configured=%s)', auth_lib.is_configured())

    # ------------------------------------------------------------------
    # Build Flask app shell
    # ------------------------------------------------------------------
    app = Flask(
        __name__,
        template_folder='templates',
        static_folder='static',
        static_url_path='/static',
    )
    app.config['SECRET_KEY'] = config.get('secret_key', 'skytrack-dev-key')
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024  # 4 MB

    # Device ID stored in app.config per spec
    app.config['DEVICE_ID'] = identity['device_id']
    app.config['DEVICE_RECORD'] = identity
    app.config['DEV_MODE'] = dev_mode
    app.config['CONFIG'] = config

    # Convenience attribute access for blueprints / workers
    app.skytrack_config = config
    app.identity = identity

    # Build the hardware/data services. Each one accepts the config dict
    # and respects mock_mode internally so DEV_MODE Just Works.
    from buzzer import Buzzer
    from health import HealthService
    from ingest import SightingsIngest
    from sensors import SensorService
    from weather import WeatherService

    app.weather_svc = WeatherService(config)
    app.sensor_svc = SensorService(config)
    app.health_svc = HealthService(config)
    app.buzzer = Buzzer(config)
    app.ingest = SightingsIngest(config)

    # Shared state surfaces consumed by blueprints
    app.enrichment_queue = _enrichment_queue
    app.cellular_state = _cellular_state
    app.cellular_state_lock = _cellular_state_lock

    # ------------------------------------------------------------------
    # Step 5: Start background services (daemon threads — non-blocking)
    # ------------------------------------------------------------------
    _start_background_services(app)

    # ------------------------------------------------------------------
    # Step 6: Register blueprints
    # ------------------------------------------------------------------
    _register_blueprints(app)
    _register_template_globals(app)
    _register_error_handlers(app)
    _register_socket_events(app)

    # Bind SocketIO last — after the routes are registered
    socketio.init_app(app)

    logger.info('SkyTrack Portal v2 app ready (DEV_MODE=%s, configured=%s)',
                dev_mode, auth_lib.is_configured())
    return app


# ---------------------------------------------------------------------------
# Background services — every long-running task runs as a daemon thread.
# These are started exactly once per process.
# ---------------------------------------------------------------------------

def _start_background_services(app: Flask) -> None:
    global _workers_started
    with _worker_lock:
        if _workers_started:
            return
        _workers_started = True

    cfg = app.skytrack_config

    # 1. ADS-B sightings ingest (the SightingsIngest class manages its own
    # daemon thread internally — start() is non-blocking).
    try:
        app.ingest.start()
        logger.info('Background service started: sightings ingest')
    except Exception as e:
        logger.warning('ingest start failed: %s', e)

    # 2a. DHT22 hardware poller — single owner of the GPIO pin.
    # The poll cadence and retry behaviour live inside SensorService;
    # this just kicks the thread off. Idempotent + no-op when hardware
    # is absent.
    try:
        app.sensor_svc.start()
    except Exception as e:
        logger.warning('sensor poller start failed: %s', e)

    # 2b. Sensor emit loop — runs at app cadence, READS THE CACHE, never
    # touches the GPIO. Hammering the DHT22 from N HTTP endpoints +
    # websocket emitters is what produced the constant "Checksum did not
    # validate" stream; SensorService now owns the pin and serves cached
    # readings to everyone else.
    def _sensor_loop():
        import logs_svc
        interval = max(5, int(cfg.get('sensor_emit_interval', 5)))
        while True:
            try:
                reading = app.sensor_svc.read()
                buzzer_eval = app.buzzer.evaluate(reading)
                socketio.emit('sensor_update', {
                    'reading': reading,
                    'buzzer': buzzer_eval,
                })
                if buzzer_eval.get('triggered'):
                    logs_svc.log_portal('system', 'buzzer_alert',
                                        buzzer_eval.get('reason', ''))
            except Exception as e:
                logger.debug('sensor loop error: %s', e)
            time.sleep(interval)

    # 3. Cellular status monitor
    def _cellular_loop():
        import network_svc
        while True:
            try:
                cell = network_svc.cellular_status()
                with _cellular_state_lock:
                    _cellular_state.clear()
                    _cellular_state.update(cell)
                socketio.emit('cellular_update', cell)
            except Exception as e:
                logger.debug('cellular loop error: %s', e)
            time.sleep(30)

    # 4. API enrichment worker — drains an in-memory queue, never polls.
    # Producers (the dashboard) push (icao, callsign) tuples onto the
    # queue; the worker calls enrich.enrich_flight() for each one,
    # subject to the budget controls in enrich.py.
    def _enrichment_worker():
        import enrich
        while True:
            try:
                job = _enrichment_queue.get(block=True)
                if job is None:
                    break
                icao, callsign = job
                rec = enrich.enrich_flight(icao, callsign, cfg)
                if rec:
                    socketio.emit('enrichment_update', rec)
            except Exception as e:
                logger.debug('enrichment worker error: %s', e)
            finally:
                try:
                    _enrichment_queue.task_done()
                except Exception:
                    pass

    # 5. Log + sightings retention cleanup — once per hour
    def _prune_loop():
        import db as _db
        while True:
            try:
                _db.prune(
                    sightings_days=int(cfg.get('sightings_retention_days', 7)),
                    logs_days=int(cfg.get('logs_retention_days', 30)),
                )
            except Exception as e:
                logger.debug('prune loop error: %s', e)
            time.sleep(3600)

    # 6. Dashboard tick — periodic push of the 4 cards + positions so
    # connected clients always see fresh data without polling REST.
    def _dashboard_tick_loop():
        import dashboard_svc
        while True:
            try:
                payload = {
                    'cards': {
                        'now': dashboard_svc.card_aircraft_now(),
                        'today': dashboard_svc.card_aircraft_today(),
                        'busiest': dashboard_svc.card_busiest_hour(),
                        'last': dashboard_svc.card_last_aircraft(),
                    },
                    'positions': dashboard_svc.aircraft_now_positions(),
                }
                socketio.emit('dashboard_tick', payload)
            except Exception as e:
                logger.debug('dashboard tick error: %s', e)
            time.sleep(10)

    workers = [
        ('skytrack-sensor', _sensor_loop),
        ('skytrack-cellular', _cellular_loop),
        ('skytrack-enrichment', _enrichment_worker),
        ('skytrack-prune', _prune_loop),
        ('skytrack-dashtick', _dashboard_tick_loop),
    ]
    for name, target in workers:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        logger.info('Background service started: %s', name)


# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------

def _register_blueprints(app: Flask) -> None:
    """Register every blueprint.

    There is no global auth gate. The auth blueprint exposes /setup, /login,
    /logout, /superuser, and /healthz. Per-route admin protection is done
    via @auth_lib.login_required(ROLE_ADMIN) inside each blueprint that
    needs it. The dashboard is intentionally public so the kiosk can render
    without authentication."""
    from blueprints.auth import auth_bp
    from blueprints.dashboard import dashboard_bp
    from blueprints.settings import settings_bp
    from blueprints.logs import logs_bp
    from blueprints.network import network_bp
    from blueprints.onboarding import onboarding_bp
    from blueprints.super import super_bp
    from blueprints.api import api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(network_bp)
    app.register_blueprint(onboarding_bp)
    app.register_blueprint(super_bp)
    app.register_blueprint(api_bp)


def _register_template_globals(app: Flask) -> None:
    """Inject device + role + config into every Jinja template."""
    import auth as auth_lib
    import device_id

    @app.context_processor
    def _inject_globals():
        identity = app.config.get('DEVICE_RECORD', {})
        role = auth_lib.current_role()
        return {
            'device': identity,
            'device_id': app.config.get('DEVICE_ID'),
            'radar_url': device_id.radar_url(identity) if identity else '',
            'config': app.skytrack_config,
            'current_role': role,
            'is_admin': role in (auth_lib.ROLE_ADMIN, auth_lib.ROLE_SUPER),
            'is_super': role == auth_lib.ROLE_SUPER,
            'configured': auth_lib.is_configured(),
            'dev_mode': app.config.get('DEV_MODE', False),
            'app_version': SKYTRACK_VERSION,
        }


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(401)
    def _401(_e):
        return render_template('errors/401.html'), 401

    @app.errorhandler(404)
    def _404(_e):
        return render_template('errors/404.html'), 404

    @app.errorhandler(500)
    def _500(e):
        logger.exception('Unhandled 500: %s', e)
        return render_template('errors/500.html'), 500


def _register_socket_events(app: Flask) -> None:
    @socketio.on('connect')
    def _on_connect():
        logger.debug('socket client connected: %s', getattr(request, 'sid', '?'))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    app = create_app()
    cfg = app.skytrack_config

    if '--selfcheck' in sys.argv:
        import db
        import enrich
        import hotspot
        checks = {
            'db': {
                'ok': db.current_version() >= 1,
                'message': f'schema v{db.current_version()}',
            },
            'device_id': {
                'ok': bool(app.config.get('DEVICE_ID')),
                'message': app.config.get('DEVICE_ID', ''),
            },
            'sensors': app.sensor_svc.selfcheck(),
            'weather': app.weather_svc.selfcheck(),
            'health': app.health_svc.selfcheck(),
            'ingest': app.ingest.selfcheck(),
            'hotspot': hotspot.selfcheck(cfg),
            'aeroapi': enrich.test_aeroapi(),
        }
        ok = all(c['ok'] for c in checks.values())
        print('SkyTrack Portal v2 self-check')
        print('-' * 50)
        for name, c in checks.items():
            label = 'OK  ' if c['ok'] else 'FAIL'
            print(f'  [{label}] {name}: {c.get("message", "")}')
        print('-' * 50)
        print('Overall:', 'PASS' if ok else 'FAIL')
        sys.exit(0 if ok else 1)

    host = cfg.get('host', '0.0.0.0')
    port = int(cfg.get('port', 8080))
    debug = bool(cfg.get('debug', False))

    import auth as auth_lib
    logger.info('SkyTrack Portal v2 starting on http://%s:%d', host, port)
    logger.info('Device: %s', app.config.get('DEVICE_ID'))
    logger.info('Configured: %s', auth_lib.is_configured())
    logger.info('DEV_MODE: %s', app.config.get('DEV_MODE'))

    try:
        socketio.run(
            app,
            host=host,
            port=port,
            debug=debug,
            allow_unsafe_werkzeug=True,
        )
    finally:
        try:
            app.ingest.stop()
            app.buzzer.shutdown()
        except Exception:
            pass


if __name__ == '__main__':
    main()
