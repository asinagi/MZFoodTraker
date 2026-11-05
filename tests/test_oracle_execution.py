"""ORACLE-RUNTIME-1 S2: the B3 execution contract on the target schema.

Each parity case loads one SQLite fixture into the target with B7, then runs the same mock inputs
through the SQLite ExecutionService and through OracleExecution from the same instant, and compares
logical projections (random ids and wall-clock values excluded). The SQLite stand-in runs by default;
OracleExecutionTests repeats every case on the real disposable Oracle test schema.
"""
import hashlib
import io
import json
import threading
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from food_trends.execution import ExecutionSettings, ExecutionWorker, LostLease
from food_trends.execution_mock import MockResponse, MockTransport, mock_controller
from food_trends.oracle_execution import OracleExecution, oracle_mock_controller
from food_trends.target_db import OracleUnavailable, SQLiteTarget, TargetError
from food_trends.target_migration import migrate
from food_trends.target_repository import TargetRepository
from food_trends.target_schema import TARGET_SCHEMA_VERSION
from food_trends.tracking_service import instant
import test_execution as fixtures
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema
from test_food_entities import snapshot


class Crash(BaseException):
    pass


def crash_at(point):
    def fault(stage):
        if stage == point:
            raise Crash()
    return fault


class NextPage(MockTransport):
    """First search page advertises a next page; later pages end."""
    def __call__(self, request, timeout=None):
        raw = super().__call__(request, timeout)
        if urlsplit(request.full_url).path.endswith('/search') and request.data is None and 'pageToken=' not in request.full_url:
            payload = json.loads(raw.getvalue())
            payload['nextPageToken'] = 'next-page'
            return MockResponse(json.dumps(payload).encode())
        return raw


class SecondPageDown(NextPage):
    def __call__(self, request, timeout=None):
        if 'pageToken=' in request.full_url:
            raise URLError('fixture timeout')
        return super().__call__(request, timeout)


class Down(MockTransport):
    def __call__(self, request, timeout=None):
        self.calls.append(urlsplit(request.full_url).path)
        raise URLError('Bearer synthetic-credential-must-never-persist')


class Limited(MockTransport):
    def __call__(self, request, timeout=None):
        self.calls.append(urlsplit(request.full_url).path)
        raise HTTPError(request.full_url, 429, 'secret upstream error', {'Retry-After': '7200'}, io.BytesIO(b'{}'))


class Unauthorized(MockTransport):
    def __call__(self, request, timeout=None):
        self.calls.append(urlsplit(request.full_url).path)
        raise HTTPError(request.full_url, 401, 'secret upstream error', {}, io.BytesIO(b'{}'))


class OracleExecutionStandInTests(unittest.TestCase):
    setUp_base = fixtures.ExecutionTests.setUp
    tearDown = fixtures.ExecutionTests.tearDown
    create_food = fixtures.ExecutionTests.create_food
    cmd = fixtures.ExecutionTests.cmd
    register = fixtures.ExecutionTests.register
    active = fixtures.ExecutionTests.active
    change = fixtures.ExecutionTests.change
    advance = fixtures.ExecutionTests.advance

    def setUp(self):
        self.setUp_base()
        self.repo = TargetRepository(self.make_target(), clock=lambda: self.now)
        self.repo.migrate()

    def make_target(self):
        return SQLiteTarget(Path(self.temp.name) / 'target.sqlite3')

    # Fixture: one committed plan, copied to the target with B7 before either runtime executes it.

    def plans(self, *sources, options=None):
        """Activate sources, commit one planning round and load it into the target once (before any run)."""
        for source in sources:
            if options:
                old = self.tracking.account(source, 'local-default')
                self.tracking.accounts[(source, 'local-default')] = replace(old, options=options)
            self.target = self.active(source)
        committed = self.planner.commit(uuid.uuid4().hex)['plans']
        migrate(self.storage.path, self.repo, now=self.now)
        return {s: next(p for p in committed if p['source_id'] == s) for s in sources}

    def plan(self, source='youtube', options=None):
        return self.plans(source, options=options)[source]

    def oracle(self, transport=None, settings=None):
        return oracle_mock_controller(self.repo, self.tracking.accounts, lambda: self.now, transport or MockTransport(), settings)

    def sqlite(self, transport=None, settings=None):
        controller = mock_controller(self.storage, self.planner, transport or MockTransport())
        if settings:
            controller.execution.settings = settings
        return controller

    def project(self, item, start):
        def offset(value):
            return None if value is None else (instant(value) - start).total_seconds()
        run, contract = item['run'], dict(item['run']['result_contract'] or {})
        for key in ('run_id', 'job_id', 'observed_at'):
            contract.pop(key, None)
        if contract.get('allowed_result_reference'):
            contract['allowed_result_reference'] = {k: v for k, v in contract['allowed_result_reference'].items() if k not in ('storage', 'run_id', 'receipt_job_id')}
        if 'active_target_ids' in contract:
            contract['active_target_ids'] = sorted(contract['active_target_ids'])
        return dict(
            job=(item['status'], item['mode'], item['attempt_count'], item['max_attempts'], item['error_kind'], bool(item['remote_duplicate_possible']),
                 item['execution_generation'], offset(item['next_retry_at']), item['finished_at'] is not None),
            calls=[(c['endpoint'], c['step_key'], c['status'], c['http_status'], c['returned_count'], c['accepted_count'], c['error_kind'],
                    c['attempt_no'], c['fencing_generation']) for c in item['calls']],
            run=(run['origin'], run['execution_status'], run['result_coverage'], run['committed_pages'], run['returned_count'], run['accepted_count']),
            contract=contract, eligible=sorted(item['eligible_target_ids']), blockers=item['execution_blockers'],
            budget=[(l['resource'], l['cost'], l['observed_usage'], l['reserved_cost'], l['remaining_budget'], l['budget_status'])
                    for l in item['current_budget']['lines']])

    def parity(self, source, scenario, transport=MockTransport, options=None, settings=None, plan=None):
        """Run scenario(controller, job) on both runtimes from the same instant; return both projections."""
        plan = plan or self.plan(source, options)
        start, results = self.now, []
        for build in (self.sqlite, self.oracle):
            self.now = start
            transport_instance = transport()
            controller = build(transport_instance, settings)
            job = controller.execution.enqueue([plan['plan_id']])[0]
            scenario(controller, job)
            item = controller.execution.status(job['job_id'])['items'][0]
            results.append((self.project(item, start), len(transport_instance.calls), controller, item))
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(results[0][1], results[1][1], 'provider request counts differ')
        return results

    # Scenarios 1-6: job creation, Outbox delivery, lease/claim, mock responses, completion.

    def test_mock_execution_matches_sqlite_for_three_sources(self):
        plans = self.plans('youtube', 'instagram_graph', 'naver_datalab')
        for source, plan in plans.items():
            with self.subTest(source=source):
                (sqlite, _, _, _), (oracle, calls, controller, item) = self.parity(source, lambda c, job: c.run_once(job['job_id']), plan=plan)
                self.assertEqual(oracle['job'][0], 'SUCCEEDED')
                self.assertGreater(calls, 0)
                self.assertEqual(item['run']['origin'], 'mock')
        with self.repo.db.transaction() as tx:
            self.assertEqual(tx.scalar("SELECT count(*) FROM source_contents WHERE source_code='naver_datalab'"), 0)
            self.assertEqual(tx.scalar('SELECT count(*) FROM execution_outbox WHERE delivered_at IS NULL'), 0)
            self.assertEqual(tx.scalar('SELECT sum(delivery_count) FROM execution_outbox'), 3)
            self.assertEqual(tx.scalar('SELECT count(*) FROM execution_results'), 3)
            self.assertEqual(tx.scalar('SELECT count(*) FROM tracking_targets WHERE last_success_at IS NOT NULL'), 3)

    def test_source_contents_match_the_sqlite_source_stores(self):
        plans = self.plans('youtube', 'instagram_graph')
        for source, plan in plans.items():
            self.parity(source, lambda c, job: c.run_once(job['job_id']), plan=plan)
        with self.storage.reader() as db:
            video = dict(db.execute('SELECT * FROM youtube_videos').fetchone())
            media = dict(db.execute('SELECT * FROM instagram_media').fetchone())
            receipts = sorted((r[0], r[1]) for r in db.execute('SELECT item_id,expires_at FROM execution_item_receipts'))
        contents = {r['payload_ref']: r for r in self.repo.rows('source_contents')}
        stored = contents['youtube_videos:' + video['video_id']]
        self.assertEqual(json.loads(stored['content_json']), json.loads(video['payload']))
        self.assertEqual(stored['source_payload_hash'], hashlib.sha256(video['payload'].encode()).hexdigest())
        self.assertEqual((instant(stored['expires_at']), instant(stored['captured_at'])), (instant(video['expires_at']), instant(video['collected_at'])))
        self.assertEqual(stored['origin'], 'mock')
        ig = json.loads(contents['instagram_media:' + media['id']]['content_json'])
        self.assertEqual({k: ig[k] for k in ('id', 'published_at', 'media_type', 'permalink', 'caption')},
                         {k: media[k] for k in ('id', 'published_at', 'media_type', 'permalink', 'caption')})
        self.assertEqual(ig['caption_tags'], ['두쫀쿠', '디저트'])
        target_receipts = sorted((r['item_id'], instant(r['expires_at'])) for r in self.repo.rows('execution_item_receipts'))
        self.assertEqual([(i, instant(e)) for i, e in receipts], target_receipts)

    def test_enqueue_is_idempotent_and_atomic(self):
        plan = self.plan()
        execution = self.oracle().execution
        job = execution.enqueue([plan['plan_id']])[0]
        self.assertEqual((job['status'], job['attempt_count'], job['mode']), ('READY', 0, 'mock'))
        self.assertEqual(execution.enqueue([plan['plan_id'], plan['plan_id']])[0]['job_id'], job['job_id'])
        counts = {t: self.repo.count(t) for t in ('execution_jobs', 'execution_outbox', 'execution_runs', 'execution_mappings')}
        self.assertEqual(counts, {'execution_jobs': 1, 'execution_outbox': 1, 'execution_runs': 1, 'execution_mappings': 1})

    # Scenario 7: duplicate delivery never repeats HTTP.

    def test_redelivery_after_completion_sends_nothing(self):
        def twice(controller, job):
            controller.run_once(job['job_id'])
            self.assertIsNone(controller.run_once(job['job_id']))
            self.assertIsNone(controller.execution.claim('late-worker', job['job_id']))
        self.parity('youtube', twice)

    def test_duplicate_pages_have_one_receipt(self):
        class Repeat(NextPage):
            pass
        (_, _, _, _), (oracle, _, _, item) = self.parity('youtube', lambda c, job: c.run_once(job['job_id']), Repeat, options={'max_pages': 2})
        self.assertEqual((item['status'], item['run']['accepted_count']), ('SUCCEEDED', 1))
        self.assertEqual(self.repo.count('execution_item_receipts'), 1)

    # Scenario 8: worker stop and restart.

    def test_crash_after_http_is_uncertain_and_resumes(self):
        def scenario(controller, job):
            with self.assertRaises(Crash):
                ExecutionWorker(controller.execution, controller.worker.adapters, fault=crash_at('after_http')).run_once(job['job_id'])
            self.advance()
            controller.run_once(job['job_id'])
        (_, _, _, _), (oracle, _, _, item) = self.parity('youtube', scenario)
        self.assertEqual(item['status'], 'SUCCEEDED')
        self.assertTrue(item['remote_duplicate_possible'])
        self.assertEqual([c['status'] for c in item['calls']], ['UNKNOWN', 'SUCCEEDED', 'SUCCEEDED'])

    def test_crash_after_page_commit_reuses_checkpoint(self):
        def scenario(controller, job):
            with self.assertRaises(Crash):
                ExecutionWorker(controller.execution, controller.worker.adapters, fault=crash_at('after_page_commit')).run_once(job['job_id'])
            self.advance()
            controller.run_once(job['job_id'])
        (_, _, _, _), (oracle, _, _, item) = self.parity('youtube', scenario)
        self.assertEqual([c['step_key'] for c in item['calls']], ['search:0', 'videos:0'])
        self.assertEqual(item['attempt_count'], 2)

    def test_send_intent_crash_on_final_attempt_fails_with_uncertainty(self):
        def scenario(controller, job):
            with self.assertRaises(Crash):
                ExecutionWorker(controller.execution, controller.worker.adapters, fault=crash_at('before_http')).run_once(job['job_id'])
            self.advance()
            self.assertIsNone(controller.run_once(job['job_id']))
        (_, _, _, _), (oracle, calls, _, item) = self.parity('youtube', scenario, settings=ExecutionSettings(max_attempts=1))
        self.assertEqual((item['status'], item['error_kind'], calls), ('FAILED', 'MAX_ATTEMPTS', 0))
        self.assertTrue(item['remote_duplicate_possible'])

    # Scenario 9: fencing.

    def test_stale_owner_cannot_renew_or_finish(self):
        def scenario(controller, job):
            execution = controller.execution
            a = execution.claim('A', job['job_id'])
            execution.begin(a)
            self.advance()
            b = execution.claim('B', job['job_id'])
            self.assertEqual((a['fencing_generation'], b['fencing_generation']), (1, 2))
            for action in (lambda: execution.renew(a), lambda: execution.finish(a, 'SUCCEEDED'), lambda: execution.fail(a, URLError('x'))):
                with self.assertRaises(LostLease):
                    action()
            self.assertIsNone(execution.claim('C', job['job_id']))
        self.parity('youtube', scenario)

    def test_lost_lease_during_page_is_rejected_without_commit(self):
        def scenario(controller, job):
            def takeover(stage):
                if stage == 'after_http':
                    self.advance()
                    controller.execution.claim('B', job['job_id'])
            result = ExecutionWorker(controller.execution, controller.worker.adapters, owner='A', fault=takeover).run_once(job['job_id'])
            self.assertEqual(result['status'], 'STALE_OWNER_REJECTED')
        (_, _, _, _), (oracle, _, _, item) = self.parity('youtube', scenario)
        self.assertEqual(item['status'], 'CLAIMED')
        self.assertEqual(self.repo.count('source_contents'), 0)

    # Scenario 10: budget shortage and retry.

    def test_budget_shortage_defers_without_http(self):
        (_, _, _, _), (oracle, calls, _, item) = self.parity('youtube', lambda c, job: c.run_once(job['job_id']),
                                                              options={'max_pages': 2, 'daily_search_limit': 1})
        self.assertEqual((item['status'], item['error_kind'], calls), ('RETRY_WAIT', 'budget', 0))
        self.assertEqual(item['current_budget']['status'], 'DEFERRED')

    def test_network_retry_exhaustion_fails_with_uncertainty(self):
        def scenario(controller, job):
            for _ in range(3):
                controller.run_once(job['job_id'])
                self.advance(600)
        (_, _, _, _), (oracle, calls, _, item) = self.parity('youtube', scenario, Down)
        self.assertEqual((item['status'], calls), ('FAILED', 3))
        self.assertTrue(item['remote_duplicate_possible'])
        self.assertNotIn('synthetic-credential', json.dumps(self.repo.rows('execution_calls')))

    def test_partial_coverage_when_retries_exhaust_after_committed_page(self):
        def scenario(controller, job):
            for _ in range(3):
                controller.run_once(job['job_id'])
                self.advance(600)
        (_, _, _, _), (oracle, _, _, item) = self.parity('youtube', scenario, SecondPageDown, options={'max_pages': 2})
        self.assertEqual((item['status'], item['run']['result_coverage'], item['run']['accepted_count']), ('PARTIAL', 'PARTIAL', 1))

    def test_retry_after_and_account_blocks(self):
        (_, _, _, _), (oracle, _, _, item) = self.parity('youtube', lambda c, job: c.run_once(job['job_id']), Limited)
        self.assertEqual(item['status'], 'RETRY_WAIT')
        self.assertGreaterEqual(oracle['job'][7], 7200)
        self.assertEqual(item['execution_blockers'], ['ACCOUNT_BLOCKED_rate_limit'])

    def test_authentication_blocks_the_account(self):
        (_, _, _, _), (oracle, _, _, item) = self.parity('youtube', lambda c, job: c.run_once(job['job_id']), Unauthorized)
        self.assertEqual((item['status'], item['error_kind']), ('BLOCKED', 'authentication'))
        self.assertEqual(item['execution_blockers'], ['ACCOUNT_BLOCKED_authentication'])

    def test_limited_page_cap(self):
        (_, _, _, _), (oracle, _, _, item) = self.parity('youtube', lambda c, job: c.run_once(job['job_id']), NextPage)
        self.assertEqual((item['status'], item['run']['result_coverage']), ('LIMITED', 'LIMITED'))

    def test_paused_target_cancels_before_budget_or_http(self):
        def scenario(controller, job):
            if isinstance(controller.execution, OracleExecution):
                with self.repo.db.transaction() as tx:
                    tx.execute("UPDATE tracking_targets SET status='PAUSED'")
            else:
                self.change('pause', self.tracking.target(self.target['target_id']))
            controller.run_once(job['job_id'])
        (_, _, _, _), (oracle, calls, _, item) = self.parity('youtube', scenario)
        self.assertEqual((item['status'], item['error_kind'], calls), ('CANCELLED', 'NO_ACTIVE_TARGETS', 0))
        self.assertEqual(self.repo.count('quota_reservations'), 0)

    # Scenario 4: two workers claim the same job concurrently.

    def test_two_workers_claim_the_same_job_once(self):
        plan = self.plan()
        execution = self.oracle().execution
        job = execution.enqueue([plan['plan_id']])[0]
        held, finished, results = threading.Event(), threading.Event(), {}

        def hold(job_id):
            held.set()
            finished.wait(3)  # Oracle: B skips the locked row and finishes; the stand-in blocks B until A commits
        execution.claim_locked = hold

        def first():
            results['A'] = execution.claim('A', job['job_id'])

        def second():
            held.wait(5)
            other = OracleExecution(self.repo, self.tracking.accounts, lambda: self.now)
            results['B'] = other.claim('B', job['job_id'])
            results['B_while_locked'] = not finished.is_set() and not results.get('A')
            finished.set()
        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertIsNotNone(results['A'])
        self.assertIsNone(results['B'])
        if self.repo.db.dialect == 'oracle':
            self.assertTrue(results['B_while_locked'], 'SKIP LOCKED must not wait for the holder')
        self.assertEqual(execution.status(job['job_id'])['items'][0]['attempt_count'], 1)
        with self.repo.db.transaction() as tx:
            self.assertEqual(tx.scalar('SELECT fencing_generation FROM execution_leases'), 1)
            self.assertEqual(tx.scalar('SELECT delivery_count FROM execution_outbox'), 1)

    def test_parallel_workers_drain_distinct_jobs(self):
        for source in ('youtube', 'naver_datalab', 'instagram_graph'):
            self.active(source)
        plans = self.planner.commit(uuid.uuid4().hex)['plans']
        migrate(self.storage.path, self.repo, now=self.now)
        execution = self.oracle().execution
        jobs = execution.enqueue([p['plan_id'] for p in plans if p['source_id'] != 'google_trends_rss' and p['decision'] != 'NOT_DUE'])
        claims, lock = [], threading.Lock()

        def worker(name):
            own = OracleExecution(self.repo, self.tracking.accounts, lambda: self.now)
            while True:
                claim = own.claim(name)
                if claim is None:
                    return
                with lock:
                    claims.append(claim['job']['job_id'])
        threads = [threading.Thread(target=worker, args=(n,)) for n in ('W1', 'W2', 'W3')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(sorted(claims), sorted(j['job_id'] for j in jobs))

    # Runtime safety: no SQLite writes, no DDL at construction, mock only, production disabled.

    def test_oracle_run_writes_nothing_to_sqlite(self):
        plan = self.plan('youtube')
        before = snapshot(self.storage)
        controller = self.oracle()
        job = controller.execution.enqueue([plan['plan_id']])[0]
        self.assertEqual(controller.run_once(job['job_id'])['status'], 'SUCCEEDED')
        self.assertEqual(snapshot(self.storage), before)

    def test_runtime_never_runs_ddl_and_refuses_unsafe_modes(self):
        empty = TargetRepository(SQLiteTarget(Path(self.temp.name) / 'empty.sqlite3'), clock=lambda: self.now)
        with self.assertRaises(TargetError) as error:
            OracleExecution(empty, self.tracking.accounts)
        self.assertEqual(error.exception.code, 'target_schema_version_mismatch')
        self.assertEqual(empty.schema_version(), 0)
        with self.assertRaises(ValueError):
            OracleExecution(self.repo, self.tracking.accounts, settings=ExecutionSettings(live_enabled=True))

        class Production(SQLiteTarget):
            settings = type('S', (), {'environment': 'production'})()
        with self.assertRaises(OracleUnavailable):
            OracleExecution(TargetRepository(Production(Path(self.temp.name) / 'p.sqlite3')), self.tracking.accounts)
        plan = self.plan()
        jobs = self.repo.count('execution_jobs')
        result = self.oracle().execution.enqueue([plan['plan_id']], mode='live')[0]
        self.assertEqual(result['status'], 'BLOCKED')
        # A mock runtime never runs a live job: live is off here and the source was never transferred from its legacy writer.
        self.assertTrue({'LIVE_DISABLED_OR_LEGACY_WRITER_NOT_TRANSFERRED', 'LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED'} <= set(result['reasons']))
        self.assertEqual(self.repo.count('execution_jobs'), jobs)

    def test_schema_v1_upgrades_explicitly(self):
        from food_trends.target_schema import TABLES
        oracle = self.repo.db.dialect == 'oracle'
        with self.repo.db.transaction() as tx:
            # Recreate a v1 schema: drop every later table (children first) and the v2 column.
            for spec in reversed([t for t in TABLES if t.meta.get('since', 1) > 1]):
                tx.execute('DROP TABLE %s%s' % (spec.name, ' CASCADE CONSTRAINTS PURGE' if oracle else ''))
            tx.execute('ALTER TABLE source_contents DROP COLUMN source_payload_hash')
            tx.execute('DELETE FROM target_schema_migrations')
            tx.execute("INSERT INTO target_schema_migrations VALUES (1,'v1',%s)" % ('SYSTIMESTAMP' if oracle else "'2026-11-03T00:00:00+00:00'"))
        self.assertEqual(self.repo.schema_version(), 1)
        with self.assertRaises(TargetError):
            OracleExecution(self.repo, self.tracking.accounts)
        result = self.repo.migrate()
        self.assertEqual((result['version'], result['applied']), (TARGET_SCHEMA_VERSION, list(range(2, TARGET_SCHEMA_VERSION + 1))))
        self.assertEqual(self.repo.migrate()['applied'], [])
        OracleExecution(self.repo, self.tracking.accounts)


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleExecutionTests(OracleExecutionStandInTests):
    """Every case above on the real disposable Oracle test schema (row locks, TSTZ, CLOB)."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db
