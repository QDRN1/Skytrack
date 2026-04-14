"""Hotspot status reader.

Phase 1 is read-only — we surface the current state of hostapd, dnsmasq,
connected stations, and the SkyTrack-Portal SSID. The actual hotspot bring-up
is handled by `scripts/hotspot_apply.sh` invoked from the installer (or the
super user "Restart Hotspot" action).
"""

import logging
import os
import re
import shutil
import subprocess
from typing import List

logger = logging.getLogger('skytrack.hotspot')


DEFAULT_LEASES = '/var/lib/misc/dnsmasq.leases'


def _systemctl_active(unit: str) -> bool:
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() == 'active'
    except Exception:
        return False


def hotspot_status(config) -> dict:
    """Return the current hotspot state for the UI top bar + Network tab."""
    return {
        'enabled': _systemctl_active('hostapd'),
        'dnsmasq': _systemctl_active('dnsmasq'),
        'ssid': config.get('hotspot_ssid', 'SkyTrack-Portal'),
        'gateway': config.get('hotspot_gateway', '10.4.26.89'),
        'subnet': config.get('hotspot_subnet', '10.4.26.0/24'),
        'channel': config.get('hotspot_channel', 6),
        'clients': list_clients(),
        'wlan0_address': _wlan0_address(),
    }


def _wlan0_address() -> str:
    try:
        result = subprocess.run(
            ['ip', '-4', 'addr', 'show', 'dev', 'wlan0'],
            capture_output=True, text=True, timeout=3,
        )
        match = re.search(r'inet\s+(\S+)', result.stdout)
        return match.group(1) if match else ''
    except Exception:
        return ''


def list_clients(leases_path: str = DEFAULT_LEASES) -> List[dict]:
    """Parse dnsmasq.leases for currently leased clients on the hotspot."""
    if not os.path.exists(leases_path):
        return []
    out = []
    try:
        with open(leases_path) as f:
            for line in f:
                # Format: <expiry> <mac> <ip> <hostname> <client_id>
                parts = line.strip().split()
                if len(parts) < 5:
                    continue
                out.append({
                    'expires_at': parts[0],
                    'mac': parts[1],
                    'ip': parts[2],
                    'hostname': parts[3] if parts[3] != '*' else '',
                })
    except Exception as e:
        logger.debug('lease parse error: %s', e)
    return out


def restart_hotspot() -> dict:
    """Bounce hostapd + dnsmasq via the helper script. Requires sudo / capabilities."""
    helper = '/opt/skytrack/scripts/hotspot_apply.sh'
    if not os.path.exists(helper):
        helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'scripts', 'hotspot_apply.sh')
    if not os.path.exists(helper):
        return {'ok': False, 'message': 'hotspot_apply.sh not installed'}
    try:
        result = subprocess.run(
            ['sudo', '-n', helper, '--reapply'],
            capture_output=True, text=True, timeout=20,
        )
        return {
            'ok': result.returncode == 0,
            'message': (result.stdout or result.stderr).strip()[-400:],
        }
    except Exception as e:
        return {'ok': False, 'message': str(e)}


def selfcheck(config) -> dict:
    if not shutil.which('hostapd'):
        return {'ok': False, 'message': 'hostapd not installed'}
    status = hotspot_status(config)
    if status['enabled']:
        return {'ok': True, 'message': f"SSID {status['ssid']} active on wlan0 ({status['wlan0_address']})"}
    return {'ok': False, 'message': 'hostapd not active'}
