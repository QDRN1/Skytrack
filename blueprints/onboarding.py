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
import hotspot
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
    full shape documented on `onboarding.get_state()`, plus a
    `hotspot_password` field when the current stage is
    `setup_now_hotspot` so the on-device kiosk can paint the
    credentials reveal card after a Chromium reload mid-flow.
    """
    state = onboarding.get_state(_cfg())
    if state.get('stage') == 'setup_now_hotspot':
        rec = auth_lib.read_auth() or {}
        state['hotspot_password'] = rec.get('hotspot_password') or ''
        cfg = _cfg()
        state['hotspot_ssid'] = cfg.get('hotspot_ssid', 'SkyTrack-Portal')
        state['hotspot_gateway'] = cfg.get('hotspot_gateway', '10.4.26.89')
    return jsonify(state)


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
# POST /api/onboarding/skip
# ---------------------------------------------------------------------------
#
# Phase 2 stabilization: the escape hatch. An operator can skip forward
# to `operational` from the welcome, pin, or hotspot screens. This is
# the ONE endpoint that enforces the logging contract: when source is
# `hotspot` we capture the full `/api/hotspot/health` snapshot into the
# network-logs table so a future audit can answer "why did the operator
# give up on hotspot setup on device X?"
#
# Valid sources:
#   welcome  → "Set up later" on the new welcome card
#   pin      → "Skip for now" on the PIN keypad
#   hotspot  → "Continue without setup" on the hotspot reveal card
#
# We never accept source='setup_offer' here — that screen already has
# its own Skip For Now button that routes through /api/onboarding/advance
# with {to: operational}, and we don't want to change that path.
#
# Logging is ADDITIVE: if the log insert fails the endpoint still
# advances the state and returns 200. The UI is never blocked by
# logging failure. (See the try/except around log_network + log_portal.)

_ALLOWED_SKIP_SOURCES = ('welcome', 'pin', 'hotspot')


@onboarding_bp.route('/api/onboarding/skip', methods=['POST'])
def api_skip():
    """Skip forward to `operational` from a whitelisted onboarding stage.

    Body: {"source": "welcome" | "pin" | "hotspot"}

    Semantics per source:

      welcome:  stage=operational,  configured=true,  pin_set unchanged.
                No hotspot probe; log_portal a simple note.
      pin:      stage=operational,  configured=true,  pin_set=false.
                No hotspot probe; log_portal a simple note.
      hotspot:  stage=operational,  configured=true,  pin_set unchanged.
                Probes hotspot_health() BEFORE advancing and stores the
                full snapshot (usable, ap_mode, stations, degraded_reasons,
                and everything else the truth probe surfaces) in
                network_logs as event='hotspot_skipped'.

    Public — the kiosk is pre-auth. Rejected with 409 if the device is
    already operational (prevents rollback-style abuse), except that
    calling with source=hotspot while operational still records the
    health snapshot for diagnostics and returns 200 with a no-op state.
    """
    payload = request.get_json(silent=True) or {}
    source = (payload.get('source') or '').strip().lower()

    if source not in _ALLOWED_SKIP_SOURCES:
        return jsonify({
            'ok': False,
            'error': f'source must be one of {list(_ALLOWED_SKIP_SOURCES)}',
        }), 400

    state = onboarding.get_state(_cfg())
    current_stage = state['stage']

    # Hotspot snapshot is captured BEFORE the advance so the log always
    # reflects the state that drove the operator's decision to skip,
    # not the state a moment later after the stage flip. Probe is
    # wrapped in its own try because a degraded subsystem (e.g. iw
    # missing on a dev box) must never wedge the skip.
    hotspot_snapshot = None
    if source == 'hotspot':
        try:
            hotspot_snapshot = hotspot.hotspot_health(_cfg())
        except Exception as e:
            logger.debug('hotspot skip: health probe failed: %s', e)
            hotspot_snapshot = {'probe_error': str(e)}

    # Refuse re-entry from operational for non-hotspot sources. Hotspot
    # is allowed to re-log (diagnostics) but does not re-advance.
    if current_stage == 'operational':
        if source == 'hotspot':
            try:
                logs_svc.log_network('hotspot_skipped', {
                    'reason': 'hotspot_not_active',
                    'source': source,
                    'from_stage': current_stage,
                    'health_snapshot': hotspot_snapshot or {},
                    'degraded_reasons': (hotspot_snapshot or {}).get('degraded_reasons') or [],
                    'usable':   (hotspot_snapshot or {}).get('usable'),
                    'ap_mode':  (hotspot_snapshot or {}).get('ap_mode'),
                    'stations': (hotspot_snapshot or {}).get('stations'),
                })
            except Exception as e:
                logger.debug('hotspot_skipped re-log failed: %s', e)
            return jsonify({'ok': True, 'state': state, 'noop': True})
        return jsonify({
            'ok': False,
            'error': 'device is already operational',
        }), 409

    # Log BEFORE the advance so the audit line exists even if the
    # advance somehow fails. Both log calls are fire-and-forget: they
    # must not break the escape hatch.
    try:
        if source == 'hotspot':
            logs_svc.log_network('hotspot_skipped', {
                'reason': 'hotspot_not_active',
                'source': source,
                'from_stage': current_stage,
                'health_snapshot': hotspot_snapshot or {},
                # Duplicate the four required fields at the top level so
                # a tail-the-log grep finds them without having to JSON-
                # parse health_snapshot on every row.
                'degraded_reasons': (hotspot_snapshot or {}).get('degraded_reasons') or [],
                'usable':   (hotspot_snapshot or {}).get('usable'),
                'ap_mode':  (hotspot_snapshot or {}).get('ap_mode'),
                'stations': (hotspot_snapshot or {}).get('stations'),
            })
        logs_svc.log_portal('system', 'onboarding_skipped', {
            'source': source,
            'from_stage': current_stage,
        })
    except Exception as e:
        # Not fatal: the UI must still transition. Log to the Python
        # logger so a dev running `journalctl -u skytrack-app` sees it.
        logger.warning('onboarding skip log failed (source=%s): %s', source, e)

    try:
        new_state = onboarding.skip_to_operational(source, config=_cfg())
    except Exception as e:
        logger.exception('onboarding skip advance failed')
        return jsonify({'ok': False, 'error': str(e)}), 500

    return jsonify({
        'ok': True,
        'state': new_state,
        'source': source,
        'hotspot_snapshot': hotspot_snapshot,  # echo for UI debugging
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
