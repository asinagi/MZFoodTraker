"""B8-INTEGRATION-1: the app analytics API on the B6 target schema answers exactly as it does on SQLite.

Each fixture is written into a disposable SQLite database and loaded into the target with B7 migrate(); every
request then goes to AppApi over the SQLite store and over TargetAnalyticsStore, and the answers are compared as
serialized JSON (so 3 and 3.0, or "" and null, differ). Nothing is excluded from the comparison: responses carry
no DBMS-internal identifiers and every run uses a fixed clock. The stand-in classes run in the default suite;
their Oracle subclasses run the same cases on the disposable MZ_TEST schema, which they reset first.
"""
import contextlib
import io
import json
import socket
import tempfile
import unittest
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from food_trends import app_api, app_contract as contract, app_mock, target_schema
from food_trends.analytics_reader import AnalyticsUnavailable, SQLiteAnalyticsStore, TargetAnalyticsStore
from food_trends.analytics_service import B8_SOURCES, FoodAnalytics
from food_trends.app_api import AppApi, StartRefused, open_store
from food_trends.food_service import FoodService
from food_trends.observation_writer import ObservationWriter
from food_trends.oracle_execution import OracleExecution
from food_trends.oracle_ingestion import OracleIngestionService
from food_trends.oracle_observations import OracleObservationService
from food_trends.runtime_ownership import RuntimeOwnership
from food_trends.target_db import OracleSettings, OracleTarget, SQLiteTarget
from food_trends.target_migration import migrate
from food_trends.target_repository import TargetRepository
from food_trends.target_schema import TABLES
from app_api_support import END, HOST, NOW, LiveServer, build_live_fixture, kst
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema
from test_app_contract import DOCS, TARGETS, schema_for

PERIODS = tuple(contract.PERIODS)
# B7 excludes rows whose retention elapsed before the load (EXCLUDED_EXPIRED), and with them B5's EXPIRED evidence
# tombstones that mark EVIDENCE_EXPIRED days. Loading as of a year before the fixture clock copies every row the SQLite
# store holds, so both stores answer from the same logical rows; the gap of a load after expiry is a B7 follow-up.
LOAD_AS_OF = timedelta(days=365)
RUNTIME_TABLES = ('runtime_ownership', 'runtime_participants', 'execution_outbox', 'execution_jobs', 'execution_leases',
                  'quota_reservations', 'quota_charges', 'aggregate_invalidations', 'entity_change_effects')


def url(path, **query):
    query = {k: v for k, v in query.items() if v is not None}
    return path + ('?' + urlencode(query) if query else '')


def exchange(api, target, method='GET', headers=None):
    """(status, body as canonical JSON text, extra headers) of one request."""
    status, body, extra = api.handle(method, target, dict(HOST, **(headers or {})))
    return status, json.dumps(body, ensure_ascii=False, sort_keys=True), sorted(extra.items())


def paged(api, target):
    """The request and every next page the app would ask for (next_cursor added to the same query)."""
    targets = [target]
    while True:
        status, body, _ = api.handle('GET', targets[-1], dict(HOST))
        cursor = (body.get('page') or {}).get('next_cursor') if status == 200 else None
        if not cursor:
            return targets
        targets.append(target + ('&' if '?' in target else '?') + urlencode({'cursor': cursor}))


def food_targets(food_ids, sources, granularities, ends=()):
    for food_id in food_ids:
        base = '/api/v1/foods/' + food_id
        yield base
        for period in PERIODS:
            for source in sources:
                yield url(base + '/metrics', period=period, source=source)
                for granularity in granularities:
                    yield url(base + '/timeline', period=period, granularity=granularity, source=source)
        for end in ends:
            yield url(base + '/timeline', period='30d', end_date=end)
            yield url(base + '/metrics', period='7d', end_date=end)


def list_targets(queries, kinds, limits):
    for kind in kinds:
        for q in (None,) + queries:
            for sort in ('name', '-last_observed_at') + (('relevance',) if q else ()):
                for limit in limits:
                    yield url('/api/v1/foods', q=q, kind=kind, sort=sort, limit=limit)
    for source in B8_SOURCES:
        for period in PERIODS:
            for kind in kinds:
                for include_zero in ('true', 'false'):
                    for limit in limits:
                        yield url('/api/v1/trends/rankings', source=source, period=period, kind=kind, include_zero=include_zero, limit=limit)
    for period in PERIODS:
        yield url('/api/v1/sources/status', period=period)


def target_state(repo):
    """Every row of every target table; on Oracle also each table's latest change SCN and the schema objects' DDL times."""
    state = {t.name: sorted(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) for r in repo.rows(t.name)) for t in TABLES}
    if repo.db.dialect == 'oracle':
        with repo.db.transaction() as tx:
            state['ora_rowscn'] = {t.name: tx.scalar('SELECT max(ORA_ROWSCN) FROM %s' % t.name) for t in TABLES}
            state['user_objects'] = sorted((r['object_name'], r['object_type'], r['status'], str(r['last_ddl_time']))
                                           for r in tx.rows('SELECT object_name, object_type, status, last_ddl_time FROM user_objects'))
    return state


def driver_code(error):
    """The Oracle error code behind a mapped exception (the adapters raise their own codes `from None`)."""
    while error is not None:
        code = getattr(error.args[0], 'full_code', None) if error.args else None
        if code:
            return code
        error = error.__context__
    return None


def build_target_fixture(directory):
    """The contract tests' live fixture plus what only reading the target can get wrong: decisions not reprocessed
    yet (JSON CLOB events, reprocessing_pending), a food created through review, and expired Instagram evidence."""
    storage = build_live_fixture(directory)
    writer = ObservationWriter(storage)
    for n in range(40, 30, -1):  # captions past the 30-day retention leave EVIDENCE_EXPIRED days
        writer.instagram_run(kst(END - timedelta(days=n), 10), 'Q_A', ['FOOD_A'], [{'id': '6%02d00' % n, 'tags': ['두쫀쿠']}], origin='live_api')
    writer.purge(NOW)
    foods = FoodService(storage)
    foods.command('alias', dict(request_key='b8-integration-alias', food_id='FOOD_B', surface_form='마라 탕', action='approve',
                                expected_revision=foods.food('FOOD_B')['revision'], approved=True, actor_ref='reviewer', reason='B8 통합 검증'))
    candidate = foods.command('submit', dict(request_key='b8-integration-submit', name='버터떡', aliases=[]))
    foods.command('review', dict(request_key='b8-integration-review', candidate_id=candidate['candidate_id'], expected_revision=candidate['revision'],
                                 action='create', approved=True, actor_ref='reviewer', reason='B8 통합 검증'))
    return storage


class TargetCase:
    """Mixin: one fixture, loaded once per class into the target (SQLite stand-in here, MZ_TEST in the Oracle subclasses)."""
    clock, origin, data_mode = NOW, 'live_api', 'database'

    @classmethod
    def make_target(cls):
        return SQLiteTarget(Path(cls.temp.name) / 'target.sqlite3')

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.storage = cls.fixture()
        cls.repo = TargetRepository(cls.make_target(), clock=lambda: cls.clock)
        cls.repo.migrate()
        cls.load = migrate(cls.storage.path, cls.repo, now=cls.clock - LOAD_AS_OF)
        cls.sqlite, cls.target = cls.api(SQLiteAnalyticsStore(cls.storage)), cls.api(TargetAnalyticsStore(cls.repo))

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls.repo.db, 'close'):
            cls.repo.db.close()
        cls.temp.cleanup()

    @classmethod
    def api(cls, store):
        return AppApi(FoodAnalytics(store, clock=lambda: cls.clock, origin=cls.origin), data_mode=cls.data_mode)

    def assertSameAnswers(self, targets, method='GET', headers=None):
        for target in targets:
            with self.subTest(method=method, target=target):
                expected, actual = exchange(self.sqlite, target, method, headers), exchange(self.target, target, method, headers)
                if actual != expected:  # a readable structural diff first; equal structures can still differ in JSON types
                    self.assertEqual(json.loads(actual[1]), json.loads(expected[1]))
                self.assertEqual(actual, expected)

    def all_pages(self, targets):
        return [page for target in targets for page in paged(self.sqlite, target)]

    def test_load_excludes_nothing(self):
        self.assertFalse([t for t, v in self.load['tables'].items() if v['dispositions']])

    # The expired evidence the target holds is what the Oracle runtime's own B4/B5 retention keeps: nothing is left to expire.
    def test_runtime_retention_has_nothing_left_to_expire(self):
        execution = OracleExecution(self.repo, {}, lambda: self.clock)
        try:
            counts = dict(OracleIngestionService(execution).purge(self.clock), **OracleObservationService(execution).purge(self.clock))
        finally:
            execution.release()
        self.assertEqual({k: v for k, v in counts.items() if v}, {})


class LiveFixtureOnTarget(TargetCase, unittest.TestCase):
    """The database server's fixture: every operation, parameter family and documented status."""

    @classmethod
    def fixture(cls):
        return build_target_fixture(cls.temp.name)

    def food_ids(self):
        return [f['food_id'] for f in FoodAnalytics(SQLiteAnalyticsStore(self.storage), clock=lambda: NOW).foods()] + ['FOOD_OLD']

    # I3: every operation and status of the contract, same answers, and the target's answers follow the document
    def test_every_operation_and_status_matches_sqlite_and_the_document(self):
        doc, statuses = contract.openapi(), set()
        for method, target, headers in TARGETS:
            self.assertSameAnswers([target], method, headers)
            status, body, _ = self.target.handle(method, target, dict(HOST, **headers))
            statuses.add(status)
            with self.subTest(method=method, target=target):
                self.assertEqual(contract.check(doc, body, schema_for(doc, method, target, status)), [])
                self.assertEqual((body['meta']['data_mode'], body['meta']['observation_origin']), ('database', 'live_api'))
        self.assertEqual(statuses, {200, 400, 403, 404, 405, 422})

    # I4: foods, detail, timeline and metrics for every food, period, source and granularity; rankings and lists page by page
    def test_parameter_matrix_matches_sqlite(self):
        foods = list(food_targets(self.food_ids(), (None,) + contract.SOURCE_CODES, ('day', 'week'), ends=('2026-10-20', '2026-11-03')))
        lists = self.all_pages(list_targets(('쿠키', '두바이', '탕', '마라 탕', ' 두바이  쫀득 ', '없는음식'), (None,) + contract.ENTITY_KINDS, ('2', '100')))
        self.assertGreater(len(lists), len(set(t.split('&cursor=')[0] for t in lists)))  # cursors were followed
        self.assertSameAnswers(foods + lists)

    # The fixture really holds what the comparison claims to cover (checked on the target's own answers).
    def test_target_answers_cover_merges_variants_gaps_expiry_and_pending_changes(self):
        def data(target):
            status, body, _ = self.target.handle('GET', target, dict(HOST))
            self.assertEqual(status, 200, target)
            return body['data']
        old = data('/api/v1/foods/FOOD_OLD')
        self.assertEqual((old['food_id'], old['redirect'], old['merged_food_ids']), ('FOOD_A', {'requested_food_id': 'FOOD_OLD', 'reason': 'MERGED'}, ['FOOD_OLD']))
        self.assertIn('HAS_VARIANT', [r['relation_type'] for r in old['relations']])
        self.assertTrue(data('/api/v1/foods/FOOD_B')['freshness']['reprocessing_pending'])
        self.assertGreaterEqual(data('/api/v1/sources/status')['aggregation']['pending_entity_changes'], 2)
        states = lambda target: {p['value_state'] for s in data(target)['series'] for p in s['points']}
        self.assertIn('EVIDENCE_EXPIRED', states('/api/v1/foods/FOOD_A/timeline?period=90d&source=instagram_graph'))
        self.assertLessEqual({'OBSERVED', 'NOT_OBSERVED'}, states('/api/v1/foods/FOOD_A/timeline?period=30d'))
        legacy = data('/api/v1/foods/FOOD_B/metrics?period=7d&source=instagram_graph')['sources'][0]['legacy_unknown']
        self.assertEqual(legacy['origin'], 'legacy_unknown')
        ranks = [i['rank'] for i in data('/api/v1/trends/rankings?source=google_trends_rss&include_zero=true')['items']]
        self.assertEqual(ranks, sorted(ranks))

    # I2 4.3: the read path writes nothing: no row, DDL, ownership, lease, outbox, reservation or invalidation change
    def test_reads_change_nothing_on_the_target(self):
        targets = [t for _, t, _ in TARGETS] + list(food_targets(['FOOD_A', 'FOOD_OLD'], (None,), ('day', 'week')))
        self.assertLessEqual(set(RUNTIME_TABLES), {t.name for t in TABLES})
        target_state(self.repo)  # a first full read settles any deferred block cleanout before the comparison
        before = target_state(self.repo)
        for target in targets:
            exchange(self.target, target)
        with LiveServer(self.target) as server:
            for method, target, headers in TARGETS:
                server.request(target, method=method, headers=headers)
        self.assertEqual(target_state(self.repo), before)

    # I2 4.3: safe reads continue whichever writer mode owns the target, and take no lease of their own
    def test_reads_continue_under_every_ownership_mode(self):
        owner = RuntimeOwnership(self.repo)
        for mode in ('SHADOW', 'RUNTIME_TEST'):
            lease = owner.acquire(mode)
            try:
                before = {t: self.repo.rows(t) for t in ('runtime_ownership', 'runtime_participants')}
                self.assertSameAnswers(['/api/v1/foods', '/api/v1/foods/FOOD_A/metrics', '/api/v1/trends/rankings?source=instagram_graph',
                                        '/api/v1/sources/status'])
                self.assertEqual({t: self.repo.rows(t) for t in before}, before)
                self.assertEqual(owner.status()['mode'], mode)
            finally:
                owner.release(lease)
        self.assertEqual(owner.status()['mode'], 'IDLE')

    # 15: over HTTP the target-backed server answers the same and opens no connection beyond loopback
    def test_http_server_matches_and_makes_no_outbound_connection(self):
        outbound, connect, resolve = [], socket.socket.connect, socket.getaddrinfo

        def record(address):
            host = address[0] if isinstance(address, tuple) else address
            if host not in ('127.0.0.1', '::1', 'localhost'):
                outbound.append(host)

        def guarded_connect(sock, address):
            record(address)
            return connect(sock, address)

        def guarded_resolve(host, *args, **kwargs):
            record((host,))
            return resolve(host, *args, **kwargs)
        with patch.object(socket.socket, 'connect', guarded_connect), patch.object(socket, 'getaddrinfo', guarded_resolve):
            with LiveServer(self.target) as server:
                for method, target, headers in TARGETS:
                    status, _, body = server.request(target, method=method, headers=headers)
                    direct = self.target.handle(method, target, dict(HOST, **headers))
                    with self.subTest(method=method, target=target):
                        self.assertEqual((status, body), (direct[0], None if method == 'HEAD' else direct[1]))
        self.assertEqual(outbound, [])

    def test_unready_target_schema_is_unavailable(self):
        with patch.object(target_schema, 'TARGET_SCHEMA_VERSION', target_schema.TARGET_SCHEMA_VERSION + 1):
            status, body, _ = self.target.handle('GET', '/api/v1/foods', dict(HOST))
            health = self.target.handle('GET', '/api/v1/health', dict(HOST))[1]['data']
        self.assertEqual((status, body['error']['code'], body['error']['details'][0]['reason']), (503, 'DATA_UNAVAILABLE', 'schema_not_ready'))
        self.assertEqual((health['status'], health['schema_ready']), ('unavailable', False))


class MockDatasetOnTarget(TargetCase, unittest.TestCase):
    """The mock dataset behind the published examples, loaded into the target."""
    clock, origin, data_mode = app_mock.MOCK_NOW, 'mock', 'mock'

    @classmethod
    def fixture(cls):
        return app_mock.cached_dataset(cls.temp.name)

    # I3/I4: the target-backed server reproduces every committed contract example exactly (text, not just values)
    def test_target_reproduces_the_committed_examples(self):
        committed = {f.stem: f.read_text(encoding='utf-8').replace('\r\n', '\n') for f in (DOCS / 'examples').glob('*.json')}
        generated = app_mock.examples(self.target)
        self.assertEqual(sorted(committed), sorted(stem for stem, _ in generated))
        for stem, document in generated:
            with self.subTest(example=stem):
                self.assertEqual(json.dumps(document, ensure_ascii=False, indent=2) + '\n', committed[stem])

    def test_every_food_ranking_and_page_matches_sqlite(self):
        food_ids = [app_mock.food_id(f[0]) for f in app_mock.FOODS]
        self.assertSameAnswers(list(food_targets(food_ids, (None,), ('day', 'week'))) +
                               self.all_pages(list_targets(('쿠키', '두바이', '라떼'), (None, 'DISH'), ('3',))))


class StoreSelection(unittest.TestCase):
    """Server storage is chosen explicitly; production is refused; an unreachable target is 503, never 500."""

    def test_unreachable_target_is_unavailable_not_an_internal_error(self):
        settings = OracleSettings('127.0.0.1:1/NO_SERVICE', 'nobody', 'unused', 'test', call_timeout_ms=3000)
        db = OracleTarget(settings)  # no driver installed: unavailable too
        self.addCleanup(db.close)
        api = AppApi(FoodAnalytics(TargetAnalyticsStore(TargetRepository(db)), clock=lambda: NOW))
        status, body, _ = api.handle('GET', '/api/v1/foods', dict(HOST))
        self.assertEqual((status, body['error']['code'], body['error']['details'][0]['reason']), (503, 'DATA_UNAVAILABLE', 'storage_unavailable'))
        doc = contract.openapi()
        self.assertEqual(contract.check(doc, body, schema_for(doc, 'GET', '/api/v1/foods', 503)), [])
        health = api.handle('GET', '/api/v1/health', dict(HOST))[1]['data']
        self.assertEqual((health['status'], health['schema_ready']), ('unavailable', False))

    def test_cli_selects_storage_explicitly_and_refuses_production(self):
        connects = []
        with patch.object(socket.socket, 'connect', lambda sock, address: connects.append(address)):
            with self.assertRaises(StartRefused) as refused:
                open_store('oracle-production')
            self.assertEqual(refused.exception.reason, 'oracle_production_runtime_disabled')
            for environ, reason in (({}, 'oracle_not_configured'),
                                    ({'ORACLE_TEST_DSN': 'localhost/FREEPDB1', 'ORACLE_TEST_USER': 'u', 'ORACLE_TEST_PASSWORD': 'pw-not-shown',
                                      'ORACLE_TEST_ENVIRONMENT': 'development'}, 'oracle_test_environment_required')):
                with self.assertRaises(StartRefused) as refused:
                    open_store('oracle-test', environ=environ)
                self.assertEqual(refused.exception.reason, reason)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(app_api.main(['--store', 'oracle-production']), 2)
        self.assertEqual(json.loads(output.getvalue()), {'started': False, 'store': 'oracle-production', 'error': 'oracle_production_runtime_disabled'})
        self.assertEqual(connects, [])  # refused before any connection
        for argv in (['--store', 'sqlite'], ['--store', 'oracle-test', '--database', 'x.sqlite3']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app_api.main(argv)


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleLiveFixtureOnTarget(LiveFixtureOnTarget):
    """The live fixture on the real disposable Oracle test schema."""

    @classmethod
    def make_target(cls):
        db = oracle_target()
        reset_target_schema(db)
        return db

    # 4.3: the request's transaction is read-only in the database itself, not by convention
    def test_read_transaction_refuses_writes(self):
        store = TargetAnalyticsStore(self.repo)
        with self.assertRaises(AnalyticsUnavailable) as refused:
            with store.session() as session:
                session._rows("UPDATE food_entities SET updated_at=updated_at WHERE food_id='FOOD_A'", {})
        self.assertEqual((refused.exception.reason, driver_code(refused.exception)), ('storage_unavailable', 'ORA-01456'))

    # 4.2: stored instants are UTC whatever time zone the database session uses (a DST zone and KST)
    def test_answers_do_not_depend_on_the_session_time_zone(self):
        targets = ['/api/v1/foods?sort=-last_observed_at', '/api/v1/foods/FOOD_A', '/api/v1/foods/FOOD_A/timeline?period=90d',
                   '/api/v1/foods/FOOD_B/metrics?period=7d', '/api/v1/sources/status?period=30d']
        for zone in ('America/Los_Angeles', '+09:00'):
            zoned = ZonedTarget(oracle_target(), zone)
            self.addCleanup(zoned.close)
            api = self.api(TargetAnalyticsStore(TargetRepository(zoned)))
            for target in targets:
                with self.subTest(zone=zone, target=target):
                    self.assertEqual(exchange(api, target), exchange(self.sqlite, target))
            with zoned.transaction() as tx:
                self.assertEqual(tx.scalar('SELECT sessiontimezone FROM dual'), zone)

    # The server's own selection path: oracle-test opens MZ_TEST read-only, refuses a schema it does not match
    def test_cli_store_opens_the_test_schema_read_only(self):
        store, close = open_store('oracle-test')
        try:
            self.assertIsInstance(store, TargetAnalyticsStore)
            api = self.api(store)
            self.assertEqual(exchange(api, '/api/v1/health'), exchange(self.sqlite, '/api/v1/health'))
            self.assertEqual(exchange(api, '/api/v1/trends/rankings?source=google_trends_rss'),
                             exchange(self.sqlite, '/api/v1/trends/rankings?source=google_trends_rss'))
        finally:
            close()
        with patch.object(target_schema, 'TARGET_SCHEMA_VERSION', target_schema.TARGET_SCHEMA_VERSION + 1):
            with self.assertRaises(StartRefused) as refused:
                open_store('oracle-test')
        self.assertEqual(refused.exception.reason, 'target_schema_not_ready')


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleMockDatasetOnTarget(MockDatasetOnTarget):
    """The mock dataset and the published examples on the real disposable Oracle test schema."""

    @classmethod
    def make_target(cls):
        db = oracle_target()
        reset_target_schema(db)
        return db


class ZonedTarget:
    """The Oracle adapter with every session switched to another time zone before each read starts."""
    dialect = 'oracle'

    def __init__(self, db, zone):
        self.db, self.zone = db, zone

    @contextmanager
    def transaction(self):
        with self.db.transaction() as tx:
            tx.execute("ALTER SESSION SET TIME_ZONE='%s'" % self.zone)  # not transactional: SET TRANSACTION still comes first
            yield tx

    def close(self):
        self.db.close()


if __name__ == '__main__':
    unittest.main()
