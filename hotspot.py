"""Hotspot status reader — deep truth probe.

The hotspot is a direct-tool stack (hostapd + dnsmasq + static IP on
wlan0). The installer brings it up via `scripts/hotspot_apply.sh`; this
module READS the live state so the UI and net_backend can answer
"is the hotspot actually usable right now?" with more than a shallow
`systemctl is-active hostapd` check.

The canonical entrypoint is `hotspot_health(config)`. It returns a rich
dict with every layer verified independently:

    {
      'usable':           True if every layer is healthy,
      'hostapd_active':   hostapd systemd unit is active,
      'dnsmasq_active':   dnsmasq systemd unit is active,
      'has_gateway_ip':   wlan0 has the configured 10.4.26.89/24,
      'ap_mode':          `iw dev wlan0 info` reports type=AP,
      'stations':         count of associated 802.11 stations,
      'leases':           count of DHCP leases in the hotspot subnet,
      'ssid':             configured SSID,
      'gateway':          configured gateway IP,
      'wlan0_address':    whatever IP wlan0 actually has (for diagnostics),
      'degraded_reasons': list of human-readable strings explaining any
                          gap between "the service is running" and
                          "a phone can join, get an IP, and reach /setup",
    }

`hotspot_status(config)` is kept as a thin wrapper for the existing
callers (blueprints/network.py, blueprints/settings.py) so nothing
breaks during the transition. New call sites should prefer
`hotspot_health`.
"""

import ipaddress
import logging
import os
import re
import shutil
import subprocess
from typing import List

logger = logging.getLogger('skytrack.hotspot')


DEFAULT_LEASES = '/var/lib/misc/dnsmasq.leases'


# ---------------------------------------------------------------------------
# Low-level probes — each one never raises, returns a small primitive.
# ---------------------------------------------------------------------------

def _systemctl_active(unit: str) -> bool:
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() == 'active'
    except Exception:
        return False


def _wlan0_address() -> str:
    """Return the primary IPv4 CIDR on wlan0, or '' if none."""
    try:
        result = subprocess.run(
            ['ip', '-4', 'addr', 'show', 'dev', 'wlan0'],
            capture_output=True, text=True, timeout=3,
        )
        match = re.search(r'inet\s+(\S+)', result.stdout)
        return match.group(1) if match else ''
    except Exception:
        return ''


def _wlan0_is_ap() -> bool:
    """True if `iw dev wlan0 info` reports interface type AP.

    This is the tightest single check that wlan0 is actually beaconing
    as an access point rather than being in managed / monitor / down
    state. hostapd can be "active" according to systemd for a brief
    window after it loses the radio; `iw` reflects the kernel-side
    reality.
    """
    try:
        result = subprocess.run(
            ['iw', 'dev', 'wlan0', 'info'],
            capture_output=True, text=True, timeout=3,
        )
        # Format includes a line like `    type AP`
        return bool(re.search(r'^\s*type\s+AP\b', result.stdout, re.MULTILINE))
    except Exception:
        return False


def _station_count() -> int:
    """Count associated 802.11 stations on wlan0.

    Uses `iw dev wlan0 station dump`. Each station entry starts with a
    'Station <mac>' line, so we just count those.
    """
    try:
        result = subprocess.run(
            ['iw', 'dev', 'wlan0', 'station', 'dump'],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return 0
        return sum(1 for line in result.stdout.splitlines()
                   if line.startswith('Station '))
    except Exception:
        return 0


def _wlan0_link_ok() -> bool:
    """True if the kernel sees a responsive wlan0 interface.

    `iw dev wlan0 link` on an AP-mode interface prints "Not connected."
    which IS normal (that subcommand reports station-mode client links).
    What we really care about here is "can iw talk to the interface at
    all?" — if wlan0 is missing, hung, or the driver has wedged, the
    command exits non-zero. That's the failure mode we want to detect.

    Crucially this is a different signal from `_wlan0_is_ap()`: the
    radio can be in the right mode but still broken, or present but
    hidden behind rfkill. This probe answers the narrower kernel-visible
    question.
    """
    try:
        result = subprocess.run(
            ['iw', 'dev', 'wlan0', 'link'],
            capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


def _rfkill_state() -> dict:
    """Return rfkill soft/hard block state for wlan radios.

    Parses `rfkill list wlan` output like:

        0: phy0: Wireless LAN
                Soft blocked: no
                Hard blocked: no

    Returns `{'available': bool, 'soft_blocked': bool, 'hard_blocked': bool}`.
    `available=False` means rfkill isn't installed or returned nothing
    usable — we treat that as "can't tell, don't flag" (the other
    probes will catch an actually-down radio).
    """
    out = {'available': False, 'soft_blocked': False, 'hard_blocked': False}
    try:
        result = subprocess.run(
            ['rfkill', 'list', 'wlan'],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return out
        out['available'] = True
        for line in result.stdout.splitlines():
            m = re.match(r'\s*Soft blocked:\s*(yes|no)', line, re.IGNORECASE)
            if m and m.group(1).lower() == 'yes':
                out['soft_blocked'] = True
                continue
            m = re.match(r'\s*Hard blocked:\s*(yes|no)', line, re.IGNORECASE)
            if m and m.group(1).lower() == 'yes':
                out['hard_blocked'] = True
    except Exception:
        # rfkill missing or errored — stay in "can't tell" state
        pass
    return out


def _address_in_subnet(cidr: str, subnet: str) -> bool:
    """True if `cidr` (e.g. '10.4.26.89/24') lies inside `subnet`."""
    if not cidr or not subnet:
        return False
    try:
        addr = ipaddress.ip_interface(cidr)
        net = ipaddress.ip_network(subnet, strict=False)
        return addr.ip in net
    except Exception:
        return False


def _count_leases_in_subnet(leases_path: str, subnet: str) -> int:
    """Count dnsmasq lease lines whose address is inside `subnet`."""
    if not os.path.exists(leases_path) or not subnet:
        return 0
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except Exception:
        return 0
    count = 0
    try:
        with open(leases_path) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 3:
                    continue
                try:
                    if ipaddress.ip_address(parts[2]) in net:
                        count += 1
                except ValueError:
                    continue
    except Exception as e:
        logger.debug('lease count error: %s', e)
    return count


# ---------------------------------------------------------------------------
# Top-level: the single source of truth for "is the hotspot usable?"
# ---------------------------------------------------------------------------

def hotspot_health(config) -> dict:
    """Deep-probe every layer of the hotspot stack.

    Returns a rich dict suitable for `/api/hotspot/health`. See the
    module docstring for the exact shape. Callers that only need a
    boolean can read the `usable` key.
    """
    ssid     = config.get('hotspot_ssid', 'SkyTrack-Portal')
    gateway  = config.get('hotspot_gateway', '10.4.26.89')
    subnet   = config.get('hotspot_subnet', '10.4.26.0/24')

    hostapd_active = _systemctl_active('hostapd')
    dnsmasq_active = _systemctl_active('dnsmasq')
    wlan0_addr     = _wlan0_address()
    ap_mode        = _wlan0_is_ap()
    link_ok        = _wlan0_link_ok()
    rfkill         = _rfkill_state()
    stations       = _station_count()
    leases         = _count_leases_in_subnet(DEFAULT_LEASES, subnet)

    # Gateway-IP check: the address on wlan0 must be inside the configured
    # hotspot subnet. We don't insist on exactly `gateway` — a custom
    # install could have shifted it — but we do insist it sits in the
    # same /24 as dnsmasq is handing out, because otherwise nothing works.
    has_gateway_ip = _address_in_subnet(wlan0_addr, subnet)

    reasons: List[str] = []
    # Order matters here: the operator reads these top-to-bottom, so
    # put the root-cause-ish signals first (rfkill, interface down)
    # before the downstream symptoms (hostapd/dnsmasq not active).
    if rfkill['hard_blocked']:
        reasons.append('wlan radio hard-blocked (rfkill)')
    if rfkill['soft_blocked']:
        reasons.append('wlan radio soft-blocked (rfkill)')
    if not link_ok:
        reasons.append('wlan0 interface state broken (iw link failed)')
    if not hostapd_active:
        reasons.append('hostapd not active')
    if not dnsmasq_active:
        reasons.append('dnsmasq not active')
    if not has_gateway_ip:
        reasons.append(f'wlan0 has no IP in {subnet}')
    if hostapd_active and not ap_mode:
        # hostapd claims to be up but the radio isn't in AP mode —
        # usually means a driver / rfkill / interface-busy race.
        reasons.append('wlan0 not in AP mode')

    usable = (hostapd_active
              and dnsmasq_active
              and has_gateway_ip
              and ap_mode
              and link_ok
              and not rfkill['hard_blocked']
              and not rfkill['soft_blocked'])

    return {
        'usable':             usable,
        'hostapd_active':     hostapd_active,
        'dnsmasq_active':     dnsmasq_active,
        'has_gateway_ip':     has_gateway_ip,
        'ap_mode':            ap_mode,
        'link_ok':            link_ok,
        'rfkill_available':   rfkill['available'],
        'rfkill_soft_blocked': rfkill['soft_blocked'],
        'rfkill_hard_blocked': rfkill['hard_blocked'],
        'stations':           stations,
        'leases':             leases,
        'ssid':               ssid,
        'gateway':            gateway,
        'subnet':             subnet,
        'wlan0_address':      wlan0_addr,
        'degraded_reasons':   reasons,
    }


# ---------------------------------------------------------------------------
# Back-compat wrappers — existing callers still use these.
# ---------------------------------------------------------------------------

def hotspot_status(config) -> dict:
    """Legacy shape used by blueprints/network.py and blueprints/settings.py.

    Internally sources from the deep probe so the "enabled" field now
    reflects actual usability rather than just the hostapd systemd unit.
    """
    h = hotspot_health(config)
    return {
        'enabled':       h['usable'],
        'dnsmasq':       h['dnsmasq_active'],
        'ssid':          h['ssid'],
        'gateway':       h['gateway'],
        'subnet':        h['subnet'],
        'channel':       config.get('hotspot_channel', 6),
        'clients':       list_clients(),
        'wlan0_address': h['wlan0_address'],
        # Rich truth for any caller that wants to render the degraded state:
        'health':        h,
    }


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
    h = hotspot_health(config)
    if h['usable']:
        return {'ok': True,
                'message': f"SSID {h['ssid']} usable on wlan0 ({h['wlan0_address']}), "
                           f"{h['stations']} station(s), {h['leases']} lease(s)"}
    return {'ok': False,
            'message': 'hotspot not usable: ' + ', '.join(h['degraded_reasons'] or ['unknown'])}
