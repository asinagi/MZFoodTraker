"""Synthetic NAVER responses are confined to tests and temporary databases."""
import io
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from food_trends.config import NaverConfig
from food_trends.naver_datalab import NaverClient, NaverError, request_body, parse_response, ENDPOINT
from food_trends.naver_service import NaverService, keyword_group
from food_trends.storage import Storage
from food_trends.service import CollectionService
from food_trends.api import make_server
from test_mvp import fixture


class Transport:
    def __init__(self):
        self.calls = []
        self.errors = []
        self.empty = False

    def __call__(self, request, timeout):
        self.calls.append(request)
        if self.errors:
            error = self.errors.pop(0)
            if error:
                raise error
        body = json.loads(request.data)
        return {**{k: body[k] for k in ('startDate', 'endDate', 'timeUnit')},
                'results': [{'title': g['groupName'], 'keywords': g['keywords'],
                             'data': [] if self.empty else [
                                 {'period': body['startDate'], 'ratio': 0},
                                 {'period': body['endDate'], 'ratio': 73.125}]} for g in body['keywordGroups']]}


def http_error(code, retry_after='0'):
    return HTTPError(ENDPOINT, code, 'do-not-expose-upstream-secret',
                     {'Retry-After': retry_after}, io.BytesIO(b'private upstream response'))


class NaverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage.create(Path(self.temp.name) / 'test.sqlite3')
        self.config = NaverConfig(client_id='synthetic-id', client_secret='synthetic-secret')
        self.sender, self.delays = Transport(), []
        self.client = NaverClient(self.config, self.sender, self.delays.append)
        self.service = NaverService(self.storage, self.config, self.client)
        self.rss = CollectionService(self.storage, fetcher=lambda _: (fixture('두바이 쫀득 쿠키'), 200))
        self.rss.collect()
        self.ids = [t['candidate_id'] for t in self.service.targets()]

    def tearDown(self):
        self.temp.cleanup()

    def assert_kind(self, kind, call):
        with self.assertRaises(NaverError) as raised:
            call()
        self.assertEqual(raised.exception.kind, kind)
        self.assertNotIn('do-not-expose', str(raised.exception))

    def test_no_keys_no_requests_and_rss_unaffected(self):
        service = NaverService(self.storage, NaverConfig(), self.client)
        self.assert_kind('not_configured', lambda: service.query(self.ids))
        self.assertEqual(service.collect()['error_type'], 'not_configured')
        service.start()
        self.assertFalse(hasattr(service, 'thread'))
        self.assertFalse(self.sender.calls)
        self.assertTrue(self.rss.collect()['success'])

    def test_endpoint_headers_body_and_parse(self):
        result = self.service.query(self.ids)
        request = self.sender.calls[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), 'POST')
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.assertEqual(headers['x-ncp-apigw-api-key-id'], self.config.client_id)
        self.assertEqual(headers['x-ncp-apigw-api-key'], self.config.client_secret)
        self.assertEqual(headers['content-type'], 'application/json')
        body = json.loads(request.data)
        self.assertEqual(body['keywordGroups'][0], keyword_group('두쫀쿠'))
        self.assertEqual(result['results'][0]['candidate_ids'], self.ids)
        self.assertEqual(result['results'][0]['data'][-1]['ratio'], 73.125)
        self.assertEqual(result['source'], 'naver_datalab')
        self.assertEqual(result['metric'], 'relative_search_index')
        self.assertEqual(set(body), {'startDate', 'endDate', 'timeUnit', 'keywordGroups'})

    def test_date_range_and_filters_validation(self):
        with patch('food_trends.naver_datalab.today', return_value=date(2026, 10, 26)):
            for days in (7, 30, 90):
                body = request_body([keyword_group('크루키')], days=days)
                self.assertEqual(body['endDate'], '2026-10-25')
                self.assertEqual((date.fromisoformat(body['endDate'])-date.fromisoformat(body['startDate'])).days, days-1)
            for kwargs in ({'start': '2015-12-31'}, {'end': '2026-10-26'}, {'start': '2026-11-01'},
                           {'start': 'bad'}, {'days': True}, {'filters': {'device': 'bad'}},
                           {'filters': {'ages': ['12']}}, {'filters': {'unknown': 1}}):
                self.assert_kind('validation', lambda: request_body([keyword_group('크루키')], **kwargs))
            self.assertEqual(request_body([keyword_group('크루키')], filters={'gender': 'f'})['gender'], 'f')

    def test_aliases_exact_dedup_and_food_only(self):
        for name in ('두쫀쿠', '두바이쫀득쿠키', ' 두바이 쫀득 쿠키 '):
            self.assertEqual(keyword_group(name)['groupName'], '두쫀쿠')
        self.assertEqual(keyword_group('두바이 초콜릿')['keywords'], ['두바이 초콜릿'])
        self.rss.fetcher = lambda _: (fixture('두바이쫀득쿠키', 'Wed, 14 Oct 2026 10:00:00 +0900'), 200)
        self.rss.collect()
        targets = self.service.targets()
        self.assertEqual(len(targets), 2)
        result = self.service.query([t['candidate_id'] for t in targets])
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(len(result['results'][0]['candidate_ids']), 2)
        self.assert_kind('validation', lambda: self.service.query([999999]))

    def test_explicit_keywords_without_rss_food_and_alias_dedup(self):
        with self.storage.connect() as db:
            db.execute("UPDATE candidates SET classification='NON_FOOD'")
        before = self.storage.status()
        result = self.service.query_keywords(['두쫀쿠', '두바이 쫀득 쿠키'], 30)
        self.assertEqual(result['origin'], 'user_keyword')
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['candidate_ids'], [])
        self.assertEqual(self.storage.status()['food_count'], 0)
        self.assertEqual(self.storage.status()['total_keywords'], before['total_keywords'])
        self.assertEqual(self.service.collect(automatic=True)['requests'], 0)
        self.assert_kind('duplicate', lambda: self.service.query_keywords(['두바이쫀득쿠키'], 30))
        self.assertEqual(len(self.sender.calls), 1)

    def test_explicit_keyword_validation(self):
        for value in (None, [], [''], [True], ['x'*101]):
            self.assert_kind('validation', lambda: self.service.query_keywords(value))

    def add_foods(self, count):
        for n in range(count):
            self.rss.fetcher = lambda _, n=n: (fixture(f'테스트{n} 쿠키'), 200)
            self.rss.collect()

    def test_batching_maximum_five_and_daily_schedule(self):
        self.add_foods(5)
        result = self.service.collect(automatic=True)
        self.assertTrue(result['success'])
        self.assertEqual(len(self.sender.calls), 2)
        self.assertEqual([len(json.loads(r.data)['keywordGroups']) for r in self.sender.calls], [5, 1])
        self.assertFalse(any('results' in r for r in result['batches']))
        self.assertEqual(self.service.collect(automatic=True)['requests'], 0)
        self.assertEqual(len(self.sender.calls), 2)

    def test_group_limits(self):
        self.assert_kind('validation', lambda: request_body([keyword_group(str(i)) for i in range(6)]))
        self.assert_kind('validation', lambda: request_body([{'groupName': 'x', 'keywords': ['x']*6}]))
        self.assert_kind('validation', lambda: request_body([keyword_group('x')]*2))

    def test_scheduler_starts_once_and_disabled_is_manual_only(self):
        disabled = NaverService(self.storage, replace(self.config, enabled=False), self.client)
        disabled.start()
        self.assertFalse(hasattr(disabled, 'thread'))
        self.assertEqual(disabled.collect(automatic=True)['error_type'], 'disabled')
        self.assertTrue(disabled.query(self.ids)['success'])
        self.service.start()
        original = self.service.thread
        try:
            self.service.start()
            self.assertIs(self.service.thread, original)
        finally:
            self.service.stop_event.set()
            original.join(5)
        self.assertFalse(original.is_alive())

    def test_duplicate_persists_across_service_instances(self):
        self.service.query(self.ids)
        other = NaverService(self.storage, self.config, self.client)
        self.assert_kind('duplicate', lambda: other.query(self.ids))
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(other.status()['daily_requests'], 1)

    def test_concurrent_query_is_not_duplicated(self):
        started, release = threading.Event(), threading.Event()
        errors = []
        def sender(request, timeout):
            started.set(); release.wait(5)
            return self.sender(request, timeout)
        self.client.sender = sender
        def query():
            try:
                self.service.query(self.ids)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=query); thread.start()
        try:
            self.assertTrue(started.wait(3))
            self.assert_kind('duplicate', lambda: self.service.query(self.ids))
        finally:
            release.set(); thread.join(5)
        self.assertFalse(errors)
        self.assertEqual(len(self.sender.calls), 1)

    def test_authentication_no_retry(self):
        for code in (401, 403):
            sender = Transport(); sender.errors = [http_error(code)]
            client = NaverClient(self.config, sender, self.delays.append)
            self.assert_kind('authentication', lambda: client.search(request_body([keyword_group('x')])))
            self.assertEqual(len(sender.calls), 1)
        self.assertFalse(self.delays)

    def test_429_backoff_and_global_cooldown(self):
        self.sender.errors = [http_error(429, '2'), None]
        self.assertTrue(self.service.query(self.ids)['success'])
        self.assertEqual(self.delays, [2])
        self.assertEqual(self.service.status()['daily_requests'], 2)
        self.assertEqual(self.service.status()['counts']['rate_limit'], 1)

    def test_retry_after_is_not_shortened(self):
        self.sender.errors = [http_error(429, '120')]
        self.assert_kind('rate_limit', lambda: self.service.query(self.ids))
        self.assertFalse(self.delays)
        self.assert_kind('duplicate', lambda: self.service.query(self.ids, days=7))

    def test_network_and_5xx_bounded_retries(self):
        for error in (URLError('private details'), http_error(503)):
            sender = Transport(); sender.errors = [error]*3
            client = NaverClient(self.config, sender, self.delays.append)
            self.assert_kind('network' if isinstance(error, URLError) and not isinstance(error, HTTPError) else 'upstream',
                             lambda: client.search(request_body([keyword_group('x')])))
            self.assertEqual(len(sender.calls), 3)

    def test_daily_budget_includes_retries_and_survives_restart(self):
        config = replace(self.config, daily_limit=2)
        sender = Transport(); sender.errors = [http_error(503)]*3
        service = NaverService(self.storage, config, NaverClient(config, sender, lambda _: None))
        self.assert_kind('budget', lambda: service.query(self.ids))
        self.assertEqual(len(sender.calls), 2)
        restarted = NaverService(self.storage, config, self.client)
        self.assert_kind('budget', lambda: restarted.query(self.ids, days=7))
        self.assertEqual(restarted.status()['daily_requests'], 2)

    def test_empty_response_and_last_success_retained_after_failure(self):
        self.sender.empty = True
        self.assertEqual(self.service.query(self.ids)['status'], 'no_data')
        before = self.service.targets()[0]['last_success_at']
        self.sender.errors = [http_error(403)]
        self.assert_kind('authentication', lambda: self.service.query(self.ids, days=7))
        self.assertEqual(self.service.targets()[0]['last_success_at'], before)

    def test_invalid_response_does_not_become_chart_data(self):
        body = request_body([keyword_group('x')])
        request = Request(ENDPOINT, data=json.dumps(body).encode())
        payload = self.sender(request, 20)
        for value in (float('nan'), 101, -1, '42', True):
            payload['results'][0]['data'][0]['ratio'] = value
            self.assert_kind('invalid_response', lambda: parse_response(payload, body))
        self.assert_kind('invalid_response', lambda: parse_response({}, body))

    def test_no_response_secrets_or_series_in_database_status_or_export(self):
        self.service.query(self.ids)
        with self.storage.connect() as db:
            dump = '\n'.join(db.iterdump())
        for sensitive in ('synthetic-id', 'synthetic-secret', '73.125', '"ratio"', '"results"'):
            self.assertNotIn(sensitive, dump)
        self.assertNotIn('naver_calls', self.storage.export())
        self.assertNotIn('synthetic-secret', json.dumps(self.service.status()))
        self.assertNotIn('synthetic-secret', repr(self.config))

    def test_api_validation_no_store_errors_and_rss_regression(self):
        server = make_server(self.rss, port=0, naver=self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def post(body, origin=None):
            headers = {'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            return urlopen(Request(base+'/api/naver/query', data=json.dumps(body).encode(), headers=headers))
        try:
            with post({'candidate_ids': self.ids, 'days': 30}) as response:
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(json.load(response)['status'], 'success')
            for body in ({'candidate_ids': self.ids, 'days': 8}, [], {'candidate_ids': [True]}):
                with self.assertRaises(HTTPError) as raised:
                    post(body)
                self.assertEqual(raised.exception.code, 400)
            with self.assertRaises(HTTPError) as raised:
                post({'candidate_ids': self.ids}, 'https://external.invalid')
            self.assertEqual(raised.exception.code, 403)
            for path in ('/', '/naver.js', '/api/status', '/api/trends', '/api/export', '/api/naver'):
                with urlopen(base+path) as response:
                    self.assertEqual(response.status, 200)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
