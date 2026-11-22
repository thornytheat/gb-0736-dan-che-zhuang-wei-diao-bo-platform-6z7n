from flask import Blueprint, Response, jsonify, request

from app.errors import DomainError
from app.manifest import canonical
from app.shift import load_shift
from app.web import store
from . import archive
from .evidence import settlement_evidence
from .service import build_settlement

bp = Blueprint('settlement', __name__)


def payload(*required):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != set(required):
        raise DomainError('BAD_REQUEST', fields=list(required))
    for field in ('run', 'expected', 'settlement'):
        if field in data and (not isinstance(data[field], str) or not data[field]):
            raise DomainError('BAD_REQUEST', field=field)
    return data


def _require_settlement():
    data = payload('run', 'vouchers')
    if not isinstance(data['vouchers'], list) or not data['vouchers']:
        raise DomainError('VOUCHER_SHAPE', reason='empty')
    return data


@bp.get('/api/settlement')
def show():
    db = store()
    run_id = db.active()
    return jsonify(run=run_id, active=db.active_settlement(run_id) if run_id else None,
                   history=db.settlement_history(run_id) if run_id else [],
                   vouchers=db.vouchers(run_id) if run_id else [])


@bp.post('/api/settlement/preview')
def preview():
    data = _require_settlement()
    db = store()
    snapshot = db.load(data['run'])
    shift = load_shift(db.sources(data['run'])['shift'])
    return jsonify(settlement=build_settlement(shift, snapshot, data['vouchers']))


@bp.post('/api/settlement/publish')
def publish():
    data = _require_settlement()
    return jsonify(archive.settle(store(), data['run'], data['vouchers']))


@bp.get('/api/settlement/<settlement_id>')
def detail(settlement_id):
    return jsonify(settlement=store().load_settlement(settlement_id))


@bp.get('/api/settlement/<settlement_id>/evidence.json')
def export(settlement_id):
    conclusion = store().load_settlement(settlement_id)
    return Response(canonical(settlement_evidence(conclusion)) + '\n', mimetype='application/json',
                    headers={'Content-Disposition':
                             f'attachment; filename="settlement-{settlement_id[:12]}.json"'})


@bp.post('/api/settlement/restore')
def rollback():
    data = payload('run', 'settlement', 'expected')
    return jsonify(archive.restore(store(), data['run'], data['settlement'], data['expected']))
