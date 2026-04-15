"""Single source of truth for the SkyTrack Portal version string.

To bump the version, edit ONLY this file. Every other place (app.py,
blueprints/settings.py, splash/index.html bootstrap, etc.) imports from
here so there is never a second literal to forget.

Semver: MAJOR.MINOR.PATCH.
  MAJOR — incompatible db/auth/config rewrite
  MINOR — new features, refactors, installer or kiosk changes
  PATCH — bug fixes only
"""

__version__ = "2.5.3"
