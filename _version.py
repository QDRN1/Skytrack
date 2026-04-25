"""Single source of truth for the SkyTrack Portal version string.

To bump the version, edit __version_base__ below. The runtime version
auto-appends the short git commit hash (e.g. "2.8.0-a3f7b21") so every
deploy is distinguishable without a manual file edit.

Semver: MAJOR.MINOR.PATCH.
  MAJOR — incompatible db/auth/config rewrite
  MINOR — new features, refactors, installer or kiosk changes
  PATCH — bug fixes only
"""

import os
import subprocess

__version_base__ = "2.8.0"

try:
    _hash = subprocess.check_output(
        ['git', 'rev-parse', '--short', 'HEAD'],
        stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(os.path.abspath(__file__)) or '.',
    ).decode().strip()
    __version__ = f"{__version_base__}-{_hash}"
except Exception:
    __version__ = __version_base__
