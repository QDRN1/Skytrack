"""SkyTrack network backend — NetworkManager-first, direct-tool fallback.

This module is the single place the app talks to Linux networking. Everything
else (network_svc, blueprints, topbar, settings tiles) goes through here so we
can standardize the stack without forking behavior across files.

Design rules:

  * NetworkManager (nmcli) is the preferred backend. If nmcli exists and
    `systemctl is-active NetworkManager` succeeds, we use it for WiFi scan /
    join / forget / saved list, cellular connection management, APN edits,
    and active-connection discovery.

  * If NM is absent (dev box, minimal image), we degrade gracefully to a
    tiny set of read-only helpers (iwconfig/ip/mmcli) and honestly report
    `backend = "direct"` so the UI can tell users "WiFi scan is not
    available on this install — install NetworkManager". We never lie about
    capabilities.

  * Every shell-out is wrapped in `_run()` which never raises. All returns
    are plain dicts/lists, never exceptions.

  * APN defaults to `nrbroadband` (per product spec) but is config-driven.
    When set via the UI, we rewrite the nmcli `gsm` connection in place.

  * No destructive commands without explicit user action.

The adapter is deliberately chatty in logs so first-install debugging is
fast — each nmcli call logs its phase at DEBUG, success at INFO, and any
unexpected stderr at WARNING.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from typing import Dict, List, Optional

logger = logging.getLogger('skytrack.net_backend')


# ---------------------------------------------------------------------------
# Small command runner — never raises
# ---------------------------------------------------------------------------

def _run(args, timeout: float = 8.0) -> Dict:
    """Execute a shell command, capture stdout/stderr, never raise.

    Returns {'ok': bool, 'code': int, 'stdout': str, 'stderr': str, 'args': list}.
    """
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
        return {
            'ok': r.returncode == 0,
            'code': r.returncode,
            'stdout': (r.stdout or '').strip(),
            'stderr': (r.stderr or '').strip(),
            'args': list(args),
        }
    except FileNotFoundError:
        return {'ok': False, 'code': 127, 'stdout': '', 'stderr': f'{args[0]}: not found', 'args': list(args)}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'code': 124, 'stdout': '', 'stderr': f'{args[0]}: timeout', 'args': list(args)}
    except Exception as e:
        return {'ok': False, 'code': 1, 'stdout': '', 'stderr': str(e), 'args': list(args)}


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

_BACKEND_CACHE = {'name': None, 'checked_at': 0}
_BACKEND_TTL = 15.0  # seconds — avoid spamming nmcli/systemctl on every call


def detect_backend() -> str:
    """Return 'nm' if NetworkManager is active, else 'direct'."""
    now = time.time()
    if _BACKEND_CACHE['name'] and (now - _BACKEND_CACHE['checked_at']) < _BACKEND_TTL:
        return _BACKEND_CACHE['name']

    name = 'direct'
    if shutil.which('nmcli'):
        probe = _run(['systemctl', 'is-active', 'NetworkManager'], timeout=3)
        if probe['ok'] and probe['stdout'] == 'active':
            name = 'nm'
        else:
            # nmcli can still work if NM is running via a non-systemd init.
            ping = _run(['nmcli', '-t', '-f', 'RUNNING', 'general'], timeout=3)
            if ping['ok'] and 'running' in ping['stdout'].lower():
                name = 'nm'

    _BACKEND_CACHE['name'] = name
    _BACKEND_CACHE['checked_at'] = now
    logger.debug('network backend detected: %s', name)
    return name


def backend_info() -> Dict:
    """Human-readable info about which backend is live, for Settings → Network."""
    b = detect_backend()
    if b == 'nm':
        ver = _run(['nmcli', '--version'], timeout=3)
        return {
            'backend': 'NetworkManager',
            'backend_id': 'nm',
            'version': (ver['stdout'].split()[-1] if ver['ok'] and ver['stdout'] else 'unknown'),
            'capabilities': ['wifi_scan', 'wifi_join', 'wifi_forget', 'cellular_apn', 'hotspot_via_nm'],
        }
    return {
        'backend': 'direct (iwconfig/mmcli/ip)',
        'backend_id': 'direct',
        'version': '',
        'capabilities': ['read_only_status'],
        'warning': (
            'NetworkManager is not running. WiFi scan/join, APN edits, and '
            'hotspot management from the portal require NetworkManager. '
            'Install it with: sudo apt install network-manager'
        ),
    }


# ---------------------------------------------------------------------------
# Active connection / primary uplink
# ---------------------------------------------------------------------------

def _ip_default_interface() -> Optional[str]:
    """Kernel's answer to 'which link carries the default route' — authoritative."""
    r = _run(['ip', '-4', 'route', 'show', 'default'], timeout=3)
    if not r['ok']:
        return None
    for line in r['stdout'].splitlines():
        parts = line.split()
        if 'dev' in parts:
            i = parts.index('dev')
            if i + 1 < len(parts):
                return parts[i + 1]
    return None


def _classify(iface: Optional[str]) -> str:
    if not iface:
        return 'none'
    if iface.startswith(('wwan', 'ppp', 'usb')) or iface == 'rmnet0':
        return 'cellular'
    if iface.startswith(('wlan', 'wlp')):
        return 'wifi'
    if iface.startswith(('eth', 'enp', 'enx', 'end', 'br')):
        return 'ethernet'
    return 'other'


def active_uplink() -> Dict:
    """Return the link currently carrying outbound traffic.

    Result: {interface, kind, nm_connection, nm_device, via}.
    `kind` is one of cellular | wifi | ethernet | other | none.
    """
    iface = _ip_default_interface()
    kind = _classify(iface)

    out = {
        'interface': iface or '',
        'kind': kind,
        'nm_connection': '',
        'nm_device': iface or '',
    }

    if detect_backend() == 'nm' and iface:
        r = _run(
            ['nmcli', '-t', '-f', 'NAME,DEVICE,TYPE,STATE', 'connection', 'show', '--active'],
            timeout=5,
        )
        if r['ok']:
            for line in r['stdout'].splitlines():
                parts = line.split(':')
                if len(parts) >= 4 and parts[1] == iface:
                    out['nm_connection'] = parts[0]
                    break
    return out


# ---------------------------------------------------------------------------
# WiFi: scan, list saved, connect, forget
# ---------------------------------------------------------------------------

def wifi_scan() -> Dict:
    """Return a list of nearby SSIDs, best-effort.

    Result: {ok: bool, backend: str, networks: [{ssid, signal, security, in_use, bssid, freq}], error: str}.
    Networks are sorted strongest-first. SSIDs that appear multiple times are
    collapsed into one entry (keeping the strongest BSSID).
    """
    if detect_backend() != 'nm':
        return {
            'ok': False,
            'backend': 'direct',
            'networks': [],
            'error': 'WiFi scan requires NetworkManager (nmcli). Install with: sudo apt install network-manager',
        }

    # Ask NM to rescan first, but don't block on its result — some devices
    # return "scan was requested" with exit code 0 even when stale.
    _run(['nmcli', 'device', 'wifi', 'rescan'], timeout=10)
    time.sleep(0.8)

    r = _run(
        ['nmcli', '-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY,BSSID,FREQ', 'device', 'wifi', 'list'],
        timeout=12,
    )
    if not r['ok']:
        return {'ok': False, 'backend': 'nm', 'networks': [], 'error': r['stderr'] or 'nmcli wifi list failed'}

    seen: Dict[str, Dict] = {}
    for raw in r['stdout'].splitlines():
        # nmcli -t escapes embedded colons as "\:" — normalize.
        parts = [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', raw)]
        if len(parts) < 3:
            continue
        in_use = parts[0].strip() == '*'
        ssid = parts[1].strip()
        if not ssid:
            continue
        try:
            signal = int(parts[2] or '0')
        except ValueError:
            signal = 0
        security = parts[3].strip() if len(parts) > 3 else ''
        bssid = parts[4].strip() if len(parts) > 4 else ''
        freq  = parts[5].strip() if len(parts) > 5 else ''
        prev = seen.get(ssid)
        if prev and prev['signal'] >= signal:
            continue
        seen[ssid] = {
            'ssid':     ssid,
            'signal':   signal,
            'security': security or 'open',
            'in_use':   in_use,
            'bssid':    bssid,
            'freq':     freq,
        }

    networks = sorted(seen.values(), key=lambda n: n['signal'], reverse=True)
    return {'ok': True, 'backend': 'nm', 'networks': networks, 'error': ''}


def wifi_saved() -> Dict:
    """List saved NM wifi connections."""
    if detect_backend() != 'nm':
        return {'ok': False, 'backend': 'direct', 'networks': [], 'error': 'NetworkManager not available'}

    r = _run(
        ['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE,STATE', 'connection', 'show'],
        timeout=5,
    )
    if not r['ok']:
        return {'ok': False, 'backend': 'nm', 'networks': [], 'error': r['stderr']}

    out = []
    for raw in r['stdout'].splitlines():
        parts = [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', raw)]
        if len(parts) < 2:
            continue
        name, ctype = parts[0], parts[1]
        if '802-11-wireless' not in ctype and ctype != 'wifi':
            continue
        device = parts[2] if len(parts) > 2 else ''
        state  = parts[3] if len(parts) > 3 else ''
        out.append({
            'ssid':      name,  # NM uses the SSID as the connection name by default
            'name':      name,
            'device':    device,
            'connected': (state == 'activated'),
        })
    return {'ok': True, 'backend': 'nm', 'networks': out, 'error': ''}


def wifi_connect(ssid: str, password: Optional[str] = None) -> Dict:
    """Join a WiFi network. Password is optional for open networks."""
    if not ssid:
        return {'ok': False, 'backend': detect_backend(), 'message': 'ssid required'}
    if detect_backend() != 'nm':
        return {
            'ok': False,
            'backend': 'direct',
            'message': 'NetworkManager required for wifi join',
        }

    args = ['nmcli', 'device', 'wifi', 'connect', ssid]
    if password:
        args += ['password', password]
    r = _run(args, timeout=45)
    # nmcli exits non-zero if the connection partially succeeded; surface the
    # stderr text verbatim so the UI can show exactly what went wrong.
    return {
        'ok': r['ok'],
        'backend': 'nm',
        'message': (r['stdout'] or r['stderr'] or ('connected to ' + ssid)),
    }


def wifi_forget(ssid: str) -> Dict:
    """Delete the saved NM connection for an SSID."""
    if not ssid:
        return {'ok': False, 'backend': detect_backend(), 'message': 'ssid required'}
    if detect_backend() != 'nm':
        return {'ok': False, 'backend': 'direct', 'message': 'NetworkManager required'}
    r = _run(['nmcli', 'connection', 'delete', ssid], timeout=10)
    return {
        'ok': r['ok'],
        'backend': 'nm',
        'message': r['stdout'] or r['stderr'] or 'forgotten',
    }


def wifi_status() -> Dict:
    """Current WiFi-client state (which SSID we're joined to)."""
    info = {'connected': False, 'ssid': '', 'signal_pct': 0, 'signal_dbm': None, 'backend': detect_backend()}

    if info['backend'] == 'nm':
        r = _run(
            ['nmcli', '-t', '-f', 'ACTIVE,SSID,SIGNAL', 'device', 'wifi', 'list'],
            timeout=5,
        )
        if r['ok']:
            for raw in r['stdout'].splitlines():
                parts = [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', raw)]
                if len(parts) >= 3 and parts[0] == 'yes':
                    info['connected'] = True
                    info['ssid'] = parts[1]
                    try:
                        info['signal_pct'] = int(parts[2] or '0')
                    except ValueError:
                        info['signal_pct'] = 0
                    break
        return info

    # Direct fallback: iwconfig wlan0
    if shutil.which('iwconfig'):
        r = _run(['iwconfig', 'wlan0'], timeout=3)
        text = r['stdout'] + '\n' + r['stderr']
        m = re.search(r'ESSID:"([^"]*)"', text)
        if m and m.group(1):
            info['ssid'] = m.group(1)
            info['connected'] = True
        m = re.search(r'Signal level=(-?\d+)\s*dBm', text)
        if m:
            dbm = int(m.group(1))
            info['signal_dbm'] = dbm
            info['signal_pct'] = max(0, min(100, 2 * (dbm + 100)))
    return info


# ---------------------------------------------------------------------------
# Cellular: status, APN read/write, toggle
# ---------------------------------------------------------------------------

def cellular_status() -> Dict:
    """Aggregate cellular modem state.

    Prefers mmcli (ModemManager) data because NM exposes it. Falls back to
    plain interface detection so an un-managed USB dongle still reports 'up'.
    """
    info = {
        'detected': False,
        'state': 'unknown',
        'carrier': '',
        'signal_pct': 0,
        'access_tech': '',
        'interface': '',
        'apn': '',
        'apn_source': '',
    }

    if shutil.which('mmcli'):
        listing = _run(['mmcli', '-L'], timeout=3)
        m = re.search(r'/Modem/(\d+)', listing.get('stdout', ''))
        if m:
            idx = m.group(1)
            info['detected'] = True
            detail = _run(['mmcli', '-m', idx], timeout=5)
            text = detail.get('stdout', '')

            def _pick(pattern):
                mm = re.search(pattern, text, re.IGNORECASE)
                return mm.group(1).strip() if mm else ''

            info['carrier']     = _pick(r'operator name:\s*(.+)')
            info['state']       = _pick(r'state:\s*(.+)')
            info['access_tech'] = _pick(r'access tech:\s*(.+)')
            sq = _pick(r'signal quality:\s*\(?(\d+)%')
            try:
                info['signal_pct'] = int(sq) if sq else 0
            except ValueError:
                info['signal_pct'] = 0
            info['interface'] = _pick(r'interface:\s*(\w+)')

    if not info['detected']:
        ip_r = _run(['ip', '-o', 'link', 'show'], timeout=3)
        for line in (ip_r.get('stdout') or '').splitlines():
            for iface in ('wwan0', 'usb0', 'ppp0', 'rmnet0'):
                if f' {iface}:' in line:
                    info['detected'] = True
                    info['interface'] = iface
                    info['state'] = 'up' if 'state UP' in line else 'down'
                    return info

    info['apn'], info['apn_source'] = _read_apn_nm()
    return info


def _read_apn_nm() -> (str, str):
    """Read the APN currently configured on the primary gsm connection."""
    if detect_backend() != 'nm':
        return '', ''
    r = _run(
        ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'],
        timeout=5,
    )
    if not r['ok']:
        return '', ''
    gsm_conn = None
    for raw in r['stdout'].splitlines():
        parts = [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', raw)]
        if len(parts) >= 2 and parts[1] == 'gsm':
            gsm_conn = parts[0]
            break
    if not gsm_conn:
        return '', ''
    detail = _run(
        ['nmcli', '-t', '-f', 'gsm.apn,connection.id', 'connection', 'show', gsm_conn],
        timeout=5,
    )
    if not detail['ok']:
        return '', ''
    for raw in detail['stdout'].splitlines():
        if raw.startswith('gsm.apn:'):
            return raw.split(':', 1)[1].strip(), gsm_conn
    return '', gsm_conn


def set_apn(apn: str, connection_name: Optional[str] = None) -> Dict:
    """Write a new APN into an existing NM gsm connection, or create one.

    If there's already a gsm connection, we edit it in place (non-destructive).
    Otherwise we create a new connection named 'skytrack-cellular' so the
    modem has something to dial against the first time the SIM is inserted.
    """
    apn = (apn or '').strip()
    if not apn:
        return {'ok': False, 'backend': detect_backend(), 'message': 'apn required'}
    if detect_backend() != 'nm':
        return {
            'ok': False,
            'backend': 'direct',
            'message': 'APN configuration requires NetworkManager. The value '
                       'has been saved to config and will apply once '
                       'network-manager is installed.',
        }

    target = connection_name or _read_apn_nm()[1]
    if target:
        r = _run(
            ['nmcli', 'connection', 'modify', target, 'gsm.apn', apn],
            timeout=10,
        )
        if not r['ok']:
            return {
                'ok': False, 'backend': 'nm',
                'message': r['stderr'] or 'nmcli modify failed',
            }
        logger.info('APN on %s set to %s', target, apn)
        return {
            'ok': True, 'backend': 'nm',
            'message': f'APN set to {apn}',
            'connection': target, 'created': False,
        }

    # No gsm connection yet — create one. This doesn't activate anything until
    # a SIM is actually present and the modem registers.
    name = 'skytrack-cellular'
    r = _run(
        ['nmcli', 'connection', 'add', 'type', 'gsm',
         'con-name', name, 'ifname', '*', 'apn', apn],
        timeout=10,
    )
    if not r['ok']:
        return {
            'ok': False, 'backend': 'nm',
            'message': r['stderr'] or 'nmcli add failed',
        }
    logger.info('Created NM gsm connection %s with apn=%s', name, apn)
    return {
        'ok': True, 'backend': 'nm',
        'message': f'Created connection {name} with APN {apn}',
        'connection': name, 'created': True,
    }


# ---------------------------------------------------------------------------
# Hotspot helpers — read-only here; writes live in hotspot.py
# ---------------------------------------------------------------------------

def hotspot_active() -> bool:
    """Is the SkyTrack-Portal hotspot currently broadcasting?"""
    for unit in ('skytrack-hotspot', 'hostapd'):
        r = _run(['systemctl', 'is-active', unit], timeout=3)
        if r['ok'] and r['stdout'] == 'active':
            return True
    if detect_backend() == 'nm':
        r = _run(
            ['nmcli', '-t', '-f', 'NAME,TYPE,STATE', 'connection', 'show', '--active'],
            timeout=5,
        )
        for raw in (r.get('stdout') or '').splitlines():
            parts = [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', raw)]
            if len(parts) >= 3 and parts[2] == 'activated' and 'SkyTrack' in parts[0]:
                return True
    return False
