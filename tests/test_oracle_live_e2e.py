"""ORACLE-LIVE-INTEGRATION-1 Phase 4: the live path end to end on one Oracle target, then back to SQLite.

The admin server runs on the live assembly (oracle_live) and B8 reads the same target through its own connections;
the existing provider clients and the RSS collector send every request into a recording fake (no request leaves the
process, and every other sqlite3 connection is refused during the live part). In order: live RSS discovery -> food
candidates -> admin approval -> exact alias -> Instagram, YouTube and NAVER tracking -> Oracle planning -> scheduling
with budgets (a spent daily search budget sends nothing) -> collection by the existing clients -> staging, DQ and
observations on the target -> B8 -> a provider failure retried, replays, a worker crash recovered by a restarted
runtime, evidence expiry -> Oracle to SQLite recovery, after which the SQLite store answers B8 as the target does and
a re-run writes nothing. Every run is live_api, nothing is counted twice, no source policy is broken, an observed zero
and a missing observation stay apart, expired content does not come back and every B8 response keeps OpenAPI v1.
"""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request
from food_trends import app_contract as contract
from food_trends.__main__ import oracle_admin_server
from food_trends.analytics_reader import SQLiteAnalyticsStore, TargetAnalyticsStore
from food_trends.analytics_service import FoodAnalytics
from food_trends.app_api import AppApi, make_app_server
from food_trends.oracle_live import oracle_live_backend
from food_trends.reverse_recovery import recover
from food_trends.runtime_storage import oracle_maintenance
from food_trends.storage import Storage
from food_trends.target_db import SQLiteTarget
from food_trends.target_migration import integrity_checks
from food_trends.target_repository import TargetRepository
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema, settle_after_ddl
import test_oracle_backend as backend_tests
from test_admin_oracle import OPENER, TOKEN, AdminClient
import test_oracle_live as live_tests
from test_oracle_live import KST, LIVE_ENV, NOW, SCOPES, LiveWire
from test_oracle_rss import BAGEL, BRAND, COOKIE, PERSON, feed

UNAPPROVED = (BAGEL[0], BRAND[0], PERSON[0])


class OracleLiveEndToEndStandInTests(unittest.TestCase):
    sqlite_refused = backend_tests.OracleBackendStandInTests.sqlite_refused
    grants = live_tests.OracleLiveStandInTests.grants
    transfer_all = live_tests.OracleLiveStandInTests.transfer_all

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.target = self.make_target()
        self.repo = TargetRepository(self.target, clock=lambda: self.now)
        self.repo.migrate()
        self.wire = LiveWire()
        self.wire.rss = feed(COOKIE, BAGEL, BRAND, PERSON)
        self.wire.install(self)
        env = patch.dict(os.environ, dict(LIVE_ENV, YOUTUBE_DAILY_SEARCH_LIMIT='2'))
        env.start()
        self.addCleanup(env.stop)
        self.registry = self.grants(*SCOPES)
        self.transfer_all()  # the approved writer transfer (reads the legacy store once, before the live part)
        self.backend = self.start_runtime()
        reader = self.reader_target()
        app = AppApi(FoodAnalytics(TargetAnalyticsStore(TargetRepository(reader)), clock=lambda: self.now, origin='live_api'), data_mode='database')
        self.b8 = 'http://127.0.0.1:%d' % self.serve(make_app_server(app, port=0, quiet=True)).server_address[1]
        self.doc, self.settled = contract.openapi(), False

    def make_target(self):
        return SQLiteTarget(Path(self.temp.name) / 'target.sqlite3')

    def reader_target(self):
        return SQLiteTarget(self.target.path)

    def serve(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def start_runtime(self):
        """One runtime process: the live assembly behind the admin server."""
        backend = oracle_live_backend(self.repo, self.registry, clock=lambda: self.now)
        self.addCleanup(backend.execution.release)
        server, backend = oracle_admin_server(self.repo, 0, TOKEN, backend=backend)
        self.admin = AdminClient(self.serve(server))
        return backend

    def ok(self, method, path, body=None):
        status, data = self.admin.call(method, path, body)
        self.assertEqual(status, 200, (method, path, data))
        return data

    def review(self, body):
        return dict(body, request_key=uuid.uuid4().hex, approved=True, actor_ref='reviewer', reason='관리 화면 검토')

    def track(self, body):
        return dict(body, request_key=uuid.uuid4().hex, actor_ref='reviewer', reason='관리 화면 추적')

    def get(self, path, base=None):
        if not self.settled:
            settle_after_ddl(self.target)
            self.settled = True
        try:
            with OPENER.open(Request((base or self.b8) + path), timeout=120) as response:
                status, body = response.status, json.loads(response.read())
        except HTTPError as error:
            status, body = error.code, json.loads(error.read())
        schema = contract.response_schema(self.doc, contract.operation_for(self.doc, urlsplit(path).path)[1], status)
        self.assertEqual(contract.check(self.doc, body, schema), [], path)
        return status, body

    def data(self, path):
        status, body = self.get(path)
        self.assertEqual(status, 200, body)
        return body['data']

    def day(self, food_id, moment, source='instagram_graph', period='7d'):
        end = self.now.astimezone(KST).date().isoformat()
        series = self.data('/api/v1/foods/%s/timeline?period=%s&end_date=%s&source=%s' % (food_id, period, end, source))['series'][0]
        return {p['start_date']: p for p in series['points']}[moment.astimezone(KST).date().isoformat()]

    def tick(self):
        results = []
        while True:
            batch = self.ok('POST', '/api/execution/tick', {'mode': 'live', 'max_jobs': 50})['results']
            results += batch
            if not batch:
                return results

    def counts(self, *tables):
        return {t: self.repo.count(t) for t in tables}

    # Phase 4

    def test_live_discovery_to_b8_and_back_to_sqlite(self):
        with self.sqlite_refused() as opened:
            # 1-2. RSS through the live interface (the existing collector; the wire answers) -> candidates
            collected = self.ok('POST', '/api/collect')
            self.assertEqual((collected['success'], collected['entity_resolution']['candidates']), (True, 3))
            self.assertEqual(self.ok('GET', '/api/status')['latest_run']['origin'], 'live_api')
            candidates = {c['surface_form']: c for c in self.ok('GET', '/api/food-candidates')['items']}
            self.assertEqual(self.data('/api/v1/foods')['items'], [])  # candidates are not foods
            # 3-4. admin approval and an exact alias
            cookie = candidates[COOKIE[0]]
            food = self.ok('POST', '/api/foods/review', self.review(dict(candidate_id=cookie['candidate_id'], expected_revision=cookie['revision'],
                                                                        action='create', canonical_name='두쫀쿠')))['resolution']
            self.ok('POST', '/api/foods/alias', self.review(dict(action='approve', food_id=food['food_id'], surface_form='두바이쫀득쿠키',
                                                                 expected_revision=self.ok('GET', '/api/foods/' + food['food_id'])['revision'])))
            self.assertEqual(self.ok('GET', '/api/foods/resolve?name=' + quote('두바이쫀득쿠키'))['resolution']['food_id'], food['food_id'])
            # 5. Instagram, YouTube and NAVER tracking on the live accounts
            for source in ('instagram_graph', 'youtube', 'naver_datalab'):
                target = self.ok('POST', '/api/tracking/register', self.track(dict(food_id=food['food_id'], source_id=source,
                                                                                   account_scope=SCOPES[source])))
                active = self.ok('POST', '/api/tracking/activate', self.track(dict(target_id=target['target_id'], expected_revision=target['revision'],
                                                                                   approved=True)))
                self.assertEqual(active['effective_status'], 'ACTIVE')
            # 6. Oracle Query Planner: preview writes nothing, commit persists
            before = self.counts('planner_plans', 'quota_reservations', 'execution_jobs')
            self.assertEqual({p['source_id'] for p in self.ok('GET', '/api/planner/preview')['plans']}, {'instagram_graph', 'youtube', 'naver_datalab'})
            self.assertEqual(self.counts(*before), before)
            self.assertTrue(self.ok('POST', '/api/planner/commit', {'request_key': uuid.uuid4().hex})['plans'])
            # 7-9. live scheduling, budgets, collection by the existing clients, staging/DQ/observations on the target
            jobs = self.ok('POST', '/api/execution/schedule', {'mode': 'live'})['items']
            self.assertEqual({j['mode'] for j in jobs}, {'live'})
            youtube = sorted(j['job_id'] for j in jobs if j['source_id'] == 'youtube')
            self.assertEqual(len(youtube), 3)  # one search per approved alias; the daily search budget is 2
            results = self.tick()
            statuses = {r['job_id']: (r['status'], r['error_kind']) for r in results}
            self.assertEqual(sorted(statuses[j] for j in youtube), [('RETRY_WAIT', 'budget'), ('SUCCEEDED', None), ('SUCCEEDED', None)])
            self.assertEqual(self.wire.calls.count('search'), 2 + 1)  # two YouTube searches and the NAVER request; the third search never left
            self.assertEqual({s for j, s in statuses.items() if j not in youtube}, {('SUCCEEDED', None)})
            oracle_maintenance(self.backend)  # links the RSS item through the approval made after its run
            # 10. B8 reads the same target: the approved food only, one post counted once over two hashtag jobs
            self.assertEqual([f['food_id'] for f in self.data('/api/v1/foods')['items']], [food['food_id']])
            first = self.day(food['food_id'], NOW)
            self.assertEqual((first['value'], first['value_state'], first['coverage']), (1, 'OBSERVED', 'COMPLETE'))
            sources = {s['source_code']: s for s in self.data('/api/v1/sources/status')['sources']}
            self.assertIsNotNone(sources['instagram_graph']['last_successful_run_at'])
            text = json.dumps([self.get(p)[1] for p in ('/api/v1/foods', '/api/v1/foods/' + food['food_id'], '/api/v1/sources/status')],
                              ensure_ascii=False)
            self.assertFalse([name for name in UNAPPROVED if name in text])
            # 11a. a provider failure is retried, not dropped; replays add nothing
            self.now = NOW + timedelta(days=1)
            self.wire.failures['recent_media'] = (503, {'error': {'code': 2, 'message': 'busy'}}, {'Retry-After': '5'})
            waited = [r for r in self.tick() if r['source_id'] == 'instagram_graph']
            self.assertEqual({(r['status'], r['error_kind']) for r in waited}, {('RETRY_WAIT', 'upstream')})
            self.assertEqual(self.day(food['food_id'], self.now)['value_state'], 'NOT_OBSERVED')  # not a zero
            self.wire.failures = {}
            self.now += timedelta(minutes=10)
            self.assertEqual({r['status'] for r in self.tick() if r['source_id'] == 'instagram_graph'}, {'SUCCEEDED'})
            self.assertEqual((self.day(food['food_id'], self.now)['value'], self.day(food['food_id'], NOW)['value']), (1, 1))
            stable = self.counts('source_contents', 'source_items', 'item_snapshots', 'food_mentions', 'food_candidates')
            self.assertEqual(self.ok('POST', '/api/collect')['new_count'], 0)
            done = next(j for j in youtube if statuses[j][0] == 'SUCCEEDED')
            self.assertIsNone(self.ok('POST', '/api/execution/run', {'job_id': done})['result'])
            self.assertEqual(self.counts(*stable), stable)
            # 11b. a worker crashes after claiming a job; a restarted runtime takes it over once its lease expires
            self.now += timedelta(hours=1)
            pending = [j for j in self.ok('POST', '/api/execution/schedule', {'mode': 'live'})['items'] if j.get('source_id') == 'instagram_graph']
            claim = self.backend.execution.claim('crashed-worker', pending[0]['job_id'])
            self.backend.execution.begin(claim)
            self.backend.execution.release()
            self.backend = self.start_runtime()  # the process starts again
            self.assertNotIn(pending[0]['job_id'], [r['job_id'] for r in self.tick()])  # the crashed worker's lease still holds it
            self.now += timedelta(minutes=3)
            taken = self.tick()
            self.assertEqual([(r['job_id'], r['status'], r['attempt_count']) for r in taken if r['job_id'] == pending[0]['job_id']],
                             [(pending[0]['job_id'], 'SUCCEEDED', 2)])
            # 11c. YouTube and NAVER tracking stop (the budget-deferred searches then end as cancelled, never sent); 31 days after the
            # first observation the caption window has closed and the target's maintenance expires the evidence
            for target in self.ok('GET', '/api/tracking/targets?food_id=' + food['food_id'])['items']:
                if target['source_id'] in ('youtube', 'naver_datalab'):
                    self.ok('POST', '/api/tracking/stop', self.track(dict(target_id=target['target_id'], expected_revision=target['revision'])))
            searches = self.wire.calls.count('search')
            self.now = NOW + timedelta(days=31)
            late = self.tick()
            self.assertEqual({(r['source_id'], r['status']) for r in late}, {('youtube', 'CANCELLED'), ('instagram_graph', 'SUCCEEDED')})
            self.assertEqual(self.wire.calls.count('search'), searches)  # no stopped source sent anything
            purged = oracle_maintenance(self.backend, self.now)
            self.assertGreater(purged['links']['expired_mentions'], 0)
            self.assertEqual(self.day(food['food_id'], NOW, period='90d')['value_state'], 'EVIDENCE_EXPIRED')
            self.assertEqual({j['status'] for j in self.backend.execution.status()['items']} - {'SUCCEEDED', 'FAILED', 'BLOCKED', 'CANCELLED'}, set())
        self.assertEqual(opened, [])
        # Everything is live_api, nothing twice, no policy row, no NAVER value, no YouTube mention.
        self.assertEqual({r['origin'] for r in self.repo.rows('execution_runs')} | {r['origin'] for r in self.repo.rows('rss_runs')}, {'live_api'})
        checks, _ = integrity_checks(self.repo, self.now)
        self.assertEqual({k: checks[k] for k in ('foreign_key_errors', 'unresolved_payload_refs', 'blocked_policy_rows', 'expired_rows_in_target')},
                         dict(foreign_key_errors=0, unresolved_payload_refs=0, blocked_policy_rows=0, expired_rows_in_target=0))
        for table, key in backend_tests.NATURAL_KEYS.items():
            values = [tuple(r[c] for c in key) for r in self.repo.rows(table)]
            self.assertEqual(len(values), len(set(values)), table)
        self.assertFalse([r for r in self.repo.rows('food_mentions') if r['source_code'] == 'youtube'])
        self.assertFalse([r for r in self.repo.rows('metric_observations') if r['source_code'] == 'naver_datalab'])
        # 12-13. Oracle -> SQLite recovery into a fresh v11 store; then both answer B8 alike and a re-run writes nothing
        self.backend.execution.release()
        destination = Path(self.temp.name) / 'recovered.sqlite3'
        Storage.create(destination)
        result = recover(destination, self.repo, now=self.now)
        self.assertTrue(result['converged'], result['plan_after']['summary'])
        with closing(sqlite3.connect(destination)) as db:
            self.assertEqual((db.execute('PRAGMA integrity_check').fetchone()[0], db.execute('PRAGMA foreign_key_check').fetchall()), ('ok', []))
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 4)  # the RSS discovery came back
            self.assertEqual(db.execute('SELECT count(*) FROM runs WHERE raw_response IS NOT NULL').fetchone()[0], 0)  # never invented
            youtube_rows = [r[0] for r in db.execute('SELECT expires_at FROM youtube_videos')]
        self.assertFalse([e for e in youtube_rows if e <= self.now.isoformat()])  # no YouTube snapshot past its 29 days came back
        sqlite_api = AppApi(FoodAnalytics(SQLiteAnalyticsStore(Storage(destination)), clock=lambda: self.now, origin='live_api'), data_mode='database')
        target_api = AppApi(FoodAnalytics(TargetAnalyticsStore(self.repo), clock=lambda: self.now, origin='live_api'), data_mode='database')
        end = self.now.astimezone(KST).date().isoformat()
        for request in ('/api/v1/foods', '/api/v1/foods/' + food['food_id'], '/api/v1/sources/status',
                        '/api/v1/foods/%s/timeline?period=90d&end_date=%s&source=instagram_graph' % (food['food_id'], end),
                        '/api/v1/foods/%s/metrics?period=7d&end_date=%s' % (food['food_id'], end)):
            with self.subTest(request=request):
                self.assertEqual(sqlite_api.handle('GET', request, {'Host': '127.0.0.1:8020'})[:2],
                                 target_api.handle('GET', request, {'Host': '127.0.0.1:8020'})[:2])
        again = recover(destination, self.repo, now=self.now)
        self.assertEqual(again['writes'], {'inserted': 0, 'content_rows': 0, 'updated': 0, 'ignored': 0})


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleLiveEndToEndTests(OracleLiveEndToEndStandInTests):
    """The live path on the real disposable Oracle test schema; B8 reads through a separate pool; no sqlite3 in the live part."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db

    def reader_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        return db


if __name__ == '__main__':
    unittest.main()
