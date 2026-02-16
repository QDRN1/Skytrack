"""WeatherAPI.com integration with caching and mock fallback."""

import time
import logging
import random
from datetime import datetime

logger = logging.getLogger('skytrack.weather')

# Stable mock data so the UI doesn't flicker randomly
_MOCK_WEATHER = {
    'current': {
        'temp_f': 34,
        'condition': 'Partly Cloudy',
        'icon': '',
        'high_f': 38,
        'low_f': 22,
    },
    'forecast': [
        {
            'day': 'Tue',
            'icon': '',
            'high_f': 41,
            'low_f': 28,
            'condition': 'Sunny',
        },
        {
            'day': 'Wed',
            'icon': '',
            'high_f': 36,
            'low_f': 19,
            'condition': 'Snow',
        },
    ],
    'cached': False,
    'offline': False,
    'mock': True,
    'last_update': None,  # filled at runtime
}


class WeatherService:
    def __init__(self, config):
        self.config = config
        self.api_key = config.get('weather_api_key', '')
        self.lat = config.get('latitude', 44.602016)
        self.lon = config.get('longitude', -92.494604)
        self._cache = None
        self._cache_time = 0
        self._cache_ttl = config.get('weather_interval', 1800)

    def get_weather(self):
        now = time.time()

        # Return cache if still fresh
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return {**self._cache, 'cached': True}

        # Try real API
        if not self.config.get('mock_mode') and self.api_key:
            data = self._fetch_weather()
            if data:
                self._cache = data
                self._cache_time = now
                return data
            # API failed: return stale cache if we have one
            if self._cache:
                return {**self._cache, 'cached': True, 'offline': True}

        # Mock mode or no API key
        mock = self._mock_weather()
        self._cache = mock
        self._cache_time = now
        return mock

    def _fetch_weather(self):
        try:
            import requests
            resp = requests.get(
                'http://api.weatherapi.com/v1/forecast.json',
                params={
                    'key': self.api_key,
                    'q': f'{self.lat},{self.lon}',
                    'days': 3,
                    'aqi': 'no',
                },
                timeout=10,
            )
            resp.raise_for_status()
            raw = resp.json()

            current = raw['current']
            today = raw['forecast']['forecastday'][0]['day']
            forecast_days = raw['forecast']['forecastday'][1:3]

            return {
                'current': {
                    'temp_f': current['temp_f'],
                    'condition': current['condition']['text'],
                    'icon': current['condition']['icon'],
                    'high_f': today['maxtemp_f'],
                    'low_f': today['mintemp_f'],
                },
                'forecast': [
                    {
                        'day': datetime.strptime(d['date'], '%Y-%m-%d').strftime('%a'),
                        'icon': d['day']['condition']['icon'],
                        'high_f': d['day']['maxtemp_f'],
                        'low_f': d['day']['mintemp_f'],
                        'condition': d['day']['condition']['text'],
                    }
                    for d in forecast_days
                ],
                'cached': False,
                'offline': False,
                'mock': False,
                'last_update': datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error(f'Weather API error: {e}')
            return None

    def _mock_weather(self):
        data = {**_MOCK_WEATHER, 'last_update': datetime.now().isoformat()}
        data['current'] = dict(data['current'])
        data['forecast'] = [dict(d) for d in data['forecast']]
        return data

    def selfcheck(self):
        if self.config.get('mock_mode'):
            return {'ok': True, 'message': 'Mock mode active'}
        if not self.api_key:
            return {'ok': False, 'message': 'No weather API key configured'}
        try:
            data = self._fetch_weather()
            if data:
                return {'ok': True, 'message': 'WeatherAPI.com reachable'}
            return {'ok': False, 'message': 'WeatherAPI.com returned no data'}
        except Exception as e:
            return {'ok': False, 'message': str(e)}
