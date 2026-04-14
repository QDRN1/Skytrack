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
# Default-route detection — the kernel's answer to "which link actually
# carries outbound traffic right now". This is the single source of truth
# for the "active network" indicator; sniffing individual interfaces can
# give confusing results when several are up at once.
# ---------------------------------------------------------------------------

def default_route_interface() -> Optional[str]:
    """Return the iface carrying the IPv4 default route, or None."""
    try:
        r = subprocess.run(
            ['ip', '-4', 'route', 'show', 'default'],
            capture_output=True, text=True, timeout=3,
        )
        for line in (r.stdout or '').splitlines():
            # Format: "default via 10.4.26.1 dev wlan0 proto dhcp ..."
            parts = line.split()
            if 'dev' in parts:
                i = parts.index('dev')
                if i + 1 < len(parts):
                    return parts[i + 1]
    except Exception:
        pass
    return None


def classify_link(iface: Optional[str]) -> str:
    """Map an interface name to a friendly link type label."""
    if not iface:
        return 'none'
    if iface.startswith(('wwan', 'ppp', 'usb')) or iface == 'rmnet0':
        return 'cellular'
    if iface.startswith(('wlan', 'wlp')):
        return 'wifi'
    if iface.startswith(('eth', 'enp', 'enx', 'end')):
        return 'ethernet'
    return 'other'


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def get_network_status(config) -> dict:
    wifi     = wifi_status()
    cellular = cellular_status()
    hs       = hotspot.hotspot_status(config)
    online   = internet_reachable()
    route_if = default_route_interface()
    primary  = classify_link(route_if)

    # Hotspot mode on wlan0 *excludes* wifi-client on the same radio. If the
    # hotspot is up we explicitly force the wifi client tile to "off" rather
    # than echoing stale iwconfig state from before the radio was reprovisioned.
    if hs.get('enabled'):
        wifi = {**wifi, 'connected': False, 'ssid': '', 'signal_dbm': None, 'signal_pct': 0}

    return {
        'wifi':     wifi,
        'cellular': cellular,
        'hotspot':  hs,
        'internet': online,                 # legacy bool kept for topbar
        'online':   online,                 # preferred key
        'metered':  bool(config.get('metered_connection')),
        'primary':  primary,                # cellular | wifi | ethernet | other | none
        'primary_interface': route_if or '',
    }
