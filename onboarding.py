"""SkyTrack onboarding state machine.

Replaces the old single `configured` boolean with an explicit, persistent
state model that the UI can drive. The previous code only knew "wizard
done / wizard not done" — the new product flow has at least seven
distinct screens before a device is fully operational, and they need to
be addressable individually so the kiosk can resume in the right place
after a power loss mid-onboarding.

State is persisted inside the same `auth.json` record (under the
`onboarding` key) so we don't introduce a second source of truth and
don't have to migrate a new file. The legacy `configured` flag is kept
in lock-step with `stage == 'operational'` so existing call-sites keep
working unchanged.

Stages (ordered)
----------------
    boot_video         Fresh device just powered on; kiosk is showing
                       the boot loop. Transient — the kiosk advances
                       past this on its own.
    setup_video        First boot only; 10-second branded explainer
                       before the PIN screen.
    pin_required       Awaiting first PIN entry.
    pin_confirm        First PIN entered; awaiting confirmation entry.
    setup_offer        PIN saved; show "Set Up Now / Skip For Now".
    setup_now_hotspot  Operator chose Set Up Now; show hotspot details.
    operational        Onboarding complete; kiosk renders /dashboard.

Migration
---------
On first read after upgrade, if `auth.json` already has a `pin_hash` we
infer the device is already operational and stamp `stage='operational'`,
`pin_set=True`, `configured=True`. No data loss; the legacy boolean is
preserved.

Completion flags
----------------
    pin_set                 True once the admin PIN has been written.
    apis_configured         True once all required external API keys
                            (FlightAware/AeroAPI, weather, etc.) are
                            present in `secrets`.
    adsb_configured         True once dump1090 is reachable.
    integrations_complete   True when both of the above are true.

These are recomputed from the live config on every read so they never
go stale.
"""

import logging
from typing import Dict, Optional

import auth as auth_lib

logger = logging.getLogger('skytrack.onboarding')

# Canonical, ordered list of stages. Tests and UI both consume this so the
# enum lives in exactly one place.
#
# Phase 2 stabilization (2.6.1) inserted `welcome` between `setup_video`
# and `pin_required`. The welcome card gates entry to PIN creation — it
# shows branding + local URL and offers "Continue setup" (→ pin_required)
# or "Set up later" (→ operational with pin_set=false). Before 2.6.1 the
# appliance jumped straight from setup_video into the keypad, which
# trapped any operator who wasn't ready to finish setup right now.
STAGES = (
    'boot_video',
    'setup_video',
    'welcome',
    'pin_required',
    'pin_confirm',
    'setup_offer',
    'setup_now_hotspot',
    'operational',
)

_STAGE_INDEX = {name: i for i, name in enumerate(STAGES)}

# Allowed forward and side transitions. The UI cannot jump backwards
# except via the explicit `pin_required` reset (used when the operator
# mistypes the confirmation PIN).
#
# Phase 2 stabilization added three new escape edges so the operator
# can ALWAYS reach the operational dashboard:
#
#   welcome      → operational   ("Set up later")
#   pin_required → operational   ("Skip for now")
#   setup_now_hotspot → operational was already present
#
# None of these clear `pin_set`. The caller (blueprints/onboarding.py
# `api_skip`) uses the fact that `pin_set` is false to log the escape
# and keep the legacy `configured` boolean in lock-step with stage.
_TRANSITIONS = {
    'boot_video':        {'setup_video', 'welcome', 'pin_required'},
    'setup_video':       {'welcome', 'pin_required'},
    'welcome':           {'pin_required', 'operational'},
    'pin_required':      {'pin_confirm', 'operational'},
    'pin_confirm':       {'setup_offer', 'pin_required'},   # re-enter on mismatch
    'setup_offer':       {'setup_now_hotspot', 'operational'},
    'setup_now_hotspot': {'operational'},
    'operational':       set(),  # terminal
}

# Required secret keys for `apis_configured` to be True. These mirror the
# integrations the user wants the appliance to ship with.
REQUIRED_API_SECRETS = ('aeroapi_key', 'openweather_api_key')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_block(rec: dict) -> dict:
    """Return the onboarding sub-dict, creating it from legacy fields if
    it doesn't exist yet. Mutates `rec` in place."""
    block = rec.get('onboarding')
    if isinstance(block, dict) and block.get('stage') in _STAGE_INDEX:
        return block

    # First read after upgrade — derive initial state from the legacy
    # `configured` flag and the presence of `pin_hash`. This is the only
    # place that needs to know about the v2.3.x record shape.
    has_pin = bool(rec.get('pin_hash'))
    legacy_configured = bool(rec.get('configured'))
    if has_pin or legacy_configured:
        stage = 'operational'
        pin_set = True
    else:
        stage = 'boot_video'
        pin_set = False

    block = {
        'stage':                  stage,
        'pin_set':                pin_set,
        'apis_configured':        False,   # recomputed on read
        'adsb_configured':        False,   # recomputed on read
        'integrations_complete':  False,   # recomputed on read
        'history': [],
    }
    rec['onboarding'] = block
    return block


def _recompute_completion(block: dict, config: Optional[dict], secrets: dict) -> None:
    """Refresh the derived completion flags. Mutates `block` in place.

    These flags are *recomputed* on every read so they cannot drift from
    the live config — there is no separate "I marked APIs configured"
    write path that could lie."""
    apis_ok = all(bool((secrets or {}).get(k)) for k in REQUIRED_API_SECRETS)
    block['apis_configured'] = apis_ok

    adsb_ok = False
    if isinstance(config, dict):
        path = (config.get('dump1090_json_path') or '').strip()
        url = (config.get('dump1090_url') or '').strip()
        adsb_ok = bool(path) or bool(url)
    block['adsb_configured'] = adsb_ok

    block['integrations_complete'] = bool(apis_ok and adsb_ok)


# ---------------------------------------------------------------------------
# Public API — read
# ---------------------------------------------------------------------------

def get_state(config: Optional[dict] = None) -> Dict:
    """Return the full onboarding state dict consumed by the kiosk + APIs.

    Shape:

        {
            'stage':                 <one of STAGES>,
            'stages':                [...],          # canonical ordering
            'pin_set':               bool,
            'apis_configured':       bool,
            'adsb_configured':       bool,
            'integrations_complete': bool,
            'configured':            bool,           # legacy alias for stage==operational
            'next_stage':            <stage|None>,   # default forward step
        }
    """
    import json as _json
    rec = auth_lib.read_auth() or {}
    before = _json.dumps(rec.get('onboarding'), sort_keys=True, default=str)
    block = _ensure_block(rec)
    _recompute_completion(block, config, rec.get('secrets') or {})
    after = _json.dumps(rec.get('onboarding'), sort_keys=True, default=str)

    if before != after:
        auth_lib.write_auth(rec)

    stage = block['stage']
    return {
        'stage':                 stage,
        'stages':                list(STAGES),
        'pin_set':               bool(block.get('pin_set')),
        'apis_configured':       bool(block.get('apis_configured')),
        'adsb_configured':       bool(block.get('adsb_configured')),
        'integrations_complete': bool(block.get('integrations_complete')),
        'configured':            stage == 'operational',
        'next_stage':            _default_next(stage),
    }


def _default_next(stage: str) -> Optional[str]:
    """The most common forward transition for a given stage. The UI can
    pick a different allowed transition (e.g. setup_offer → operational
    when the operator hits Skip), but this is the default the
    `/api/onboarding/advance` endpoint will use when no `to` is supplied."""
    if stage == 'pin_confirm':
        return 'setup_offer'
    if stage == 'setup_offer':
        return 'setup_now_hotspot'
    # The welcome card's primary action is "Continue setup", so the
    # default forward step from welcome is pin_required — NOT operational.
    # "Set up later" is an explicit secondary action, never the default.
    if stage == 'welcome':
        return 'pin_required'
    # Same principle at setup_video: prefer the normal onboarding path
    # forward (welcome), not the escape hatch.
    if stage == 'setup_video':
        return 'welcome'
    if stage == 'boot_video':
        return 'setup_video'
    forwards = sorted(
        _TRANSITIONS.get(stage, set()),
        key=lambda s: _STAGE_INDEX.get(s, 99),
    )
    return forwards[0] if forwards else None


# ---------------------------------------------------------------------------
# Public API — write
# ---------------------------------------------------------------------------

def advance(to: Optional[str] = None,
            config: Optional[dict] = None) -> Dict:
    """Transition the persistent stage forward.

    `to` must be one of the allowed transitions for the current stage,
    or omitted to take the default forward step. Raises `ValueError` on
    an invalid transition so the caller can return a 400 to the UI.

    Returns the same shape as `get_state()`.
    """
    rec = auth_lib.read_auth() or {}
    block = _ensure_block(rec)
    current = block['stage']

    if to is None:
        to = _default_next(current)
        if to is None:
            raise ValueError(f'no forward transition from {current}')

    if to not in _STAGE_INDEX:
        raise ValueError(f'unknown stage: {to}')

    allowed = _TRANSITIONS.get(current, set())
    if to not in allowed:
        # Idempotent: already there.
        if to == current:
            _recompute_completion(block, config, rec.get('secrets') or {})
            auth_lib.write_auth(rec)
            return get_state(config)
        raise ValueError(f'cannot transition {current} -> {to}')

    block['stage'] = to
    block.setdefault('history', []).append({'from': current, 'to': to})
    # Trim history so auth.json doesn't grow unbounded across debugging.
    block['history'] = block['history'][-32:]

    # Keep the legacy `configured` boolean in lock-step.
    if to == 'operational':
        rec['configured'] = True

    _recompute_completion(block, config, rec.get('secrets') or {})
    auth_lib.write_auth(rec)
    logger.info('onboarding stage: %s -> %s', current, to)
    return get_state(config)


def set_pin(pin: str, config: Optional[dict] = None) -> Dict:
    """First-time PIN creation entry-point used by the kiosk.

    Differs from the legacy `auth.complete_setup()` in three ways:
      1. It does NOT mark the device as `configured` — that only happens
         when the operator finishes the setup_offer screen.
      2. It advances the onboarding stage to `setup_offer`.
      3. It always (re)generates the hotspot password so the Set Up Now
         screen has something to display.

    Returns the post-write onboarding state. Raises ValueError on an
    invalid PIN.
    """
    pin = (pin or '').strip()
    if not pin or not pin.isdigit() or not (4 <= len(pin) <= 8):
        raise ValueError('Admin PIN must be 4–8 digits')

    rec = auth_lib.read_auth() or {}
    block = _ensure_block(rec)

    # Reuse the canonical hashing helper so we never have two PIN-write
    # paths that could disagree on hash format.
    auth_lib.change_admin_pin(pin)

    # change_admin_pin() did its own write, so re-read.
    rec = auth_lib.read_auth() or {}
    block = _ensure_block(rec)
    block['pin_set'] = True
    if not rec.get('hotspot_password'):
        rec['hotspot_password'] = auth_lib._random_hotspot_password()
    block['stage'] = 'setup_offer'
    block.setdefault('history', []).append({
        'from': 'pin_confirm', 'to': 'setup_offer', 'reason': 'pin_set',
    })
    block['history'] = block['history'][-32:]

    _recompute_completion(block, config, rec.get('secrets') or {})
    auth_lib.write_auth(rec)
    logger.info('onboarding: PIN set, stage -> setup_offer')
    return get_state(config)


def skip_to_operational(source: str, config: Optional[dict] = None) -> Dict:
    """Phase 2 stabilization escape hatch.

    Force-transition to `operational` from any onboarding stage that has
    an explicit skip button (`welcome`, `pin_required`, `setup_offer`,
    `setup_now_hotspot`). Keeps `pin_set` as-is so a skip from welcome
    or pin_required leaves the device operational BUT unauthenticated
    for admin actions, which is the correct state — the operator
    deferred PIN creation and the Settings page will still require one
    before anything admin-gated runs.

    `source` is recorded in onboarding history so `GET /api/onboarding/state`
    can show which button triggered the skip. Callers in
    `blueprints/onboarding.py` are expected to ALSO write a
    `logs_svc.log_portal` or `log_network` line at the same time; this
    helper only mutates auth.json.

    Returns the post-write state dict. Idempotent — if the device is
    already operational we just return the current state without
    touching history.
    """
    rec = auth_lib.read_auth() or {}
    block = _ensure_block(rec)
    current = block['stage']

    if current == 'operational':
        _recompute_completion(block, config, rec.get('secrets') or {})
        auth_lib.write_auth(rec)
        return get_state(config)

    block['stage'] = 'operational'
    block.setdefault('history', []).append({
        'from':   current,
        'to':     'operational',
        'reason': f'skip:{source}',
    })
    block['history'] = block['history'][-32:]

    # Lock-step the legacy flag so callers that still check
    # `rec['configured']` directly see the user's choice.
    rec['configured'] = True

    _recompute_completion(block, config, rec.get('secrets') or {})
    auth_lib.write_auth(rec)
    logger.info('onboarding skip (%s): %s -> operational', source, current)
    return get_state(config)


def reset_to_pin_required() -> Dict:
    """Wipe back to `pin_required`. Used when the operator mistypes the
    confirmation PIN and we want to force them to start over from the
    first PIN entry.

    NOTE: this is the *internal* pin-pad rewind, not the admin-only
    factory reset. It does not touch `pin_hash` or `configured` and
    does NOT wipe history. The admin-only full factory reset is
    handled by `factory_reset()` below and is wired to the HTTP
    endpoint `POST /api/onboarding/reset`."""
    rec = auth_lib.read_auth() or {}
    block = _ensure_block(rec)
    prev = block['stage']
    block['stage'] = 'pin_required'
    block.setdefault('history', []).append({
        'from': prev, 'to': 'pin_required', 'reason': 'reset',
    })
    block['history'] = block['history'][-32:]
    auth_lib.write_auth(rec)
    return get_state()


def factory_reset(config: Optional[dict] = None) -> Dict:
    """Admin-gated logical reset of the onboarding state machine.

    Returns the device to the earliest valid stage before PIN entry
    (`welcome`) so the operator can re-run the full onboarding flow
    end-to-end. Exposed via `POST /api/onboarding/reset` in
    `blueprints/onboarding.py` (admin auth required).

    This is a LOGICAL reset only. It mutates `auth.json` and nothing
    else — per the Phase 2 stabilization spec, OS-level state is
    explicitly preserved:

      - hostapd / dnsmasq config           — untouched
      - Cloudflare tunnel config           — untouched
      - systemd units                      — not restarted
      - sqlite tables (sightings, logs)    — untouched
      - device_id + firstboot marker       — untouched
      - config.yaml                        — untouched
      - integration secrets (aeroapi_key)  — preserved so the device
        still works for flight tracking after the setup flow is re-run

    Fields cleared in auth.json:

      - `pin_hash`          the admin PIN (so the operator is forced
                            through the welcome → pin flow again)
      - `admin_hash`        legacy v2.3.x field, paranoid cleanup
      - `configured`        legacy flag back to False
      - `onboarding` block  stage reset to 'welcome', history wiped,
                            pin_set back to False

    The stored `hotspot_password` is ALSO preserved so the reveal
    card still has credentials to display when the operator reaches
    the hotspot stage again. Regenerating it would require restarting
    hostapd, which the spec forbids.

    Returns the post-reset state dict (same shape as `get_state()`).
    """
    rec = auth_lib.read_auth() or {}

    # The admin PIN is the only auth material we wipe — everything
    # else in the record (hotspot_password, secrets, etc.) is kept.
    rec.pop('pin_hash', None)
    rec.pop('admin_hash', None)  # legacy v2.3.x field, paranoid cleanup

    # Legacy flag back to False so any call-site that still checks
    # `rec['configured']` directly sees the reset.
    rec['configured'] = False

    # Rebuild the onboarding block from scratch so `history` is wiped
    # and `stage` is the earliest valid stage before PIN.
    rec['onboarding'] = {
        'stage':                 'welcome',
        'pin_set':               False,
        'apis_configured':       False,   # recomputed on read
        'adsb_configured':       False,   # recomputed on read
        'integrations_complete': False,   # recomputed on read
        'history': [{
            'from':   None,
            'to':     'welcome',
            'reason': 'factory_reset',
        }],
    }

    auth_lib.write_auth(rec)
    logger.warning('onboarding: FACTORY RESET — PIN cleared, stage -> welcome')
    return get_state(config)
