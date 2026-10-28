"""B3 infrastructure: crash injection, fenced commits and durable delivery without network."""
import json
import sqlite3
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from food_trends.execution import ExecutionService, ExecutionSettings, ExecutionWorker, ExecutionError, LostLease, RequestSession
from food_trends.migrations import migrate
from food_trends.query_planner import QueryPlanner
from food_trends.tracking_service import instant
import test_tracking as tracking_fixtures
from test_food_entities import snapshot


class Crash(BaseException):
    pass


class FakeAdapter:
    def __init__(self, callback=None):
        self.calls = 0
        self.callback = callback

    def execute(self, execution, claim, plan, fault):
        session = RequestSession(execution, claim, plan, fault)
        if not session.state.get('done'):
            def send():
                self.calls += 1
                if self.callback:
                    self.callback()
                return []
            session.request('search', 'page:0', send, lambda payload: payload, lambda db, rows, targets: (0, 0), lambda rows: {'done': True})
        return {'status': 'SUCCEEDED'}


class ExecutionTests(unittest.TestCase):
    create_food = tracking_fixtures.TrackingTests.create_food
    cmd = tracking_fixtures.TrackingTests.cmd
    register = tracking_fixtures.TrackingTests.register
    active = tracking_fixtures.TrackingTests.active
    change = tracking_fixtures.TrackingTests.change
    tearDown = tracking_fixtures.TrackingTests.tearDown

    def setUp(self):
        tracking_fixtures.TrackingTests.setUp(self)
        self.planner = QueryPlanner(self.storage, self.tracking)
        self.execution = ExecutionService(self.storage, self.planner)
        self.adapter = FakeAdapter()
        self.worker = ExecutionWorker(self.execution, {('youtube', 'local-default'): self.adapter})

    def job(self, source='youtube'):
        self.target = self.active(source)
        plans = self.planner.commit(uuid.uuid4().hex)
        plan = next(p for p in plans['plans'] if p['source_id']==source)
        return self.execution.enqueue([plan['plan_id']])[0]

    def row(self, job):
        return self.execution.status(job['job_id'])['items'][0]

    def advance(self, seconds=121):
        self.now += timedelta(seconds=seconds)

    def test_schedule_creates_atomic_job_outbox_and_advances_due(self):
        self.active()
        jobs = self.execution.schedule()
        self.assertEqual(len(jobs), 3)
        self.assertEqual(self.tracking.due(), [])
        self.assertEqual(self.execution.schedule(), [])
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM execution_outbox').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM tracking_slots').fetchone()[0], 1)
        self.assertEqual(self.adapter.calls, 0)

    def test_enqueue_same_plan_and_outbox_delivery_do_not_repeat_http(self):
        job=self.job()
        self.assertEqual(self.execution.enqueue([job['plan_id']])[0]['job_id'], job['job_id'])
        self.assertEqual(self.worker.run_once(job['job_id'])['status'], 'SUCCEEDED')
        self.assertIsNone(self.worker.run_once(job['job_id']))
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.row(job)['run']['result_contract']['returned_count'], 0)

    def test_read_status_and_preview_do_not_write(self):
        job=self.job();before=snapshot(self.storage)
        self.execution.status();self.execution.status(job['job_id']);self.planner.preview()
        self.assertEqual(snapshot(self.storage),before)

    def test_worker_claim_is_atomic_across_connections(self):
        job=self.job()
        with ThreadPoolExecutor(2) as pool:
            claims=list(pool.map(lambda owner:self.execution.claim(owner, job['job_id']), ('a','b')))
        self.assertEqual(sum(c is not None for c in claims),1)

    def test_lease_expiry_rejects_old_owner_renew_and_finish(self):
        job=self.job();a=self.execution.claim('A',job['job_id']);self.execution.begin(a)
        self.advance();b=self.execution.claim('B',job['job_id'])
        self.assertGreater(b['fencing_generation'],a['fencing_generation'])
        for action in (lambda:self.execution.renew(a),lambda:self.execution.finish(a,'SUCCEEDED')):
            with self.assertRaises(LostLease):action()

    def test_claim_crash_resumes_without_http(self):
        job=self.job()
        with self.assertRaises(Crash):
            ExecutionWorker(self.execution,self.worker.adapters,fault=lambda stage:(_ for _ in ()).throw(Crash()) if stage=='after_claim' else None).run_once(job['job_id'])
        self.advance();result=self.worker.run_once(job['job_id'])
        self.assertEqual(result['status'],'SUCCEEDED');self.assertEqual(self.adapter.calls,1)

    def test_send_intent_crash_retains_unknown_debit_and_retries_new_reservation(self):
        job=self.job()
        with self.assertRaises(Crash):
            ExecutionWorker(self.execution,self.worker.adapters,fault=lambda stage:(_ for _ in ()).throw(Crash()) if stage=='before_http' else None).run_once(job['job_id'])
        self.assertEqual(self.adapter.calls,0)
        self.advance();result=self.worker.run_once(job['job_id'])
        self.assertEqual(result['status'],'SUCCEEDED');self.assertTrue(result['remote_duplicate_possible'])
        self.assertEqual([c['status'] for c in result['calls']],['UNKNOWN','SUCCEEDED'])
        with self.storage.reader() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM quota_reservations WHERE state='ATTEMPTED'").fetchone()[0],2)

    def test_http_response_crash_is_explicitly_uncertain(self):
        job=self.job()
        with self.assertRaises(Crash):
            ExecutionWorker(self.execution,self.worker.adapters,fault=lambda stage:(_ for _ in ()).throw(Crash()) if stage=='after_http' else None).run_once(job['job_id'])
        self.advance();result=self.worker.run_once(job['job_id'])
        self.assertTrue(result['remote_duplicate_possible']);self.assertEqual(self.adapter.calls,2)

    def test_interrupted_send_on_final_attempt_is_not_hidden_when_job_fails(self):
        crash=lambda stage:(_ for _ in ()).throw(Crash()) if stage=='before_http' else None
        self.execution=ExecutionService(self.storage,self.planner,ExecutionSettings(max_attempts=1))
        worker=ExecutionWorker(self.execution,self.worker.adapters);job=self.job()
        with self.assertRaises(Crash):
            ExecutionWorker(self.execution,self.worker.adapters,fault=crash).run_once(job['job_id'])
        self.advance();self.assertIsNone(worker.run_once(job['job_id']))
        result=self.row(job)
        self.assertEqual((result['status'],result['error_kind']),('FAILED','MAX_ATTEMPTS'))
        self.assertTrue(result['remote_duplicate_possible'])
        self.assertEqual([c['status'] for c in result['calls']],['UNKNOWN'])
        self.assertEqual(self.adapter.calls,0)

    def test_final_attempt_crash_after_clean_failures_keeps_uncertainty(self):
        job=self.job();real=self.adapter.execute
        self.adapter.execute=lambda *args:(_ for _ in ()).throw(ExecutionError('upstream',503))
        for _ in range(2):
            self.worker.run_once(job['job_id']);self.advance(600)
        self.assertFalse(self.row(job)['remote_duplicate_possible'])
        self.adapter.execute=real
        with self.assertRaises(Crash):
            ExecutionWorker(self.execution,self.worker.adapters,
                fault=lambda stage:(_ for _ in ()).throw(Crash()) if stage=='before_http' else None).run_once(job['job_id'])
        self.advance();self.assertIsNone(self.worker.run_once(job['job_id']))
        result=self.row(job)
        self.assertEqual((result['status'],result['attempt_count']),('FAILED',3))
        self.assertTrue(result['remote_duplicate_possible'])
        self.assertNotIn('SENT',[c['status'] for c in result['calls']])

    def test_page_commit_then_crash_reuses_checkpoint_without_http_or_quota(self):
        job=self.job()
        with self.assertRaises(Crash):
            ExecutionWorker(self.execution,self.worker.adapters,fault=lambda stage:(_ for _ in ()).throw(Crash()) if stage=='after_page_commit' else None).run_once(job['job_id'])
        self.advance();result=self.worker.run_once(job['job_id'])
        self.assertEqual(result['status'],'SUCCEEDED');self.assertEqual(self.adapter.calls,1)
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM quota_reservations').fetchone()[0],1)

    def test_database_commit_failure_rolls_back_page_then_recovers(self):
        job=self.job()
        def failure(stage):
            if stage=='before_page_commit':raise sqlite3.OperationalError('fixture commit failure')
        result=ExecutionWorker(self.execution,self.worker.adapters,fault=failure).run_once(job['job_id'])
        self.assertEqual(result['status'],'RETRY_WAIT');self.assertEqual(result['calls'][0]['status'],'UNKNOWN')
        self.advance();self.assertEqual(self.worker.run_once(job['job_id'])['status'],'SUCCEEDED')

    def test_shared_query_stop_one_target_keeps_other_result_mapping(self):
        one=self.active();otherfood=self.create_food('다른 음식',['두쫀쿠'],scope='food:second')
        other=self.cmd('register',food_id=otherfood['food_id'],source_id='youtube')
        other=self.change('activate',other,approved=True)
        p=next(p for p in self.planner.commit('shared')['plans'] if p['canonical_query']['parameters']['q']=='두쫀쿠')
        job=self.execution.enqueue([p['plan_id']])[0]
        self.adapter.callback=lambda:self.change('stop',self.tracking.target(one['target_id']))
        result=self.worker.run_once(job['job_id'])
        self.assertEqual(result['run']['result_contract']['active_target_ids'],[other['target_id']])
        self.assertEqual(self.adapter.calls,1)
        with self.storage.reader() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT target_id FROM execution_results')],[other['target_id']])

    def test_pause_before_claim_cancels_without_budget_or_http(self):
        job=self.job();self.change('pause',self.tracking.target(self.target['target_id']))
        self.assertEqual(self.worker.run_once(job['job_id'])['status'],'CANCELLED')
        self.assertEqual(self.adapter.calls,0)

    def test_policy_change_blocks_before_http(self):
        job=self.job();account=self.tracking.account('youtube','local-default')
        self.tracking.accounts[('youtube','local-default')]=replace(account,policy_allowed=False)
        self.assertEqual(self.worker.run_once(job['job_id'])['status'],'BLOCKED')
        self.assertEqual(self.adapter.calls,0)

    def test_live_execution_default_disabled_and_rss_remains_legacy_owned(self):
        self.active();plans=self.planner.commit('discovery',options={'include_discovery':True})['plans']
        rss=next(p for p in plans if p['source_id']=='google_trends_rss')
        self.assertEqual(self.execution.enqueue([rss['plan_id']])[0]['status'],'BLOCKED')
        yt=next(p for p in plans if p['source_id']=='youtube')
        self.assertEqual(self.execution.enqueue([yt['plan_id']],mode='live')[0]['status'],'BLOCKED')
        self.assertFalse(self.execution.settings.automatic)

    def test_authentication_blocks_entire_account(self):
        job=self.job();self.adapter.callback=lambda:(_ for _ in ()).throw(ExecutionError('authentication',401))
        self.assertEqual(self.worker.run_once(job['job_id'])['status'],'BLOCKED')
        self.assertTrue(self.execution.status()['items'][0]['execution_blockers'])

    def test_retry_after_is_not_clamped(self):
        job=self.job();self.adapter.callback=lambda:(_ for _ in ()).throw(ExecutionError('rate_limit',429,7200))
        result=self.worker.run_once(job['job_id'])
        self.assertEqual(result['status'],'RETRY_WAIT')
        self.assertGreaterEqual((instant(result['next_retry_at'])-self.now).total_seconds(),7200)
        self.assertIsNone(self.worker.run_once(job['job_id']))

    def test_retry_exhaustion_and_unknown_network_are_not_success(self):
        job=self.job();self.adapter.callback=lambda:(_ for _ in ()).throw(ExecutionError('network'))
        for _ in range(3):
            result=self.worker.run_once(job['job_id']);self.advance()
        self.assertEqual(result['status'],'FAILED');self.assertTrue(result['remote_duplicate_possible'])
        self.assertEqual(self.adapter.calls,3)

    def test_migration_six_reexecution_and_relational_checks(self):
        self.assertEqual(migrate(self.storage.path)['applied'],[])
        with self.storage.reader() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
