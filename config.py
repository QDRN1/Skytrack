"""SkyTrack v2 configuration loader.

Priority: env vars > config.yaml > defaults.

Credentials (admin PIN, API keys, hotspot password, super-user override)
are NOT in config.yaml — they live in /var/lib/skytrack/auth.json and are
accessed via the auth module. Keep them out of git and out of the repo.
"""

import os
import logging

logger = logging.getLogger('skytrack.config')

DEFAULT_CONFIG = {
    # --- General ---
    'device_name_prefix': 'QDRN-SkyTrack',
    'host': '0.0.0.0',
    'port': 8080,
    'debug': False,
    'secret_key': 'skytrack-change-me-in-production',
    # mock_mode is NOT a hardware kill-switch — sensors/buzzer try real
    # hardware first regardless. It only affects code paths that *cannot*
    # gracefully fall back on their own (e.g. ADS-B mock fleet). Real
    # installs should leave this True; the sensor layer ignores it on a
    # detected Pi and always probes the GPIO before using mock values.
    'mock_mode': True,
    'timezone': 'auto',

    # --- Localization / units ---
    'units_temperature': 'F',   # F | C
    'units_speed': 'kts',       # kts | mph | kmh
    'clock_format': '12h',      # 12h | 24h
    'default_theme': 'dark',    # light | dark | auto

    # --- Location defaults (Red Wing, MN) ---
    'latitude': 44.602016,
    'longitude': -92.494604,
    'map_zoom': 8,
    'location_source': 'gps',           # gps | manual
    'location_address': '',             # human-readable, set by geocode

    # --- Kiosk carousel ----------------------------------------------------
    'kiosk_cards': ['aircraft_now', 'aircraft_today', 'busiest_hour',
                    'last_aircraft', 'weather', 'top_airlines',
                    'activity_trend', 'device_info'],
    'kiosk_carousel_interval': 8,       # seconds between card auto-rotate
    'kiosk_map_interval': 15,           # seconds the map page stays visible
    'kiosk_show_map': True,

    # --- Hardware (DHT + buzzer pins) -----------------------------------
    'dht_pin': 21,
    'buzzer_pin': 18,
    'buzzer_enabled': True,
    'buzzer_threshold_temp_f': 95.0,
    'buzzer_threshold_hum': 80.0,
    'buzzer_volume': 60,        # 0-100 PWM duty cycle
    'sensor_interval': 20,      # seconds between DHT22 poll cycles
    'sensor_cache_window_sec': 90,  # how long a 'cached' reading stays valid
    'sensor_retry_attempts': 3,     # attempts inside a single poll cycle
    'sensor_retry_delay_sec': 1.5,  # delay between retry attempts
    'sensor_emit_interval': 5,      # how often app.py rebroadcasts the cache

    # --- ADS-B ingestion -----------------------------------------------
    # dump1090-fa writes its live snapshot to dump1090_json_path on a tmpfs.
    # dump1090_url is an OPTIONAL HTTP fallback for remote feeders — it MUST
    # NOT point at SkyTrack itself (port 8080) or we'll self-loop and spam
    # 404s. Default is empty so the ingester only reads the local file.
    'dump1090_json_path': '/run/dump1090-fa/aircraft.json',
    'dump1090_url': '',
    'ingest_interval': 5,       # seconds
    'sightings_retention_days': 7,
    'logs_retention_days': 30,
    'max_records': 100000,
    'ignore_helicopters': False,
    'ignore_ground_targets': False,
    'min_altitude_ft': 0,
    'signal_threshold_dbm': -100,
    'default_dashboard_time_filter': '24h',  # 1h | 6h | 24h | 7d

    # --- Hotspot / Network ----------------------------------------------
    'hotspot_ssid': 'SkyTrack-Portal',
    'hotspot_gateway': '10.4.26.89',
    'hotspot_subnet': '10.4.26.0/24',
    'hotspot_dhcp_start': '10.4.26.100',
    'hotspot_dhcp_end': '10.4.26.200',
    'hotspot_channel': 6,
    'hotspot_country': 'US',
    'hotspot_auto_start': True,
    'metered_connection': False,    # cellular metered mode
    'cellular_enabled': True,
    # Default APN for the cellular modem. The operator can change this in
    # Settings → Network → Cellular. `nrbroadband` is the SkyTrack product
    # default; env/YAML still wins.
    'cellular_apn': 'nrbroadband',
    'wifi_client_enabled': True,
    'time_sync_source': 'ntp',      # ntp | cellular | gps

    # --- Integrations (keys live in auth.json) --------------------------
    'aeroapi_enabled': False,
    'aeroapi_calls_per_hour': 10,
    'aeroapi_calls_per_day': 200,
    'aeroapi_cost_per_call': 0.01,
    'aeroapi_monthly_budget_usd': 5,
    'opensky_enabled': False,
    'opensky_poll_minutes': 10,
    'enrichment_ttl_hours': 24,
    'weather_enabled': True,
    'weather_provider': 'open-meteo',  # open-meteo (free, keyless) | openweather (key req'd) | none

    # --- Weather --------------------------------------------------------
    'weather_interval': 1800,     # 30 min

    # --- Feeders (sharing) ----------------------------------------------
    'feed_over_cellular': False,
    'feed_over_wifi_only': True,

    # --- Radar publishing (display-only in phase 1) ---------------------
    'radar_publish_enabled': False,

    # --- Auth / sessions ------------------------------------------------
    'session_timeout_hours': 2,    # sliding window
    'lockout_max_attempts': 5,
    'lockout_window_seconds': 300,

    # --- Updates & backup -----------------------------------------------
    # OTA runs against a dedicated workspace under /var/lib/skytrack — we
    # never run git commands inside /opt/skytrack (the installer rsyncs the
    # repo there without a .git directory). On apply we rsync the workspace
    # into /opt/skytrack and restart skytrack-app.
    'ota_remote': 'origin',
    'ota_branch': 'main',
    'ota_enabled': True,
    'ota_auto_check': True,
    'ota_channel': 'stable',        # stable | beta
    'ota_last_check': None,
    'ota_workspace_dir': '/var/lib/skytrack/ota-workspace',
    'ota_repo_url': 'https://github.com/QDRN1/Skytrack.git',
    # OTA git auth mode — how the workspace authenticates to the upstream.
    #   'ssh'         — use git's ssh (works with deploy keys or user keys)
    #   'https_none'  — public HTTPS, no credentials
    #   'https_token' — HTTPS with a personal-access token read from
    #                   auth.json as ota_git_token. When set, the token is
    #                   injected into the remote URL only at command time,
    #                   never persisted into the workspace's git config.
    # Today's installs use deploy keys (mode='ssh'). This is designed so
    # moving away from deploy keys is a one-line change, not a code rewrite.
    'ota_auth_mode': 'ssh',
    'update_check_enabled': True,
    'update_allow_cellular': False,
    'auto_backup_frequency': 'weekly',  # off | daily | weekly | monthly
    'backup_frequency': 'off',          # touch-shell equivalent
    'backup_keep': 5,

    # --- Paths ----------------------------------------------------------
    'data_dir': '/var/lib/skytrack',
    'geo_data_dir': '/var/lib/skytrack/geo',
    'log_dir': '/var/log/skytrack',
    'db_path': '/var/lib/skytrack/skytrack.db',
    'auth_path': '/var/lib/skytrack/auth.json',
    'device_id_path': '/var/lib/skytrack/device_id',
    'backup_dir': '/var/lib/skytrack/backups',

    # --- Display / kiosk ------------------------------------------------
    # display_rotation accepts: 0 | 90 | 180 | 270
    # (UI labels these "Normal", "Right", "Upside Down", "Left")
    # Default is 90 (right) — the SkyTrack appliance ships in portrait mode.
    'display_rotation': '90',
    'display_output': 'HDMI-1',
    'display_brightness': 100,           # 0-100
    'display_sleep_minutes': 0,          # 0 = never dim
    'display_animation_level': 'full',   # full | reduced | off
    'display_particle_density': 80,      # 0-100
    'display_fullscreen_on_boot': True,

    # --- Burn-in prevention ---------------------------------------------
    'jitter_interval': 300,       # 5 min
    'refresh_interval': 3600,     # 60 min

    # --- Alerts ---------------------------------------------------------
    'alert_no_aircraft_minutes': 30,
    'alert_cell_disconnect': True,
    'alert_wifi_disconnect': False,
    'alert_api_budget_pct': 80,    # warn at 80% of daily budget
    'alert_low_storage_pct': 90,   # warn at 90% disk usage
    # Touch-shell notification policy
    'alerts_enabled': True,
    'alert_military': True,
    'alert_emergency': True,
    'alert_heavy': False,
    'alert_watchlist': True,
    'alert_radius_nm': 25,
    'alert_cooldown_minutes': 30,
    'alert_delivery': 'both',      # toast | sound | both | off
    'alert_volume': 70,

    # --- Data pruning (touch-shell) -------------------------------------
    'data_retention_days': 30,
    'data_max_rows': 500000,
    'data_autovacuum': True,

    # --- Logging --------------------------------------------------------
    'log_level': 'INFO',

    # --- GPS / geocoding -----------------------------------------------
    'geo_offline_enabled': True,

    # --- Cloudflare Tunnel (optional remote access) --------------------
    'cloudflared_enabled': False,
    'cloudflared_tunnel_name': '',      # display name (e.g. skytrack-baycity)
    'cloudflared_hostname': '',         # public hostname once configured
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
