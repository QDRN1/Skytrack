"""Auth blueprint — first-boot setup wizard, PIN/admin login, super-user.

This is the only blueprint that sees unauthenticated traffic. Every other
blueprint sits behind `@auth_lib.login_required(...)`.

Endpoints:
  GET  /                  → root redirect (setup | login | dashboard)
  GET  /healthz           → liveness probe (always public)
  GET  /setup             → first-boot wizard page
  POST /setup             → submit wizard form
  GET  /login             → PIN keypad page
  POST /login             → PIN or admin password
  GET  /logout            → clear session
  GET  /superuser         → hidden super-user login page
  POST /superuser         → super-user verify
"""

import logging
import time

from flask import (Blueprint, current_app, jsonify, redirect, render_template,
                   request, url_for)

# Top-level auth module (the credential store / decorator). Aliased to
# avoid colliding with the `blueprints.auth` package name.
import auth as auth_lib
import device_id
import logs_svc

logger = logging.getLogger('skytrack.auth_bp')

auth_bp = Blueprint('auth', __name__)


# ---------------------------------------------------------------------------
# Liveness + root redirect
# ---------------------------------------------------------------------------

@auth_bp.route('/healthz')
def healthz():
    """Always-public liveness probe used by systemd, watchdog scripts, CI."""
    return jsonify({
        'ok': True,
        'configured': auth_lib.is_configured(),
        'device': current_app.config.get('DEVICE_ID'),
    })


@auth_bp.route('/')
def root():
    if not auth_lib.is_configured():
        return redirect(url_for('auth.setup_page'))
    if not auth_lib.current_role():
        return redirect(url_for('auth.login_page'))
    return redirect(url_for('dashboard.index'))


# ---------------------------------------------------------------------------
# First-boot wizard — first-client lock + 10-min timeout
# ---------------------------------------------------------------------------

_setup_lock = {
    'first_client_ip': None,
    'first_seen_at': 0,
    'timeout_seconds': 600,
}


def _setup_locked_to_other(ip: str) -> bool:
    now = int(time.time())
    locked_ip = _setup_lock['first_client_ip']
    locked_at = _setup_lock['first_seen_at']
    if locked_ip and (now - locked_at) > _setup_lock['timeout_seconds']:
        _setup_lock['first_client_ip'] = None
        _setup_lock['first_seen_at'] = 0
        return False
    if locked_ip is None:
        _setup_lock['first_client_ip'] = ip
        _setup_lock['first_seen_at'] = now
        return False
    return locked_ip != ip


@auth_bp.route('/setup')
def setup_page():
    if auth_lib.is_configured():
        return redirect(url_for('auth.login_page'))
    ip = request.remote_addr or 'unknown'
    if _setup_locked_to_other(ip):
        return render_template('setup_locked.html'), 423
    identity = current_app.config.get('DEVICE_RECORD', {})
    cfg = current_app.skytrack_config
    return render_template(
        'setup.html',
        device=identity,
        radar_url=device_id.radar_url(identity) if identity else '',
        ssid=cfg.get('hotspot_ssid', 'SkyTrack-Portal'),
        gateway=cfg.get('hotspot_gateway', '10.4.26.89'),
    )


@auth_bp.route('/setup', methods=['POST'])
def setup_submit():
    if auth_lib.is_configured():
        return jsonify({'ok': False, 'error': 'already configured'}), 409

    ip = request.remote_addr or 'unknown'
    if _setup_locked_to_other(ip):
        return jsonify({'ok': False, 'error': 'setup locked to another client'}), 423

    payload = request.get_json(silent=True) or request.form
    pin = (payload.get('pin') or '').strip()
    admin_pw = payload.get('admin_password') or ''

    try:
        rec = auth_lib.complete_setup(pin, admin_pw)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    timeout = int(current_app.skytrack_config.get('session_timeout_hours', 8)) * 3600
    auth_lib.login_as(auth_lib.ROLE_ADMIN, timeout_seconds=timeout)
    logs_svc.log_portal('admin', 'first_boot_setup_complete',
                        {'device': current_app.config.get('DEVICE_ID')})
    return jsonify({
        'ok': True,
        'next': url_for('dashboard.index'),
        'hotspot_password': rec.get('hotspot_password'),
    })


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------

@auth_bp.route('/login')
def login_page():
    if not auth_lib.is_configured():
        return redirect(url_for('auth.setup_page'))
    return render_template('login.html')


@auth_bp.route('/login', methods=['POST'])
def login_submit():
    payload = request.get_json(silent=True) or request.form
    role = (payload.get('role') or 'pin').strip().lower()
    ip = request.remote_addr or 'unknown'

    if auth_lib.is_locked_out(ip, role):
        return jsonify({'ok': False, 'error': 'temporary lockout — try again later'}), 429

    timeout = int(current_app.skytrack_config.get('session_timeout_hours', 8)) * 3600

    if role == auth_lib.ROLE_PIN:
        pin = (payload.get('pin') or '').strip()
        ok = auth_lib.verify_pin(pin)
        auth_lib.record_attempt(ip, auth_lib.ROLE_PIN, ok)
        if not ok:
            return jsonify({'ok': False, 'error': 'invalid PIN'}), 401
        auth_lib.login_as(auth_lib.ROLE_PIN, timeout_seconds=timeout)
        logs_svc.log_portal('pin', 'login_success', {'ip': ip})
        return jsonify({'ok': True, 'next': url_for('dashboard.index')})

    if role == auth_lib.ROLE_ADMIN:
        password = payload.get('password') or ''
        ok = auth_lib.verify_admin(password)
        auth_lib.record_attempt(ip, auth_lib.ROLE_ADMIN, ok)
        if not ok:
            return jsonify({'ok': False, 'error': 'invalid admin password'}), 401
        auth_lib.login_as(auth_lib.ROLE_ADMIN, timeout_seconds=timeout)
        logs_svc.log_portal('admin', 'login_success', {'ip': ip})
        return jsonify({'ok': True, 'next': url_for('dashboard.index')})

    return jsonify({'ok': False, 'error': 'unknown role'}), 400


@auth_bp.route('/logout', methods=['GET', 'POST'])
def logout():
    role = auth_lib.current_role()
    if role:
        logs_svc.log_portal(role, 'logout', {'ip': request.remote_addr})
    auth_lib.logout()
    return redirect(url_for('auth.login_page'))


# ---------------------------------------------------------------------------
# Hidden super-user gate (also reachable via 5-tap on the topbar logo)
# ---------------------------------------------------------------------------

@auth_bp.route('/superuser')
def superuser_page():
    if auth_lib.current_role() == auth_lib.ROLE_SUPER:
        return redirect(url_for('super.console'))
    return render_template('superuser_login.html')


@auth_bp.route('/superuser', methods=['POST'])
def superuser_login():
    payload = request.get_json(silent=True) or request.form
    username = (payload.get('username') or '').strip()
    password = payload.get('password') or ''
    ip = request.remote_addr or 'unknown'

    if auth_lib.is_locked_out(ip, auth_lib.ROLE_SUPER):
        return jsonify({'ok': False, 'error': 'temporary lockout'}), 429

    ok = auth_lib.verify_super(username, password)
    auth_lib.record_attempt(ip, auth_lib.ROLE_SUPER, ok)
    if not ok:
        return jsonify({'ok': False, 'error': 'invalid credentials'}), 401

    timeout = int(current_app.skytrack_config.get('session_timeout_hours', 8)) * 3600
    auth_lib.login_as(auth_lib.ROLE_SUPER, timeout_seconds=timeout)
    logs_svc.log_portal('super', 'login_success', {'ip': ip, 'username': username})
    return jsonify({'ok': True, 'next': url_for('super.console')})


# ---------------------------------------------------------------------------
# Global setup-wizard guard — first request from any user that hits a non-
# auth route gets redirected to /setup if the device is not configured.
# Setup, login, healthz, static, and super-user endpoints bypass this.
# ---------------------------------------------------------------------------

_BYPASS_ENDPOINTS = {
    'auth.healthz',
    'auth.root',
    'auth.setup_page',
    'auth.setup_submit',
    'auth.login_page',
    'auth.login_submit',
    'auth.superuser_page',
    'auth.superuser_login',
    'static',
}


@auth_bp.before_app_request
def _enforce_setup():
    if auth_lib.is_configured():
        return None
    if (request.endpoint or '') in _BYPASS_ENDPOINTS:
        return None
    if request.path.startswith('/static/'):
        return None
    return redirect(url_for('auth.setup_page'))
