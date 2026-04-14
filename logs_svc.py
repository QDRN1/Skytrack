"""Thin helpers around the portal_logs and network_logs tables."""

import json
import logging

from db import get_conn

logger = logging.getLogger('skytrack.logs')


def log_portal(actor: str, action: str, detail=None) -> None:
    """Record an audit event. Detail can be a dict or string."""
    if isinstance(detail, (dict, list)):
        detail_str = json.dumps(detail, default=str)
    else:
        detail_str = detail
    try:
        conn = get_conn()
        conn.execute(
            'INSERT INTO portal_logs (actor, action, detail) VALUES (?, ?, ?)',
            (actor, action, detail_str),
        )
        conn.commit()
    except Exception as e:
        logger.debug('log_portal failed: %s', e)


def log_network(event: str, detail=None) -> None:
    if isinstance(detail, (dict, list)):
        detail_str = json.dumps(detail, default=str)
    else:
        detail_str = detail
    try:
        conn = get_conn()
        conn.execute(
            'INSERT INTO network_logs (event, detail) VALUES (?, ?)',
            (event, detail_str),
        )
        conn.commit()
    except Exception as e:
        logger.debug('log_network failed: %s', e)


def tail_portal(limit: int = 200):
    conn = get_conn()
    rows = conn.execute(
        'SELECT ts, actor, action, detail FROM portal_logs ORDER BY id DESC LIMIT ?',
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def tail_network(limit: int = 200):
    conn = get_conn()
    rows = conn.execute(
        'SELECT ts, event, detail FROM network_logs ORDER BY id DESC LIMIT ?',
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
