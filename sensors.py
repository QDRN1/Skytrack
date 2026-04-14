"""DHT22 temperature/humidity sensor with honest hardware init and fallback.

Design goals (Issue #7):

  * Real hardware is tried first — we do NOT silently stay in mock mode
    just because `config.mock_mode` is set in the defaults file. Mock mode
    is reserved for dev boxes that don't have GPIO at all.
  * Init success/failure is logged once at INFO/WARNING with a clear cause.
  * Every reading carries `source` ("dht22" | "mock" | "dht22_cached") so
    the topbar can honestly label the temperature instead of pretending
    mock data is real.
  * `last_error` is preserved on the service so operators can see the
    actual init error in Settings → Diagnostics without tailing the log.
"""

import logging
import os
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger('skytrack.sensors')


class SensorService:
    def __init__(self, config):
        self.config = config
        self.pin = int(config.get('dht_pin', 21))
        self._sensor = None
        self._last_good_read: Optional[Dict] = None
        self._source = 'mock'
        self._last_error: str = ''
        self._init_sensor()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------
    def _init_sensor(self) -> None:
        """Try to bring up the DHT22 on the configured pin.

        We only skip hardware probing when the operator has *explicitly*
        opted into mock mode AND we're not on a device that looks like a
        Pi. This way, leaving `mock_mode: true` in config.yaml on a real
        Pi doesn't accidentally mask broken hardware — hardware still
        gets initialized and reports honestly.
        """
        if self._looks_like_dev_box() and self.config.get('mock_mode'):
            self._source = 'mock'
            logger.info('Sensor: dev box + mock_mode=true → using synthetic readings')
            return

        try:
            import board           # noqa: F401
            import adafruit_dht
        except ImportError as e:
            self._source = 'mock'
            self._last_error = f'adafruit_dht/board not installed: {e}'
            logger.warning('Sensor: %s — falling back to mock readings', self._last_error)
            return

        try:
            hw_pin = getattr(board, f'D{self.pin}')
        except AttributeError:
            self._source = 'mock'
            self._last_error = f'board has no attribute D{self.pin}'
            logger.warning('Sensor: %s — check dht_pin setting', self._last_error)
            return

        try:
            # use_pulseio=False is required on Pi 4/5 Bookworm — the default
            # C extension is unreliable on newer kernels.
            self._sensor = adafruit_dht.DHT22(hw_pin, use_pulseio=False)
            self._source = 'dht22'
            logger.info('Sensor: DHT22 initialized on GPIO %d (BCM)', self.pin)
        except Exception as e:
            self._source = 'mock'
            self._last_error = f'DHT22 init failed on GPIO {self.pin}: {e}'
            logger.warning('Sensor: %s', self._last_error)

    @staticmethod
    def _looks_like_dev_box() -> bool:
        """Heuristic: are we running on something that isn't a Pi?"""
        try:
            with open('/proc/device-tree/model') as f:
                model = f.read().lower()
                if 'raspberry pi' in model:
                    return False
        except Exception:
            pass
        return not os.path.exists('/proc/device-tree/model')

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def read(self) -> Dict:
        """Return the latest sensor reading. Never raises.

        Result always includes:
          temperature_c, temperature_f, humidity, timestamp,
          mock (bool), source (str: 'dht22'|'dht22_cached'|'mock'),
          pin (int), last_error (str)
        """
        if self._sensor is None:
            return self._mock_reading(reason='no-hardware')

        try:
            temp_c = self._sensor.temperature
            humidity = self._sensor.humidity
            if temp_c is not None and humidity is not None:
                reading = {
                    'temperature_c': round(temp_c, 1),
                    'temperature_f': round(temp_c * 9 / 5 + 32, 1),
                    'humidity':      round(humidity, 1),
                    'timestamp':     datetime.now().isoformat(),
                    'mock':          False,
                    'source':        'dht22',
                    'pin':           self.pin,
                    'last_error':    '',
                }
                self._last_good_read = reading
                return reading
            # DHT2x returns None on transient glitches — serve the cache.
            return self._cached_or_mock(reason='dht22-null')
        except RuntimeError as e:
            # RuntimeError is the documented DHT transient-failure case.
            self._last_error = f'transient: {e}'
            return self._cached_or_mock(reason=self._last_error)
        except Exception as e:
            self._last_error = f'read failed: {e}'
            logger.warning('Sensor: %s', self._last_error)
            return self._cached_or_mock(reason=self._last_error)

    def _cached_or_mock(self, reason: str) -> Dict:
        if self._last_good_read:
            reading = dict(self._last_good_read)
            reading['source']     = 'dht22_cached'
            reading['last_error'] = reason
            reading['timestamp']  = datetime.now().isoformat()
            return reading
        return self._mock_reading(reason=reason)

    def _mock_reading(self, reason: str = '') -> Dict:
        return {
            'temperature_c': 22.4,
            'temperature_f': 72.3,
            'humidity':      55.2,
            'timestamp':     datetime.now().isoformat(),
            'mock':          True,
            'source':        'mock',
            'pin':           self.pin,
            'last_error':    reason or self._last_error,
        }

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def source(self) -> str:
        return self._source

    @property
    def available(self) -> bool:
        return self._sensor is not None

    @property
    def last_error(self) -> str:
        return self._last_error

    def selfcheck(self) -> Dict:
        if self._sensor is None:
            return {
                'ok': False,
                'source': self._source,
                'message': self._last_error or 'DHT22 not initialized (mock readings)',
            }
        data = self.read()
        if data and data.get('source') == 'dht22':
            return {
                'ok': True,
                'source': 'dht22',
                'message': f"Temp={data['temperature_f']}°F  Hum={data['humidity']}%",
            }
        return {
            'ok': False,
            'source': data.get('source', 'mock'),
            'message': data.get('last_error') or 'DHT22 read returned no data',
        }
