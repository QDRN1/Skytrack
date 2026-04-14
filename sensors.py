"""DHT22 temperature/humidity sensor with single-owner polling + caching.

Design goals:

  * Real hardware is tried first — we do NOT silently stay in mock mode
    just because `config.mock_mode` is set in the defaults file. Mock mode
    is reserved for dev boxes that don't have GPIO at all.

  * The DHT22 spec is "one read every two seconds, max" but in practice
    on a Pi 4/5 with use_pulseio=False the AM2302 chip is even noisier
    than that — checksum errors and partial-buffer errors are normal,
    intermittent, and not a fault. The right pattern is:

        - one owner thread polls the hardware on a slow cadence (default
          20s)
        - each cycle does up to 3 attempts with a small delay between
          retries (1.5s)
        - the result of that cycle is cached and exposed via read()
        - every other code path (HTTP endpoints, websocket emitter,
          Quick Check, Long Check) reads the *cache* and never touches
          the hardware directly

    This is the difference between a sensor that says "checksum did not
    validate" 50× per minute (because every API call hammered the GPIO)
    and a sensor that says "real, 74.7°F, 46.6%" with the occasional
    short blip into 'cached' that the operator doesn't even notice.

  * `source` (used by the API and Quick Check) is one of:

        real    – most recent poll cycle succeeded
        cached  – most recent poll cycle failed but we have a previous
                  good read inside `sensor_cache_window_sec`
        error   – we have no usable reading at all (poll failing AND
                  cache stale OR sensor was never read successfully)
        mock    – hardware genuinely absent (init failed or dev box +
                  mock_mode)

  * `last_error` is preserved on the service so operators can see the
    actual init / read error in Settings → Diagnostics and via
    /api/hardware/summary without tailing the log.
"""

import logging
import os
import threading
import time
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger('skytrack.sensors')


class SensorService:
    def __init__(self, config):
        self.config = config
        self.pin = int(config.get('dht_pin', 21))
        # Polling cadence — DHT22 is rated for one read every 2s but in
        # practice on a Pi 4/5 with adafruit_dht use_pulseio=False the
        # error rate goes way down if you give it more breathing room.
        self.poll_interval_sec = max(5, int(config.get('sensor_interval', 20)))
        # How fresh a previous good read has to be to count as 'cached'
        # instead of 'error'. Default is generous — three failed cycles
        # before we admit total error.
        self.cache_window_sec = max(
            self.poll_interval_sec * 2,
            int(config.get('sensor_cache_window_sec', 90)),
        )
        # Per-cycle retry tuning — small enough that one full cycle
        # (3 attempts × 1.5s) still finishes inside the gap between
        # consecutive emit ticks in app.py's _sensor_loop.
        self.retry_attempts = max(1, int(config.get('sensor_retry_attempts', 3)))
        self.retry_delay_sec = max(0.5, float(config.get('sensor_retry_delay_sec', 1.5)))

        self._sensor = None
        self._last_good_read: Optional[Dict] = None
        self._last_good_at_monotonic: float = 0.0
        self._latest_reading: Dict = self._mock_reading(reason='startup')
        self._last_error: str = ''
        # State tracked by the poller — exposed on every read() result so
        # consumers don't have to re-derive it from timestamps.
        self._state: str = 'mock'

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._init_sensor()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------
    def _init_sensor(self) -> None:
        """Try to bring up the DHT22 on the configured pin.

        We only skip hardware probing when the operator has *explicitly*
        opted into mock mode AND we're not on a device that looks like a
        Pi. Leaving `mock_mode: true` in config.yaml on a real Pi does
        NOT mask broken hardware — hardware still gets initialized and
        reports honestly.
        """
        if self._looks_like_dev_box() and self.config.get('mock_mode'):
            self._state = 'mock'
            self._latest_reading = self._mock_reading(reason='dev box + mock_mode')
            logger.info('Sensor: dev box + mock_mode=true → using synthetic readings')
            return

        try:
            import board           # noqa: F401
            import adafruit_dht
        except ImportError as e:
            self._state = 'mock'
            self._last_error = f'adafruit_dht/board not installed: {e}'
            self._latest_reading = self._mock_reading(reason=self._last_error)
            logger.warning('Sensor: %s — falling back to mock readings', self._last_error)
            return

        try:
            hw_pin = getattr(board, f'D{self.pin}')
        except AttributeError:
            self._state = 'mock'
            self._last_error = f'board has no attribute D{self.pin}'
            self._latest_reading = self._mock_reading(reason=self._last_error)
            logger.warning('Sensor: %s — check dht_pin setting', self._last_error)
            return

        try:
            # use_pulseio=False is required on Pi 4/5 Bookworm — the default
            # C extension is unreliable on newer kernels.
            self._sensor = adafruit_dht.DHT22(hw_pin, use_pulseio=False)
            # We do NOT seed `_state='real'` here — that's a lie until
            # the first successful poll lands. Stay 'mock' until then so
            # Quick Check doesn't claim "real, no temperature" during the
            # ~20s it takes for the first poll cycle to complete.
            logger.info(
                'Sensor: DHT22 initialized on GPIO %d (BCM), poll=%ds, retries=%d, cache_window=%ds',
                self.pin, self.poll_interval_sec, self.retry_attempts, self.cache_window_sec,
            )
        except Exception as e:
            self._state = 'mock'
            self._last_error = f'DHT22 init failed on GPIO {self.pin}: {e}'
            self._latest_reading = self._mock_reading(reason=self._last_error)
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
    # Background poller — single owner of the GPIO pin
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the background polling thread (idempotent)."""
        if self._sensor is None:
            # No hardware to poll — read() just returns the mock dict
            # forever. Don't waste a thread on it.
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._poll_loop,
                name='dht22-poll',
                daemon=True,
            )
            self._thread.start()
        logger.info('Sensor: background poll thread started')

    def stop(self) -> None:
        self._stop.set()

    def _poll_loop(self) -> None:
        # Stagger the first poll by a couple seconds so we don't compete
        # with the buzzer GPIO init that's happening on app startup.
        time.sleep(2.0)
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                # Hard belt-and-braces — _poll_once() is supposed to
                # never raise, but if a wrapped library decides to throw
                # something exotic we don't want the entire poll thread
                # to die silently.
                logger.warning('Sensor: poll loop unexpected error: %s', e)
            # sleep in 0.25s slices so stop() takes effect quickly.
            slept = 0.0
            while slept < self.poll_interval_sec and not self._stop.is_set():
                time.sleep(0.25)
                slept += 0.25

    def _poll_once(self) -> None:
        """Run one poll cycle: up to N retries, then update the cache."""
        attempts = 0
        last_err = ''
        while attempts < self.retry_attempts and not self._stop.is_set():
            attempts += 1
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
                        'source':        'real',
                        'state':         'real',
                        'pin':           self.pin,
                        'last_error':    '',
                        'attempts':      attempts,
                    }
                    with self._lock:
                        self._last_good_read = reading
                        self._last_good_at_monotonic = time.monotonic()
                        self._latest_reading = reading
                        self._state = 'real'
                        self._last_error = ''
                    return
                last_err = 'sensor returned None (partial buffer)'
            except RuntimeError as e:
                # adafruit_dht raises RuntimeError("Checksum did not
                # validate. Try again.") and similar transient errors.
                # Documented behaviour, not a fault.
                last_err = f'transient: {e}'
            except Exception as e:
                last_err = f'read failed: {e}'
                logger.debug('Sensor: %s', last_err)
            # Sleep between retries — but only if we have another
            # attempt left in this cycle.
            if attempts < self.retry_attempts:
                time.sleep(self.retry_delay_sec)

        # All attempts in this cycle failed. Decide whether to report
        # 'cached' (have a recent good read) or 'error' (no usable data).
        with self._lock:
            self._last_error = last_err or 'unknown DHT22 read failure'
            age = time.monotonic() - self._last_good_at_monotonic
            if self._last_good_read and age <= self.cache_window_sec:
                cached = dict(self._last_good_read)
                cached['source']     = 'cached'
                cached['state']      = 'cached'
                cached['last_error'] = self._last_error
                cached['timestamp']  = datetime.now().isoformat()
                cached['cache_age_sec'] = round(age, 1)
                cached['attempts']   = attempts
                self._latest_reading = cached
                self._state = 'cached'
            else:
                # No usable data — report error honestly. Keep
                # temperature_f / humidity null so the topbar can dim
                # the row instead of showing a stale value as if it
                # were live.
                self._latest_reading = {
                    'temperature_c': None,
                    'temperature_f': None,
                    'humidity':      None,
                    'timestamp':     datetime.now().isoformat(),
                    'mock':          False,
                    'source':        'error',
                    'state':         'error',
                    'pin':           self.pin,
                    'last_error':    self._last_error,
                    'attempts':      attempts,
                }
                self._state = 'error'

    # ------------------------------------------------------------------
    # Read accessor — never touches the hardware
    # ------------------------------------------------------------------
    def read(self) -> Dict:
        """Return the latest cached reading. Never raises, never blocks
        on the GPIO. The background poll thread is the single owner of
        the DHT22 pin; everything else just consumes the cache.
        """
        with self._lock:
            return dict(self._latest_reading)

    def _mock_reading(self, reason: str = '') -> Dict:
        return {
            'temperature_c': 22.4,
            'temperature_f': 72.3,
            'humidity':      55.2,
            'timestamp':     datetime.now().isoformat(),
            'mock':          True,
            'source':        'mock',
            'state':         'mock',
            'pin':           self.pin,
            'last_error':    reason or self._last_error,
        }

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def source(self) -> str:
        return self._state

    @property
    def state(self) -> str:
        return self._state

    @property
    def available(self) -> bool:
        """Did the DHT22 init succeed? This stays True even when the
        most recent poll cycle failed — the hardware is still wired up
        and we expect transient errors. It only goes False when the
        sensor library / pin / chip is genuinely missing."""
        return self._sensor is not None

    @property
    def last_error(self) -> str:
        return self._last_error

    def selfcheck(self) -> Dict:
        if self._sensor is None:
            return {
                'ok': False,
                'source': self._state,
                'message': self._last_error or 'DHT22 not initialized (mock readings)',
            }
        data = self.read()
        state = data.get('state') or data.get('source') or 'unknown'
        if state == 'real':
            return {
                'ok': True,
                'source': 'real',
                'message': f"Temp={data['temperature_f']}°F  Hum={data['humidity']}%",
            }
        if state == 'cached':
            return {
                'ok': True,
                'source': 'cached',
                'message': (
                    f"Temp={data['temperature_f']}°F  Hum={data['humidity']}% "
                    f"(cached, last good {data.get('cache_age_sec', '?')}s ago)"
                ),
            }
        return {
            'ok': False,
            'source': state,
            'message': data.get('last_error') or 'DHT22 read returned no data',
        }
