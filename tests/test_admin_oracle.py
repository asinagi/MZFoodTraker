"""ORACLE-BACKEND-COMPLETION-1 Phase B: the admin web server on the Oracle runtime alone.

The existing server (api.make_server) and its handlers run over the Oracle backend exactly as
`python -m food_trends --backend oracle-test` assembles it (oracle_admin_server). Every route the web UI calls answers
from the target over loopback HTTP while every other sqlite3 connection is refused. The admin token, Origin and
loopback checks still guard every state change; invalid requests get the handlers' error codes; the legacy direct
collectors answer 503; RSS collection without a Mock response is refused; per-source collection, failure and budget
status is served; the CLI refuses oracle-production and SQLite commands mixed into the Oracle mode. The stand-in runs
by default; the Oracle subclass repeats every case on the disposable MZ_TEST schema.
"""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener
from food_trends.__main__ import main, oracle_admin_server
from food_trends.api import TOKEN_HEADER
from food_trends.target_db import SQLiteTarget
from food_trends.target_repository import TargetRepository
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema
import test_oracle_backend as backend_tests
from test_oracle_backend import Feed, InstagramDown
from test_oracle_rss import BAGEL, BRAND, COOKIE, feed

NOW = datetime(2026, 11, 2, 3, tzinfo=timezone.utc)
TOKEN = 'a1' * 20  # synthetic token of the disposable test server
OPENER = build_opener(ProxyHandler({}))
STATIC = ('/app.js', '/admin.js', '/style.css', '/foods.js', '/tracking.js', '/planner.js', '/execution.js',
          '/instagram.js', '/naver.js', '/youtube.js')


class AdminClient:
    """Loopback HTTP as the admin page sends it: JSON bodies and this server's admin token header."""

    def __init__(self, server):
        self.base = 'http://127.0.0.1:%d' % server.server_address[1]

    def call(self, method, path, body=None, token=TOKEN, headers=None, raw=None):
        data = raw if raw is not None else None if body is None else json.dumps(body, ensure_ascii=False).encode()
        sent = dict({'Content-Type': 'application/json'} if data is not None else {}, **(headers or {}))
        if token:
            sent[TOKEN_HEADER] = token
        try:
            with OPENER.open(Request(self.base + path, data=data, headers=sent, method=method), timeout=120) as response:
                return response.status, self._body(response)
        except HTTPError as error:
            return error.code, self._body(error)

    @staticmethod
    def _body(response):
        payload = response.read()
        return json.loads(payload) if response.headers.get_content_type() == 'application/json' else payload


class AdminOracleStandInTests(unittest.TestCase):
    sqlite_refused = backend_tests.OracleBackendStandInTests.sqlite_refused

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.target = self.make_target()
        self.repo = TargetRepository(self.target, clock=lambda: self.now)
        self.repo.migrate()
        self.xml = feed(COOKIE, BAGEL, BRAND)
        self.backend, self.http = self.serve()

    def make_target(self):
        return SQLiteTarget(Path(self.temp.name) / 'target.sqlite3')

    def serve(self, fetcher='mock', transport=None):
        server, backend = oracle_admin_server(self.repo, 0, TOKEN, self.fetch if fetcher == 'mock' else fetcher,
                                              lambda: self.now, transport or Feed())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(backend.execution.release)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return backend, AdminClient(server)

    def fetch(self, timeout):
        if isinstance(self.xml, Exception):
            raise self.xml
        return self.xml, 200

    def ok(self, method, path, body=None, http=None):
        status, data = (http or self.http).call(method, path, body)
        self.assertEqual(status, 200, (method, path, data))
        return data

    def review(self, body):
        return dict(body, request_key=uuid.uuid4().hex, approved=True, actor_ref='reviewer', reason='관리 화면 검토')

    def track(self, body):
        return dict(body, request_key=uuid.uuid4().hex, actor_ref='reviewer', reason='관리 화면 추적')

    def created_food(self, name):
        candidate = self.ok('POST', '/api/foods/submit', dict(request_key=uuid.uuid4().hex, name=name, aliases=[]))
        return self.ok('POST', '/api/foods/review', self.review(dict(candidate_id=candidate['candidate_id'], expected_revision=candidate['revision'],
                                                                     action='create')))['resolution']

    # Phase B

    def test_every_ui_route_reads_and_writes_the_target_alone(self):
        with self.sqlite_refused() as opened:
            page = self.ok('GET', '/')
            self.assertIn(TOKEN.encode(), page)  # the page carries the token admin.js sends on every POST
            for path in STATIC:
                self.ok('GET', path)
            # RSS discovery: collection, status, list, filters, detail and the audit export
            collected = self.ok('POST', '/api/collect')
            self.assertEqual((collected['success'], collected['entity_resolution']), (True, {'state': 'completed', 'candidates': 3}))
            status = self.ok('GET', '/api/status')
            self.assertEqual((status['latest_run']['success'], status['total_keywords'], status['storage'], status['live_enabled']),
                             (1, 3, self.storage_label(), False))
            self.assertEqual(len(self.ok('GET', '/api/trends?classification=ALL&q=')['items']), 3)
            self.assertEqual(sorted(i['keyword'] for i in self.ok('GET', '/api/trends?classification=FOOD&q=')['items']), sorted([COOKIE[0], BRAND[0]]))
            self.assertEqual([i['id'] for i in self.ok('GET', '/api/trends?classification=ALL&q=' + quote('쫀득'))['items']], [1])
            self.assertIn('raw_xml', self.ok('GET', '/api/trends/1'))
            self.assertEqual(self.http.call('GET', '/api/trends/99')[0], 404)
            self.assertEqual(self.http.call('GET', '/api/trends/abc')[0], 400)
            self.assertEqual(self.http.call('GET', '/api/trends?classification=BAD')[0], 400)
            exported = self.ok('GET', '/api/export')
            self.assertEqual((len(exported['items']), exported['raw_response_retained']), (3, False))
            # Candidate review: create a food from the RSS candidate; exact alias management; rename, relations and merge
            candidates = {c['surface_form']: c for c in self.ok('GET', '/api/food-candidates')['items']}
            self.assertEqual({k: (c['proposed_kind'], c['status']) for k, c in candidates.items()},
                             {COOKIE[0]: ('PRODUCT', 'NEEDS_REVIEW'), BAGEL[0]: ('UNKNOWN', 'NEEDS_REVIEW'), BRAND[0]: ('BRAND', 'NEEDS_REVIEW')})
            cookie = candidates[COOKIE[0]]
            resolved = self.ok('POST', '/api/foods/review', self.review(dict(candidate_id=cookie['candidate_id'], expected_revision=cookie['revision'],
                                                                             action='create', canonical_name='두쫀쿠')))
            food = resolved['resolution']
            self.assertEqual((resolved['status'], resolved['resolution_method']), ('RESOLVED', 'HUMAN_APPROVAL'))
            self.assertEqual(self.ok('GET', '/api/food-candidates/' + cookie['candidate_id'])['resolved_food_id'], food['food_id'])
            self.assertEqual(self.ok('GET', '/api/foods/resolve?name=' + quote(COOKIE[0]))['resolution']['food_id'], food['food_id'])
            alias = self.ok('POST', '/api/foods/alias', self.review(dict(action='approve', food_id=food['food_id'], surface_form='두쫀쿠디저트',
                                                                         expected_revision=self.ok('GET', '/api/foods/' + food['food_id'])['revision'])))
            revoked = self.ok('POST', '/api/foods/alias', self.review(dict(action='revoke', alias_id=alias['alias_id'], expected_revision=alias['revision'])))
            self.assertEqual(revoked['status'], 'REVOKED')
            change = [e for e in self.ok('GET', '/api/food-events?subject_id=' + alias['alias_id'])['items'] if e['action'] == 'CHANGED'][0]
            restored = self.ok('POST', '/api/foods/revert-alias', self.review(dict(event_id=change['event_id'], expected_revision=revoked['revision'])))
            self.assertEqual(restored['status'], 'ACTIVE')
            renamed = self.ok('POST', '/api/foods/rename', self.review(dict(food_id=food['food_id'], canonical_name='두바이 쫀득 쿠키',
                                                                             expected_revision=self.ok('GET', '/api/foods/' + food['food_id'])['revision'])))
            self.assertEqual(renamed['canonical_name'], '두바이 쫀득 쿠키')
            mala, xiangguo, tanghulu = (self.created_food(n) for n in ('마라탕', '마라샹궈', '탕후루'))
            related = self.ok('POST', '/api/foods/relate', self.review(dict(from_food_id=xiangguo['food_id'], to_food_id=mala['food_id'],
                                                                             relation_type='VARIANT_OF', expected_revision=xiangguo['revision'])))
            self.assertEqual((related['relation_type'], related['status']), ('VARIANT_OF', 'ACTIVE'))
            merged = self.ok('POST', '/api/foods/merge', self.review(dict(food_id=tanghulu['food_id'], target_food_id=food['food_id'],
                                                                           expected_revision=tanghulu['revision'],
                                                                           expected_target_revision=self.ok('GET', '/api/foods/' + food['food_id'])['revision'])))
            self.assertEqual((merged['source']['status'], merged['source']['merged_into'], merged['target']['food_id']),
                             ('MERGED', food['food_id'], food['food_id']))
            self.assertEqual(self.ok('GET', '/api/foods/' + tanghulu['food_id'])['status'], 'MERGED')
            bagel = candidates[BAGEL[0]]
            self.assertEqual(self.ok('POST', '/api/foods/import', dict(request_key=uuid.uuid4().hex, source_code='google_trends_rss',
                                                                       source_item_id=2))['candidate_id'], bagel['candidate_id'])
            self.assertEqual(self.ok('POST', '/api/foods/resolve', dict(request_key=uuid.uuid4().hex, candidate_id=bagel['candidate_id'],
                                                                        expected_revision=bagel['revision']))['status'], 'NEEDS_REVIEW')
            rejected = self.ok('POST', '/api/foods/review', self.review(dict(candidate_id=candidates[BRAND[0]]['candidate_id'],
                                                                             expected_revision=candidates[BRAND[0]]['revision'], action='reject')))
            self.assertEqual(rejected['status'], 'REJECTED')
            self.assertEqual(len(self.ok('GET', '/api/foods')['items']), 4)
            # Tracking: register and activate; due list
            accounts = self.ok('GET', '/api/tracking/accounts')
            self.assertEqual({a['source_id'] for a in accounts['items']}, {'instagram_graph', 'youtube', 'naver_datalab'})
            target = self.ok('POST', '/api/tracking/register', self.track(dict(food_id=food['food_id'], source_id='instagram_graph')))
            target = self.ok('POST', '/api/tracking/activate', self.track(dict(target_id=target['target_id'], expected_revision=target['revision'],
                                                                               approved=True)))
            self.assertEqual(target['effective_status'], 'ACTIVE')
            self.assertEqual([t['target_id'] for t in self.ok('GET', '/api/tracking/targets?food_id=' + food['food_id'])['items']], [target['target_id']])
            self.assertIn(target['target_id'], [t['target_id'] for t in self.ok('GET', '/api/tracking/due')['items']])
            # Planning: preview (GET and POST) writes nothing, commit persists; history and the plan itself
            preview = self.ok('GET', '/api/planner/preview?target_id=' + target['target_id'])
            self.assertEqual(({p['source_id'] for p in preview['plans']}, preview['reservations_created']), ({'instagram_graph'}, 0))
            self.ok('POST', '/api/planner/preview', {'target_ids': [target['target_id']]})
            committed = self.ok('POST', '/api/planner/commit', {'request_key': uuid.uuid4().hex, 'target_ids': [target['target_id']]})
            plan_ids = [p['plan_id'] for p in committed['plans']]
            self.assertTrue(plan_ids)
            self.assertTrue(self.ok('GET', '/api/planner/history?target_id=' + target['target_id'])['items'])
            self.assertEqual(self.ok('GET', '/api/planner/plans/' + plan_ids[0])['plan']['plan_id'], plan_ids[0])
            # Execution: schedule (enqueue is idempotent), run one job, job detail, tick, status with per-source state, recover-rss
            jobs = self.ok('POST', '/api/execution/schedule', {'mode': 'mock'})['items']
            self.assertEqual({j['source_id'] for j in jobs}, {'instagram_graph'})
            self.assertEqual(self.ok('POST', '/api/execution/enqueue', {'plan_ids': [jobs[0]['plan_id']], 'mode': 'mock'})['items'][0]['job_id'],
                             jobs[0]['job_id'])
            ran = self.ok('POST', '/api/execution/run', {'job_id': jobs[0]['job_id']})['result']
            self.assertEqual((ran['status'], ran['ingestion']['created']), ('SUCCEEDED', True))
            self.assertEqual(self.ok('GET', '/api/execution/jobs/' + jobs[0]['job_id'])['items'][0]['status'], 'SUCCEEDED')
            ticked = self.ok('POST', '/api/execution/tick', {'mode': 'mock', 'max_jobs': 10})
            self.assertEqual({r['status'] for r in ticked['results']}, {'SUCCEEDED'})
            execution = self.ok('GET', '/api/execution/status')
            self.assertEqual((execution['counts'], execution['available_modes']), ({'SUCCEEDED': len(jobs)}, ['preview', 'mock']))
            self.assertEqual({s['source_id'] for s in execution['sources']}, {'instagram_graph', 'youtube', 'naver_datalab', 'google_trends_rss'})
            self.assertEqual(self.ok('POST', '/api/execution/recover-rss', {})['results'], [])
            # Lifecycle: interval, pause, resume, stop; the history lists each change
            target = self.ok('POST', '/api/tracking/interval', self.track(dict(target_id=target['target_id'],
                                                                               expected_revision=self.ok('GET', '/api/tracking/targets?food_id=' + food['food_id'])['items'][0]['revision'],
                                                                               interval_seconds=7200)))
            self.assertEqual(target['interval_seconds'], 7200)
            target = self.ok('POST', '/api/tracking/pause', self.track(dict(target_id=target['target_id'], expected_revision=target['revision'])))
            self.assertEqual(target['status'], 'PAUSED')
            target = self.ok('POST', '/api/tracking/resume', self.track(dict(target_id=target['target_id'], expected_revision=target['revision'],
                                                                             approved=True)))
            self.assertEqual(target['status'], 'ACTIVE')
            target = self.ok('POST', '/api/tracking/stop', self.track(dict(target_id=target['target_id'], expected_revision=target['revision'])))
            self.assertEqual(target['status'], 'STOPPED')
            self.assertEqual([e['action'] for e in self.ok('GET', '/api/tracking/events?target_id=' + target['target_id'])['items']],
                             ['REGISTERED', 'ACTIVATE', 'INTERVAL', 'PAUSE', 'RESUME', 'STOP'])
        self.assertEqual(opened, [])
        self.assertEqual((self.repo.count('food_entities'), self.repo.count('rss_runs'), self.repo.count('execution_jobs')), (4, 1, len(jobs)))

    def test_state_changes_stay_guarded_and_errors_are_reported(self):
        submit = dict(request_key=uuid.uuid4().hex, name='마라탕', aliases=[])
        before = self.repo.count('food_commands')
        refused = [self.http.call('POST', '/api/foods/submit', submit, token=None),
                   self.http.call('POST', '/api/foods/submit', submit, token='x' * 40),
                   self.http.call('POST', '/api/foods/submit', submit, headers={'Origin': 'http://evil.example'}),
                   self.http.call('POST', '/api/foods/submit', submit, headers={'Host': 'evil.example'}),
                   self.http.call('POST', '/api/collect', token=None),
                   self.http.call('POST', '/api/execution/schedule', {'mode': 'mock'}, token=None)]
        self.assertEqual([s for s, _ in refused], [401, 401, 403, 403, 401, 401])
        self.assertEqual(self.http.call('GET', '/api/foods', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual((self.repo.count('food_commands'), self.repo.count('rss_runs'), self.repo.count('execution_jobs')), (before, 0, 0))
        # Malformed or refused requests get the handlers' codes and write nothing.
        self.assertEqual(self.http.call('POST', '/api/foods/submit', raw=json.dumps(submit).encode(), headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.http.call('POST', '/api/foods/submit', raw=b'{' + b' ' * 17000 + b'}')[0], 413)
        self.assertEqual(self.http.call('POST', '/api/foods/submit', raw=b'{not json')[0], 400)
        self.assertEqual(self.http.call('POST', '/api/foods/submit', dict(submit, unknown=1))[0], 400)
        self.assertEqual(self.http.call('POST', '/api/foods/review', self.review(dict(candidate_id='CAND_missing', expected_revision=1,
                                                                                      action='reject')))[0], 404)
        self.assertEqual(self.http.call('GET', '/api/foods/FOOD_missing')[0], 404)
        self.ok('POST', '/api/collect')
        brand = next(c for c in self.ok('GET', '/api/food-candidates')['items'] if c['surface_form'] == BRAND[0])
        conflict = self.http.call('POST', '/api/foods/review', self.review(dict(candidate_id=brand['candidate_id'], expected_revision=brand['revision'],
                                                                                action='create', entity_kind='PRODUCT')))
        self.assertEqual(conflict[0], 409)  # a brand is never approved as a food
        stale = self.http.call('POST', '/api/foods/review', self.review(dict(candidate_id=brand['candidate_id'], expected_revision=brand['revision'] + 5,
                                                                             action='reject')))
        self.assertEqual(stale[0], 409)
        self.assertEqual(self.http.call('POST', '/api/tracking/register', self.track(dict(food_id='FOOD_missing', source_id='instagram_graph')))[0], 404)
        self.assertEqual(self.http.call('POST', '/api/tracking/launch', self.track({}))[0], 400)
        self.assertEqual(self.http.call('POST', '/api/planner/commit', {'request_key': uuid.uuid4().hex, 'surprise': True})[0], 400)
        self.assertEqual(self.http.call('POST', '/api/execution/run', {})[0], 400)
        self.assertEqual(self.http.call('POST', '/api/execution/drop', {})[0], 400)
        self.assertEqual(self.http.call('POST', '/api/execution/recover-rss', {'limit': 0})[0], 400)
        self.assertEqual(self.http.call('GET', '/api/planner/plans/PLAN_missing')[0], 404)
        self.assertEqual(self.http.call('GET', '/api/execution/jobs/JOB_missing')[0], 404)
        self.assertEqual((self.http.call('GET', '/api/nowhere')[0], self.http.call('POST', '/api/nowhere', {})[0]), (404, 404))
        # The legacy direct collectors are SQLite services and are not attached in the Oracle mode.
        for method, path, body in (('GET', '/api/youtube', None), ('GET', '/api/naver', None), ('GET', '/api/instagram', None),
                                   ('GET', '/api/instagram/media?hashtag_id=1', None), ('POST', '/api/youtube/collect', {}),
                                   ('POST', '/api/naver/collect', {}), ('POST', '/api/instagram/check', {})):
            self.assertEqual(self.http.call(method, path, body)[0], 503, path)
        # Without a Mock response the Oracle mode does not fetch the feed at all.
        _, live = self.serve(fetcher=None)
        runs = self.repo.count('rss_runs')
        status, body = live.call('POST', '/api/collect')
        self.assertEqual((status, body['accepted'], body['error']), (409, False, 'oracle_live_collection_disabled'))
        self.assertEqual(self.repo.count('rss_runs'), runs)

    def test_per_source_status_reports_collection_failure_and_budget(self):
        self.xml = URLError('fixture outage')
        self.assertEqual(self.http.call('POST', '/api/collect')[0], 502)
        self.xml = feed(COOKIE)
        self.ok('POST', '/api/collect')
        backend, http = self.serve(transport=InstagramDown())
        cookie = self.ok('GET', '/api/food-candidates', http=http)['items'][0]
        food = self.ok('POST', '/api/foods/review', self.review(dict(candidate_id=cookie['candidate_id'], expected_revision=cookie['revision'],
                                                                    action='create', canonical_name='두쫀쿠')), http=http)['resolution']
        for source in ('instagram_graph', 'youtube'):
            target = self.ok('POST', '/api/tracking/register', self.track(dict(food_id=food['food_id'], source_id=source)), http=http)
            self.ok('POST', '/api/tracking/activate', self.track(dict(target_id=target['target_id'], expected_revision=target['revision'],
                                                                      approved=True)), http=http)
        self.ok('POST', '/api/execution/tick', {'mode': 'mock', 'max_jobs': 10}, http=http)
        sources = {s['source_id']: s for s in self.ok('GET', '/api/execution/status', http=http)['sources']}
        youtube, instagram, rss = sources['youtube'], sources['instagram_graph'], sources['google_trends_rss']
        self.assertEqual((set(youtube['job_counts']), youtube['last_failure'], youtube['block']), ({'SUCCEEDED'}, None, None))
        self.assertIsNotNone(youtube['last_success_at'])
        self.assertEqual(youtube['budget']['status'], 'READY_LOCAL')
        self.assertTrue(all(line['source_id'] == 'youtube' for line in youtube['budget']['lines']))
        self.assertIsNone(instagram['last_success_at'])
        self.assertEqual(instagram['last_failure']['status'], 'RETRY_WAIT')
        self.assertTrue(instagram['last_failure']['error_kind'])
        self.assertIn('lines', instagram['budget'])
        self.assertEqual((sources['naver_datalab']['job_counts'], sources['naver_datalab']['budget']), ({}, None))
        self.assertEqual(rss['job_counts'], {'SUCCEEDED': 1, 'FAILED': 1})
        self.assertIn('URLError', rss['last_failure']['error'])
        self.assertIsNotNone(rss['last_success_at'])

    def test_the_cli_refuses_production_and_mixed_sqlite_commands(self):
        def run(*argv):
            stderr = io.StringIO()
            with patch('sys.argv', ['food_trends', *argv]), patch.dict(os.environ, {'ORACLE_TEST_DSN': ''}), \
                    contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as stop:
                main()
            return stop.exception.code, stderr.getvalue()
        code, message = run('--backend', 'oracle-production')
        self.assertEqual((code, 'oracle_production_runtime_disabled' in message), (2, True))
        code, message = run('--backend', 'oracle-test', '--collect-once')
        self.assertEqual((code, '--collect-once' in message), (2, True))
        code, message = run('--rss-mock-file', 'feed.xml')
        self.assertEqual(code, 2)
        code, message = run('--backend', 'oracle-test')
        self.assertEqual((code, 'oracle_not_configured' in message), (2, True))
        code, message = run('--backend', 'oracle-test', '--maintenance')
        self.assertEqual((code, 'oracle_not_configured' in message), (2, True))

    def storage_label(self):
        return 'sqlite_target_standin'


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class AdminOracleTests(AdminOracleStandInTests):
    """Every case on the real disposable Oracle test schema; no sqlite3 connection during the Oracle flows."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db

    def storage_label(self):
        return 'oracle_test'


if __name__ == '__main__':
    unittest.main()
