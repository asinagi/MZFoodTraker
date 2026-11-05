"""B7 shadow on disposable databases: SQLite stays the record, the SQLite stand-in plays the target."""
import uuid
import unittest
from datetime import timedelta
from pathlib import Path
from food_trends.execution_mock import MockTransport, mock_controller
from food_trends.food_observations import FoodObservationService
from food_trends.ingestion import IngestionService
from food_trends.service import CollectionService
from food_trends.shadow import ShadowService
from food_trends.target_db import SQLiteTarget, TransientError
from food_trends.target_migration import migrate
from food_trends.target_repository import TargetRepository
import test_execution as fixtures
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema
from test_food_observations import rss_xml


class DownTarget(SQLiteTarget):
    def transaction(self):
        raise TransientError('connection_or_timeout')


class ShadowTests(unittest.TestCase):
    setUp_base = fixtures.ExecutionTests.setUp
    tearDown = fixtures.ExecutionTests.tearDown
    create_food = fixtures.ExecutionTests.create_food
    cmd = fixtures.ExecutionTests.cmd
    register = fixtures.ExecutionTests.register
    active = fixtures.ExecutionTests.active
    job = fixtures.ExecutionTests.job
    advance = fixtures.ExecutionTests.advance

    def setUp(self):
        self.setUp_base()
        self.transport = MockTransport()
        self.repo = TargetRepository(self.make_target(), clock=lambda: self.now)
        self.repo.migrate()
        self.shadow = ShadowService(self.storage, self.repo, clock=lambda: self.now)

    def make_target(self):
        return SQLiteTarget(Path(self.temp.name) / 'target.sqlite3')

    def activity(self, *sources, keyword='두쫀쿠'):
        for source in sources:
            self.active(source)
        controller = mock_controller(self.storage, self.planner, self.transport)
        for j in controller.execution.schedule():
            controller.run_once(j['job_id'])
        CollectionService(self.storage, fetcher=lambda _: (rss_xml((keyword, [keyword + ' 리뷰'])), 200)).collect()

    def outbox(self):
        with self.storage.reader() as db:
            return db.execute('SELECT count(*) FROM shadow_outbox').fetchone()[0]

    def start(self, *sources):
        """B7 initial load, then capture from here on."""
        self.activity(*sources)
        migrate(self.storage.path, self.repo, now=self.now)
        self.shadow.enable()

    def test_capture_is_disabled_by_default(self):
        self.activity('youtube')
        self.assertEqual(self.outbox(), 0)
        self.assertFalse(self.shadow.status()['enabled'])

    def test_new_activity_converges_without_any_provider_call(self):
        self.start('youtube')
        self.now += timedelta(seconds=21601)
        self.activity('instagram_graph', 'naver_datalab', keyword='두바이쫀득쿠키')
        self.foods.command('alias', dict(request_key=uuid.uuid4().hex, food_id=self.food['food_id'], surface_form='두쫀',
                                         action='approve', expected_revision=self.foods.food(self.food['food_id'])['revision'],
                                         approved=True, actor_ref='reviewer', reason='별칭 추가'))
        FoodObservationService(self.storage, lambda: self.now).apply_entity_changes()
        self.assertGreater(self.outbox(), 0)
        calls = len(self.transport.calls)
        result = self.shadow.deliver(limit=100000)
        self.assertEqual((result['failed'], result['pending'], len(self.transport.calls)), (0, 0, calls))
        comparison = self.shadow.compare()
        self.assertTrue(comparison['consistent'], comparison['summary'])
        self.assertEqual(comparison['checks']['food_view_mismatches'], [])

    def test_redelivery_is_idempotent(self):
        self.start('youtube', 'instagram_graph')
        self.activity(keyword='두쫀쿠 신메뉴')
        self.shadow.deliver(limit=100000)
        counts = {t: self.repo.count(t) for t in ('source_items', 'run_items', 'source_contents', 'food_mentions', 'quality_results')}
        with self.storage.connect() as db:
            db.execute('UPDATE shadow_outbox SET delivered_at=NULL')
        self.shadow.deliver(limit=100000)
        self.assertEqual({t: self.repo.count(t) for t in counts}, counts)
        self.assertTrue(self.shadow.compare()['consistent'])

    def test_target_outage_leaves_events_pending_and_recovers(self):
        self.start('youtube')
        self.activity(keyword='두쫀쿠 품절')
        down = ShadowService(self.storage, TargetRepository(DownTarget(Path(self.temp.name) / 'unused.sqlite3')), clock=lambda: self.now)
        result = down.deliver(limit=100000)
        self.assertEqual(result['delivered'], 0)
        self.assertEqual(result['pending'], result['failing'])
        with self.storage.reader() as db:
            self.assertEqual({r[0] for r in db.execute('SELECT DISTINCT last_error_code FROM shadow_outbox WHERE delivered_at IS NULL')},
                             {'connection_or_timeout'})
        self.assertEqual(self.shadow.deliver(limit=100000)['delivered'], 0)  # backoff respected
        self.now += timedelta(hours=2)
        self.assertEqual(self.shadow.deliver(limit=100000)['pending'], 0)
        self.assertTrue(self.shadow.compare()['consistent'])

    def test_lost_acknowledgement_after_target_commit_converges(self):
        self.start('youtube')
        self.activity(keyword='두쫀쿠 맛집')
        lost = {'n': 0}
        def drop(stage, event):
            if stage == 'after_target_commit' and lost['n'] < 3:
                lost['n'] += 1
                raise TransientError('connection_or_timeout')
        self.shadow.fault = drop
        self.shadow.deliver(limit=100000)
        self.shadow.fault = lambda stage, event: None
        self.now += timedelta(hours=2)
        self.shadow.deliver(limit=100000)
        applied = self.repo.rows('shadow_applied')
        self.assertEqual(len(applied), len({a['event_id'] for a in applied}))
        self.assertTrue(self.shadow.compare()['consistent'])

    def test_child_delivered_before_parent_retries_until_parent_arrives(self):
        self.start('youtube')
        other = self.create_food('마라탕', ['마라 탕'])
        with self.storage.connect() as db:  # hold back the parent row's event
            db.execute("UPDATE shadow_outbox SET next_attempt_at=? WHERE entity_table='food_entities'", ((self.now + timedelta(minutes=30)).isoformat(),))
        first = self.shadow.deliver(limit=100000)
        with self.storage.reader() as db:
            codes = {r[0] for r in db.execute("SELECT last_error_code FROM shadow_outbox WHERE entity_table='food_aliases' AND delivered_at IS NULL")}
        self.assertEqual(codes, {'foreign_key_violation'})
        self.assertGreater(first['failed'], 0)
        self.now += timedelta(hours=1)
        self.shadow.deliver(limit=100000)
        self.now += timedelta(hours=1)
        self.assertEqual(self.shadow.deliver(limit=100000)['pending'], 0)
        self.assertEqual(self.repo.get('food_entities', {'food_id': other['food_id']})['canonical_name'], '마라탕')
        self.assertTrue(self.shadow.compare()['consistent'])

    def test_drift_is_detected_and_repaired_from_the_record(self):
        self.start('youtube', 'instagram_graph')
        with self.repo.db.transaction() as tx:
            tx.execute("UPDATE source_items SET first_origin='live_api'")
            tx.execute('DELETE FROM quality_results WHERE result_id=(SELECT min(result_id) FROM quality_results)')
        drift = self.shadow.compare()
        self.assertFalse(drift['consistent'])
        self.assertGreater(drift['summary']['changed'] + drift['summary']['missing'], 0)
        repaired = self.shadow.repair()
        self.assertGreater(repaired['restored'], 0)
        self.assertTrue(self.shadow.compare()['consistent'])

    def test_upstream_deletion_and_expiry_propagate(self):
        self.start('youtube')
        with self.storage.connect() as db:
            db.execute('DELETE FROM youtube_videos')
        IngestionService(self.storage, lambda: self.now).purge()
        FoodObservationService(self.storage, lambda: self.now).purge()
        self.shadow.deliver(limit=100000)
        self.assertFalse([r for r in self.repo.rows('source_contents') if r['source_code'] == 'youtube'])
        self.assertEqual({r['availability'] for r in self.repo.rows('source_items') if r['source_code'] == 'youtube'}, {'DELETED'})
        self.assertTrue(self.shadow.compare()['consistent'])

    def test_evidence_expiry_reaches_the_target_as_a_tombstone(self):
        self.start('instagram_graph')
        before = {m['mention_key'] for m in self.repo.rows('food_mentions') if m['status'] == 'ACTIVE' and m['expires_at']}
        self.assertTrue(before)
        self.now += timedelta(days=31)  # past the caption window: the source store drops the caption, B5 expires the link
        with self.storage.connect() as db:
            db.execute('UPDATE instagram_media SET caption=NULL,caption_purged_at=? WHERE caption IS NOT NULL', (self.now.isoformat(),))
        FoodObservationService(self.storage, lambda: self.now).purge()
        self.shadow.deliver(limit=100000)
        after = {m['mention_key']: m['status'] for m in self.repo.rows('food_mentions')}
        self.assertEqual({k: after.get(k) for k in before}, dict.fromkeys(before, 'EXPIRED'))  # kept as EXPIRED, never deleted
        self.assertFalse([c for c in self.repo.rows('source_contents') if c['source_code'] == 'instagram_graph' and 'caption' in c['content_json']])
        result = self.shadow.compare()
        self.assertTrue(result['consistent'], result['summary'])
        self.assertEqual(result['checks']['expired_rows_in_target'], 0)
        self.assertGreaterEqual(result['checks']['expired_tombstones_in_target'], len(before))


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleShadowTests(ShadowTests):
    """Same shadow cases delivering to the real Oracle test schema."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db
