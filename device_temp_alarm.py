"""Device temperature alarm — monitors CPU/SoC temp and triggers buzzer.

The alarm pattern is: beep beep beep beeeeeeep, 30s pause, repeat.
When active, the kiosk shows a red overlay and the web portal shows
a critical banner. The alarm auto-clears when temperature drops below
the threshold minus a 5-degree hysteresis band.
"""

import logging
import threading
import time

logger = logging.getLogger('skytrack.device_temp_alarm')


class DeviceTempAlarm:
    def __init__(self, config, buzzer):
        self.config = config
        self.buzzer = buzzer
        self._active = False
        self._cpu_temp_c = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    @property
    def threshold_c(self):
        try:
            v = float(self.config.get('device_temp_alarm_c', 80))
            return max(50.0, min(100.0, v))
        except (TypeError, ValueError):
            return 80.0

    @property
    def hysteresis_c(self):
        return 5.0

    def is_active(self):
        with self._lock:
            return self._active

    def cpu_temp_c(self):
        with self._lock:
            return self._cpu_temp_c

    def status(self):
        with self._lock:
            return {
                'active': self._active,
                'cpu_temp_c': self._cpu_temp_c,
                'threshold_c': self.threshold_c,
            }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='device-temp-alarm')
        self._thread.start()
        logger.info('Device temp alarm started (threshold=%s°C)', self.threshold_c)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _read_cpu_temp(self):
        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                return int(f.read().strip()) / 1000.0
        except Exception:
            return None

    def _alarm_pattern(self):
        """beep beep beep beeeeeeep"""
        if not self.buzzer or not self.buzzer.enabled or not self.buzzer.available():
            return
        for _ in range(3):
            self.buzzer.beep(0.15)
            time.sleep(0.15)
        self.buzzer.beep(0.8)

    def _loop(self):
        first = True
        while not self._stop.is_set():
            temp = self._read_cpu_temp()
            with self._lock:
                self._cpu_temp_c = temp

            if first:
                first = False
                threshold = self.threshold_c
                logger.info('Device temp alarm: first read %.1f°C, threshold %.1f°C',
                            temp if temp is not None else -1, threshold)

            if temp is not None:
                threshold = self.threshold_c
                with self._lock:
                    was_active = self._active
                    if temp >= threshold:
                        self._active = True
                    elif was_active and temp < (threshold - self.hysteresis_c):
                        self._active = False
                        logger.info('Device temp alarm cleared: %.1f°C < %.1f°C',
                                    temp, threshold - self.hysteresis_c)

                if self._active:
                    if not was_active:
                        logger.warning('Device temp alarm ACTIVE: %.1f°C >= %.1f°C',
                                       temp, threshold)
                    self._alarm_pattern()
                    self._stop.wait(30)
                    continue

            self._stop.wait(10)
