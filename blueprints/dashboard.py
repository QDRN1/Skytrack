"""Dashboard blueprint — the main authenticated landing page.

UI shows 4 cards, recent activity, top airlines/routes, trend chart,
search box, weather card, and the live aircraft map. All data is
served via REST endpoints under /api/dashboard/*; the page itself is
mostly static and hydrates with JavaScript.
"""

import logging

from flask import Blueprint, current_app, jsonify, render_template, request

import auth as auth_lib
import dashboard_svc
import device_id
import enrich

logger = logging.getLogger('skytrack.dashboard')

dashboard_bp = Blueprint('dashboard', __name__)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@dashboard_bp.route('/dashboard')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def index():
    cfg = current_app.skytrack_config
    return render_template(
        'dashboard.html',
        center_lat=cfg.get('latitude'),
        center_lon=cfg.get('longitude'),
        map_zoom=cfg.get('map_zoom', 8),
    )


# ---------------------------------------------------------------------------
# Card APIs
# ---------------------------------------------------------------------------

@dashboard_bp.route('/api/dashboard/cards')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_cards():
    return jsonify({
        'now':     dashboard_svc.card_aircraft_now(),
        'today':   dashboard_svc.card_aircraft_today(),
        'busiest': dashboard_svc.card_busiest_hour(),
        'last':    dashboard_svc.card_last_aircraft(),
    })


@dashboard_bp.route('/api/dashboard/recent')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_recent():
    limit = int(request.args.get('limit', 25))
    return jsonify(dashboard_svc.recent_aircraft(limit=limit))


@dashboard_bp.route('/api/dashboard/airlines')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_airlines():
    range_key = request.args.get('range', '24h')
    return jsonify(dashboard_svc.top_airlines(range_key))


@dashboard_bp.route('/api/dashboard/routes')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_routes():
    range_key = request.args.get('range', '24h')
    return jsonify(dashboard_svc.top_routes(range_key))


@dashboard_bp.route('/api/dashboard/trend')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_trend():
    range_key = request.args.get('range', '24h')
    return jsonify(dashboard_svc.trend(range_key))


@dashboard_bp.route('/api/dashboard/positions')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_positions():
    return jsonify(dashboard_svc.aircraft_now_positions())


@dashboard_bp.route('/api/dashboard/search')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_search():
    q = request.args.get('q', '').strip()
    range_key = request.args.get('range', '24h')
    if not q:
        return jsonify([])
    return jsonify(dashboard_svc.search(q, range_key=range_key))


@dashboard_bp.route('/api/dashboard/weather')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_weather():
    return jsonify(current_app.weather_svc.get_weather())


@dashboard_bp.route('/api/dashboard/sensor')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_sensor():
    reading = current_app.sensor_svc.read()
    eval_result = current_app.buzzer.evaluate(reading)
    return jsonify({'reading': reading, 'buzzer': eval_result})


@dashboard_bp.route('/api/dashboard/identity')
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_identity():
    return jsonify({
        'device': current_app.identity,
        'radar_url': device_id.radar_url(current_app.identity),
    })


# ---------------------------------------------------------------------------
# Per-aircraft enrichment (on demand only — see enrich.py for budget rules)
# ---------------------------------------------------------------------------

@dashboard_bp.route('/api/dashboard/enrich/<icao>', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_PIN)
def api_enrich(icao):
    callsign = (request.args.get('callsign') or '').strip() or None
    record = enrich.enrich_flight(icao, callsign, current_app.skytrack_config)
    if not record:
        return jsonify({'ok': False, 'cached': False, 'enriched': None}), 200
    return jsonify({'ok': True, 'enriched': record})
