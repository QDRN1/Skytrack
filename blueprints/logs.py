"""Logs blueprint — Settings → Logs tab and JSON tail endpoints.

Surfaces:
  • Portal audit log    (portal_logs table)
  • Network event log   (network_logs table)
  • Application log     (tail of /var/log/skytrack/skytrack.log)
"""

import logging
import os

from flask import Blueprint, current_app, jsonify, render_template, request

import auth as auth_lib
import logs_svc

logger = logging.getLogger('skytrack.logs_bp')

logs_bp = Blueprint('logs', __name__)


# ---------------------------------------------------------------------------
# Page (rendered as a partial inside the Settings tabs)
# ---------------------------------------------------------------------------

@logs_bp.route('/logs')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def index():
    return render_template('logs/index.html')


# ---------------------------------------------------------------------------
# JSON tails
# ---------------------------------------------------------------------------

@logs_bp.route('/api/logs/portal')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_portal_tail():
    limit = int(request.args.get('limit', 100))
    return jsonify(logs_svc.tail_portal(limit=limit))


@logs_bp.route('/api/logs/network')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_network_tail():
    limit = int(request.args.get('limit', 100))
    return jsonify(logs_svc.tail_network(limit=limit))


@logs_bp.route('/api/logs/app')
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_app_tail():
    """Tail the application log file. Admin-only since it can leak."""
    limit = int(request.args.get('limit', 200))
    log_dir = current_app.skytrack_config.get('log_dir', '/var/log/skytrack')
    path = os.path.join(log_dir, 'skytrack.log')
    lines = []
    try:
        if os.path.exists(path):
            with open(path, 'r', errors='replace') as f:
                lines = f.readlines()[-limit:]
    except OSError as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True, 'lines': [ln.rstrip() for ln in lines]})
