"""ORACLE-CUTOVER-PREP-1 phase 4: Oracle -> SQLite reverse recovery rehearsed on disposable stores.

A SQLite period (food, tracking, mock collection, RSS) is loaded into the target with B7; the Oracle runtime then
works alone (a new food, an alias change, a paused target, a new target, mock collection). Recovery must refuse a busy
target, bring back exactly the target-only rows and the authorized mutable changes, leave a SQLite store that answers
the app API as the target does and reconciles with it, write nothing on a re-run, resume after a failure, never
overwrite SQLite rows when the destination is not the loaded file, keep retention, and list what cannot come back.
The stand-in runs by default; the Oracle subclass repeats every case on the disposable MZ_TEST schema.
"""
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from food_trends.analytics_reader import SQLiteAnalyticsStore, TargetAnalyticsStore
from food_trends.analytics_service import FoodAnalytics
from food_trends.app_api import AppApi
from food_trends.execution_mock import mock_controller
from food_trends.food_service import FoodService
from food_trends.query_planner import QueryPlanner
from food_trends.reverse_recovery import Recovery, RecoveryRefused, plan, recover
from food_trends.runtime_storage import oracle_backend
from food_trends.service import CollectionService
from food_trends.storage import Storage
from food_trends.target_db import SQLiteTarget, TransientError
from food_trends.target_migration import migrate, reconcile
from food_trends.target_repository import TargetRepository
from food_trends.tracking_access import mock_accounts
from food_trends.tracking_service import TrackingService
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema, settle_after_ddl
from test_food_observations import rss_xml
from test_oracle_backend import Feed

T0 = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)
HOST = {'Host': '127.0.0.1:8020'}


def ask(api, target):
    status, body, extra = api.handle('GET', target, dict(HOST))
    return json.dumps([status, body, sorted(extra.items())], ensure_ascii=False, sort_keys=True)


class RecoveryStandInTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = T0
        self.path = Path(self.temp.name) / 'sqlite-period.sqlite3'
        self.storage = Storage.create(self.path)
        self.sqlite_period()
        self.repo = TargetRepository(self.make_target(), clock=lambda: self.now)
        self.repo.migrate()
        self.load = migrate(self.path, self.repo, now=self.now)
        self.oracle_period()

    def make_target(self):
        return SQLiteTarget(Path(self.temp.name) / 'target.sqlite3')

    # The SQLite system of record before the cut-over, then the Oracle runtime alone after it.

    def sqlite_period(self):
        foods, self.tracking = FoodService(self.storage), TrackingService(self.storage, mock_accounts(), lambda: self.now)
        c = foods.command('submit', dict(request_key=uuid.uuid4().hex, name='두쫀쿠', aliases=['두바이쫀득쿠키']))
        self.food = foods.command('review', dict(request_key=uuid.uuid4().hex, candidate_id=c['candidate_id'], expected_revision=c['revision'],
                                                 action='create', approved=True, actor_ref='reviewer', reason='음식 확인'))['resolution']
        self.targets = {}
        for source in ('instagram_graph', 'youtube'):
            t = self.tracking.command('register', dict(request_key=uuid.uuid4().hex, food_id=self.food['food_id'], source_id=source,
                                                       actor_ref='reviewer', reason='추적'))
            self.targets[source] = self.tracking.command('activate', dict(request_key=uuid.uuid4().hex, target_id=t['target_id'],
                                                                          expected_revision=t['revision'], approved=True, actor_ref='reviewer', reason='추적'))
        transport = Feed()
        controller = mock_controller(self.storage, QueryPlanner(self.storage, self.tracking), transport)
        for job in controller.execution.schedule():
            controller.run_once(job['job_id'])
        CollectionService(self.storage, fetcher=lambda _: (rss_xml(('두쫀쿠', ['두쫀쿠 품절'])), 200)).collect()
        with closing(sqlite3.connect(self.path)) as db:  # the cut-over load reads a checkpointed file
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def oracle_period(self):
        self.now = T0 + timedelta(hours=2)
        transport = Feed()
        transport.posts = [('910001', '#두쫀쿠 #신상'), ('910002', '#마라탕')]
        backend = oracle_backend(self.repo, mock_accounts(), lambda: self.now, transport)
        try:
            c = backend.foods.command('submit', dict(request_key=uuid.uuid4().hex, name='마라탕', aliases=[]))
            self.new_food = backend.foods.command('review', dict(request_key=uuid.uuid4().hex, candidate_id=c['candidate_id'],
                expected_revision=c['revision'], action='create', approved=True, actor_ref='reviewer', reason='Oracle 기간 등록'))['resolution']
            food = backend.foods.food(self.food['food_id'])
            backend.foods.command('alias', dict(request_key=uuid.uuid4().hex, action='approve', food_id=food['food_id'], surface_form='신상',
                                                expected_revision=food['revision'], approved=True, actor_ref='reviewer', reason='Oracle 기간 별칭'))
            youtube = backend.tracking.target(self.targets['youtube']['target_id'])
            backend.tracking.command('pause', dict(request_key=uuid.uuid4().hex, target_id=youtube['target_id'], expected_revision=youtube['revision'],
                                                   actor_ref='reviewer', reason='Oracle 기간 중지'))
            instagram = backend.tracking.target(self.targets['instagram_graph']['target_id'])
            backend.tracking.command('refresh', dict(request_key=uuid.uuid4().hex, target_id=instagram['target_id'], expected_revision=instagram['revision'],
                                                     actor_ref='reviewer', reason='별칭 반영'))
            t = backend.tracking.command('register', dict(request_key=uuid.uuid4().hex, food_id=self.new_food['food_id'], source_id='instagram_graph',
                                                          actor_ref='reviewer', reason='Oracle 기간 추적'))
            backend.tracking.command('activate', dict(request_key=uuid.uuid4().hex, target_id=t['target_id'], expected_revision=t['revision'],
                                                      approved=True, actor_ref='reviewer', reason='Oracle 기간 추적'))
            self.new_target = t['target_id']
            backend.execution.schedule()
            while backend.controller.run_once() is not None:
                pass
            backend.observations.apply_entity_changes()
        finally:
            backend.execution.release()

    def recovery(self, **options):
        return Recovery(self.path, self.repo, now=self.now, **options)

    def apps(self):
        sqlite = AppApi(FoodAnalytics(SQLiteAnalyticsStore(Storage(self.path)), clock=lambda: self.now, origin='mock'))
        target = AppApi(FoodAnalytics(TargetAnalyticsStore(self.repo), clock=lambda: self.now, origin='mock'))
        return sqlite, target

    def requests(self):
        day = (self.now + timedelta(hours=9)).date().isoformat()
        out = ['/api/v1/foods', '/api/v1/sources/status', '/api/v1/trends/rankings?source=instagram_graph&include_zero=true&end_date=' + day]
        for food in (self.food['food_id'], self.new_food['food_id']):
            out += ['/api/v1/foods/' + food, '/api/v1/foods/%s/timeline?period=7d&end_date=%s' % (food, day),
                    '/api/v1/foods/%s/metrics?period=7d&end_date=%s' % (food, day)]
        return out

    # Cases

    def test_a_busy_target_is_refused(self):
        backend = oracle_backend(self.repo, mock_accounts(), lambda: self.now, Feed())
        try:
            backend.tracking.target(self.new_target)
            backend.foods.command('submit', dict(request_key=uuid.uuid4().hex, name='탕후루', aliases=[]))  # joins RUNTIME_TEST
            with self.assertRaises(RecoveryRefused) as refused:
                recover(self.path, self.repo, now=self.now)
            self.assertEqual(refused.exception.code, 'ownership_not_idle:RUNTIME_TEST')
            self.now += timedelta(hours=2)
            backend.execution.schedule()  # unfinished jobs keep the runtime mode after the process leaves
        finally:
            backend.execution.release()
        ready = self.recovery().preconditions()
        self.assertEqual((ready['ready'], ready['reasons']), (False, ['ownership_not_idle:RUNTIME_TEST', 'runtime_jobs_in_flight']))
        with self.assertRaises(RecoveryRefused):
            recover(self.path, self.repo, now=self.now)

    def test_dry_run_classifies_and_writes_nothing(self):
        before, target_before = self.path.read_bytes(), {t: self.repo.count(t) for t in ('food_entities', 'tracking_events', 'migration_batches')}
        result = plan(self.path, self.repo, now=self.now)
        self.assertEqual((self.path.read_bytes(), {t: self.repo.count(t) for t in target_before}), (before, target_before))
        self.assertTrue(result['preconditions']['ready'])
        self.assertTrue(result['baseline']['verified'])
        tables = result['tables']
        self.assertEqual((tables['food_entities']['target_only'], tables['food_entities']['mutable_updates']), (1, 1))
        self.assertEqual(tables['tracking_targets']['target_only'], 1)
        self.assertEqual(tables['tracking_targets']['mutable_updates'], 2)
        self.assertGreater(tables['tracking_events']['target_only'], 0)
        self.assertGreater(tables['food_mentions']['target_only'], 0)
        self.assertEqual(result['summary']['conflicts'], 0)
        self.assertEqual(set(result['losses']), {'tracking_slots', 'instagram_caption_tags', 'instagram_caption_captured_at'})
        self.assertEqual(result['not_recovered']['runtime_participants']['rows'], self.repo.count('runtime_participants'))

    def test_rss_discovered_on_the_target_is_listed_as_not_written_back(self):
        backend = oracle_backend(self.repo, mock_accounts(), lambda: self.now, Feed(),
                                 rss_fetcher=lambda _: (rss_xml(('마라탕', ['마라탕 맛집 인기'])), 200))
        try:
            self.assertEqual(backend.rss.collect()['entity_resolution'], {'state': 'completed', 'candidates': 1})
        finally:
            backend.execution.release()
        result = plan(self.path, self.repo, now=self.now)
        self.assertEqual({t: result['losses'][t]['rows'] for t in ('rss_runs', 'rss_observations', 'rss_classifications', 'candidate_rss_item')},
                         {'rss_runs': 1, 'rss_observations': 1, 'rss_classifications': 1, 'candidate_rss_item': 1})
        self.assertEqual(result['contents_not_recovered'].get('rss_content_written_on_target'), 1)

    def test_recovery_brings_back_the_oracle_period_and_reruns_write_nothing(self):
        result = recover(self.path, self.repo, now=self.now, apply_updates=True)
        self.assertTrue(result['updates_applied'])
        self.assertTrue(result['converged'], result['plan_after']['summary'])
        self.assertEqual(result['plan_after']['summary'], {'target_only': 0, 'mutable_updates': 0, 'conflicts': 0,
                                                           'sqlite_only': result['plan_after']['summary']['sqlite_only']})
        self.assertGreater(result['writes']['inserted'], 0)
        self.assertEqual(result['writes']['content_rows'], 2)
        # The SQLite system resumes on the recovered rows: identity, tracking history in append order, plans.
        storage = Storage(self.path)
        foods, tracking = FoodService(storage), TrackingService(storage, mock_accounts(), lambda: self.now)
        self.assertEqual(foods.food(self.new_food['food_id'])['canonical_name'], '마라탕')
        self.assertIn('신상', [a['surface_form'] for a in foods.food(self.food['food_id'])['approved_aliases']])
        self.assertEqual(tracking.target(self.targets['youtube']['target_id'])['status'], 'PAUSED')
        self.assertEqual([e['action'] for e in tracking.history(self.new_target)], ['REGISTERED', 'ACTIVATE'])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual((db.execute('PRAGMA integrity_check').fetchone()[0], db.execute('PRAGMA foreign_key_check').fetchall()), ('ok', []))
        # Both stores now answer the app alike and reconcile row for row.
        settle_after_ddl(self.repo.db)
        sqlite, target = self.apps()
        for request in self.requests():
            with self.subTest(request=request):
                self.assertEqual(ask(sqlite, request), ask(target, request))
        self.assertTrue(reconcile(self.path, self.repo, now=self.now, source_storage=storage)['consistent'])
        # SQLite commands continue from the recovered revisions.
        target_row = tracking.target(self.new_target)
        paused = tracking.command('pause', dict(request_key=uuid.uuid4().hex, target_id=self.new_target, expected_revision=target_row['revision'],
                                                actor_ref='reviewer', reason='복구 후 SQLite 명령'))
        self.assertEqual(paused['status'], 'PAUSED')
        # A second run after the SQLite system moved on writes nothing and overwrites nothing.
        again = recover(self.path, self.repo, now=self.now, apply_updates=True)
        self.assertEqual(again['writes'], {'inserted': 0, 'content_rows': 0, 'updated': 0, 'ignored': 0})
        self.assertEqual(tracking.target(self.new_target)['status'], 'PAUSED')

    def test_a_failure_mid_run_resumes_and_converges(self):
        state = {'raised': False}

        def drop(stage, table, n):
            if stage == 'row' and table == 'food_mentions' and not state['raised']:
                state['raised'] = True
                raise TransientError('connection_or_timeout')
        with self.assertRaises(TransientError):
            self.recovery(fault=drop).run(apply_updates=True)
        self.assertEqual(self.recovery().preconditions()['mode'], 'IDLE')  # the lease was released
        partial = self.recovery().plan()
        self.assertGreater(partial['tables']['food_mentions']['target_only'], 0)
        self.assertEqual(partial['tables']['food_entities']['target_only'], 0)  # committed chunks stay
        self.assertTrue(self.recovery().baseline()['resumes_recovery'])
        result = recover(self.path, self.repo, now=self.now, apply_updates=True)
        self.assertEqual((result['updates_applied'], result['converged']), (True, True), result['plan_after']['summary'])
        self.assertTrue(reconcile(self.path, self.repo, now=self.now)['consistent'])

    def test_a_changed_destination_is_never_overwritten(self):
        storage = Storage(self.path)
        foods = FoodService(storage)
        food = foods.food(self.food['food_id'])
        foods.command('rename', dict(request_key=uuid.uuid4().hex, food_id=food['food_id'], canonical_name='SQLite 쪽 이름', approved=True,
                                     actor_ref='someone', reason='전환 후 SQLite 쓰기', expected_revision=food['revision']))
        result = recover(self.path, self.repo, now=self.now, apply_updates=True)
        self.assertEqual((result['baseline']['verified'], result['updates_applied']), (False, False))
        self.assertEqual(foods.food(food['food_id'])['canonical_name'], 'SQLite 쪽 이름')  # kept
        self.assertGreater(result['writes']['inserted'], 0)  # target-only rows still come back
        self.assertEqual(result['writes']['updated'], 0)
        self.assertGreater(result['plan_after']['summary']['mutable_updates'] + result['plan_after']['summary']['conflicts'], 0)

    def test_retention_holds_on_the_way_back(self):
        self.now = T0 + timedelta(days=31)  # past the YouTube 29 days and the 30-day caption window
        result = recover(self.path, self.repo, now=self.now, apply_updates=True)
        self.assertEqual(result['plan_before']['contents_not_recovered'], {})
        with closing(sqlite3.connect(self.path)) as db:
            captions = db.execute("SELECT count(*) FROM instagram_media WHERE id IN ('910001','910002') AND caption IS NOT NULL").fetchone()[0]
            purged = db.execute("SELECT count(*) FROM instagram_media WHERE id IN ('910001','910002') AND caption_purged_at IS NOT NULL").fetchone()[0]
            tags = db.execute("SELECT count(*) FROM instagram_caption_tags WHERE media_id IN ('910001','910002')").fetchone()[0]
        self.assertEqual((captions, purged, tags), (0, 2, 0))

    def test_expired_target_content_is_not_brought_back(self):
        with self.repo.db.transaction() as tx:  # a target-only YouTube snapshot whose 29 days have passed
            tx.execute("""INSERT INTO source_contents (payload_ref,item_key,source_code,origin,content_json,content_hash,captured_at,refreshed_at,expires_at)
                VALUES ('youtube_videos:expiredvid1','ITEM_x','youtube','mock','{}','%s',:b_t,:b_t,:b_t)""" % ('0' * 64),
                       {'b_t': (T0 - timedelta(days=40)) if self.repo.db.dialect == 'oracle' else (T0 - timedelta(days=40)).isoformat()},
                       ts_binds=('b_t',) if self.repo.db.dialect == 'oracle' else ())
        result = recover(self.path, self.repo, now=self.now, apply_updates=True)
        self.assertEqual(result['plan_before']['contents_not_recovered'], {'expired_content': 1})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM youtube_videos WHERE video_id='expiredvid1'").fetchone())

    def test_the_operational_database_is_refused(self):
        root = Path(__file__).resolve().parents[1]
        with self.assertRaises(RecoveryRefused) as refused:
            Recovery(root / 'data' / 'trends.sqlite3', self.repo)
        self.assertEqual(refused.exception.code, 'operational_database_requires_approval')


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleRecoveryTests(RecoveryStandInTests):
    """Every recovery case reading the real disposable Oracle test schema."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db


if __name__ == '__main__':
    unittest.main()
