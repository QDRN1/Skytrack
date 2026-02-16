"""System health monitoring: uptime, disk, CPU temp, network, cloudflared."""

import os
import time
import shutil
import socket
import subprocess
import logging
from datetime import datetime

logger = logging.getLogger('skytrack.health')

_start_time = time.time()


class HealthService:
    def __init__(self, config):
        self.config = config

    def get_status(self):
        return {
            'uptime': self._get_uptime(),
            'disk': self._get_disk_usage(),
            'cpu_temp': self._get_cpu_temp(),
            'network': self._get_network_status(),
            'cloudflared': self._get_cloudflared_status(),
            'timestamp': datetime.now().isoformat(),
        }

    def _get_uptime(self):
        elapsed = time.time() - _start_time
        days = int(elapsed // 86400)
        hours = int((elapsed % 86400) // 3600)
        minutes = int((elapsed % 3600) // 60)
        if days:
            display = f'{days}d {hours}h {minutes}m'
        elif hours:
            display = f'{hours}h {minutes}m'
        else:
            display = f'{minutes}m'
        return {'seconds': int(elapsed), 'display': display}

    def _get_disk_usage(self):
        try:
            usage = shutil.disk_usage('/')
            return {
                'total_gb': round(usage.total / (1024 ** 3), 1),
                'used_gb': round(usage.used / (1024 ** 3), 1),
                'free_gb': round(usage.free / (1024 ** 3), 1),
                'percent': round(usage.used / usage.total * 100, 1),
            }
        except Exception as e:
            logger.warning(f'Disk usage error: {e}')
            return None

    def _get_cpu_temp(self):
        try:
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                temp_c = int(f.read().strip()) / 1000
            return {
                'celsius': round(temp_c, 1),
                'fahrenheit': round(temp_c * 9 / 5 + 32, 1),
            }
        except FileNotFoundError:
            return {'celsius': None, 'fahrenheit': None, 'note': 'Not on Raspberry Pi'}
        except Exception as e:
            logger.warning(f'CPU temp error: {e}')
            return {'celsius': None, 'fahrenheit': None}

    def _get_network_status(self):
        status = {'wifi': False, 'cellular': False, 'internet': False, 'wifi_strength': 0, 'cell_strength': 0}

        # WiFi
        try:
            result = subprocess.run(['iwconfig'], capture_output=True, text=True, timeout=5)
            if 'ESSID:' in result.stdout and 'off/any' not in result.stdout:
                status['wifi'] = True
                # Try to parse signal level
                for line in result.stdout.splitlines():
                    if 'Signal level' in line:
                        try:
                            # Format: Signal level=-XX dBm
                            sig = line.split('Signal level=')[1].split(' ')[0]
                            dbm = int(sig.replace('dBm', ''))
                            # Convert dBm to 0-100 scale roughly
                            status['wifi_strength'] = max(0, min(100, 2 * (dbm + 100)))
                        except (IndexError, ValueError):
                            status['wifi_strength'] = 75 if status['wifi'] else 0
        except FileNotFoundError:
            pass
        except Exception:
            pass

        # Cellular
        try:
            result = subprocess.run(['ip', 'link', 'show'], capture_output=True, text=True, timeout=5)
            for iface in ('wwan0', 'usb0', 'ppp0'):
                if iface in result.stdout:
                    # Check if UP
                    for line in result.stdout.splitlines():
                        if iface in line and 'UP' in line:
                            status['cellular'] = True
                            status['cell_strength'] = 60  # placeholder
                            break
        except FileNotFoundError:
            pass
        except Exception:
            pass

        # Internet
        try:
            socket.create_connection(('8.8.8.8', 53), timeout=3)
            status['internet'] = True
        except OSError:
            pass

        return status

    def _get_cloudflared_status(self):
        if not self.config.get('cloudflared_enabled'):
            return {'installed': False, 'running': False}
        try:
            result = subprocess.run(
                ['systemctl', 'is-active', 'cloudflared'],
                capture_output=True, text=True, timeout=5,
            )
            running = result.stdout.strip() == 'active'
            return {'installed': True, 'running': running}
        except Exception:
            return {'installed': False, 'running': False}

    def selfcheck(self):
        try:
            status = self.get_status()
            return {'ok': True, 'message': f"Uptime: {status['uptime']['display']}"}
        except Exception as e:
            return {'ok': False, 'message': str(e)}
