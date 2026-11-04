"""ORACLE-RUNTIME-2 R1: B2 query planning on the target schema.

QueryPlanner's request serialization, alias grouping, sharing of identical requests across FOOD_IDs,
policy and permission checks, version stamps and budget decisions run unchanged; only its storage
primitives read and write target tables. Foods, approved aliases and tracking targets are read as the
SQLite system of record delivered them (B7/Shadow); no recognition or approval rule exists here.
preview() only reads: no plan rows, no reservation, no HTTP. commit() stores immutable, insert-only
plan history, idempotent by request key, inside one fenced runtime transaction.
"""
import json
from .food_service import FoodError, dump
from .query_planner import PLANNER_VERSION, QueryPlanner
from .tracking_service import instant


class OracleQueryPlanner(QueryPlanner):
    def __init__(self, execution, rss_interval=1800):
        if type(rss_interval) is not int or rss_interval <= 0:
            raise ValueError('Invalid RSS interval')
        self.storage, self.tracking, self.execution = None, execution, execution
        self.rss_interval = rss_interval
        self.quota = execution.quota
        execution.planner = self

    def _validate(self, db, plan, now):
        return self.execution._validate(db, plan, now)

    def _reader(self):
        return self.execution.reader()

    def _writer(self):
        return self.execution.transaction()

    def _target(self, t, target_id):
        row = self.execution._target(t, target_id)
        if row is None:
            raise FoodError('추적 대상을 찾을 수 없습니다.', 404)
        row['execution_enabled'] = False
        return row

    @staticmethod
    def _target_ids(t):
        return [r['target_id'] for r in t.rows('SELECT target_id FROM tracking_targets ORDER BY target_id')]

    @staticmethod
    def _active_aliases(t, food_id, scope):
        return t.rows("""SELECT * FROM food_aliases WHERE food_id=:b_f AND scope=:b_s AND status='ACTIVE'
            ORDER BY normalized_form,alias_id""", 'food_aliases', b_f=food_id, b_s=scope)

    @staticmethod
    def _command(t, key):
        return t.one('SELECT * FROM planner_commands WHERE request_key=:b_k', 'planner_commands', b_k=key)

    @staticmethod
    def _put_command(t, key, fingerprint, result, created_at):
        t.insert('planner_commands', dict(request_key=key, payload_hash=fingerprint, result_json=dump(result), created_at=created_at))

    @staticmethod
    def _put_plan(t, p):
        t.insert_absent('planner_queries', dict(query_id=p['query_id'], source_id=p['source_id'], account_scope=p['account_scope'],
            query_kind=p['query_kind'], canonical_json=dump(p['canonical_query']), spec_hash=p['spec_hash'], planner_version=PLANNER_VERSION,
            created_at=p['created_at']))
        t.insert_absent('planner_plans', dict(plan_id=p['plan_id'], query_id=p['query_id'], due_slot=p['due_slot'], mapping_hash=p['mapping_hash'],
            account_fingerprint=p['account_fingerprint'], payload_json=dump(p), created_at=p['created_at']))
        for m in p['targets']:
            t.insert_absent('planner_query_targets', dict(query_id=p['query_id'], target_id=m['target_id'], generation=m['generation'],
                food_revision=m['food_revision'], mapping_json=dump(m)))
            t.insert_absent('planner_plan_targets', dict(plan_id=p['plan_id'], target_id=m['target_id'], generation=m['generation'],
                food_revision=m['food_revision'], due_at=m['due_at'], mapping_json=dump(m)))

    def plan(self, plan_id):
        """Stored plan with its current validation and budget (read-only)."""
        with self.execution.reader() as t:
            p = self.execution._plan(t, plan_id)
            now = instant(self.tracking.clock())
            return {'plan': p, 'current_validation': self._validate(t, p, now), 'current_budget': self.quota.assess(t, p, now)}

    def history(self, target_id=None):
        with self.execution.reader() as t:
            if target_id:
                rows = t.rows('''SELECT p.payload_json FROM planner_plans p JOIN planner_plan_targets m ON m.plan_id=p.plan_id
                    WHERE m.target_id=:b_t ORDER BY p.created_at,p.plan_id''', b_t=target_id)
            else:
                rows = t.rows('SELECT payload_json FROM planner_plans ORDER BY created_at,plan_id')
            return [json.loads(r['payload_json']) for r in rows]
