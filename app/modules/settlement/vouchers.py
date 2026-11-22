from app.clock import instant, stamp
from app.errors import DomainError

# 现场凭证固定字段；凭证是现场签收/管制记录，不允许携带多余字段。
VOUCHER_FIELDS = {'voucher_id', 'leg_id', 'truck', 'station', 'at', 'count', 'outcome'}
OUTCOMES = ('delivered', 'returned')


def validate_vouchers(shift, legs, vouchers):
    """核对现场凭证与原派车单，返回规范化后的凭证列表。

    每趟已发布行程必须恰好有一张凭证：delivered 为到站卸车签收，
    returned 为临时管制后原车返回装车站。凭证与派车单的车辆、站点和辆数
    必须一致，否则逐张给出原因码，绝不静默改账。
    """
    if not isinstance(vouchers, list) or not vouchers:
        raise DomainError('VOUCHER_SHAPE', reason='empty')
    plan = {leg['id']: leg for leg in legs}
    seen, covered, normalized = set(), set(), []
    for index, voucher in enumerate(vouchers, 1):
        if not isinstance(voucher, dict) or set(voucher) != VOUCHER_FIELDS:
            raise DomainError('VOUCHER_SHAPE', index=index)
        key = voucher['voucher_id']
        if not isinstance(key, str) or not key.strip() or key in seen:
            raise DomainError('VOUCHER_SHAPE', index=index, field='voucher_id')
        seen.add(key)
        leg_id = voucher['leg_id']
        leg = plan.get(leg_id)
        if leg is None:
            raise DomainError('VOUCHER_SHAPE', voucher=key, field='leg_id')
        if leg_id in covered:
            raise DomainError('CONFLICTING_VOUCHER', leg=leg_id, new_voucher=key)
        covered.add(leg_id)
        if voucher['outcome'] not in OUTCOMES:
            raise DomainError('VOUCHER_SHAPE', voucher=key, field='outcome')
        if voucher['truck'] != leg['truck']:
            raise DomainError('VOUCHER_TRUCK_MISMATCH', voucher=key, leg=leg_id,
                              voucher_truck=voucher['truck'], waybill_truck=leg['truck'])
        expected_station = leg['to'] if voucher['outcome'] == 'delivered' else leg['from']
        if voucher['station'] != expected_station:
            raise DomainError('VOUCHER_STATION_MISMATCH', voucher=key, leg=leg_id,
                              station=voucher['station'], expected=expected_station)
        if type(voucher['count']) is not int or voucher['count'] != leg['count']:
            raise DomainError('VOUCHER_COUNT_MISMATCH', voucher=key, leg=leg_id,
                              count=voucher['count'], expected=leg['count'])
        try:
            at = instant(voucher['at'])
        except DomainError:
            raise DomainError('VOUCHER_TIME_INVALID', voucher=key, leg=leg_id) from None
        if not instant(leg['depart']) <= at < shift.end:
            raise DomainError('VOUCHER_TIME_INVALID', voucher=key, leg=leg_id, at=stamp(at),
                              shift_end=stamp(shift.end))
        normalized.append(dict(voucher, voucher_id=key.strip(), at=stamp(at)))
    if set(plan) - covered:
        raise DomainError('LEGS_NOT_SETTLED', legs=sorted(set(plan) - covered))
    return sorted(normalized, key=lambda row: (row['leg_id'], row['voucher_id']))
