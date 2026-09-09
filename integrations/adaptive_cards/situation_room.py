"""SkyTrack Situation Room — a maximal Adaptive Card payload.

This is deliberately the *ceiling* of what an Adaptive Card can do in
Microsoft Teams: full-width canvas, native charts, Fluent icons,
bordered/rounded tiles, a scrollable table, a collapsible drill-down,
a nested ShowCard form, compound buttons, an overflow menu, and an
auto-refresh hook.

Everything Teams-only carries a ``fallback`` so the same payload still
renders (degraded, not broken) in Outlook Actionable Messages, Bot
Framework WebChat, or the reference adaptivecards.io renderer.

Target schema version is 1.5 — that is the version Teams' chart and
icon extensions are documented against.

Two entry points:

    collect_snapshot(app)   pull live numbers off a running SkyTrack app
    demo_snapshot()         deterministic fake data, works anywhere

Both hand the same dict shape to ``build_card``.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger('skytrack.adaptive_card')

SCHEMA = 'http://adaptivecards.io/schemas/adaptive-card.json'
VERSION = '1.5'

# Teams chart palette tokens, in the order we want series drawn.
_SERIES_COLORS = [
    'categoricalBlue', 'categoricalTeal', 'categoricalPurple',
    'categoricalMarigold', 'categoricalLime', 'categoricalLavender',
    'categoricalRed', 'categoricalGreen',
]

_LEVEL_TO_STYLE = {'good': 'good', 'warning': 'warning', 'attention': 'attention'}
_LEVEL_TO_ICON = {'good': 'CheckmarkCircle', 'warning': 'Warning', 'attention': 'ErrorCircle'}


# ---------------------------------------------------------------------------
# small builder helpers
# ---------------------------------------------------------------------------

def _text(txt, **kw) -> Dict[str, Any]:
    el = {'type': 'TextBlock', 'text': txt, 'wrap': True}
    el.update(kw)
    return el


def _icon(name, size='Small', color='Accent', style='Filled', **kw) -> Dict[str, Any]:
    """Fluent icon with a text fallback for hosts that lack the Icon element."""
    el = {
        'type': 'Icon', 'name': name, 'size': size, 'color': color, 'style': style,
        'fallback': _text('', spacing='None'),
    }
    el.update(kw)
    return el


def _chart(el: Dict[str, Any], fallback_text: str) -> Dict[str, Any]:
    """Attach a readable text fallback to a Teams-only Chart.* element."""
    el['fallback'] = _text(fallback_text, wrap=True, isSubtle=True, size='Small')
    return el


def _kpi_tile(kpi: Dict[str, Any]) -> Dict[str, Any]:
    """One bordered, rounded KPI column with icon, big number and delta."""
    delta = kpi.get('delta')
    delta_dir = kpi.get('delta_dir', 'flat')
    delta_color = {'up': 'Good', 'down': 'Attention', 'flat': 'Default'}[delta_dir]
    delta_icon = {'up': 'ArrowTrending', 'down': 'ArrowTrendingDown',
                  'flat': 'Subtract'}[delta_dir]

    footer: List[Dict[str, Any]] = []
    if delta is not None:
        footer.append({
            'type': 'ColumnSet',
            'spacing': 'Small',
            'columns': [
                {'type': 'Column', 'width': 'auto', 'verticalContentAlignment': 'Center',
                 'items': [_icon(delta_icon, size='xxSmall', color=delta_color)]},
                {'type': 'Column', 'width': 'stretch', 'verticalContentAlignment': 'Center',
                 'items': [_text(delta, size='Small', color=delta_color,
                                 weight='Bolder', spacing='Small')]},
            ],
        })

    return {
        'type': 'Column',
        'width': 'stretch',
        'minHeight': '108px',
        'style': kpi.get('style', 'emphasis'),
        'showBorder': True,
        'roundedCorners': True,
        'spacing': 'Small',
        'verticalContentAlignment': 'Top',
        'selectAction': {
            'type': 'Action.ToggleVisibility',
            'title': kpi['label'],
            'targetElements': ['detailPanel'],
        },
        'items': [
            {
                'type': 'ColumnSet',
                'columns': [
                    {'type': 'Column', 'width': 'auto', 'verticalContentAlignment': 'Center',
                     'items': [_icon(kpi.get('icon', 'Circle'), size='Small',
                                     color=kpi.get('icon_color', 'Accent'))]},
                    {'type': 'Column', 'width': 'stretch', 'verticalContentAlignment': 'Center',
                     'items': [_text(kpi['label'].upper(), size='Small', isSubtle=True,
                                     weight='Bolder', spacing='Small')]},
                ],
            },
            {
                'type': 'RichTextBlock',
                'spacing': 'Small',
                'inlines': [
                    {'type': 'TextRun', 'text': str(kpi['value']),
                     'size': 'ExtraLarge', 'weight': 'Bolder',
                     'color': kpi.get('value_color', 'Default')},
                    {'type': 'TextRun', 'text': ' ' + kpi.get('unit', ''),
                     'size': 'Small', 'isSubtle': True},
                ],
            },
        ] + footer,
    }


def _table_cell(items, **kw) -> Dict[str, Any]:
    cell = {'type': 'TableCell', 'items': items}
    cell.update(kw)
    return cell


# ---------------------------------------------------------------------------
# the card
# ---------------------------------------------------------------------------

def build_card(snap: Dict[str, Any]) -> Dict[str, Any]:
    """Render the Situation Room Adaptive Card from a snapshot dict."""
    device = snap.get('device', {})
    status = snap.get('status', {})
    level = status.get('level', 'good')
    base_url = device.get('base_url', '')

    body: List[Dict[str, Any]] = []

    # --- hero band -------------------------------------------------------
    # Teams container styles are all *light* tints — there is no dark
    # "accent" band. A dark hero with Light text is only correct when a
    # backgroundImage is actually supplied; otherwise fall back to the
    # emphasis tint with normal text colors so it stays readable.
    hero_img = device.get('hero_image')
    hero_text = 'Light' if hero_img else 'Default'
    hero_sub = 'Light' if hero_img else 'Accent'

    hero_items: List[Dict[str, Any]] = [{
        'type': 'ColumnSet',
        'columns': [
            {
                'type': 'Column', 'width': 'stretch', 'verticalContentAlignment': 'Center',
                'items': [
                    {
                        'type': 'ColumnSet',
                        'columns': [
                            {'type': 'Column', 'width': 'auto',
                             'verticalContentAlignment': 'Center',
                             'items': [_icon('Airplane', size='Large',
                                             color=hero_sub)]},
                            {'type': 'Column', 'width': 'stretch', 'items': [
                                _text('SkyTrack Situation Room', size='ExtraLarge',
                                      weight='Bolder', color=hero_text, spacing='Small',
                                      wrap=True),
                                _text('{} · {}'.format(device.get('name', 'SkyTrack'),
                                                       device.get('site', 'unknown site')),
                                      size='Small', color=hero_text, isSubtle=True,
                                      spacing='None', wrap=True),
                            ]},
                        ],
                    },
                ],
            },
            {
                'type': 'Column', 'width': 'auto', 'verticalContentAlignment': 'Center',
                'targetWidth': 'atLeast:Narrow',
                'items': [
                    {
                        'type': 'Badge',
                        'text': device.get('short_id', '—'),
                        'style': 'Accent',
                        'appearance': 'Filled',
                        'shape': 'Rounded',
                        'size': 'Medium',
                        'icon': 'Fingerprint',
                        'horizontalAlignment': 'Right',
                        'fallback': _text('**{}**'.format(device.get('short_id', '—')),
                                          color=hero_text, horizontalAlignment='Right'),
                    },
                    _text('v{}'.format(device.get('version', '?')), size='Small',
                          color=hero_text, isSubtle=True, horizontalAlignment='Right',
                          spacing='Small'),
                ],
            },
        ],
    }]

    hero: Dict[str, Any] = {
        'type': 'Container',
        'bleed': True,
        'style': 'default' if hero_img else 'emphasis',
        'showBorder': not hero_img,
        'roundedCorners': True,
        'minHeight': '92px',
        'verticalContentAlignment': 'Center',
        'items': hero_items,
    }
    if hero_img:
        hero['backgroundImage'] = {
            'url': hero_img,
            'fillMode': 'Cover',
            'horizontalAlignment': 'Center',
            'verticalAlignment': 'Center',
        }
    body.append(hero)

    # --- status strip ----------------------------------------------------
    body.append({
        'type': 'Container',
        'style': _LEVEL_TO_STYLE.get(level, 'good'),
        'roundedCorners': True,
        'showBorder': True,
        'spacing': 'Medium',
        'speak': status.get('headline', ''),
        'items': [{
            'type': 'ColumnSet',
            'columns': [
                {'type': 'Column', 'width': 'auto', 'verticalContentAlignment': 'Center',
                 'items': [_icon(_LEVEL_TO_ICON.get(level, 'Info'), size='Small',
                                 color={'good': 'Good', 'warning': 'Warning',
                                        'attention': 'Attention'}.get(level, 'Accent'))]},
                {'type': 'Column', 'width': 'stretch', 'verticalContentAlignment': 'Center',
                 'items': [_text(status.get('headline', 'Nominal'), weight='Bolder',
                                 spacing='Small', wrap=True)]},
                {'type': 'Column', 'width': 'auto', 'verticalContentAlignment': 'Center',
                 'targetWidth': 'atLeast:Standard',
                 'items': [_text(status.get('since', ''), size='Small', isSubtle=True,
                                 horizontalAlignment='Right')]},
            ],
        }],
    })

    # --- KPI row ---------------------------------------------------------
    kpis = snap.get('kpis', [])
    if kpis:
        body.append({
            'type': 'ColumnSet',
            'spacing': 'Medium',
            'columns': [_kpi_tile(k) for k in kpis],
        })

    # --- charts row: 24h traffic + airline mix --------------------------
    trend = snap.get('trend', [])
    airlines = snap.get('airlines', [])
    chart_cols: List[Dict[str, Any]] = []

    if trend:
        chart_cols.append({
            'type': 'Column', 'width': 60, 'style': 'default',
            'showBorder': True, 'roundedCorners': True,
            'items': [_chart({
                'type': 'Chart.Line',
                'title': 'Contacts per hour · 24h',
                'xAxisTitle': 'Hour (UTC)',
                'yAxisTitle': 'Unique aircraft',
                'colorSet': 'categorical',
                'data': [{
                    'legend': 'Unique aircraft',
                    'values': [{'x': p['x'], 'y': p['y']} for p in trend],
                }],
            }, 'Contacts per hour (24h): ' + ', '.join(
                '{}={}'.format(p['x'][-5:], p['y']) for p in trend[-8:]))],
        })

    if airlines:
        chart_cols.append({
            'type': 'Column', 'width': 40, 'style': 'default',
            'showBorder': True, 'roundedCorners': True, 'spacing': 'Small',
            'targetWidth': 'atLeast:Narrow',
            'items': [_chart({
                'type': 'Chart.Donut',
                'title': 'Operator mix · 24h',
                'colorSet': 'categorical',
                'data': [
                    {'legend': a['name'], 'value': a['count'],
                     'color': _SERIES_COLORS[i % len(_SERIES_COLORS)]}
                    for i, a in enumerate(airlines)
                ],
            }, 'Operator mix: ' + ', '.join(
                '{} {}'.format(a['name'], a['count']) for a in airlines))],
        })

    if chart_cols:
        body.append({'type': 'ColumnSet', 'spacing': 'Medium', 'columns': chart_cols})

    # --- gauges row ------------------------------------------------------
    gauges = snap.get('gauges', {})
    gauge_cols: List[Dict[str, Any]] = []

    if gauges.get('disk_pct') is not None:
        used = round(gauges['disk_pct'])
        gauge_cols.append({
            'type': 'Column', 'width': 'stretch', 'showBorder': True,
            'roundedCorners': True, 'items': [_chart({
                'type': 'Chart.Gauge',
                'title': 'Disk',
                'subLabel': gauges.get('disk_label', ''),
                'value': used,
                'valueFormat': 'Percentage',
                'showMinMax': False,
                'segments': [
                    {'legend': 'Used', 'value': used,
                     'color': 'attention' if used >= 85 else
                              'warning' if used >= 70 else 'good'},
                    {'legend': 'Free', 'value': 100 - used, 'color': 'neutral'},
                ],
            }, 'Disk {}% used'.format(used))],
        })

    if gauges.get('cpu_temp_c') is not None:
        temp = round(gauges['cpu_temp_c'])
        gauge_cols.append({
            'type': 'Column', 'width': 'stretch', 'showBorder': True,
            'roundedCorners': True, 'spacing': 'Small', 'items': [_chart({
                'type': 'Chart.Gauge',
                'title': 'CPU temp',
                'subLabel': '{} °C'.format(temp),
                'value': temp,
                'min': 20,
                'max': 90,
                'valueFormat': 'Fraction',
                'segments': [
                    {'legend': 'Cool', 'value': 30, 'color': 'good'},
                    {'legend': 'Warm', 'value': 20, 'color': 'warning'},
                    {'legend': 'Throttle', 'value': 20, 'color': 'attention'},
                ],
            }, 'CPU temp {} C'.format(temp))],
        })

    signal = snap.get('signal', [])
    if signal:
        gauge_cols.append({
            'type': 'Column', 'width': 'stretch', 'showBorder': True,
            'roundedCorners': True, 'spacing': 'Small',
            'targetWidth': 'atLeast:Standard',
            'items': [_chart({
                'type': 'Chart.HorizontalBar',
                'title': 'Link quality',
                'colorSet': 'categorical',
                'displayMode': 'AbsoluteWithAxis',
                'data': [
                    {'x': s['label'], 'y': s['value'],
                     'color': _SERIES_COLORS[i % len(_SERIES_COLORS)]}
                    for i, s in enumerate(signal)
                ],
            }, 'Link quality: ' + ', '.join(
                '{} {}'.format(s['label'], s['value']) for s in signal))],
        })

    if gauge_cols:
        body.append({'type': 'ColumnSet', 'spacing': 'Medium', 'columns': gauge_cols})

    # --- recent contacts table (scrollable) ------------------------------
    recent = snap.get('recent', [])
    if recent:
        header = {
            'type': 'TableRow',
            'style': 'emphasis',
            'cells': [_table_cell([_text(h, weight='Bolder', size='Small')])
                      for h in ('Time', 'Flight', 'Operator', 'Alt', 'Dist')],
        }
        rows = [header]
        for r in recent:
            rows.append({
                'type': 'TableRow',
                'cells': [
                    _table_cell([_text(r['time'], size='Small', isSubtle=True)]),
                    _table_cell([_text('**{}**'.format(r['flight']), size='Small')]),
                    _table_cell([_text(r['airline'], size='Small', wrap=True)]),
                    _table_cell([_text(r['alt'], size='Small',
                                       horizontalAlignment='Right')]),
                    _table_cell([_text(r['dist'], size='Small',
                                       horizontalAlignment='Right')]),
                ],
            })

        body.append({
            'type': 'Container',
            'spacing': 'Medium',
            'maxHeight': '260px',
            'items': [
                {
                    'type': 'ColumnSet',
                    'columns': [
                        {'type': 'Column', 'width': 'auto',
                         'verticalContentAlignment': 'Center',
                         'items': [_icon('Radar', size='xSmall')]},
                        {'type': 'Column', 'width': 'stretch',
                         'verticalContentAlignment': 'Center',
                         'items': [_text('RECENT CONTACTS', weight='Bolder',
                                         size='Small', isSubtle=True, spacing='Small')]},
                    ],
                },
                {
                    'type': 'Table',
                    'spacing': 'Small',
                    'showGridLines': True,
                    'roundedCorners': True,
                    'firstRowAsHeaders': True,
                    'gridStyle': 'default',
                    'horizontalCellContentAlignment': 'Left',
                    'columns': [
                        {'width': 1}, {'width': 1}, {'width': 2},
                        {'width': 1}, {'width': 1},
                    ],
                    'rows': rows,
                },
            ],
        })

    # --- collapsible drill-down -----------------------------------------
    detail_items: List[Dict[str, Any]] = []
    facts = snap.get('facts', [])
    if facts:
        detail_items.append({
            'type': 'FactSet',
            'facts': [{'title': f['title'], 'value': f['value']} for f in facts],
        })
    if snap.get('log_tail'):
        detail_items.append({
            'type': 'CodeBlock',
            'language': 'PlainText',
            'codeSnippet': snap['log_tail'],
            'startLineNumber': 1,
            'spacing': 'Medium',
            'fallback': _text('```\n{}\n```'.format(snap['log_tail']),
                              fontType='Monospace', size='Small', wrap=True),
        })

    if detail_items:
        body.append({
            'type': 'Container',
            'id': 'detailPanel',
            'isVisible': False,
            'style': 'emphasis',
            'roundedCorners': True,
            'showBorder': True,
            'spacing': 'Medium',
            'maxHeight': '320px',
            'items': detail_items,
        })

    # --- compound button rail -------------------------------------------
    body.append({
        'type': 'ColumnSet',
        'spacing': 'Medium',
        'targetWidth': 'atLeast:Narrow',
        'columns': [
            {'type': 'Column', 'width': 'stretch', 'items': [{
                'type': 'CompoundButton',
                'title': 'Live radar',
                'description': 'Open the map view',
                'icon': {'name': 'Radar', 'style': 'Filled'},
                'selectAction': {'type': 'Action.OpenUrl',
                                 'title': 'Live radar',
                                 'url': device.get('radar_url') or base_url or 'https://qdrn.io'},
                'fallback': {'type': 'ActionSet', 'actions': [
                    {'type': 'Action.OpenUrl', 'title': 'Live radar',
                     'url': device.get('radar_url') or base_url or 'https://qdrn.io'}]},
            }]},
            {'type': 'Column', 'width': 'stretch', 'spacing': 'Small', 'items': [{
                'type': 'CompoundButton',
                'title': 'Restart app',
                'description': 'skytrack-app.service',
                'icon': {'name': 'ArrowSync', 'style': 'Filled'},
                'badge': 'admin',
                'selectAction': {
                    'type': 'Action.Execute',
                    'verb': 'skytrack.restartApp',
                    'title': 'Restart app',
                    'data': {'device': device.get('short_id'), 'unit': 'skytrack-app'},
                },
                'fallback': {'type': 'ActionSet', 'actions': [
                    {'type': 'Action.Submit', 'title': 'Restart app',
                     'data': {'verb': 'skytrack.restartApp'}}]},
            }]},
            {'type': 'Column', 'width': 'stretch', 'spacing': 'Small',
             'targetWidth': 'atLeast:Standard', 'items': [{
                'type': 'CompoundButton',
                'title': 'Diagnostics',
                'description': 'Toggle detail panel',
                'icon': {'name': 'Stethoscope', 'style': 'Filled'},
                'selectAction': {'type': 'Action.ToggleVisibility',
                                 'title': 'Diagnostics',
                                 'targetElements': ['detailPanel']},
                'fallback': {'type': 'ActionSet', 'actions': [
                    {'type': 'Action.ToggleVisibility', 'title': 'Diagnostics',
                     'targetElements': ['detailPanel']}]},
             }]},
        ],
    })

    body.append(_text(
        'Snapshot {} · auto-refreshes in Teams'.format(snap.get('generated_at', '')),
        size='Small', isSubtle=True, spacing='Medium', horizontalAlignment='Right'))

    # --- card actions ----------------------------------------------------
    actions: List[Dict[str, Any]] = [
        {
            'type': 'Action.Execute',
            'title': 'Refresh',
            'verb': 'skytrack.refresh',
            'iconUrl': 'icon:ArrowSync,filled',
            'style': 'positive',
            'data': {'device': device.get('short_id')},
            'fallback': {'type': 'Action.Submit', 'title': 'Refresh',
                         'data': {'verb': 'skytrack.refresh'}},
        },
        {
            'type': 'Action.ShowCard',
            'title': 'Cellular',
            'iconUrl': 'icon:CellularData1,filled',
            'card': {
                'type': 'AdaptiveCard',
                '$schema': SCHEMA,
                'version': VERSION,
                'body': [
                    _text('Change APN', weight='Bolder', size='Medium'),
                    _text('Applied on the next modem re-registration. '
                          'The device drops off the network for up to 60 seconds.',
                          size='Small', isSubtle=True, wrap=True),
                    {
                        'type': 'Input.ChoiceSet',
                        'id': 'apnPreset',
                        'label': 'Carrier preset',
                        'style': 'compact',
                        'value': 'custom',
                        'choices': [
                            {'title': 'AT&T — broadband', 'value': 'broadband'},
                            {'title': 'AT&T IoT — m2m.com.attz', 'value': 'm2m.com.attz'},
                            {'title': 'T-Mobile — fast.t-mobile.com',
                             'value': 'fast.t-mobile.com'},
                            {'title': 'Custom…', 'value': 'custom'},
                        ],
                    },
                    {
                        'type': 'Input.Text',
                        'id': 'apnCustom',
                        'label': 'Custom APN',
                        'placeholder': 'apn.carrier.net',
                        'isRequired': False,
                        'maxLength': 64,
                    },
                    {
                        'type': 'Input.Toggle',
                        'id': 'apnReboot',
                        'title': 'Reboot after applying',
                        'value': 'false',
                        'valueOn': 'true',
                        'valueOff': 'false',
                    },
                ],
                'actions': [
                    {
                        'type': 'Action.Execute',
                        'title': 'Apply',
                        'verb': 'skytrack.setApn',
                        'style': 'positive',
                        'associatedInputs': 'auto',
                        'data': {'device': device.get('short_id')},
                        'fallback': {'type': 'Action.Submit', 'title': 'Apply',
                                     'data': {'verb': 'skytrack.setApn'}},
                    },
                    {
                        'type': 'Action.ResetInputs',
                        'title': 'Reset',
                        'fallback': 'drop',
                    },
                ],
            },
        },
        {
            'type': 'Action.ToggleVisibility',
            'title': 'Detail',
            'iconUrl': 'icon:ChevronDown,regular',
            'targetElements': ['detailPanel'],
        },
        {
            'type': 'Action.OpenUrl',
            'title': 'Open dashboard',
            'url': base_url or 'https://qdrn.io',
            'iconUrl': 'icon:Open,regular',
            'mode': 'secondary',
        },
        {
            'type': 'Action.Execute',
            'title': 'Reboot device',
            'verb': 'skytrack.reboot',
            'style': 'destructive',
            'mode': 'secondary',
            'data': {'device': device.get('short_id')},
            'fallback': 'drop',
        },
    ]

    card: Dict[str, Any] = {
        'type': 'AdaptiveCard',
        '$schema': SCHEMA,
        'version': VERSION,
        'speak': '{}. {}'.format(device.get('name', 'SkyTrack'),
                                 status.get('headline', '')),
        'msteams': {'width': 'Full'},
        'body': body,
        'actions': actions,
    }

    refresh_ids = snap.get('refresh_user_ids') or []
    card['refresh'] = {
        'userIds': refresh_ids,
        'action': {
            'type': 'Action.Execute',
            'title': 'Refresh',
            'verb': 'skytrack.refresh',
            'data': {'device': device.get('short_id')},
        },
    }
    return card


# ---------------------------------------------------------------------------
# snapshot sources
# ---------------------------------------------------------------------------

def collect_snapshot(app=None, base_url: str = '') -> Dict[str, Any]:
    """Build a snapshot from a live SkyTrack Flask app.

    Every section is independently guarded — a missing sensor or an
    empty database degrades that one block, it does not fail the card.
    """
    if app is None:
        from flask import current_app as app  # type: ignore

    import _version

    snap: Dict[str, Any] = {'generated_at': _utc_stamp()}

    identity = {}
    try:
        identity = app.config.get('DEVICE_RECORD', {}) or {}
    except Exception as exc:
        logger.warning('device record unavailable: %s', exc)

    radar_url = ''
    try:
        import device_id
        radar_url = device_id.radar_url(identity) if identity else ''
    except Exception:
        pass

    snap['device'] = {
        'name': identity.get('name') or 'SkyTrack',
        'short_id': identity.get('short_id') or identity.get('device_id') or '—',
        'version': getattr(_version, '__version__', '?'),
        'site': identity.get('location') or identity.get('site') or '',
        'radar_url': radar_url,
        'base_url': base_url,
    }

    health = {}
    try:
        health = app.health_svc.get_status() or {}
    except Exception as exc:
        logger.warning('health unavailable: %s', exc)

    disk = health.get('disk') or {}
    cpu = health.get('cpu_temp') or {}
    snap['gauges'] = {
        'disk_pct': disk.get('percent'),
        'disk_label': ('{} GB free of {} GB'.format(disk.get('free_gb'), disk.get('total_gb'))
                       if disk else ''),
        'cpu_temp_c': cpu.get('celsius') if isinstance(cpu, dict) else None,
    }

    try:
        import dashboard_svc
        now = dashboard_svc.card_aircraft_now()
        today = dashboard_svc.card_aircraft_today()
        busiest = dashboard_svc.card_busiest_hour()
        last = dashboard_svc.card_last_aircraft()
        snap['kpis'] = [
            {'label': 'Aircraft now', 'value': now.get('count', 0), 'unit': 'live',
             'icon': 'Airplane', 'value_color': 'Accent'},
            {'label': 'Seen today', 'value': today.get('count', 0), 'unit': 'unique',
             'icon': 'CalendarLtr'},
            {'label': 'Busiest hour', 'value': (
                '{:02d}Z'.format(busiest['hour']) if busiest.get('hour') is not None else '—'),
             'unit': '{} ac'.format(busiest.get('count', 0)), 'icon': 'ChartMultiple'},
            {'label': 'Last contact', 'value': (last or {}).get('flight') or '—',
             'unit': (last or {}).get('airline', ''), 'icon': 'Clock'},
        ]
    except Exception as exc:
        logger.warning('dashboard KPIs unavailable: %s', exc)
        snap['kpis'] = []

    try:
        import dashboard_svc
        raw = dashboard_svc.trend('24h') or {}
        points = raw.get('points') or raw.get('data') or []
        snap['trend'] = [
            {'x': str(p.get('x') or p.get('label') or p.get('hour')),
             'y': int(p.get('y') or p.get('count') or 0)}
            for p in points
        ]
    except Exception as exc:
        logger.warning('trend unavailable: %s', exc)
        snap['trend'] = []

    try:
        import dashboard_svc
        snap['airlines'] = [
            {'name': a.get('airline') or a.get('name') or 'Unknown',
             'count': int(a.get('count') or a.get('n') or 0)}
            for a in (dashboard_svc.top_airlines('24h', 6) or [])
        ]
    except Exception as exc:
        logger.warning('top airlines unavailable: %s', exc)
        snap['airlines'] = []

    try:
        import dashboard_svc
        snap['recent'] = [
            {'time': str(r.get('ts', ''))[11:16],
             'flight': r.get('flight') or r.get('callsign') or r.get('icao', ''),
             'airline': r.get('airline') or '',
             'alt': ('{:,} ft'.format(int(r['altitude'])) if r.get('altitude') else '—'),
             'dist': ('{:.0f} nm'.format(float(r['distance'])) if r.get('distance') else '—')}
            for r in (dashboard_svc.recent_aircraft(12) or [])
        ]
    except Exception as exc:
        logger.warning('recent aircraft unavailable: %s', exc)
        snap['recent'] = []

    net = health.get('network') or {}
    snap['signal'] = []
    for label, key in (('Wi-Fi', 'wifi_signal'), ('Cellular', 'cell_signal')):
        val = net.get(key)
        if isinstance(val, (int, float)):
            snap['signal'].append({'label': label, 'value': int(val)})

    sensor = {}
    try:
        sensor = app.sensor_svc.read() or {}
    except Exception:
        pass

    snap['facts'] = [
        {'title': 'Pi uptime', 'value': (health.get('uptime') or {}).get('display', '—')},
        {'title': 'App uptime', 'value': (health.get('app_uptime') or {}).get('display', '—')},
        {'title': 'Cloudflared', 'value': str(health.get('cloudflared', '—'))},
        {'title': 'Link', 'value': str(net.get('type') or net.get('interface') or '—')},
        {'title': 'Internal temp',
         'value': ('{} °F'.format(sensor.get('temperature_f')) if sensor.get('temperature_f')
                   else '—')},
        {'title': 'Humidity',
         'value': ('{} %'.format(sensor.get('humidity')) if sensor.get('humidity') else '—')},
        {'title': 'Sensor source', 'value': str(sensor.get('source', '—'))},
    ]

    ok = (snap['gauges'].get('disk_pct') or 0) < 85 and health.get('cloudflared') not in (
        None, False, 'inactive', 'failed')
    snap['status'] = {
        'level': 'good' if ok else 'warning',
        'headline': ('All systems nominal' if ok
                     else 'Degraded — check the detail panel'),
        'since': 'as of {}'.format(snap['generated_at']),
    }
    snap['log_tail'] = ''
    return snap


def demo_snapshot(seed: int = 7) -> Dict[str, Any]:
    """Deterministic sample data so the card renders off-device."""
    rng = random.Random(seed)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    trend = []
    for i in range(23, -1, -1):
        hour = now - timedelta(hours=i)
        base = 14 + 16 * (1 if 11 <= hour.hour <= 22 else 0)
        trend.append({'x': hour.strftime('%Y-%m-%dT%H:00:00'),
                      'y': max(1, base + rng.randint(-6, 9))})

    airlines = [
        {'name': 'Delta', 'count': 184}, {'name': 'American', 'count': 121},
        {'name': 'United', 'count': 96}, {'name': 'Southwest', 'count': 74},
        {'name': 'SkyWest', 'count': 52}, {'name': 'Other', 'count': 118},
    ]

    fleet = [
        ('DAL1287', 'Delta', 34000, 41), ('AAL906', 'American', 28500, 18),
        ('UAL2213', 'United', 37000, 63), ('SWA455', 'Southwest', 12200, 9),
        ('SKW3390', 'SkyWest', 21000, 27), ('FDX1155', 'FedEx', 39000, 88),
        ('UPS2841', 'UPS', 36000, 71), ('JBU1207', 'JetBlue', 31000, 52),
        ('N472QD', 'General aviation', 4500, 6), ('RPA4412', 'Republic', 24000, 33),
    ]
    recent = []
    for i, (flight, airline, alt, dist) in enumerate(fleet):
        ts = now.replace(minute=0) - timedelta(minutes=i * 4 + rng.randint(0, 3))
        recent.append({'time': ts.strftime('%H:%M'), 'flight': flight,
                       'airline': airline, 'alt': '{:,} ft'.format(alt),
                       'dist': '{} nm'.format(dist)})

    return {
        'generated_at': _utc_stamp(),
        'device': {
            'name': 'QDRNow-SkyTrack',
            'short_id': 'QDRN-BC01',
            'version': '2.8.0',
            'site': 'Bay City, MI',
            'radar_url': 'https://radar2.qdrn.io',
            'base_url': 'https://radar2.qdrn.io',
            'hero_image': '',
        },
        'status': {
            'level': 'warning',
            'headline': 'Cellular link degraded — failed over to Wi-Fi 41 min ago',
            'since': 'as of ' + _utc_stamp(),
        },
        'kpis': [
            {'label': 'Aircraft now', 'value': 23, 'unit': 'live', 'icon': 'Airplane',
             'value_color': 'Accent', 'delta': '+6 vs 1h ago', 'delta_dir': 'up'},
            {'label': 'Seen today', 'value': '1,412', 'unit': 'unique',
             'icon': 'CalendarLtr', 'delta': '+11%', 'delta_dir': 'up'},
            {'label': 'Msg rate', 'value': 964, 'unit': '/sec', 'icon': 'Pulse',
             'delta': '-4%', 'delta_dir': 'down'},
            {'label': 'Max range', 'value': 191, 'unit': 'nm', 'icon': 'Target',
             'delta': 'no change', 'delta_dir': 'flat'},
        ],
        'trend': trend,
        'airlines': airlines,
        'gauges': {
            'disk_pct': 62,
            'disk_label': '47.4 GB free of 125 GB',
            'cpu_temp_c': 58,
        },
        'signal': [
            {'label': 'Wi-Fi RSSI', 'value': 71},
            {'label': 'LTE RSRP', 'value': 34},
            {'label': 'Tunnel', 'value': 98},
        ],
        'recent': recent,
        'facts': [
            {'title': 'Pi uptime', 'value': '19d 4h 22m'},
            {'title': 'App uptime', 'value': '2h 06m'},
            {'title': 'Cloudflared', 'value': 'active (radar2.qdrn.io)'},
            {'title': 'Link', 'value': 'wlan0 (failover from wwan0)'},
            {'title': 'Internal temp', 'value': '81.4 °F'},
            {'title': 'Humidity', 'value': '58 %'},
            {'title': 'Watchdog triggers (24h)', 'value': '3 — last 41m ago'},
        ],
        'log_tail': (
            'May 17 14:02:11 skytrack health_watchdog[2211]: cellular unreachable, '
            'attempt 3/3\n'
            'May 17 14:02:12 skytrack health_watchdog[2211]: failing over wwan0 -> wlan0\n'
            'May 17 14:02:19 skytrack cloudflared[1180]: Registered tunnel connection '
            'connIndex=0 location=ord07\n'
            'May 17 14:02:19 skytrack health_watchdog[2211]: tunnel healthy, '
            'app healthy, exiting 0'
        ),
        'refresh_user_ids': [],
    }


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


if __name__ == '__main__':
    import json
    import sys
    print(json.dumps(build_card(demo_snapshot()), indent=2))
    sys.exit(0)
