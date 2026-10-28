"""B4 common ingestion, staging and data quality on disposable databases and Mock transports."""
import json
import unittest
from datetime import timedelta
from urllib.error import URLError
from urllib.parse import urlsplit
from food_trends.execution_mock import MockTransport, MockResponse, mock_controller
from food_trends.ingestion import IngestionService, PolicyBlocked, item_key, naver_metrics, METRIC_DEFINITIONS
from food_trends import data_quality as dq
from food_trends.service import CollectionService
from food_trends.tracking_service import stamp
import test_execution as fixtures
from test_mvp import fixture as rss_fixture

STAGING = ('ingest_runs', 'source_items', 'item_snapshots', 'run_items', 'quality_results')


class IngestionTests(unittest.TestCase):
    setUp = fixtures.ExecutionTests.setUp
    tearDown = fixtures.ExecutionTests.tearDown
    create_food = fixtures.ExecutionTests.create_food
    cmd = fixtures.ExecutionTests.cmd
    register = fixtures.ExecutionTests.register
    active = fixtures.ExecutionTests.active
    job = fixtures.ExecutionTests.job
    advance = fixtures.ExecutionTests.advance

    def staging(self):
        with self.storage.reader() as db:
            return {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1,2')] for t in STAGING}

    def count(self, sql, *args):
        with self.storage.reader() as db:
            return db.execute(sql, args).fetchone()[0]

    def run_all(self, sources=('instagram_graph', 'youtube', 'naver_datalab'), transport=None):
        for source in sources:
            self.active(source)
        transport = transport or MockTransport()
        controller = mock_controller(self.storage, self.planner, transport)
        jobs = controller.execution.schedule()
        return controller, [controller.run_once(j['job_id']) for j in jobs], transport

    def rss(self, title='두쫀쿠'):
        return CollectionService(self.storage, fetcher=lambda _: (rss_fixture(title), 200)).collect()

    def ingestion(self):
        return IngestionService(self.storage, lambda: self.now)

    # 9, 17: one envelope for four sources; NAVER and policy-forbidden data never staged
    def test_four_sources_share_the_envelope_and_naver_stays_transient(self):
        rss = self.rss()
        self.assertTrue(rss['ingestion']['created'])
        controller, results, _ = self.run_all()
        self.assertTrue(all(r['ingestion']['created'] for r in results))
        with self.storage.reader() as db:
            runs = {r['source_code']: dict(r) for r in db.execute('SELECT * FROM ingest_runs')}
            self.assertEqual(set(runs), {'google_trends_rss', 'instagram_graph', 'youtube', 'naver_datalab'})
            for run in runs.values():
                self.assertEqual(run['contract_version'], 'source-envelope-v1')
                self.assertTrue(run['adapter_version'] and run['policy_version'] and run['source_schema_version'])
                self.assertIn(run['origin'], ('mock',))
            self.assertEqual(runs['naver_datalab']['metric_storage'], 'TRANSIENT_ONLY')
            self.assertEqual(runs['youtube']['metric_storage'], 'NONE')
            self.assertEqual(db.execute("SELECT count(*) FROM source_items WHERE source_code='naver_datalab'").fetchone()[0], 0)
            dump = '\n'.join(db.iterdump())
        self.assertNotIn('"ratio"', dump)
        self.assertIn(runs['naver_datalab']['job_id'], [r['job_id'] for r in results])

    def test_policy_gate_refuses_naver_content_and_credential_metadata(self):
        service, before = self.ingestion(), self.staging()
        base = dict(source_code='naver_datalab', run_ref='manual:1', origin='mock', coverage='COMPLETE', observed_at=stamp(self.now),
                    request_context={'startDate': '2026-10-01'}, source_schema_version='datalab-search-v1')
        with self.assertRaises(PolicyBlocked):
            service.ingest(base, [{'external_key': 'x', 'payload_ref': 'none', 'payload_hash': '0' * 64}])
        with self.assertRaises(PolicyBlocked):
            service.ingest(dict(base, source_code='youtube', request_context={'q': 'x', 'api_key': 'synthetic'}))
        self.assertEqual(self.staging(), before)

    # 10, 22(B4 side): one content item across alias queries
    def test_same_video_from_three_alias_queries_is_one_item_with_three_run_links(self):
        self.run_all(('youtube',))
        self.assertEqual(self.count("SELECT count(*) FROM source_items WHERE source_code='youtube'"), 1)
        self.assertEqual(self.count('SELECT count(*) FROM run_items'), 3)
        self.assertEqual(self.count('SELECT count(*) FROM item_snapshots'), 1)

    # 11, 12, 20: re-observation, snapshot lineage, idempotent reprocessing
    def test_reobservation_keeps_history_and_reprocessing_is_idempotent(self):
        first, second = self.rss(), self.rss()
        self.assertEqual(self.count('SELECT count(*) FROM source_items'), 1)
        self.assertEqual(self.count('SELECT count(*) FROM run_items'), 2)
        self.assertEqual(self.count('SELECT count(*) FROM item_snapshots'), 1)
        with self.storage.reader() as db:
            ref = db.execute('SELECT payload_ref FROM item_snapshots').fetchone()[0]
            self.assertEqual(db.execute('SELECT source_id FROM items WHERE id=?', (int(ref.split(':')[1]),)).fetchone()[0],
                             db.execute('SELECT external_key FROM source_items').fetchone()[0])
        before = self.staging()
        again = self.ingestion().rss_run(second['run_id'])
        self.assertFalse(again['created'])
        self.assertEqual(self.staging(), before)
        self.assertEqual(self.ingestion().ingest_pending()['ingested'], 0)

    def test_changed_content_creates_a_new_snapshot_of_the_same_item(self):
        self.run_all(('youtube',))
        with self.storage.connect() as db:
            payload = json.loads(db.execute('SELECT payload FROM youtube_videos').fetchone()[0])
            db.execute('UPDATE youtube_videos SET payload=?', (json.dumps(dict(payload, title='changed title'), ensure_ascii=False),))
            db.execute('DELETE FROM youtube_video_provenance')  # re-observed through the origin-unknown path
        self.ingestion().legacy_backfill('youtube')
        self.assertEqual(self.count('SELECT count(*) FROM source_items'), 1)
        self.assertEqual(self.count('SELECT count(*) FROM item_snapshots'), 2)

    # 13: not provided / null / empty / expired / deleted stay distinct
    def test_field_states_and_lifecycle_states_are_distinct(self):
        self.run_all(('instagram_graph',))
        with self.storage.connect() as db:
            db.execute("UPDATE instagram_media SET caption=NULL,caption_purged_at=?", (stamp(self.now),))
            db.execute("UPDATE instagram_media SET origin='legacy_unknown'")
        self.ingestion().legacy_backfill('instagram_graph')
        with self.storage.reader() as db:
            states = [json.loads(r[0]) for r in db.execute('SELECT field_states_json FROM item_snapshots ORDER BY captured_at')]
        self.assertEqual(states[0]['caption'], 'provided')
        self.assertEqual(states[-1]['caption'], 'expired')
        record = {'external_key': 'abcdefghijk', 'field_states': {'title': 'null', 'description': 'empty', 'duration': 'not_provided'},
                  'fields': {}, 'published_at': None}
        rules = {r['rule_code']: r for r in dq.content_rules('youtube', record)}
        self.assertEqual(rules['fields.states']['details']['not_present'], ['description', 'duration', 'title'])
        self.assertEqual(rules['time.published_at']['outcome'], 'NOT_APPLICABLE')

    # 14, 15: malformed input is isolated without storing its values
    def test_malformed_ids_times_types_and_text_are_quarantined_without_values(self):
        run = dict(source_code='youtube', run_ref='manual:bad', origin='mock', coverage='COMPLETE', observed_at=stamp(self.now),
                   request_context={'q': '두쫀쿠'}, source_schema_version='youtube-v3-video-v1')
        bad = [dict(external_key='no', payload_ref='youtube_videos:no', payload_hash='1' * 64, fields={}),
               dict(external_key='abcdefghijk', published_at='2026-10-27T00:00:00', payload_ref='r', payload_hash='2' * 64, fields={}),
               dict(external_key='bcdefghijkl', payload_ref='r', payload_hash='3' * 64, fields={'view_count': '-5'}),
               dict(external_key='cdefghijklm', payload_ref='r', payload_hash='4' * 64, fields={'title': 'bad\udc80text'}),
               dict(external_key='defghijklmn', payload_ref='r', payload_hash='5' * 64, fields={}, payload_valid=False)]
        good = dict(external_key='efghijklmno', published_at='2026-10-27T09:00:00+09:00', payload_ref='r', payload_hash='6' * 64,
                    fields={'view_count': '12345678901234567890'}, field_states={'title': 'provided'})
        summary = self.ingestion().ingest(run, bad + [good])
        self.assertEqual((summary['rejected_items'], summary['staged_items']), (5, 1))
        with self.storage.reader() as db:
            fails = db.execute("SELECT rule_code,item_key,subject_hash,details_json FROM quality_results WHERE outcome='FAIL'").fetchall()
            self.assertEqual({r[0] for r in fails}, {'format.external_key', 'time.published_at', 'type.counts', 'encoding.text', 'payload.json'})
            self.assertTrue(all(r[1] is None and len(r[2]) == 64 for r in fails))
            self.assertFalse(any('-5' in r[3] or 'bad' in r[3] for r in fails))
            self.assertEqual([tuple(r) for r in db.execute('SELECT external_key FROM source_items')], [('efghijklmno',)])

    def test_metric_values_units_and_periods_are_checked(self):
        definitions = METRIC_DEFINITIONS
        base = dict(metric_code='instagram.observed_posts', unit='unique_observed_post', value_state='OBSERVED', count_value=0)
        self.assertEqual(dq.status(dq.metric_rules(base, definitions, True)), 'PASS')  # a measured zero is valid
        for broken in (dict(base, unit='percent'), dict(base, count_value=-1), dict(base, decimal_value=float('nan'), count_value=None),
                       dict(base, value_state='NOT_OBSERVED'),
                       dict(base, period_start='2026-10-02T00:00:00+00:00', period_end='2026-10-01T00:00:00+00:00')):
            self.assertEqual(dq.status(dq.metric_rules(broken, definitions, True)), 'FAIL', broken)
        self.assertEqual(dq.status(dq.metric_rules(dict(base, value_state='NOT_OBSERVED', count_value=None), definitions, True)), 'PASS')

    # 16: partial collection is not complete success
    def test_partial_execution_is_staged_with_partial_coverage(self):
        old = self.tracking.account('youtube', 'local-default')
        from dataclasses import replace
        self.tracking.accounts[('youtube', 'local-default')] = replace(old, options={'max_pages': 2})

        class Flaky(MockTransport):
            def __call__(self, request, timeout=None):
                if 'pageToken=' in request.full_url:
                    raise URLError('fixture timeout')
                raw = super().__call__(request, timeout)
                if urlsplit(request.full_url).path.endswith('/search'):
                    data = json.loads(raw.getvalue()); data['nextPageToken'] = 'next'
                    return MockResponse(json.dumps(data).encode())
                return raw
        job = self.job()
        controller = mock_controller(self.storage, self.planner, Flaky())
        result = controller.run_once(job['job_id'])
        for _ in range(2):
            self.advance(); result = controller.run_once(job['job_id'])
        self.assertEqual(result['status'], 'PARTIAL')
        with self.storage.reader() as db:
            run = db.execute('SELECT * FROM ingest_runs').fetchone()
            self.assertEqual((run['coverage'], run['execution_status'], run['staged_items']), ('PARTIAL', 'PARTIAL', 1))
            self.assertEqual(db.execute("SELECT outcome FROM quality_results WHERE rule_code='coverage.scope'").fetchone()[0], 'WARN')
        complete = self.run_all(('naver_datalab',))[1][0]
        self.assertEqual(complete['ingestion']['quality'].get('WARN'), None)

    def test_failed_rss_run_is_not_observed_not_zero(self):
        failed = CollectionService(self.storage, fetcher=lambda _: (b'<html>down</html>', 200)).collect()
        self.assertFalse(failed['success'])
        with self.storage.reader() as db:
            run = db.execute('SELECT coverage,staged_items,execution_status FROM ingest_runs').fetchone()
        self.assertEqual(tuple(run), ('NONE', 0, 'FAILED'))

    # 18: expiry and deletion propagate; expired data is never revived by re-ingestion
    def test_expired_and_deleted_content_propagate_and_are_not_revived(self):
        _, results, _ = self.run_all(('youtube',))
        with self.storage.connect() as db:
            expiry = db.execute('SELECT expires_at FROM youtube_videos').fetchone()[0]
        later = stamp(self.now + timedelta(days=30))
        self.assertEqual(self.ingestion().purge(later)['expired_items'], 1)
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT availability FROM source_items').fetchone()[0], 'EXPIRED')
            self.assertEqual(db.execute('SELECT count(*) FROM item_snapshots WHERE purged_at IS NULL').fetchone()[0], 0)
        self.now += timedelta(days=30)
        self.assertGreater(self.now.isoformat(), expiry)
        before = self.staging()
        self.ingestion().execution_run(results[0]['run_id'])  # re-ingest after expiry: nothing revived
        self.assertEqual(self.staging()['source_items'], before['source_items'])
        with self.storage.connect() as db:  # upstream deletion of a still-valid item
            db.execute('DELETE FROM youtube_videos')
            db.execute("UPDATE source_items SET availability='AVAILABLE',expires_at=NULL")
        self.assertEqual(self.ingestion().purge()['deleted_items'], 1)

    # 19: origin-unknown legacy rows stay labelled
    def test_legacy_rows_stay_origin_unknown_in_a_labelled_logical_run(self):
        self.run_all(('youtube',))
        with self.storage.connect() as db:
            db.execute('DELETE FROM youtube_video_provenance')
        summary = self.ingestion().legacy_backfill('youtube')
        with self.storage.reader() as db:
            run = db.execute('SELECT * FROM ingest_runs WHERE ingest_run_id=?', (summary['ingest_run_id'],)).fetchone()
            self.assertEqual((run['origin'], run['coverage'], run['run_ref']), ('legacy_unknown', 'UNKNOWN', 'legacy:youtube:origin-unknown'))
            self.assertIsNone(run['execution_status'])
            self.assertEqual(db.execute('SELECT origin FROM run_items WHERE ingest_run_id=?', (run['ingest_run_id'],)).fetchone()[0], 'legacy_unknown')
            self.assertEqual(db.execute('SELECT first_origin FROM source_items').fetchone()[0], 'mock')  # first real observation kept

    def test_naver_transient_metrics_keep_whole_request_context(self):
        body = {'startDate': '2026-10-01', 'endDate': '2026-10-02', 'timeUnit': 'date',
                'keywordGroups': [{'groupName': 'FOOD_a', 'keywords': ['두쫀쿠']}, {'groupName': 'FOOD_b', 'keywords': ['쿠키']}]}
        results = [{'title': 'FOOD_a', 'keywords': ['두쫀쿠'], 'data': [{'period': '2026-10-01', 'ratio': 100}]},
                   {'title': 'FOOD_b', 'keywords': ['쿠키'], 'data': []}]
        metrics = naver_metrics(body, results, stamp(self.now))
        self.assertEqual([m['value_state'] for m in metrics], ['OBSERVED', 'NOT_PROVIDED'])
        self.assertIsNone(metrics[1]['decimal_value'])
        other = naver_metrics(dict(body, endDate='2026-10-03'), results, stamp(self.now))
        self.assertNotEqual(metrics[0]['context_hash'], other[0]['context_hash'])
        self.assertEqual(metrics[0]['comparison_context']['keywordGroups'], body['keywordGroups'])

    def test_item_keys_are_deterministic_per_source(self):
        self.assertEqual(item_key('youtube', 'abc'), item_key('youtube', 'abc'))
        self.assertNotEqual(item_key('youtube', 'abc'), item_key('instagram_graph', 'abc'))
