"""ORACLE-CUTOVER-PREP-1 phase 3: the backend runs on the target alone, with no SQLite application database.

Food registration and exact-alias approval, tracking activation, Oracle planning, due slots and jobs, the mock
collector, B4 staging/DQ, B5 mentions and metrics and the B8 app API all run on one target (the SQLite stand-in
here, the disposable MZ_TEST schema in the Oracle subclass) while every other sqlite3 connection is refused (on
Oracle, every sqlite3 connection). Pause, resume and alias changes reach plans, slots and analytics; replays add
nothing; failed, partial and expired observations are labelled as such; policy-blocked sources stay blocked; the
seven OpenAPI v1 operations keep their contract. The identity and tracking commands also decide exactly as the
SQLite services do for the same inputs.
"""
import json
import re
import sqlite3
from collections import Counter
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlsplit
from food_trends import app_contract as contract
from food_trends.analytics_reader import TargetAnalyticsStore
from food_trends.analytics_service import FoodAnalytics
from food_trends.app_api import AppApi
from food_trends.execution_mock import MockResponse, MockTransport
from food_trends.food_service import FoodError, FoodService
from food_trends.runtime_ownership import OwnershipRefused, RuntimeOwnership
from food_trends.runtime_storage import oracle_backend
from food_trends.storage import Storage
from food_trends.target_db import SQLiteTarget
from food_trends.target_migration import integrity_checks
from food_trends.target_repository import TargetRepository
from food_trends.target_schema import TABLES
from food_trends.tracking_access import mock_accounts
from food_trends.tracking_service import TrackingService
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema, settle_after_ddl

NOW = datetime(2026, 11, 2, 3, tzinfo=timezone.utc)  # 2026-11-02 12:00 KST
KST = timezone(timedelta(hours=9))
HOST = {'Host': '127.0.0.1:8020'}
OPENAPI_PATHS = {'/api/v1/health', '/api/v1/foods', '/api/v1/foods/{food_id}', '/api/v1/foods/{food_id}/timeline',
                 '/api/v1/foods/{food_id}/metrics', '/api/v1/trends/rankings', '/api/v1/sources/status'}
NATURAL_KEYS = {'source_items': ('source_code', 'external_key'), 'run_items': ('ingest_run_id', 'item_key'), 'food_mentions': ('mention_key',),
                'metric_observations': ('observation_key',), 'item_query_matches': ('ingest_run_id', 'item_key', 'query_ref'),
                'quality_results': ('event_key',), 'tracking_slots': ('target_id', 'generation', 'due_slot'),
                'execution_jobs': ('query_id', 'due_slot', 'exec_mode', 'execution_generation'), 'food_aliases': ('alias_id',)}


class Feed(MockTransport):
    """Instagram recent media from a list the test sets per run; other endpoints as MockTransport."""
    def __init__(self):
        super().__init__()
        self.posts = [('900001', '#두쫀쿠 #디저트')]

    def __call__(self, request, timeout=None):
        if urlsplit(request.full_url).path.endswith('recent_media'):
            self.calls.append('recent_media')
            data = [{'id': i, 'timestamp': '2026-10-26T00:00:00Z', 'media_type': 'IMAGE', 'caption': c,
                     'permalink': 'https://www.instagram.com/p/mock%s/' % i} for i, c in self.posts]
            return MockResponse(json.dumps({'data': data}, ensure_ascii=False).encode())
        return super().__call__(request, timeout)


class InstagramDown(Feed):
    def __call__(self, request, timeout=None):
        if urlsplit(request.full_url).path.endswith(('recent_media', 'ig_hashtag_search')):
            self.calls.append('down')
            raise URLError('fixture outage')
        return super().__call__(request, timeout)


class EndlessPages(Feed):
    """Every media page advertises another one: the run stops at the page cap (LIMITED coverage)."""
    def __call__(self, request, timeout=None):
        if urlsplit(request.full_url).path.endswith('recent_media'):
            page = int(parse_qs(urlsplit(request.full_url).query).get('after', ['0'])[0])
            self.calls.append('recent_media')
            body = {'data': [{'id': '95%04d' % page, 'timestamp': '2026-10-26T00:00:00Z', 'media_type': 'IMAGE', 'caption': '#두쫀쿠',
                              'permalink': 'https://www.instagram.com/p/mock95%04d/' % page}],
                    'paging': {'cursors': {'after': str(page + 1)}, 'next': 'https://graph.example/next'}}
            return MockResponse(json.dumps(body, ensure_ascii=False).encode())
        return super().__call__(request, timeout)


class OracleBackendStandInTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.target = self.make_target()
        self.repo = TargetRepository(self.target, clock=lambda: self.now)
        self.repo.migrate()
        self.transport = Feed()
        self.backend = self.assemble(self.transport)

    def make_target(self):
        return SQLiteTarget(Path(self.temp.name) / 'target.sqlite3')

    def assemble(self, transport):
        backend = oracle_backend(self.repo, mock_accounts(), lambda: self.now, transport)
        self.addCleanup(backend.execution.release)
        return backend

    def app(self, origin='mock'):
        return AppApi(FoodAnalytics(TargetAnalyticsStore(self.repo), clock=lambda: self.now, origin=origin), data_mode='database')

    @contextmanager
    def sqlite_refused(self):
        """Every sqlite3 connection fails except the stand-in target file itself (on Oracle: all of them)."""
        allowed = {Path(self.target.path).resolve()} if self.target.dialect == 'sqlite' else set()
        opened, original = [], sqlite3.connect

        def guarded(path, *args, **kwargs):
            text = str(path)
            if text.startswith('file:'):
                text = unquote(urlsplit(text).path).lstrip('/') if re.match(r'^file:/+[A-Za-z]:', text) else unquote(urlsplit(text).path)
            if text == ':memory:' or Path(text).resolve() not in allowed:
                opened.append(Path(text).name)
                raise AssertionError('a SQLite database was opened during the Oracle-only flow')
            return original(path, *args, **kwargs)
        with patch('sqlite3.connect', guarded):
            yield opened

    # Helpers

    def food(self, name, aliases=()):
        candidate = self.backend.foods.command('submit', dict(request_key=uuid.uuid4().hex, name=name, aliases=list(aliases)))
        return self.backend.foods.command('review', dict(request_key=uuid.uuid4().hex, candidate_id=candidate['candidate_id'],
            expected_revision=candidate['revision'], action='create', approved=True, actor_ref='reviewer', reason='음식과 별칭 확인'))['resolution']

    def alias(self, action, **data):
        return self.backend.foods.command('alias', dict(request_key=data.pop('request_key', uuid.uuid4().hex), action=action, approved=True,
                                                        actor_ref='reviewer', reason='별칭 검토', **data))

    def track(self, action, **data):
        return self.backend.tracking.command(action, dict(request_key=data.pop('request_key', uuid.uuid4().hex),
                                                          actor_ref='fixture-reviewer', reason='추적 관리 검토', **data))

    def activate(self, food_id, source):
        target = self.track('register', food_id=food_id, source_id=source)
        return self.track('activate', target_id=target['target_id'], expected_revision=target['revision'], approved=True)

    def run_all(self, controller=None):
        controller, results = controller or self.backend.controller, []
        while True:
            result = controller.run_once()
            if result is None:
                return results
            results.append(result)

    def get(self, target, origin='mock'):
        if not getattr(self, 'settled', False):  # the app API's read-only snapshot cannot read tables defined a moment ago on Oracle
            settle_after_ddl(self.target)
            self.settled = True
        status, body, _ = self.app(origin).handle('GET', target, dict(HOST))
        return status, body

    def day(self, food_id, moment=None, source='instagram_graph', period='7d'):
        day = (moment or self.now).astimezone(KST).date().isoformat()
        status, body = self.get('/api/v1/foods/%s/timeline?period=%s&end_date=%s&source=%s' % (food_id, period, (self.now.astimezone(KST).date()).isoformat(), source))
        self.assertEqual(status, 200, body)
        return {p['start_date']: p for p in body['data']['series'][0]['points']}[day]

    def rows(self, table):
        return self.repo.rows(table)

    def assertNoDuplicates(self):
        for table, key in NATURAL_KEYS.items():
            values = [tuple(r[c] for c in key) for r in self.rows(table)]
            self.assertEqual(len(values), len(set(values)), table)

    # 1-10: one flow, Oracle alone

    def test_the_whole_backend_runs_on_the_target_alone(self):
        with self.sqlite_refused() as opened:
            # 1. food registration and exact alias approval
            food = self.food('두쫀쿠', ['두바이쫀득쿠키'])
            extra = self.alias('approve', food_id=food['food_id'], surface_form='두쫀쿠디저트', expected_revision=food['revision'])
            self.assertEqual(extra['status'], 'ACTIVE')
            self.assertEqual(self.backend.foods.resolve_name('두바이쫀득쿠키')['resolution']['food_id'], food['food_id'])
            # 2. tracking activation (three sources; the policy decides what may be stored)
            targets = {s: self.activate(food['food_id'], s) for s in ('instagram_graph', 'youtube', 'naver_datalab')}
            self.assertEqual({t['effective_status'] for t in targets.values()}, {'ACTIVE'})
            # 3. Oracle planning: the preview writes nothing
            before = {t.name: self.repo.count(t.name) for t in TABLES}
            preview = self.backend.planner.preview()
            self.assertEqual(({p['source_id'] for p in preview['plans']}, preview['reservations_created'], preview['http_requests']),
                             ({'instagram_graph', 'youtube', 'naver_datalab'}, 0, 0))
            self.assertEqual({t.name: self.repo.count(t.name) for t in TABLES}, before)
            # 4. due slots and jobs, committed together: one request per approved alias (Instagram, YouTube), one NAVER group
            jobs = self.backend.execution.schedule()
            self.assertEqual(Counter(j['source_id'] for j in jobs), {'instagram_graph': 3, 'youtube': 3, 'naver_datalab': 1})
            self.assertEqual(sorted((s['status'], s['job_id'] is not None) for s in self.rows('tracking_slots')), [('SCHEDULED', True)] * 3)
            self.assertEqual(len(self.rows('execution_outbox')), 7)
            # 5-7. mock collection, staging/DQ, mentions and metrics (the controller stages and links each terminal job)
            results = self.run_all()
            self.assertEqual([r['status'] for r in results], ['SUCCEEDED'] * 7)
            self.assertFalse([r for r in results if (r.get('ingestion') or {}).get('state') == 'pending_retry'])
            # 8. state on the target
            self.assertEqual({s['status'] for s in self.rows('tracking_slots')}, {'COMPLETED'})
            status = self.backend.execution.status()
            self.assertEqual((status['counts'], status['storage']), ({'SUCCEEDED': 7}, self.storage_label()))
            mentions = self.rows('food_mentions')
            self.assertEqual([(m['food_id'], m['source_code'], m['status']) for m in mentions], [(food['food_id'], 'instagram_graph', 'ACTIVE')])
            metrics = [(m['metric_code'], m['value_state'], m['count_value']) for m in self.rows('metric_observations')]
            self.assertEqual(metrics, [('instagram.observed_posts', 'OBSERVED', 1)] * 3)  # one per Instagram run of the food
            self.assertEqual({r['source_code'] for r in self.rows('ingest_runs')}, {'instagram_graph', 'youtube', 'naver_datalab'})
            self.assertTrue(self.rows('quality_results'))
            # 9. B8 analytics read the target
            status_code, foods = self.get('/api/v1/foods')
            self.assertEqual((status_code, [f['food_id'] for f in foods['data']['items']]), (200, [food['food_id']]))
            point = self.day(food['food_id'])
            self.assertEqual((point['value'], point['value_state'], point['coverage']), (1, 'OBSERVED', 'COMPLETE'))
            # 10. alias change: revoking the matched alias withdraws the mention (the day stays observed: 0, not null);
            # approving an alias another held hashtag matches links the same post again
            alias = next(a for a in self.backend.foods.food(food['food_id'])['approved_aliases'] if a['surface_form'] == '두쫀쿠')
            self.alias('revoke', alias_id=alias['alias_id'], expected_revision=alias['revision'])
            self.assertTrue(self.get('/api/v1/foods/' + food['food_id'])[1]['data']['freshness']['reprocessing_pending'])
            self.backend.observations.apply_entity_changes()
            self.assertEqual((self.day(food['food_id'])['value'], self.day(food['food_id'])['value_state']), (0, 'OBSERVED'))
            self.assertEqual([m['status'] for m in self.rows('food_mentions')], ['INVALIDATED'])
            self.alias('approve', food_id=food['food_id'], surface_form='디저트', expected_revision=self.backend.foods.food(food['food_id'])['revision'])
            self.backend.observations.apply_entity_changes()
            self.assertEqual(self.day(food['food_id'])['value'], 1)
            self.assertFalse(self.get('/api/v1/foods/' + food['food_id'])[1]['data']['freshness']['reprocessing_pending'])
            # 10. the alias changes moved the food revision: its targets wait for an explicit refresh and are not planned
            instagram = self.backend.tracking.target(targets['instagram_graph']['target_id'])
            self.assertEqual((instagram['effective_status'], instagram['blockers']), ('BLOCKED', ['STALE_FOOD_REVISION']))
            # pause and resume: a paused target is not planned; a refreshed, resumed target starts a new generation that runs
            paused = self.track('pause', target_id=instagram['target_id'], expected_revision=instagram['revision'])
            self.now += timedelta(hours=7)
            later = self.backend.execution.schedule()
            self.assertNotIn('instagram_graph', [j['source_id'] for j in later])
            self.run_all()
            refreshed = self.track('refresh', target_id=paused['target_id'], expected_revision=paused['revision'])
            self.assertEqual((refreshed['status'], refreshed['blockers'], refreshed['next_due_at']), ('PAUSED', [], None))
            resumed = self.track('resume', target_id=refreshed['target_id'], expected_revision=refreshed['revision'], approved=True)
            self.assertEqual((resumed['status'], resumed['generation']), ('ACTIVE', refreshed['generation'] + 1))
            jobs = self.backend.execution.schedule()
            self.assertEqual(Counter(j['source_id'] for j in jobs), {'instagram_graph': 3})
            self.run_all()
            slots = [s for s in self.rows('tracking_slots') if s['target_id'] == instagram['target_id']]
            self.assertEqual(sorted((s['generation'], s['status']) for s in slots),
                             [(targets['instagram_graph']['generation'], 'COMPLETED'), (resumed['generation'], 'COMPLETED')])
            history = [e['action'] for e in self.backend.tracking.history(instagram['target_id'])]
            self.assertEqual(history, ['REGISTERED', 'ACTIVATE', 'PAUSE', 'REFRESH', 'RESUME'])
            self.assertEqual(self.day(food['food_id'])['value'], 1)
        self.assertEqual(opened, [])
        checks, _ = integrity_checks(self.repo, self.now)
        self.assertEqual({k: checks[k] for k in ('foreign_key_errors', 'unresolved_payload_refs', 'blocked_policy_rows', 'expired_rows_in_target')},
                         dict(foreign_key_errors=0, unresolved_payload_refs=0, blocked_policy_rows=0, expired_rows_in_target=0))
        self.assertNoDuplicates()

    def storage_label(self):
        return 'sqlite_target_standin'

    def test_replays_and_retries_add_nothing(self):
        with self.sqlite_refused():
            key = uuid.uuid4().hex
            candidate = self.backend.foods.command('submit', dict(request_key=key, name='두쫀쿠', aliases=[]))
            self.assertEqual(self.backend.foods.command('submit', dict(request_key=key, name='두쫀쿠', aliases=[])), candidate)
            with self.assertRaises(FoodError) as clash:
                self.backend.foods.command('submit', dict(request_key=key, name='마라탕', aliases=[]))
            self.assertEqual(clash.exception.status, 409)
            review = dict(request_key=uuid.uuid4().hex, candidate_id=candidate['candidate_id'], expected_revision=candidate['revision'],
                          action='create', approved=True, actor_ref='reviewer', reason='음식 확인')
            food = self.backend.foods.command('review', review)['resolution']
            self.assertEqual(self.backend.foods.command('review', review)['resolution']['food_id'], food['food_id'])
            register = dict(request_key=uuid.uuid4().hex, food_id=food['food_id'], source_id='instagram_graph')
            target = self.track('register', **register)
            self.assertEqual(self.track('register', **register), target)
            self.assertEqual(self.track('register', food_id=food['food_id'], source_id='instagram_graph')['target_id'], target['target_id'])
            self.track('activate', target_id=target['target_id'], expected_revision=target['revision'], approved=True)
            with self.assertRaises(FoodError) as stale:  # a second activation with the old revision is refused
                self.track('activate', target_id=target['target_id'], expected_revision=target['revision'], approved=True)
            self.assertEqual(stale.exception.status, 409)
            first = self.backend.execution.schedule()
            self.assertEqual(self.backend.execution.schedule(), [])
            self.assertEqual(self.backend.execution.enqueue([first[0]['plan_id']])[0]['job_id'], first[0]['job_id'])
            self.run_all()
            self.backend.observations.apply_entity_changes()  # the approvals' first reprocessing
            snapshot = {t.name: self.repo.count(t.name) for t in TABLES if t.name not in ('runtime_ownership', 'runtime_participants', 'planner_commands')}
            self.assertIsNone(self.backend.controller.run_once())
            self.assertEqual(self.backend.ingestion.ingest_pending()['ingested'], 0)
            self.backend.observations.link_pending()
            self.backend.observations.apply_entity_changes()
            self.assertEqual({t: self.repo.count(t) for t in snapshot}, snapshot)
            self.assertEqual(len(self.rows('food_commands')), 2)
            self.assertEqual([c['operation'] for c in sorted(self.rows('tracking_commands'), key=lambda c: c['created_at'])],
                             ['register', 'register', 'activate'])  # the refused activation rolled back with its command row
        self.assertNoDuplicates()

    def test_failure_partial_pages_and_expiry_are_labelled(self):
        with self.sqlite_refused():
            food = self.food('두쫀쿠')
            self.activate(food['food_id'], 'instagram_graph')
            self.backend.execution.schedule()
            self.run_all()
            observed_day = self.now
            # Next day: the provider is down; after the retries the run failed and the day is NOT_OBSERVED (never zero).
            self.now += timedelta(days=1)
            down = self.assemble(InstagramDown())
            down.execution.schedule()
            for _ in range(5):
                self.run_all(down.controller)
                self.now += timedelta(minutes=10)
            failed = self.day(food['food_id'], self.now - timedelta(minutes=50))
            self.assertEqual((failed['value'], failed['value_state'], failed['runs']['failed']), (None, 'NOT_OBSERVED', 1))
            self.assertIn(('instagram.observed_posts', 'NOT_OBSERVED', None),
                          [(m['metric_code'], m['value_state'], m['count_value']) for m in self.rows('metric_observations')])
            # Next day: every page advertises another; the run stops at the page cap and the day is LIMITED, not COMPLETE.
            self.now += timedelta(days=1)
            pages = self.assemble(EndlessPages())
            pages.execution.schedule()
            self.run_all(pages.controller)
            limited = self.day(food['food_id'])
            self.assertEqual((limited['value_state'], limited['coverage']), ('OBSERVED', 'LIMITED'))
            self.assertGreater(limited['value'], 1)
            # 31 days later the caption window has closed: the runtime's own retention expires the evidence.
            self.now = observed_day + timedelta(days=31)
            purged = dict(self.backend.ingestion.purge(self.now), **self.backend.observations.purge(self.now))
            self.assertGreater(purged['purged_captions'], 0)
            self.assertGreater(purged['expired_mentions'], 0)
            expired = self.day(food['food_id'], observed_day, period='90d')
            self.assertEqual((expired['value'], expired['value_state']), (None, 'EVIDENCE_EXPIRED'))
            first = next(c for c in self.rows('source_contents') if c['payload_ref'] == 'instagram_media:900001')
            self.assertNotIn('caption', json.loads(first['content_json']))
            self.assertEqual({m['status'] for m in self.rows('food_mentions') if m['item_key'] == first['item_key']}, {'EXPIRED'})

    def test_policy_blocked_sources_stay_blocked(self):
        with self.sqlite_refused():
            food = self.food('두쫀쿠')
            with self.assertRaises(FoodError) as refused:
                self.track('register', food_id=food['food_id'], source_id='google_trends_rss')
            self.assertEqual(refused.exception.status, 403)
            for source in ('youtube', 'naver_datalab', 'instagram_graph'):
                self.activate(food['food_id'], source)
            self.backend.execution.schedule()
            self.run_all()
            self.assertFalse([r for r in self.rows('source_items') if r['source_code'] == 'naver_datalab'])
            self.assertFalse([r for r in self.rows('source_contents') if r['source_code'] == 'naver_datalab'])
            self.assertFalse([m for m in self.rows('food_mentions') if m['source_code'] != 'instagram_graph'])
            self.assertFalse([m for m in self.rows('metric_observations') if m['source_code'] in ('youtube', 'naver_datalab')])
            self.assertTrue([m for m in self.rows('item_query_matches') if m['verification_status'] == 'SEARCH_ONLY'])
            detail = self.get('/api/v1/foods/' + food['food_id'])[1]['data']
            self.assertEqual({s['source_code']: s['availability'] for s in detail['sources']},
                             {'google_trends_rss': 'INCLUDED', 'instagram_graph': 'INCLUDED', 'youtube': 'POLICY_BLOCKED', 'naver_datalab': 'NOT_STORED'})
            for source in ('youtube', 'naver_datalab'):
                self.assertEqual(self.get('/api/v1/trends/rankings?source=' + source)[0], 422)
            rss = self.backend.planner.preview(options={'include_discovery': True})
            discovery = [p for p in rss['plans'] if p['source_id'] == 'google_trends_rss']
            self.assertEqual(len(discovery), 1)  # RSS discovery stays with the existing SQLite collector: the runtime refuses it
            with self.backend.execution.reader() as t:
                self.assertIn('RSS_OWNED_BY_EXISTING_DISCOVERY_SCHEDULER', self.backend.execution._validate(t, discovery[0], self.now)['reasons'])

    def test_openapi_operations_keep_their_contract_on_the_target(self):
        doc = contract.openapi()
        self.assertEqual(set(doc['paths']), OPENAPI_PATHS)
        with self.sqlite_refused():
            food = self.food('두쫀쿠', ['두바이쫀득쿠키'])
            self.activate(food['food_id'], 'instagram_graph')
            self.backend.execution.schedule()
            self.run_all()
            base = '/api/v1/foods/' + food['food_id']
            requests = ['/api/v1/health', '/api/v1/foods?q=두바이', base, base + '/timeline?period=7d&end_date=2026-11-02', base + '/metrics?period=7d',
                        '/api/v1/trends/rankings?source=instagram_graph&end_date=2026-11-02', '/api/v1/sources/status', '/api/v1/foods/FOOD_MISSING']
            for target in requests:
                status, body = self.get(target)
                with self.subTest(target=target):
                    self.assertEqual(contract.check(doc, body, contract.response_schema(doc, contract.operation_for(doc, urlsplit(target).path)[1], status)), [])
                    self.assertEqual(body['meta']['data_mode'], 'database')

    def test_writes_go_only_to_the_selected_store(self):
        owner = RuntimeOwnership(self.repo)
        with self.sqlite_refused():
            food = self.food('두쫀쿠')
            self.activate(food['food_id'], 'instagram_graph')
            self.assertEqual(owner.status()['mode'], 'RUNTIME_TEST')
            with self.assertRaises(OwnershipRefused) as refused:  # Shadow (and B7) cannot write while the runtime owns the target
                owner.acquire('SHADOW')
            self.assertEqual(refused.exception.code, 'ownership_held_by_RUNTIME_TEST')
        self.backend.execution.release()
        self.assertEqual(owner.status()['mode'], 'IDLE')
        # Shadow owns the target: identity and tracking commands are refused and write nothing.
        before = {t: self.repo.count(t) for t in ('food_commands', 'food_entities', 'tracking_commands', 'tracking_targets')}
        with owner.held('SHADOW'):
            backend = oracle_backend(self.repo, mock_accounts(), lambda: self.now, Feed())
            with self.assertRaises(OwnershipRefused):
                backend.foods.command('submit', dict(request_key=uuid.uuid4().hex, name='마라탕', aliases=[]))
            with self.assertRaises(OwnershipRefused):
                backend.tracking.command('register', dict(request_key=uuid.uuid4().hex, food_id=food['food_id'], source_id='youtube',
                                                          actor_ref='reviewer', reason='추적'))
        self.assertEqual({t: self.repo.count(t) for t in before}, before)
        # The SQLite services, for their part, never open the target or an Oracle driver connection.
        storage = Storage.create(Path(self.temp.name) / 'app.sqlite3')
        with patch('food_trends.target_db.OracleTarget.transaction', side_effect=AssertionError('Oracle opened')), \
                patch('food_trends.target_db.SQLiteTarget.transaction', side_effect=AssertionError('target opened')):
            foods = FoodService(storage)
            c = foods.command('submit', dict(request_key=uuid.uuid4().hex, name='마라탕', aliases=[]))
            foods.command('review', dict(request_key=uuid.uuid4().hex, candidate_id=c['candidate_id'], expected_revision=c['revision'],
                                         action='create', approved=True, actor_ref='reviewer', reason='음식 확인'))
        self.assertEqual(self.repo.count('food_entities'), before['food_entities'])


class IdentityParityStandInTests(unittest.TestCase):
    """The same identity and tracking script on the SQLite services and on the target services: same decisions."""
    make_target = OracleBackendStandInTests.make_target

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.repo = TargetRepository(self.make_target(), clock=lambda: self.now)
        self.repo.migrate()
        backend = oracle_backend(self.repo, mock_accounts(), lambda: self.now, MockTransport())
        self.addCleanup(backend.execution.release)
        storage = Storage.create(Path(self.temp.name) / 'app.sqlite3')
        self.sides = {'sqlite': (FoodService(storage), TrackingService(storage, mock_accounts(), lambda: self.now)),
                      'target': (backend.foods, backend.tracking)}

    @staticmethod
    def normalize(value, names):
        """IDs become placeholders in order of first appearance; timestamps, row sequences and the RSS item reference drop out."""
        if isinstance(value, dict):
            return {k: IdentityParityStandInTests.normalize(json.loads(value[k]) if k.endswith('_json') and isinstance(value[k], str) else value[k], names)
                    for k in sorted(value) if not k.endswith('_at') and k not in ('event_sequence', 'source_item_id', 'source_item_key')}
        if isinstance(value, (list, tuple)):
            return [IdentityParityStandInTests.normalize(v, names) for v in value]
        if isinstance(value, str) and re.fullmatch(r'((FOOD|ALIAS|CAND|EVT|REL|TARGET|TEVT|SLOT)_)?[0-9a-f]{32}', value):
            return names.setdefault(value, '%s#%d' % (value.split('_')[0] if '_' in value else 'KEY', len(names)))
        return value

    def script(self, foods, tracking):
        """Every command family with its refusals; returns each outcome (result or error status)."""
        out, ids = [], {}

        def do(service, operation, body, keep=None):
            try:
                result = service.command(operation, dict(body, request_key=body.get('request_key') or uuid.uuid4().hex))
            except FoodError as exc:
                out.append(('error', operation, exc.status))
                return None
            out.append(('ok', operation, result))
            if keep:
                ids[keep] = result
            return result
        review = dict(approved=True, actor_ref='reviewer', reason='검토')
        a = do(foods, 'submit', dict(name='두쫀쿠', aliases=['두바이쫀득쿠키']))
        food = do(foods, 'review', dict(review, candidate_id=a['candidate_id'], expected_revision=a['revision'], action='create'))['resolution']
        b = do(foods, 'submit', dict(name='마라탕', aliases=[]))
        other = do(foods, 'review', dict(review, candidate_id=b['candidate_id'], expected_revision=b['revision'], action='create'))['resolution']
        brand = do(foods, 'submit', dict(name='어느브랜드', aliases=[], entity_kind='BRAND'))
        do(foods, 'review', dict(review, candidate_id=brand['candidate_id'], expected_revision=brand['revision'], action='create'))
        do(foods, 'review', dict(review, candidate_id=brand['candidate_id'], expected_revision=brand['revision'], action='reject'))
        do(foods, 'resolve', dict(candidate_id=a['candidate_id'], expected_revision=a['revision']))
        current = lambda food_id: foods.food(food_id)
        alias = do(foods, 'alias', dict(review, action='approve', food_id=food['food_id'], surface_form='두쫀', expected_revision=current(food['food_id'])['revision']))
        do(foods, 'alias', dict(review, action='approve', food_id=other['food_id'], surface_form='두쫀', expected_revision=current(other['food_id'])['revision']))
        revoked = do(foods, 'alias', dict(review, action='revoke', alias_id=alias['alias_id'], expected_revision=alias['revision']))
        event = foods.events(alias['alias_id'])[-1]
        do(foods, 'revert-alias', dict(review, event_id=event['event_id'], expected_revision=revoked['revision']))
        do(foods, 'relate', dict(review, from_food_id=other['food_id'], to_food_id=food['food_id'], relation_type='VARIANT_OF',
                                 expected_revision=current(other['food_id'])['revision']))
        do(foods, 'relate', dict(review, from_food_id=food['food_id'], to_food_id=other['food_id'], relation_type='VARIANT_OF',
                                 expected_revision=current(food['food_id'])['revision']))
        do(foods, 'rename', dict(review, food_id=food['food_id'], canonical_name='두바이쫀득쿠키 원조', expected_revision=current(food['food_id'])['revision']))
        c = do(foods, 'submit', dict(name='두쫀쿠 미니', aliases=['미니두쫀']))
        mini = do(foods, 'review', dict(review, candidate_id=c['candidate_id'], expected_revision=c['revision'], action='create'))['resolution']
        do(foods, 'merge', dict(review, food_id=mini['food_id'], target_food_id=food['food_id'], expected_revision=current(mini['food_id'])['revision'],
                                expected_target_revision=current(food['food_id'])['revision']))
        do(foods, 'merge', dict(review, food_id=other['food_id'], target_food_id=food['food_id'], expected_revision=current(other['food_id'])['revision'],
                                expected_target_revision=current(food['food_id'])['revision']))
        out.append(('ok', 'resolve_name', foods.resolve_name('미니두쫀')))
        # Creation times can tie at the wall clock's resolution (the services then order by random ids): compare by name.
        out.append(('ok', 'foods', sorted(foods.foods(), key=lambda f: f['canonical_name'])))
        out.append(('ok', 'candidates', sorted(foods.candidates(), key=lambda c: c['surface_form'])))
        t = dict(actor_ref='fixture-reviewer', reason='추적 검토')
        do(tracking, 'register', dict(t, food_id=food['food_id'], source_id='google_trends_rss'))
        do(tracking, 'register', dict(t, food_id=mini['food_id'], source_id='instagram_graph'))
        target = do(tracking, 'register', dict(t, food_id=food['food_id'], source_id='instagram_graph'))
        key = uuid.uuid4().hex
        do(tracking, 'register', dict(t, request_key=key, food_id=food['food_id'], source_id='youtube', interval_seconds=21600))
        do(tracking, 'register', dict(t, request_key=key, food_id=food['food_id'], source_id='youtube', interval_seconds=21600))
        do(tracking, 'register', dict(t, request_key=key, food_id=food['food_id'], source_id='naver_datalab'))
        do(tracking, 'register', dict(t, food_id=food['food_id'], source_id='youtube', interval_seconds=60))
        active = do(tracking, 'activate', dict(t, target_id=target['target_id'], expected_revision=target['revision'], approved=False))
        active = do(tracking, 'activate', dict(t, target_id=target['target_id'], expected_revision=target['revision'], approved=True))
        scheduled = do(tracking, 'schedule', dict(t, target_id=target['target_id'], expected_revision=active['revision']))
        do(tracking, 'schedule', dict(t, target_id=target['target_id'], expected_revision=scheduled['target']['revision']))
        paused = do(tracking, 'pause', dict(t, target_id=target['target_id'], expected_revision=scheduled['target']['revision']))
        do(tracking, 'pause', dict(t, target_id=target['target_id'], expected_revision=paused['revision']))
        resumed = do(tracking, 'resume', dict(t, target_id=target['target_id'], expected_revision=paused['revision'], approved=True))
        changed = do(tracking, 'interval', dict(t, target_id=target['target_id'], expected_revision=resumed['revision'], interval_seconds=7200))
        do(foods, 'alias', dict(review, action='approve', food_id=food['food_id'], surface_form='두쫀쿠정품', expected_revision=current(food['food_id'])['revision']))
        stale = tracking.target(target['target_id'])
        out.append(('ok', 'stale_target', stale))
        refreshed = do(tracking, 'refresh', dict(t, target_id=target['target_id'], expected_revision=changed['revision']))
        stopped = do(tracking, 'stop', dict(t, target_id=target['target_id'], expected_revision=refreshed['revision']))
        do(tracking, 'stop', dict(t, target_id=target['target_id'], expected_revision=stopped['revision']))
        do(tracking, 'register', dict(t, food_id=food['food_id'], source_id='instagram_graph', reregister=True, expected_revision=stopped['revision']))
        out.append(('ok', 'targets', sorted(tracking.list(), key=lambda t: (t['food_id'] != food['food_id'], t['source_id']))))
        out.append(('ok', 'history', tracking.history(target['target_id'])))
        out.append(('ok', 'due', tracking.due()))
        return out

    def test_identity_and_tracking_commands_decide_as_the_sqlite_services(self):
        outcomes = {}
        for side, (foods, tracking) in self.sides.items():
            outcomes[side] = self.normalize(self.script(foods, tracking), {})
        for n, (left, right) in enumerate(zip(outcomes['sqlite'], outcomes['target'])):
            with self.subTest(step=n, operation=left[1]):
                self.assertEqual(right, left)
        self.assertEqual(len(outcomes['target']), len(outcomes['sqlite']))
        errors = [(o[1], o[2]) for o in outcomes['target'] if o[0] == 'error']
        self.assertEqual(errors, [('review', 409), ('resolve', 409), ('alias', 409), ('relate', 409), ('merge', 409), ('register', 403),
                                  ('register', 409), ('register', 409), ('register', 400), ('activate', 409), ('schedule', 409), ('pause', 409)])


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleBackendTests(OracleBackendStandInTests):
    """The whole flow on the real disposable Oracle test schema; no sqlite3 connection at all."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db

    def storage_label(self):
        return 'oracle_test'


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleIdentityParityTests(IdentityParityStandInTests):
    """The parity script against the real disposable Oracle test schema."""
    make_target = OracleBackendTests.make_target


if __name__ == '__main__':
    unittest.main()
