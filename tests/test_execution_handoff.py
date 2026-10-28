"""B3 handoff gaps: result coverage, Instagram usage checks, Live gate and the admin boundary."""
import json
import os
import threading
import unittest
from argparse import Namespace
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from food_trends.api import make_server
from food_trends.config import InstagramConfig
from food_trends.execution import ExecutionService, ExecutionSettings, ExecutionWorker
from food_trends.execution_mock import MockTransport, MockResponse, mock_controller
from food_trends.execution_ownership import transfer
from food_trends.execution_runtime import live_controller, live_readiness
from food_trends.execution_sources import InstagramExecutor, remote_usage_check_needed
from food_trends.instagram_graph import GraphClient
from food_trends.service import CollectionService
from food_trends.tracking_access import Account
from food_trends.tracking_service import stamp
import test_execution as fixtures
from test_food_entities import snapshot

IG_USER = '17841400000000001'
IG_SCOPE = 'ig:' + IG_USER


class Crash(BaseException):
    pass


class HandoffTests(unittest.TestCase):
    setUp = fixtures.ExecutionTests.setUp
    tearDown = fixtures.ExecutionTests.tearDown
    create_food = fixtures.ExecutionTests.create_food
    cmd = fixtures.ExecutionTests.cmd
    register = fixtures.ExecutionTests.register
    active = fixtures.ExecutionTests.active
    job = fixtures.ExecutionTests.job
    advance = fixtures.ExecutionTests.advance

    def youtube_pages(self, pages=2):
        old = self.tracking.account('youtube', 'local-default')
        self.tracking.accounts[('youtube', 'local-default')] = replace(old, options={'max_pages': pages})

    # 4.1 result coverage independent of execution status

    def test_partial_pages_then_repeated_worker_crash_keeps_committed_data_and_lineage(self):
        self.youtube_pages(2)

        class Dies(MockTransport):
            def __call__(self, request, timeout=None):
                if 'pageToken=' in request.full_url:
                    self.calls.append('crash')
                    raise Crash()  # The worker process dies after the request left.
                raw = super().__call__(request, timeout)
                if urlsplit(request.full_url).path.endswith('/search'):
                    data = json.loads(raw.getvalue()); data['nextPageToken'] = 'next'
                    return MockResponse(json.dumps(data).encode())
                return raw
        transport = Dies(); job = self.job()
        controller = mock_controller(self.storage, self.planner, transport)
        for _ in range(3):
            with self.assertRaises(Crash):
                controller.run_once(job['job_id'])
            self.advance()
        self.assertIsNone(controller.run_once(job['job_id']))  # attempt limit finalises without HTTP
        http = len(transport.calls)
        row = self.execution.status(job['job_id'])['items'][0]
        contract = row['run']['result_contract']
        self.assertEqual((row['status'], row['error_kind']), ('FAILED', 'MAX_ATTEMPTS'))
        self.assertEqual((row['run']['result_coverage'], contract['result_coverage'], contract['coverage']),
                         ('PARTIAL', 'PARTIAL', 'partial_request'))
        self.assertEqual(contract['execution_status'], 'FAILED')
        self.assertEqual(contract['committed_steps'], ['search:0', 'videos:0'])
        self.assertEqual(contract['resume_state'], {'phase': 'search', 'page': 1, 'more': True, 'limited': False, 'done': False})
        self.assertTrue(row['remote_duplicate_possible'] and contract['remote_duplicate_possible'])
        self.assertEqual([c['status'] for c in row['calls']].count('UNKNOWN'), 3)
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_videos').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM execution_results').fetchone()[0], 1)
        self.assertIsNone(self.tracking.target(self.target['target_id'])['last_success_at'])
        # Re-delivery of the finished job sends nothing and stores nothing again.
        before = snapshot(self.storage)
        self.assertIsNone(controller.run_once(job['job_id']))
        self.assertEqual((len(transport.calls), snapshot(self.storage)), (http, before))

    def test_failure_before_any_page_reports_no_committed_data(self):
        class Down(MockTransport):
            def __call__(self, request, timeout=None):
                raise URLError('fixture outage')
        job = self.job(); controller = mock_controller(self.storage, self.planner, Down())
        for _ in range(3):
            result = controller.run_once(job['job_id']); self.advance(600)
        contract = result['run']['result_contract']
        self.assertEqual((result['status'], result['run']['result_coverage'], contract['coverage']),
                         ('FAILED', 'NONE', 'no_committed_data'))
        self.assertIsNone(contract['allowed_result_reference'])
        self.assertEqual(contract['active_target_ids'], [])

    def test_limited_and_complete_coverage_are_distinct_from_status(self):
        class Pages(MockTransport):
            def __call__(self, request, timeout=None):
                raw = super().__call__(request, timeout)
                if urlsplit(request.full_url).path.endswith('/search'):
                    data = json.loads(raw.getvalue()); data['nextPageToken'] = 'more'
                    return MockResponse(json.dumps(data).encode())
                return raw
        job = self.job(); limited = mock_controller(self.storage, self.planner, Pages()).run_once(job['job_id'])
        self.assertEqual((limited['status'], limited['run']['result_coverage'], limited['run']['result_contract']['coverage']),
                         ('LIMITED', 'LIMITED', 'limited_request'))
        naver = self.job('naver_datalab'); complete = mock_controller(self.storage, self.planner).run_once(naver['job_id'])
        self.assertEqual((complete['run']['result_coverage'], complete['run']['committed_pages']), ('COMPLETE', 1))

    # 4.2 Instagram provider usage check

    def test_usage_check_decision_rules(self):
        now = self.now
        fresh = {'checked_at': stamp(now), 'verified': True, 'fingerprint': 'f'}
        lookup = {'phase': 'lookup', 'remote_usage': fresh}
        self.assertFalse(remote_usage_check_needed(lookup, 'f', now + timedelta(minutes=14)))
        self.assertTrue(remote_usage_check_needed(lookup, 'f', now + timedelta(minutes=16)))   # expired
        self.assertTrue(remote_usage_check_needed(lookup, 'changed', now))                      # account/version/policy/tag
        self.assertTrue(remote_usage_check_needed({'phase': 'lookup'}, 'f', now))               # never checked
        self.assertTrue(remote_usage_check_needed(dict(lookup, remote_usage=dict(fresh, verified=False)), 'f', now))
        resumed = {'phase': 'media', 'looked_up_at': stamp(now), 'remote_usage': fresh}
        self.assertFalse(remote_usage_check_needed(resumed, 'f', now + timedelta(days=6)))
        self.assertTrue(remote_usage_check_needed(resumed, 'f', now + timedelta(days=7)))       # tag window lapsed
        self.assertTrue(remote_usage_check_needed(resumed, 'changed', now))
        self.assertTrue(remote_usage_check_needed(dict(resumed, looked_up_at=None), 'f', now))

    def live_instagram(self, transport):
        self.tracking.accounts[('instagram_graph', IG_SCOPE)] = Account(
            'instagram_graph', IG_SCOPE, configured=True, permission_verified=True, policy_allowed=True, mode='live')
        self.execution = ExecutionService(self.storage, self.planner,
                                          ExecutionSettings(live_enabled=True, exclusive_live_sources=('instagram_graph',)))
        transfer(self.execution, 'instagram_graph', IG_SCOPE, approved_by='fixture', evidence='isolated test', legacy_stopped=True)
        self.target = self.active_scope('instagram_graph', IG_SCOPE)
        plan = next(p for p in self.planner.commit('ig-live')['plans'] if p['source_id'] == 'instagram_graph')
        job = self.execution.enqueue([plan['plan_id']], mode='live')[0]
        client = GraphClient(InstagramConfig(token='synthetic-fixture-token', user_id=IG_USER), transport)
        client.origin = 'live_api'  # injected transport; still no socket
        return job, {('instagram_graph', IG_SCOPE): InstagramExecutor(client)}

    def active_scope(self, source, scope):
        target = self.register(source, account_scope=scope)
        return self.cmd('activate', target_id=target['target_id'], expected_revision=target['revision'], approved=True)

    @staticmethod
    def usage_calls(transport):
        return sum(p.endswith('recently_searched_hashtags') for p in transport.calls)

    def test_resume_after_lookup_does_not_repeat_usage_check(self):
        transport = MockTransport(); job, adapters = self.live_instagram(transport)
        crash = lambda stage: (_ for _ in ()).throw(Crash()) if stage == 'after_page_commit' and transport.calls[-1].endswith('recent_media') else None
        with self.assertRaises(Crash):
            ExecutionWorker(self.execution, adapters, fault=crash).run_once(job['job_id'])
        self.advance()
        result = ExecutionWorker(self.execution, adapters).run_once(job['job_id'])
        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertEqual(self.usage_calls(transport), 1)
        self.assertEqual(sum(p.endswith('ig_hashtag_search') for p in transport.calls), 1)

    def test_lookup_retry_reuses_fresh_check_and_rechecks_after_expiry(self):
        for wait, expected in ((121, 1), (16 * 60, 2)):
            with self.subTest(wait=wait):
                self.tearDown(); self.setUp()
                transport = MockTransport(); job, adapters = self.live_instagram(transport)
                seen = {'http': 0}
                def crash(stage):
                    if stage == 'before_http':
                        seen['http'] += 1
                        if seen['http'] == 2:  # the lookup, after a verified usage check
                            raise Crash()
                with self.assertRaises(Crash):
                    ExecutionWorker(self.execution, adapters, fault=crash).run_once(job['job_id'])
                self.advance(wait)
                result = ExecutionWorker(self.execution, adapters).run_once(job['job_id'])
                self.assertEqual(result['status'], 'SUCCEEDED')
                self.assertEqual(self.usage_calls(transport), expected)

    def test_failed_usage_check_is_not_cached(self):
        class Broken(MockTransport):
            broken = True
            def __call__(self, request, timeout=None):
                if self.broken and urlsplit(request.full_url).path.endswith('recently_searched_hashtags'):
                    self.broken = False; self.calls.append(urlsplit(request.full_url).path)
                    raise URLError('fixture timeout during usage check')
                return super().__call__(request, timeout)
        transport = Broken(); job, adapters = self.live_instagram(transport)
        worker = ExecutionWorker(self.execution, adapters)
        self.assertEqual(worker.run_once(job['job_id'])['status'], 'RETRY_WAIT')
        self.advance(600)
        self.assertEqual(worker.run_once(job['job_id'])['status'], 'SUCCEEDED')
        self.assertEqual(self.usage_calls(transport), 2)

    # 4.3 Live controller gate

    def registry(self, grants):
        path = os.path.join(self.temp.name, 'registry.json')
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(grants, stream)
        return path

    def test_live_readiness_is_fail_closed_and_read_only(self):
        before = snapshot(self.storage)
        closed = live_readiness(self.storage, None, {})
        self.assertFalse(closed['ready'])
        self.assertEqual(set(closed['blockers']), {'B3_LIVE_DISABLED', 'REGISTRY_MISSING_OR_INVALID', 'NO_EXCLUSIVE_SOURCE'})
        grant = [{'source_id': 'youtube', 'account_scope': 'local-default', 'permission_verified': True,
                  'policy_allowed': True, 'approved_by': 'operator', 'evidence': 'reviewed console quota'}]
        env = {'B3_LIVE_ENABLED': 'true', 'B3_EXCLUSIVE_SOURCES': 'youtube'}
        with patch.dict(os.environ, {'YOUTUBE_API_KEY': 'synthetic-readiness-key', 'YOUTUBE_COLLECTION_ENABLED': 'true'}):
            pending = live_readiness(self.storage, self.registry(grant), env)
            self.assertEqual(set(pending['sources']['youtube']['blockers']),
                             {'LEGACY_SCHEDULER_ENABLED', 'LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED'})
            self.assertNotIn('synthetic-readiness-key', json.dumps(pending))
            self.assertEqual(snapshot(self.storage), before)
            with self.storage.connect() as db:
                db.execute("INSERT INTO execution_source_ownership VALUES ('youtube','local-default','B3','operator','window',?)", (stamp(self.now),))
            env['YOUTUBE_COLLECTION_ENABLED'] = 'false'
            self.assertTrue(live_readiness(self.storage, self.registry(grant), env)['ready'])
            controller = live_controller(self.storage, self.registry(grant), env)
            self.assertEqual(set(controller.worker.adapters), {('youtube', 'local-default')})
            self.assertFalse(controller.execution.settings.automatic)
            self.assertFalse(controller.status()['worker_started'])
            with self.storage.connect() as db:
                db.execute('DELETE FROM schema_migrations WHERE version=(SELECT max(version) FROM schema_migrations)')
            self.assertIn('SCHEMA_VERSION_MISMATCH', live_readiness(self.storage, self.registry(grant), env)['blockers'])

    def test_cli_mode_selection_refuses_unsafe_live_configuration(self):
        from food_trends.__main__ import execution_for_mode
        args = lambda **kw: Namespace(**dict(dict(execution_mode='preview', execution_registry=None, execution_automatic=False), **kw))
        self.assertEqual(execution_for_mode(self.storage, args()), (None, None))
        self.assertIsNotNone(execution_for_mode(self.storage, args(execution_automatic=True))[1])
        self.assertIsNotNone(execution_for_mode(self.storage, args(execution_mode='live'))[1])
        controller, error = execution_for_mode(self.storage, args(execution_mode='live', execution_registry='missing.json'), {})
        self.assertIsNone(controller)
        self.assertIn('B3_LIVE_DISABLED', error)

    # 4.4 admin boundary

    def test_state_changing_requests_need_the_process_admin_token(self):
        controller = mock_controller(self.storage, self.planner)
        collected = []
        rss = CollectionService(self.storage, fetcher=lambda _: collected.append(1))
        server = make_server(rss, port=0, tracking=self.tracking, planner=self.planner, execution=controller)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = 'http://127.0.0.1:%d' % server.server_port
        before = snapshot(self.storage)
        body = {'request_key': 'k', 'name': '쿠키', 'actor_ref': 'owner', 'reason': 'actor_ref is not authentication'}
        try:
            for path in ('/api/foods/submit', '/api/tracking/register', '/api/planner/commit', '/api/execution/schedule',
                         '/api/execution/run', '/api/collect', '/api/instagram/check', '/api/naver/query', '/api/youtube/collect'):
                for token in (None, 'x' * 43):
                    headers = {'Content-Type': 'application/json'}
                    if token:
                        headers['X-MZ-Admin-Token'] = token
                    with self.subTest(path=path, token=bool(token)), self.assertRaises(HTTPError) as error:
                        urlopen(Request(base + path, data=json.dumps(body).encode(), headers=headers))
                    self.assertEqual(error.exception.code, 401)
                    self.assertNotIn('x' * 43, error.exception.read().decode())
            self.assertEqual((snapshot(self.storage), collected), (before, []))
            with urlopen(base + '/') as page:
                self.assertIn(('content="%s"' % server.admin_token).encode(), page.read())
            with self.assertRaises(HTTPError) as rebinding:
                urlopen(Request(base + '/api/status', headers={'Host': 'attacker.example'}))
            self.assertEqual(rebinding.exception.code, 403)
            ok = Request(base + '/api/execution/schedule', data=b'{"mode":"mock"}',
                         headers={'Content-Type': 'application/json', 'X-MZ-Admin-Token': server.admin_token})
            with urlopen(ok) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_server_refuses_non_loopback_bind_and_weak_tokens(self):
        rss = CollectionService(self.storage, fetcher=lambda _: None)
        with self.assertRaises(ValueError):
            make_server(rss, host='0.0.0.0', port=0, tracking=self.tracking, planner=self.planner)
        with self.assertRaises(ValueError):
            make_server(rss, port=0, tracking=self.tracking, planner=self.planner, admin_token='short')
