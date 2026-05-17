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
    """Kernel's answer to 'which link carries the default route' — authoritative.

    Uses `ip route get 8.8.8.8` which is the most reliable way to determine
    which interface would actually carry outbound traffic, especially when
    multiple default routes exist with different metrics.
    """
    r = _run(['ip', 'route', 'get', '8.8.8.8'], timeout=3)
    if r['ok']:
        for line in r['stdout'].splitlines():
            parts = line.split()
            if 'dev' in parts:
                i = parts.index('dev')
                if i + 1 < len(parts):
                    return parts[i + 1]

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
                parts = [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', line)]
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
        if prev:
            if in_use:
                prev['in_use'] = True
            if prev['signal'] >= signal:
                continue
        seen[ssid] = {
            'ssid':     ssid,
            'signal':   signal,
            'security': security or 'open',
            'in_use':   in_use or (prev['in_use'] if prev else False),
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
    """Join a WiFi network. Password is optional for open networks.

    If NM already has a saved connection profile for this SSID, activate
    it with ``nmcli connection up``.  Otherwise create a new connection
    with ``nmcli device wifi connect``.
    """
    if not ssid:
        return {'ok': False, 'backend': detect_backend(), 'message': 'ssid required'}
    if detect_backend() != 'nm':
        return {
            'ok': False,
            'backend': 'direct',
            'message': 'NetworkManager required for wifi join',
        }

    # If NM already knows this SSID, try activating the saved profile.
    saved = wifi_saved()
    known = [n for n in (saved.get('networks') or []) if n.get('ssid') == ssid]
    if known:
        if password:
            _run(['nmcli', 'connection', 'modify', ssid,
                  'wifi-sec.key-mgmt', 'wpa-psk',
                  'wifi-sec.psk', password], timeout=10)
        r = _run(['nmcli', 'connection', 'up', ssid], timeout=45)
        return {
            'ok': r['ok'],
            'backend': 'nm',
            'message': (r['stdout'] or r['stderr'] or ('connected to ' + ssid)),
        }

    # New network — create the connection profile with explicit key-mgmt.
    if password:
        r = _run([
            'nmcli', 'connection', 'add',
            'type', 'wifi',
            'con-name', ssid,
            'ssid', ssid,
            'wifi-sec.key-mgmt', 'wpa-psk',
            'wifi-sec.psk', password,
        ], timeout=15)
        if not r['ok']:
            return {
                'ok': False,
                'backend': 'nm',
                'message': (r['stderr'] or r['stdout'] or 'failed to create connection'),
            }
        r = _run(['nmcli', 'connection', 'up', ssid], timeout=45)
        return {
            'ok': r['ok'],
            'backend': 'nm',
            'message': (r['stdout'] or r['stderr'] or ('connected to ' + ssid)),
        }

    # Open network (no password)
    r = _run(['nmcli', 'device', 'wifi', 'connect', ssid], timeout=45)
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


def _read_apn_nm() -> tuple:
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
    """Write a new APN into an existing NM gsm connection (or create one)
    and re-activate it so the modem actually dials with the new value.

    Sequence:
      1. If a gsm connection exists, `nmcli connection modify <name> gsm.apn`.
         Otherwise create one called `cellular` (matching the verified
         working flow on the reference device:
             nmcli connection add type gsm con-name cellular apn nrbroadband
         — note the deliberate absence of any `ifname` argument; pinning
         to wwan0 prevents NM from binding to the modem at all on
         SIM7600G-H, and pinning to `*` is rejected as an invalid
         interface name on some nmcli builds).
      2. `nmcli connection down <name>` (best effort) then `up <name>`
         so the new APN takes effect immediately on the live link, not
         only on the next modem cycle.
      3. Return `{ok, backend, message, connection, applied}`.

    Permission errors are surfaced verbatim from nmcli stderr so the UI
    can tell operators exactly which polkit action is missing instead of
    pretending the save succeeded.
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
    created = False
    if target:
        r = _run(
            ['nmcli', 'connection', 'modify', target, 'gsm.apn', apn],
            timeout=15,
        )
        if not r['ok']:
            return {
                'ok': False, 'backend': 'nm',
                'message': r['stderr'] or 'nmcli modify failed',
                'connection': target, 'applied': False,
            }
        logger.info('APN on %s set to %s', target, apn)
    else:
        # No existing gsm profile — create one. Use the same canonical
        # name the operator validated by hand so we don't end up with
        # two parallel profiles fighting for the modem.
        target = 'cellular'
        r = _run(
            ['nmcli', 'connection', 'add', 'type', 'gsm',
             'con-name', target, 'apn', apn],
            timeout=15,
        )
        if not r['ok']:
            return {
                'ok': False, 'backend': 'nm',
                'message': r['stderr'] or 'nmcli add failed',
                'connection': target, 'applied': False,
            }
        created = True
        logger.info('Created NM gsm connection %s with apn=%s', target, apn)

    # Best-effort re-activate so the running pppd/mobile-broadband session
    # picks up the new APN. If the modem isn't present (no SIM yet), this
    # call simply fails — that's expected and we don't downgrade the result.
    applied = False
    apply_msg = ''
    _run(['nmcli', 'connection', 'down', target], timeout=15)
    up = _run(['nmcli', 'connection', 'up', target], timeout=45)
    if up['ok']:
        applied = True
        apply_msg = 'connection re-activated'
    else:
        apply_msg = up['stderr'] or up['stdout'] or 'connection saved; modem will pick it up on next dial'

    return {
        'ok': True, 'backend': 'nm',
        'message': f'APN set to {apn} — {apply_msg}',
        'connection': target,
        'created': created,
        'applied': applied,
    }


# ---------------------------------------------------------------------------
# Low-level routing / interface helpers — used by the normalized state model
# ---------------------------------------------------------------------------

def _default_route_iface() -> Optional[str]:
    """Read the kernel's default-route interface. Authoritative.

    Reads /proc/net/route directly so we don't depend on `ip` being in
    PATH (busybox-only systems). Returns None if no default route exists.
    """
    try:
        with open('/proc/net/route', 'r') as f:
            next(f)  # header
            for line in f:
                fields = line.split()
                if len(fields) >= 11 and fields[1] == '00000000':
                    return fields[0]
    except Exception:
        pass
    # Fallback for environments without /proc/net/route (e.g. tests)
    return _ip_default_interface()


def _iface_ipv4(iface: str) -> str:
    """Return the first IPv4 address bound to an interface, or ''."""
    if not iface:
        return ''
    r = _run(['ip', '-4', '-o', 'addr', 'show', 'dev', iface], timeout=3)
    if not r['ok']:
        return ''
    for line in r['stdout'].splitlines():
        m = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', line)
        if m:
            return m.group(1)
    return ''


def _nm_profile_exists(name: str) -> bool:
    """Does a NetworkManager connection profile by this exact name exist?"""
    if detect_backend() != 'nm' or not name:
        return False
    r = _run(['nmcli', '-t', '-f', 'NAME', 'connection', 'show'], timeout=5)
    if not r['ok']:
        return False
    for raw in r['stdout'].splitlines():
        if raw.replace('\\:', ':').strip() == name:
            return True
    return False


def _nm_profile_active(name: str) -> bool:
    """Is the named NM connection currently activated?"""
    if detect_backend() != 'nm' or not name:
        return False
    r = _run(
        ['nmcli', '-t', '-f', 'NAME,STATE', 'connection', 'show', '--active'],
        timeout=5,
    )
    if not r['ok']:
        return False
    for raw in r['stdout'].splitlines():
        parts = [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', raw)]
        if len(parts) >= 2 and parts[0] == name and parts[1] == 'activated':
            return True
    return False


def _gsm_profile_name() -> str:
    """Name of the live GSM profile, or '' if none exists."""
    return _read_apn_nm()[1] or ''


def _service_active(unit: str) -> bool:
    """Is a systemd unit currently active?"""
    r = _run(['systemctl', 'is-active', unit], timeout=3)
    return bool(r['ok'] and r['stdout'] == 'active')


def _radio_state(domain: str) -> bool:
    """Is `nmcli radio <domain>` enabled? domain ∈ {wifi, wwan}."""
    if detect_backend() != 'nm':
        return True  # no NM, no toggle — assume on
    r = _run(['nmcli', '-t', '-f', domain.upper(), 'radio'], timeout=3)
    if not r['ok']:
        return True
    return r['stdout'].strip().lower() == 'enabled'


def set_radio(domain: str, enabled: bool) -> dict:
    """Toggle a network domain on or off.

    For WiFi we use connection-level management instead of `nmcli radio wifi`
    because the radio is shared with the hotspot — killing it would lock the
    operator out. Instead we bring individual wifi-client connections up/down.

    For wwan (cellular), radio-level toggle is safe since the cellular modem
    has its own dedicated hardware.
    """
    if detect_backend() != 'nm':
        return {'ok': False, 'state': 'unknown', 'error': 'NetworkManager not available'}

    if domain == 'wifi':
        return _toggle_wifi_client(enabled)

    state = 'on' if enabled else 'off'
    r = _run(['nmcli', 'radio', domain, state], timeout=5)
    return {'ok': r['ok'], 'state': state, 'error': r.get('stderr', '')}


def _toggle_wifi_client(enabled: bool) -> dict:
    """Enable or disable WiFi client connections without touching the radio.

    When disabling: disconnect all active wifi-client (non-hotspot) connections.
    When enabling: ensure the radio is on, then try to activate saved connections.
    """
    if not enabled:
        r = _run(
            ['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE,STATE', 'connection', 'show', '--active'],
            timeout=5,
        )
        if not r['ok']:
            return {'ok': False, 'state': 'off', 'error': r.get('stderr', '')}

        disconnected = []
        for raw in r['stdout'].splitlines():
            parts = [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', raw)]
            if len(parts) < 4:
                continue
            name, ctype = parts[0], parts[1]
            if ctype not in ('802-11-wireless', 'wifi'):
                continue
            if 'SkyTrack' in name or 'hotspot' in name.lower():
                continue
            dr = _run(['nmcli', 'connection', 'down', name], timeout=10)
            if dr['ok']:
                disconnected.append(name)
                logger.info('WiFi client connection %s disconnected', name)

        return {'ok': True, 'state': 'off', 'disconnected': disconnected, 'error': ''}

    radio = _run(['nmcli', 'radio', 'wifi'], timeout=3)
    if radio['ok'] and 'disabled' in radio['stdout'].lower():
        _run(['nmcli', 'radio', 'wifi', 'on'], timeout=5)
        import time as _time
        _time.sleep(1)

    r = _run(
        ['nmcli', '-t', '-f', 'NAME,TYPE,AUTOCONNECT', 'connection', 'show'],
        timeout=5,
    )
    if not r['ok']:
        return {'ok': True, 'state': 'on', 'error': 'Radio on but could not list connections'}

    activated = []
    for raw in r['stdout'].splitlines():
        parts = [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', raw)]
        if len(parts) < 3:
            continue
        name, ctype = parts[0], parts[1]
        if ctype not in ('802-11-wireless', 'wifi'):
            continue
        if 'SkyTrack' in name or 'hotspot' in name.lower():
            continue
        ur = _run(['nmcli', 'connection', 'up', name], timeout=30)
        if ur['ok']:
            activated.append(name)
            logger.info('WiFi client connection %s activated', name)
            break

    return {'ok': True, 'state': 'on', 'activated': activated, 'error': ''}


# ---------------------------------------------------------------------------
# Normalized network state — the SINGLE source of truth for the UI.
#
# Phase 1 of the product-quality pass establishes this contract: the
# frontend MUST consume only this dict (or its serialized form via
# /api/network/state). Anything that infers state from raw mmcli/nmcli
# output in JS is a regression and should be deleted.
# ---------------------------------------------------------------------------

def normalized_network_state(config) -> Dict:
    """Return the full, normalized network truth model.

    Every dimension is independently checked against the kernel / nmcli /
    mmcli, never inferred. This means the dict can legitimately contain
    states like:
        modem_registered: True, bearer_connected: True,
        nm_profile_active: True, ip_assigned: False
    which is the actual "modem dialed but interface didn't get an IP"
    failure mode — the UI can render that honestly instead of guessing.
    """
    cfg = config or {}

    # ---- WiFi ----
    wifi_radio_on = _radio_state('wifi')
    w = wifi_status()
    wifi_iface = ''
    if detect_backend() == 'nm':
        r = _run(
            ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device'],
            timeout=5,
        )
        if r['ok']:
            for line in r['stdout'].splitlines():
                parts = [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', line)]
                if len(parts) >= 2 and parts[1] == 'wifi':
                    wifi_iface = parts[0]
                    break
    wifi_ipv4 = _iface_ipv4(wifi_iface) if wifi_iface else ''

    wifi = {
        'enabled':       bool(wifi_radio_on),
        'connected':     bool(w.get('connected')),
        'ssid':          w.get('ssid') or '',
        'signal_pct':    int(w.get('signal_pct') or 0),
        'interface':     wifi_iface,
        'ipv4':          wifi_ipv4,
    }

    # ---- Cellular ----
    cell_raw = cellular_status()
    gsm_name = _gsm_profile_name()
    nm_present = bool(gsm_name)
    nm_active  = _nm_profile_active(gsm_name) if gsm_name else False
    cell_iface = cell_raw.get('interface') or ''
    cell_ipv4  = _iface_ipv4(cell_iface) if cell_iface else ''
    cell_state = (cell_raw.get('state') or '').lower()

    # mmcli reports 'registered' or 'connected' once the modem is on the
    # network; bearer_connected is a stricter check for an actual data session.
    modem_registered = (
        bool(cell_raw.get('detected'))
        and any(s in cell_state for s in ('registered', 'connected', 'enabled'))
    )
    bearer_connected = ('connected' in cell_state)

    cellular = {
        'enabled':            bool(cfg.get('cellular_enabled', True))
                              and _radio_state('wwan'),
        'modem_present':      bool(cell_raw.get('detected')),
        'modem_registered':   modem_registered,
        'bearer_connected':   bearer_connected,
        'nm_profile_present': nm_present,
        'nm_profile_active':  nm_active,
        'nm_profile_name':    gsm_name,
        'ip_assigned':        bool(cell_ipv4),
        'route_active':       (_default_route_iface() == cell_iface) if cell_iface else False,
        'apn':                cell_raw.get('apn') or (cfg.get('cellular_apn') or ''),
        'apn_source':         cell_raw.get('apn_source') or '',
        'carrier':            cell_raw.get('carrier') or '',
        'signal_pct':         int(cell_raw.get('signal_pct') or 0),
        'access_tech':        cell_raw.get('access_tech') or '',
        'interface':          cell_iface,
        'ipv4':               cell_ipv4,
    }

    # ---- Hotspot ----
    # Source of truth is hotspot.hotspot_health(), which does a deep
    # probe (hostapd + dnsmasq + wlan0 in AP mode with a gateway IP in
    # the hotspot subnet). Phase 2 moved `actually_usable` off a naive
    # `systemctl is-active hostapd` so the UI stops claiming the
    # hotspot is up when no client could actually get an IP.
    #
    # NetworkManager-shared mode is still checked as a secondary path:
    # a few dev installs run the hotspot under NM instead of the
    # direct hostapd stack, and we don't want to flag those as broken.
    try:
        from hotspot import hotspot_health
        hs_health = hotspot_health(cfg)
    except Exception as e:
        logger.debug('hotspot_health failed: %s', e)
        hs_health = {
            'usable': False, 'hostapd_active': False, 'dnsmasq_active': False,
            'has_gateway_ip': False, 'ap_mode': False, 'stations': 0,
            'leases': 0, 'degraded_reasons': ['probe error: ' + str(e)],
        }
    dnsmasq_present = bool(shutil.which('dnsmasq'))

    nm_hotspot_active = False
    if detect_backend() == 'nm':
        r = _run(
            ['nmcli', '-t', '-f', 'NAME,TYPE,STATE', 'connection', 'show', '--active'],
            timeout=5,
        )
        if r['ok']:
            for raw in r['stdout'].splitlines():
                parts = [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', raw)]
                if len(parts) >= 3 and parts[2] == 'activated' and 'SkyTrack' in parts[0]:
                    nm_hotspot_active = True
                    break

    hs_service = bool(hs_health['hostapd_active'] or nm_hotspot_active)
    hs_dhcp    = bool(hs_health['dnsmasq_active'] or nm_hotspot_active)
    hs_usable  = bool(hs_health['usable'] or nm_hotspot_active)

    hotspot = {
        'enabled':          bool(cfg.get('hotspot_auto_start') or hs_service),
        'configured':       bool(cfg.get('hotspot_ssid')),
        'service_running':  hs_service,
        'dhcp_active':      hs_dhcp,
        'dhcp_installed':   dnsmasq_present,
        'actually_usable':  hs_usable,
        'ssid':             cfg.get('hotspot_ssid') or '',
        'local_url':        f"http://{cfg.get('hotspot_gateway') or '10.4.26.89'}",
        'gateway':          cfg.get('hotspot_gateway') or '10.4.26.89',
        # Rich truth surfaced for the UI so it can show WHY the
        # hotspot is degraded, not just `usable=false`.
        'ap_mode':          hs_health['ap_mode'],
        'has_gateway_ip':   hs_health['has_gateway_ip'],
        'stations':         hs_health['stations'],
        'leases':           hs_health['leases'],
        'degraded_reasons': hs_health['degraded_reasons'],
        # password/seconds_remaining are session data — added by the
        # blueprint when the request is admin-authed (see network.py).
    }

    # ---- Routing ----
    route_iface = _default_route_iface() or ''
    route_kind  = _classify(route_iface)
    routing = {
        'primary':           route_kind,
        'primary_interface': route_iface,
        'internet_reachable': _internet_reachable_quick(),
    }

    # ---- Preferences ----
    preferences = {
        'preferred_uplink': cfg.get('preferred_uplink') or 'auto',  # auto|wifi|cellular
    }

    return {
        'wifi':        wifi,
        'cellular':    cellular,
        'hotspot':     hotspot,
        'routing':     routing,
        'preferences': preferences,
        'backend':     detect_backend(),
    }


def _internet_reachable_quick(timeout: float = 1.5) -> bool:
    """A very fast reachability probe used inside the state aggregator.

    We deliberately keep this tiny so calling normalized_network_state()
    in a hot path (poll) doesn't add measurable latency. The richer
    reachability check lives in network_svc.internet_reachable().
    """
    import socket
    try:
        socket.create_connection(('1.1.1.1', 53), timeout=timeout).close()
        return True
    except OSError:
        return False


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
