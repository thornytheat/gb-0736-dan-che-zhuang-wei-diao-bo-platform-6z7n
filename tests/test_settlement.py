import pytest

VOUCHERS = [
    {'voucher_id': 'V-01', 'leg_id': 'M-01', 'truck': 'T-01', 'station': '002',
     'at': '2026-11-20T08:14:30+08:00', 'count': 3, 'outcome': 'delivered'},
    {'voucher_id': 'V-02', 'leg_id': 'M-02', 'truck': 'T-02', 'station': '003',
     'at': '2026-11-20T09:20:00+08:00', 'count': 2, 'outcome': 'returned'},
]


def publish_settlement(client, run_id, vouchers):
    return client.post('/api/settlement/publish', json={'run': run_id, 'vouchers': vouchers})


def test_settlement_uses_actual_execution_and_returns_bikes_to_origin(client, run_id):
    response = client.post('/api/settlement/preview', json={'run': run_id, 'vouchers': VOUCHERS})
    assert response.status_code == 200
    data = response.json['settlement']
    conclusion = data['conclusion']
    # 采用实际执行段：M-01 整趟落地，M-02 装车后返程，四辆口径中只有 3 辆落地。
    assert (conclusion['delivered_legs'], conclusion['returned_legs']) == (1, 1)
    assert (conclusion['bikes_loaded'], conclusion['bikes_landed'], conclusion['bikes_returned']) == (5, 3, 2)
    assert conclusion['in_transit_end'] == 0
    by_leg = {row['leg_id']: row for row in data['legs']}
    assert by_leg['M-01']['status'] == 'delivered' and by_leg['M-01']['landed_count'] == 3
    returned = by_leg['M-02']
    assert returned['status'] == 'returned'
    assert returned['return_station'] == '003' and returned['empty_return_count'] == 2
    assert returned['pay_segments'] == ['load', 'empty_return', 'return_unload']
    # 未落地车辆没有进入目的地 001，而是回到装车站 003。
    station_003 = next(row for row in data['stations'] if row['station'] == '003')
    assert station_003['returned_bikes'] == 2
    assert data['inventory']['ok'] is True and data['inventory']['in_transit_end'] == 0


def test_station_borrowable_and_missed_demand_change_vs_baselines(client, run_id):
    data = client.post('/api/settlement/preview',
                       json={'run': run_id, 'vouchers': VOUCHERS}).json['settlement']
    station_002 = next(row for row in data['stations'] if row['station'] == '002')
    # 002 实际拿到 3 辆落地车，未成交由基准 5 笔降到 2 笔。
    assert station_002['missed_base'] == 5 and station_002['missed_actual'] == 2
    assert station_002['missed_delta_vs_base'] == -3
    # 相对原派车单：M-02 未执行，003 的缺车未被补走，未成交与派车单口径相同。
    assert station_002['missed_waybill'] == 2 and station_002['missed_delta_vs_waybill'] == 0
    waybill = data['comparison']['vs_waybill']
    base = data['comparison']['vs_base']
    assert waybill['after'] == base['after'] == 8
    assert base['before'] == 11 and base['delta'] == -3


def test_retry_same_vouchers_is_idempotent_and_never_double_settles(client, run_id):
    first = publish_settlement(client, run_id, VOUCHERS)
    assert first.json['created'] is True and first.json['duplicate'] is False
    second = publish_settlement(client, run_id, VOUCHERS)
    assert second.json == {'id': first.json['id'], 'created': False, 'changed': False, 'duplicate': True}
    listing = client.get('/api/settlement').json
    assert len(listing['history']) == 1 and listing['active'] == first.json['id']
    with client.application.extensions['shift_store'].connect() as db:
        assert db.execute('SELECT count(*) FROM settlement_runs').fetchone() == (1,)
        assert db.execute('SELECT count(*) FROM settlement_vouchers').fetchone() == (2,)


def test_correction_creates_new_version_but_keeps_old_conclusion_and_vouchers(client, run_id):
    first = publish_settlement(client, run_id, VOUCHERS).json['id']
    corrected = [dict(VOUCHERS[0]),
                 {'voucher_id': 'V-03', 'leg_id': 'M-02', 'truck': 'T-02', 'station': '001',
                  'at': '2026-11-20T09:25:00+08:00', 'count': 2, 'outcome': 'delivered'}]
    result = publish_settlement(client, run_id, corrected)
    assert result.json['created'] is True and result.json['superseded'] is True
    assert result.json['id'] != first
    listing = client.get('/api/settlement').json
    assert listing['active'] == result.json['id'] and len(listing['history']) == 2
    # 旧版结论仍可重开读取，原凭证 V-02 留档未删。
    old = client.get(f'/api/settlement/{first}')
    assert old.status_code == 200 and old.json['settlement']['id'] == first
    archived = {row['voucher_id'] for row in listing['vouchers']}
    assert {'V-01', 'V-02', 'V-03'} <= archived


def test_reopen_and_restore_historical_settlement(client, run_id):
    first = publish_settlement(client, run_id, VOUCHERS).json['id']
    corrected = [dict(VOUCHERS[0]),
                 {'voucher_id': 'V-03', 'leg_id': 'M-02', 'truck': 'T-02', 'station': '001',
                  'at': '2026-11-20T09:25:00+08:00', 'count': 2, 'outcome': 'delivered'}]
    second = publish_settlement(client, run_id, corrected).json['id']
    restored = client.post('/api/settlement/restore',
                           json={'run': run_id, 'settlement': first, 'expected': run_id})
    assert restored.json == {'id': first, 'created': False, 'changed': True}
    assert client.get('/api/settlement').json['active'] == first
    # 回切不删新版本，两个版本都还在。
    assert client.get(f'/api/settlement/{second}').status_code == 200


def test_failed_publish_keeps_previous_conclusion_untouched(client, run_id):
    first = publish_settlement(client, run_id, VOUCHERS)
    bad = [dict(VOUCHERS[0], truck='T-02'), VOUCHERS[1]]
    response = publish_settlement(client, run_id, bad)
    assert response.status_code == 422 and response.json['code'] == 'VOUCHER_TRUCK_MISMATCH'
    listing = client.get('/api/settlement').json
    assert listing['active'] == first.json['id'] and len(listing['history']) == 1


def test_original_waybill_and_sources_remain_archived(client, run_id, app):
    publish_settlement(client, run_id, VOUCHERS)
    db = app.extensions['shift_store']
    snapshot = db.load(run_id)
    assert [leg['id'] for leg in snapshot['dispatch']['legs']] == ['M-01', 'M-02']
    sources = db.sources(run_id)
    assert set(sources) == {'events', 'capacity', 'shift'}
    # 结算证据里附原派车单，逐趟可追溯。
    active = client.get('/api/settlement').json['active']
    detail = client.get(f'/api/settlement/{active}').json['settlement']
    assert [leg['id'] for leg in detail['waybill']] == ['M-01', 'M-02']


def test_stale_run_after_replan_rejects_settlement_publish(client, run_id):
    publish_settlement(client, run_id, VOUCHERS)
    new_run = client.post('/api/dispatch/publish', json={'expected': run_id, 'legs': []}).json['id']
    response = publish_settlement(client, run_id, VOUCHERS)
    assert response.status_code == 409 and response.json['code'] == 'STALE_SETTLEMENT'
    # 新班次还没有结算，旧班次的结算结论仍可读取。
    assert client.get('/api/settlement').json['run'] == new_run
    assert client.get('/api/settlement').json['active'] is None


@pytest.mark.parametrize('mutation, code', [
    (lambda v: dict(v, truck='T-02'), 'VOUCHER_TRUCK_MISMATCH'),
    (lambda v: dict(v, station='004'), 'VOUCHER_STATION_MISMATCH'),
    (lambda v: dict(v, count=2), 'VOUCHER_COUNT_MISMATCH'),
    (lambda v: dict(v, at='2026-11-20T07:00:00+08:00'), 'VOUCHER_TIME_INVALID'),
])
def test_voucher_against_waybill_mismatches_are_rejected(client, run_id, mutation, code):
    vouchers = [mutation(VOUCHERS[0]), VOUCHERS[1]]
    response = client.post('/api/settlement/preview', json={'run': run_id, 'vouchers': vouchers})
    assert response.status_code == 422 and response.json['code'] == code


def test_returned_voucher_station_must_be_loading_origin(client, run_id):
    vouchers = [VOUCHERS[0], dict(VOUCHERS[1], station='001')]
    response = client.post('/api/settlement/preview', json={'run': run_id, 'vouchers': vouchers})
    assert response.json['code'] == 'VOUCHER_STATION_MISMATCH'
    assert response.json['details']['expected'] == '003'


def test_every_leg_needs_one_voucher(client, run_id):
    response = client.post('/api/settlement/preview', json={'run': run_id, 'vouchers': VOUCHERS[:1]})
    assert response.json['code'] == 'LEGS_NOT_SETTLED'
    assert response.json['details']['legs'] == ['M-02']
    duplicate = [*VOUCHERS, dict(VOUCHERS[0], voucher_id='V-09')]
    again = client.post('/api/settlement/preview', json={'run': run_id, 'vouchers': duplicate})
    assert again.json['code'] == 'CONFLICTING_VOUCHER'


def test_empty_and_malformed_voucher_payloads_are_rejected(client, run_id):
    assert client.post('/api/settlement/publish', json={'run': run_id, 'vouchers': []}).json['code'] == 'VOUCHER_SHAPE'
    bad_shape = client.post('/api/settlement/publish',
                            json={'run': run_id, 'vouchers': [{'voucher_id': 'X'}]})
    assert bad_shape.json['code'] == 'VOUCHER_SHAPE'


def test_unknown_settlement_and_run_return_structured_404(client, run_id):
    missing = client.get('/api/settlement/deadbeef')
    assert missing.status_code == 404 and missing.json['code'] == 'SETTLE_NOT_FOUND'
    bad_run = client.post('/api/settlement/preview',
                          json={'run': 'no-such-run', 'vouchers': VOUCHERS})
    assert bad_run.status_code == 404 and bad_run.json['code'] == 'RUN_NOT_FOUND'


def test_settlement_identity_is_content_addressed_and_internally_consistent(client, run_id):
    data = client.post('/api/settlement/preview',
                       json={'run': run_id, 'vouchers': VOUCHERS}).json['settlement']
    # 编号由班次、凭证和规则版本共同决定，同输入必然同编号。
    again = client.post('/api/settlement/preview',
                        json={'run': run_id, 'vouchers': list(reversed(VOUCHERS))}).json['settlement']
    assert data['id'] == again['id']
    assert data['run_id'] == run_id
    assert data['inventory']['ok'] is True
    # 全网车辆守恒：可借+暂存+在骑 = 33 辆，在途为零。
    fleet = sum(row['borrowable'] + row['held'] for row in data['stations']) + data['inventory']['road_end']
    assert fleet == 33


def test_delivered_voucher_station_must_be_destination(client, run_id):
    vouchers = [dict(VOUCHERS[0], station='001'), VOUCHERS[1]]
    response = client.post('/api/settlement/preview', json={'run': run_id, 'vouchers': vouchers})
    assert response.json['code'] == 'VOUCHER_STATION_MISMATCH'
    assert response.json['details']['expected'] == '002'


def test_storage_failure_rolls_back_and_keeps_old_conclusion(client, run_id, app):
    first = publish_settlement(client, run_id, VOUCHERS).json['id']
    corrected = [dict(VOUCHERS[0]),
                 {'voucher_id': 'V-03', 'leg_id': 'M-02', 'truck': 'T-02', 'station': '001',
                  'at': '2026-11-20T09:25:00+08:00', 'count': 2, 'outcome': 'delivered'}]
    db = app.extensions['shift_store']
    with db.connect() as conn:
        conn.execute("CREATE TRIGGER stop_settlement BEFORE INSERT ON settlement_runs "
                     "BEGIN SELECT RAISE(ABORT, 'test'); END")
    response = publish_settlement(client, run_id, corrected)
    assert (response.status_code, response.json['code']) == (503, 'STORE_UNAVAILABLE')
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM settlement_runs').fetchone() == (1,)
        assert conn.execute('SELECT count(*) FROM settlement_vouchers').fetchone() == (2,)
        assert conn.execute('SELECT settlement_id FROM settlement_active').fetchone() == (first,)
    assert client.get('/api/settlement').json['active'] == first


def test_evidence_download_has_integrity_and_keeps_waybill_and_vouchers(client, run_id):
    from app.manifest import canonical, digest
    publish_settlement(client, run_id, VOUCHERS)
    settlement_id = client.get('/api/settlement').json['active']
    response = client.get(f'/api/settlement/{settlement_id}/evidence.json')
    assert response.status_code == 200
    assert response.headers['Content-Disposition'].startswith('attachment;')
    body = response.json
    assert body['evidence_sha256'] == digest(canonical(body['evidence']).encode())
    evidence = body['evidence']
    assert [leg['id'] for leg in evidence['waybill']] == ['M-01', 'M-02']
    assert {row['voucher_id'] for row in evidence['vouchers']} == {'V-01', 'V-02'}
    assert evidence['conclusion']['bikes_returned'] == 2
