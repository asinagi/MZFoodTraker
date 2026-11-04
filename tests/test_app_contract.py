"""B8 app API contract: the OpenAPI document, real and mock responses against it, examples against the mock
server, mock/real separation, no database writes and no outbound connections."""
import contextlib
import json
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from food_trends import app_api, app_contract as contract, app_mock
from food_trends.analytics_reader import AnalyticsUnavailable
from food_trends.analytics_service import FoodAnalytics
from food_trends.app_api import AppApi
from food_trends.observation_writer import snapshot
from app_api_support import NOW, LiveServer, api_for, build_live_fixture, get

DOCS = Path(__file__).resolve().parents[1] / 'docs' / 'app-api'
KEYWORDS = {'type', 'format', 'enum', 'properties', 'required', 'items', 'nullable', 'description', 'minimum', 'maximum',
            'minLength', 'maxLength', 'pattern', 'default', '$ref'}
TARGETS = (  # every operation, parameter family and documented status against the live fixture
    ('GET', '/api/v1/health', {}), ('GET', '/api/v1/foods', {}), ('GET', '/api/v1/foods?q=%EC%BF%A0%ED%82%A4', {}),
    ('GET', '/api/v1/foods?sort=-last_observed_at&limit=2', {}), ('GET', '/api/v1/foods?kind=DISH', {}),
    ('GET', '/api/v1/foods/FOOD_A', {}), ('GET', '/api/v1/foods/FOOD_OLD', {}),
    ('GET', '/api/v1/foods/FOOD_A/timeline', {}), ('GET', '/api/v1/foods/FOOD_A/timeline?period=90d&granularity=week', {}),
    ('GET', '/api/v1/foods/FOOD_B/timeline?period=7d&end_date=2026-11-03', {}),
    ('GET', '/api/v1/foods/FOOD_A/metrics', {}), ('GET', '/api/v1/foods/FOOD_B/metrics?period=30d&source=instagram_graph', {}),
    ('GET', '/api/v1/trends/rankings?source=google_trends_rss', {}),
    ('GET', '/api/v1/trends/rankings?source=instagram_graph&period=30d&include_zero=true&limit=1', {}),
    ('GET', '/api/v1/sources/status?period=30d', {}), ('HEAD', '/api/v1/sources/status', {}),
    ('GET', '/api/v1/foods?bogus=1', {}), ('GET', '/api/v1/foods?cursor=x', {}), ('GET', '/api/v1/foods/FOOD_NONE', {}),
    ('GET', '/api/v1/foods/FOOD_A/timeline?period=1y', {}), ('GET', '/api/v1/trends/rankings?source=youtube', {}),
    ('GET', '/api/v1/health', {'X-MZ-Admin-Token': 'x'}), ('GET', '/api/v1/health', {'Host': 'rebind.example'}),
    ('POST', '/api/v1/foods', {}), ('GET', '/api/v1/unknown', {}))


def document_problems(doc):
    """Structural OpenAPI 3.0.3 checks for the subset the contract uses (no third-party validator needed)."""
    problems = []

    def schema(node, where):
        if '$ref' in node:
            if set(node) != {'$ref'}:
                problems.append(where + ': $ref siblings are ignored in 3.0')
            if node['$ref'].rsplit('/', 1)[1] not in doc['components']['schemas']:
                problems.append(where + ': dangling ' + node['$ref'])
            return
        if set(node) - KEYWORDS:
            problems.append('%s: unsupported keywords %s' % (where, sorted(set(node) - KEYWORDS)))
        if node.get('type') not in ('object', 'array', 'string', 'integer', 'number', 'boolean'):
            problems.append('%s: type %r' % (where, node.get('type')))
        if node.get('type') == 'object':
            if set(node.get('required', [])) - set(node.get('properties', {})):
                problems.append(where + ': required names a missing property')
            for name, child in node.get('properties', {}).items():
                schema(child, where + '.' + name)
        if node.get('type') == 'array':
            schema(node['items'], where + '[]')
        if 'enum' in node and node.get('nullable') and None not in node['enum']:
            problems.append(where + ': nullable enum must list null (OpenAPI 3.0.3)')
        if 'default' in node and 'enum' in node and node['default'] not in node['enum']:
            problems.append(where + ': default outside enum')

    def resolve(item, section):
        return doc['components'][section][item['$ref'].rsplit('/', 1)[1]] if '$ref' in item else item

    if doc.get('openapi') != '3.0.3' or not doc['info'].get('title') or not doc['info'].get('version'):
        problems.append('info')
    for name, node in doc['components']['schemas'].items():
        schema(node, name)
    operation_ids = []
    for path, item in doc['paths'].items():
        if set(item) != {'get'}:
            problems.append(path + ': read-only API exposes GET only')
        operation = item['get']
        operation_ids.append(operation['operationId'])
        params = [resolve(p, 'parameters') for p in operation['parameters']]
        if {p['name'] for p in params if p['in'] == 'path'} != set(re.findall(r'\{([a-z_]+)\}', path)):
            problems.append(path + ': path parameters do not match the template')
        if any(p['in'] == 'path' and not p['required'] for p in params) or len({p['name'] for p in params}) != len(params):
            problems.append(path + ': path parameters must be required and names unique')
        for p in params:
            schema(p['schema'], path + ':' + p['name'])
        if '200' not in operation['responses']:
            problems.append(path + ': no 200 response')
        for status, response in operation['responses'].items():
            response = resolve(response, 'responses')
            media = response['content']['application/json']
            if not response.get('description'):
                problems.append(path + status + ': description')
            for name, example in media.get('examples', {}).items():
                errors = contract.check(doc, example['value'], media['schema'])
                if errors:
                    problems.append('%s %s %s: %s' % (path, status, name, errors[:3]))
    if len(operation_ids) != len(set(operation_ids)):
        problems.append('duplicate operationId')
    return problems


def schema_for(doc, method, target, status):
    _, operation = contract.operation_for(doc, target.split('?')[0])
    if operation is None or method not in ('GET', 'HEAD') or str(status) not in operation['responses']:
        return {'$ref': '#/components/schemas/ErrorResponse'}
    return contract.response_schema(doc, operation, status)


class OpenApiDocument(unittest.TestCase):
    # 1: the published files are current, YAML and JSON are the same document, and the document is valid
    def test_published_files_are_current_and_equivalent(self):
        yaml_text, json_text = contract.render(DOCS)
        self.assertEqual((DOCS / 'openapi.yaml').read_text(encoding='utf-8'), yaml_text)
        self.assertEqual((DOCS / 'openapi.json').read_text(encoding='utf-8'), json_text)
        self.assertEqual(contract.load_yaml_subset(yaml_text), json.loads(json_text))

    def test_document_is_structurally_valid(self):
        doc = json.loads((DOCS / 'openapi.json').read_text(encoding='utf-8'))
        self.assertEqual(document_problems(doc), [])
        self.assertEqual(len(doc['paths']), 7)
        self.assertTrue(all(p.startswith(contract.BASE_PATH + '/') for p in doc['paths']))
        self.assertEqual(doc['security'], [])

    def test_yaml_emitter_keeps_tricky_values(self):
        sample = {'multi': 'a\n\n  b', 'yes': 'no', 'on': None, 'n': [1, 2.5, True], 'quote': 'say "hi": #x', 'empty': {}, 'list': [],
                  'tab': 'a\tb', 'breaks': 'x\u2028y', '200': {'k': '-'}, 'trail': 'a \nb', 'unicode': '두쫀쿠'}
        text = contract.to_yaml(sample)
        self.assertEqual(contract.load_yaml_subset(text), sample)
        self.assertNotIn('\u2028', text)
        self.assertIn('"yes": "no"', text)

    # 11: no composite trend score exists in the contract
    def test_contract_has_no_composite_score(self):
        doc = contract.openapi()
        names = set()

        def walk(node):
            if isinstance(node, dict):
                names.update(node.get('properties', {}))
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)
        walk(doc['components']['schemas'])
        self.assertFalse({n for n in names if re.search(r'score|composite|total_trend|overall', n)})
        self.assertEqual(doc['components']['schemas']['RankingBasis']['properties']['scope']['enum'], ['SINGLE_SOURCE'])
        self.assertTrue(doc['components']['parameters']['RankingSource']['required'])
        self.assertEqual(doc['components']['parameters']['Metric']['schema']['enum'], [contract.METRIC_CODE])

    def test_error_codes_and_reasons_are_documented(self):
        errors = (DOCS / 'ERRORS.md').read_text(encoding='utf-8')
        self.assertFalse([code for code in contract.ERRORS if '`%s`' % code not in errors])
        self.assertFalse([reason for reason in contract.DETAIL_REASONS if '`%s`' % reason not in errors])
        covered = {json.loads(f.read_text(encoding='utf-8'))['response'].get('error', {}).get('code') for f in (DOCS / 'examples').glob('error-*.json')}
        self.assertEqual(covered, set(contract.ERRORS))
        guide = (DOCS / 'README.md').read_text(encoding='utf-8')
        self.assertFalse([path for path in contract.OPERATIONS if path not in guide])


class MockServerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.api = app_mock.mock_api(app_mock.cached_dataset(cls.temp.name))
        cls.doc = contract.openapi()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    # 12, P3: committed mock responses are exactly what the mock server returns, and they follow the document
    def test_examples_are_the_mock_server_output(self):
        generated = dict(app_mock.examples(self.api))
        committed = {f.stem: json.loads(f.read_text(encoding='utf-8')) for f in (DOCS / 'examples').glob('*.json')}
        self.assertEqual(sorted(committed), sorted(generated))
        # .gitignore drops credential-shaped names (*token*.json, auth*.json, ...): such an example would never be committed.
        self.assertFalse([s for s in generated if re.search(r'token|credential|secret|cookie|^auth', s)])
        for stem, document in generated.items():
            with self.subTest(example=stem):
                self.assertEqual(committed[stem], document)
                request = document['request']
                schema = schema_for(self.doc, request['method'], request['path'], document['status'])
                self.assertEqual(contract.check(self.doc, document['response'], schema), [])

    def test_mock_data_is_always_marked_as_mock(self):
        for stem, document in app_mock.examples(self.api):
            body = document['response']
            with self.subTest(example=stem):
                self.assertEqual((body['meta']['data_mode'], body['meta']['observation_origin'], body['meta']['notice']),
                                 ('mock', 'mock', app_api.MOCK_NOTICE))
                text = json.dumps(body, ensure_ascii=False)
                self.assertTrue(all(n.startswith('[MOCK] ') for n in re.findall(r'"canonical_name": "([^"]*)"', text)))
                self.assertTrue(all(i.startswith('FOOD_MOCK_') for i in re.findall(r'"food_id": "([^"]*)"', text)))
                self.assertNotIn('"origin": "live_api"', text)

    def test_mock_server_over_http_allows_any_origin(self):
        with LiveServer(self.api, any_origin=True, allow_lan=True) as server:
            status, headers, body = server.request('/api/v1/trends/rankings?source=instagram_graph', headers={'Origin': 'http://localhost:5173'})
            self.assertEqual((status, headers['Access-Control-Allow-Origin']), (200, '*'))
            self.assertEqual(contract.check(self.doc, body, schema_for(self.doc, 'GET', '/api/v1/trends/rankings', 200)), [])
            status, _, body = server.request('/api/v1/foods', headers={'X-Mock-Error': '503'})
            self.assertEqual((status, body['error']['code'], body['error']['details'][0]['reason']), (503, 'DATA_UNAVAILABLE', 'mock_simulated'))


class Down:
    """A store whose schema is not ready (503) or whose reads fail unexpectedly (500)."""

    def __init__(self, error):
        self.error = error

    def ready(self):
        return False

    def session(self):
        raise self.error


class DatabaseServerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.storage = build_live_fixture(cls.temp.name)
        cls.api = api_for(cls.storage)
        cls.doc = contract.openapi()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_targets(self, api):
        return [(method, target, get(api, target, headers, method)) for method, target, headers in TARGETS]

    # 2: every real response, success or error, follows the published document (strict: no undeclared fields)
    def test_real_responses_follow_the_document(self):
        statuses = set()
        for method, target, (status, body, _) in self.run_targets(self.api):
            statuses.add(status)
            with self.subTest(method=method, target=target):
                self.assertEqual(contract.check(self.doc, body, schema_for(self.doc, method, target, status)), [])
        self.assertEqual(statuses, {200, 400, 403, 404, 405, 422})
        for error, code, reason in ((AnalyticsUnavailable('schema_not_ready'), 'DATA_UNAVAILABLE', 'schema_not_ready'),
                                    (RuntimeError('secret path C:/internal'), 'INTERNAL_ERROR', 'internal')):
            api = AppApi(FoodAnalytics(Down(error), clock=lambda: NOW))
            # An unexpected failure is logged server-side; its text never reaches the response.
            with self.assertLogs(level='ERROR') if code == 'INTERNAL_ERROR' else contextlib.nullcontext():
                status, body, _ = get(api, '/api/v1/foods')
            self.assertEqual((status, body['error']['code'], body['error']['details'][0]['reason']), (contract.ERRORS[code][0], code, reason))
            self.assertNotIn('secret path', json.dumps(body, ensure_ascii=False))
            self.assertEqual(contract.check(self.doc, body, schema_for(self.doc, 'GET', '/api/v1/foods', status)), [])
            health = get(api, '/api/v1/health')[1]['data']
            self.assertEqual((health['status'], health['schema_ready']), ('unavailable', False))

    # 12: the database server never presents itself or mock-origin rows as mock data
    def test_database_responses_are_not_mock(self):
        for method, target, (status, body, _) in self.run_targets(self.api):
            meta = body['meta']
            self.assertEqual((meta['data_mode'], meta['observation_origin'], meta['notice']), ('database', 'live_api', None))
            self.assertNotIn('[MOCK]', json.dumps(body, ensure_ascii=False))
        tanghulu = get(self.api, '/api/v1/foods/FOOD_C/timeline?period=7d&source=google_trends_rss')[1]['data']['series'][0]['points']
        self.assertEqual(tanghulu[-1]['value'], 0)  # the mock-origin RSS run that day mentioned it; a live server does not count it

    # 14: reads change nothing (logical dump and file bytes), over direct calls and over HTTP
    def test_requests_change_nothing_in_the_database(self):
        path = Path(self.storage.path)
        before, raw = snapshot(path), path.read_bytes()
        self.run_targets(self.api)
        with LiveServer(self.api) as server:
            for method, target, headers in TARGETS:
                server.request(target, method=method, headers=headers)
        self.assertEqual(snapshot(path), before)
        self.assertEqual(path.read_bytes(), raw)

    # 15: serving every request opens no connection other than the test client's own loopback requests
    def test_requests_make_no_outbound_connections(self):
        outbound, loopback = [], []
        connect, resolve = socket.socket.connect, socket.getaddrinfo

        def record(address):
            host = address[0] if isinstance(address, tuple) else address
            (loopback if host in ('127.0.0.1', '::1', 'localhost') else outbound).append(host)

        def guarded_connect(sock, address):
            record(address)
            return connect(sock, address)

        def guarded_resolve(host, *args, **kwargs):
            record((host,))
            return resolve(host, *args, **kwargs)
        with patch.object(socket.socket, 'connect', guarded_connect), patch.object(socket, 'getaddrinfo', guarded_resolve):
            self.run_targets(self.api)
            with LiveServer(self.api) as server:
                for method, target, headers in TARGETS:
                    server.request(target, method=method, headers=headers)
        self.assertEqual(outbound, [])
        self.assertGreaterEqual(len([h for h in loopback if h == '127.0.0.1']), len(TARGETS))  # the guard saw the client's requests


if __name__ == '__main__':
    unittest.main()
