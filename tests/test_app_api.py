"""B8 app API request contract: parameters, pagination, sorting, errors and the HTTP adapter (no network)."""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote
from food_trends import app_api
from food_trends.app_api import make_app_server
from app_api_support import NOW, LiveServer, api_for, build_live_fixture, get


class AppApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.storage = build_live_fixture(cls.temp.name)
        cls.api = api_for(cls.storage)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def ok(self, target, **kw):
        status, body, _ = get(self.api, target, **kw)
        self.assertEqual(status, 200, body)
        return body

    def error(self, target, status, code, reason=None, parameter=None, **kw):
        got, body, extra = get(self.api, target, **kw)
        self.assertEqual((got, body['error']['code']), (status, code), body)
        if reason:
            self.assertEqual(body['error']['details'][0]['reason'], reason)
        if parameter:
            self.assertEqual(body['error']['details'][0]['parameter'], parameter)
        return body, extra


class FoodsEndpoint(AppApiCase):
    # 3, 4: list, search, detail and FOOD_ID handling
    def test_list_search_and_detail(self):
        names = [f['canonical_name'] for f in self.ok('/api/v1/foods')['data']['items']]
        self.assertEqual(names, ['두쫀쿠', '마라탕', '약과', '짭쫀쿠', '탕후루'])  # merged FOOD_OLD is not listed
        found = self.ok('/api/v1/foods?q=' + quote('  쿠키 '))['data']
        self.assertEqual(found['query'], {'q': '쿠키', 'kind': None, 'sort': 'relevance'})
        self.assertEqual([(f['food_id'], f['match']) for f in found['items']],
                         [('FOOD_A', {'field': 'alias', 'text': '두바이 쫀득 쿠키', 'kind': 'contains'})])
        self.assertEqual(self.ok('/api/v1/foods?q=' + quote('두바이쿠키'))['data']['items'][0]['match']['kind'], 'exact')
        empty = self.ok('/api/v1/foods?q=' + quote('없는음식'))
        self.assertEqual((empty['data']['items'], empty['page']['total_items'], empty['page']['next_cursor']), ([], 0, None))
        self.assertEqual([f['food_id'] for f in self.ok('/api/v1/foods?kind=PRODUCT')['data']['items']], ['FOOD_A', 'FOOD_D', 'FOOD_V'])
        detail = self.ok('/api/v1/foods/FOOD_OLD')['data']
        self.assertEqual((detail['food_id'], detail['redirect'], detail['merged_food_ids']),
                         ('FOOD_A', {'requested_food_id': 'FOOD_OLD', 'reason': 'MERGED'}, ['FOOD_OLD']))
        self.assertEqual(detail['relations'][0]['relation_type'], 'HAS_VARIANT')
        self.assertEqual([s['availability'] for s in detail['sources']], ['INCLUDED', 'INCLUDED', 'POLICY_BLOCKED', 'NOT_STORED'])
        self.error('/api/v1/foods/FOOD_NONE', 404, 'FOOD_NOT_FOUND', 'not_found', 'food_id')
        self.error('/api/v1/foods/bad%24id', 400, 'INVALID_PARAMETER', 'invalid_format', 'food_id')

    # 13: sorting and cursor pagination
    def test_sorting_and_cursor_pagination(self):
        recent = [f['food_id'] for f in self.ok('/api/v1/foods?sort=-last_observed_at')['data']['items']]
        self.assertEqual(recent[:2], ['FOOD_B', 'FOOD_A'])  # FOOD_B's 14:00 Instagram run on 11-02 is the latest observation
        self.assertEqual(set(recent[2:]), {'FOOD_C', 'FOOD_D', 'FOOD_V'})  # never observed by live runs: last, by name
        pages, target = [], '/api/v1/foods?limit=2'
        while target:
            body = self.ok(target)
            pages.append(([f['food_id'] for f in body['data']['items']], body['page']['has_more'], body['page']['total_items']))
            target = '/api/v1/foods?limit=2&cursor=' + body['page']['next_cursor'] if body['page']['next_cursor'] else None
        self.assertEqual(pages, [(['FOOD_A', 'FOOD_B'], True, 5), (['FOOD_D', 'FOOD_V'], True, 5), (['FOOD_C'], False, 5)])
        cursor = self.ok('/api/v1/foods?limit=1')['page']['next_cursor']
        self.assertEqual(len(self.ok('/api/v1/foods?limit=3&cursor=' + cursor)['data']['items']), 3)  # limit may change per page
        self.error('/api/v1/foods?kind=DISH&cursor=' + cursor, 400, 'INVALID_CURSOR', 'mismatched_query')
        self.error('/api/v1/foods?cursor=bm90LWpzb24', 400, 'INVALID_CURSOR', 'malformed')
        self.error('/api/v1/foods?sort=relevance', 400, 'INVALID_PARAMETER', 'requires_parameter', 'sort')

    def test_parameter_errors_are_explicit(self):
        cases = (('/api/v1/foods?foo=1', 'unknown_parameter', 'foo'), ('/api/v1/foods?q=a&q=b', 'repeated_parameter', 'q'),
                 ('/api/v1/foods?q=', 'out_of_range', 'q'), ('/api/v1/foods?q=' + 'x' * 101, 'out_of_range', 'q'),
                 ('/api/v1/foods?q=%01', 'invalid_format', 'q'), ('/api/v1/foods?kind=SNACK', 'unsupported_value', 'kind'),
                 ('/api/v1/foods?limit=0', 'out_of_range', 'limit'), ('/api/v1/foods?limit=101', 'out_of_range', 'limit'),
                 ('/api/v1/foods?limit=ten', 'invalid_format', 'limit'),
                 ('/api/v1/foods/FOOD_A/timeline?period=14d', 'unsupported_value', 'period'),
                 ('/api/v1/foods/FOOD_A/timeline?granularity=month', 'unsupported_value', 'granularity'),
                 ('/api/v1/foods/FOOD_A/timeline?end_date=2026-11-04', 'out_of_range', 'end_date'),
                 ('/api/v1/foods/FOOD_A/timeline?end_date=20261102', 'invalid_format', 'end_date'),
                 ('/api/v1/foods/FOOD_A/timeline?end_date=2026-02-30', 'invalid_format', 'end_date'),
                 ('/api/v1/foods/FOOD_A/metrics?source=tiktok', 'unsupported_value', 'source'),
                 ('/api/v1/trends/rankings', 'required', 'source'),
                 ('/api/v1/trends/rankings?source=instagram_graph&metric=score', 'unsupported_value', 'metric'),
                 ('/api/v1/trends/rankings?source=instagram_graph&include_zero=yes', 'unsupported_value', 'include_zero'),
                 ('/api/v1/sources/status?period=1d', 'unsupported_value', 'period'))
        for target, reason, parameter in cases:
            with self.subTest(target=target):
                body, _ = self.error(target, 400, 'INVALID_PARAMETER', reason, parameter)
                self.assertTrue(body['error']['message'])
        allowed = self.error('/api/v1/foods/FOOD_A/timeline?period=14d', 400, 'INVALID_PARAMETER')[0]['error']['details'][0]['allowed']
        self.assertEqual(allowed, ['7d', '30d', '90d'])
        self.error('/api/v1/trends', 404, 'ROUTE_NOT_FOUND', 'unknown_route')
        self.error('/api/v1/foods/', 404, 'ROUTE_NOT_FOUND')


class AnalyticsEndpoints(AppApiCase):
    def test_rankings_metrics_and_live_origin(self):
        rss = self.ok('/api/v1/trends/rankings?source=google_trends_rss')
        self.assertEqual([(e['rank'], e['food']['food_id'], e['current']['value']) for e in rss['data']['items']], [(1, 'FOOD_A', 15), (2, 'FOOD_B', 7)])
        self.assertEqual(rss['data']['excluded'], {'insufficient_coverage': 0, 'not_observed': 0, 'zero_value': 3})  # mock-origin rows ignored
        self.assertEqual(rss['data']['basis']['period'], {'start_date': '2026-10-27', 'end_date': '2026-11-02', 'days': 7, 'timezone': 'Asia/Seoul'})
        zero = self.ok('/api/v1/trends/rankings?source=google_trends_rss&include_zero=true')['data']['items']
        self.assertEqual([(e['rank'], e['current']['value']) for e in zero], [(1, 15), (2, 7), (3, 0), (3, 0), (3, 0)])
        instagram = self.ok('/api/v1/trends/rankings?source=instagram_graph')['data']
        self.assertEqual([(e['food']['food_id'], e['current']['value'], e['change']['reason']) for e in instagram['items']],
                         [('FOOD_A', 77, 'PREVIOUS_PERIOD_INSUFFICIENT'), ('FOOD_B', 7, None)])
        self.assertEqual(instagram['items'][1]['change'], {'state': 'COMPARABLE', 'reason': None, 'absolute': 0, 'ratio': 0.0})
        self.assertEqual(instagram['basis']['observation_scope'], 'PER_FOOD_QUERY')
        for source, reason in (('youtube', 'policy_blocked'), ('naver_datalab', 'not_stored')):
            body, _ = self.error('/api/v1/trends/rankings?source=' + source, 422, 'SOURCE_NOT_RANKABLE', reason, 'source')
            self.assertEqual(body['error']['details'][0]['allowed'], ['google_trends_rss', 'instagram_graph'])
        metrics = self.ok('/api/v1/foods/FOOD_A/metrics')['data']
        rss_metrics = metrics['sources'][0]
        self.assertEqual(rss_metrics['metrics'][0]['current']['value'], 15)
        self.assertEqual({(p['keyword'], p['raw_value']) for p in rss_metrics['provider_values']}, {('두바이쿠키', '2,000+')})
        self.assertEqual(len(rss_metrics['provider_values']), 2)  # only items whose trend keyword is the alias itself
        legacy = self.ok('/api/v1/foods/FOOD_B/metrics?source=instagram_graph')['data']['sources'][0]['legacy_unknown']
        self.assertEqual(legacy, {'value': 1, 'coverage': 'UNKNOWN', 'origin': 'legacy_unknown'})
        status = self.ok('/api/v1/sources/status')['data']
        self.assertEqual(status['sources'][0]['last_run_at'], '2026-11-02T00:00:00Z')  # the later mock-origin run is not a live run

    def test_ranking_cursor_keeps_its_end_date_across_midnight(self):
        clock = {'now': NOW}
        api = api_for(self.storage, clock=lambda: clock['now'])
        first = get(api, '/api/v1/trends/rankings?source=google_trends_rss&limit=1')[1]
        clock['now'] = NOW + timedelta(days=1)
        second = get(api, '/api/v1/trends/rankings?source=google_trends_rss&limit=1&cursor=' + first['page']['next_cursor'])[1]
        self.assertEqual(second['data']['basis']['period'], first['data']['basis']['period'])
        self.assertEqual([e['food']['food_id'] for e in first['data']['items'] + second['data']['items']], ['FOOD_A', 'FOOD_B'])
        fresh = get(api, '/api/v1/trends/rankings?source=google_trends_rss&limit=1')[1]
        self.assertEqual(fresh['data']['basis']['period']['end_date'], '2026-11-03')

    def test_timeline_meta_and_notices(self):
        body = self.ok('/api/v1/foods/FOOD_A/timeline?period=7d&granularity=week')
        self.assertEqual(body['data']['granularity'], 'week')
        self.assertEqual(body['meta'], {'api_version': '1.0.0', 'data_mode': 'database', 'observation_origin': 'live_api',
                                        'generated_at': '2026-11-03T03:00:00Z', 'timezone': 'Asia/Seoul', 'notice': None})
        test_db = get(api_for(self.storage, origin='mock'), '/api/v1/health')[1]['meta']
        self.assertEqual((test_db['observation_origin'], test_db['notice']), ('mock', app_api.TEST_NOTICE))
        mock = get(api_for(self.storage, origin='mock', data_mode='mock'), '/api/v1/health')[1]['meta']
        self.assertEqual((mock['data_mode'], mock['notice']), ('mock', app_api.MOCK_NOTICE))
        self.assertEqual(self.ok('/api/v1/health')['data'], {'status': 'ok', 'api_version': '1.0.0', 'data_mode': 'database',
                                                             'observation_origin': 'live_api', 'schema_ready': True})


class HttpBoundary(AppApiCase):
    def test_methods_hosts_and_admin_token(self):
        body, extra = self.error('/api/v1/foods', 405, 'METHOD_NOT_ALLOWED', 'method_not_allowed', method='POST')
        self.assertEqual(extra, {'Allow': 'GET, HEAD, OPTIONS'})
        self.error('/api/v1/health', 403, 'FORBIDDEN_HOST', 'not_ip_or_localhost', headers={'Host': 'attacker.example'})
        for host in ('localhost:8020', '10.0.2.2:8020', '192.168.0.7', '[::1]:8020'):
            with self.subTest(host=host):
                self.assertEqual(get(self.api, '/api/v1/health', headers={'Host': host})[0], 200)
        body, _ = self.error('/api/v1/health', 400, 'ADMIN_TOKEN_NOT_ACCEPTED', 'forbidden_header', headers={'x-mz-admin-token': 'value'})
        self.assertNotIn('value', json.dumps(body))
        self.assertEqual(get(api_for(self.storage), '/api/v1/health', headers={'X-Mock-Error': '503'})[0], 200)  # mock-only switch
        self.assertEqual(get(api_for(self.storage, origin='mock', data_mode='mock'), '/api/v1/health', headers={'X-Mock-Error': '503'})[0], 503)

    def test_real_http_round_trip_and_cors(self):
        with LiveServer(self.api, cors_origins=('http://localhost:3000',)) as server:
            status, headers, body = server.request('/api/v1/foods?limit=1')
            self.assertEqual((status, headers['Content-Type'], headers['Cache-Control'], headers['X-Content-Type-Options']),
                             (200, 'application/json; charset=utf-8', 'no-store', 'nosniff'))
            self.assertEqual(headers['Server'], 'MZFoodTraker-AppAPI/1')
            self.assertNotIn('Access-Control-Allow-Origin', headers)
            self.assertEqual(body['page']['limit'], 1)
            _, allowed, _ = server.request('/api/v1/health', headers={'Origin': 'http://localhost:3000'})
            self.assertEqual((allowed['Access-Control-Allow-Origin'], allowed['Vary']), ('http://localhost:3000', 'Origin'))
            _, other, _ = server.request('/api/v1/health', headers={'Origin': 'https://example.com'})
            self.assertNotIn('Access-Control-Allow-Origin', other)
            status, preflight, _ = server.request('/api/v1/foods', method='OPTIONS', headers={'Origin': 'http://localhost:3000'})
            self.assertEqual((status, preflight['Access-Control-Allow-Methods']), (204, 'GET, HEAD, OPTIONS'))
            status, head, none = server.request('/api/v1/health', method='HEAD')
            self.assertEqual((status, none), (200, None))
            self.assertGreater(int(head['Content-Length']), 0)
            status, refused, body = server.request('/api/v1/foods', method='DELETE')
            self.assertEqual((status, refused['Allow'], body['error']['code']), (405, 'GET, HEAD, OPTIONS', 'METHOD_NOT_ALLOWED'))
            status, _, body = server.request('/api/v1/foods', method='POST', data=b'{"name": "x"}', headers={'Content-Type': 'application/json'})
            self.assertEqual((status, body['error']['code']), (405, 'METHOD_NOT_ALLOWED'))
            status, _, body = server.request('/api/v1/health', headers={'Host': 'attacker.example'})
            self.assertEqual((status, body['error']['code']), (403, 'FORBIDDEN_HOST'))

    def test_database_server_binds_loopback_only_and_cli_refuses_unready_databases(self):
        with self.assertRaises(ValueError):
            make_app_server(self.api, host='0.0.0.0', port=0)
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / 'empty.sqlite3'
            empty.write_bytes(b'')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(app_api.main(['--database', str(empty)]), 2)
            self.assertFalse(json.loads(output.getvalue())['started'])
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app_api.main(['--database', str(Path(directory) / 'missing.sqlite3')])


if __name__ == '__main__':
    unittest.main()
