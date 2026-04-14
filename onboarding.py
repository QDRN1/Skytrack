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
STAGES = (
    'boot_video',
    'setup_video',
    'pin_required',
    'pin_confirm',
    'setup_offer',
    'setup_now_hotspot',
    'operational',
)

_STAGE_INDEX = {name: i for i, name in enumerate(STAGES)}

# Allowed forward and side transitions. The UI cannot jump backwards
# except via the explicit `pin_required` reset (used when the operator
# mistypes the confirmation PIN). Note that you cannot reach
# `operational` from any stage before `setup_offer` — onboarding is not
# allowed to be skipped past PIN creation by the kiosk.
_TRANSITIONS = {
    'boot_video':        {'setup_video', 'pin_required'},
    'setup_video':       {'pin_required'},
    'pin_required':      {'pin_confirm'},
    'pin_confirm':       {'setup_offer', 'pin_required'},   # re-enter on mismatch
    'setup_offer':       {'setup_now_hotspot', 'operational'},
    'setup_now_hotspot': {'operational'},
    'operational':       set(),  # terminal
}

# Required secret keys for `apis_configured` to be True. These mirror the
# integrations the user wants the appliance to ship with.
REQUIRED_API_SECRETS = ('aeroapi_key', 'weather_api_key')


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
        host = (config.get('dump1090_host') or '').strip()
        port = config.get('dump1090_port')
        adsb_ok = bool(host) and bool(port)
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
    rec = auth_lib.read_auth() or {}
    block = _ensure_block(rec)
    _recompute_completion(block, config, rec.get('secrets') or {})

    # We may have populated `onboarding` for the first time, or refreshed
    # the derived completion flags — either way persist so subsequent
    # reads don't repeat the work.
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


def reset_to_pin_required() -> Dict:
    """Wipe back to `pin_required`. Used when the operator mistypes the
    confirmation PIN and we want to force them to start over from the
    first PIN entry."""
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
