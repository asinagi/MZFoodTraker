"""ORACLE-RUNTIME-1 S3: B4 ingestion, staging and DQ on the target schema.

Staged rows from the SQLite IngestionService and from OracleIngestionService are compared table by
table as logical values after the same mock runs (S2 harness) or the same Source Envelope input.
Only run-specific identifiers (ingest/job/run ids, random DQ result ids) are excluded.
"""
import json
import unittest
from datetime import timedelta
from food_trends import data_quality as dq
from food_trends.ingestion import IngestionService, PolicyBlocked, naver_metrics
from food_trends.oracle_ingestion import OracleIngestionService
from food_trends.service import CollectionService
from food_trends.target_db import PolicyRefused, logical
import test_oracle_execution as s2
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema
from test_food_observations import rss_xml

STAGING = ('ingest_runs', 'source_items', 'item_snapshots', 'run_items', 'quality_results')
RUN_SPECIFIC = {'ingest_runs': ('ingest_run_id', 'run_ref', 'job_id'), 'run_items': ('ingest_run_id',),
                'quality_results': ('result_id', 'event_key', 'ingest_run_id')}


class Empty(s2.MockTransport):
    """Provider answers with zero items: a measured zero, not a failure."""
    def __call__(self, request, timeout=None):
        self.calls.append(request.full_url)
        return s2.MockResponse(b'{"items": []}')


class OracleIngestionStandInTests(unittest.TestCase):
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
    plans = s2.OracleExecutionStandInTests.plans
    plan = s2.OracleExecutionStandInTests.plan
    oracle = s2.OracleExecutionStandInTests.oracle
    sqlite = s2.OracleExecutionStandInTests.sqlite
    project = s2.OracleExecutionStandInTests.project
    parity = s2.OracleExecutionStandInTests.parity

    def staged(self, table, side, drop=()):
        if side == 'sqlite':
            with self.storage.reader() as db:
                rows = [logical(table, dict(r)) for r in db.execute('SELECT * FROM %s' % table)]
        else:
            rows = self.repo.rows(table)
        skip = set(RUN_SPECIFIC.get(table, ())) | set(drop)
        return sorted(json.dumps({k: v for k, v in r.items() if k not in skip}, sort_keys=True, ensure_ascii=False) for r in rows)

    def assertStagingEqual(self, drop=()):
        for table in STAGING:
            with self.subTest(table=table):
                self.assertEqual(self.staged(table, 'sqlite', drop), self.staged(table, 'target', drop))

    def run_both(self, sources=('youtube', 'instagram_graph', 'naver_datalab'), transport=s2.MockTransport, rounds=1, options=None):
        plans = self.plans(*sources, options=options)
        results = {}
        for source, plan in plans.items():
            def scenario(controller, job):
                for _ in range(rounds):
                    result = controller.run_once(job['job_id'])
                    if result and result.get('ingestion'):
                        self.assertNotEqual(result['ingestion'].get('state'), 'pending_retry', result['ingestion'])
                    self.advance(600)
            results[source] = self.parity(source, scenario, transport, plan=plan)
        return results

    def ingestion(self):
        return OracleIngestionService(self.oracle().execution)

    # Scenarios 11-14: common input, item dedupe, snapshots, DQ.

    def test_mock_runs_stage_identically_on_both_databases(self):
        self.run_both()
        self.assertStagingEqual()
        self.assertEqual(self.repo.count('source_items'), 2)  # NAVER never becomes content
        with self.repo.db.transaction() as tx:
            self.assertEqual(tx.scalar("SELECT metric_storage FROM ingest_runs WHERE source_code='naver_datalab'"), 'TRANSIENT_ONLY')
            self.assertEqual(tx.scalar('SELECT count(*) FROM item_snapshots s JOIN source_contents c ON c.payload_ref=s.payload_ref'), 2)

    def test_same_source_envelope_gives_same_rows_for_rss_and_naver(self):
        CollectionService(self.storage, fetcher=lambda _: (rss_xml(('두쫀쿠', ['두쫀쿠 리뷰'])), 200)).collect()
        captured = []
        sqlite = IngestionService(self.storage, lambda: self.now)
        real = sqlite.ingest
        sqlite.ingest = lambda run, contents=(), metrics=(): captured.append((run, list(contents), list(metrics))) or real(run, contents, metrics)
        with self.storage.reader() as db:
            run_id = db.execute('SELECT max(id) FROM runs').fetchone()[0]
        self.assertFalse(sqlite.rss_run(run_id)['created'])  # already staged by the collector: idempotent
        body = {'startDate': '2026-10-01', 'endDate': '2026-10-27', 'timeUnit': 'date',
                'keywordGroups': [{'groupName': 'FOOD_X', 'keywords': ['두쫀쿠']}]}
        naver = (dict(source_code='naver_datalab', run_ref='naver:fixture', origin='mock', execution_status='SUCCEEDED', coverage='COMPLETE',
                      error_kind=None, request_context=body, observed_at='2026-10-27T00:00:00+00:00', returned_count=1, accepted_count=1,
                      source_schema_version=dq.KNOWN_SCHEMAS['naver_datalab']),
                 [], naver_metrics(body, [{'title': 'FOOD_X', 'data': [{'period': '2026-10-01', 'ratio': 73.125987}]}], '2026-10-27T00:00:00+00:00'))
        sqlite_naver = real(*naver)
        oracle = self.ingestion()
        oracle_rss = oracle.ingest(*captured[0])
        oracle_naver = oracle.ingest(*naver)
        self.assertEqual(oracle_naver['transient_metrics'], sqlite_naver['transient_metrics'])
        self.assertEqual(oracle_rss['storable_metrics'][0]['metric_code'], 'rss.approx_traffic')
        self.assertStagingEqual(drop=('ingested_at', 'checked_at'))  # the collector staged RSS on the wall clock
        self.assertNotIn('73.125987', json.dumps([self.repo.rows(t) for t in STAGING]))

    def test_reingest_is_idempotent(self):
        self.run_both(sources=('youtube',))
        oracle = self.ingestion()
        run_ids = [r['run_id'] for r in self.repo.rows('execution_runs')]
        before = {t: self.repo.count(t) for t in STAGING}
        for run_id in run_ids:
            self.assertFalse(oracle.execution_run(run_id)['created'])
        self.assertEqual(oracle.ingest_pending()['ingested'], 0)
        self.assertEqual({t: self.repo.count(t) for t in STAGING}, before)

    def test_duplicate_pages_and_items_stage_once(self):
        self.run_both(sources=('youtube',), transport=s2.NextPage, options={'max_pages': 2})
        self.assertStagingEqual()
        self.assertEqual((self.repo.count('source_items'), self.repo.count('item_snapshots'), self.repo.count('run_items')), (1, 1, 1))

    # Failure is not zero.

    def test_failed_collection_and_measured_zero_are_distinct(self):
        self.run_both(sources=('youtube',), transport=s2.Down, rounds=3)
        self.assertStagingEqual()
        failed = self.repo.rows('ingest_runs')[0]
        self.assertEqual((failed['execution_status'], failed['coverage'], failed['staged_items']), ('FAILED', 'NONE', 0))

    def test_measured_zero_is_complete_coverage(self):
        self.run_both(sources=('youtube',), transport=Empty)
        self.assertStagingEqual()
        zero = self.repo.rows('ingest_runs')[0]
        self.assertEqual((zero['execution_status'], zero['coverage'], zero['staged_items'], zero['returned_count']), ('SUCCEEDED', 'COMPLETE', 0, 0))

    def test_partial_collection_is_recorded_as_partial(self):
        self.run_both(sources=('youtube',), transport=s2.SecondPageDown, rounds=3, options={'max_pages': 2})
        self.assertStagingEqual()
        self.assertEqual(self.repo.rows('ingest_runs')[0]['coverage'], 'PARTIAL')

    # Scenario 18: policy refusals.

    def test_policy_refusals(self):
        self.run_both(sources=('youtube',))
        oracle = self.ingestion()
        base = dict(source_code='naver_datalab', run_ref='naver:refused', origin='mock', execution_status='SUCCEEDED', coverage='COMPLETE',
                    error_kind=None, request_context={}, observed_at='2026-10-27T00:00:00+00:00', returned_count=0, accepted_count=0,
                    source_schema_version='naver-datalab-search-v1')
        with self.assertRaises(PolicyBlocked):
            oracle.ingest(base, [{'external_key': 'x'}])
        with self.assertRaises(PolicyBlocked):
            oracle.ingest(dict(base, request_context={'access_token': 'synthetic-secret-value'}))
        with self.assertRaises(PolicyRefused):
            with oracle.execution.transaction() as t:
                oracle._put_item(t, dict(item_key='ITEM_x', source_code='naver_datalab', external_key='x', key_kind='k', published_at=None,
                                         first_seen='2026-10-27T00:00:00+00:00', last_seen='2026-10-27T00:00:00+00:00', availability='AVAILABLE',
                                         first_origin='mock', payload_ref='naver:x', expires_at=None))
        self.assertFalse([r for r in self.repo.rows('ingest_runs') if r['run_ref'] == 'naver:refused'])
        # An expired record is never staged (and never revives a staged item).
        with oracle.execution.transaction() as t:
            content = t.one("SELECT * FROM source_contents WHERE source_code='youtube'", 'source_contents')
        record = dict(oracle._content_record('youtube', content, 'mock'), expires_at='2026-01-01T00:00:00+00:00')
        items = self.repo.count('source_items')
        result = oracle.ingest(dict(base, source_code='youtube', run_ref='yt:expired', source_schema_version=dq.KNOWN_SCHEMAS['youtube']), [record])
        self.assertEqual((result['staged_items'], result['expired_items'], result['rejected_items']), (0, 1, 0))
        self.assertEqual(self.repo.count('source_items'), items)

    # Scenario 20: expiry and deletion.

    def test_expiry_propagates(self):
        self.run_both(sources=('youtube', 'instagram_graph'))
        self.now += timedelta(days=30)
        sqlite_purge = IngestionService(self.storage, lambda: self.now).purge()
        oracle = self.ingestion()
        oracle_purge = oracle.purge()
        self.assertEqual({k: oracle_purge[k] for k in sqlite_purge}, sqlite_purge)
        self.assertStagingEqual()
        self.assertEqual({r['availability'] for r in self.repo.rows('source_items') if r['source_code'] == 'youtube'}, {'EXPIRED'})
        self.assertEqual((oracle_purge['purged_contents'], oracle_purge['purged_captions']), (1, 1))
        media = json.loads([r for r in self.repo.rows('source_contents') if r['source_code'] == 'instagram_graph'][0]['content_json'])
        self.assertNotIn('caption', media)
        self.assertEqual(oracle.purge(), dict(expired_items=0, deleted_items=0, purged_snapshots=0, purged_contents=0, purged_captions=0, http_requests=0))

    def test_upstream_deletion_propagates(self):
        self.run_both(sources=('youtube',))
        with self.storage.connect() as db:
            db.execute('DELETE FROM youtube_videos')
        oracle = self.ingestion()
        with oracle.execution.transaction() as t:
            t.run("DELETE FROM source_contents WHERE source_code='youtube'")
        sqlite_purge = IngestionService(self.storage, lambda: self.now).purge()
        oracle_purge = oracle.purge()
        self.assertEqual({k: oracle_purge[k] for k in sqlite_purge}, sqlite_purge)
        self.assertEqual(oracle_purge['deleted_items'], 1)
        self.assertStagingEqual()

    def test_sqlite_only_adapters_are_refused_on_the_target(self):
        self.plan()
        oracle = self.ingestion()
        for call in (lambda: oracle.rss_run(1), lambda: oracle.legacy_backfill('youtube')):
            with self.assertRaises(ValueError):
                call()


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleIngestionTests(OracleIngestionStandInTests):
    """Every case above on the real disposable Oracle test schema."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db
