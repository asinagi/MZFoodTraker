"""ORACLE-RUNTIME-2 R1: B2 query planning on the target schema.

Targets, foods and approved aliases are prepared on the SQLite system of record and loaded with B7;
then the SQLite QueryPlanner and the OracleQueryPlanner plan from the same instant. Results and the
stored planner tables are compared as logical values. Lifecycle changes made on SQLite reach the
target through B7's row derivation, as Shadow would deliver them.
"""
import json
import unittest
import uuid
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch
from food_trends.food_service import FoodError
from food_trends.oracle_execution import OracleExecution
from food_trends.oracle_planner import OracleQueryPlanner
from food_trends.target_db import logical
from food_trends.target_migration import Plan, migrate, open_source
from food_trends.target_schema import BY_NAME
import test_oracle_execution as s2
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema

PLANNER_TABLES = ('planner_queries', 'planner_plans', 'planner_query_targets', 'planner_plan_targets', 'planner_commands')
LIFECYCLE = ('food_entities', 'food_aliases', 'food_relations', 'food_candidates', 'food_events', 'food_commands', 'tracking_targets')


def sync(storage, repo, now, tables=LIFECYCLE):
    """Deliver the current SQLite rows of these tables to the target, as Shadow does (B7 derivation + upsert)."""
    with closing(open_source(storage.path)) as src:
        plan = Plan(src, now)
        with repo.db.transaction() as tx:
            for table in tables:
                for row in plan._source_rows(table):
                    repo.upsert(tx, table, row)


def stored(storage, repo, table):
    with storage.reader() as db:
        left = sorted(json.dumps(logical(table, dict(r)), sort_keys=True, ensure_ascii=False) for r in db.execute('SELECT * FROM %s' % table))
    right = sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in repo.rows(table))
    return left, right


class OraclePlannerStandInTests(unittest.TestCase):
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

    def load(self, *sources):
        for source in sources:
            self.target = self.active(source)
        migrate(self.storage.path, self.repo, now=self.now)

    def oracle(self):
        return OracleQueryPlanner(OracleExecution(self.repo, self.tracking.accounts, lambda: self.now))

    def set_account(self, source, **options):
        old = self.tracking.account(source, 'local-default')
        self.tracking.accounts[(source, 'local-default')] = replace(old, options=dict(old.options, **options))

    def assertPlannerTablesEqual(self):
        for table in PLANNER_TABLES:
            with self.subTest(table=table):
                left, right = stored(self.storage, self.repo, table)
                self.assertEqual(left, right)

    def snapshot(self):
        return {t: sorted(json.dumps(r, sort_keys=True) for r in self.repo.rows(t)) for t in BY_NAME}

    # R1-1, 2, 3: same FOOD_ID / exact aliases give the same queries; no duplicates; shared queries keep every mapping.

    def test_commit_matches_sqlite_for_every_source_and_shared_query(self):
        self.load('youtube', 'instagram_graph', 'naver_datalab')
        second = self.create_food('다른 음식', ['두쫀쿠'], scope='food:second')
        target = self.cmd('register', food_id=second['food_id'], source_id='youtube')
        self.change('activate', target, approved=True)
        migrate(self.storage.path, self.repo, now=self.now)
        options = {'include_discovery': True}
        sqlite = self.planner.commit('plan-1', options=options)
        oracle = self.oracle()
        result = oracle.commit('plan-1', options=options)
        self.assertEqual(result, sqlite)
        self.assertPlannerTablesEqual()
        shared = next(p for p in result['plans'] if p['source_id'] == 'youtube' and p['canonical_query']['parameters']['q'] == '두쫀쿠')
        self.assertEqual({m['food_id'] for m in shared['targets']}, {self.food['food_id'], second['food_id']})
        rss = [p for p in result['plans'] if p['source_id'] == 'google_trends_rss']
        self.assertEqual((len(rss), rss[0]['targets']), (1, []))  # one fixed discovery feed, no food targets
        # Replay and a new key with the same input: same plans, no new rows.
        before = self.snapshot()
        self.assertEqual(oracle.commit('plan-1', options=options), result)
        again = oracle.commit('plan-2', options=options)
        self.assertEqual(again['stored_plan_ids'], result['stored_plan_ids'])
        after = self.snapshot()
        self.assertEqual({t: v for t, v in after.items() if t != 'planner_commands'}, {t: v for t, v in before.items() if t != 'planner_commands'})
        with self.assertRaises(FoodError):
            oracle.commit('plan-1', options={})  # same key, different request
        with self.repo.db.transaction() as tx:
            self.assertEqual(tx.scalar("SELECT count(*) FROM planner_query_targets WHERE query_id=:q", {'q': shared['query_id']}), 2)
            for table, key in (('planner_queries', 'query_id'), ('planner_plans', 'plan_id')):
                self.assertEqual(tx.scalar('SELECT count(*) FROM %s' % table), tx.scalar('SELECT count(DISTINCT %s) FROM %s' % (key, table)))

    def test_previews_match_including_budget_and_policy_rejections(self):
        self.set_account('youtube', daily_search_limit=1)
        self.load('youtube', 'naver_datalab')
        account = self.tracking.account('instagram_graph', 'local-default')
        self.tracking.accounts[('instagram_graph', 'local-default')] = replace(account, policy_allowed=False)
        self.register('instagram_graph')
        migrate(self.storage.path, self.repo, now=self.now)
        left, right = self.planner.preview(), self.oracle().preview()
        self.assertEqual(right, left)
        decisions = sorted(p['decision'] for p in right['plans'] if p['source_id'] == 'youtube')
        self.assertEqual(decisions, ['DEFERRED_BUDGET', 'DEFERRED_BUDGET', 'READY_FOR_B3_RECHECK'])
        self.assertTrue(any(r['policy_status'] == 'BLOCKED' for r in right['rejections']))

    # R1-4: preview is read-only.

    def test_preview_is_read_only(self):
        self.load('youtube', 'instagram_graph', 'naver_datalab')
        before = self.snapshot()
        planner = self.oracle()
        for _ in range(3):
            preview = planner.preview(options={'include_discovery': True})
        self.assertEqual((preview['preview_only'], preview['reservations_created'], preview['http_requests']), (True, 0, 0))
        self.assertEqual(self.snapshot(), before)
        stored_plans = planner.commit('store-only')
        self.assertEqual(self.repo.count('quota_reservations'), 0)
        self.assertTrue(all(not p['execution_enabled'] for p in stored_plans['plans']))
        current = planner.plan(stored_plans['stored_plan_ids'][0])
        self.assertTrue(current['current_validation']['valid'])
        self.assertEqual(self.repo.count('quota_reservations'), 0)

    # R1-5: invalidated targets are never revived; generation, revision and policy_version are checked.

    def test_invalidated_targets_are_not_planned_or_executed(self):
        self.load('youtube')
        planner = self.oracle()
        old = planner.commit('old')
        self.change('pause', self.tracking.target(self.target['target_id']))
        sync(self.storage, self.repo, self.now)
        result = planner.commit('after-pause')
        self.assertEqual(result['plans'], [])
        self.assertEqual(result, dict(self.planner.commit('after-pause'), evaluated_at=result['evaluated_at']))
        jobs = planner.execution.enqueue(old['stored_plan_ids'])
        self.assertEqual({j['status'] for j in jobs}, {'BLOCKED'})
        self.assertTrue(all('NO_CURRENT_TARGET_MAPPING' in j['reasons'] for j in jobs))
        self.assertEqual(self.repo.count('execution_jobs'), 0)

    def test_alias_revocation_needs_refresh_and_keeps_old_plan(self):
        self.load('youtube')
        planner = self.oracle()
        saved = planner.commit('old')
        old = next(p for p in saved['plans'] if p['canonical_query']['parameters']['q'] == '두바이쫀득쿠키')
        alias = next(a for a in self.foods.food(self.food['food_id'])['approved_aliases'] if a['surface_form'] == '두바이쫀득쿠키')
        self.foods.command('alias', dict(request_key='revoke', alias_id=alias['alias_id'], expected_revision=alias['revision'], action='revoke',
                                         approved=True, actor_ref='reviewer', reason='잘못된 별칭'))
        sync(self.storage, self.repo, self.now)
        self.assertFalse(planner.plan(old['plan_id'])['current_validation']['valid'])
        stale = planner.preview()
        self.assertEqual(stale['plans'], [])
        self.assertIn('STALE_FOOD_REVISION', stale['rejections'][0]['reasons'])
        self.change('refresh', self.tracking.target(self.target['target_id']))
        sync(self.storage, self.repo, self.now)
        new = planner.commit('new')
        self.assertEqual(new, dict(self.planner.commit('new'), evaluated_at=new['evaluated_at']))
        self.assertNotIn('두바이쫀득쿠키', {p['canonical_query']['parameters']['q'] for p in new['plans']})
        self.assertTrue(set(saved['stored_plan_ids']).isdisjoint(new['stored_plan_ids']))
        self.assertEqual(planner.plan(old['plan_id'])['plan'], old)  # history is preserved
        self.assertEqual(len(planner.history(self.target['target_id'])), len(saved['plans']) + len(new['plans']))

    def test_version_change_invalidates_old_plan(self):
        self.load('youtube')
        planner = self.oracle()
        plan = planner.commit('v1')['plans'][0]
        with patch('food_trends.oracle_execution.COST_VERSION', 'fixture-next-cost'), patch('food_trends.query_planner.COST_VERSION', 'fixture-next-cost'), \
                patch('food_trends.quota.COST_VERSION', 'fixture-next-cost'):
            self.assertIn('POLICY_OR_PLANNER_VERSION_CHANGED', planner.plan(plan['plan_id'])['current_validation']['reasons'])
            self.assertNotIn(plan['query_id'], {p['query_id'] for p in planner.preview()['plans']})

    def test_unapproved_and_rejected_requests_match(self):
        self.foods.command('submit', dict(request_key='proposal', name='짭쫀쿠', aliases=['비승인 별칭']))
        self.load('youtube', 'naver_datalab')
        with self.assertRaises(FoodError):
            self.oracle().preview(options={'operation': 'food_extraction'})
        with self.assertRaises(FoodError):
            self.oracle().preview(options={'policy_allowed': True})
        words = {p['canonical_query']['parameters'].get('q') for p in self.oracle().preview()['plans']}
        self.assertFalse({'짭쫀쿠', '비승인 별칭'} & words)


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OraclePlannerTests(OraclePlannerStandInTests):
    """Every case above on the real disposable Oracle test schema."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db
