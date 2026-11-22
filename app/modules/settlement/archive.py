from datetime import datetime, timezone

from app.clock import stamp
from app.errors import DomainError
from app.shift import load_shift
from .service import build_settlement


def settle(store, run_id, vouchers):
    """出具并发布结算结论；同一凭证重试幂等，新凭证出新版，旧版结论与凭证全部留档。"""
    # 班次已重排时先拦下，避免把旧班次的凭证误结到当前班次。
    if store.active() != run_id:
        raise DomainError('STALE_SETTLEMENT', 409, current=store.active())
    snapshot = store.load(run_id)
    shift = load_shift(store.sources(run_id)['shift'])
    conclusion = build_settlement(shift, snapshot, vouchers)
    if store.active_settlement(run_id) == conclusion['id']:
        # 同一现场凭证重试：结论编号相同，直接沿用，不产生新版本、不重复结算。
        return {'id': conclusion['id'], 'created': False, 'changed': False, 'duplicate': True}
    # 同一行程若此前已用其他凭证结算，在结论中标注替换关系，旧版本仍留档可回切。
    prior_by_leg = {row['leg_id']: row['voucher_id'] for row in store.vouchers(run_id)}
    conclusion['supersedes'] = [
        {'leg_id': row['leg_id'], 'previous_voucher': prior_by_leg[row['leg_id']],
         'new_voucher': row['voucher_id']}
        for row in conclusion['vouchers']
        if prior_by_leg.get(row['leg_id']) not in (None, row['voucher_id'])]
    conclusion['generated_at'] = stamp(datetime.now(timezone.utc))
    created = store.publish_settlement(conclusion, conclusion['vouchers'], run_id)
    return {'id': conclusion['id'], 'created': created, 'changed': True,
            'duplicate': not created, 'superseded': bool(conclusion['supersedes'])}


def reopen(store, run_id, settlement_id):
    """重开核对：载入指定历史版本结论，不改动任何已留档凭证。"""
    return store.load_settlement(settlement_id)


def restore(store, run_id, settlement_id, expected):
    return store.restore_settlement(run_id, settlement_id, expected)
