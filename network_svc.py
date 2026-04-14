"""Unified network status — WiFi client, cellular modem, hotspot.

Phase 1 is read-only. We surface what's there for the top bar and the
Settings → Network tab. We don't switch radios or manage failover here.
"""

import logging
import re
import shutil
import socket
import subprocess
from typing import Optional

import hotspot

logger = logging.getLogger('skytrack.network')


# ---------------------------------------------------------------------------
# WiFi client (only meaningful when hotspot is OFF — single radio)
# ---------------------------------------------------------------------------

def wifi_status() -> dict:
    """Best-effort WiFi-client read using iwconfig + iw."""
    info = {
        'connected': False,
        'ssid': '',
        'signal_dbm': None,
        'signal_pct': 0,
    }
    if not shutil.which('iwconfig'):
        return info
    try:
        result = subprocess.run(
            ['iwconfig', 'wlan0'],
            capture_output=True, text=True, timeout=3,
        )
        text = result.stdout or ''
        m = re.search(r'ESSID:"([^"]*)"', text)
        if m and m.group(1):
            info['ssid'] = m.group(1)
            info['connected'] = True
        m = re.search(r'Signal level=(-?\d+)\s*dBm', text)
        if m:
            dbm = int(m.group(1))
            info['signal_dbm'] = dbm
            info['signal_pct'] = max(0, min(100, 2 * (dbm + 100)))
    except Exception as e:
        logger.debug('iwconfig wlan0 failed: %s', e)
    return info


# ---------------------------------------------------------------------------
# Cellular modem (read-only via mmcli, fallback to interface detection)
# ---------------------------------------------------------------------------

def cellular_status() -> dict:
    info = {
        'detected': False,
        'state': 'unknown',
        'carrier': '',
        'signal_pct': 0,
        'signal_quality': '',
        'access_tech': '',
        'interface': '',
    }
    if shutil.which('mmcli'):
        info.update(_mmcli_status())
    if not info['detected']:
        info.update(_interface_fallback())
    return info


def _mmcli_status() -> dict:
    info = {}
    try:
        listing = subprocess.run(
            ['mmcli', '-L'],
            capture_output=True, text=True, timeout=3,
        )
        m = re.search(r'/Modem/(\d+)', listing.stdout or '')
        if not m:
            return info
        idx = m.group(1)
        info['detected'] = True

        detail = subprocess.run(
            ['mmcli', '-m', idx],
            capture_output=True, text=True, timeout=4,
        )
        text = detail.stdout or ''

        for key, regex in (
            ('carrier', r'operator name:\s*(.+)'),
            ('state', r'state:\s*(.+)'),
            ('access_tech', r'access tech:\s*(.+)'),
            ('signal_quality', r'signal quality:\s*\(?(\d+)%'),
        ):
            mm = re.search(regex, text, re.IGNORECASE)
            if mm:
                info[key] = mm.group(1).strip()

        if info.get('signal_quality'):
            try:
                info['signal_pct'] = int(info['signal_quality'])
            except ValueError:
                pass

        # Find the bearer interface name
        m_iface = re.search(r'interface:\s*(\w+)', text, re.IGNORECASE)
        if m_iface:
            info['interface'] = m_iface.group(1).strip()
    except Exception as e:
        logger.debug('mmcli status failed: %s', e)
    return info


def _interface_fallback() -> dict:
    info = {}
    try:
        result = subprocess.run(
            ['ip', '-o', 'link', 'show'],
            capture_output=True, text=True, timeout=3,
        )
        for line in (result.stdout or '').splitlines():
            for iface in ('wwan0', 'usb0', 'ppp0'):
                if f' {iface}:' in line:
                    info['detected'] = True
                    info['interface'] = iface
                    info['state'] = 'up' if 'state UP' in line else 'down'
                    return info
    except Exception:
        pass
    return info


# ---------------------------------------------------------------------------
# Internet reachability
# ---------------------------------------------------------------------------

def internet_reachable(timeout: float = 2.0) -> bool:
    try:
        socket.create_connection(('1.1.1.1', 53), timeout=timeout).close()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def get_network_status(config) -> dict:
    return {
        'wifi': wifi_status(),
        'cellular': cellular_status(),
        'hotspot': hotspot.hotspot_status(config),
        'internet': internet_reachable(),
        'metered': bool(config.get('metered_connection')),
    }
