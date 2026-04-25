"""OTA git auth abstraction.

The installer currently provisions this device with a deploy key. The
product direction is to move away from deploy keys, so the code must not
hard-wire that assumption. Every OTA git invocation flows through
`git_command()` below, which dispatches on `ota_auth_mode`:

  * 'ssh'         — git uses ssh, picking up whichever identity the
                    operator has configured (deploy key OR user key OR
                    agent). This is today's production mode.
  * 'https_none'  — plain HTTPS, no credentials. For a public mirror.
  * 'https_token' — HTTPS with a token read from auth.json (`ota_git_token`).
                    The token is injected into the clone URL at command
                    time so it never lands in the workspace's `.git/config`.

Swapping auth models is a one-line config change (`ota_auth_mode`) plus
dropping the new secret into auth.json. No Python needs to be rewritten.

This module deliberately knows nothing about Flask, requests, or the
wider app — it's a pure helper so `blueprints/settings.py` can import it
without creating a circular dependency.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Tuple
from urllib.parse import urlparse, urlunparse, quote

logger = logging.getLogger('skytrack.git_auth')


def resolve_remote_url(config: dict, mode: str | None = None) -> str:
    """Return the git remote URL to hand to git, with credentials if needed.

    For 'ssh' and 'https_none' the URL is passed through unchanged. For
    'https_token' we read `ota_git_token` from auth.json (via the auth
    module) and splice it into the URL as `https://<token>@host/...`. The
    token is NOT stored in git config — it's only used for the duration of
    one process invocation.
    """
    base = (config.get('ota_repo_url') or '').strip()
    if not base:
        return ''

    mode = (mode or config.get('ota_auth_mode') or 'ssh').lower()

    if mode in ('ssh', 'https_none'):
        return base

    if mode == 'https_token':
        try:
            import auth as auth_lib
            token = (auth_lib.get_secret('ota_git_token') or '').strip()
        except Exception:
            token = ''
        if not token:
            logger.warning('ota_auth_mode=https_token but no ota_git_token secret set')
            return base
        parsed = urlparse(base)
        if parsed.scheme not in ('http', 'https'):
            logger.warning('ota_auth_mode=https_token only makes sense for https remotes; '
                           'got %s', base)
            return base
        netloc = f'{quote(token, safe="")}@{parsed.hostname}'
        if parsed.port:
            netloc += f':{parsed.port}'
        return urlunparse(parsed._replace(netloc=netloc))

    logger.warning('Unknown ota_auth_mode %r — falling back to raw URL', mode)
    return base


def build_env(config: dict, home_dir: str) -> Dict[str, str]:
    """Return an environment dict for shelling out to git.

    Always non-interactive: any prompt is a failure. HOME is redirected
    to a writable directory we control so ssh can manage known_hosts.
    """
    ssh_dir = os.path.join(home_dir, '.ssh')
    try:
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    except Exception:
        pass
    known_hosts = os.path.join(ssh_dir, 'known_hosts')
    if not os.path.exists(known_hosts):
        try:
            with open(known_hosts, 'a'):
                pass
            os.chmod(known_hosts, 0o600)
        except Exception:
            pass

    env = os.environ.copy()
    env.update({
        'HOME': home_dir,
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_ASKPASS': '/bin/true',
    })

    mode = (config.get('ota_auth_mode') or 'ssh').lower()
    if mode == 'ssh':
        env['GIT_SSH_COMMAND'] = (
            'ssh -o BatchMode=yes '
            '-o StrictHostKeyChecking=accept-new '
            f'-o UserKnownHostsFile={known_hosts} '
            '-o ConnectTimeout=10'
        )
    # For https_none and https_token we do NOT set GIT_SSH_COMMAND — we
    # want git to go straight to HTTPS without trying SSH at all.

    # Strip anything that could reintroduce interactive prompting
    for k in ('SSH_ASKPASS', 'DISPLAY'):
        env.pop(k, None)
    if mode != 'ssh':
        env.pop('SSH_AUTH_SOCK', None)

    return env


def redact_url(url: str) -> str:
    """Return a display-safe version of a URL (token stripped)."""
    if not url:
        return ''
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            host = parsed.hostname or ''
            if parsed.port:
                host += f':{parsed.port}'
            return urlunparse(parsed._replace(netloc=host))
    except Exception:
        pass
    return url


def describe(config: dict) -> Dict:
    """Return a human-readable summary of the current OTA auth configuration."""
    mode = (config.get('ota_auth_mode') or 'ssh').lower()
    url = (config.get('ota_repo_url') or '').strip()
    return {
        'mode': mode,
        'mode_label': {
            'ssh': 'SSH (deploy key or user key)',
            'https_none': 'HTTPS (public, no credentials)',
            'https_token': 'HTTPS with personal access token',
        }.get(mode, mode),
        'repo': redact_url(url),
        'remote': config.get('ota_remote') or 'origin',
        'branch': config.get('ota_branch') or 'claude/skytrack-adsb-tracker-N8p6u',
    }
