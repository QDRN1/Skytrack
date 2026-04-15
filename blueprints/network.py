"""Network blueprint — Settings → Network tab + status APIs.

This module owns the legacy `/network` page (kept for back-compat with the
top-bar nav link and direct bookmarks) but every privileged action is
delegated to the canonical `/api/settings/network/...` endpoints in the
settings blueprint so we never have two implementations of the same verb
that drift apart. In particular:

  * Hotspot password generation uses `auth.set_hotspot_password()` — same
    function the Settings → Network card calls. There is exactly one
    write path for that secret.
  * APN edits go through `network_svc.set_cellular_apn()` which routes
    through `net_backend.set_apn()` and re-activates the NM gsm
    connection. Same code path the Settings page uses.
  * Hotspot service restarts go through `hotspot.restart_hotspot()`.

`/api/network/status` remains public because the topbar uses it before
sign-in. Everything else is admin-gated.
"""

import logging

from flask import (Blueprint, current_app, jsonify, render_template, request)

import auth as auth_lib
import hotspot
import logs_svc
import network_svc

logger = logging.getLogger('skytrack.network_bp')

network_bp = Blueprint('network', __name__)


# ---------------------------------------------------------------------------
# Page (rendered as a partial inside the Settings tabs)
# ---------------------------------------------------------------------------

@network_bp.route('/network')
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def index():
    return render_template('network/index.html')


# ---------------------------------------------------------------------------
# Public status — used by the topbar pill on every page (incl. dashboard)
# ---------------------------------------------------------------------------

@network_bp.route('/api/network/status')
def api_status():
    cfg = current_app.skytrack_config
    cached = getattr(current_app, 'cellular_state', {}) or {}
    status = network_svc.get_network_status(cfg)
    # Prefer the daemon-cached cellular state (fresher than a cold mmcli call)
    if cached.get('detected'):
        merged = dict(status.get('cellular') or {})
        merged.update(cached)
        if not merged.get('apn'):
            merged['apn'] = (status.get('cellular') or {}).get('apn') or ''
        status['cellular'] = merged
    return jsonify(status)


@network_bp.route('/api/network/state')
def api_state():
    """Phase 1 — normalized network truth model.

    Canonical endpoint the kiosk network card and the onboarding flow
    consume. Every dimension is a hard fact read from nmcli / mmcli /
    /proc/net/route, never inferred. The shape is fixed and documented
    on `net_backend.normalized_network_state()`:

        wifi:        {enabled, connected, ssid, signal_pct, interface, ipv4}
        cellular:    {enabled, modem_present, modem_registered,
                      bearer_connected, nm_profile_present, nm_profile_active,
                      nm_profile_name, ip_assigned, route_active,
                      apn, apn_source, carrier, signal_pct, access_tech,
                      interface, ipv4}
        hotspot:     {enabled, configured, service_running, dhcp_active,
                      dhcp_installed, actually_usable, ssid, local_url,
                      gateway, password?, seconds_remaining?}
        routing:     {primary, primary_interface, internet_reachable}
        preferences: {preferred_uplink}
        backend:     'nm' | 'direct'

    `password` and `seconds_remaining` are only present for admin
    callers — the kiosk pre-auth view never sees the hotspot password.
    """
    cfg = current_app.skytrack_config
    state = network_svc.normalized_state(cfg)

    # Daemon-cached cellular fields (mmcli is slow on a cold call) win
    # over the freshly-polled value when they're newer.
    cached = getattr(current_app, 'cellular_state', {}) or {}
    if cached.get('detected'):
        cellular = state.get('cellular') or {}
        for k in ('signal_pct', 'access_tech', 'carrier'):
            if cached.get(k) is not None:
                cellular[k] = cached.get(k)
        state['cellular'] = cellular

    # Admin-only enrichments — never leak the hotspot password to a
    # pre-auth caller.
    if auth_lib.current_role() == auth_lib.ROLE_ADMIN:
        try:
            rec = auth_lib.read_auth() or {}
            state['hotspot']['password'] = rec.get('hotspot_password') or ''
        except Exception:
            pass

    return jsonify(state)


# ---------------------------------------------------------------------------
# Hotspot — credentials + regenerate + restart
#
# These exist so the legacy /network page keeps working. They are thin
# pass-throughs to the same code Settings → Network calls; there is one
# implementation of each verb in the codebase, not two.
# ---------------------------------------------------------------------------

@network_bp.route('/api/network/hotspot/credentials')
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_hotspot_credentials():
    rec = auth_lib.read_auth()
    cfg = current_app.skytrack_config
    return jsonify({
        'ssid': cfg.get('hotspot_ssid', 'SkyTrack-Portal'),
        'password': rec.get('hotspot_password'),
        'gateway': cfg.get('hotspot_gateway'),
        'subnet': cfg.get('hotspot_subnet'),
    })


@network_bp.route('/api/network/hotspot/regenerate', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_hotspot_regenerate():
    """Rotate password AND bounce hostapd atomically.

    See blueprints/settings.py:api_hotspot_regenerate for why the
    rotate-then-restart dance is a single request now (phase 2).
    """
    if hotspot.ssh_on_wlan0():
        return jsonify({
            'ok': False,
            'message': 'refused: your SSH session is on wlan0 — rotating the hotspot password would lock you out. Run from eth0 or a console.',
        }), 409
    pw = auth_lib.set_hotspot_password(None)
    restart = hotspot.restart_hotspot()
    logs_svc.log_network('hotspot_password_regenerated', {
        'len': len(pw or ''),
        'source': 'network_page',
        'restart_ok': bool(restart.get('ok')),
    })
    return jsonify({
        'ok': True,
        'password': pw,
        'restart_ok': bool(restart.get('ok')),
        'restart_message': restart.get('message') or '',
    })


@network_bp.route('/api/network/hotspot/restart', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_hotspot_restart():
    result = hotspot.restart_hotspot()
    logs_svc.log_network('hotspot_restart', result)
    status = 200 if result.get('ok') else 409
    return jsonify(result), status


@network_bp.route('/api/hotspot/health')
def api_hotspot_health():
    """Deep hotspot health probe — PUBLIC, no secrets.

    The kiosk polls this from the setup_now_hotspot card to show a
    live "waiting for your device…" / "1 device connected" status
    line. It is intentionally unauthenticated because the kiosk is
    rendered pre-login and the hotspot card is exactly the flow that
    needs live feedback before the admin has a session.

    Exposes only health flags + counts. No SSID password, no MAC
    addresses, no lease details — just the truth model from
    `hotspot.hotspot_health()`. Admin-authed callers who need more
    should use `/api/network/hotspot/credentials`.
    """
    cfg = current_app.skytrack_config
    h = hotspot.hotspot_health(cfg)
    resp = jsonify({
        'usable':           h['usable'],
        'hostapd_active':   h['hostapd_active'],
        'dnsmasq_active':   h['dnsmasq_active'],
        'has_gateway_ip':   h['has_gateway_ip'],
        'ap_mode':          h['ap_mode'],
        'stations':         h['stations'],
        'leases':           h['leases'],
        'ssid':             h['ssid'],
        'gateway':          h['gateway'],
        'degraded_reasons': h['degraded_reasons'],
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ---------------------------------------------------------------------------
# Other network endpoints used by the legacy page
# ---------------------------------------------------------------------------

@network_bp.route('/api/network/metered', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_metered_toggle():
    payload = request.get_json(silent=True) or {}
    val = bool(payload.get('metered_connection'))
    current_app.skytrack_config['metered_connection'] = val
    logs_svc.log_network('metered_toggle', {'metered': val})
    return jsonify({'ok': True, 'metered_connection': val})


@network_bp.route('/api/network/cellular/apn', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_cellular_apn_legacy():
    """Legacy alias: same write path as Settings → Network → Apply APN.

    Persists to config.yaml AND re-activates the NM connection so the
    new APN is picked up by the live cellular link.
    """
    payload = request.get_json(silent=True) or {}
    apn = (payload.get('apn') or '').strip()
    if not apn:
        return jsonify({'ok': False, 'message': 'apn required'}), 400
    cfg = current_app.skytrack_config
    cfg['cellular_apn'] = apn
    try:
        from config import save_user_config
        import os
        path = os.environ.get(
            'SKYTRACK_CONFIG',
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '..', 'config.yaml'),
        )
        save_user_config(os.path.abspath(path), {'cellular_apn': apn})
    except Exception as e:
        logger.warning('config persist failed: %s', e)
    result = network_svc.set_cellular_apn(apn)
    logs_svc.log_network('cellular_apn_set', {
        'apn': apn,
        'ok': bool(result.get('ok')),
        'backend': result.get('backend'),
        'source': 'network_page',
    })
    return jsonify({
        'ok': bool(result.get('ok')),
        'apn': apn,
        'backend': result.get('backend'),
        'message': result.get('message') or 'APN updated',
    })
