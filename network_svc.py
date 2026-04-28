"""SkyTrack network facade.

All network reads go through `net_backend` (NetworkManager-first with a
direct-tool fallback). This module adds the higher-level aggregation that
the topbar, /api/network/status, and Settings → Network tiles expect, plus
internet-reachability probing.

Keep this thin — the actual OS-level work lives in `net_backend.py` so the
stack can be standardized in one place.

Standalone supervisor mode (--supervise):
  Runs as a long-lived process for systemd. Periodically probes network
  state and logs transitions. Used by skytrack-network.service.
"""

import argparse
import logging
import socket
import sys
import time
from typing import Optional

import hotspot
import net_backend

logger = logging.getLogger('skytrack.network')


# ---------------------------------------------------------------------------
# Thin re-exports so existing call-sites keep working
# ---------------------------------------------------------------------------

def wifi_status() -> dict:
    return net_backend.wifi_status()


def wifi_scan() -> dict:
    return net_backend.wifi_scan()


def wifi_saved() -> dict:
    return net_backend.wifi_saved()


def wifi_connect(ssid: str, password: Optional[str] = None) -> dict:
    return net_backend.wifi_connect(ssid, password)


def wifi_forget(ssid: str) -> dict:
    return net_backend.wifi_forget(ssid)


def cellular_status() -> dict:
    return net_backend.cellular_status()


def set_cellular_apn(apn: str) -> dict:
    return net_backend.set_apn(apn)


def set_wifi_radio(enabled: bool) -> dict:
    return net_backend.set_radio('wifi', enabled)


def set_cellular_radio(enabled: bool) -> dict:
    return net_backend.set_radio('wwan', enabled)


def backend_info() -> dict:
    return net_backend.backend_info()


def normalized_state(config) -> dict:
    """Phase 1 of the network truth model.

    Thin re-export so blueprints can import from the service layer
    instead of reaching into net_backend directly. The dict shape is
    documented on `net_backend.normalized_network_state()`.
    """
    return net_backend.normalized_network_state(config)


def default_route_interface() -> Optional[str]:
    return net_backend.active_uplink().get('interface') or None


def classify_link(iface: Optional[str]) -> str:
    """Map an interface name to a friendly link type label."""
    return net_backend._classify(iface)


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
# Aggregate status consumed by the topbar + Settings tiles
# ---------------------------------------------------------------------------

def get_network_status(config) -> dict:
    """Return the full picture the UI needs in one call.

    The dict intentionally keeps historical keys (`internet`, `primary`,
    `primary_interface`) so existing callers don't break, and adds:

      * `backend` — 'nm' | 'direct' — which stack is live
      * `uplink`  — {interface, kind, nm_connection, ...}
      * `cellular.apn` — currently-configured APN, if any
    """
    wifi     = wifi_status()
    cellular = cellular_status()

    # Config override for APN always wins over what NM currently has, so the
    # UI shows the value the operator asked for even before nmcli reports it
    # back (e.g. during a brief modem reprovision).
    cfg_apn = (config.get('cellular_apn') or '').strip()
    if cfg_apn and not cellular.get('apn'):
        cellular['apn'] = cfg_apn
        cellular['apn_source'] = 'config'

    hs       = hotspot.hotspot_status(config)
    online   = internet_reachable()
    uplink   = net_backend.active_uplink()
    primary  = uplink['kind']

    # Hotspot mode on wlan0 *excludes* wifi-client on the same radio.
    if hs.get('enabled'):
        wifi = {**wifi, 'connected': False, 'ssid': '', 'signal_dbm': None, 'signal_pct': 0}

    return {
        'backend':           net_backend.detect_backend(),
        'wifi':              wifi,
        'cellular':          cellular,
        'hotspot':           hs,
        'internet':          online,
        'online':            online,
        'metered':           bool(config.get('metered_connection')),
        'primary':           primary,
        'primary_interface': uplink.get('interface') or '',
        'uplink':            uplink,
    }


# ---------------------------------------------------------------------------
# Standalone supervisor entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SkyTrack network supervisor')
    parser.add_argument('--supervise', action='store_true',
                        help='Run the long-lived supervisor loop')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )

    if not args.supervise:
        logger.error('Use --supervise to start the supervisor loop')
        sys.exit(1)

    from config import load_config
    cfg = load_config()

    logger.info('Network supervisor starting (poll every 30s)')
    last_primary = None
    last_online = None
    while True:
        try:
            status = get_network_status(cfg)
            primary = status.get('primary', 'none')
            online = status.get('internet', False)
            if primary != last_primary or online != last_online:
                logger.info('Network state: primary=%s online=%s', primary, online)
                last_primary = primary
                last_online = online
        except Exception as e:
            logger.warning('Network probe error: %s', e)
        time.sleep(30)
