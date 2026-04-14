"""Buzzer control on a configurable GPIO pin with PWM volume.

Honest-hardware policy (Issue #8):
  * Real GPIO is tried first unless the device is clearly not a Pi.
  * `available()` reflects the *actual* init outcome — if the PWM starts,
    it's True. If init fails, we report it once at WARNING and stay False.
  * `last_error` is exposed so Settings → Hardware can show the real
    reason a buzzer test would fail before the operator presses the
    button.
  * `test()` drives a short audible pattern and returns a structured
    result the UI can surface without exposing any shell output.
"""

import logging
import os
import threading
import time
from typing import Dict

logger = logging.getLogger('skytrack.buzzer')


class Buzzer:
    def __init__(self, config):
        self.config = config
        self.pin = int(config.get('buzzer_pin', 18))
        self.enabled = bool(config.get('buzzer_enabled', True))
        self.volume = max(0, min(100, int(config.get('buzzer_volume', 60))))
        self._gpio = None
        self._pwm = None
        self._lock = threading.Lock()
        self._available = False
        self._last_error = ''
        self._init_gpio()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_dev_box() -> bool:
        try:
            with open('/proc/device-tree/model') as f:
                model = f.read().lower()
                if 'raspberry pi' in model:
                    return False
        except Exception:
            pass
        return not os.path.exists('/proc/device-tree/model')

    def _init_gpio(self) -> None:
        if self._looks_like_dev_box() and self.config.get('mock_mode'):
            self._last_error = 'dev box + mock_mode=true'
            logger.info('Buzzer: %s — GPIO skipped, actions are no-ops', self._last_error)
            return

        try:
            import RPi.GPIO as GPIO
        except ImportError as e:
            self._last_error = f'RPi.GPIO not installed: {e}'
            logger.warning('Buzzer: %s — disabling actions', self._last_error)
            return

        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.OUT)
            self._gpio = GPIO
            self._pwm = GPIO.PWM(self.pin, 2000)  # 2 kHz tone
            self._pwm.start(0)
            self._available = True
            self._last_error = ''
            logger.info('Buzzer: initialized on GPIO %d at 2 kHz', self.pin)
        except Exception as e:
            self._last_error = f'GPIO init failed on pin {self.pin}: {e}'
            logger.warning('Buzzer: %s', self._last_error)

    def shutdown(self) -> None:
        with self._lock:
            try:
                if self._pwm:
                    self._pwm.stop()
                if self._gpio:
                    self._gpio.cleanup(self.pin)
            except Exception as e:
                logger.debug('Buzzer: shutdown error: %s', e)
            self._pwm = None
            self._gpio = None
            self._available = False

    # ------------------------------------------------------------------
    # Public controls
    # ------------------------------------------------------------------
    def available(self) -> bool:
        return self._available

    @property
    def last_error(self) -> str:
        return self._last_error

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, int(volume)))

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not self.enabled:
            self.off()

    def on(self) -> None:
        if not self.enabled or not self._available or not self._pwm:
            return
        with self._lock:
            try:
                self._pwm.ChangeDutyCycle(self.volume)
            except Exception as e:
                logger.debug('Buzzer: on error: %s', e)

    def off(self) -> None:
        if not self._pwm:
            return
        with self._lock:
            try:
                self._pwm.ChangeDutyCycle(0)
            except Exception as e:
                logger.debug('Buzzer: off error: %s', e)

    def beep(self, duration_s: float = 0.25) -> None:
        if not self.enabled or not self._available:
            return
        self.on()
        time.sleep(max(0.01, min(2.0, duration_s)))
        self.off()

    def test(self) -> Dict:
        """Sound a short three-beep pattern. Used by Settings → Hardware."""
        if not self._available:
            return {
                'ok': False,
                'available': False,
                'message': self._last_error or 'Buzzer hardware not available',
            }
        if not self.enabled:
            return {
                'ok': False,
                'available': True,
                'message': 'Buzzer is disabled in Settings — enable it before testing.',
            }
        try:
            for _ in range(3):
                self.beep(0.15)
                time.sleep(0.1)
            return {'ok': True, 'available': True, 'message': 'Buzzer test OK — three beeps sent'}
        except Exception as e:
            return {'ok': False, 'available': True, 'message': str(e)}

    # ------------------------------------------------------------------
    # Threshold-driven alert (called from sensor loop)
    # ------------------------------------------------------------------
    def evaluate(self, sensor_reading: dict) -> Dict:
        """Compare the latest sensor reading against thresholds.

        Returns a dict describing whether an alert fired, plus the buzzer's
        current health (`available`, `enabled`, `volume`, `last_error`).
        """
        threshold_temp = float(self.config.get('buzzer_threshold_temp_f', 95.0))
        threshold_hum  = float(self.config.get('buzzer_threshold_hum', 80.0))
        temp_f   = sensor_reading.get('temperature_f')
        humidity = sensor_reading.get('humidity')

        triggered = False
        reasons = []
        if temp_f is not None and temp_f >= threshold_temp:
            triggered = True
            reasons.append(f'temp {temp_f}°F ≥ {threshold_temp}°F')
        if humidity is not None and humidity >= threshold_hum:
            triggered = True
            reasons.append(f'humidity {humidity}% ≥ {threshold_hum}%')

        if triggered and self.enabled and self._available:
            self.beep(0.4)

        return {
            'triggered':        triggered,
            'reason':           ', '.join(reasons),
            'temp_threshold_f': threshold_temp,
            'hum_threshold':    threshold_hum,
            'available':        self._available,
            'enabled':          self.enabled,
            'volume':           self.volume,
            'last_error':       self._last_error,
        }
