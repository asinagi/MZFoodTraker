"""ORACLE-BACKEND-COMPLETION-1 Phase C: Google RSS discovery to B8 analytics on one Oracle backend, end to end over HTTP.

The admin server (as `python -m food_trends --backend oracle-test` assembles it) and the B8 app API (as
`app_api --store oracle-test` reads, through its own connections) run against the same target while every other
sqlite3 connection is refused: RSS Mock collection -> food candidates -> admin approval -> exact alias -> tracking ->
Oracle Query Planner -> Mock collection -> B4/B5 -> Oracle storage -> B8 queries. Unapproved candidates never reach
analytics; replays and two aliases matching one post count once; alias changes (after the target's maintenance) and
pause/resume are reflected; failed, partial and expired observations keep their states; every admin request gets its
success or error code and every B8 response keeps the OpenAPI v1 contract. The stand-in runs by default; the Oracle
subclass repeats the flow on the disposable MZ_TEST schema.
"""
import json
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request
from food_trends import app_contract as contract
from food_trends.__main__ import oracle_admin_server
from food_trends.analytics_reader import TargetAnalyticsStore
from food_trends.analytics_service import FoodAnalytics
from food_trends.app_api import AppApi, make_app_server
from food_trends.execution_mock import MockResponse
from food_trends.runtime_storage import oracle_maintenance
from food_trends.target_db import SQLiteTarget
from food_trends.target_migration import integrity_checks
from food_trends.target_repository import TargetRepository
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema, settle_after_ddl
import test_oracle_backend as backend_tests
from test_admin_oracle import OPENER, TOKEN, AdminClient
from test_oracle_backend import Feed
from test_oracle_rss import BAGEL, BRAND, COOKIE, PERSON, feed

NOW = datetime(2026, 11, 2, 3, tzinfo=timezone.utc)  # 2026-11-02 12:00 KST
KST = timezone(timedelta(hours=9))
UNAPPROVED = (BAGEL[0], BRAND[0], PERSON[0])


class Instagram(Feed):
    """The Instagram behaviour of each phase: posts, an outage, or pages that never end (the run is LIMITED)."""

    def __init__(self):
        super().__init__()
        self.mode, self.posts = 'normal', [('900001', '#두쫀쿠 #두바이쫀득쿠키 #디저트')]

    def __call__(self, request, timeout=None):
        url = urlsplit(request.full_url)
        if self.mode == 'down' and url.path.endswith(('recent_media', 'ig_hashtag_search')):
            self.calls.append('down')
            raise URLError('fixture outage')
        if self.mode == 'endless' and url.path.endswith('recent_media'):
            page = int(parse_qs(url.query).get('after', ['0'])[0])
            self.calls.append('recent_media')
            body = {'data': [{'id': '95%04d' % page, 'timestamp': '2026-10-26T00:00:00Z', 'media_type': 'IMAGE', 'caption': '#두쫀쿠',
                              'permalink': 'https://www.instagram.com/p/mock95%04d/' % page}],
                    'paging': {'cursors': {'after': str(page + 1)}, 'next': 'https://graph.example/next'}}
            return MockResponse(json.dumps(body, ensure_ascii=False).encode())
        return super().__call__(request, timeout)


class OracleEndToEndStandInTests(unittest.TestCase):
    sqlite_refused = backend_tests.OracleBackendStandInTests.sqlite_refused

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.target = self.make_target()
        self.repo = TargetRepository(self.target, clock=lambda: self.now)
        self.repo.migrate()
        self.xml, self.instagram = feed(COOKIE, BAGEL, BRAND, PERSON), Instagram()
        server, self.backend = oracle_admin_server(self.repo, 0, TOKEN, lambda timeout: (self.xml, 200), lambda: self.now, self.instagram)
        self.start(server)
        self.addCleanup(self.backend.execution.release)
        self.admin = AdminClient(server)
        # B8 reads the same target through its own connections, as the separate app API process does.
        reader = self.reader_target()
        app = AppApi(FoodAnalytics(TargetAnalyticsStore(TargetRepository(reader)), clock=lambda: self.now, origin='mock'), data_mode='database')
        self.b8 = 'http://127.0.0.1:%d' % self.start(make_app_server(app, port=0, quiet=True)).server_address[1]
        self.doc, self.settled = contract.openapi(), False

    def make_target(self):
        return SQLiteTarget(Path(self.temp.name) / 'target.sqlite3')

    def reader_target(self):
        return SQLiteTarget(self.target.path)

    def start(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    # Requests

    def ok(self, method, path, body=None):
        status, data = self.admin.call(method, path, body)
        self.assertEqual(status, 200, (method, path, data))
        return data

    def review(self, body):
        return dict(body, request_key=uuid.uuid4().hex, approved=True, actor_ref='reviewer', reason='관리 화면 검토')

    def track(self, body):
        return dict(body, request_key=uuid.uuid4().hex, actor_ref='reviewer', reason='관리 화면 추적')

    def get(self, path):
        """One B8 request; every response is checked against the OpenAPI v1 contract."""
        if not self.settled:  # Oracle refuses a read-only snapshot of tables defined a moment ago (B8 answers 503 then)
            settle_after_ddl(self.target)
            self.settled = True
        try:
            with OPENER.open(Request(self.b8 + path), timeout=120) as response:
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

    def day(self, food_id, moment=None, source='instagram_graph', period='7d'):
        end = self.now.astimezone(KST).date().isoformat()
        series = self.data('/api/v1/foods/%s/timeline?period=%s&end_date=%s&source=%s' % (food_id, period, end, source))['series'][0]
        return {p['start_date']: p for p in series['points']}[(moment or self.now).astimezone(KST).date().isoformat()]

    def run_jobs(self):
        results = []
        while True:
            ticked = self.ok('POST', '/api/execution/tick', {'mode': 'mock', 'max_jobs': 50})
            results += ticked['results']
            if not ticked['results']:
                return results

    def target_of(self, food_id, source):
        return next(t for t in self.ok('GET', '/api/tracking/targets?food_id=' + food_id)['items'] if t['source_id'] == source)

    def counts(self, *tables):
        return {t: self.repo.count(t) for t in tables}

    # Phase C

    def test_rss_discovery_to_b8_analytics_on_one_backend(self):
        with self.sqlite_refused() as opened:
            # 1. RSS Mock collection -> candidates (FOOD and REVIEW items; the brand behind the type gate)
            collected = self.ok('POST', '/api/collect')
            self.assertEqual((collected['success'], collected['entity_resolution']['candidates']), (True, 3))
            candidates = {c['surface_form']: c for c in self.ok('GET', '/api/food-candidates')['items']}
            self.assertEqual({k: c['status'] for k, c in candidates.items()}, {COOKIE[0]: 'NEEDS_REVIEW', BAGEL[0]: 'NEEDS_REVIEW', BRAND[0]: 'NEEDS_REVIEW'})
            # Candidates are not foods: B8 lists nothing yet.
            self.assertEqual(self.data('/api/v1/foods')['items'], [])
            # 2. admin approval (the brand cannot be approved as a food) and exact alias confirmation
            brand = candidates[BRAND[0]]
            self.assertEqual(self.admin.call('POST', '/api/foods/review', self.review(dict(candidate_id=brand['candidate_id'], action='create',
                                                                                          expected_revision=brand['revision'])))[0], 409)
            self.assertEqual(self.admin.call('POST', '/api/foods/review', self.review(dict(candidate_id='CAND_missing', action='reject',
                                                                                          expected_revision=1)))[0], 404)
            cookie = candidates[COOKIE[0]]
            food = self.ok('POST', '/api/foods/review', self.review(dict(candidate_id=cookie['candidate_id'], expected_revision=cookie['revision'],
                                                                        action='create', canonical_name='두쫀쿠')))['resolution']
            alias = self.ok('POST', '/api/foods/alias', self.review(dict(action='approve', food_id=food['food_id'], surface_form='두바이쫀득쿠키',
                                                                         expected_revision=self.ok('GET', '/api/foods/' + food['food_id'])['revision'])))
            self.assertEqual(self.ok('GET', '/api/foods/resolve?name=' + quote('두바이쫀득쿠키'))['resolution']['food_id'], food['food_id'])
            # 3. tracking activation
            for source in ('instagram_graph', 'youtube'):
                registered = self.ok('POST', '/api/tracking/register', self.track(dict(food_id=food['food_id'], source_id=source)))
                self.ok('POST', '/api/tracking/activate', self.track(dict(target_id=registered['target_id'], expected_revision=registered['revision'],
                                                                          approved=True)))
            # 4. Oracle Query Planner: the preview writes nothing; the commit persists the plans
            before = self.counts('planner_plans', 'planner_queries', 'quota_reservations')
            preview = self.ok('GET', '/api/planner/preview')
            self.assertEqual({p['source_id'] for p in preview['plans']}, {'instagram_graph', 'youtube'})
            self.assertEqual(self.counts(*before), before)
            committed = self.ok('POST', '/api/planner/commit', {'request_key': uuid.uuid4().hex})
            self.assertTrue(committed['plans'])
            # 5-7. Mock collection; B4 staging and B5 links for every terminal job; the target's maintenance links the RSS item
            jobs = self.ok('POST', '/api/execution/schedule', {'mode': 'mock'})['items']
            self.assertEqual({j['source_id'] for j in jobs}, {'instagram_graph', 'youtube'})
            results = self.run_jobs()
            self.assertEqual({r['status'] for r in results}, {'SUCCEEDED'})
            self.assertFalse([r for r in results if (r.get('ingestion') or {}).get('state') == 'pending_retry'])
            self.assertTrue(self.data('/api/v1/foods/' + food['food_id'])['freshness']['reprocessing_pending'])
            maintained = oracle_maintenance(self.backend)
            self.assertGreater(maintained['entity_changes_reprocessed'], 0)
            # 8. B8 reads what the admin server wrote: the approved food only, one post counted once although two aliases and two
            # jobs matched it, the RSS item linked through the approved alias
            foods = self.data('/api/v1/foods')['items']
            self.assertEqual([f['food_id'] for f in foods], [food['food_id']])
            point = self.day(food['food_id'])
            self.assertEqual((point['value'], point['value_state'], point['coverage']), (1, 'OBSERVED', 'COMPLETE'))
            rss_mentions = [m for m in self.repo.rows('food_mentions') if m['source_code'] == 'google_trends_rss' and m['status'] == 'ACTIVE']
            self.assertEqual({m['food_id'] for m in rss_mentions}, {food['food_id']})
            self.assertEqual(len({m['item_key'] for m in rss_mentions}), 1)
            ranking = self.data('/api/v1/trends/rankings?source=instagram_graph&end_date=%s' % self.now.astimezone(KST).date().isoformat())
            # Rankings need every day of the period observed: the one approved food is considered and set aside for coverage.
            self.assertEqual((ranking['items'], ranking['excluded']['insufficient_coverage']), ([], 1))
            sources = {s['source_code']: s for s in self.data('/api/v1/sources/status')['sources']}
            self.assertIsNotNone(sources['instagram_graph']['last_successful_run_at'])
            text = json.dumps([self.get(p)[1] for p in ('/api/v1/foods', '/api/v1/foods/' + food['food_id'], '/api/v1/sources/status',
                                                         '/api/v1/trends/rankings?source=google_trends_rss&include_zero=true')], ensure_ascii=False)
            self.assertFalse([name for name in UNAPPROVED if name in text])  # unapproved candidates never reach analytics
            # 9. replays add nothing: the same feed again, a finished job again, a tick with nothing due, maintenance again
            stable = self.counts('source_contents', 'source_items', 'item_snapshots', 'food_mentions', 'food_candidates', 'execution_jobs')
            again = self.ok('POST', '/api/collect')  # a new observation run of the same items: no new item, candidate or mention
            self.assertEqual((again['new_count'], again['entity_resolution']['candidates'], again['ingestion']['created']), (0, 3, True))
            self.assertIsNone(self.ok('POST', '/api/execution/run', {'job_id': jobs[0]['job_id']})['result'])
            self.assertEqual(self.ok('POST', '/api/execution/tick', {'mode': 'mock', 'max_jobs': 10})['results'], [])
            self.assertEqual(oracle_maintenance(self.backend)['entity_changes_reprocessed'], 0)
            self.assertEqual(self.counts(*stable), stable)
            self.assertEqual(self.day(food['food_id'])['value'], 1)
            # 10. alias changes (after maintenance): one of two matching aliases revoked still counts the post once; both revoked -> an
            # observed 0; a new alias another held hashtag matches -> 1 again
            aliases = {a['surface_form']: a for a in self.ok('GET', '/api/foods/' + food['food_id'])['approved_aliases']}
            self.ok('POST', '/api/foods/alias', self.review(dict(action='revoke', alias_id=aliases['두쫀쿠']['alias_id'],
                                                                 expected_revision=aliases['두쫀쿠']['revision'])))
            oracle_maintenance(self.backend)
            self.assertEqual(self.day(food['food_id'])['value'], 1)
            self.ok('POST', '/api/foods/alias', self.review(dict(action='revoke', alias_id=alias['alias_id'], expected_revision=alias['revision'])))
            self.assertTrue(self.data('/api/v1/foods/' + food['food_id'])['freshness']['reprocessing_pending'])
            oracle_maintenance(self.backend)
            self.assertEqual((self.day(food['food_id'])['value'], self.day(food['food_id'])['value_state']), (0, 'OBSERVED'))
            self.ok('POST', '/api/foods/alias', self.review(dict(action='approve', food_id=food['food_id'], surface_form='디저트',
                                                                 expected_revision=self.ok('GET', '/api/foods/' + food['food_id'])['revision'])))
            oracle_maintenance(self.backend)
            self.assertEqual(self.day(food['food_id'])['value'], 1)
            self.assertFalse(self.data('/api/v1/foods/' + food['food_id'])['freshness']['reprocessing_pending'])
            # 11. the alias changes moved the food revision: the target waits for a refresh; pause keeps it out of the schedule,
            # refresh + resume starts a new generation whose queries carry the current aliases
            instagram = self.target_of(food['food_id'], 'instagram_graph')
            self.assertEqual((instagram['effective_status'], instagram['blockers']), ('BLOCKED', ['STALE_FOOD_REVISION']))
            paused = self.ok('POST', '/api/tracking/pause', self.track(dict(target_id=instagram['target_id'], expected_revision=instagram['revision'])))
            self.now += timedelta(hours=7)
            self.assertNotIn('instagram_graph', [j['source_id'] for j in self.ok('POST', '/api/execution/schedule', {'mode': 'mock'})['items']])
            refreshed = self.ok('POST', '/api/tracking/refresh', self.track(dict(target_id=paused['target_id'], expected_revision=paused['revision'])))
            resumed = self.ok('POST', '/api/tracking/resume', self.track(dict(target_id=refreshed['target_id'], expected_revision=refreshed['revision'],
                                                                             approved=True)))
            self.assertEqual((resumed['status'], resumed['generation']), ('ACTIVE', refreshed['generation'] + 1))
            resumed_jobs = self.ok('POST', '/api/execution/schedule', {'mode': 'mock'})['items']
            self.assertEqual({j['source_id'] for j in resumed_jobs}, {'instagram_graph'})
            self.assertEqual({r['status'] for r in self.run_jobs()}, {'SUCCEEDED'})
            self.assertEqual([e['action'] for e in self.ok('GET', '/api/tracking/events?target_id=' + instagram['target_id'])['items']],
                             ['REGISTERED', 'ACTIVATE', 'PAUSE', 'REFRESH', 'RESUME'])
            observed_day = NOW
            # 12. next day the provider is down: after the retries the run failed and the day is NOT_OBSERVED, never a zero
            self.now = NOW + timedelta(days=1)
            self.instagram.mode = 'down'
            down = [j['job_id'] for j in self.ok('POST', '/api/execution/schedule', {'mode': 'mock'})['items']]
            self.assertTrue(down)
            for _ in range(6):
                for job_id in down:
                    self.ok('POST', '/api/execution/run', {'job_id': job_id})
                self.now += timedelta(minutes=10)
            self.assertEqual({self.ok('GET', '/api/execution/jobs/' + j)['items'][0]['status'] for j in down}, {'FAILED'})
            failed = self.day(food['food_id'], NOW + timedelta(days=1))
            self.assertEqual((failed['value'], failed['value_state']), (None, 'NOT_OBSERVED'))
            # 13. the next day every page advertises another: the run stops at the page cap and the day is LIMITED, not COMPLETE
            self.now = NOW + timedelta(days=2)
            self.instagram.mode = 'endless'
            for job in self.ok('POST', '/api/execution/schedule', {'mode': 'mock'})['items']:
                self.assertEqual(self.ok('POST', '/api/execution/run', {'job_id': job['job_id']})['result']['status'], 'LIMITED')
            limited = self.day(food['food_id'], NOW + timedelta(days=2))
            self.assertEqual((limited['value_state'], limited['coverage']), ('OBSERVED', 'LIMITED'))
            # 14. 31 days after the first observation the caption window has closed: the target's maintenance expires the evidence
            self.now = observed_day + timedelta(days=31)
            purged = oracle_maintenance(self.backend, self.now)
            self.assertGreater(purged['staging']['purged_captions'], 0)
            self.assertGreater(purged['links']['expired_mentions'], 0)
            expired = self.day(food['food_id'], observed_day, period='90d')
            self.assertEqual((expired['value'], expired['value_state']), (None, 'EVIDENCE_EXPIRED'))
        self.assertEqual(opened, [])
        checks, _ = integrity_checks(self.repo, self.now)
        self.assertEqual({k: checks[k] for k in ('foreign_key_errors', 'unresolved_payload_refs', 'blocked_policy_rows', 'expired_rows_in_target')},
                         dict(foreign_key_errors=0, unresolved_payload_refs=0, blocked_policy_rows=0, expired_rows_in_target=0))
        for table, key in backend_tests.NATURAL_KEYS.items():
            values = [tuple(r[c] for c in key) for r in self.repo.rows(table)]
            self.assertEqual(len(values), len(set(values)), table)


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleEndToEndTests(OracleEndToEndStandInTests):
    """The flow on the real disposable Oracle test schema; B8 reads through a separate connection pool; no sqlite3 at all."""

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
