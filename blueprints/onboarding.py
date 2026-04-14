"""Onboarding blueprint — first-boot state machine API.

This blueprint exposes the explicit onboarding state machine added in
v2.4.0 (Phase 2 of the appliance product-quality push). It replaces the
single `configured` boolean with named stages the kiosk can address
individually so the device can resume in the right place after a power
loss mid-onboarding.

Endpoints
---------
  GET   /api/onboarding/state    Read the current state. Public — the
                                 kiosk needs this before any login.
  POST  /api/onboarding/advance  Move to the next stage. Public until
                                 the device is operational; admin-only
                                 once `stage == operational` so an
                                 attacker can't roll a configured
                                 device back through onboarding.
  POST  /api/onboarding/pin      First-time PIN creation. Public ONLY
                                 while `pin_set == false`; rejected
                                 with 409 once a PIN exists. Use the
                                 standard /api/settings/network/... or
                                 super-user endpoints to change a PIN
                                 after onboarding.

Notes
-----
The Flask app keeps its existing `/setup` and `/kiosk` page routes
unchanged. Phase 3 will introduce new templates that consume this API,
but Phase 2 is intentionally backend-only so the live appliance keeps
booting through the existing /setup wizard until the new UI lands.
"""

import logging

from flask import Blueprint, current_app, jsonify, request

import auth as auth_lib
import logs_svc
import onboarding

logger = logging.getLogger('skytrack.onboarding_bp')

onboarding_bp = Blueprint('onboarding', __name__)


def _cfg() -> dict:
    return current_app.skytrack_config


# ---------------------------------------------------------------------------
# GET /api/onboarding/state
# ---------------------------------------------------------------------------

@onboarding_bp.route('/api/onboarding/state')
def api_state():
    """Return the canonical onboarding state.

    Public so the kiosk page can render before any login. Returns the
    full shape documented on `onboarding.get_state()`.
    """
    return jsonify(onboarding.get_state(_cfg()))


# ---------------------------------------------------------------------------
# POST /api/onboarding/advance
# ---------------------------------------------------------------------------

@onboarding_bp.route('/api/onboarding/advance', methods=['POST'])
def api_advance():
    """Move the persistent stage forward.

    Body: {"to": "<stage>"} or {} for the default forward step.

    Auth model: public while the device is still onboarding, admin-only
    once the device is `operational` so an attacker who reaches the
    kiosk URL can't roll a live appliance back through setup.
    """
    state = onboarding.get_state(_cfg())
    if state['stage'] == 'operational':
        if auth_lib.current_role() not in (auth_lib.ROLE_ADMIN, auth_lib.ROLE_SUPER):
            return jsonify({
                'ok': False,
                'error': 'admin authentication required to re-enter onboarding',
            }), 401

    payload = request.get_json(silent=True) or {}
    target = (payload.get('to') or '').strip() or None

    try:
        new_state = onboarding.advance(to=target, config=_cfg())
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    logs_svc.log_portal('system', 'onboarding_advance', {
        'from': state['stage'],
        'to':   new_state['stage'],
    })
    return jsonify({'ok': True, 'state': new_state})


# ---------------------------------------------------------------------------
# POST /api/onboarding/pin
# ---------------------------------------------------------------------------

@onboarding_bp.route('/api/onboarding/pin', methods=['POST'])
def api_pin():
    """First-time PIN creation.

    Accepted ONLY while `pin_set == false`. The kiosk calls this after
    the operator types and confirms a 4-digit PIN on the keypad. On
    success the stage advances to `setup_offer` and a fresh hotspot
    password is generated so the Set Up Now screen has data to display.

    Once a PIN exists this endpoint returns 409 — use the standard
    Settings → Account flow (admin-gated) to rotate a PIN after the
    appliance is operational.
    """
    state = onboarding.get_state(_cfg())
    if state['pin_set']:
        return jsonify({
            'ok': False,
            'error': 'PIN already set; use settings to rotate it',
        }), 409

    payload = request.get_json(silent=True) or request.form
    pin = (payload.get('pin') or '').strip()
    confirm = (payload.get('confirm') or '').strip()

    # Confirm field is optional: the kiosk does its own first-pass
    # confirmation in JS (re-enter screen) so we just need the final
    # value here. But if the caller does send `confirm` we honor it as
    # belt-and-braces.
    if confirm and confirm != pin:
        return jsonify({'ok': False, 'error': 'PIN confirmation does not match'}), 400

    try:
        new_state = onboarding.set_pin(pin, config=_cfg())
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    logs_svc.log_portal('admin', 'onboarding_pin_created', {
        'len': len(pin),
    })
    return jsonify({
        'ok': True,
        'state': new_state,
        'hotspot_password': (auth_lib.read_auth() or {}).get('hotspot_password'),
    })


# ---------------------------------------------------------------------------
# POST /api/onboarding/reset
# ---------------------------------------------------------------------------

@onboarding_bp.route('/api/onboarding/reset', methods=['POST'])
def api_reset():
    """Rewind to `pin_required`.

    Used by the kiosk when the operator mistypes the confirmation PIN
    on the second screen so we can show a clean entry pad again. Public
    while still onboarding, admin-only once operational.
    """
    state = onboarding.get_state(_cfg())
    if state['stage'] == 'operational':
        if auth_lib.current_role() not in (auth_lib.ROLE_ADMIN, auth_lib.ROLE_SUPER):
            return jsonify({
                'ok': False,
                'error': 'admin authentication required',
            }), 401
    new_state = onboarding.reset_to_pin_required()
    return jsonify({'ok': True, 'state': new_state})
