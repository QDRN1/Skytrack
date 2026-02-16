"""DHT22 temperature/humidity sensor reader with graceful fallback."""

import logging
import random
from datetime import datetime

logger = logging.getLogger('skytrack.sensors')


class SensorService:
    def __init__(self, config):
        self.config = config
        self.pin = config.get('dht_pin', 21)
        self._sensor = None
        self._last_good_read = None
        self._init_sensor()

    def _init_sensor(self):
        if self.config.get('mock_mode'):
            logger.info('Sensor running in mock mode')
            return
        try:
            import board
            import adafruit_dht
            hw_pin = getattr(board, f'D{self.pin}')
            self._sensor = adafruit_dht.DHT22(hw_pin)
            logger.info(f'DHT22 initialized on GPIO {self.pin} (BCM)')
        except ImportError:
            logger.warning('adafruit_dht / board not installed; falling back to mock mode')
        except Exception as e:
            logger.warning(f'Could not initialize DHT22: {e}; falling back to mock mode')

    def read(self):
        if self.config.get('mock_mode') or self._sensor is None:
            return self._mock_read()

        try:
            temp_c = self._sensor.temperature
            humidity = self._sensor.humidity
            if temp_c is not None and humidity is not None:
                self._last_good_read = {
                    'temperature_c': round(temp_c, 1),
                    'temperature_f': round(temp_c * 9 / 5 + 32, 1),
                    'humidity': round(humidity, 1),
                    'timestamp': datetime.now().isoformat(),
                    'mock': False,
                }
                return self._last_good_read

            logger.warning('DHT22 returned None values')
            return self._last_good_read or self._mock_read()
        except RuntimeError as e:
            # DHT sensors throw RuntimeError on transient read failures
            logger.debug(f'DHT22 transient read error: {e}')
            return self._last_good_read or self._mock_read()
        except Exception as e:
            logger.warning(f'DHT22 read error: {e}')
            return self._last_good_read or self._mock_read()

    def _mock_read(self):
        return {
            'temperature_c': 22.4,
            'temperature_f': 72.3,
            'humidity': 55.2,
            'timestamp': datetime.now().isoformat(),
            'mock': True,
        }

    def selfcheck(self):
        if self.config.get('mock_mode'):
            return {'ok': True, 'message': 'Mock mode active'}
        if self._sensor is None:
            return {'ok': False, 'message': 'DHT22 sensor not initialized'}
        try:
            data = self.read()
            if data and not data.get('mock'):
                return {'ok': True, 'message': f"Temp={data['temperature_f']}F Hum={data['humidity']}%"}
            return {'ok': False, 'message': 'Sensor read fell back to mock data'}
        except Exception as e:
            return {'ok': False, 'message': str(e)}
