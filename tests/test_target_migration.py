"""B7 SQLite -> target migration on disposable copies; target is the SQLite stand-in (Oracle unverified)."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from food_trends.execution_mock import MockTransport, mock_controller
from food_trends.food_observations import FoodObservationService
from food_trends.ingestion import IngestionService
from food_trends.service import CollectionService
from food_trends.storage import Storage
from food_trends.target_db import SQLiteTarget, TransientError
from food_trends.target_migration import migrate, reconcile
from food_trends.target_repository import TargetRepository
import test_execution as fixtures
from test_food_observations import rss_xml

AT = datetime(2026, 10, 28, tzinfo=timezone.utc)


def backup(source, destination):
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)


class Down(MockTransport):
    def __call__(self, request, timeout=None):
        raise URLError('fixture outage')


class SourceFixture(unittest.TestCase):
    """Build one realistic v10 source: approved food, three Mock sources, RSS (ok and failed), legacy rows."""
    create_food = fixtures.ExecutionTests.create_food
    cmd = fixtures.ExecutionTests.cmd
    register = fixtures.ExecutionTests.register
    active = fixtures.ExecutionTests.active
    job = fixtures.ExecutionTests.job
    advance = fixtures.ExecutionTests.advance

    def runTest(self):  # pragma: no cover - fixture builder only
        pass

    def build(self, path):
        fixtures.ExecutionTests.setUp(self)
        for source in ('instagram_graph', 'youtube', 'naver_datalab'):
            self.active(source)
        controller = mock_controller(self.storage, self.planner, MockTransport())
        for j in controller.execution.schedule():
            controller.run_once(j['job_id'])
        # A later Instagram slot fails entirely: NOT_OBSERVED with no value.
        self.now += fixtures.timedelta(seconds=3601)
        failing = mock_controller(self.storage, self.planner, Down())
        jobs = [j for j in failing.execution.schedule() if j['source_id'] == 'instagram_graph']
        for _ in range(3):
            for j in jobs:
                failing.run_once(j['job_id'])
            self.advance(600)
        CollectionService(self.storage, fetcher=lambda _: (rss_xml(('두쫀쿠', ['두쫀쿠 품절'])), 200)).collect()
        CollectionService(self.storage, fetcher=lambda _: (b'<html>down</html>', 200)).collect()
        with self.storage.connect() as db:
            db.execute("""INSERT INTO instagram_media(id,published_at,first_seen,last_seen,media_type,permalink,source,origin)
                VALUES ('800001','2026-10-20T00:00:00+00:00','2026-10-20T00:00:00+00:00','2026-10-20T00:00:00+00:00','IMAGE',NULL,'instagram_graph','legacy_unknown')""")
        IngestionService(self.storage, lambda: AT).legacy_backfill('instagram_graph')
        FoodObservationService(self.storage, lambda: AT).link_pending()
        backup(self.storage.path, path)
        self.temp.cleanup()


class MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.fixture_dir.name) / 'source.sqlite3'
        SourceFixture().build(cls.fixture)

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source.sqlite3'
        backup(self.fixture, self.source)
        self.repo = TargetRepository(SQLiteTarget(Path(self.temp.name) / 'target.sqlite3'), clock=lambda: AT)
        self.repo.migrate()

    def run_migration(self, **kw):
        return migrate(self.source, self.repo, now=kw.pop('now', AT), **kw)

    def test_full_migration_is_consistent_and_rerun_changes_nothing(self):
        first = self.run_migration()
        result = reconcile(self.source, self.repo, now=AT, source_storage=Storage(self.source))
        self.assertTrue(result['consistent'], json.dumps(result['summary'], ensure_ascii=False))
        self.assertGreater(result['checks']['food_views_compared'], 0)
        self.assertEqual(result['checks']['food_view_mismatches'], [])
        loaded = {t: v['loaded_rows'] for t, v in first['tables'].items()}
        self.assertGreater(loaded['source_items'], 0)
        self.assertGreater(loaded['food_mentions'], 0)
        counts = {t: self.repo.count(t) for t in first['tables']}
        # Re-running a completed batch writes nothing; a new pass after manual reset only re-reads.
        again = self.run_migration()
        self.assertEqual({t: v['writes'] for t, v in again['tables'].items()}, {t: {'inserted': 0, 'updated': 0, 'unchanged': 0} for t in again['tables']})
        with self.repo.db.transaction() as tx:
            tx.execute('DELETE FROM migration_batches')
        third = self.run_migration()
        self.assertEqual(sum(v['writes']['inserted'] + v['writes']['updated'] for v in third['tables'].values()), 0)
        self.assertEqual({t: self.repo.count(t) for t in first['tables']}, counts)

    def test_legacy_keys_payload_refs_origin_and_null_semantics(self):
        self.run_migration()
        result = reconcile(self.source, self.repo, now=AT)
        self.assertEqual(result['checks']['unresolved_payload_refs'], 0)
        with closing(sqlite3.connect(self.source)) as src:
            items = src.execute('SELECT count(*) FROM items').fetchone()[0]
            legacy = src.execute("SELECT count(*) FROM source_items WHERE first_origin='legacy_unknown'").fetchone()[0]
        mapped = self.repo.rows('legacy_key_map')
        self.assertEqual(sum(m['legacy_table'] == 'items' for m in mapped), items)
        self.assertTrue(all(m['target_key'].startswith('items:') for m in mapped if m['legacy_table'] == 'items'))
        self.assertEqual(result['checks']['legacy_unknown_items'], legacy)
        self.assertGreaterEqual(legacy, 1)
        self.assertGreater(result['checks']['not_observed_metrics_with_null_value'], 0)
        legacy_run = [r for r in self.repo.rows('ingest_runs') if r['origin'] == 'legacy_unknown']
        self.assertTrue(legacy_run and all(r['coverage'] == 'UNKNOWN' for r in legacy_run))

    def test_expired_rows_are_excluded_with_children_and_expiry_never_recomputed(self):
        late = datetime(2026, 12, 1, tzinfo=timezone.utc)
        report = self.run_migration(now=late)
        youtube = report['tables']['source_items']['dispositions']
        self.assertEqual(sum(v for k, v in youtube.items() if k.startswith('EXCLUDED_EXPIRED')), 1)
        self.assertTrue(any(k.startswith('EXCLUDED_EXPIRED:parent_excluded') for k in report['tables']['run_items']['dispositions']))
        self.assertFalse([r for r in self.repo.rows('source_items') if r['source_code'] == 'youtube'])
        captions = [json.loads(r['content_json']) for r in self.repo.rows('source_contents') if r['source_code'] == 'instagram_graph']
        self.assertFalse(any('caption' in c for c in captions))  # the 30-day caption window has closed
        self.assertTrue(all(r['field_purged_at'] for r in self.repo.rows('source_contents')
                            if r['source_code'] == 'instagram_graph' and r['field_expires_at']))
        self.assertTrue(reconcile(self.source, self.repo, now=late)['consistent'])
        on_time = TargetRepository(SQLiteTarget(Path(self.temp.name) / 'on-time.sqlite3'), clock=lambda: AT)
        on_time.migrate()
        migrate(self.source, on_time, now=AT)
        with closing(sqlite3.connect(self.source)) as src:
            source_expiry = dict(src.execute("SELECT item_key,expires_at FROM source_items WHERE source_code='youtube'").fetchall())
        target_expiry = {r['item_key']: r['expires_at'] for r in on_time.rows('source_items') if r['source_code'] == 'youtube'}
        self.assertEqual({k: datetime.fromisoformat(v) for k, v in source_expiry.items()},
                         {k: datetime.fromisoformat(v) for k, v in target_expiry.items()})

    def test_interrupted_batch_rolls_back_and_resumes_without_duplicates(self):
        state = {'raised': False}
        def drop(stage, table, n):
            if stage == 'row' and table == 'quality_results' and n == 5 and not state['raised']:
                state['raised'] = True
                raise TransientError('connection_or_timeout')
        with self.assertRaises(TransientError):
            self.run_migration(batch_size=4, fault=drop)
        batch = [r for r in self.repo.rows('migration_batches') if r['table_name'] == 'quality_results'][0]
        self.assertEqual((batch['status'], batch['loaded_rows']), ('RUNNING', 4))  # only the committed chunk
        self.assertEqual(self.repo.count('quality_results'), 4)
        self.run_migration(batch_size=4)
        self.assertTrue(reconcile(self.source, self.repo, now=AT)['consistent'])

    def test_lost_acknowledgement_after_commit_is_harmless(self):
        state = {'raised': False}
        def lost(stage, table, n):
            if stage == 'after_commit' and table == 'food_mentions' and not state['raised']:
                state['raised'] = True
                raise TransientError('connection_or_timeout')
        with self.assertRaises(TransientError):
            self.run_migration(fault=lost)
        report = self.run_migration()
        self.assertEqual(report['tables']['food_mentions']['writes']['inserted'], 0)
        self.assertTrue(reconcile(self.source, self.repo, now=AT)['consistent'])

    def test_invalid_and_policy_rows_are_recorded_by_code_without_values(self):
        with closing(sqlite3.connect(self.source)) as src:  # simulate legacy inconsistencies in the copy only
            src.execute("UPDATE quality_results SET checked_at='2026-10-27T00:00:00' WHERE rowid=(SELECT min(rowid) FROM quality_results)")
            youtube = src.execute("SELECT item_key FROM source_items WHERE source_code='youtube'").fetchone()[0]
            alias = src.execute('SELECT alias_id,food_id FROM food_aliases LIMIT 1').fetchone()
            src.execute("""INSERT INTO food_mentions VALUES ('MENTION_yt','yt-key',?,NULL,?,?,'title',0,3,'r','EXACT_ALIAS_TEXT','mock','p',
                'ACTIVE',NULL,'2026-10-27T00:00:00+00:00',NULL,NULL)""", (youtube, alias[1], alias[0]))
            src.commit()
        report = self.run_migration()
        self.assertIn('REJECTED_INVALID:naive_timestamp', report['tables']['quality_results']['dispositions'])
        self.assertIn('EXCLUDED_POLICY:mention_source_blocked', report['tables']['food_mentions']['dispositions'])
        dump = json.dumps(self.repo.rows('migration_rejections'))
        self.assertNotIn('2026-10-27T00:00:00"', dump)
        self.assertNotIn('MENTION_yt', dump)
        self.assertEqual(reconcile(self.source, self.repo, now=AT)['checks']['blocked_policy_rows'], 0)

    def test_reconciliation_detects_tampering_and_missing_rows(self):
        self.run_migration()
        with self.repo.db.transaction() as tx:
            tx.execute("UPDATE metric_observations SET raw_value='9999+' WHERE source_code='google_trends_rss'")
            tx.execute("DELETE FROM quality_results WHERE result_id=(SELECT min(result_id) FROM quality_results)")
        result = reconcile(self.source, self.repo, now=AT)
        self.assertFalse(result['consistent'])
        self.assertGreater(result['tables']['metric_observations']['changed'], 0)
        self.assertEqual(result['tables']['quality_results']['missing'], 1)
        self.assertIn('metric_observations', result['summary']['hash_mismatch_tables'])

    def test_source_copy_is_never_written(self):
        before = self.source.read_bytes()
        self.run_migration()
        reconcile(self.source, self.repo, now=AT)
        self.assertEqual(self.source.read_bytes(), before)
