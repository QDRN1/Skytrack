"""Dashboard blueprint — the public landing page.

The dashboard is intentionally **public**. The on-device kiosk display
points at /dashboard and must never be blocked by a login screen.
Anyone who connects to the SkyTrack-Portal Wi-Fi can browse the live
aircraft, weather, sensor, and identity panels without authenticating.

Read-only data lives under /api/dashboard/* and is also public. The
only endpoint that requires admin is `api_enrich`, because it spends
AeroAPI / OpenSky budget when called.
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
# Page (public)
# ---------------------------------------------------------------------------

@dashboard_bp.route('/dashboard')
def index():
    cfg = current_app.skytrack_config
    return render_template(
        'dashboard.html',
        center_lat=cfg.get('latitude'),
        center_lon=cfg.get('longitude'),
        map_zoom=cfg.get('map_zoom', 8),
    )


# ---------------------------------------------------------------------------
# Card APIs (public read-only)
# ---------------------------------------------------------------------------

@dashboard_bp.route('/api/dashboard/cards')
def api_cards():
    return jsonify({
        'now':     dashboard_svc.card_aircraft_now(),
        'today':   dashboard_svc.card_aircraft_today(),
        'busiest': dashboard_svc.card_busiest_hour(),
        'last':    dashboard_svc.card_last_aircraft(),
    })


@dashboard_bp.route('/api/dashboard/recent')
def api_recent():
    limit = int(request.args.get('limit', 25))
    return jsonify(dashboard_svc.recent_aircraft(limit=limit))


@dashboard_bp.route('/api/dashboard/airlines')
def api_airlines():
    range_key = request.args.get('range', '24h')
    return jsonify(dashboard_svc.top_airlines(range_key))


@dashboard_bp.route('/api/dashboard/routes')
def api_routes():
    range_key = request.args.get('range', '24h')
    return jsonify(dashboard_svc.top_routes(range_key))


@dashboard_bp.route('/api/dashboard/trend')
def api_trend():
    range_key = request.args.get('range', '24h')
    return jsonify(dashboard_svc.trend(range_key))


@dashboard_bp.route('/api/dashboard/positions')
def api_positions():
    return jsonify(dashboard_svc.aircraft_now_positions())


@dashboard_bp.route('/api/dashboard/search')
def api_search():
    q = request.args.get('q', '').strip()
    range_key = request.args.get('range', '24h')
    if not q:
        return jsonify([])
    return jsonify(dashboard_svc.search(q, range_key=range_key))


@dashboard_bp.route('/api/dashboard/weather')
def api_weather():
    return jsonify(current_app.weather_svc.get_weather())


@dashboard_bp.route('/api/dashboard/sensor')
def api_sensor():
    reading = current_app.sensor_svc.read()
    eval_result = current_app.buzzer.evaluate(reading)
    return jsonify({'reading': reading, 'buzzer': eval_result})


@dashboard_bp.route('/api/dashboard/identity')
def api_identity():
    return jsonify({
        'device': current_app.identity,
        'radar_url': device_id.radar_url(current_app.identity),
    })


# ---------------------------------------------------------------------------
# Per-aircraft enrichment (admin only — spends AeroAPI / OpenSky budget)
# ---------------------------------------------------------------------------

@dashboard_bp.route('/api/dashboard/enrich/<icao>', methods=['POST'])
@auth_lib.login_required(auth_lib.ROLE_ADMIN)
def api_enrich(icao):
    callsign = (request.args.get('callsign') or '').strip() or None
    record = enrich.enrich_flight(icao, callsign, current_app.skytrack_config)
    if not record:
        return jsonify({'ok': False, 'cached': False, 'enriched': None}), 200
    return jsonify({'ok': True, 'enriched': record})
