"""Network blueprint — Settings → Network tab + status APIs.

Phase 1 is read-only for WiFi / cellular. Hotspot has a Restart action
that shells out to scripts/hotspot_apply.sh under sudo.
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
# Status APIs
# ---------------------------------------------------------------------------

@network_bp.route('/api/network/status')
def api_status():
    """Public — used by the topbar pill on every page (including dashboard)."""
    cfg = current_app.skytrack_config
    cached = getattr(current_app, 'cellular_state', {}) or {}
    status = network_svc.get_network_status(cfg)
    # Prefer the daemon-cached cellular state (fresher than a cold mmcli call)
    if cached.get('detected'):
        status['cellular'] = cached
    return jsonify(status)


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
    pw = auth_lib.set_hotspot_password()
    logs_svc.log_network('hotspot_password_regenerated', {'len': len(pw)})
    return jsonify({'ok': True, 'password': pw})


@network_bp.route('/api/network/hotspot/restart', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_hotspot_restart():
    result = hotspot.restart_hotspot()
    logs_svc.log_network('hotspot_restart', result)
    return jsonify(result)


@network_bp.route('/api/network/metered', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_metered_toggle():
    payload = request.get_json(silent=True) or {}
    val = bool(payload.get('metered_connection'))
    current_app.skytrack_config['metered_connection'] = val
    logs_svc.log_network('metered_toggle', {'metered': val})
    return jsonify({'ok': True, 'metered_connection': val})
