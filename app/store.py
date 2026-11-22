import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import DomainError
from .manifest import canonical

SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    run_id TEXT NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL,
    raw BLOB NOT NULL,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS active (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    run_id TEXT NOT NULL REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS settlement_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    generated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_vouchers (
    run_id TEXT NOT NULL REFERENCES runs(id),
    voucher_id TEXT NOT NULL,
    leg_id TEXT NOT NULL,
    raw TEXT NOT NULL,
    PRIMARY KEY (run_id, voucher_id)
);
CREATE TABLE IF NOT EXISTS settlement_active (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    settlement_id TEXT NOT NULL REFERENCES settlement_runs(id)
);
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA foreign_keys = ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def active(self):
        with self.connect() as db:
            row = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
        return row[0] if row else None

    def load(self, run_id=None):
        with self.connect() as db:
            if run_id is None:
                row = db.execute('SELECT payload FROM runs JOIN active ON runs.id = active.run_id').fetchone()
            else:
                row = db.execute('SELECT payload FROM runs WHERE id = ?', (run_id,)).fetchone()
        if row is None:
            raise DomainError('RUN_NOT_FOUND', 404)
        return json.loads(row[0])

    def sources(self, run_id):
        with self.connect() as db:
            return dict(db.execute('SELECT name, raw FROM sources WHERE run_id = ?', (run_id,)))

    def history(self):
        sql = '''SELECT runs.id, generated_at, json_extract(payload, '$.shift.name'),
                        json_extract(payload, '$.manifest.inputs.events.sha256'),
                        json_extract(payload, '$.dispatch.comparison.delta'), active.run_id IS NOT NULL
                 FROM runs LEFT JOIN active ON runs.id = active.run_id
                 ORDER BY generated_at DESC, runs.id'''
        keys = ('id', 'generated_at', 'shift', 'source_sha256', 'delta', 'active')
        with self.connect() as db:
            return [dict(zip(keys, row)) for row in db.execute(sql)]

    def publish(self, snapshot, sources, expected):
        manifest = snapshot['manifest']
        run_id = manifest['id']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
            active = row[0] if row else None
            existing = db.execute('SELECT payload FROM runs WHERE id = ?', (run_id,)).fetchone()
            if existing is not None:
                stored = json.loads(existing[0])
                comparable = dict(snapshot, manifest=dict(manifest))
                for key in ('generated_at', 'elapsed_ms'):
                    comparable['manifest'][key] = stored['manifest'][key]
                if stored != comparable:
                    raise DomainError('REPLAY_DRIFT', 409)
                if active == run_id:
                    return False
            if active != expected:
                raise DomainError('STALE_RUN', 409, current=active)
            if existing is None:
                db.execute('INSERT INTO runs VALUES (?, ?, ?)',
                           (run_id, manifest['generated_at'], canonical(snapshot)))
                db.executemany('INSERT INTO sources VALUES (?, ?, ?)',
                               [(run_id, name, raw) for name, raw in sources.items()])
            db.execute('INSERT INTO active VALUES (1, ?) ON CONFLICT(slot) DO UPDATE SET run_id = excluded.run_id',
                       (run_id,))
            return existing is None

    def load_settlement(self, settlement_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM settlement_runs WHERE id = ?', (settlement_id,)).fetchone()
        if row is None:
            raise DomainError('SETTLE_NOT_FOUND', 404)
        return json.loads(row[0])

    def active_settlement(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT settlement_id FROM settlement_active WHERE run_id = ?', (run_id,)).fetchone()
        return row[0] if row else None

    def settlement_history(self, run_id):
        sql = '''SELECT s.id, s.generated_at, a.settlement_id IS NOT NULL,
                        json_extract(s.payload, '$.conclusion.delivered_legs'),
                        json_extract(s.payload, '$.conclusion.returned_legs'),
                        json_extract(s.payload, '$.conclusion.bikes_landed'),
                        json_extract(s.payload, '$.conclusion.bikes_returned')
                 FROM settlement_runs s LEFT JOIN settlement_active a
                 ON a.settlement_id = s.id AND a.run_id = s.run_id
                 WHERE s.run_id = ? ORDER BY s.generated_at DESC, s.id'''
        keys = ('id', 'generated_at', 'active', 'delivered_legs', 'returned_legs',
                'bikes_landed', 'bikes_returned')
        with self.connect() as db:
            return [dict(zip(keys, row)) for row in db.execute(sql, (run_id,))]

    def vouchers(self, run_id):
        with self.connect() as db:
            rows = db.execute('SELECT raw FROM settlement_vouchers WHERE run_id = ? ORDER BY leg_id, voucher_id',
                              (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def publish_settlement(self, conclusion, vouchers, expected_run):
        """事务内发布结算结论；任一步失败整体回滚，旧版结论与凭证全部保留。"""
        settlement_id = conclusion['id']
        run_id = conclusion['run_id']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
            current = current[0] if current else None
            if current != expected_run or run_id != expected_run:
                raise DomainError('STALE_SETTLEMENT', 409, current=current)
            existing = db.execute('SELECT 1 FROM settlement_runs WHERE id = ?', (settlement_id,)).fetchone()
            if existing is not None:
                # 同一现场凭证重试：幂等返回，绝不重复结算。
                db.execute('INSERT INTO settlement_active VALUES (?, ?) '
                           'ON CONFLICT(run_id) DO UPDATE SET settlement_id = excluded.settlement_id',
                           (run_id, settlement_id))
                return False
            db.execute('INSERT INTO settlement_runs (id, run_id, generated_at, payload) VALUES (?, ?, ?, ?)',
                       (settlement_id, run_id, conclusion['generated_at'], canonical(conclusion)))
            db.executemany('INSERT OR IGNORE INTO settlement_vouchers (run_id, voucher_id, leg_id, raw) '
                           'VALUES (?, ?, ?, ?)',
                           [(run_id, row['voucher_id'], row['leg_id'], canonical(row)) for row in vouchers])
            db.execute('INSERT INTO settlement_active (run_id, settlement_id) VALUES (?, ?) '
                       'ON CONFLICT(run_id) DO UPDATE SET settlement_id = excluded.settlement_id',
                       (run_id, settlement_id))
            return True

    def restore_settlement(self, run_id, settlement_id, expected_run):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
            current = current[0] if current else None
            if current != expected_run or run_id != expected_run:
                raise DomainError('STALE_SETTLEMENT', 409, current=current)
            row = db.execute('SELECT 1 FROM settlement_runs WHERE id = ? AND run_id = ?',
                             (settlement_id, run_id)).fetchone()
            if row is None:
                raise DomainError('SETTLE_NOT_FOUND', 404)
            db.execute('INSERT INTO settlement_active VALUES (?, ?) '
                       'ON CONFLICT(run_id) DO UPDATE SET settlement_id = excluded.settlement_id',
                       (run_id, settlement_id))
        return {'id': settlement_id, 'created': False, 'changed': True}
