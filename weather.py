"""Weather provider service.

Supports three modes, selected by ``weather_provider`` in config:

    open-meteo   — free, keyless, api.open-meteo.com (default)
    openweather  — OpenWeatherMap, requires ``openweather_api_key`` secret
    none         — disabled; returns a mock card stamped with provider="off"

The service hides provider differences behind a single ``get_weather()``
contract returning::

    {
        "current":  {"temp_f", "condition", "icon", "high_f", "low_f"},
        "forecast": [{"day", "icon", "high_f", "low_f", "condition"}, ...],
        "provider": "open-meteo" | "openweather" | "mock" | "off",
        "cached":   bool,
        "offline":  bool,
        "mock":     bool,
        "last_update": ISO-8601 string,
    }

Failures always degrade to cached data first, then to a stable mock, so
the dashboard weather card is never blank and never shows a crash trace.
"""

import logging
import time
from datetime import datetime

logger = logging.getLogger('skytrack.weather')

# Stable fallback so the dashboard card never goes blank even without
# internet or an API key. Stamp "mock": True so the UI can annotate it.
_MOCK_WEATHER = {
    'current': {
        'temp_f': 34,
        'condition': 'Partly Cloudy',
        'icon': '',
        'high_f': 38,
        'low_f': 22,
    },
    'forecast': [
        {'day': 'Tue', 'icon': '', 'high_f': 41, 'low_f': 28, 'condition': 'Sunny'},
        {'day': 'Wed', 'icon': '', 'high_f': 36, 'low_f': 19, 'condition': 'Snow'},
    ],
}


# Open-Meteo numeric weather codes → (condition text, icon hint).
# Source: https://open-meteo.com/en/docs — WMO 4677 weather codes.
_OPEN_METEO_CODES = {
    0:  ('Clear',              'clear'),
    1:  ('Mainly Clear',       'clear'),
    2:  ('Partly Cloudy',      'partly-cloudy'),
    3:  ('Overcast',           'cloudy'),
    45: ('Fog',                'fog'),
    48: ('Freezing Fog',       'fog'),
    51: ('Light Drizzle',      'drizzle'),
    53: ('Drizzle',            'drizzle'),
    55: ('Dense Drizzle',      'drizzle'),
    56: ('Light Freezing Drizzle', 'drizzle'),
    57: ('Freezing Drizzle',   'drizzle'),
    61: ('Light Rain',         'rain'),
    63: ('Rain',               'rain'),
    65: ('Heavy Rain',         'rain'),
    66: ('Light Freezing Rain', 'sleet'),
    67: ('Freezing Rain',      'sleet'),
    71: ('Light Snow',         'snow'),
    73: ('Snow',               'snow'),
    75: ('Heavy Snow',         'snow'),
    77: ('Snow Grains',        'snow'),
    80: ('Light Showers',      'rain'),
    81: ('Showers',            'rain'),
    82: ('Violent Showers',    'rain'),
    85: ('Light Snow Showers', 'snow'),
    86: ('Snow Showers',       'snow'),
    95: ('Thunderstorm',       'storm'),
    96: ('Thunderstorm w/ Hail', 'storm'),
    99: ('Severe Thunderstorm', 'storm'),
}


class WeatherService:
    """Provider-agnostic weather facade with caching and mock fallback."""

    def __init__(self, config):
        self.config = config
        self.lat = config.get('latitude', 44.602016)
        self.lon = config.get('longitude', -92.494604)
        self._cache = None
        self._cache_time = 0
        self._cache_ttl = int(config.get('weather_interval', 1800) or 1800)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get_weather(self):
        """Return a weather payload, using cache + provider + mock fallback."""
        now = time.time()

        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return {**self._cache, 'cached': True}

        provider = (self.config.get('weather_provider') or 'open-meteo').lower()

        if provider == 'none' or not self.config.get('weather_enabled', True):
            data = self._mock_weather()
            data['provider'] = 'off'
            return self._cache_and_return(data)

        fetcher = {
            'open-meteo':  self._fetch_open_meteo,
            'openmeteo':   self._fetch_open_meteo,
            'openweather': self._fetch_openweather,
        }.get(provider)

        if fetcher is None:
            logger.warning('Unknown weather_provider %r, using mock', provider)
            return self._cache_and_return(self._mock_weather())

        data = None
        try:
            data = fetcher()
        except Exception as e:
            logger.warning('Weather fetch (%s) failed: %s', provider, e)

        if data:
            return self._cache_and_return(data)

        # Fetch failed — prefer stale cache over mock if we have one.
        if self._cache:
            return {**self._cache, 'cached': True, 'offline': True}
        return self._cache_and_return(self._mock_weather())

    def selfcheck(self):
        provider = (self.config.get('weather_provider') or 'open-meteo').lower()
        if provider == 'none':
            return {'ok': True, 'message': 'Weather provider disabled'}
        try:
            if provider.startswith('openmeteo') or provider == 'open-meteo':
                data = self._fetch_open_meteo()
                return (
                    {'ok': True, 'message': 'Open-Meteo reachable'}
                    if data else
                    {'ok': False, 'message': 'Open-Meteo returned no data'}
                )
            if provider == 'openweather':
                if not self._openweather_key():
                    return {'ok': False,
                            'message': 'OpenWeather selected but no API key is set '
                                       '(Settings → Integrations)'}
                data = self._fetch_openweather()
                return (
                    {'ok': True, 'message': 'OpenWeather reachable'}
                    if data else
                    {'ok': False, 'message': 'OpenWeather returned no data'}
                )
        except Exception as e:
            return {'ok': False, 'message': str(e)}
        return {'ok': False, 'message': f'Unknown provider {provider}'}

    # ------------------------------------------------------------------
    # Cache helper
    # ------------------------------------------------------------------

    def _cache_and_return(self, data):
        self._cache = data
        self._cache_time = time.time()
        return data

    # ------------------------------------------------------------------
    # Provider: Open-Meteo (free, keyless)
    # ------------------------------------------------------------------

    def _fetch_open_meteo(self):
        import requests
        params = {
            'latitude': self.lat,
            'longitude': self.lon,
            'current': 'temperature_2m,weather_code',
            'daily': 'weather_code,temperature_2m_max,temperature_2m_min',
            'temperature_unit': 'fahrenheit',
            'wind_speed_unit': 'kn',
            'timezone': 'auto',
            'forecast_days': 4,
        }
        resp = requests.get(
            'https://api.open-meteo.com/v1/forecast',
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()

        current_raw = raw.get('current') or {}
        daily = raw.get('daily') or {}
        codes = daily.get('weather_code') or []
        highs = daily.get('temperature_2m_max') or []
        lows  = daily.get('temperature_2m_min') or []
        days  = daily.get('time') or []

        today_high = highs[0] if highs else current_raw.get('temperature_2m')
        today_low  = lows[0] if lows else current_raw.get('temperature_2m')

        cur_code = current_raw.get('weather_code', 0)
        cur_condition, cur_icon = _OPEN_METEO_CODES.get(
            cur_code, (f'Code {cur_code}', 'unknown'),
        )

        forecast = []
        for i in range(1, min(3, len(days))):
            code = codes[i] if i < len(codes) else 0
            cond, icon = _OPEN_METEO_CODES.get(code, (f'Code {code}', 'unknown'))
            try:
                day_label = datetime.strptime(days[i], '%Y-%m-%d').strftime('%a')
            except Exception:
                day_label = days[i]
            forecast.append({
                'day':       day_label,
                'icon':      icon,
                'high_f':    round(highs[i]) if i < len(highs) and highs[i] is not None else None,
                'low_f':     round(lows[i])  if i < len(lows)  and lows[i]  is not None else None,
                'condition': cond,
            })

        return {
            'current': {
                'temp_f':    round(current_raw.get('temperature_2m')) if current_raw.get('temperature_2m') is not None else None,
                'condition': cur_condition,
                'icon':      cur_icon,
                'high_f':    round(today_high) if today_high is not None else None,
                'low_f':     round(today_low)  if today_low  is not None else None,
            },
            'forecast':    forecast,
            'provider':    'open-meteo',
            'cached':      False,
            'offline':     False,
            'mock':        False,
            'last_update': datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Provider: OpenWeatherMap
    # ------------------------------------------------------------------

    def _openweather_key(self):
        # First look in config (legacy), then in auth.json secrets.
        key = (self.config.get('openweather_api_key') or '').strip()
        if key:
            return key
        try:
            import auth as auth_lib
            return (auth_lib.get_secret('openweather_api_key') or '').strip()
        except Exception:
            return ''

    def _fetch_openweather(self):
        key = self._openweather_key()
        if not key:
            return None
        import requests
        resp = requests.get(
            'https://api.openweathermap.org/data/2.5/onecall',
            params={
                'lat':     self.lat,
                'lon':     self.lon,
                'exclude': 'minutely,hourly,alerts',
                'units':   'imperial',
                'appid':   key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        current = raw.get('current') or {}
        daily = raw.get('daily') or []

        def _ow_entry(d):
            wx = (d.get('weather') or [{}])[0]
            temp = d.get('temp') or {}
            icon_code = wx.get('icon') or ''
            return {
                'day':       datetime.fromtimestamp(d.get('dt', 0)).strftime('%a'),
                'icon':      f'https://openweathermap.org/img/wn/{icon_code}@2x.png' if icon_code else '',
                'high_f':    round(temp.get('max')) if temp.get('max') is not None else None,
                'low_f':     round(temp.get('min')) if temp.get('min') is not None else None,
                'condition': wx.get('description', '').title(),
            }

        cur_wx = (current.get('weather') or [{}])[0]
        today  = daily[0] if daily else {}
        today_temp = today.get('temp') or {}

        return {
            'current': {
                'temp_f':    round(current.get('temp')) if current.get('temp') is not None else None,
                'condition': cur_wx.get('description', '').title(),
                'icon':      f"https://openweathermap.org/img/wn/{cur_wx.get('icon','')}@2x.png" if cur_wx.get('icon') else '',
                'high_f':    round(today_temp.get('max')) if today_temp.get('max') is not None else None,
                'low_f':     round(today_temp.get('min')) if today_temp.get('min') is not None else None,
            },
            'forecast':    [_ow_entry(d) for d in daily[1:3]],
            'provider':    'openweather',
            'cached':      False,
            'offline':     False,
            'mock':        False,
            'last_update': datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Mock fallback
    # ------------------------------------------------------------------

    def _mock_weather(self):
        data = {
            'current':     dict(_MOCK_WEATHER['current']),
            'forecast':    [dict(d) for d in _MOCK_WEATHER['forecast']],
            'provider':    'mock',
            'cached':      False,
            'offline':     False,
            'mock':        True,
            'last_update': datetime.now().isoformat(),
        }
        return data
