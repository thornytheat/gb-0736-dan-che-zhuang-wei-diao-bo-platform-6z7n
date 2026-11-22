from app.clock import instant, stamp
from app.errors import DomainError
from app.modules.replay.state import Dock, attempt

RETURN_KIND = 'return_unload'


def actual_legs(plan_legs, vouchers):
    """把原派车单与现场凭证展开为实际执行的装卸动作。

    delivered：装车后在目的地卸车落地；returned：装车后遭遇管制，
    原车空驶返程，在装车站卸回，不进入目的地库存。
    """
    by_leg = {row['leg_id']: row for row in vouchers}
    actions, rows = [], []
    for leg in plan_legs:
        voucher = by_leg[leg['id']]
        actions.append((instant(leg['depart']), 2, leg['id'],
                        {'station': leg['from'], 'count': leg['count'], 'line': None}))
        if voucher['outcome'] == 'delivered':
            kind, station, at = 'unload', leg['to'], voucher['at']
        else:
            kind, station, at = RETURN_KIND, leg['from'], voucher['at']
        rows.append({'id': leg['id'], 'kind': kind, 'station': station, 'at': at,
                     'count': leg['count'], 'voucher_id': voucher['voucher_id'],
                     'outcome': voucher['outcome']})
        actions.append((instant(at), 1, leg['id'],
                        {'station': station, 'count': leg['count'], 'line': None,
                         'settle_kind': kind, 'voucher_id': voucher['voucher_id']}))
    return sorted(actions, key=lambda item: item[:3]), rows


def settle_replay(shift, events, segments, plan_legs, vouchers):
    """按实际执行段回放库存，返程回站车辆进桩，桩满转站内暂存而不是丢车。"""
    docks = {code: Dock(0, row['opening']) for code, row in shift.stations.items()}
    road, transit = shift.opening_road, 0
    timeline, executed = [], {}
    action_rows, actual = actual_legs(plan_legs, vouchers)
    for row in actual:
        executed[row['id']] = row
    steps = [(instant(row['start']), 0, row['station'], row) for row in segments]
    steps.extend(action_rows)
    steps.extend((instant(event['at']), 3, event['id'], event) for event in events)
    steps.sort(key=lambda row: row[:3])
    for at, priority, key, row in steps:
        station = row['station']
        dock = docks[station]
        previous = dock.public()
        code, kind, quantity = None, None, 0
        if priority == 0:
            kind = 'capacity'
            dock.capacity, dock.version_line = row['slots'], row['line']
            dock.reseat()
        elif priority in (1, 2):
            kind = row.get('settle_kind', 'load')
            quantity = row['count']
            if kind == 'load':
                if dock.docked < quantity:
                    raise DomainError('PICKUP_SHORT', leg=key, station=station, at=stamp(at),
                                      available=dock.docked, requested=quantity,
                                      version_line=dock.version_line)
                dock.docked -= quantity
                transit += quantity
                dock.reseat()
            else:
                # 到站落地与返程回站都把车放回站内；桩满由 reseat 转暂存，守恒不丢。
                dock.docked += quantity
                transit -= quantity
                dock.reseat()
        else:
            kind = row['kind']
            quantity = 1
            code, road = attempt(dock, kind, road)
        timeline.append({'at': stamp(at), 'id': key, 'kind': kind, 'station': station,
                         'line': row.get('line'), 'code': code, 'late': row.get('late', False),
                         'before': previous, 'after': dock.public(), 'road': road, 'transit': transit,
                         'quantity': quantity, 'voucher_id': executed.get(key, {}).get('voucher_id')})
    return {'timeline': timeline,
            'closing': {code: dock.public() for code, dock in docks.items()},
            'road': road, 'transit': transit, 'actual': actual}
