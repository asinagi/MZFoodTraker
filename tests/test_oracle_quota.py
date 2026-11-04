"""ORACLE-RUNTIME-2 R4: legacy usage ledger, budget coverage and reservation safety on the target.

Legacy collector calls are written to the SQLite call tables (as the collectors do), imported with
their evidence, and the Oracle assessment is compared with the SQLite QuotaManager on the same plans.
"""
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from food_trends.execution_mock import MockTransport
from food_trends.food_service import FoodError
from food_trends.oracle_execution import OracleExecution, oracle_mock_controller
from food_trends.quota_ledger import import_legacy_usage
from food_trends.target_migration import migrate
import test_oracle_execution as s2
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema


class OracleQuotaStandInTests(unittest.TestCase):
    setUp_base = s2.OracleExecutionStandInTests.setUp_base
    setUp = s2.OracleExecutionStandInTests.setUp
    tearDown = s2.OracleExecutionStandInTests.tearDown
    make_target = s2.OracleExecutionStandInTests.make_target
    create_food = s2.OracleExecutionStandInTests.create_food
    cmd = s2.OracleExecutionStandInTests.cmd
    register = s2.OracleExecutionStandInTests.register
    active = s2.OracleExecutionStandInTests.active
    change = s2.OracleExecutionStandInTests.change
    advance = s2.OracleExecutionStandInTests.advance

    def set_account(self, source, scope='local-default', **options):
        old = self.tracking.account(source, 'local-default')
        self.tracking.accounts[(source, scope)] = replace(old, account_scope=scope, options=dict(old.options, **options))

    def legacy_youtube(self, status='success', origin='live_api', endpoint='search', day='2026-10-26'):
        with self.storage.connect() as db:
            db.execute('INSERT INTO youtube_calls(day,endpoint,started_at,finished_at,status,origin) VALUES (?,?,?,?,?,?)',
                       (day, endpoint, self.now.isoformat(), None if status == 'running' else self.now.isoformat(), status, origin))

    def load(self, *sources):
        self.targets = {s: self.active(s) for s in sources}
        migrate(self.storage.path, self.repo, now=self.now)

    def controller(self, transport=None, verified=False):
        controller = oracle_mock_controller(self.repo, self.tracking.accounts, lambda: self.now, transport or MockTransport())
        controller.execution.require_verified_usage = verified
        return controller

    def import_usage(self, **kw):
        return import_legacy_usage(self.storage.path, self.repo, now=self.now, **kw)

    def youtube_lines(self, preview):
        return sorted((l['resource'], l['observed_usage'], l['reserved_cost'], l['remaining_budget'], l['budget_status'])
                      for p in preview['plans'] if p['source_id'] == 'youtube' for l in p['budget']['lines'])

    # 17, 18: the imported ledger reproduces the SQLite budget; unknown origins and unfinished calls are counted.

    def test_legacy_usage_and_new_reservations_match_sqlite(self):
        self.set_account('youtube', daily_search_limit=3)
        self.legacy_youtube()  # a completed, counted call
        self.legacy_youtube(status='running', origin='legacy_unknown')  # origin and outcome unknown
        self.legacy_youtube(day='2026-10-20')  # a closed window: irrelevant, not copied
        self.load('youtube')
        result = self.import_usage()
        self.assertEqual((result['rows'], result['by_evidence']['LEGACY_LOCAL_COUNT'], result['by_evidence']['LEGACY_UNKNOWN_ORIGIN'],
                          result['by_evidence']['PROVIDER_CONFIRMED']), (2, 1, 1, 0))
        oracle = self.controller()
        sqlite_preview, oracle_preview = self.planner.preview(), oracle.execution.planner.preview()
        self.assertEqual(oracle_preview, sqlite_preview)
        self.assertIn(('youtube.search_calls', 2, 0, 1, 'READY_LOCAL'), self.youtube_lines(oracle_preview))
        plan = self.planner.commit('one')['plans'][0]
        self.assertEqual(oracle.execution.planner.commit('one')['plans'][0]['plan_id'], plan['plan_id'])
        self.planner.quota.reserve(plan['plan_id'], 'attempt-1')
        with oracle.execution.transaction() as t:
            oracle.execution.quota.reserve(plan['plan_id'], 'attempt-1', connection=t)
        self.assertEqual(self.youtube_lines(oracle.execution.planner.preview()), self.youtube_lines(self.planner.preview()))
        with oracle.execution.reader() as t:
            usage = oracle.execution.quota.usage(t, plan, self.now)
        line = next(l for l in usage['lines'] if l['resource'] == 'youtube.search_calls')
        self.assertEqual((line['legacy_usage'], line['reserved_cost'], line['remaining_budget']), (2, 1, 0))
        self.assertEqual(line['legacy_evidence'], ['LEGACY_LOCAL_COUNT', 'LEGACY_UNKNOWN_ORIGIN'])
        self.assertEqual((line['usage_confidence'], usage['usage_verified']), ('IMPORTED_LEGACY_WITH_UNCERTAIN', True))
        self.assertEqual((line['provider_timezone'], line['provider_window'], line['provider_usage_verified']),
                         ('America/Los_Angeles', line['local_window'], False))

    # 18, 19: unknown or stale external usage blocks execution before any HTTP; a full budget defers it.

    def test_unverified_or_stale_usage_blocks_execution_without_http(self):
        self.load('youtube')
        transport = MockTransport()
        strict = self.controller(transport, verified=True)
        jobs = strict.execution.schedule()
        self.assertTrue(jobs and all(j['status'] == 'BLOCKED' and 'EXTERNAL_USAGE_UNVERIFIED' in j['reasons'] for j in jobs))
        self.import_usage()
        self.now += timedelta(minutes=16)  # the import is older than the freshness window
        with strict.execution.reader() as t:
            self.assertEqual(strict.execution.quota.coverage(t, 'youtube', 'local-default', self.now), 'STALE')
        self.assertTrue(all(j['status'] == 'BLOCKED' for j in strict.execution.enqueue([j['plan_id'] for j in jobs])))
        self.import_usage()
        accepted = strict.execution.enqueue([j['plan_id'] for j in jobs])
        self.assertTrue(all(j['status'] == 'READY' for j in accepted))
        self.assertEqual(transport.calls, [])
        # Usage known through the job's start, then the import goes stale while it waits: refused at begin, still no HTTP.
        self.now += timedelta(minutes=16)
        result = strict.run_once(accepted[0]['job_id'])
        self.assertEqual((result['status'], result['error_kind']), ('BLOCKED', 'EXTERNAL_USAGE_UNVERIFIED'))
        self.assertEqual(transport.calls, [])

    def test_full_budget_defers_without_http(self):
        self.set_account('youtube', daily_search_limit=1)
        self.legacy_youtube()
        self.load('youtube')
        self.import_usage()
        transport = MockTransport()
        controller = self.controller(transport, verified=True)
        jobs = controller.execution.schedule()
        results = [controller.run_once(j['job_id']) for j in jobs]
        self.assertEqual({(r['status'], r['error_kind']) for r in results}, {('RETRY_WAIT', 'budget')})
        self.assertEqual((transport.calls, self.repo.count('quota_reservations')), ([], 0))

    # 20: no duplicate ledger rows, reservations or charges; the last budget unit goes to exactly one plan.

    def test_imports_and_reservations_are_not_duplicated(self):
        self.set_account('youtube', daily_search_limit=2)
        self.legacy_youtube()
        self.load('youtube')
        first, second = self.import_usage(), self.import_usage()
        self.assertEqual((first['writes'], second['writes']), ({'inserted': 1}, {'unchanged': 1}))
        self.assertEqual(self.repo.count('quota_usage_ledger'), 1)
        controller = self.controller()
        plans = controller.execution.planner.commit('plans')['plans']
        self.assertEqual(len(plans), 3)
        results, barrier = {}, threading.Barrier(2)

        def reserve(plan):
            execution = OracleExecution(self.repo, self.tracking.accounts, lambda: self.now)
            barrier.wait(10)
            try:
                with execution.transaction() as t:
                    results[plan['plan_id']] = execution.quota.reserve(plan['plan_id'], 'race-' + plan['plan_id'], connection=t)['state']
            except FoodError as exc:
                results[plan['plan_id']] = exc.status
        threads = [threading.Thread(target=reserve, args=(p,)) for p in plans[:2]]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(sorted(results.values(), key=str), sorted(['RESERVED', 409], key=str))
        winner = next(p for p in plans[:2] if results[p['plan_id']] == 'RESERVED')
        with controller.execution.transaction() as t:  # replaying the same attempt key returns the same reservation
            again = controller.execution.quota.reserve(winner['plan_id'], 'race-' + winner['plan_id'], connection=t)
        with self.repo.db.transaction() as tx:
            self.assertEqual(again['reservation_id'], tx.scalar("SELECT reservation_id FROM quota_reservations WHERE state='RESERVED'"))
            self.assertEqual(tx.scalar("SELECT count(*) FROM quota_reservations WHERE state='RESERVED'"), 1)
            self.assertEqual(tx.scalar('SELECT count(*) FROM quota_charges'), tx.scalar('SELECT count(DISTINCT reservation_id) FROM quota_charges') * 2)

    # 21: usage of another account scope is never merged.

    def test_account_scopes_keep_separate_budgets(self):
        for scope in ('ig:1001', 'ig:1002'):
            self.set_account('instagram_graph', scope)
        with self.storage.connect() as db:
            for user, tag in (('1001', '#두쫀쿠'), ('1002', '말차라떼'), ('1002', '두쫀쿠')):
                db.execute('INSERT INTO instagram_searches(user_id,name,searched_at) VALUES (?,?,?)', (user, tag, self.now.isoformat()))
        self.load('youtube')
        result = self.import_usage()
        self.assertEqual(sorted(s for s in result['scopes'] if s.startswith('instagram')), ['instagram_graph/ig:1001', 'instagram_graph/ig:1002'])
        oracle = OracleExecution(self.repo, self.tracking.accounts, lambda: self.now)
        for scope, used in (('ig:1001', 1), ('ig:1002', 2)):
            plan = dict(source_id='instagram_graph', account_scope=scope, account_options={}, canonical_query={'parameters': {'q': '디저트'}, 'max_pages': 1})
            with self.storage.reader() as db:
                sqlite = self.planner.quota.assess(db, plan, self.now)
            with oracle.reader() as t:
                target = oracle.quota.assess(t, plan, self.now)
            self.assertEqual(target, sqlite)
            self.assertEqual(target['lines'][0]['observed_usage'], used)

    # 22: no usage is ever recorded as provider-confirmed.

    def test_no_debit_is_ever_confirmed(self):
        self.legacy_youtube()
        self.load('youtube', 'naver_datalab')
        self.import_usage()
        controller = self.controller(verified=True)
        for job in controller.execution.schedule():
            controller.run_once(job['job_id'])
        self.assertFalse([r for r in self.repo.rows('quota_usage_ledger') if r['evidence'] == 'PROVIDER_CONFIRMED' or r['confidence'] == 'CONFIRMED'])
        self.assertEqual({c['remote_debit_confirmed'] for c in self.repo.rows('execution_calls')}, {0})
        for item in controller.execution.status()['items']:
            self.assertFalse(item['run']['result_contract']['remote_debit_confirmed'])
            self.assertFalse(item['current_budget']['remote_debit_confirmed'])
            self.assertTrue(all(l['provider_usage_verified'] is False and l['provider_observed_usage'] is None for l in item['current_budget']['lines']))


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleQuotaTests(OracleQuotaStandInTests):
    """Every case above on the real disposable Oracle test schema."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db
