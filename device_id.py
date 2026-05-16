"""Device identity — QDRN-SkyTrack-XXXXX.

Deterministically derived from the Raspberry Pi CPU serial (or MAC fallback)
so that the same physical device always produces the same ID, even after
an SD card reflash. See the collision analysis in the project plan file.
"""

import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger('skytrack.device_id')

DEFAULT_ID_PATH = Path('/var/lib/skytrack/device_id')

# 31-char alphabet — no ambiguous 0/O/1/I/L
_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
_SALT = b'QDRN-SkyTrack-v2-device-id'
_PREFIX = 'QDRN-SkyTrack-'
_ALGO = 'hmac-sha256-v1'


def _id_path():
    return Path(os.environ.get('SKYTRACK_DEVICE_ID_PATH', str(DEFAULT_ID_PATH)))


def _read_hardware_serial() -> str:
    """Return a stable hardware identifier, preferring the Pi CPU serial."""
    # Primary: Raspberry Pi CPU serial (globally unique 64-bit)
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.startswith('Serial'):
                    serial = line.split(':', 1)[1].strip()
                    if serial and set(serial) != {'0'}:
                        return f'pi-cpu:{serial}'
    except OSError:
        pass

    # Fallback 1: eth0 MAC (permanent on a Pi)
    try:
        with open('/sys/class/net/eth0/address') as f:
            mac = f.read().strip()
            if mac and mac != '00:00:00:00:00:00':
                return f'eth0-mac:{mac}'
    except OSError:
        pass

    # Fallback 2: whatever MAC Python can find
    node = uuid.getnode()
    return f'uuid-node:{node:012x}'


def _derive_id(hw_source: str, collision_counter: int = 0) -> str:
    """HMAC(salt, hw|counter) → 5 chars from the 31-char alphabet."""
    msg = hw_source.encode()
    if collision_counter:
        msg += f'|collision-{collision_counter}'.encode()

    digest = hmac.HMAC(_SALT, msg, hashlib.sha256).digest()
    # 5 chars × log2(31) ≈ 24.8 bits — 4 bytes of digest is plenty
    val = int.from_bytes(digest[:4], 'big')
    chars = []
    for _ in range(5):
        chars.append(_ALPHABET[val % len(_ALPHABET)])
        val //= len(_ALPHABET)
    return _PREFIX + ''.join(reversed(chars))


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def get_or_create_device_id() -> dict:
    """Idempotent. Returns the full record dict from disk or creates a new one.

    Record shape:
        {
          "device_id": "QDRN-SkyTrack-ABCDE",
          "short_id":  "ABCDE",
          "hw_source": "pi-cpu:1000000012345678",
          "created_at": "2026-04-14T12:00:00+00:00",
          "algo":       "hmac-sha256-v1",
          "collision_counter": 0
        }
    """
    path = _id_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning('Unreadable device_id file at %s (%s); regenerating', path, e)

    hw = _read_hardware_serial()
    device_id = _derive_id(hw, collision_counter=0)
    record = {
        'device_id': device_id,
        'short_id': device_id[len(_PREFIX):],
        'hw_source': hw,
        'created_at': _now_iso(),
        'algo': _ALGO,
        'collision_counter': 0,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(record, indent=2))
    tmp.replace(path)
    try:
        path.chmod(0o444)
    except OSError:
        # Non-root filesystems may reject chmod — non-fatal
        pass

    logger.info('Created device ID %s from %s', device_id, hw)
    return record


def regenerate_device_id(reason: str = 'super-user-forced') -> dict:
    """Bump the collision counter and re-derive. Super-User only path."""
    path = _id_path()
    old = {}
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except json.JSONDecodeError:
            old = {}

    hw = old.get('hw_source') or _read_hardware_serial()
    counter = int(old.get('collision_counter', 0)) + 1
    device_id = _derive_id(hw, collision_counter=counter)

    record = {
        'device_id': device_id,
        'short_id': device_id[len(_PREFIX):],
        'hw_source': hw,
        'created_at': _now_iso(),
        'algo': _ALGO,
        'collision_counter': counter,
        'regenerated_reason': reason,
        'previous_device_id': old.get('device_id'),
    }

    # Temporarily make writable
    try:
        path.chmod(0o644)
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(record, indent=2))
    tmp.replace(path)
    try:
        path.chmod(0o444)
    except OSError:
        pass

    logger.warning('Regenerated device ID: %s → %s (reason=%s)',
                   old.get('device_id'), device_id, reason)
    return record


def radar_url(record: dict) -> str:
    """Return the display-only remote radar URL for this device."""
    return f'https://radar.qdrn.io/{record["short_id"]}'
