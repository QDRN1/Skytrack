"""Buzzer control on a configurable GPIO pin with PWM volume.

Graceful fallback: if RPi.GPIO is not available (dev box, mock mode), all
operations become no-ops and `available()` returns False. Settings UI shows
the pin as disabled in that case.
"""

import logging
import threading
import time

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
        self._init_gpio()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _init_gpio(self):
        if self.config.get('mock_mode'):
            logger.info('Buzzer mock mode (GPIO not initialized)')
            return
        try:
            import RPi.GPIO as GPIO
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.OUT)
            self._gpio = GPIO
            self._pwm = GPIO.PWM(self.pin, 2000)  # 2 kHz tone
            self._pwm.start(0)
            self._available = True
            logger.info('Buzzer initialized on GPIO %d', self.pin)
        except ImportError:
            logger.info('RPi.GPIO not installed; buzzer disabled')
        except Exception as e:
            logger.warning('Could not initialize buzzer GPIO %d: %s', self.pin, e)

    def shutdown(self):
        with self._lock:
            try:
                if self._pwm:
                    self._pwm.stop()
                if self._gpio:
                    self._gpio.cleanup(self.pin)
            except Exception as e:
                logger.debug('buzzer shutdown error: %s', e)
            self._pwm = None
            self._gpio = None
            self._available = False

    # ------------------------------------------------------------------
    # Public controls
    # ------------------------------------------------------------------
    def available(self) -> bool:
        return self._available

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
                logger.debug('buzzer on error: %s', e)

    def off(self) -> None:
        if not self._pwm:
            return
        with self._lock:
            try:
                self._pwm.ChangeDutyCycle(0)
            except Exception as e:
                logger.debug('buzzer off error: %s', e)

    def beep(self, duration_s: float = 0.25) -> None:
        if not self.enabled or not self._available:
            return
        self.on()
        time.sleep(max(0.01, min(2.0, duration_s)))
        self.off()

    def test(self) -> dict:
        """Settings → Hardware → Test button."""
        if not self._available:
            return {'ok': False, 'message': 'Buzzer hardware not available'}
        try:
            for _ in range(3):
                self.beep(0.15)
                time.sleep(0.1)
            return {'ok': True, 'message': 'Buzzer test OK'}
        except Exception as e:
            return {'ok': False, 'message': str(e)}

    # ------------------------------------------------------------------
    # Threshold-driven alert (called from sensor loop)
    # ------------------------------------------------------------------
    def evaluate(self, sensor_reading: dict) -> dict:
        """Compare the latest sensor reading against thresholds.

        Returns {'triggered': bool, 'reason': str, ...}.
        """
        threshold_temp = float(self.config.get('buzzer_threshold_temp_f', 95.0))
        threshold_hum = float(self.config.get('buzzer_threshold_hum', 80.0))
        temp_f = sensor_reading.get('temperature_f')
        humidity = sensor_reading.get('humidity')

        triggered = False
        reasons = []
        if temp_f is not None and temp_f >= threshold_temp:
            triggered = True
            reasons.append(f'temp {temp_f}°F ≥ {threshold_temp}°F')
        if humidity is not None and humidity >= threshold_hum:
            triggered = True
            reasons.append(f'humidity {humidity}% ≥ {threshold_hum}%')

        if triggered and self.enabled:
            self.beep(0.4)

        return {
            'triggered': triggered,
            'reason': ', '.join(reasons),
            'temp_threshold_f': threshold_temp,
            'hum_threshold': threshold_hum,
            'available': self._available,
            'enabled': self.enabled,
            'volume': self.volume,
        }
