"""ORACLE-CUTOVER-PREP-1 phase 4: Oracle -> SQLite reverse recovery rehearsed on disposable stores.

A SQLite period (food, tracking, mock collection, RSS) is loaded into the target with B7; the Oracle runtime then
works alone (a new food, an alias change, a paused target, a new target, mock collection, and in the RSS cases RSS
discovery with a candidate approved as a food). Recovery must refuse a busy target, bring back exactly the target-only
rows and the authorized mutable changes (RSS items, runs, observations, classifications and history included), leave a
SQLite store that answers the app API and the RSS screens as the target does and reconciles with it, write nothing on a
re-run, resume after a failure, never overwrite or re-attach SQLite rows when the destination is not the loaded file,
keep retention, and list what cannot come back.
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
from test_oracle_rss import stable

COOKIE = ('두바이 쫀득쿠키', ['두바이 쫀득쿠키 디저트 맛집 인기'])  # FOOD
MALA = ('마라탕', ['마라탕 맛집 인기'])  # REVIEW, then FOOD with the news below
MALA_FOOD = ('마라탕', ['마라탕 디저트 맛집 인기'])
DOJJONKU = ('두쫀쿠', ['두쫀쿠 품절'])  # the SQLite period's item, observed again on the target
BAGEL = ('성수 베이글', ['성수 베이글 맛 후기 줄서는'])  # REVIEW

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

    def rss_period(self, *feeds):
        """RSS discovery on the target after the cut-over, one run per feed; the food candidate of the first run is approved."""
        state = {}
        backend = oracle_backend(self.repo, mock_accounts(), lambda: self.now, Feed(), rss_fetcher=lambda _: (state['xml'], 200))
        try:
            for n, entries in enumerate(feeds):
                state['xml'] = rss_xml(*entries)
                self.assertTrue(backend.rss.collect()['success'])
                if n == 0:
                    cookie = next(c for c in backend.foods.candidates() if c['surface_form'] == COOKIE[0])
                    self.rss_food = backend.foods.command('review', dict(
                        request_key=uuid.uuid4().hex, candidate_id=cookie['candidate_id'], expected_revision=cookie['revision'], action='create',
                        approved=True, actor_ref='reviewer', reason='RSS 후보 승인'))['resolution']
                self.now += timedelta(minutes=30)
        finally:
            backend.execution.release()

    def target_rss(self):
        backend = oracle_backend(self.repo, mock_accounts(), lambda: self.now, Feed())
        self.addCleanup(backend.execution.release)
        return backend.rss

    def test_rss_discovered_on_the_target_comes_back_to_sqlite(self):
        self.rss_period((COOKIE, MALA), (COOKIE, MALA_FOOD, DOJJONKU, BAGEL))
        before = plan(self.path, self.repo, now=self.now)
        tables = before['tables']
        self.assertEqual({t: tables[t]['target_only'] for t in ('rss_items', 'rss_runs', 'rss_observations', 'rss_classifications',
                                                                'rss_classification_history')},
                         {'rss_items': 3, 'rss_runs': 2, 'rss_observations': 6, 'rss_classifications': 3, 'rss_classification_history': 1})
        self.assertEqual(tables['rss_items']['mutable_updates'], 1)  # the loaded item observed again: last seen and payload
        self.assertEqual(before['losses']['rss_raw_response']['rows'], 2)  # never kept on the target, not invented
        self.assertNotIn('candidate_rss_item', before['losses'])
        self.assertEqual(before['summary']['conflicts'], 0)
        result = recover(self.path, self.repo, now=self.now, apply_updates=True)
        self.assertTrue(result['converged'], result['plan_after']['summary'])
        storage = Storage(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual((db.execute('PRAGMA integrity_check').fetchone()[0], db.execute('PRAGMA foreign_key_check').fetchall()), ('ok', []))
            self.assertEqual([r[0] for r in db.execute('SELECT id FROM runs ORDER BY id')], [1, 2, 3])
            self.assertEqual(db.execute('SELECT count(*) FROM runs WHERE raw_response IS NOT NULL AND id>1').fetchone()[0], 0)
            numbers = dict(db.execute('SELECT keyword,id FROM items').fetchall())
            self.assertEqual(numbers, {DOJJONKU[0]: 1, COOKIE[0]: 2, MALA[0]: 3, BAGEL[0]: 4})  # the numbers the target kept
            history = db.execute('SELECT item_id,result_json FROM classification_history').fetchall()
        self.assertEqual([(i, json.loads(r)['classification']) for i, r in history], [(3, 'REVIEW')])
        # The SQLite RSS screens now show what the target shows: items, classifications, latest contracts and the last run.
        rss = self.target_rss()
        self.assertEqual({r['id']: stable(r) for r in storage.trends()}, {r['id']: stable(r) for r in rss.storage.trends()})
        mine, theirs = storage.status(), rss.status()
        for key in ('total_keywords', 'food_count', 'review_count', 'non_food_count', 'distinct_keywords'):
            self.assertEqual(mine[key], theirs[key], key)
        self.assertEqual(stable(mine['latest_run']), stable(theirs['latest_run']))
        # Candidates refer to their recovered items; the approved food and the exact-alias resolution are kept.
        candidates = {c['surface_form']: c for c in FoodService(storage).candidates() if c['source_code'] == 'google_trends_rss'}
        self.assertEqual({k: c['source_item_id'] for k, c in candidates.items()}, {COOKIE[0]: 2, MALA[0]: 3, BAGEL[0]: 4})
        self.assertEqual((candidates[COOKIE[0]]['status'], candidates[COOKIE[0]]['resolved_food_id']), ('RESOLVED', self.rss_food['food_id']))
        self.assertEqual((candidates[MALA[0]]['status'], candidates[MALA[0]]['resolved_food_id'], candidates[MALA[0]]['proposed_kind']),
                         ('RESOLVED', self.new_food['food_id'], 'PRODUCT'))
        # The app API answers alike and the stores reconcile row for row; a re-run writes nothing.
        settle_after_ddl(self.repo.db)
        sqlite, target = self.apps()
        day = (self.now + timedelta(hours=9)).date().isoformat()
        food = '/api/v1/foods/' + self.rss_food['food_id']
        for request in self.requests() + [food, food + '/timeline?period=7d&source=google_trends_rss&end_date=' + day,
                                          '/api/v1/trends/rankings?source=google_trends_rss&include_zero=true&end_date=' + day]:
            with self.subTest(request=request):
                self.assertEqual(ask(sqlite, request), ask(target, request))
        self.assertTrue(reconcile(self.path, self.repo, now=self.now, source_storage=storage)['consistent'])
        again = recover(self.path, self.repo, now=self.now, apply_updates=True)
        self.assertEqual(again['writes'], {'inserted': 0, 'content_rows': 0, 'updated': 0, 'ignored': 0})
        # The SQLite collector continues after the recovered numbers.
        self.assertEqual(CollectionService(storage, fetcher=lambda _: (rss_xml(('탕후루', ['탕후루 디저트 인기'])), 200)).collect()['run_id'], 4)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT id FROM items WHERE keyword='탕후루'").fetchone()[0], 5)

    def test_rss_recovery_resumes_and_never_attaches_to_other_sqlite_rows(self):
        self.rss_period((COOKIE, MALA), (MALA_FOOD, BAGEL))
        state = {'raised': False}

        def drop(stage, table, n):
            if stage == 'row' and table == 'rss_observations' and n == 2 and not state['raised']:
                state['raised'] = True
                raise TransientError('connection_or_timeout')
        with self.assertRaises(TransientError):
            self.recovery(fault=drop).run(apply_updates=True)
        partial = self.recovery().plan()
        self.assertEqual((partial['tables']['rss_items']['target_only'], partial['tables']['rss_runs']['target_only']), (0, 0))  # committed
        self.assertEqual(partial['tables']['rss_observations']['target_only'], 4)  # the failed chunk rolled back
        result = recover(self.path, self.repo, now=self.now, apply_updates=True)
        self.assertTrue(result['converged'], result['plan_after']['summary'])
        with closing(sqlite3.connect(self.path)) as db:
            counts = [db.execute('SELECT count(*) FROM %s' % t).fetchone()[0] for t in ('items', 'runs', 'observations', 'candidates', 'classification_history')]
        self.assertEqual(counts, [4, 3, 1 + 4, 4, 1])  # the SQLite period's item, run and observation plus the target's, each once

    def test_rss_numbers_taken_by_a_later_sqlite_run_stay_conflicts(self):
        # After the load the SQLite collector ran once more: run 2 and item 2 are its own, the numbers the target also used next.
        CollectionService(self.storage, fetcher=lambda _: (rss_xml(('탕후루', ['탕후루 디저트 인기'])), 200)).collect()
        with closing(sqlite3.connect(self.path)) as db:
            mine = (db.execute('SELECT * FROM runs WHERE id=2').fetchone(), db.execute('SELECT * FROM items WHERE id=2').fetchone(),
                    db.execute('SELECT count(*) FROM observations WHERE run_id=2').fetchone()[0])
        self.rss_period((COOKIE, MALA), (MALA_FOOD, BAGEL))
        result = recover(self.path, self.repo, now=self.now, apply_updates=True)
        self.assertEqual((result['baseline']['verified'], result['updates_applied']), (False, False))
        tables = result['plan_before']['tables']
        self.assertEqual((tables['rss_runs']['conflicts'], tables['rss_items']['conflicts']), (1, 1))  # run 2 and items:2 are SQLite's own
        self.assertGreater(tables['rss_observations']['conflicts'], 0)
        self.assertGreater(tables['run_items']['conflicts'], 0)  # staging of the target's runs:2 is not attached to SQLite's runs:2
        # The target's cookie candidate is keyed by item number 2, a key SQLite's own candidate holds: it and its bridge events stay out.
        self.assertEqual((tables['food_candidates']['conflicts'], tables['food_candidates']['target_only']), (1, 3))  # the rest come back:
        # the Oracle period's own submission and the RSS candidates of items 3 and 4.
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual((db.execute('SELECT * FROM runs WHERE id=2').fetchone(), db.execute('SELECT * FROM items WHERE id=2').fetchone(),
                              db.execute('SELECT count(*) FROM observations WHERE run_id=2').fetchone()[0]), mine)
            self.assertEqual((db.execute('PRAGMA integrity_check').fetchone()[0], db.execute('PRAGMA foreign_key_check').fetchall()), ('ok', []))
            self.assertEqual([r[0] for r in db.execute('SELECT id FROM runs ORDER BY id')], [1, 2, 3])  # the target's run 3 came back
        again = recover(self.path, self.repo, now=self.now, apply_updates=True)
        self.assertEqual(again['writes'], {'inserted': 0, 'content_rows': 0, 'updated': 0, 'ignored': 0})

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
