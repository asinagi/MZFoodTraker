"""ORACLE-RUNTIME-1 S2: the B3 execution contract on the target schema (Oracle, or its SQLite stand-in).

Same states, Outbox, lease/fencing, call ledger, budget and result contract as ExecutionService, in
portable SQL over target transactions. Plans, foods and targets reach the target through B7 migration
or Shadow. Planning (oracle_planner) and due-slot scheduling run here too: tracking_slots follows the
SQLite slot contract (one row per target, generation and due slot) and adds its lifecycle.

Locking: a claim locks the job row with SELECT ... FOR UPDATE SKIP LOCKED on Oracle (BEGIN IMMEDIATE
serializes the stand-in) and re-reads the lease under that lock. Every fenced write locks the job row,
then the lease row, in that order, so a stale worker can neither write after a takeover commits nor
deadlock with a claimer. No transaction is open while a provider request is in flight.

Mock by default. Live settings are accepted only from the live assembly (oracle_live) with a passed readiness
check, and a live job still needs, at enqueue and again before its first request, live enabled for its source,
a live account, the source transferred to this runtime as its only writer (quota_accounts EXCLUSIVE, so its external
usage is known) and no active block; an Oracle target labelled production is refused. Construction
checks the target schema version and never runs DDL. The legacy call tables (youtube_calls, naver_calls,
instagram_searches) and quota_events do not exist on the target: budget usage is the workflow charges
recorded here, and reservation transitions are kept on quota_reservations only.
"""
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from .contracts import CONTRACT_VERSION, SOURCES, policy_for, require_operation, validate_metadata
from .entity_types import FOOD_KINDS
from .execution import CONTENT_ENDPOINTS, ExecutionError, ExecutionSettings, LostLease, RequestSession
from .execution_sources import (SourceAdapter, NaverExecutor, lookup_checkpoint, media_checkpoint, media_request, parse_lookup,
                                parse_media, parse_search, parse_videos, public_videos, remote_usage_check_needed, search_checkpoint,
                                usage_fingerprint, videos_checkpoint)
from .food_service import FoodError, dump, identity, text
from .ingestion import item_key
from .instagram_analysis import extract_hashtags
from .query_planner import PLANNER_VERSION, digest
from .quota import COST_VERSION, QuotaManager, daily_window
from .target_db import OracleTarget, OracleUnavailable, SQLiteTarget, TargetError, UniqueViolation, canonical_json, convert, iso, logical, utc
from .target_repository import policy
from .target_schema import BY_NAME, TARGET_SCHEMA_VERSION
from .tracking_service import instant, stamp

ACTIVE_STATES = ('READY', 'RETRY_WAIT', 'CLAIMED', 'RUNNING')


def _states(values):
    return '(%s)' % ','.join("'%s'" % v for v in values)


def _text(value):
    """Oracle LOBs are read; IS JSON columns may arrive already decoded and are re-serialized canonically."""
    if hasattr(value, 'read'):
        return value.read()
    return canonical_json(value) if isinstance(value, (dict, list)) else value


class _Tx:
    """Named binds over one target transaction. Datetimes bind as UTC instants (TSTZ on Oracle)."""

    def __init__(self, cursor):
        self.cursor, self.oracle = cursor, cursor.dialect == 'oracle'

    def run(self, sql, **binds):
        values, ts = {}, []
        for name, value in binds.items():
            if isinstance(value, datetime):
                values[name] = utc(value) if self.oracle else iso(value)
                if self.oracle:
                    ts.append(name)
            else:
                values[name] = value
        return self.cursor.execute(sql, values, ts_binds=tuple(ts))

    def rows(self, sql, table=None, **binds):
        result = self.run(sql, **binds)
        if self.oracle:
            names = [d[0].lower() for d in result.description]
            rows = [dict(zip(names, (_text(v) for v in r))) for r in result.fetchall()]
        else:
            rows = [dict(r) for r in result.fetchall()]
        return [logical(table, r) for r in rows] if table else rows

    def one(self, sql, table=None, **binds):
        rows = self.rows(sql, table, **binds)
        return rows[0] if rows else None

    def scalar(self, sql, **binds):
        row = self.one(sql, **binds)
        return next(iter(row.values())) if row else None

    def _binds(self, table, row):
        spec = BY_NAME[table]
        return {'b_' + k: convert(spec.column(k), v, 'oracle' if self.oracle else 'sqlite') for k, v in row.items()}

    def insert(self, table, row):
        self.run('INSERT INTO %s (%s) VALUES (%s)' % (table, ','.join(row), ','.join(':b_' + k for k in row)), **self._binds(table, row))

    def insert_absent(self, table, row, key=None):
        """INSERT OR IGNORE: 1 when inserted, 0 when the key exists. A concurrent insert of the same key counts as
        present (the failed statement alone is rolled back; the transaction continues)."""
        spec, dialect = BY_NAME[table], 'oracle' if self.oracle else 'sqlite'
        key = key or spec.key
        where = ' AND '.join('%s=:k_%s' % (k, k) for k in key)
        if self.scalar('SELECT count(*) AS c FROM %s WHERE %s' % (table, where),
                       **{'k_' + k: convert(spec.column(k), row[k], dialect) for k in key}):
            return 0
        try:
            self.insert(table, row)
        except Exception as exc:
            mapped = exc if isinstance(exc, TargetError) else (OracleTarget if self.oracle else SQLiteTarget).map_error(exc)
            if isinstance(mapped, UniqueViolation):
                return 0
            raise mapped from None
        return 1

    def update(self, table, where, **changes):
        binds = self._binds(table, changes)
        binds.update({'w_' + k: v for k, v in self._binds(table, where).items()})
        return self.run('UPDATE %s SET %s WHERE %s' % (table, ','.join('%s=:b_%s' % (k, k) for k in changes),
                                                        ' AND '.join('%s=:w_b_%s' % (k, k) for k in where)), **binds).rowcount


class OracleQuota(QuotaManager):
    """B2 budget accounting on the target: workflow reservations plus the imported legacy usage ledger (R4).

    Legacy usage is counted exactly as QuotaManager._observed counts the SQLite call tables, from
    quota_usage_ledger rows for the same account scope and window, so assess() equals the SQLite result.
    usage() adds per line where the usage came from and how certain it is; nothing claims a provider-confirmed debit.
    """
    LEGACY_FRESH = timedelta(minutes=15)

    def __init__(self, execution):
        super().__init__(None, execution, execution._validate)
        self.execution = execution

    def _ledger(self, db, scope, resource):
        return db.rows('''SELECT * FROM quota_usage_ledger WHERE account_scope=:b_a AND quota_type=:b_r''', 'quota_usage_ledger',
                       b_a=scope, b_r=resource)

    def _observed(self, db, plan, resource, now):
        scope, observed, known = plan['account_scope'], 0, {}
        if resource.startswith('youtube.') or resource == 'naver.calls':
            zone = 'America/Los_Angeles' if resource.startswith('youtube.') else 'Asia/Seoul'
            start = instant(daily_window(now, zone)['start'])
            observed = sum(r['usage'] for r in self._ledger(db, scope, resource) if instant(r['window_start']) == start)
        elif resource == 'instagram.unique_tags':
            for r in self._ledger(db, scope, resource):
                if instant(r['window_end']) > instant(now):
                    known.setdefault(r['distinct_key'], r['window_end'])
            observed = len(known)
        return observed, known

    def coverage(self, db, source, scope, now):
        """How well usage outside this runtime is known for one account scope."""
        row = db.one('SELECT * FROM quota_accounts WHERE source_id=:b_s AND account_scope=:b_a', 'quota_accounts', b_s=source, b_a=scope)
        if row is None or row['usage_status'] == 'UNVERIFIED':
            return 'UNVERIFIED'
        if row['usage_status'] == 'EXCLUSIVE':
            return 'RUNTIME_EXCLUSIVE'
        if row['legacy_observed_through'] is None or instant(row['legacy_observed_through']) < instant(now) - self.LEGACY_FRESH:
            return 'STALE'
        return 'IMPORTED_LEGACY'

    def usage(self, db, plan, now, exclude=None):
        """assess() plus, per line, where the usage came from and how certain it is (assess() itself stays the SQLite contract)."""
        budget = self.assess(db, plan, now, exclude)
        coverage = self.coverage(db, plan['source_id'], plan['account_scope'], now)
        for line in budget['lines']:
            rows = [r for r in self._ledger(db, plan['account_scope'], line['resource'])
                    if instant(r['window_end']) > instant(now) and instant(r['window_start']) <= instant(now)]
            legacy, _ = self._observed(db, plan, line['resource'], now)
            uncertain = any(r['confidence'] != 'COUNTED' for r in rows)
            window = {'start': line['window_start'], 'end': line['window_end'], 'timezone': line['reset_timezone']}
            line.update(legacy_usage=legacy, workflow_usage=line['observed_usage'] - legacy, provider_window=window, local_window=window,
                        provider_timezone=line['reset_timezone'], legacy_evidence=sorted({r['evidence'] for r in rows}),
                        usage_confidence=coverage + ('_WITH_UNCERTAIN' if uncertain and coverage == 'IMPORTED_LEGACY' else ''),
                        source_of_truth='target:quota_usage_ledger+quota_reservations')
        budget['usage_coverage'] = coverage
        budget['usage_verified'] = coverage in ('IMPORTED_LEGACY', 'RUNTIME_EXCLUSIVE')
        return budget

    def _lock_account(self, db, plan):
        """Serialize usage assessment and reservation per account scope (two plans cannot both take the last unit)."""
        row = dict(source_id=plan['source_id'], account_scope=plan['account_scope'], usage_status='UNVERIFIED', legacy_observed_through=None,
                   evidence_ref=None, updated_at=self.execution.now())
        db.insert_absent('quota_accounts', row)
        if db.oracle:
            db.run('SELECT source_id FROM quota_accounts WHERE source_id=:b_s AND account_scope=:b_a FOR UPDATE',
                   b_s=plan['source_id'], b_a=plan['account_scope']).fetchall()

    def _recorded(self, db, source, scope, resource):
        rows = db.rows('''SELECT c.reservation_id,c.resource_code,c.cost,c.distinct_key,c.window_start,c.window_end,r.state,r.expires_at
            FROM quota_charges c JOIN quota_reservations r ON r.reservation_id=c.reservation_id JOIN planner_queries q ON q.query_id=r.query_id
            WHERE q.source_id=:b_s AND q.account_scope=:b_a AND c.resource_code=:b_r AND r.state IN ('RESERVED','ATTEMPTED')''',
                       b_s=source, b_a=scope, b_r=resource)
        return [dict(reservation_id=r['reservation_id'], resource=r['resource_code'], cost=int(r['cost']), distinct_key=r['distinct_key'],
                     window_start=stamp(utc_value(r['window_start'])), window_end=stamp(utc_value(r['window_end'])), state=r['state'],
                     expires_at=stamp(utc_value(r['expires_at']))) for r in rows]

    def _charges(self, db, reservation, lines):
        db.run('DELETE FROM quota_charges WHERE reservation_id=:b_r', b_r=reservation)
        for c in lines:
            db.insert('quota_charges', dict(reservation_id=reservation, resource_code=c['resource'], cost=c['cost'], distinct_key=c['distinct_key'],
                                            window_start=c['window_start'], window_end=c['window_end'], reset_timezone=c['reset_timezone'],
                                            local_limit=c['local_limit'], provider_limit=c['provider_limit']))

    def _reservation(self, db, **where):
        clause = ' AND '.join('%s=:b_%s' % (k, k) for k in where)
        return db.one('SELECT * FROM quota_reservations WHERE ' + clause, 'quota_reservations', **{'b_' + k: v for k, v in where.items()})

    def reserve(self, plan_id, attempt_key, attempt_no=1, ttl_seconds=120, *, connection):
        key = text(attempt_key, 128)
        if type(attempt_no) is not int or attempt_no < 1 or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise FoodError('예약 번호·기한을 확인하세요.')
        db, now = connection, self.execution.now()
        old = self._reservation(db, attempt_key=key)
        if old:
            if old['plan_id'] != plan_id or old['attempt_no'] != attempt_no:
                raise FoodError('같은 예약 키의 요청이 다릅니다.', 409)
            return old
        plan = self.execution._plan(db, plan_id)
        self._lock_account(db, plan)
        validation = self.validator(db, plan, now)
        if not validation['valid']:
            raise FoodError('현재 대상·세대·정책에 유효한 계획이 아닙니다.', 409)
        previous = db.one('''SELECT * FROM quota_reservations WHERE query_id=:b_q AND due_slot=:b_d AND attempt_no=
            (SELECT max(attempt_no) FROM quota_reservations WHERE query_id=:b_q AND due_slot=:b_d)''', 'quota_reservations',
                          b_q=plan['query_id'], b_d=plan['due_slot'])
        if attempt_no != (previous['attempt_no'] + 1 if previous else 1):
            raise FoodError('이 요청 슬롯의 예약 시도 번호가 중복되거나 순서가 다릅니다.', 409)
        if previous and previous['state'] == 'RESERVED' and instant(previous['expires_at']) > now:
            raise FoodError('이미 유효한 예약이 있습니다.', 409)
        budget = self.assess(db, plan, now)
        if budget['status'] != 'READY_LOCAL':
            raise FoodError('예약 가능한 로컬 예산이 부족합니다.', 409)
        expiry = min([now + timedelta(seconds=ttl_seconds)] + [instant(c['window_end']) for c in budget['lines']])
        reservation = identity('RESERVATION')
        db.insert('quota_reservations', dict(reservation_id=reservation, plan_id=plan_id, query_id=plan['query_id'], due_slot=plan['due_slot'],
                                             attempt_key=key, attempt_no=attempt_no, state='RESERVED', reserved_at=now, expires_at=expiry,
                                             attempted_at=None, target_snapshot_json=dump(validation['eligible_targets'])))
        self._charges(db, reservation, budget['lines'])
        return self._reservation(db, reservation_id=reservation)

    def mark_attempted(self, reservation_id, *, connection):
        db, now = connection, self.execution.now()
        row = self._reservation(db, reservation_id=reservation_id)
        if row is None:
            raise FoodError('예약을 찾을 수 없습니다.', 404)
        if row['state'] == 'ATTEMPTED':
            return row
        if row['state'] != 'RESERVED' or instant(row['expires_at']) <= now:
            raise FoodError('예약이 해제되었거나 만료됐습니다.', 409)
        plan = self.execution._plan(db, row['plan_id'])
        self._lock_account(db, plan)
        validation = self.validator(db, plan, now)
        frozen = {t['target_id'] for t in json.loads(row['target_snapshot_json'])}
        eligible = [t for t in validation['eligible_targets'] if t['target_id'] in frozen]
        if not validation['valid'] or not eligible and plan['targets']:
            raise FoodError('예약 이후 대상·정책이 변경됐습니다.', 409)
        budget = self.assess(db, plan, now, exclude=reservation_id)
        if budget['status'] != 'READY_LOCAL':
            raise FoodError('실행 시도 전 예산 재검사 실패', 409)
        self._charges(db, reservation_id, budget['lines'])
        db.update('quota_reservations', {'reservation_id': reservation_id}, state='ATTEMPTED', attempted_at=now, target_snapshot_json=dump(eligible))
        return self._reservation(db, reservation_id=reservation_id)


def utc_value(value):
    """A fetched timestamp: Oracle returns naive UTC datetimes, the stand-in ISO text."""
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return instant(value)


class OracleExecution:
    """ExecutionService's method contract (enqueue/claim/renew/begin/finish/fail/status) on the target schema."""
    session_class = None  # set below to OracleRequestSession

    def __init__(self, repo, accounts, clock=None, settings=None, ownership=None, require_verified_usage=False, live_readiness=None):
        from .runtime_ownership import RuntimeOwnership
        self.repo, self.db = repo, repo.db
        self.accounts = accounts if isinstance(accounts, dict) else {(a.source_id, a.account_scope): a for a in accounts}
        self.clock = clock or repo.clock
        self.settings = settings or ExecutionSettings()
        if self.settings.live_enabled and not (live_readiness or {}).get('ready'):
            raise ValueError('Oracle live execution needs the live assembly and a passed readiness check (oracle_live)')
        target_settings = getattr(self.db, 'settings', None)
        if target_settings is not None and target_settings.environment == 'production':
            raise OracleUnavailable('oracle_production_runtime_disabled')
        version = repo.schema_version()
        if version != TARGET_SCHEMA_VERSION:
            # Never migrates here: the operator runs --target-migrate explicitly.
            raise TargetError('target_schema_version_mismatch')
        self.quota = OracleQuota(self)
        self.planner = None  # an OracleQueryPlanner attaches itself (schedule() needs one)
        self.claim_locked = lambda job_id: None  # test hook: runs while the claim holds the job lock
        self.fault = lambda stage: None  # test hook: failure injection inside write transactions
        # R3: every write transaction runs under this process's RUNTIME_TEST ownership lease (joined lazily).
        self.ownership = ownership or RuntimeOwnership(repo)
        self.participant_id = identity('RUNTIME')
        self.lease, self._renew_at = None, None
        # R4: live execution always needs verified external usage; mock runs can opt in to test the rule.
        self.require_verified_usage = require_verified_usage

    def now(self):
        return instant(self.clock())

    def _owned(self):
        moment = self.ownership.now()
        if self.lease is None:
            self.lease = self.ownership.acquire('RUNTIME_TEST', self.participant_id)
            self._renew_at = moment + self.ownership.ttl / 2
        elif moment >= self._renew_at:
            from .runtime_ownership import OwnershipRefused
            try:
                self.ownership.renew(self.lease)
            except OwnershipRefused as exc:
                if exc.code != 'ownership_expired':
                    raise
                # The lease lapsed while this process was idle. It is never revived: the process joins again with a new
                # lease, which is refused while another mode owns the target; job leases and fencing still guard every job.
                self.participant_id = identity('RUNTIME')
                self.lease = self.ownership.acquire('RUNTIME_TEST', self.participant_id)
            self._renew_at = moment + self.ownership.ttl / 2
        return self.lease

    def release(self):
        """Leave the runtime mode (the target returns to IDLE when no runtime lease or unfinished job remains)."""
        if self.lease is not None:
            self.ownership.release(self.lease)
            self.lease = None

    def account(self, source, scope):
        account = self.accounts.get((source, scope))
        if account is None:
            raise FoodError('등록된 출처·계정 범위가 아닙니다.')
        return account

    @contextmanager
    def transaction(self):
        """A runtime write transaction, guarded by the ownership lease (refused while Shadow owns the target)."""
        lease = self._owned()
        with self.db.transaction() as cursor:
            t = _Tx(cursor)
            self.ownership.guard(t, lease)
            yield t

    @contextmanager
    def reader(self):
        """A transaction that only reads (status, preview, history)."""
        with self.db.transaction() as cursor:
            yield _Tx(cursor)

    def schedule(self, options=None, mode='mock'):
        """ExecutionService.schedule on the target: commit due plans with the Oracle planner, then enqueue them."""
        if self.planner is None:
            raise FoodError('Oracle 계획기가 연결되지 않았습니다.', 409)
        plans = self.planner.commit(identity('SCHEDULE'), options=options)
        ids = [p['plan_id'] for p in plans['plans'] if p['source_id'] != 'google_trends_rss' and p['decision'] != 'NOT_DUE']
        return self.enqueue(ids, mode) if ids else []

    # Reads

    def _job(self, t, job_id):
        row = t.one('SELECT * FROM execution_jobs WHERE job_id=:b_j', 'execution_jobs', b_j=job_id)
        if row is None:
            raise FoodError('실행 작업을 찾을 수 없습니다.', 404)
        row['mode'] = row.pop('exec_mode')
        return row

    def _plan(self, t, plan_id):
        row = t.one('SELECT payload_json FROM planner_plans WHERE plan_id=:b_p', b_p=plan_id)
        if row is None:
            raise FoodError('계획을 찾을 수 없습니다.', 404)
        return json.loads(row['payload_json'])

    def _lease(self, t, job_id):
        return t.one('SELECT * FROM execution_leases WHERE resource_key=:b_j', 'execution_leases', b_j=job_id)

    def _target(self, t, target_id):
        """TrackingService._target over the target tables; missing rows are never eligible."""
        row = t.one('SELECT * FROM tracking_targets WHERE target_id=:b_t', 'tracking_targets', b_t=target_id)
        if row is None:
            return None
        food = t.one('SELECT * FROM food_entities WHERE food_id=:b_f', 'food_entities', b_f=row['food_id'])
        approved = t.scalar("SELECT count(*) AS c FROM food_candidates WHERE resolved_food_id=:b_f AND status='RESOLVED'", b_f=row['food_id'])
        if not food or food['status'] != 'ACTIVE' or food['entity_kind'] not in FOOD_KINDS or not approved:
            reasons = ['FOOD_INACTIVE_OR_UNAPPROVED']
        else:
            reasons = [] if food['revision'] == row['food_revision'] else ['STALE_FOOD_REVISION']
        try:
            reasons += self.account(row['source_id'], row['account_scope']).blockers()
        except FoodError:
            reasons += ['ACCOUNT_UNAVAILABLE']
        row['blockers'] = reasons
        row['effective_status'] = 'BLOCKED' if row['status'] == 'ACTIVE' and reasons else row['status']
        row['planning_eligible'] = row['effective_status'] == 'ACTIVE' and not reasons
        return row

    def _validate(self, t, plan, now):
        """QueryPlanner._validate on target data: same reasons and the same due-slot rule."""
        source, eligible, reasons = plan['source_id'], [], []
        if (plan['planner_version'] != PLANNER_VERSION or plan['contract']['policy_version'] != policy_for(source)['version'] or
                plan['adapter_version'] != SOURCES[source]['adapter_version'] or plan['contract_version'] != CONTRACT_VERSION or
                plan['cost_version'] != COST_VERSION):
            reasons.append('POLICY_OR_PLANNER_VERSION_CHANGED')
        if source == 'google_trends_rss':
            reasons.append('RSS_OWNED_BY_EXISTING_DISCOVERY_SCHEDULER')
        else:
            try:
                account = self.account(source, plan['account_scope'])
                reasons += account.blockers()
                if digest(account.public()) != plan['account_fingerprint']:
                    reasons.append('ACCOUNT_CONFIG_CHANGED')
            except FoodError:
                reasons.append('ACCOUNT_UNAVAILABLE')
        for m in plan['targets']:
            target = self._target(t, m['target_id'])
            if (not target or not target['planning_eligible'] or target['generation'] != m['generation'] or
                    target['food_revision'] != m['food_revision'] or instant(m['due_at']) > instant(now)):
                continue
            due = instant(m['due_at'])
            current = target['next_due_at'] is not None and instant(target['next_due_at']) == due
            if not current and not t.scalar('SELECT count(*) AS c FROM tracking_slots WHERE target_id=:b_t AND generation=:b_g AND due_slot=:b_d',
                                            b_t=m['target_id'], b_g=m['generation'], b_d=stamp(due)):
                continue
            eligible.append(m)
        if plan['targets'] and not eligible:
            reasons.append('NO_CURRENT_TARGET_MAPPING')
        return {'valid': not reasons, 'eligible_targets': eligible if not reasons else [], 'reasons': reasons,
                'execution_enabled': False, 'lease_fencing_required_from_B3': True}

    def _gate(self, t, plan, mode):
        if plan['source_id'] == 'google_trends_rss':
            return ['RSS_OWNED_BY_EXISTING_DISCOVERY_SCHEDULER']
        account = self.account(plan['source_id'], plan['account_scope'])
        reasons = list(account.blockers())
        if account.mode != mode:
            reasons.append('EXECUTION_MODE_MISMATCH')
        coverage = self.quota.coverage(t, plan['source_id'], plan['account_scope'], self.now())
        if mode == 'live' and (not self.settings.live_enabled or plan['source_id'] not in self.settings.exclusive_live_sources):
            reasons.append('LIVE_DISABLED_OR_LEGACY_WRITER_NOT_TRANSFERRED')
        if mode == 'live' and coverage != 'RUNTIME_EXCLUSIVE':
            reasons.append('LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED')  # the legacy collector may still use this account
        if (mode == 'live' or self.require_verified_usage) and coverage not in ('IMPORTED_LEGACY', 'RUNTIME_EXCLUSIVE'):
            reasons.append('EXTERNAL_USAGE_UNVERIFIED')
        block = t.one('SELECT * FROM execution_blocks WHERE source_id=:b_s AND account_scope=:b_a', 'execution_blocks',
                      b_s=plan['source_id'], b_a=plan['account_scope'])
        if block and (block['until_at'] is None or instant(block['until_at']) > self.now()):
            reasons.append('ACCOUNT_BLOCKED_' + block['error_kind'])
        return reasons

    def _eligible(self, t, job, started_only=False):
        result = []
        for row in t.rows('SELECT * FROM execution_mappings WHERE job_id=:b_j ORDER BY plan_id,target_id', 'execution_mappings', b_j=job['job_id']):
            if started_only and not row['started']:
                continue
            validation = self._validate(t, self._plan(t, row['plan_id']), self.now())
            if any(m['target_id'] == row['target_id'] and m['generation'] == row['generation'] and
                   m['food_revision'] == row['food_revision'] for m in validation['eligible_targets']):
                result.append(json.loads(row['mapping_json']))
        return result

    # Commands

    def enqueue(self, plan_ids, mode='mock'):
        if mode not in ('mock', 'live') or not isinstance(plan_ids, list) or not 1 <= len(plan_ids) <= 200:
            raise FoodError('실행 모드와 계획 목록을 확인하세요.')
        result = []
        with self.transaction() as t:
            for plan_id in dict.fromkeys(plan_ids):
                p = self._plan(t, plan_id)
                if t.oracle:  # one enqueuer per query, then per target (sorted); the stand-in is already serialized
                    t.run('SELECT query_id FROM planner_queries WHERE query_id=:b_q FOR UPDATE', b_q=p['query_id']).fetchall()
                    for target_id in sorted({m['target_id'] for m in p['targets']}):
                        t.run('SELECT target_id FROM tracking_targets WHERE target_id=:b_t FOR UPDATE', b_t=target_id).fetchall()
                reasons = self._gate(t, p, mode)
                validation = self._validate(t, p, self.now())
                if reasons or not validation['valid']:
                    result.append({'plan_id': plan_id, 'status': 'BLOCKED', 'reasons': reasons + validation['reasons']})
                    continue
                existing = [self._job(t, r['job_id']) for r in t.rows('''SELECT job_id FROM execution_jobs WHERE query_id=:b_q AND due_slot=:b_d
                    AND exec_mode=:b_m ORDER BY execution_generation''', b_q=p['query_id'], b_d=p['due_slot'], b_m=mode)]
                uncovered = [m for m in validation['eligible_targets'] if not t.scalar('''SELECT count(*) AS c FROM execution_mappings m
                    JOIN execution_jobs j ON j.job_id=m.job_id WHERE j.query_id=:b_q AND j.due_slot=:b_d AND j.exec_mode=:b_m
                    AND m.target_id=:b_t AND m.generation=:b_g AND m.food_revision=:b_r''', b_q=p['query_id'], b_d=p['due_slot'], b_m=mode,
                    b_t=m['target_id'], b_g=m['generation'], b_r=m['food_revision'])]
                if existing and not uncovered:
                    result.append(existing[-1])
                    continue
                # Before claim, later plans can join one shared transport job.
                reusable = next((j for j in existing if j['status'] == 'READY' and j['attempt_count'] == 0 and not any(
                    t.scalar('SELECT count(*) AS c FROM execution_mappings WHERE job_id=:b_j AND target_id=:b_t', b_j=j['job_id'], b_t=m['target_id'])
                    for m in uncovered)), None)
                if reusable:
                    job_id = reusable['job_id']
                else:
                    epoch = max((j['execution_generation'] for j in existing), default=0) + 1
                    job_id, run_id, now = identity('JOB'), identity('RUN'), self.now()
                    t.insert('execution_jobs', dict(job_id=job_id, job_key=digest([p['query_id'], p['due_slot'], mode, epoch]), query_id=p['query_id'],
                        plan_id=plan_id, source_id=p['source_id'], account_scope=p['account_scope'], due_slot=p['due_slot'],
                        execution_generation=epoch, exec_mode=mode, status='READY', attempt_count=0, max_attempts=self.settings.max_attempts,
                        created_at=now, run_id=run_id, remote_duplicate_possible=0))
                    t.insert('execution_outbox', dict(event_id=identity('OUTBOX'), job_id=job_id, created_at=now, delivery_count=0))
                    t.insert('execution_runs', dict(run_id=run_id, job_id=job_id, source_code=p['source_id'], origin='mock' if mode == 'mock' else 'live_api',
                        execution_status='READY', returned_count=0, accepted_count=0))
                for m in uncovered:
                    if not t.scalar('SELECT count(*) AS c FROM execution_mappings WHERE job_id=:b_j AND target_id=:b_t', b_j=job_id, b_t=m['target_id']):
                        t.insert('execution_mappings', dict(job_id=job_id, plan_id=plan_id, target_id=m['target_id'], generation=m['generation'],
                                                            food_revision=m['food_revision'], mapping_json=dump(m), started=0))
                    target = self._target(t, m['target_id'])
                    if target['next_due_at'] is not None and instant(target['next_due_at']) == instant(m['due_at']):
                        self._schedule_slot(t, target, plan_id, job_id)
                self.fault('before_enqueue_commit')
                result.append(self._job(t, job_id))
        return result

    def _schedule_slot(self, t, target, plan_id, job_id):
        """TrackingService 'schedule' in the enqueue transaction: the slot, its job and the next due time commit together."""
        now = self.now()
        due = instant(target['next_due_at'])
        if not target['planning_eligible'] or due > now:
            raise FoodError('현재 예정된 활성 대상이 아닙니다.', 409)
        t.insert('tracking_slots', dict(slot_id=identity('SLOT'), target_id=target['target_id'], plan_id=plan_id, job_id=job_id,
                                        generation=target['generation'], food_revision=target['food_revision'], due_slot=stamp(due),
                                        status='SCHEDULED', created_at=now, claimed_at=None, completed_at=None, cancellation_reason=None))
        steps = int((now - due).total_seconds() // target['interval_seconds']) + 1
        t.run('UPDATE tracking_targets SET next_due_at=:b_next,revision=revision+1,updated_at=:b_now WHERE target_id=:b_t',
              b_next=due + timedelta(seconds=steps * target['interval_seconds']), b_now=now, b_t=target['target_id'])
        self.fault('after_schedule_slot')

    def _finish_slots(self, t, job, status, error, eligible):
        """Slots end with their job: completed for targets whose data the run kept, otherwise failed or cancelled with a reason."""
        kept = {m['target_id'] for m in eligible}
        for slot in t.rows("SELECT * FROM tracking_slots WHERE job_id=:b_j AND status IN ('SCHEDULED','CLAIMED')", 'tracking_slots', b_j=job['job_id']):
            target = self._target(t, slot['target_id'])
            if target is None:
                reason = 'TARGET_MISSING'
            elif target['status'] != 'ACTIVE':
                reason = 'TARGET_' + target['status']
            elif target['generation'] != slot['generation']:
                reason = 'STALE_GENERATION'
            elif target['blockers']:
                reason = target['blockers'][0]
            else:
                reason = error
            if status in ('SUCCEEDED', 'PARTIAL', 'LIMITED') and slot['target_id'] in kept:
                state, reason = 'COMPLETED', None
            elif status == 'FAILED' and reason == error:
                state = 'FAILED'
            else:
                state = 'CANCELLED'
            t.update('tracking_slots', {'slot_id': slot['slot_id']}, status=state, completed_at=self.now(),
                     cancellation_reason=None if state == 'COMPLETED' else reason or status)

    def _fence(self, t, job_id, owner, token):
        if t.oracle:  # job row, then lease row: the same order as claim, so no deadlock
            t.run('SELECT job_id FROM execution_jobs WHERE job_id=:b_j FOR UPDATE', b_j=job_id).fetchall()
            t.run('SELECT resource_key FROM execution_leases WHERE resource_key=:b_j FOR UPDATE', b_j=job_id).fetchall()
        lease = self._lease(t, job_id)
        if not lease or lease['owner_id'] != owner or lease['fencing_generation'] != token or instant(lease['expires_at']) <= self.now():
            raise LostLease()
        job = self._job(t, job_id)
        if job['status'] not in ('CLAIMED', 'RUNNING'):
            raise LostLease()
        return job

    def _put_lease(self, t, job_id, **values):
        if not t.update('execution_leases', {'resource_key': job_id}, **values):
            t.insert('execution_leases', dict(resource_key=job_id, **values))

    def claim(self, owner, job_id=None, mode=None):
        owner = text(owner, 100)
        now = self.now()
        with self.transaction() as t:
            sql = f'''SELECT j.job_id FROM execution_jobs j WHERE j.status IN {_states(ACTIVE_STATES)}
                AND (j.next_retry_at IS NULL OR j.next_retry_at<=:b_now) AND EXISTS (SELECT 1 FROM execution_outbox o WHERE o.job_id=j.job_id)
                AND NOT EXISTS (SELECT 1 FROM execution_leases l WHERE l.resource_key=j.job_id AND l.expires_at>:b_now)'''
            binds = {'b_now': now}
            if job_id is not None:
                sql += ' AND j.job_id=:b_j'
                binds['b_j'] = job_id
            if mode is not None:
                sql += ' AND j.exec_mode=:b_m'
                binds['b_m'] = mode
            for candidate in [r['job_id'] for r in t.rows(sql + ' ORDER BY j.created_at,j.job_id', **binds)]:
                if t.oracle and not t.rows('SELECT job_id FROM execution_jobs WHERE job_id=:b_j FOR UPDATE SKIP LOCKED', b_j=candidate):
                    continue  # another worker holds this job
                # Re-read under the lock: a concurrent claim may have committed after the candidate scan.
                job, old = self._job(t, candidate), self._lease(t, candidate)
                if (job['status'] not in ACTIVE_STATES or job['next_retry_at'] and instant(job['next_retry_at']) > now or
                        old and instant(old['expires_at']) > now):
                    continue
                self.claim_locked(candidate)
                # An interrupted send remains charged; no claim of remote exactly-once. This precedes the
                # attempt limit so a final interrupted attempt is not finalised as a certain failure.
                unknown = t.run("UPDATE execution_calls SET status='UNKNOWN',error_kind='INTERRUPTED_AFTER_SEND' WHERE job_id=:b_j AND status='SENT'",
                                b_j=candidate).rowcount
                if unknown:
                    t.update('execution_jobs', {'job_id': candidate}, remote_duplicate_possible=1)
                if job['attempt_count'] >= job['max_attempts']:
                    self._terminal(t, job, 'FAILED', 'MAX_ATTEMPTS')
                    continue
                token = old['fencing_generation'] + 1 if old else 1
                # Recover reservations left before the attempted/send boundary.
                t.run("UPDATE quota_reservations SET state='RELEASED' WHERE attempt_key LIKE :b_k AND state='RESERVED'", b_k=candidate + ':%')
                self._put_lease(t, candidate, owner_id=owner, fencing_generation=token, acquired_at=now, renewed_at=now,
                                expires_at=now + timedelta(seconds=self.settings.lease_seconds))
                t.run('''UPDATE execution_jobs SET status='CLAIMED',attempt_count=attempt_count+1,started_at=coalesce(started_at,:b_now),
                    next_retry_at=NULL WHERE job_id=:b_j''', b_now=now, b_j=candidate)
                t.run('UPDATE execution_outbox SET delivery_count=delivery_count+1 WHERE job_id=:b_j', b_j=candidate)
                t.run('''UPDATE tracking_slots SET status='CLAIMED',claimed_at=coalesce(claimed_at,:b_now)
                    WHERE job_id=:b_j AND status IN ('SCHEDULED','CLAIMED')''', b_now=now, b_j=candidate)
                return {'job': self._job(t, candidate), 'owner_id': owner, 'fencing_generation': token}
        return None

    def renew(self, claim):
        with self.transaction() as t:
            self._fence(t, claim['job']['job_id'], claim['owner_id'], claim['fencing_generation'])
            self._put_lease(t, claim['job']['job_id'], renewed_at=self.now(), expires_at=self.now() + timedelta(seconds=self.settings.lease_seconds))

    def _result_contract(self, t, job, status, error):
        """Execution status and data coverage are independent; committed pages are never discarded."""
        calls = t.one(f'''SELECT count(*) AS n,coalesce(sum(CASE WHEN endpoint IN ('recent_media','videos','naver_search') THEN returned_count ELSE 0 END),0) AS returned,
            coalesce(sum(accepted_count),0) AS accepted,coalesce(sum(CASE WHEN endpoint IN {CONTENT_ENDPOINTS} THEN 1 ELSE 0 END),0) AS pages
            FROM execution_calls WHERE job_id=:b_j AND status='SUCCEEDED' ''', b_j=job['job_id'])
        returned, accepted, pages = int(calls['returned']), int(calls['accepted']), int(calls['pages'])
        coverage = ('COMPLETE' if status == 'SUCCEEDED' else 'LIMITED' if status == 'LIMITED' else 'PARTIAL' if pages else 'NONE')
        steps = [r['step_key'] for r in t.rows(f"""SELECT step_key FROM execution_calls WHERE job_id=:b_j AND status='SUCCEEDED'
            AND endpoint IN {CONTENT_ENDPOINTS} ORDER BY call_id""", b_j=job['job_id'])]
        checkpoint = t.one('SELECT state_json FROM execution_checkpoints WHERE job_id=:b_j', b_j=job['job_id'])
        state = json.loads(checkpoint['state_json']) if checkpoint else {}
        eligible = self._eligible(t, job, started_only=True) if coverage != 'NONE' else []
        plan = self._plan(t, job['plan_id'])
        contract = dict(run_id=job['run_id'], job_id=job['job_id'], source_code=job['source_id'], query_id=job['query_id'],
            active_target_ids=[m['target_id'] for m in eligible], origin='mock' if job['mode'] == 'mock' else 'live_api',
            contract_version=CONTRACT_VERSION, policy_version=plan['policy_version'], observed_at=stamp(self.now()),
            request_context=plan['contract']['request_conditions'], execution_status=status,
            coverage={'COMPLETE': 'complete_request', 'PARTIAL': 'partial_request', 'LIMITED': 'limited_request', 'NONE': 'no_committed_data'}[coverage],
            result_coverage=coverage, committed_pages=pages, committed_steps=steps,
            resume_state={k: state[k] for k in ('phase', 'page', 'more', 'limited', 'done') if k in state} or None,
            remote_duplicate_possible=bool(job['remote_duplicate_possible']),
            returned_count=returned, accepted_count=accepted, error_kind=error,
            allowed_result_reference=None if job['source_id'] == 'naver_datalab' or coverage == 'NONE' else
                {'storage': 'target_source_contents', 'run_id': job['run_id'], 'receipt_job_id': job['job_id'],
                 'receipt_table': 'execution_item_receipts', 'exclude_expired': True},
            returned_count_basis='content_response_rows_or_transient_naver_groups',
            accepted_count_basis='unique_persisted_items_or_transient_naver_groups',
            mapping_semantics='search_provenance_only', remote_debit_confirmed=False)
        validate_metadata(contract)
        return contract, eligible

    def _terminal(self, t, job, status, error=None):
        job = self._job(t, job['job_id'])  # re-read flags set earlier in this transaction
        contract, eligible = self._result_contract(t, job, status, error)
        now = self.now()
        t.update('execution_jobs', {'job_id': job['job_id']}, status=status, error_kind=error, finished_at=now, next_retry_at=None)
        run = t.one('SELECT observed_at FROM execution_runs WHERE run_id=:b_r', 'execution_runs', b_r=job['run_id']) or {}
        t.update('execution_runs', {'run_id': job['run_id']}, execution_status=status, result_coverage=contract['result_coverage'],
                 committed_pages=contract['committed_pages'], observed_at=run.get('observed_at') or now,
                 returned_count=contract['returned_count'], accepted_count=contract['accepted_count'], result_contract_json=dump(contract))
        # Partial data keeps its target lineage; only a complete success advances last_success_at.
        for m in eligible:
            if not t.scalar('SELECT count(*) AS c FROM execution_results WHERE run_id=:b_r AND target_id=:b_t', b_r=job['run_id'], b_t=m['target_id']):
                t.insert('execution_results', dict(run_id=job['run_id'], target_id=m['target_id'], generation=m['generation'],
                                                   food_revision=m['food_revision'], created_at=now))
            if status == 'SUCCEEDED':
                t.run('UPDATE tracking_targets SET last_success_at=:b_now WHERE target_id=:b_t AND generation=:b_g AND food_revision=:b_r',
                      b_now=now, b_t=m['target_id'], b_g=m['generation'], b_r=m['food_revision'])
        t.update('execution_outbox', {'job_id': job['job_id']}, delivered_at=now)
        self._finish_slots(t, job, status, error, eligible)

    def begin(self, claim):
        with self.transaction() as t:
            job = self._fence(t, claim['job']['job_id'], claim['owner_id'], claim['fencing_generation'])
            plan = self._plan(t, job['plan_id'])
            reasons = self._gate(t, plan, job['mode'])
            validation = self._validate(t, plan, self.now())
            # A shared job may outlive its original plan's targets; all member plans are checked.
            eligible = self._eligible(t, job)
            reasons += [r for r in validation['reasons'] if r != 'NO_CURRENT_TARGET_MAPPING']
            if reasons or plan['targets'] and not eligible:
                self._terminal(t, job, 'BLOCKED' if reasons else 'CANCELLED', reasons[0] if reasons else 'NO_ACTIVE_TARGETS')
                return None
            t.update('execution_jobs', {'job_id': job['job_id']}, status='RUNNING')
            for m in eligible:
                t.update('execution_mappings', {'job_id': job['job_id'], 'target_id': m['target_id']}, started=1)
            # Prefer a plan with a currently valid mapping for the B2 reservation.
            for row in t.rows('SELECT DISTINCT plan_id FROM execution_mappings WHERE job_id=:b_j ORDER BY plan_id', b_j=job['job_id']):
                candidate = self._plan(t, row['plan_id'])
                if self._validate(t, candidate, self.now())['valid']:
                    plan = candidate
                    break
            return plan

    def finish(self, claim, status, error=None, *, connection=None):
        if status not in ('SUCCEEDED', 'PARTIAL', 'LIMITED'):
            raise ValueError('Invalid completed status')
        if connection is None:
            with self.transaction() as t:
                return self.finish(claim, status, error, connection=t)
        t = connection
        job = self._fence(t, claim['job']['job_id'], claim['owner_id'], claim['fencing_generation'])
        if not t.scalar("SELECT count(*) AS c FROM execution_calls WHERE job_id=:b_j AND status='SUCCEEDED'", b_j=job['job_id']):
            raise FoodError('HTTP 성공 기록 없이 실행 성공을 확정할 수 없습니다.', 409)
        self._terminal(t, job, status, error)

    def _put_block(self, t, source, scope, kind, until):
        values = dict(error_kind=kind, blocked_at=self.now(), until_at=until)
        if not t.update('execution_blocks', {'source_id': source, 'account_scope': scope}, **values):
            t.insert('execution_blocks', dict(source_id=source, account_scope=scope, **values))

    def fail(self, claim, error):
        kind = getattr(error, 'kind', 'internal')
        allowed = {'authentication', 'permission', 'rate_limit', 'quota', 'budget', 'network', 'upstream', 'invalid_response', 'validation',
                   'policy', 'no_targets', 'checkpoint_expired', 'window_changed', 'internal'}
        if kind not in allowed:
            kind = 'internal'
        with self.transaction() as t:
            job = self._fence(t, claim['job']['job_id'], claim['owner_id'], claim['fencing_generation'])
            now = self.now()
            if kind in ('authentication', 'permission'):
                self._put_block(t, job['source_id'], job['account_scope'], kind, None)
                self._terminal(t, job, 'BLOCKED', kind)
            elif kind in ('policy', 'validation', 'invalid_response', 'checkpoint_expired'):
                self._terminal(t, job, 'FAILED' if kind in ('validation', 'invalid_response', 'checkpoint_expired') else 'BLOCKED', kind)
            elif kind == 'no_targets':
                self._terminal(t, job, 'CANCELLED', kind)
            elif job['attempt_count'] >= job['max_attempts']:
                if t.scalar("SELECT count(*) AS c FROM execution_calls WHERE job_id=:b_j AND status='SUCCEEDED' AND accepted_count>0", b_j=job['job_id']):
                    self.finish(claim, 'PARTIAL', kind, connection=t)
                else:
                    self._terminal(t, job, 'FAILED', kind)
            else:
                delay = max(2 ** job['attempt_count'] * 5, float(getattr(error, 'retry_after', 0)))
                t.update('execution_jobs', {'job_id': job['job_id']}, status='RETRY_WAIT', error_kind=kind, next_retry_at=now + timedelta(seconds=delay))
                t.update('execution_runs', {'run_id': job['run_id']}, execution_status='RETRY_WAIT')
            if kind in ('quota', 'rate_limit'):
                zone = 'America/Los_Angeles' if job['source_id'] == 'youtube' else 'Asia/Seoul' if job['source_id'] == 'naver_datalab' else 'UTC'
                delay = max(float(getattr(error, 'retry_after', 0)),
                            (instant(daily_window(now, zone)['end']) - now).total_seconds() if kind == 'quota' else 60)
                until = now + timedelta(seconds=delay)
                self._put_block(t, job['source_id'], job['account_scope'], kind, until)
                if self._job(t, job['job_id'])['status'] == 'RETRY_WAIT':
                    t.run('UPDATE execution_jobs SET next_retry_at=:b_u WHERE job_id=:b_j AND next_retry_at<:b_u', b_u=until, b_j=job['job_id'])
            t.update('execution_leases', {'resource_key': job['job_id']}, expires_at=now)

    def status(self, job_id=None):
        with self.reader() as t:
            if job_id:
                rows = [self._job(t, job_id)]
            else:
                limit = 'FETCH FIRST 200 ROWS ONLY' if t.oracle else 'LIMIT 200'
                rows = [self._job(t, r['job_id']) for r in t.rows('SELECT job_id FROM execution_jobs ORDER BY created_at DESC,job_id ' + limit)]
            for row in rows:
                plan = self._plan(t, row['plan_id'])
                row['execution_blockers'] = self._gate(t, plan, row['mode'])
                row['eligible_target_ids'] = [m['target_id'] for m in self._eligible(t, row)]
                row['current_budget'] = self.quota.usage(t, plan, self.now())
                row['calls'] = t.rows('SELECT * FROM execution_calls WHERE job_id=:b_j ORDER BY call_id', 'execution_calls', b_j=row['job_id'])
                run = t.one('SELECT * FROM execution_runs WHERE job_id=:b_j', 'execution_runs', b_j=row['job_id'])
                run['result_contract'] = json.loads(run.pop('result_contract_json') or 'null')
                row['run'] = run
            last = t.scalar("SELECT max(finished_at) AS m FROM execution_jobs WHERE status='SUCCEEDED'")
            return {'items': rows, 'live_enabled': False, 'automatic_enabled': False,
                    'counts': {r['status']: int(r['n']) for r in t.rows('SELECT status,count(*) AS n FROM execution_jobs GROUP BY status')},
                    'last_success_at': stamp(utc_value(last)) if last else None, 'read_only': True, 'storage': self.storage_label(),
                    'sources': self._sources(t)}

    def _sources(self, t):
        """Per source account: job outcomes, the latest failure, an active block and the budget of its latest plan; RSS discovery
        from its own runs (collected outside B3, no budget). Additive to the SQLite status shape; reads only."""
        now, limit, out = self.now(), ' FETCH FIRST 1 ROWS ONLY' if t.oracle else ' LIMIT 1', []
        for source, scope in sorted(self.accounts):
            jobs, where = 'FROM execution_jobs WHERE source_id=:b_s AND account_scope=:b_a', {'b_s': source, 'b_a': scope}
            success = t.scalar("SELECT max(finished_at) AS m %s AND status='SUCCEEDED'" % jobs, **where)
            failure = t.one('SELECT * %s AND error_kind IS NOT NULL ORDER BY coalesce(finished_at,created_at) DESC,job_id%s' % (jobs, limit),
                            'execution_jobs', **where)
            block = t.one('SELECT * FROM execution_blocks WHERE source_id=:b_s AND account_scope=:b_a', 'execution_blocks', **where)
            latest = t.one('SELECT plan_id %s ORDER BY created_at DESC,job_id%s' % (jobs, limit), **where)
            out.append({'source_id': source, 'account_scope': scope,
                        'job_counts': {r['status']: int(r['n']) for r in t.rows('SELECT status,count(*) AS n %s GROUP BY status' % jobs, **where)},
                        'last_success_at': stamp(utc_value(success)) if success else None,
                        'last_failure': {k: failure[k] for k in ('job_id', 'status', 'error_kind', 'finished_at')} if failure else None,
                        'block': {k: block[k] for k in ('error_kind', 'blocked_at', 'until_at')}
                                 if block and (block['until_at'] is None or instant(block['until_at']) > now) else None,
                        'budget': self.quota.usage(t, self._plan(t, latest['plan_id']), now) if latest else None})
        runs = {int(r['success']): int(r['n']) for r in t.rows('SELECT success,count(*) AS n FROM rss_runs WHERE finished_at IS NOT NULL GROUP BY success')}
        success = t.scalar('SELECT max(finished_at) AS m FROM rss_runs WHERE success=1')
        failure = t.one('SELECT * FROM rss_runs WHERE success=0 ORDER BY run_id DESC' + limit, 'rss_runs')
        out.append({'source_id': 'google_trends_rss', 'account_scope': 'public-feed', 'collector': 'oracle_rss',
                    'job_counts': {'SUCCEEDED': runs.get(1, 0), 'FAILED': runs.get(0, 0)},
                    'last_success_at': stamp(utc_value(success)) if success else None,
                    'last_failure': {'run_id': failure['run_id'], 'error': failure['error'], 'finished_at': failure['finished_at']} if failure else None,
                    'block': None, 'budget': None})
        return out

    def storage_label(self):
        settings = getattr(self.db, 'settings', None)
        return 'oracle_' + settings.environment if settings is not None else 'sqlite_target_standin'

    # Source content on the target (allowed fields only; NAVER values never reach durable state)

    def new_items(self, t, session, values, key, days):
        """One accepted item per logical job, irrespective of duplicate pages/retries."""
        result, now = [], self.now()
        for value in {v[key]: v for v in values}.values():
            if not t.scalar('SELECT count(*) AS c FROM execution_item_receipts WHERE job_id=:b_j AND item_id=:b_i',
                            b_j=session.claim['job']['job_id'], b_i=value[key]):
                t.insert('execution_item_receipts', dict(job_id=session.claim['job']['job_id'], item_id=value[key], observed_at=now,
                                                         expires_at=now + timedelta(days=days)))
                result.append(value)
        return result

    def put_content(self, t, row):
        """Insert or refresh one source_contents row; captured_at stays the first capture."""
        policy('source_contents', row, self.now())
        old = t.one('SELECT * FROM source_contents WHERE payload_ref=:b_p', 'source_contents', b_p=row['payload_ref'])
        if old is None:
            t.insert('source_contents', row)
            return None
        # A refreshed observation replaces the content and its provenance, as the source stores do.
        t.update('source_contents', {'payload_ref': row['payload_ref']},
                 **{k: row[k] for k in ('origin', 'content_json', 'content_hash', 'refreshed_at', 'expires_at', 'field_expires_at',
                                        'field_purged_at', 'source_payload_hash')})
        return old

    def content(self, t, payload_ref):
        return t.one('SELECT * FROM source_contents WHERE payload_ref=:b_p', 'source_contents', b_p=payload_ref)

    def delete_content(self, t, payload_ref):
        return t.run('DELETE FROM source_contents WHERE payload_ref=:b_p', b_p=payload_ref).rowcount

    def purge(self, at=None):
        """execution_maintenance.purge on the target: expired item receipts go; an expired checkpoint keeps only a tombstone
        (its expiry still refuses a replay)."""
        now = instant(at) if at else self.now()
        with self.transaction() as t:
            receipts = t.run('DELETE FROM execution_item_receipts WHERE expires_at<=:b_now', b_now=now).rowcount
            expired = [r['job_id'] for r in t.rows('SELECT * FROM execution_checkpoints WHERE expires_at<=:b_now', 'execution_checkpoints', b_now=now)
                       if json.loads(r['state_json']) != {'expired': True}]
            for job_id in expired:
                t.update('execution_checkpoints', {'job_id': job_id}, state_json=dump({'expired': True}))
        return {'expired_item_receipts': receipts, 'expired_checkpoints': len(expired), 'http_requests': 0}


class OracleRequestSession(RequestSession):
    """RequestSession over the target: fence + quota + send intention commit before each HTTP."""

    def __init__(self, execution, claim, plan, fault=None):
        self.execution, self.claim, self.plan = execution, claim, plan
        self.fault = fault or (lambda stage: None)
        self.reservation_id = None
        self.counts = {}
        self.state = {}
        with execution.reader() as t:
            checkpoint = t.one('SELECT * FROM execution_checkpoints WHERE job_id=:b_j', 'execution_checkpoints', b_j=claim['job']['job_id'])
        if checkpoint:
            if instant(checkpoint['expires_at']) <= execution.now():
                raise ExecutionError('checkpoint_expired')
            self.state = json.loads(checkpoint['state_json'])

    def save_state(self, state):
        self._check_state(state)
        with self.execution.transaction() as t:
            job = self._fence(t)
            t.update('execution_checkpoints', {'job_id': job['job_id']}, state_json=dump(state), updated_at=self.execution.now())
        self.state = state

    def start(self, state):
        """First checkpoint of a job, written under the fence before any request."""
        with self.execution.transaction() as t:
            job = self._fence(t)
            now = self.execution.now()
            t.insert('execution_checkpoints', dict(job_id=job['job_id'], state_json=dump(state), updated_at=now, expires_at=now + timedelta(days=7)))
        self.state = state

    def _before(self, endpoint, step_key):
        e, p = self.execution, self.plan
        with e.transaction() as t:
            job = self._fence(t)
            if e._gate(t, p, job['mode']):
                raise ExecutionError('policy')
            validation = e._validate(t, p, e.now())
            if not validation['valid']:
                raise ExecutionError('no_targets' if validation['reasons'] == ['NO_CURRENT_TARGET_MAPPING'] else 'policy')
            if self.reservation_id is None:
                number = int(t.scalar('SELECT coalesce(max(attempt_no),0)+1 AS n FROM quota_reservations WHERE query_id=:b_q AND due_slot=:b_d',
                                      b_q=p['query_id'], b_d=p['due_slot']))
                try:
                    reservation = e.quota.reserve(p['plan_id'], job['job_id'] + ':' + str(job['attempt_count']), number,
                                                  e.settings.lease_seconds, connection=t)
                except FoodError:
                    budget = e.quota.assess(t, p, e.now())
                    if budget['status'] != 'READY_LOCAL':
                        resets = [instant(line['window_end']) for line in budget['lines'] if line['budget_status'] != 'READY_LOCAL']
                        raise ExecutionError('budget', retry_after=max((r - e.now()).total_seconds() for r in resets)) from None
                    raise ExecutionError('policy') from None
                self.reservation_id = reservation['reservation_id']
            e.quota.mark_attempted(self.reservation_id, connection=t)
            for c in t.rows('SELECT window_end FROM quota_charges WHERE reservation_id=:b_r', 'quota_charges', b_r=self.reservation_id):
                if instant(c['window_end']) <= e.now():
                    raise ExecutionError('window_changed')
            # Workflow reservation is a ceiling, never permission for an unbounded client retry.
            maximum = p['canonical_query'].get('max_pages', 1) if endpoint in ('search', 'videos', 'recent_media') else 3 if endpoint == 'recent_searches' else 1
            if self.counts.get(endpoint, 0) >= maximum:
                raise ExecutionError('validation')
            # Deterministic, insertion-ordered call ids (the fence serializes writers of one job).
            sequence = int(t.scalar('SELECT count(*) AS c FROM execution_calls WHERE job_id=:b_j', b_j=job['job_id'])) + 1
            call_id = '%s:C%05d' % (job['job_id'], sequence)
            t.insert('execution_calls', dict(call_id=call_id, job_id=job['job_id'], reservation_id=self.reservation_id, attempt_no=job['attempt_count'],
                                             step_key=step_key, endpoint=endpoint, fencing_generation=self.claim['fencing_generation'],
                                             started_at=e.now(), status='SENT', remote_debit_confirmed=0))
            e._put_lease(t, job['job_id'], renewed_at=e.now(), expires_at=e.now() + timedelta(seconds=e.settings.lease_seconds))
            run = t.one('SELECT observed_at FROM execution_runs WHERE run_id=:b_r', 'execution_runs', b_r=job['run_id'])
            if run['observed_at'] is None:
                t.update('execution_runs', {'run_id': job['run_id']}, observed_at=e.now())
        self.counts[endpoint] = self.counts.get(endpoint, 0) + 1
        return call_id

    def request(self, endpoint, step_key, send, parse, persist, checkpoint):
        self.fault('before_send_intent')
        call_id = self._before(endpoint, step_key)
        self.fault('before_http')
        try:
            payload = send()  # no target transaction is open here
            self.fault('after_http')
            parsed = parse(payload)
            next_state = checkpoint(parsed)
            validate_metadata(next_state)
            self._check_state(next_state)
            with self.execution.transaction() as t:
                job = self._fence(t)
                if self.execution._gate(t, self.plan, job['mode']):
                    raise ExecutionError('policy')
                returned, accepted = persist(t, parsed, self.execution._eligible(t, job, started_only=True))
                now = self.execution.now()
                t.update('execution_calls', {'call_id': call_id}, status='SUCCEEDED', finished_at=now, http_status=200,
                         returned_count=returned, accepted_count=accepted)
                values = dict(state_json=dump(next_state), updated_at=now, expires_at=now + timedelta(days=7))
                if not t.update('execution_checkpoints', {'job_id': job['job_id']}, **values):
                    t.insert('execution_checkpoints', dict(job_id=job['job_id'], **values))
                self.fault('before_page_commit')
            self.state = next_state
            self.fault('after_page_commit')
            return parsed
        except LostLease:
            raise
        except Exception as exc:
            kind = 'internal' if isinstance(exc, TargetError) else getattr(exc, 'kind', 'invalid_response')
            if kind not in ('authentication', 'permission', 'quota', 'rate_limit', 'upstream', 'network', 'invalid_response', 'validation',
                            'policy', 'no_targets', 'internal'):
                kind = 'invalid_response'
            with self.execution.transaction() as t:
                self._fence(t)
                retry_after = getattr(exc, 'retry_after', 0)
                t.update('execution_calls', {'call_id': call_id}, status='UNKNOWN' if kind in ('network', 'internal') else 'FAILED',
                         finished_at=self.execution.now(), http_status=getattr(exc, 'status', None), error_kind=kind,
                         retry_after_seconds=float(retry_after) if retry_after is not None else None)
                if kind in ('network', 'internal'):
                    t.update('execution_jobs', {'job_id': self.claim['job']['job_id']}, remote_duplicate_possible=1)
            raise ExecutionError(kind, getattr(exc, 'status', None), getattr(exc, 'retry_after', 0)) from None


OracleExecution.session_class = OracleRequestSession


def _content_row(payload_ref, key, source, origin, content, captured, now, **extra):
    serialized = json.dumps(content, ensure_ascii=False)
    return dict(payload_ref=payload_ref, item_key=key, source_code=source, origin=origin, content_json=serialized,
                content_hash=hashlib.sha256(canonical_json(content).encode()).hexdigest(), captured_at=captured, refreshed_at=now,
                **{k: extra.get(k) for k in ('expires_at', 'field_expires_at', 'field_purged_at', 'source_payload_hash')})


class OracleYouTubeExecutor(SourceAdapter):
    """YouTubeExecutor's request sequence; public snapshots go to source_contents (29-day expiry)."""

    def execute(self, execution, claim, plan, fault):
        s = self._session(execution, claim, plan, fault)
        params = dict(plan['canonical_query']['parameters'])
        origin = 'mock' if claim['job']['mode'] == 'mock' else 'live_api'
        if not s.state:
            s.start({'phase': 'search', 'page': 0, 'cursor': None})
        while not s.state.get('done'):
            state = dict(s.state)
            page = state['page']
            if state['phase'] == 'search':
                request = dict(params)
                if state.get('cursor'):
                    request['pageToken'] = state['cursor']
                s.request('search', 'search:' + str(page),
                          lambda: self.client.get('search', request, lambda endpoint: None, lambda *args: None, single_attempt=True),
                          lambda payload: parse_search(payload, state), lambda t, parsed, targets: (parsed['count'], 0),
                          lambda parsed: search_checkpoint(state, parsed, plan['canonical_query']['max_pages']))
            else:
                requested = state['pending_ids']
                detail = {'part': 'snippet,statistics,contentDetails,status', 'id': ','.join(requested)}

                def persist(t, videos, targets, requested=requested):
                    public = public_videos(videos)
                    fresh = execution.new_items(t, s, public, 'video_id', 29)
                    # Requested IDs that are no longer public are removed, independent of receipts.
                    for gone in sorted(set(requested) - {v['video_id'] for v in public}):
                        execution.delete_content(t, 'youtube_videos:' + gone)
                    now = execution.now()
                    for video in fresh:
                        ref = 'youtube_videos:' + video['video_id']
                        old = execution.content(t, ref)
                        serialized = json.dumps(video, ensure_ascii=False)
                        execution.put_content(t, _content_row(ref, item_key('youtube', video['video_id']), 'youtube', origin, video,
                                                              old['captured_at'] if old else now, now, expires_at=now + timedelta(days=29),
                                                              source_payload_hash=hashlib.sha256(serialized.encode()).hexdigest()))
                    return len(videos), len(fresh)
                s.request('videos', 'videos:' + str(page),
                          lambda: self.client.get('videos', detail, lambda endpoint: None, lambda *args: None, single_attempt=True),
                          lambda payload: parse_videos(payload, requested), persist, lambda parsed: videos_checkpoint(state))
        return {'status': 'LIMITED' if s.state.get('limited') else 'SUCCEEDED'}


class OracleInstagramExecutor(SourceAdapter):
    """InstagramExecutor's sequence; media metadata and the caption window go to source_contents. In live mode the
    provider's own recent unique-tag set is checked first, as the existing collector does."""

    def execute(self, execution, claim, plan, fault):
        s = self._session(execution, claim, plan, fault)
        cfg = self.client.config
        params = plan['canonical_query']['parameters']
        retention = min(cfg.caption_retention_days, plan['contract']['policy']['retention_days'])
        origin = 'mock' if claim['job']['mode'] == 'mock' else 'live_api'
        if not s.state:
            s.start({'phase': 'lookup', 'page': 0, 'hashtag_id': None})
        if claim['job']['mode'] == 'live' and remote_usage_check_needed(s.state, usage_fingerprint(cfg, plan, params['q']), execution.now()):
            self._remote_usage(s, cfg, plan, params['q'], execution)
        if s.state['phase'] == 'lookup':
            s.request('hashtag_lookup', 'lookup',
                      lambda: self.client.get('ig_hashtag_search', {'user_id': cfg.user_id, 'q': params['q']}, single_attempt=True),
                      parse_lookup, lambda t, identifier, targets: (int(identifier is not None), 0),
                      lambda identifier: lookup_checkpoint(s.state, identifier, execution.now()))
        while not s.state.get('done'):
            state = dict(s.state)
            page = state['page']
            request = media_request(cfg, params, state)

            def save(t, parsed, targets):
                require_operation('instagram_graph', 'existing_caption_hashtags')
                media = parsed[0]
                fresh = execution.new_items(t, s, media, 'id', retention)
                for item in fresh:
                    self._store(execution, t, item, retention, origin)
                return len(media), len(fresh)
            s.request('recent_media', 'media:' + str(page),
                      lambda: self.client.get(state['hashtag_id'] + '/recent_media', request, single_attempt=True),
                      lambda payload: parse_media(payload, state), save,
                      lambda parsed: media_checkpoint(state, parsed, plan['canonical_query']['max_pages']))
        return {'status': 'LIMITED' if s.state.get('limited') else 'SUCCEEDED'}

    def _remote_usage(self, s, cfg, plan, tagname, execution):
        """InstagramExecutor's check of the provider's 7-day unique hashtags (at most 3 pages): a full window that does not
        already hold this tag defers the job for a week instead of spending a lookup. The result is kept in the checkpoint."""
        remote, cursor = [], None
        for page in range(3):
            request = {'limit': 30}
            if cursor:
                request['after'] = cursor

            def parse_recent(payload, cursor=cursor):
                rows, paging = payload.get('data'), payload.get('paging') or {}
                if not isinstance(rows, list) or not isinstance(paging, dict):
                    raise ExecutionError('invalid_response')
                ids = [r.get('id') for r in rows if isinstance(r, dict)]
                if len(ids) != len(rows) or any(not isinstance(v, str) or not v.isdigit() for v in ids):
                    raise ExecutionError('invalid_response')
                after = (paging.get('cursors') or {}).get('after') if paging.get('next') else None
                if paging.get('next') and (not isinstance(after, str) or after == cursor):
                    raise ExecutionError('invalid_response')
                return ids, after
            parsed = s.request('recent_searches', 'remote-usage:' + str(page),
                               lambda request=request: self.client.get(cfg.user_id + '/recently_searched_hashtags', request, single_attempt=True),
                               parse_recent, lambda t, parsed, targets: (len(parsed[0]), 0), lambda parsed: dict(s.state))
            remote.extend(parsed[0])
            cursor = parsed[1]
            if not cursor:
                break
        if cursor:
            raise ExecutionError('invalid_response')
        if len(set(remote)) >= min(30, cfg.search_soft_limit) and s.state.get('hashtag_id') not in remote:
            raise ExecutionError('budget', retry_after=7 * 86400)
        s.save_state(dict(s.state, remote_usage={'checked_at': stamp(execution.now()), 'verified': True,
                                                 'fingerprint': usage_fingerprint(cfg, plan, tagname), 'provider_unique_tags': len(set(remote)),
                                                 'tag_in_provider_window': s.state.get('hashtag_id') in remote}))

    @staticmethod
    def _store(execution, t, item, retention, origin):
        """InstagramStorage.finish_run merge rules: a missing field keeps the stored value; a changed caption restarts its window."""
        now, ref = execution.now(), 'instagram_media:' + item['id']
        old = execution.content(t, ref)
        previous = json.loads(old['content_json']) if old else {}
        content = {'id': item['id'], 'source': 'instagram_graph'}
        for field in ('published_at', 'media_type', 'permalink'):
            content[field] = item.get(field) if item.get(field) is not None else previous.get(field)
        caption = item.get('caption')
        field_expires = old['field_expires_at'] if old else None
        field_purged = old['field_purged_at'] if old else None
        if caption is not None:
            content['caption'] = caption
            content['caption_tags'] = sorted({tag['normalized_name'] for tag in extract_hashtags(caption)})
            if caption != previous.get('caption') or field_expires is None:
                field_expires = now + timedelta(days=retention)
            field_purged = None
        elif 'caption' in previous:
            content['caption'], content['caption_tags'] = previous['caption'], previous.get('caption_tags', [])
        execution.put_content(t, _content_row(ref, item_key('instagram_graph', item['id']), 'instagram_graph',
                                              origin, content,
                                              old['captured_at'] if old else now, now, field_expires_at=field_expires, field_purged_at=field_purged))


def oracle_adapters(clients):
    classes = {'instagram_graph': OracleInstagramExecutor, 'youtube': OracleYouTubeExecutor, 'naver_datalab': NaverExecutor}
    return {key: classes[key[0]](client) for key, client in clients.items()}


def oracle_mock_controller(repo, accounts, clock=None, transport=None, settings=None, pipeline=True):
    """Explicit Oracle-runtime assembly with mock transports; constructs no worker thread and runs no DDL.

    With pipeline (the default, as mock_controller) a terminal job is staged by B4 and linked by B5 on
    the same target.
    """
    from .execution_mock import MockTransport, mock_clients
    from .oracle_planner import OracleQueryPlanner
    from .oracle_rss import OracleExecutionController
    execution = OracleExecution(repo, accounts, clock, settings)
    OracleQueryPlanner(execution)
    clients = mock_clients(execution.accounts.values(), transport or MockTransport())
    ingestion = observations = None
    if pipeline:
        from .oracle_ingestion import OracleIngestionService
        from .oracle_observations import OracleObservationService
        ingestion, observations = OracleIngestionService(execution), OracleObservationService(execution)
    return OracleExecutionController(execution, oracle_adapters(clients), ingestion, observations)
