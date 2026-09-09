"""Adaptive Card endpoints.

Serves the SkyTrack Situation Room card payload so an outbound
integration (Teams incoming webhook, Power Automate, a bot, or a
scheduled push) can fetch a ready-to-post Adaptive Card instead of
re-implementing the layout on the other side.

Admin-gated: the payload carries device internals (uptime, disk,
link state, recent contacts) that the public dashboard does not show.
"""

import logging

from flask import Blueprint, jsonify, request

import auth as auth_lib
from integrations.adaptive_cards import build_card, collect_snapshot, demo_snapshot

logger = logging.getLogger('skytrack.cards_bp')

cards_bp = Blueprint('cards', __name__)


def _base_url() -> str:
    return request.url_root.rstrip('/')


@cards_bp.route('/api/cards/situation-room')
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def situation_room():
    """Live card payload. ?wrap=teams returns a Teams webhook envelope."""
    try:
        snap = collect_snapshot(base_url=_base_url())
    except Exception as exc:
        logger.exception('snapshot failed, serving demo data: %s', exc)
        snap = demo_snapshot()

    card = build_card(snap)
    if request.args.get('wrap') == 'teams':
        return jsonify({
            'type': 'message',
            'attachments': [{
                'contentType': 'application/vnd.microsoft.card.adaptive',
                'contentUrl': None,
                'content': card,
            }],
        })
    return jsonify(card)


@cards_bp.route('/api/cards/situation-room/demo')
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def situation_room_demo():
    """Deterministic sample payload — for renderer testing and design work."""
    card = build_card(demo_snapshot())
    if request.args.get('wrap') == 'teams':
        return jsonify({
            'type': 'message',
            'attachments': [{
                'contentType': 'application/vnd.microsoft.card.adaptive',
                'contentUrl': None,
                'content': card,
            }],
        })
    return jsonify(card)
