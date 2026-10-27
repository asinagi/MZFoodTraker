"""Durable scheduler/outbox and fenced execution. No HTTP client is imported here."""
import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from .contracts import CONTRACT_VERSION, validate_metadata
from .food_service import FoodError, identity, dump, text
from .query_planner import digest
from .tracking_service import instant, stamp

TERMINAL = ('SUCCEEDED', 'PARTIAL', 'LIMITED', 'BLOCKED', 'FAILED', 'CANCELLED')


class LostLease(FoodError):
    def __init__(self):
        super().__init__('실행 소유권이 만료되었거나 변경됐습니다.', 409)


class ExecutionError(Exception):
    def __init__(self, kind, status=None, retry_after=0):
        self.kind, self.status, self.retry_after = kind, status, retry_after
        super().__init__(kind)


@dataclass(frozen=True)
class ExecutionSettings:
    live_enabled: bool = False
    automatic: bool = False
    lease_seconds: int = 120
    max_attempts: int = 3
    # A deployment must explicitly transfer each source from its legacy writer.
    exclusive_live_sources: tuple = ()

    def __post_init__(self):
        if not 10 <= self.lease_seconds <= 300 or not 1 <= self.max_attempts <= 5:
            raise ValueError('Invalid execution bounds')
        if self.automatic and not self.live_enabled:
            raise ValueError('Automatic live execution requires explicit live configuration')


class ExecutionService:
    def __init__(self, storage, planner, settings=None):
        self.storage, self.planner, self.tracking = storage, planner, planner.tracking
        self.settings = settings or ExecutionSettings()

    def now(self):
        return instant(self.tracking.clock())

    def _job(self, db, job_id):
        return self.tracking.foods._row(db, 'execution_jobs', 'job_id', job_id)

    def _plan(self, db, plan_id):
        return json.loads(self.tracking.foods._row(db, 'planner_plans', 'plan_id', plan_id)['payload_json'])

    def _gate(self, db, plan, mode):
        if plan['source_id'] == 'google_trends_rss':
            return ['RSS_OWNED_BY_EXISTING_DISCOVERY_SCHEDULER']
        account = self.tracking.account(plan['source_id'], plan['account_scope'])
        reasons = list(account.blockers())
        if account.mode != mode:
            reasons.append('EXECUTION_MODE_MISMATCH')
        if mode == 'live' and (not self.settings.live_enabled or plan['source_id'] not in self.settings.exclusive_live_sources):
            reasons.append('LIVE_DISABLED_OR_LEGACY_WRITER_NOT_TRANSFERRED')
        block = db.execute('SELECT * FROM execution_blocks WHERE source_id=? AND account_scope=?',
                           (plan['source_id'], plan['account_scope'])).fetchone()
        if block and (block['until_at'] is None or instant(block['until_at']) > self.now()):
            reasons.append('ACCOUNT_BLOCKED_' + block['error_kind'])
        return reasons

    def _eligible(self, db, job, started_only=False):
        result = []
        for row in db.execute('SELECT * FROM execution_mappings WHERE job_id=?', (job['job_id'],)):
            if started_only and not row['started']:
                continue
            plan = self._plan(db, row['plan_id'])
            validation = self.planner._validate(db, plan, self.now())
            if any(m['target_id'] == row['target_id'] and m['generation'] == row['generation'] and
                   m['food_revision'] == row['food_revision'] for m in validation['eligible_targets']):
                result.append(json.loads(row['mapping_json']))
        return result

    def enqueue(self, plan_ids, mode='mock'):
        if mode not in ('mock', 'live') or not isinstance(plan_ids, list) or not 1 <= len(plan_ids) <= 200:
            raise FoodError('실행 모드와 계획 목록을 확인하세요.')
        result = []
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for plan_id in dict.fromkeys(plan_ids):
                p = self._plan(db, plan_id)
                reasons = self._gate(db, p, mode)
                validation = self.planner._validate(db, p, self.now())
                if reasons or not validation['valid']:
                    result.append({'plan_id': plan_id, 'status': 'BLOCKED', 'reasons': reasons + validation['reasons']})
                    continue
                existing = [dict(r) for r in db.execute('SELECT * FROM execution_jobs WHERE query_id=? AND due_slot=? AND mode=? ORDER BY execution_generation',
                            (p['query_id'], p['due_slot'], mode))]
                uncovered = []
                for m in validation['eligible_targets']:
                    covered = db.execute('''SELECT j.job_id FROM execution_mappings m JOIN execution_jobs j USING(job_id)
                        WHERE j.query_id=? AND j.due_slot=? AND j.mode=? AND m.target_id=? AND m.generation=? AND m.food_revision=?''',
                        (p['query_id'], p['due_slot'], mode, m['target_id'], m['generation'], m['food_revision'])).fetchone()
                    if not covered:
                        uncovered.append(m)
                if existing and not uncovered:
                    result.append(existing[-1])
                    continue
                # Before claim, later plans can join one shared transport job.
                reusable = next((j for j in existing if j['status'] == 'READY' and j['attempt_count'] == 0), None)
                if reusable:
                    job_id = reusable['job_id']
                else:
                    epoch = max((j['execution_generation'] for j in existing), default=0) + 1
                    job_id, run_id, now = identity('JOB'), identity('RUN'), stamp(self.now())
                    key = digest([p['query_id'], p['due_slot'], mode, epoch])
                    db.execute('''INSERT INTO execution_jobs(job_id,job_key,query_id,plan_id,source_id,account_scope,due_slot,
                        execution_generation,mode,status,max_attempts,created_at,run_id) VALUES (?,?,?,?,?,?,?,?,?,'READY',?,?,?)''',
                        (job_id, key, p['query_id'], plan_id, p['source_id'], p['account_scope'], p['due_slot'], epoch,
                         mode, self.settings.max_attempts, now, run_id))
                    db.execute('INSERT INTO execution_outbox(event_id,job_id,created_at) VALUES (?,?,?)', (identity('OUTBOX'), job_id, now))
                    db.execute('INSERT INTO execution_runs(run_id,job_id,source_code,origin,execution_status) VALUES (?,?,?,?,?)',
                               (run_id, job_id, p['source_id'], 'mock' if mode == 'mock' else 'live_api', 'READY'))
                for m in uncovered:
                    db.execute('INSERT OR IGNORE INTO execution_mappings(job_id,plan_id,target_id,generation,food_revision,mapping_json) VALUES (?,?,?,?,?,?)',
                        (job_id, plan_id, m['target_id'], m['generation'], m['food_revision'], dump(m)))
                    target = self.tracking._target(db, m['target_id'])
                    if target['next_due_at'] == m['due_at']:
                        ctx = {'request_key': 'b3-slot-' + digest([m['target_id'], m['generation'], m['due_at']]),
                               'actor_ref': 'execution-scheduler', 'reason': '실행 Outbox와 예정 슬롯 원자 확정',
                               'target_id': m['target_id'], 'expected_revision': target['revision']}
                        db.execute('INSERT OR IGNORE INTO tracking_commands VALUES (?,?,?,?,?)',
                                   (ctx['request_key'], 'schedule', digest(ctx), '{}', stamp(self.now())))
                        scheduled = self.tracking._change(db, 'schedule', ctx)
                        db.execute('UPDATE tracking_commands SET result_json=? WHERE request_key=?', (dump(scheduled), ctx['request_key']))
                result.append(self._job(db, job_id))
        return result

    def schedule(self, options=None, mode='mock'):
        # Persisted plans are harmless if the subsequent atomic outbox transaction fails.
        plans = self.planner.commit(identity('SCHEDULE'), options=options)
        ids = [p['plan_id'] for p in plans['plans'] if p['source_id'] != 'google_trends_rss' and p['decision'] != 'NOT_DUE']
        return self.enqueue(ids, mode) if ids else []

    def _fence(self, db, job_id, owner, token):
        lease = db.execute('SELECT * FROM execution_leases WHERE resource_key=?', (job_id,)).fetchone()
        if not lease or lease['owner_id'] != owner or lease['fencing_generation'] != token or instant(lease['expires_at']) <= self.now():
            raise LostLease()
        job = self._job(db, job_id)
        if job['status'] not in ('CLAIMED', 'RUNNING'):
            raise LostLease()
        return job

    def claim(self, owner, job_id=None):
        owner = text(owner, 100)
        now = self.now()
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('''SELECT j.* FROM execution_jobs j JOIN execution_outbox o USING(job_id)
                LEFT JOIN execution_leases l ON l.resource_key=j.job_id
                WHERE j.status IN ('READY','RETRY_WAIT','CLAIMED','RUNNING')
                AND (j.next_retry_at IS NULL OR j.next_retry_at<=?) AND (l.expires_at IS NULL OR l.expires_at<=?)
                AND (? IS NULL OR j.job_id=?) ORDER BY j.created_at,j.job_id''', (stamp(now), stamp(now), job_id, job_id)).fetchall()
            for row in rows:
                job = dict(row)
                if job['attempt_count'] >= job['max_attempts']:
                    self._terminal(db, job, 'FAILED', 'MAX_ATTEMPTS')
                    continue
                old = db.execute('SELECT * FROM execution_leases WHERE resource_key=?', (job['job_id'],)).fetchone()
                token = old['fencing_generation'] + 1 if old else 1
                # An interrupted send remains charged; no claim of remote exactly-once.
                unknown = db.execute("UPDATE execution_calls SET status='UNKNOWN',error_kind='INTERRUPTED_AFTER_SEND' WHERE job_id=? AND status='SENT'", (job['job_id'],)).rowcount
                if unknown:
                    db.execute('UPDATE execution_jobs SET remote_duplicate_possible=1 WHERE job_id=?', (job['job_id'],))
                # Recover reservations left before the attempted/send boundary.
                for reservation in db.execute('''SELECT r.* FROM quota_reservations r WHERE r.attempt_key LIKE ? AND r.state='RESERVED' ''', (job['job_id'] + ':%',)).fetchall():
                    db.execute("UPDATE quota_reservations SET state='RELEASED' WHERE reservation_id=?", (reservation['reservation_id'],))
                    self.planner.quota._event(db, reservation['reservation_id'], 'RELEASED', now, {'recovered_before_attempt': True})
                db.execute('''INSERT INTO execution_leases VALUES (?,?,?,?,?,?) ON CONFLICT(resource_key) DO UPDATE SET
                    owner_id=excluded.owner_id,fencing_generation=excluded.fencing_generation,acquired_at=excluded.acquired_at,
                    renewed_at=excluded.renewed_at,expires_at=excluded.expires_at''',
                    (job['job_id'], owner, token, stamp(now), stamp(now), stamp(now + timedelta(seconds=self.settings.lease_seconds))))
                db.execute("UPDATE execution_jobs SET status='CLAIMED',attempt_count=attempt_count+1,started_at=coalesce(started_at,?),next_retry_at=NULL WHERE job_id=?", (stamp(now), job['job_id']))
                db.execute('UPDATE execution_outbox SET delivery_count=delivery_count+1 WHERE job_id=?', (job['job_id'],))
                return {'job': self._job(db, job['job_id']), 'owner_id': owner, 'fencing_generation': token}
        return None

    def renew(self, claim):
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._fence(db, claim['job']['job_id'], claim['owner_id'], claim['fencing_generation'])
            db.execute('UPDATE execution_leases SET renewed_at=?,expires_at=? WHERE resource_key=?',
                       (stamp(self.now()), stamp(self.now() + timedelta(seconds=self.settings.lease_seconds)), claim['job']['job_id']))

    def _terminal(self, db, job, status, error=None):
        db.execute('UPDATE execution_jobs SET status=?,error_kind=?,finished_at=?,next_retry_at=NULL WHERE job_id=?',
                   (status, error, stamp(self.now()), job['job_id']))
        db.execute('UPDATE execution_runs SET execution_status=? WHERE run_id=?', (status, job['run_id']))
        db.execute('UPDATE execution_outbox SET delivered_at=? WHERE job_id=?', (stamp(self.now()), job['job_id']))

    def begin(self, claim):
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._fence(db, claim['job']['job_id'], claim['owner_id'], claim['fencing_generation'])
            plan = self._plan(db, job['plan_id'])
            reasons = self._gate(db, plan, job['mode'])
            validation = self.planner._validate(db, plan, self.now())
            # A shared job may outlive its original plan's targets; all member plans are checked.
            eligible = self._eligible(db, job)
            reasons += [r for r in validation['reasons'] if r != 'NO_CURRENT_TARGET_MAPPING']
            if reasons or plan['targets'] and not eligible:
                self._terminal(db, job, 'BLOCKED' if reasons else 'CANCELLED', reasons[0] if reasons else 'NO_ACTIVE_TARGETS')
                return None
            db.execute("UPDATE execution_jobs SET status='RUNNING' WHERE job_id=?", (job['job_id'],))
            for m in eligible:
                db.execute('UPDATE execution_mappings SET started=1 WHERE job_id=? AND target_id=?', (job['job_id'], m['target_id']))
            # Prefer a plan with a currently valid mapping for the B2 reservation.
            for row in db.execute('SELECT DISTINCT plan_id FROM execution_mappings WHERE job_id=?', (job['job_id'],)):
                candidate = self._plan(db, row['plan_id'])
                if self.planner._validate(db, candidate, self.now())['valid']:
                    plan = candidate
                    break
            return plan

    def finish(self, claim, status, error=None):
        if status not in ('SUCCEEDED', 'PARTIAL', 'LIMITED'):
            raise ValueError('Invalid completed status')
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._fence(db, claim['job']['job_id'], claim['owner_id'], claim['fencing_generation'])
            calls = db.execute('SELECT count(*),coalesce(sum(returned_count),0),coalesce(sum(accepted_count),0) FROM execution_calls WHERE job_id=? AND status=\'SUCCEEDED\'', (job['job_id'],)).fetchone()
            if not calls[0]:
                raise FoodError('HTTP 성공 기록 없이 실행 성공을 확정할 수 없습니다.', 409)
            eligible = self._eligible(db, job, started_only=True)
            plan = self._plan(db, job['plan_id'])
            contract = dict(run_id=job['run_id'], job_id=job['job_id'], source_code=job['source_id'], query_id=job['query_id'],
                active_target_ids=[m['target_id'] for m in eligible], origin='mock' if job['mode']=='mock' else 'live_api',
                contract_version=CONTRACT_VERSION, policy_version=plan['policy_version'], observed_at=stamp(self.now()),
                request_context=plan['contract']['request_conditions'], execution_status=status,
                coverage='complete_request' if status=='SUCCEEDED' else 'partial_request', returned_count=calls[1], accepted_count=calls[2],
                error_kind=error, allowed_result_reference=None if job['source_id']=='naver_datalab' else {'storage':'existing_source_tables','run_id':job['run_id']},
                mapping_semantics='search_provenance_only', remote_debit_confirmed=False)
            validate_metadata(contract)
            db.execute('UPDATE execution_runs SET observed_at=?,returned_count=?,accepted_count=?,result_contract_json=? WHERE run_id=?',
                       (stamp(self.now()), calls[1], calls[2], dump(contract), job['run_id']))
            for m in eligible:
                db.execute('INSERT OR IGNORE INTO execution_results VALUES (?,?,?,?,?)', (job['run_id'], m['target_id'], m['generation'], m['food_revision'], stamp(self.now())))
                if status == 'SUCCEEDED':
                    db.execute('UPDATE tracking_targets SET last_success_at=? WHERE target_id=? AND generation=? AND food_revision=?',
                               (stamp(self.now()), m['target_id'], m['generation'], m['food_revision']))
            self._terminal(db, job, status, error)

    def fail(self, claim, error):
        kind = getattr(error, 'kind', 'internal')
        allowed = {'authentication','permission','rate_limit','quota','budget','network','upstream','invalid_response','validation',
                   'policy','no_targets','checkpoint_expired','window_changed','internal'}
        if kind not in allowed:
            kind = 'internal'
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._fence(db, claim['job']['job_id'], claim['owner_id'], claim['fencing_generation'])
            if kind in ('authentication','permission'):
                db.execute('INSERT INTO execution_blocks VALUES (?,?,?,?,NULL) ON CONFLICT(source_id,account_scope) DO UPDATE SET error_kind=excluded.error_kind,blocked_at=excluded.blocked_at,until_at=NULL',
                           (job['source_id'], job['account_scope'], kind, stamp(self.now())))
                self._terminal(db, job, 'BLOCKED', kind)
            elif kind in ('policy','validation','invalid_response','checkpoint_expired'):
                self._terminal(db, job, 'FAILED' if kind in ('validation','invalid_response','checkpoint_expired') else 'BLOCKED', kind)
            elif kind == 'no_targets':
                self._terminal(db, job, 'CANCELLED', kind)
            elif job['attempt_count'] >= job['max_attempts']:
                successes = db.execute("SELECT 1 FROM execution_calls WHERE job_id=? AND status='SUCCEEDED' AND accepted_count>0", (job['job_id'],)).fetchone()
                self._terminal(db, job, 'PARTIAL' if successes else 'FAILED', kind)
            else:
                delay = max(2 ** job['attempt_count'] * 5, float(getattr(error, 'retry_after', 0)))
                db.execute("UPDATE execution_jobs SET status='RETRY_WAIT',error_kind=?,next_retry_at=? WHERE job_id=?",
                           (kind, stamp(self.now()+timedelta(seconds=delay)), job['job_id']))
                db.execute("UPDATE execution_runs SET execution_status='RETRY_WAIT' WHERE run_id=?", (job['run_id'],))
            db.execute('UPDATE execution_leases SET expires_at=? WHERE resource_key=?', (stamp(self.now()), job['job_id']))

    def status(self, job_id=None):
        with self.storage.reader() as db:
            rows = [self._job(db, job_id)] if job_id else [dict(r) for r in db.execute('SELECT * FROM execution_jobs ORDER BY created_at DESC,job_id LIMIT 200')]
            for row in rows:
                plan = self._plan(db, row['plan_id'])
                row['execution_blockers'] = self._gate(db, plan, row['mode'])
                row['eligible_target_ids'] = [m['target_id'] for m in self._eligible(db, row)]
                row['current_budget'] = self.planner.quota.assess(db, plan, self.now())
                row['calls'] = [dict(r) for r in db.execute('SELECT * FROM execution_calls WHERE job_id=? ORDER BY started_at,call_id', (row['job_id'],))]
                run = dict(db.execute('SELECT * FROM execution_runs WHERE job_id=?', (row['job_id'],)).fetchone())
                run['result_contract'] = json.loads(run.pop('result_contract_json') or 'null')
                row['run'] = run
            return {'items': rows, 'live_enabled': self.settings.live_enabled, 'automatic_enabled': self.settings.automatic,
                    'counts': dict(db.execute('SELECT status,count(*) FROM execution_jobs GROUP BY status')),
                    'last_success_at': db.execute("SELECT max(finished_at) FROM execution_jobs WHERE status='SUCCEEDED'").fetchone()[0],
                    'read_only': True}


class ExecutionWorker:
    def __init__(self, execution, adapters, owner=None, fault=None):
        self.execution, self.adapters = execution, adapters
        self.owner, self.fault = owner or identity('WORKER'), fault or (lambda stage: None)

    def run_once(self, job_id=None):
        claim = self.execution.claim(self.owner, job_id)
        if claim is None:
            return None
        try:
            self.fault('after_claim')
            plan = self.execution.begin(claim)
            if plan is None:
                return self.execution.status(claim['job']['job_id'])['items'][0]
            adapter = self.adapters.get((plan['source_id'], plan['account_scope']))
            if adapter is None:
                raise ExecutionError('policy')
            result = adapter.execute(self.execution, claim, plan, self.fault)
            self.fault('before_finish')
            self.execution.finish(claim, result.get('status', 'SUCCEEDED'), result.get('error_kind'))
        except LostLease:
            return {'job_id': claim['job']['job_id'], 'status': 'STALE_OWNER_REJECTED'}
        except Exception as exc:
            try:
                self.execution.fail(claim, exc)
            except LostLease:
                return {'job_id': claim['job']['job_id'], 'status': 'STALE_OWNER_REJECTED'}
        return self.execution.status(claim['job']['job_id'])['items'][0]


class RequestSession:
    """One workflow attempt. Fence + quota + send intention commit before each HTTP.

    The caller supplies existing parser/storage functions; no provider body is persisted here.
    A page's source writes, checkpoint and call completion share the fenced transaction.
    """
    def __init__(self, execution, claim, plan, fault=None):
        self.execution, self.claim, self.plan = execution, claim, plan
        self.fault = fault or (lambda stage: None)
        self.reservation_id = None
        self.counts = {}
        self.state = {}
        with execution.storage.reader() as db:
            checkpoint = db.execute('SELECT * FROM execution_checkpoints WHERE job_id=?', (claim['job']['job_id'],)).fetchone()
            if checkpoint:
                if instant(checkpoint['expires_at']) <= execution.now():
                    raise ExecutionError('checkpoint_expired')
                self.state = json.loads(checkpoint['state_json'])

    def _fence(self, db):
        return self.execution._fence(db, self.claim['job']['job_id'], self.claim['owner_id'], self.claim['fencing_generation'])

    def _before(self, endpoint, step_key):
        e, p = self.execution, self.plan
        with e.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._fence(db)
            if e._gate(db, p, job['mode']):
                raise ExecutionError('policy')
            validation = e.planner._validate(db, p, e.now())
            if not validation['valid']:
                raise ExecutionError('no_targets' if validation['reasons'] == ['NO_CURRENT_TARGET_MAPPING'] else 'policy')
            if self.reservation_id is None:
                number = db.execute('SELECT coalesce(max(attempt_no),0)+1 FROM quota_reservations WHERE query_id=? AND due_slot=?', (p['query_id'], p['due_slot'])).fetchone()[0]
                try:
                    reservation = e.planner.quota.reserve(p['plan_id'], job['job_id'] + ':' + str(job['attempt_count']), number,
                                                         e.settings.lease_seconds, connection=db)
                except FoodError:
                    budget = e.planner.quota.assess(db, p, e.now())
                    if budget['status'] != 'READY_LOCAL':
                        resets = [instant(line['window_end']) for line in budget['lines'] if line['budget_status'] != 'READY_LOCAL']
                        raise ExecutionError('budget', retry_after=max((r-e.now()).total_seconds() for r in resets)) from None
                    raise ExecutionError('policy') from None
                self.reservation_id = reservation['reservation_id']
            e.planner.quota.mark_attempted(self.reservation_id, connection=db)
            for c in db.execute('SELECT * FROM quota_charges WHERE reservation_id=?', (self.reservation_id,)):
                if instant(c['window_end']) <= e.now():
                    raise ExecutionError('window_changed')
            # Workflow reservation is a ceiling, never permission for an unbounded client retry.
            maximum = p['canonical_query'].get('max_pages', 1) if endpoint in ('search', 'videos', 'recent_media') else 1
            if self.counts.get(endpoint, 0) >= maximum:
                raise ExecutionError('validation')
            call_id = identity('CALL')
            db.execute('''INSERT INTO execution_calls(call_id,job_id,reservation_id,attempt_no,step_key,endpoint,fencing_generation,started_at,status)
                VALUES (?,?,?,?,?,?,?,?,'SENT')''',
                (call_id, job['job_id'], self.reservation_id, job['attempt_count'], step_key, endpoint, self.claim['fencing_generation'], stamp(e.now())))
            db.execute('UPDATE execution_leases SET renewed_at=?,expires_at=? WHERE resource_key=?',
                       (stamp(e.now()), stamp(e.now()+timedelta(seconds=e.settings.lease_seconds)), job['job_id']))
            db.execute('UPDATE execution_runs SET observed_at=coalesce(observed_at,?) WHERE run_id=?', (stamp(e.now()), job['run_id']))
        self.counts[endpoint] = self.counts.get(endpoint, 0) + 1
        return call_id

    def request(self, endpoint, step_key, send, parse, persist, checkpoint):
        self.fault('before_send_intent')
        call_id = self._before(endpoint, step_key)
        self.fault('before_http')
        try:
            payload = send()
            self.fault('after_http')
            parsed = parse(payload)
            next_state = checkpoint(parsed)
            validate_metadata(next_state)
            if set(next_state) - {'phase','page','cursor','hashtag_id','pending_ids','requested_ids','more','limited','done','legacy_tag_id','legacy_query_id'}:
                raise ExecutionError('validation')
            with self.execution.storage.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                job = self._fence(db)
                if self.execution._gate(db, self.plan, job['mode']):
                    raise ExecutionError('policy')
                returned, accepted = persist(db, parsed, self.execution._eligible(db, job, started_only=True))
                db.execute("UPDATE execution_calls SET status='SUCCEEDED',finished_at=?,http_status=200,returned_count=?,accepted_count=? WHERE call_id=?",
                           (stamp(self.execution.now()), returned, accepted, call_id))
                expiry = self.execution.now() + timedelta(days=7)
                db.execute('''INSERT INTO execution_checkpoints VALUES (?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                    state_json=excluded.state_json,updated_at=excluded.updated_at,expires_at=excluded.expires_at''',
                    (job['job_id'], dump(next_state), stamp(self.execution.now()), stamp(expiry)))
                self.fault('before_page_commit')
            self.state = next_state
            self.fault('after_page_commit')
            return parsed
        except LostLease:
            raise
        except Exception as exc:
            kind = 'internal' if isinstance(exc, sqlite3.Error) else getattr(exc, 'kind', 'invalid_response')
            if kind not in ('authentication','permission','quota','rate_limit','upstream','network','invalid_response','validation','policy','no_targets','internal'):
                kind = 'invalid_response'
            with self.execution.storage.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self._fence(db)
                db.execute("UPDATE execution_calls SET status=?,finished_at=?,http_status=?,error_kind=?,retry_after_seconds=? WHERE call_id=?",
                           ('UNKNOWN' if kind in ('network','internal') else 'FAILED', stamp(self.execution.now()), getattr(exc, 'status', None), kind,
                            getattr(exc, 'retry_after', 0), call_id))
                if kind in ('network','internal'):
                    db.execute('UPDATE execution_jobs SET remote_duplicate_possible=1 WHERE job_id=?', (self.claim['job']['job_id'],))
            raise ExecutionError(kind, getattr(exc, 'status', None), getattr(exc, 'retry_after', 0)) from None
