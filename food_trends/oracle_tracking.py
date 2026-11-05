"""ORACLE-CUTOVER-PREP-1: food-based tracking targets on the target schema (Oracle, or its SQLite stand-in).

TrackingService's decisions run unchanged: approved and identified foods only, account and policy gates,
per-source interval limits, explicit approval to activate or resume, generations and food revisions that
invalidate older plans, a replayable result per request key and one immutable event per change. Only its
storage primitives read and write target tables (v7 tracking_commands and tracking_events, tracking_targets,
tracking_slots) inside the Oracle runtime's ownership-guarded transactions. Tracking commands are serialized
among themselves by an exclusive lock on tracking_commands, and a command locks its target row before reading
it, as the scheduler's enqueue does, so a due-slot advance never interleaves with a pause or an interval change.
"""
from contextlib import contextmanager
from .food_service import dump
from .oracle_foods import OracleFoodService
from .tracking_service import TrackingService, instant, stamp


class OracleTrackingService(TrackingService):
    def __init__(self, execution, accounts=None, clock=None):
        super().__init__(None, list(execution.accounts.values()) if accounts is None else accounts, clock or execution.clock)
        self.foods, self.execution = OracleFoodService(execution), execution

    def _reader(self):
        return self.execution.reader()

    @contextmanager
    def _writer(self):
        with self.execution.transaction() as t:
            if t.oracle:
                t.run('LOCK TABLE tracking_commands IN EXCLUSIVE MODE')
            yield t

    @staticmethod
    def _lock_target(t, target_id):
        if t.oracle and isinstance(target_id, str):
            t.run('SELECT target_id FROM tracking_targets WHERE target_id=:b_t FOR UPDATE', b_t=target_id).fetchall()

    @staticmethod
    def _resolved_candidate(t, food_id):
        return bool(t.scalar("SELECT count(*) AS c FROM food_candidates WHERE resolved_food_id=:b_f AND status='RESOLVED'", b_f=food_id))

    @staticmethod
    def _target_ids(t, food_id):
        if food_id is None:
            return [r['target_id'] for r in t.rows('SELECT target_id FROM tracking_targets ORDER BY created_at,target_id')]
        return [r['target_id'] for r in t.rows('SELECT target_id FROM tracking_targets WHERE food_id=:b_f ORDER BY created_at,target_id',
                                               b_f=food_id)]

    @staticmethod
    def _history(t, target_id):
        return t.rows('SELECT * FROM tracking_events WHERE target_id=:b_t ORDER BY event_sequence', 'tracking_events', b_t=target_id)

    @staticmethod
    def _put_event(t, row):
        # Append order is authoritative (the SQLite rowid); the tracking_commands lock serializes every appender here.
        sequence = int(t.scalar('SELECT coalesce(max(event_sequence),0)+1 AS n FROM tracking_events'))
        t.insert('tracking_events', dict(row, event_sequence=sequence))

    @staticmethod
    def _command(t, key):
        return t.one('SELECT * FROM tracking_commands WHERE request_key=:b_k', 'tracking_commands', b_k=key)

    @staticmethod
    def _put_command(t, key, operation, fingerprint, created_at):
        t.insert('tracking_commands', dict(request_key=key, operation=operation, payload_hash=fingerprint, result_json='{}', created_at=created_at))

    @staticmethod
    def _command_result(t, key, result):
        t.update('tracking_commands', {'request_key': key}, result_json=dump(result))

    @staticmethod
    def _has_alias(t, food_id, scope):
        return bool(t.scalar("SELECT count(*) AS c FROM food_aliases WHERE food_id=:b_f AND scope=:b_s AND status='ACTIVE'", b_f=food_id, b_s=scope))

    @staticmethod
    def _target_for(t, food_id, source, scope):
        return t.one('SELECT * FROM tracking_targets WHERE food_id=:b_f AND source_id=:b_s AND account_scope=:b_a', 'tracking_targets',
                     b_f=food_id, b_s=source, b_a=scope)

    @staticmethod
    def _reregister(t, target, food_revision, now):
        t.run("""UPDATE tracking_targets SET status='PENDING',generation=generation+1,revision=revision+1,food_revision=:b_r,
            next_due_at=NULL,updated_at=:b_now WHERE target_id=:b_t""", b_r=food_revision, b_now=instant(now), b_t=target['target_id'])

    @staticmethod
    def _put_target(t, row):
        t.insert('tracking_targets', dict(row, status='PENDING', generation=1, next_due_at=None, last_success_at=None, revision=1))

    @staticmethod
    def _put_slot(t, row):
        # The target's slot carries its lifecycle; one scheduled by command has no plan or job yet. due_slot is the normalized UTC
        # stamp the runtime's enqueue and plan validation use.
        t.insert('tracking_slots', dict(slot_id=row['slot_id'], target_id=row['target_id'], plan_id=None, job_id=None, generation=row['generation'],
                                        food_revision=row['food_revision'], due_slot=stamp(row['due_slot']), status='SCHEDULED',
                                        created_at=row['created_at'], claimed_at=None, completed_at=None, cancellation_reason=None))

    @staticmethod
    def _advance_due(t, target, next_due, now):
        t.run('UPDATE tracking_targets SET next_due_at=:b_next,revision=revision+1,updated_at=:b_now WHERE target_id=:b_t',
              b_next=instant(next_due), b_now=instant(now), b_t=target['target_id'])

    @staticmethod
    def _set_state(t, target, status, next_due, food_revision, interval, now):
        t.run("""UPDATE tracking_targets SET status=:b_s,next_due_at=:b_next,food_revision=:b_r,interval_seconds=:b_i,
            generation=generation+1,revision=revision+1,updated_at=:b_now WHERE target_id=:b_t""",
              b_s=status, b_next=instant(next_due) if next_due else None, b_r=food_revision, b_i=interval, b_now=instant(now),
              b_t=target['target_id'])
