"""Super-user blueprint — hidden Matrix-styled console.

Not exposed through the main UI. Reachable via:
  • /superuser  (after auth on auth_bp)
  • 5-tap or long-press on the topbar logo (front-end shortcut)

Capabilities:
  • Edit super-user credentials (the only place where this is allowed)
  • Force-regenerate the device ID (collision recovery)
  • Run db.prune()
  • Vacuum the database
  • Wipe / re-seed auth.json (factory reset)
"""

import logging

from flask import Blueprint, current_app, jsonify, render_template, request

import auth as auth_lib
import db
import device_id
import logs_svc

logger = logging.getLogger('skytrack.super_bp')

super_bp = Blueprint('super', __name__)


# ---------------------------------------------------------------------------
# Console page
# ---------------------------------------------------------------------------

@super_bp.route('/super')
@auth_lib.login_required(auth_lib.ROLE_SUPER)
def console():
    identity = current_app.config.get('DEVICE_RECORD', {})
    return render_template(
        'superuser.html',
        identity=identity,
        radar_url=device_id.radar_url(identity) if identity else '',
        schema_version=db.current_version(),
    )


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@super_bp.route('/api/super/credentials', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_SUPER)
def api_change_super_credentials():
    payload = request.get_json(silent=True) or {}
    try:
        auth_lib.change_super_credentials(
            payload.get('username') or '',
            payload.get('password') or '',
        )
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    logs_svc.log_portal('super', 'change_super_credentials', {})
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Device identity recovery
# ---------------------------------------------------------------------------

@super_bp.route('/api/super/device/regenerate', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_SUPER)
def api_regenerate_device_id():
    payload = request.get_json(silent=True) or {}
    reason = payload.get('reason') or 'super-user-forced'
    rec = device_id.regenerate_device_id(reason=reason)
    # Refresh the in-process cached identity
    current_app.config['DEVICE_ID'] = rec['device_id']
    current_app.config['DEVICE_RECORD'] = rec
    current_app.identity = rec
    logs_svc.log_portal('super', 'device_id_regenerate', {
        'new': rec['device_id'], 'reason': reason,
    })
    return jsonify({'ok': True, 'device': rec})


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

@super_bp.route('/api/super/db/prune', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_SUPER)
def api_prune():
    cfg = current_app.skytrack_config
    db.prune(
        sightings_days=int(cfg.get('sightings_retention_days', 7)),
        logs_days=int(cfg.get('logs_retention_days', 30)),
    )
    logs_svc.log_portal('super', 'db_prune', {})
    return jsonify({'ok': True})


@super_bp.route('/api/super/db/vacuum', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_SUPER)
def api_vacuum():
    conn = db.get_conn()
    conn.execute('VACUUM')
    logs_svc.log_portal('super', 'db_vacuum', {})
    return jsonify({'ok': True})


@super_bp.route('/api/super/factory_reset', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_SUPER)
def api_factory_reset():
    """Wipe auth.json and force the wizard to run again. Device ID is preserved."""
    rec = auth_lib._empty_record()  # type: ignore[attr-defined]
    auth_lib.write_auth(rec)
    logs_svc.log_portal('super', 'factory_reset', {})
    return jsonify({'ok': True})
