"""Auth blueprint — first-boot setup wizard, admin login, super-user.

This blueprint is **not** a global gate. The portal is public by default
and individual routes opt in to admin protection via
`@auth_lib.login_required(ROLE_ADMIN)`. The only thing this blueprint
does globally is redirect to /setup when the device hasn't been
activated yet, and only for the small handful of endpoints that need
that nudge (root, dashboard).

Endpoints
---------
  GET  /                  → root redirect (setup | dashboard)
  GET  /healthz           → liveness probe (always public)
  GET  /setup             → first-boot wizard page
  POST /setup             → submit wizard form
  GET  /login             → fallback full-page login (modal is preferred)
  POST /login             → admin login (PIN only)
  POST /api/auth/login    → JSON-only admin login (used by the modal)
  GET  /api/auth/status   → session snapshot for the front-end shell
  POST /logout            → clear session
  GET  /superuser         → hidden super-user login page
  POST /superuser         → super-user verify
"""

import logging
import threading
import time

from flask import (Blueprint, current_app, jsonify, redirect, render_template,
                   request, url_for)

# Top-level auth module aliased to avoid colliding with `blueprints.auth`.
import auth as auth_lib
import device_id
import logs_svc
import onboarding

logger = logging.getLogger('skytrack.auth_bp')

auth_bp = Blueprint('auth', __name__)


# ---------------------------------------------------------------------------
# Liveness + root redirect
# ---------------------------------------------------------------------------

@auth_bp.route('/healthz')
def healthz():
    """Always-public liveness probe used by systemd, watchdog, CI.

    CORS: the on-device Chromium splash page is loaded from a `file://`
    URL (see `kiosk/xinitrc`) and polls this endpoint to decide when the
    backend is up and it's safe to redirect to `/kiosk`. Chromium treats
    a `file://` origin as "null" and will block the response body (and
    therefore the .ok check in splash/index.html) unless we explicitly
    opt in via `Access-Control-Allow-Origin: *`. Without this header the
    splash used to get stuck polling forever even though Flask was
    answering — the handoff from splash to /kiosk silently never fired.
    """
    resp = jsonify({
        'ok': True,
        'configured': auth_lib.is_configured(),
        'device': current_app.config.get('DEVICE_ID'),
    })
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@auth_bp.route('/')
def root():
    """Land on /setup if the device isn't activated yet, otherwise on the
    public dashboard. Never bounces through a login screen."""
    if not auth_lib.is_configured():
        return redirect(url_for('auth.setup_page'))
    return redirect(url_for('dashboard.index'))


# ---------------------------------------------------------------------------
# Kiosk display — the on-device fullscreen Chromium target
# ---------------------------------------------------------------------------
#
# Distinct from the admin's /setup wizard. The admin walks through /setup
# on their phone over the SkyTrack-Portal hotspot. The on-device display
# is a Chromium kiosk pointed at /kiosk and renders the appropriate
# screen for the current `onboarding.stage`:
#
#   boot_video         one-shot intro video (fresh boot)
#   setup_video        looping setup explainer
#   pin_required       on-screen PIN keypad (first entry)
#   pin_confirm        on-screen PIN keypad (re-entry)
#   setup_offer        "Set Up Now / Skip" prompt
#   setup_now_hotspot  hotspot credentials card
#   operational        permanent live dashboard
#
# kiosk.js polls /api/onboarding/state every ~1.5s so the kiosk advances
# in lockstep with whatever happens on the admin's phone (or another
# kiosk operator). It never navigates away from /kiosk except to the
# file:// splash if the backend dies (handled by the runtime watchdog).

@auth_bp.route('/kiosk')
def kiosk_display():
    """Fullscreen kiosk landing page for the on-device Chromium display."""
    cfg = current_app.skytrack_config
    identity = current_app.config.get('DEVICE_RECORD', {})
    onb_state = onboarding.get_state(cfg)
    return render_template(
        'kiosk.html',
        device_id=current_app.config.get('DEVICE_ID', ''),
        device=identity,
        ssid=cfg.get('hotspot_ssid', 'SkyTrack-Portal'),
        gateway=cfg.get('hotspot_gateway', '10.4.26.89'),
        kiosk_configured=auth_lib.is_configured(),
        onboarding_state=onb_state,
        onboarding_stage=onb_state.get('stage') or 'boot_video',
    )


# ---------------------------------------------------------------------------
# First-boot wizard — first-client lock + 10-min timeout
# ---------------------------------------------------------------------------

_setup_lock = {
    'first_client_ip': None,
    'first_seen_at': 0,
    'timeout_seconds': 600,
}
_setup_lock_mutex = threading.Lock()


def _setup_locked_to_other(ip: str) -> bool:
    now = int(time.time())
    with _setup_lock_mutex:
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
        return redirect(url_for('dashboard.index'))
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
    cfg = current_app.skytrack_config

    # Drive the onboarding state machine. set_pin() advances the stage
    # to setup_offer; the phone wizard already shows the hotspot password
    # card on the next screen, so jump straight to operational and let
    # the kiosk follow along on its next poll.
    try:
        onboarding.set_pin(pin, config=cfg)
        onboarding.advance(to='operational', config=cfg)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    rec = auth_lib.read_auth() or {}
    auth_lib.login_as(auth_lib.ROLE_ADMIN)
    logs_svc.log_portal('admin', 'first_boot_setup_complete', {
        'device': current_app.config.get('DEVICE_ID'),
    })
    return jsonify({
        'ok': True,
        'next': url_for('dashboard.index'),
        'hotspot_password': rec.get('hotspot_password'),
    })


# ---------------------------------------------------------------------------
# Login / logout — admin only (no PIN role)
# ---------------------------------------------------------------------------

@auth_bp.route('/login')
def login_page():
    """Fallback full-page login. The preferred path is the modal that
    `static/js/auth.js` opens whenever a protected route returns 401."""
    if not auth_lib.is_configured():
        return redirect(url_for('auth.setup_page'))
    next_url = request.args.get('next') or url_for('dashboard.index')
    return render_template('login.html', next=next_url)


def _do_admin_login():
    """Shared login impl used by both /login and /api/auth/login."""
    payload = request.get_json(silent=True) or request.form
    pin = (payload.get('pin') or '').strip()
    ip = request.remote_addr or 'unknown'

    if auth_lib.is_locked_out(ip, auth_lib.ROLE_ADMIN):
        return jsonify({'ok': False, 'error': 'temporary lockout — try again later'}), 429

    ok = auth_lib.verify_admin_credential(pin=pin)
    auth_lib.record_attempt(ip, auth_lib.ROLE_ADMIN, ok)
    if not ok:
        return jsonify({'ok': False, 'error': 'invalid PIN'}), 401

    auth_lib.login_as(auth_lib.ROLE_ADMIN)
    logs_svc.log_portal('admin', 'login_success', {'ip': ip, 'method': 'pin'})
    next_url = (payload.get('next') or '').strip() or url_for('dashboard.index')
    return jsonify({
        'ok': True,
        'next': next_url,
        'role': auth_lib.ROLE_ADMIN,
        'method': 'pin',
    })


@auth_bp.route('/login', methods=['POST'])
def login_submit():
    return _do_admin_login()


@auth_bp.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    """JSON-only sibling of POST /login, used by the in-page modal."""
    return _do_admin_login()


@auth_bp.route('/api/auth/status')
def api_auth_status():
    """Used by the topbar and the modal to know who is signed in."""
    return jsonify(auth_lib.session_state())


@auth_bp.route('/logout', methods=['GET', 'POST'])
def logout():
    role = auth_lib.current_role()
    if role:
        logs_svc.log_portal(role, 'logout', {'ip': request.remote_addr})
    auth_lib.logout()
    if request.method == 'POST' or request.path.startswith('/api/'):
        return jsonify({'ok': True})
    return redirect(url_for('dashboard.index'))


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

    auth_lib.login_as(auth_lib.ROLE_SUPER)
    logs_svc.log_portal('super', 'login_success', {'ip': ip, 'username': username})
    return jsonify({'ok': True, 'next': url_for('super.console')})
