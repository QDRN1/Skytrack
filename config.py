"""SkyTrack v2 configuration loader.

Priority: env vars > config.yaml > defaults.

Credentials (PIN, admin password, API keys, hotspot password) are NOT in
config.yaml — they live in /var/lib/skytrack/auth.json and are accessed via
the auth module. Keep them out of git and out of the repo directory.
"""

import os
import logging

logger = logging.getLogger('skytrack.config')

DEFAULT_CONFIG = {
    # --- General ---
    'device_name_prefix': 'QDRN-SkyTrack',
    'host': '0.0.0.0',
    'port': 80,
    'debug': False,
    'secret_key': 'skytrack-change-me-in-production',
    'mock_mode': True,
    'timezone': 'auto',

    # --- Localization / units ---
    'units_temperature': 'F',   # F | C
    'units_speed': 'kts',       # kts | mph | kmh
    'clock_format': '12h',      # 12h | 24h
    'default_theme': 'light',   # light | dark

    # --- Location defaults (Red Wing, MN) ---
    'latitude': 44.602016,
    'longitude': -92.494604,
    'map_zoom': 8,

    # --- Hardware ---
    'dht_pin': 21,
    'buzzer_pin': 18,
    'buzzer_enabled': True,
    'buzzer_threshold_temp_f': 95.0,
    'buzzer_threshold_hum': 80.0,
    'buzzer_volume': 60,        # 0-100 PWM duty cycle
    'sensor_interval': 15,      # seconds between reads

    # --- ADS-B ingestion ---
    'dump1090_json_path': '/run/dump1090-fa/aircraft.json',
    'dump1090_url': 'http://localhost:8080/data/aircraft.json',
    'ingest_interval': 5,       # seconds
    'sightings_retention_days': 7,
    'logs_retention_days': 30,

    # --- Hotspot / Network ---
    'hotspot_ssid': 'SkyTrack-Portal',
    'hotspot_gateway': '10.4.26.89',
    'hotspot_subnet': '10.4.26.0/24',
    'hotspot_dhcp_start': '10.4.26.100',
    'hotspot_dhcp_end': '10.4.26.200',
    'hotspot_channel': 6,
    'hotspot_country': 'US',
    'metered_connection': False,  # cellular metered mode

    # --- Integrations (keys live in auth.json) ---
    'aeroapi_enabled': False,
    'aeroapi_calls_per_hour': 10,
    'aeroapi_calls_per_day': 200,
    'aeroapi_cost_per_call': 0.01,
    'opensky_enabled': False,
    'opensky_poll_minutes': 10,
    'enrichment_ttl_hours': 24,

    # --- Weather ---
    'weather_interval': 1800,     # 30 min

    # --- Radar publishing (display-only in phase 1) ---
    'radar_publish_enabled': False,

    # --- Auth / sessions ---
    'session_timeout_hours': 8,
    'admin_password_session_minutes': 15,  # re-auth prompt in settings

    # --- Updates ---
    'ota_remote': 'origin',
    'ota_branch': 'main',
    'ota_enabled': True,

    # --- Paths ---
    'data_dir': '/var/lib/skytrack',
    'geo_data_dir': '/var/lib/skytrack/geo',
    'log_dir': '/var/log/skytrack',
    'db_path': '/var/lib/skytrack/skytrack.db',
    'auth_path': '/var/lib/skytrack/auth.json',
    'device_id_path': '/var/lib/skytrack/device_id',
    'backup_dir': '/var/lib/skytrack/backups',

    # --- Display / kiosk ---
    'display_rotation': '0',
    'display_output': 'HDMI-1',

    # --- Burn-in prevention ---
    'jitter_interval': 300,       # 5 min
    'refresh_interval': 3600,     # 60 min

    # --- Logging ---
    'log_level': 'INFO',

    # --- GPS / geocoding ---
    'geo_offline_enabled': True,

    # --- Cloudflare Tunnel (optional remote access) ---
    'cloudflared_enabled': False,
}


_ENV_MAP = {
    'SKYTRACK_PORT':            ('port', int),
    'SKYTRACK_HOST':            ('host', str),
    'SKYTRACK_DEBUG':           ('debug', lambda x: x.lower() in ('1', 'true', 'yes')),
    'SKYTRACK_MOCK':            ('mock_mode', lambda x: x.lower() in ('1', 'true', 'yes')),
    'SKYTRACK_LAT':             ('latitude', float),
    'SKYTRACK_LON':             ('longitude', float),
    'SKYTRACK_DHT_PIN':         ('dht_pin', int),
    'SKYTRACK_BUZZER_PIN':      ('buzzer_pin', int),
    'SKYTRACK_LOG_LEVEL':       ('log_level', str),
    'SKYTRACK_DB_PATH':         ('db_path', str),
    'SKYTRACK_AUTH_PATH':       ('auth_path', str),
    'SKYTRACK_DEVICE_ID_PATH':  ('device_id_path', str),
    'SKYTRACK_GEO_DATA_DIR':    ('geo_data_dir', str),
    'SKYTRACK_SECRET_KEY':      ('secret_key', str),
    'SKYTRACK_INGEST_INTERVAL': ('ingest_interval', int),
    'SKYTRACK_DISPLAY_ROTATION': ('display_rotation', str),
    'SKYTRACK_DISPLAY_OUTPUT':  ('display_output', str),
}


def load_config(config_path: str = None) -> dict:
    """Load config from YAML with env-var overrides. Safe defaults on failure."""
    if config_path is None:
        config_path = os.environ.get(
            'SKYTRACK_CONFIG',
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml'),
        )

    config = DEFAULT_CONFIG.copy()

    if os.path.exists(config_path):
        try:
            import yaml
            with open(config_path, 'r') as f:
                user_config = yaml.safe_load(f) or {}
            config.update(user_config)
            logger.info('Config loaded from %s', config_path)
        except ImportError:
            logger.warning('PyYAML not installed; skipping config.yaml')
        except Exception as e:
            logger.warning('Could not load config from %s: %s', config_path, e)
    else:
        logger.info('No config file at %s, using defaults', config_path)

    # Environment overrides
    for env_key, (cfg_key, conv) in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        try:
            config[cfg_key] = conv(val)
        except (ValueError, TypeError):
            logger.warning('Invalid value for %s: %s', env_key, val)

    return config


def save_user_config(config_path: str, updates: dict) -> None:
    """Persist a subset of config keys back to YAML. Used by Settings → General."""
    try:
        import yaml
    except ImportError:
        raise RuntimeError('PyYAML not available; cannot persist settings')

    existing = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                existing = yaml.safe_load(f) or {}
        except Exception:
            existing = {}

    existing.update(updates)

    parent = os.path.dirname(config_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = config_path + '.tmp'
    with open(tmp, 'w') as f:
        yaml.safe_dump(existing, f, sort_keys=False)
    os.replace(tmp, config_path)
