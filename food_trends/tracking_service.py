"""Food-based target state and deterministic scheduling; deliberately no HTTP clients."""
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from .food_service import FoodService, FoodError, identity, text, dump
from .entity_types import FOOD_KINDS
from .tracking_access import Account, SOURCES, DEFAULT_INTERVALS, permitted_operation


def instant(value):
    try:
        value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError()
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise FoodError('시각에는 시간대가 필요합니다.') from None


def stamp(value):
    return instant(value).isoformat(timespec='microseconds')


class TrackingService:
    def __init__(self, storage, accounts=None, clock=None):
        self.storage, self.foods = storage, FoodService(storage)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        accounts = [Account(s) for s in SOURCES] if accounts is None else accounts
        self.accounts = {(a.source_id, a.account_scope): a for a in accounts}
        if len(self.accounts) != len(accounts):
            raise ValueError('Duplicate account scope')

    # Storage primitives. The Oracle runtime (oracle_tracking) overrides only these; the decisions below stay shared.

    def _reader(self):
        return self.storage.reader()

    @contextmanager
    def _writer(self):
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    @staticmethod
    def _lock_target(db, target_id):
        pass  # BEGIN IMMEDIATE already serializes every SQLite writer

    @staticmethod
    def _resolved_candidate(db, food_id):
        return db.execute("SELECT 1 FROM food_candidates WHERE resolved_food_id=? AND status='RESOLVED' LIMIT 1", (food_id,)).fetchone() is not None

    @staticmethod
    def _target_ids(db, food_id):
        sql, args = ('SELECT target_id FROM tracking_targets', ())
        if food_id is not None:
            sql += ' WHERE food_id=?'; args = (food_id,)
        return [r[0] for r in db.execute(sql + ' ORDER BY created_at,target_id', args)]

    @staticmethod
    def _history(db, target_id):
        return [dict(r) for r in db.execute('SELECT rowid AS event_sequence,* FROM tracking_events WHERE target_id=? ORDER BY rowid', (target_id,))]

    @staticmethod
    def _put_event(db, row):
        db.execute('INSERT INTO tracking_events VALUES (?,?,?,?,?,?,?,?,?)', tuple(row.values()))

    @staticmethod
    def _command(db, key):
        row = db.execute('SELECT * FROM tracking_commands WHERE request_key=?', (key,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _put_command(db, key, operation, fingerprint, created_at):
        db.execute('INSERT INTO tracking_commands VALUES (?,?,?,?,?)', (key, operation, fingerprint, '{}', created_at))

    @staticmethod
    def _command_result(db, key, result):
        db.execute('UPDATE tracking_commands SET result_json=? WHERE request_key=?', (dump(result), key))

    @staticmethod
    def _has_alias(db, food_id, scope):
        return db.execute("SELECT 1 FROM food_aliases WHERE food_id=? AND scope=? AND status='ACTIVE'", (food_id, scope)).fetchone() is not None

    @staticmethod
    def _target_for(db, food_id, source, scope):
        row = db.execute('SELECT * FROM tracking_targets WHERE food_id=? AND source_id=? AND account_scope=?', (food_id, source, scope)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _reregister(db, target, food_revision, now):
        db.execute("UPDATE tracking_targets SET status='PENDING',generation=generation+1,revision=revision+1,food_revision=?,next_due_at=NULL,updated_at=? WHERE target_id=?", (food_revision, now, target['target_id']))

    @staticmethod
    def _put_target(db, row):
        db.execute('''INSERT INTO tracking_targets VALUES (?,?,?,?,?,'PENDING',1,?,NULL,NULL,?,?,?,?,1,?,?)''', tuple(row.values()))

    @staticmethod
    def _put_slot(db, row):
        db.execute('INSERT INTO tracking_slots VALUES (?,?,?,?,?,?)', tuple(row.values()))

    @staticmethod
    def _advance_due(db, target, next_due, now):
        db.execute('UPDATE tracking_targets SET next_due_at=?,revision=revision+1,updated_at=? WHERE target_id=?', (next_due, now, target['target_id']))

    @staticmethod
    def _set_state(db, target, status, next_due, food_revision, interval, now):
        db.execute('''UPDATE tracking_targets SET status=?,next_due_at=?,food_revision=?,interval_seconds=?,
            generation=generation+1,revision=revision+1,updated_at=? WHERE target_id=?''',
                   (status, next_due, food_revision, interval, now, target['target_id']))

    # Decisions

    def account(self, source, scope):
        account = self.accounts.get((source, scope))
        if account is None:
            raise FoodError('등록된 출처·계정 범위가 아닙니다.')
        return account

    def _approved_food(self, db, food_id):
        food = self.foods._active(db, food_id)
        if food['entity_kind'] not in FOOD_KINDS or not self._resolved_candidate(db, food_id):
            raise FoodError('승인·식별된 음식만 추적할 수 있습니다.', 409)
        return food

    def blockers(self, db, target):
        try:
            food = self._approved_food(db, target['food_id'])
            reasons = [] if food['revision'] == target['food_revision'] else ['STALE_FOOD_REVISION']
        except FoodError:
            reasons = ['FOOD_INACTIVE_OR_UNAPPROVED']
        try:
            reasons += self.account(target['source_id'], target['account_scope']).blockers()
        except FoodError:
            reasons += ['ACCOUNT_UNAVAILABLE']
        return reasons

    def _target(self, db, target_id):
        row = self.foods._row(db, 'tracking_targets', 'target_id', target_id)
        row['blockers'] = self.blockers(db, row)
        row['effective_status'] = 'BLOCKED' if row['status'] == 'ACTIVE' and row['blockers'] else row['status']
        row['planning_eligible'] = row['effective_status'] == 'ACTIVE' and not row['blockers']
        row['execution_enabled'] = False
        return row

    def target(self, target_id):
        with self._reader() as db:
            return self._target(db, target_id)

    def list(self, food_id=None):
        with self._reader() as db:
            return [self._target(db, target_id) for target_id in self._target_ids(db, food_id)]

    def due(self, at=None):
        now = instant(at or self.clock())
        return [r for r in self.list() if r['planning_eligible'] and r['next_due_at'] and instant(r['next_due_at']) <= now]

    def history(self, target_id):
        with self._reader() as db:
            return self._history(db, target_id)

    def valid_mapping(self, db, target_id, generation, food_revision):
        target = self._target(db, target_id)
        return (target['planning_eligible'] and target['generation'] == generation and target['food_revision'] == food_revision)

    def _event(self, db, row, action, before, ctx):
        after = self.foods._row(db, 'tracking_targets', 'target_id', row['target_id'])
        self._put_event(db, dict(event_id=identity('TEVT'), target_id=row['target_id'], action=action, actor_ref=ctx['actor_ref'],
                                 reason=ctx['reason'], occurred_at=stamp(self.clock()), before_json=dump(before) if before else None,
                                 after_json=dump(after), request_key=ctx['request_key']))

    def command(self, operation, body):
        common = {'request_key', 'actor_ref', 'reason'}
        fields = {'register': {'food_id', 'source_id', 'account_scope', 'alias_scope', 'interval_seconds', 'candidate_id',
                               'reregister', 'expected_revision'},
                  'activate': {'target_id', 'expected_revision', 'approved'},
                  'pause': {'target_id', 'expected_revision'}, 'resume': {'target_id', 'expected_revision', 'approved'},
                  'stop': {'target_id', 'expected_revision'}, 'interval': {'target_id', 'expected_revision', 'interval_seconds'},
                  'refresh': {'target_id', 'expected_revision'}, 'schedule': {'target_id', 'expected_revision'}}
        if operation not in fields or not isinstance(body, dict) or set(body) - (fields[operation] | common):
            raise FoodError('추적 명령의 입력을 확인하세요.')
        ctx = dict(body, request_key=text(body.get('request_key'), 128),
                   actor_ref=text(body.get('actor_ref'), 100), reason=text(body.get('reason'), 1000))
        fingerprint = hashlib.sha256(dump(body).encode()).hexdigest()
        with self._writer() as db:
            old = self._command(db, ctx['request_key'])
            if old:
                if old['operation'] != operation or old['payload_hash'] != fingerprint:
                    raise FoodError('같은 요청 키의 입력이 다릅니다.', 409)
                return json.loads(old['result_json'])
            self._put_command(db, ctx['request_key'], operation, fingerprint, stamp(self.clock()))
            result = self._register(db, ctx) if operation == 'register' else self._change(db, operation, ctx)
            self._command_result(db, ctx['request_key'], result)
            return result

    def _interval(self, source, value):
        minimum = 21600 if source == 'youtube' else 86400 if source == 'naver_datalab' else 1
        if type(value) is not int or not minimum <= value <= 366 * 86400:
            raise FoodError('기존 출처의 최소 간격과 최대 366일 범위를 지켜야 합니다.')
        return value

    def _register(self, db, ctx):
        food = self._approved_food(db, ctx.get('food_id'))
        source, scope = ctx.get('source_id'), ctx.get('account_scope', 'local-default')
        permitted_operation(source, 'tracking')
        account = self.account(source, scope)
        interval = self._interval(source, ctx.get('interval_seconds', account.interval_seconds or DEFAULT_INTERVALS[source]))
        alias_scope = self.foods._scope(ctx.get('alias_scope', food['default_scope']))
        if not self._has_alias(db, food['food_id'], alias_scope):
            raise FoodError('해당 범위에 승인된 정확 별칭이 없습니다.', 409)
        previous = self._target_for(db, food['food_id'], source, scope)
        if previous:
            if previous['status'] == 'STOPPED' and ctx.get('reregister') is True:
                self.foods._revision(previous, ctx.get('expected_revision'))
                self._reregister(db, previous, food['revision'], stamp(self.clock()))
                self._event(db, previous, 'REREGISTERED', previous, ctx)
            return self._target(db, previous['target_id'])
        origin, method = 'user', 'MANUAL'
        if ctx.get('candidate_id') is not None:
            candidate = self.foods._row(db, 'food_candidates', 'candidate_id', ctx['candidate_id'])
            if candidate['status'] != 'RESOLVED' or candidate['resolved_food_id'] != food['food_id']:
                raise FoodError('식별된 후보와 음식 ID가 일치하지 않습니다.', 409)
            origin, method = candidate['origin'], 'DISCOVERY' if candidate['source_code'] != 'user_input' else 'MANUAL'
        target_id, now = identity('TARGET'), stamp(self.clock())
        self._put_target(db, dict(target_id=target_id, food_id=food['food_id'], source_id=source, account_scope=scope, alias_scope=alias_scope,
                                  interval_seconds=interval, origin=origin, registration_method=method, candidate_id=ctx.get('candidate_id'),
                                  food_revision=food['revision'], created_at=now, updated_at=now))
        self._event(db, {'target_id': target_id}, 'REGISTERED', None, ctx)
        return self._target(db, target_id)

    def _change(self, db, action, ctx):
        self._lock_target(db, ctx.get('target_id'))
        row = self.foods._row(db, 'tracking_targets', 'target_id', ctx.get('target_id'))
        self.foods._revision(row, ctx.get('expected_revision'))
        now = instant(self.clock())
        if action == 'schedule':
            target = self._target(db, row['target_id'])
            if not target['planning_eligible'] or not row['next_due_at'] or instant(row['next_due_at']) > now:
                raise FoodError('현재 예정된 활성 대상이 아닙니다.', 409)
            slot_id = identity('SLOT')
            self._put_slot(db, dict(slot_id=slot_id, target_id=row['target_id'], generation=row['generation'], food_revision=row['food_revision'],
                                    due_slot=row['next_due_at'], created_at=stamp(now)))
            due = instant(row['next_due_at'])
            steps = int((now - due).total_seconds() // row['interval_seconds']) + 1
            next_due = stamp(due + timedelta(seconds=steps * row['interval_seconds']))
            self._advance_due(db, row, next_due, stamp(now))
            self._event(db, row, 'SLOT_SCHEDULED', row, ctx)
            return {'slot_id': slot_id, 'due_slot': row['next_due_at'], 'target': self._target(db, row['target_id']), 'http_job_created': False}
        status, next_due, food_revision, interval = row['status'], row['next_due_at'], row['food_revision'], row['interval_seconds']
        if action in ('activate', 'resume'):
            allowed = ('PENDING', 'BLOCKED') if action == 'activate' else ('PAUSED', 'BLOCKED')
            if status not in allowed or ctx.get('approved') is not True:
                raise FoodError('현재 상태에서 승인된 활성화·재개를 할 수 없습니다.', 409)
            # Refresh is separate: activation cannot silently consume changed aliases.
            reasons = self.blockers(db, row)
            status, next_due = ('BLOCKED', None) if reasons else ('ACTIVE', stamp(now))
        elif action == 'pause':
            if status != 'ACTIVE':
                raise FoodError('활성 대상만 일시 중단할 수 있습니다.', 409)
            status, next_due = 'PAUSED', None
        elif action == 'stop':
            if status == 'STOPPED':
                return self._target(db, row['target_id'])
            status, next_due = 'STOPPED', None
        elif action == 'interval':
            interval = self._interval(row['source_id'], ctx.get('interval_seconds'))
            next_due = stamp(now + timedelta(seconds=interval)) if status == 'ACTIVE' else None
        elif action == 'refresh':
            food_revision = self._approved_food(db, row['food_id'])['revision']
            next_due = stamp(now) if status == 'ACTIVE' else None
        self._set_state(db, row, status, next_due, food_revision, interval, stamp(now))
        self._event(db, row, action.upper(), row, ctx)
        return self._target(db, row['target_id'])
