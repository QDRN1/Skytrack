"""SkyTrack configuration loader.

Loads settings from config.yaml with environment variable overrides.
All settings have sensible defaults so the app runs in mock mode out of the box.
"""

import os
import logging

logger = logging.getLogger('skytrack.config')

DEFAULT_CONFIG = {
    # General
    'device_name': 'QDRNow-SkyTrack',
    'host': '0.0.0.0',
    'port': 5000,
    'debug': False,
    'secret_key': 'skytrack-change-me-in-production',
    'mock_mode': True,

    # Location (default: Red Wing, MN area)
    'latitude': 44.602016,
    'longitude': -92.494604,
    'map_zoom': 8,

    # DHT22 Sensor
    'dht_pin': 21,
    'humidity_threshold': 80,
    'sensor_interval': 30,

    # Weather (WeatherAPI.com)
    'weather_api_key': '',
    'weather_interval': 1800,  # 30 minutes

    # Aircraft (dump1090)
    'dump1090_json_path': '/run/dump1090-fa/aircraft.json',
    'dump1090_url': 'http://localhost:8080/data/aircraft.json',
    'aircraft_interval': 15,

    # Health
    'health_interval': 60,

    # Alerts (SMTP)
    'smtp_host': '',
    'smtp_port': 587,
    'smtp_user': '',
    'smtp_pass': '',
    'alert_from': '',
    'alert_to': '',
    'alert_cooldown': 300,  # 5 minutes between alerts

    # Cloudflare Tunnel
    'cloudflared_enabled': False,

    # Display
    'display_rotation': 'normal',     # normal, left, right, inverted
    'display_output': 'HDMI-1',

    # GPS / Offline Geocoding
    'geo_offline_enabled': True,
    'geo_data_dir': '/var/lib/skytrack/geo',

    # Burn-in prevention
    'jitter_interval': 300,      # 5 minutes - micro-jitter
    'refresh_interval': 3600,    # 60 minutes - full-screen refresh scene
    'refresh_duration': 8,       # seconds per refresh scene

    # Logging
    'log_dir': '/var/log/skytrack',
    'log_level': 'INFO',
}

# Environment variable -> (config key, type converter)
_ENV_MAP = {
    'SKYTRACK_PORT':            ('port', int),
    'SKYTRACK_HOST':            ('host', str),
    'SKYTRACK_DEBUG':           ('debug', lambda x: x.lower() in ('true', '1', 'yes')),
    'SKYTRACK_MOCK':            ('mock_mode', lambda x: x.lower() in ('true', '1', 'yes')),
    'SKYTRACK_WEATHER_API_KEY': ('weather_api_key', str),
    'SKYTRACK_LAT':             ('latitude', float),
    'SKYTRACK_LON':             ('longitude', float),
    'SKYTRACK_DHT_PIN':         ('dht_pin', int),
    'SKYTRACK_HUMIDITY_THRESH': ('humidity_threshold', float),
    'SKYTRACK_SECRET_KEY':      ('secret_key', str),
    'SKYTRACK_LOG_LEVEL':       ('log_level', str),
    'SKYTRACK_GEO_ENABLED':     ('geo_offline_enabled', lambda x: x.lower() in ('true', '1', 'yes')),
    'SKYTRACK_GEO_DATA_DIR':    ('geo_data_dir', str),
    'SKYTRACK_DISPLAY_ROTATION': ('display_rotation', str),
    'SKYTRACK_DISPLAY_OUTPUT':  ('display_output', str),
}


def load_config(config_path=None):
    """Load configuration from YAML file with env-var overrides.

    Priority: env vars > config.yaml > defaults
    """
    if config_path is None:
        config_path = os.environ.get(
            'SKYTRACK_CONFIG',
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
        )

    config = DEFAULT_CONFIG.copy()

    # Load YAML if present
    if os.path.exists(config_path):
        try:
            import yaml
            with open(config_path, 'r') as f:
                user_config = yaml.safe_load(f) or {}
            config.update(user_config)
            logger.info(f"Config loaded from {config_path}")
        except ImportError:
            logger.warning("PyYAML not installed; skipping config.yaml")
        except Exception as e:
            logger.warning(f"Could not load config from {config_path}: {e}")
    else:
        logger.info(f"No config file at {config_path}, using defaults (mock mode)")

    # Environment variable overrides
    for env_key, (config_key, converter) in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is not None:
            try:
                config[config_key] = converter(val)
            except (ValueError, TypeError):
                logger.warning(f"Invalid value for {env_key}: {val}")

    return config
