from collections import Counter

from app.config import SETTLE_RULE_VERSION
from app.errors import DomainError
from app.manifest import canonical, digest
from app.modules.demand.service import demand
from app.modules.dispatch.compare import compare
from app.modules.inventory.service import reconcile
from .replay import settle_replay
from .vouchers import validate_vouchers


def _work(shift, plan_legs, vouchers):
    """司机班工时口径：装车、落地卸车、返程空驶回站分段统计。"""
    by_leg = {row['leg_id']: row for row in vouchers}
    rows, totals = [], Counter()
    for leg in plan_legs:
        voucher = by_leg[leg['id']]
        delivered = voucher['outcome'] == 'delivered'
        rows.append({
            'leg_id': leg['id'], 'truck': leg['truck'], 'voucher_id': voucher['voucher_id'],
            'from': leg['from'], 'to': leg['to'], 'count': leg['count'],
            'loaded_at': leg['depart'], 'settled_at': voucher['at'],
            'outcome': voucher['outcome'],
            'status': 'delivered' if delivered else 'returned',
            'return_station': None if delivered else leg['from'],
            'load_count': leg['count'],
            'landed_count': leg['count'] if delivered else 0,
            'returned_count': 0 if delivered else leg['count'],
            'empty_return_count': 0 if delivered else leg['count'],
            'pay_segments': ['load', 'unload'] if delivered else ['load', 'empty_return', 'return_unload'],
        })
        totals['legs'] += 1
        totals['loaded'] += leg['count']
        totals['landed' if delivered else 'returned'] += leg['count']
        if not delivered:
            totals['empty_legs'] += 1
    return rows, dict(totals)


def _station_rows(shift, vouchers, base, plan, actual):
    """逐站给出可借车与未成交相对原派车方案、无调拨基准的变化。"""
    by_station = Counter()
    for voucher in vouchers:
        if voucher['outcome'] == 'returned':
            by_station[voucher['station']] += voucher['count']
    plan_miss = {row['station']: row['after'] for row in plan['stations']}
    base_miss = {row['station']: row['before'] for row in plan['stations']}
    rows = []
    for code in shift.stations:
        closing = actual['closing'][code]
        rows.append({
            'station': code,
            'borrowable': closing['docked'],
            'held': closing['held'],
            'free_slots': closing['free'],
            'returned_bikes': by_station.get(code, 0),
            'missed_base': base_miss[code],
            'missed_waybill': plan_miss[code],
            'missed_actual': sum(1 for row in actual['timeline']
                                 if row['station'] == code and row['kind'] in {'rent', 'return'} and row['code']),
        })
    for row in rows:
        row['missed_delta_vs_waybill'] = row['missed_actual'] - row['missed_waybill']
        row['missed_delta_vs_base'] = row['missed_actual'] - row['missed_base']
    return rows


def build_settlement(shift, snapshot, vouchers):
    """依据已发布班次和现场凭证，出具这趟调拨的结算结论。"""
    plan_legs = snapshot['dispatch']['legs']
    clean = validate_vouchers(shift, plan_legs, vouchers)
    received, limits = snapshot['intake'], snapshot['capacity']
    actual = settle_replay(shift, received['events'], limits['segments'], plan_legs, clean)
    actual_demand = demand(shift, actual)
    audit = reconcile(shift, actual, received, limits)
    if not audit['ok']:
        raise DomainError('INVENTORY_MISMATCH', 409)
    against_waybill = compare(shift, _replay(snapshot, 'plan'), actual)
    against_base = compare(shift, _replay(snapshot, 'base'), actual)
    work, totals = _work(shift, plan_legs, clean)
    identity = {
        'run_id': snapshot['manifest']['id'],
        'rule_version': SETTLE_RULE_VERSION,
        'vouchers': clean,
        'plan': [{key: leg[key] for key in ('id', 'truck', 'from', 'to', 'depart', 'arrive', 'count')}
                 for leg in plan_legs],
    }
    return {
        'id': digest(canonical(identity).encode()),
        'run_id': snapshot['manifest']['id'],
        'rule_version': SETTLE_RULE_VERSION,
        'zone': snapshot['manifest']['zone'],
        'waybill': identity['plan'],
        'vouchers': clean,
        'conclusion': {
            'basis': 'actual_execution',
            'summary': ('按现场凭证结算：到站签收的行程按整趟落地结算；临时管制返程的行程只结装车与空驶返程段，'
                        '车辆退回装车站，不计入目的地。'),
            'delivered_legs': sum(1 for row in work if row['status'] == 'delivered'),
            'returned_legs': sum(1 for row in work if row['status'] == 'returned'),
            'bikes_loaded': totals['loaded'],
            'bikes_landed': totals.get('landed', 0),
            'bikes_returned': totals.get('returned', 0),
            'empty_return_legs': totals.get('empty_legs', 0),
            'in_transit_end': actual['transit'],
        },
        'legs': work,
        'stations': _station_rows(shift, clean, snapshot['replay']['base'],
                                  snapshot['dispatch']['comparison'], actual),
        'comparison': {'vs_waybill': against_waybill, 'vs_base': against_base},
        'demand': actual_demand,
        'inventory': audit,
        'timeline': actual['timeline'],
        'actual': actual['actual'],
    }


def _replay(snapshot, name):
    return snapshot['replay'][name]
