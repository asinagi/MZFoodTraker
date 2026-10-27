"""Mock YouTube only. Temporary databases; never loads .env or contacts Google."""
import io
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, urlopen
from food_trends.api import make_server
from food_trends.config import YouTubeConfig
from food_trends.service import CollectionService
from food_trends.storage import Storage, utcnow
from food_trends.youtube import YouTubeClient, YouTubeError, parse_video
from food_trends.youtube_service import YouTubeService
from food_trends.youtube_storage import pacific_day
from test_mvp import fixture

ID1, ID2 = 'abcdefghij1', 'abcdefghij2'


def resource(identity=ID1, title='신상 쿠키'):
    return {'id': identity, 'snippet': {'title': title, 'description': '합성 테스트',
            'publishedAt': '2026-10-25T01:00:00Z', 'channelId': 'UC-test', 'channelTitle': '테스트 채널',
            'thumbnails': {'high': {'url': 'https://i.ytimg.com/vi/'+identity+'/hqdefault.jpg'}}},
            'statistics': {'viewCount': '9007199254740993'}, 'contentDetails': {'duration': 'PT2M3S'},
            'status': {'privacyStatus': 'public', 'uploadStatus': 'processed', 'embeddable': True}}


class Response(io.BytesIO):
    status = 200


class Transport:
    def __init__(self):
        self.calls, self.errors = [], []
        self.pages, self.missing, self.title = False, False, '신상 쿠키'

    def __call__(self, request, timeout):
        self.calls.append(request)
        if self.errors:
            error = self.errors.pop(0)
            if error:
                raise error
        path, params = urlsplit(request.full_url).path, parse_qs(urlsplit(request.full_url).query)
        if path.endswith('/search'):
            identity = ID2 if params.get('pageToken') else ID1
            data = {'items': [{'id': {'videoId': identity}, 'snippet': {}}, {'id': {'videoId': identity}, 'snippet': {}}]}
            if self.pages and not params.get('pageToken'):
                data['nextPageToken'] = 'page-2'
        else:
            data = {'items': [] if self.missing else [resource(identity, self.title) for identity in params['id'][0].split(',')]}
        return Response(json.dumps(data).encode())


def error(status, reason='unknown'):
    return HTTPError('https://example.com/?key=synthetic-secret', status, 'synthetic-secret', {},
                     Response(json.dumps({'error': {'errors': [{'reason': reason}]}}).encode()))


class YouTubeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage.create(Path(self.temp.name)/'youtube.sqlite3')
        self.config = YouTubeConfig(api_key='synthetic-secret', discovery_queries=('신상 디저트',), queries_per_run=3)
        self.sender, self.delays = Transport(), []
        self.client = YouTubeClient(self.config, self.sender, self.delays.append)
        self.service = YouTubeService(self.storage, self.config, self.client)
        self.rss = CollectionService(self.storage, fetcher=lambda _: (fixture('두바이 쫀득 쿠키'), 200))
        self.rss.collect()

    def tearDown(self):
        self.temp.cleanup()

    def test_parse_null_statistics_and_exact_large_count(self):
        parsed = parse_video(resource())
        self.assertEqual(parsed['view_count'], '9007199254740993')
        self.assertIsNone(parsed['like_count'])
        self.assertIsNone(parsed['comment_count'])
        self.assertEqual(parsed['duration'], 'PT2M3S')
        self.assertEqual(parsed['video_url'], 'https://www.youtube.com/watch?v='+ID1)

    def test_bad_shapes_rejected(self):
        for item in ({}, {'id': 'invalid'}, dict(resource(), statistics={'viewCount': -1}),
                     dict(resource(), status={'privacyStatus': 'unexpected'})):
            with self.assertRaises(YouTubeError):
                parse_video(item)

    def test_query_provenance_and_no_new_candidates(self):
        before = self.storage.status()['total_keywords']
        result = self.service.collect()
        self.assertTrue(result['success'])
        status = self.service.status()
        self.assertEqual(status['stored_videos'], 1)
        self.assertEqual(len(status['videos'][0]['matches']), 3)
        self.assertEqual(status['search_requests'], 3)
        self.assertEqual(self.storage.status()['total_keywords'], before)
        self.assertFalse(status['policy']['food_extraction_enabled'])
        self.assertNotIn('synthetic-secret', json.dumps(status))
        self.assertNotIn('synthetic-secret', repr(self.config))
        for request in self.sender.calls:
            self.assertNotIn('synthetic-secret', request.full_url)
            self.assertEqual(request.get_header('X-goog-api-key'), 'synthetic-secret')

    def test_exact_aliases_and_derivatives(self):
        self.rss.fetcher = lambda _: (fixture('짭쫀쿠').replace('짭쫀쿠의 식감 알아보기'.encode(), '새 디저트 짭쫀쿠 레시피'.encode()), 200)
        self.rss.collect()
        queries = self.service.store.sync_queries(self.config)
        groups = {q['query']: q['food_id'] for q in queries if q['mode'] == 'tracking'}
        self.assertEqual(groups['두쫀쿠'], groups['두바이쫀득쿠키'])
        self.assertEqual(groups['두쫀쿠'], groups['두바이 쫀득 쿠키'])
        self.assertNotEqual(groups['두쫀쿠'], groups['짭쫀쿠'])

    def test_one_video_multiple_foods_keeps_relations(self):
        self.rss.fetcher = lambda _: (fixture('피스타치오 라떼'), 200)
        self.rss.collect()
        queries = self.service.store.sync_queries(self.config)
        groups = {}
        for q in queries:
            if q['food_id'] is not None:
                groups.setdefault(q['food_id'], q)
        with self.service.store.operation() as owner:
            for q in groups.values():
                self.service._search(q, owner, 1)
        matches = self.service.status()['videos'][0]['matches']
        self.assertEqual(len({m['food_id'] for m in matches}), 2)
        self.assertTrue(all(m['verification'] == 'search_match_unverified' for m in matches))

    def test_pagination_and_dedup(self):
        self.sender.pages = True
        query = self.service.store.sync_queries(self.config)[0]
        with self.service.store.operation() as owner:
            found = self.service._search(query, owner, 2)
        self.assertEqual(found, {ID1, ID2})
        self.assertEqual(self.service.status()['stored_videos'], 2)
        searches = [parse_qs(urlsplit(r.full_url).query) for r in self.sender.calls if '/search?' in r.full_url]
        self.assertEqual(searches[0]['publishedBefore'], searches[1]['publishedBefore'])
        self.assertEqual(searches[0]['publishedAfter'], searches[1]['publishedAfter'])
        self.assertEqual(searches[1]['pageToken'], ['page-2'])
        self.assertEqual(self.service.status()['search_requests'], 2)

    def test_restart_resumes_token_and_interval(self):
        self.sender.pages = True
        self.service.collect(small=True)
        restarted = YouTubeService(self.storage, self.config, self.client)
        count = len(self.sender.calls)
        self.assertTrue(restarted.collect(automatic=True)['skipped'])
        self.assertEqual(len(self.sender.calls), count)
        restarted.collect(small=True)
        self.assertTrue(any('pageToken=page-2' in r.full_url for r in self.sender.calls[count:]))

    def test_quota_error_persists_and_never_retries(self):
        self.sender.errors = [error(403, 'quotaExceeded')]
        result = self.service.collect()
        self.assertEqual(result['error_type'], 'quota')
        self.assertEqual(len(self.sender.calls), 1)
        restarted = YouTubeService(self.storage, self.config, self.client)
        self.assertEqual(restarted.collect()['error_type'], 'quota')
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(restarted.status()['search_requests'], 1)
        self.assertNotIn('synthetic-secret', json.dumps(result))

    def test_daily_budget_persists_across_service_instances(self):
        config = replace(self.config, daily_search_limit=1)
        limited = YouTubeService(self.storage, config, YouTubeClient(config, self.sender, self.delays.append))
        self.assertEqual(limited.collect()['error_type'], 'budget')
        self.assertEqual(limited.status()['search_requests'], 1)
        self.assertEqual(limited.status()['search_remaining_estimate'], 0)

    def test_transient_retry_charges_each_attempt(self):
        self.sender.errors = [URLError('synthetic-secret'), error(503), None]
        result = self.service.collect(small=True)
        self.assertTrue(result['success'])
        self.assertEqual(self.delays, [1, 2])
        self.assertEqual(self.service.status()['search_requests'], 4)

    def test_exhausted_network_retries(self):
        self.sender.errors = [URLError('synthetic-secret') for _ in range(3)]
        result = self.service.collect()
        self.assertEqual(result['error_type'], 'network')
        self.assertEqual(len(self.sender.calls), 3)
        self.assertNotIn('synthetic-secret', json.dumps(result))

    def test_rate_limit_and_permission_no_immediate_retry(self):
        for code, reason, expected in ((429, 'unknown', 'rate_limit'), (403, 'forbidden', 'permission'), (401, 'unknown', 'authentication')):
            self.service.store.set_state('cooldown_until', '0')
            self.sender.errors = [error(code, reason)]
            count = len(self.sender.calls)
            self.assertEqual(self.service.collect()['error_type'], expected)
            self.assertEqual(len(self.sender.calls), count+1)
            self.assertEqual(self.service.collect()['error_type'], 'cooldown')

    def test_cross_instance_lock(self):
        other = YouTubeService(self.storage, self.config, self.client)
        with self.service.store.operation():
            self.assertEqual(other.collect()['error_type'], 'busy')
        self.assertFalse(self.service.status()['collecting'])

    def test_refresh_modifies_metadata_and_removes_missing(self):
        self.service.collect(small=True)
        self.sender.title = '수정된 제목'
        self.assertTrue(self.service.collect(refresh_only=True)['success'])
        self.assertEqual(self.service.status()['videos'][0]['title'], '수정된 제목')
        self.sender.missing = True
        self.assertTrue(self.service.collect(refresh_only=True)['success'])
        self.assertEqual(self.service.status()['stored_videos'], 0)
        with self.storage.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_matches').fetchone()[0], 0)

    def test_private_video_removed(self):
        self.service.collect(small=True)
        self.service.store.save_videos([ID1], [dict(parse_video(resource()), privacy_status='private')])
        self.assertEqual(self.service.status()['stored_videos'], 0)

    def test_expiry_runs_without_credentials(self):
        self.service.collect(small=True)
        with self.storage.connect() as db:
            db.execute('UPDATE youtube_videos SET expires_at=?', ('2000-01-01',))
        service = YouTubeService(self.storage, YouTubeConfig())
        self.assertEqual(service.status()['stored_videos'], 0)
        self.assertEqual(service.collect()['error_type'], 'not_configured')

    def test_match_retention_separate_from_metadata(self):
        self.service.collect(small=True)
        with self.storage.connect() as db:
            db.execute("UPDATE youtube_matches SET matched_at='2000-01-01'")
        self.service.store.purge()
        self.assertEqual(self.service.status()['stored_videos'], 1)
        self.assertEqual(self.service.status()['videos'][0]['matches'], [])

    def test_full_local_delete_preserves_rss_and_quota(self):
        self.service.collect(small=True)
        count = self.service.status()['search_requests']
        self.assertEqual(self.service.delete_data()['deleted_videos'], 1)
        self.assertEqual(self.service.status()['stored_videos'], 0)
        self.assertEqual(self.service.status()['search_requests'], count)
        self.assertEqual(self.storage.status()['total_keywords'], 1)

    def test_invalid_page_does_not_advance_checkpoint(self):
        query = self.service.store.sync_queries(self.config)[0]
        with self.service.store.operation() as owner:
            with patch.object(self.service, '_request', return_value={'items': [{'id': {'videoId': 'invalid'}}]}):
                with self.assertRaises(YouTubeError):
                    self.service._search(query, owner, 1)
        with self.storage.connect() as db:
            self.assertIsNone(db.execute('SELECT after_at FROM youtube_queries WHERE id=?', (query['id'],)).fetchone()[0])

    def test_invalid_json_and_response(self):
        for raw in (b'not-json', b'[]', b'{"items":null}'):
            client = YouTubeClient(self.config, lambda *a, **k: Response(raw), self.delays.append)
            service = YouTubeService(self.storage, self.config, client)
            service.store.set_state('cooldown_until', '0')
            self.assertEqual(service.collect()['error_type'], 'invalid_response')

    def test_batching_fifty_ids(self):
        identities = [f'{i:011d}' for i in range(101)]
        with self.service.store.operation() as owner:
            self.assertEqual(len(self.service._details(identities, owner)), 101)
        self.assertEqual(len(self.sender.calls), 3)

    def test_query_fairness_and_both_modes(self):
        self.service.collect(small=True)
        self.service.collect(small=True)
        params = [parse_qs(urlsplit(r.full_url).query) for r in self.sender.calls if '/search?' in r.full_url]
        self.assertNotEqual(params[1]['q'], params[3]['q'])

    def test_bad_config(self):
        for kwargs in ({'discovery_queries': ()}, {'discovery_queries': (12,)}, {'interval_seconds': 10},
                       {'daily_search_limit': 101}, {'timeout': float('nan')}):
            with self.assertRaises(ValueError):
                YouTubeConfig(**kwargs)

    def test_existing_env_key_alias(self):
        with patch.dict(os.environ, {'YOUTUBE_API_KEY': '', 'YOUTUBE_DATA_API_V3_KEY': 'synthetic-secret'}):
            self.assertTrue(YouTubeConfig.from_env().configured)
            self.assertNotIn('synthetic-secret', repr(YouTubeConfig.from_env()))

    def test_run_metrics_match_calls_and_existing_upsert(self):
        first = self.service.collect()
        self.assertEqual(first['origin'], 'mock')
        self.assertFalse(first['live_connection_verified'])
        self.assertEqual(first['search_result_rows'], 6)
        self.assertEqual(first['unique_search_videos'], 1)
        self.assertEqual(first['duplicate_search_rows'], 5)
        self.assertEqual(first['new_videos'], 1)
        self.assertEqual(first['updated_existing_videos'], 0)
        self.assertEqual(first['saved_videos'], 1)
        self.assertEqual(first['http_statuses'], {'search': {'200': 3}, 'videos': {'200': 3}})
        second = self.service.collect()
        self.assertEqual(second['new_videos'], 0)
        self.assertEqual(second['updated_existing_videos'], 1)
        status = self.service.status()
        self.assertEqual(status['connection_status'], 'mock_only')
        self.assertEqual(status['latest_run']['run_id'], second['run_id'])
        self.assertEqual(status['videos'][0]['provenance']['origin'], 'mock')
        with self.storage.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_calls WHERE run_id=?', (second['run_id'],)).fetchone()[0], second['requests'])

    def test_empty_refresh_never_claims_http_success(self):
        result = self.service.collect(refresh_only=True)
        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'no_request')
        self.assertEqual(result['requests'], 0)
        self.assertFalse(result['live_connection_verified'])
        self.assertEqual(self.service.status()['status'], 'no_request')

    def test_legacy_http_history_is_not_labeled_live_verified(self):
        with self.storage.connect() as db:
            db.execute('''INSERT INTO youtube_calls(day,endpoint,started_at,status,http_status)
                          VALUES (?,'search',?,'success',200)''', (pacific_day(), utcnow()))
            db.execute('''INSERT INTO youtube_calls(day,endpoint,started_at,status)
                          VALUES (?,'search',?,'network')''', (pacific_day(), utcnow()))
        state = self.service.status()
        self.assertEqual(state['connection_status'], 'legacy_http_success')
        self.assertIsNone(state['last_live_http_success_at'])
        self.assertEqual(state['search_requests'], 2)
        self.assertEqual(sum(c['attempts'] for c in state['daily_call_breakdown'] if c['http_status'] is None), 1)

    def test_verification_minimal_calls_preserve_checkpoint_and_sources(self):
        from food_trends.youtube_verify import verify
        queries = self.service.store.sync_queries(self.config)
        with self.storage.connect() as db:
            before = [tuple(r) for r in db.execute('SELECT id,after_at,before_at,page_token,last_attempt FROM youtube_queries ORDER BY id')]
        result = verify(self.service)
        self.assertEqual(result['requests'], 2)
        self.assertTrue(result['protected_tables_unchanged'])
        self.assertFalse(result['verification_passed'])  # Mock can never pass live verification.
        self.assertEqual(result['data'], 'mock')
        self.assertEqual(result['calls'][0]['result_count'], 2)
        self.assertEqual(result['saved_videos'], 1)
        self.assertEqual(parse_qs(urlsplit(self.sender.calls[0].full_url).query)['maxResults'], ['5'])
        with self.storage.connect() as db:
            after = [tuple(r) for r in db.execute('SELECT id,after_at,before_at,page_token,last_attempt FROM youtube_queries ORDER BY id')]
        self.assertEqual(before, after)

    def test_partial_failure_keeps_correct_saved_count(self):
        self.sender.pages = True
        self.service.config = replace(self.config, max_pages=2)
        self.sender.errors = [None, None, error(403, 'forbidden')]
        result = self.service.collect()
        self.assertFalse(result['success'])
        self.assertEqual(result['saved_videos'], 1)
        self.assertEqual(result['new_videos'], 1)
        self.assertEqual(result['videos_received'], 1)
        self.assertEqual(self.service.status()['stored_videos'], 1)

    def test_failed_response_counts_include_retries_and_no_http(self):
        self.sender.errors = [URLError('secret'), error(503), None]
        result = self.service.collect(verification=True)
        self.assertEqual(result['http_statuses']['search'], {'no_http_response': 1, '503': 1, '200': 1})
        self.assertEqual(result['requests'], 4)

    def test_api_and_csrf_and_no_implicit_requests(self):
        server = make_server(self.rss, port=0, youtube=self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base+'/api/youtube') as response:
                self.assertEqual(json.load(response)['status'], 'unverified')
            self.assertEqual(len(self.sender.calls), 0)
            request = Request(base+'/api/youtube/collect', data=b'{}', headers={'Content-Type': 'application/json'})
            with urlopen(request) as response:
                self.assertTrue(json.load(response)['success'])
            request.add_header('Origin', 'https://other.example')
            with self.assertRaises(HTTPError) as raised:
                urlopen(request)
            self.assertEqual(raised.exception.code, 403)
            with urlopen(base+'/youtube.js') as response:
                self.assertIn(b'refreshYouTubeOverview', response.read())
            with urlopen(base+'/api/trends') as response:
                self.assertEqual(len(json.load(response)['items']), 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_quota_pacific_day(self):
        self.assertRegex(pacific_day(), r'^\d{4}-\d{2}-\d{2}$')


if __name__ == '__main__':
    unittest.main()
