"""ORACLE-LIVE-INTEGRATION-1 Phases 2-3: the live collection path of the Oracle runtime, verified without a real request.

The existing clients (GraphClient, YouTubeClient, NaverClient, collector.fetch) are assembled exactly as for live use,
so every run is labelled live_api, but their network openers are replaced by a recording fake (LiveWire): nothing
leaves the process, and the offline runner would refuse any socket that tried. Live stays closed until every gate
passes (live switch, exclusive source list, reviewed registry grant, configured credentials, legacy scheduler off,
writer transfer after a fresh legacy usage import, no block, target neither production nor Shadow-owned); the gates
are checked again before every request. Then RSS, Instagram, YouTube and NAVER collect onto the target alone (no
sqlite3 connection), each under its source policy: NAVER values and YouTube mentions are never stored. A spent budget
sends nothing, provider errors block or retry as the B3 contract says, the RSS schedule starts only for an approved
live source, an interrupted RSS run is finished as failed on the next start, and mock and live data never mix in B8.
The stand-in runs by default; the Oracle subclass repeats every case on the disposable MZ_TEST schema.
"""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from food_trends.__main__ import main
from food_trends.analytics_reader import TargetAnalyticsStore
from food_trends.analytics_service import FoodAnalytics
from food_trends.app_api import AppApi
from food_trends.execution import ExecutionSettings
from food_trends.execution_mock import MockResponse
from food_trends.food_service import FoodError
from food_trends.oracle_execution import OracleExecution
from food_trends.oracle_live import oracle_live_backend, oracle_live_readiness, transfer_writer
from food_trends.quota_ledger import import_legacy_usage
from food_trends.runtime_ownership import RuntimeOwnership
from food_trends.runtime_storage import oracle_backend, oracle_maintenance
from food_trends.storage import Storage
from food_trends.target_db import SQLiteTarget
from food_trends.target_repository import TargetRepository
from food_trends.tracking_access import mock_accounts
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema, settle_after_ddl
import test_oracle_backend as backend_tests
from test_oracle_backend import Feed
from test_oracle_rss import BAGEL, BRAND, COOKIE, feed

NOW = datetime(2026, 11, 2, 3, tzinfo=timezone.utc)
KST = timezone(timedelta(hours=9))
IG_USER = '17841400000000001'
SCOPES = {'instagram_graph': 'ig:' + IG_USER, 'youtube': 'local-default', 'naver_datalab': 'local-default', 'google_trends_rss': 'public-feed'}
SYNTHETIC = 'synthetic-live-fixture'  # stands in for a credential; the wire never forwards it anywhere
LIVE_ENV = {'B3_LIVE_ENABLED': 'true', 'B3_EXCLUSIVE_SOURCES': ','.join(SCOPES), 'B3_AUTOMATIC_ENABLED': 'false',
            'INSTAGRAM_ACCESS_TOKEN': SYNTHETIC, 'INSTAGRAM_USER_ID': IG_USER, 'FACEBOOK_PAGE_ID': '100000000000001',
            'INSTAGRAM_COLLECTION_ENABLED': 'false', 'YOUTUBE_API_KEY': SYNTHETIC, 'YOUTUBE_COLLECTION_ENABLED': 'false',
            'YOUTUBE_DAILY_SEARCH_LIMIT': '40', 'NAVER_DATALAB_CLIENT_ID': SYNTHETIC, 'NAVER_DATALAB_CLIENT_SECRET': SYNTHETIC,
            'NAVER_DATALAB_ENABLED': 'false', 'INSTAGRAM_GRAPH_API_VERSION': 'v26.0', 'YOUTUBE_DATA_API_V3_KEY': ''}


class LiveWire:
    """The network under the real clients: provider-shaped answers (MockTransport's), an RSS feed and per-endpoint failures.
    Installed by replacing each client module's opener; every request is recorded and none leaves the process."""

    def __init__(self):
        self.provider, self.calls, self.failures, self.rss = Feed(), [], {}, feed(COOKIE, BAGEL, BRAND)
        self.provider.posts = [('900001', '#두쫀쿠 #두바이쫀득쿠키 #디저트')]

    def open(self, request, timeout=None):
        url = request.full_url if hasattr(request, 'full_url') else request
        path = urlsplit(url).path
        self.calls.append('rss' if 'trends.google.com' in url else path.rsplit('/', 1)[-1])
        for suffix, (status, body, headers) in self.failures.items():
            if path.endswith(suffix):
                raise HTTPError(url, status, 'fixture failure', headers, io.BytesIO(json.dumps(body).encode()))
        if 'trends.google.com' in url:
            return MockResponse(self.rss)
        result = self.provider(request, timeout)
        return result if hasattr(result, 'read') else MockResponse(json.dumps(result, ensure_ascii=False).encode())

    def opener(self, *handlers):
        return SimpleNamespace(open=self.open)

    def install(self, case):
        for target, value in (('food_trends.instagram_graph.build_opener', self.opener), ('food_trends.youtube.build_opener', self.opener),
                              ('food_trends.naver_datalab.build_opener', self.opener), ('food_trends.collector.urlopen', self.open)):
            patcher = patch(target, value)
            patcher.start()
            case.addCleanup(patcher.stop)


class OracleLiveStandInTests(unittest.TestCase):
    sqlite_refused = backend_tests.OracleBackendStandInTests.sqlite_refused

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.target = self.make_target()
        self.repo = TargetRepository(self.target, clock=lambda: self.now)
        self.repo.migrate()
        self.wire = LiveWire()
        self.wire.install(self)
        self.env = patch.dict(os.environ, LIVE_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.registry = self.grants(*SCOPES)

    def make_target(self):
        return SQLiteTarget(Path(self.temp.name) / 'target.sqlite3')

    def grants(self, *sources):
        path = Path(self.temp.name) / ('registry-%s.json' % uuid.uuid4().hex[:8])
        path.write_text(json.dumps([dict(source_id=s, account_scope=SCOPES[s], permission_verified=True, policy_allowed=True,
                                         approved_by='fixture-operator', evidence='synthetic test grant, not a real approval') for s in sources]),
                        encoding='utf-8')
        return str(path)

    def transfer_all(self):
        """The approved writer transfer: a fresh legacy usage import, then each source handed to the Oracle runtime."""
        legacy = Storage.create(Path(self.temp.name) / ('legacy-%s.sqlite3' % uuid.uuid4().hex[:8]))
        import_legacy_usage(legacy.path, self.repo, now=self.now, scopes=[('instagram_graph', SCOPES['instagram_graph'])])
        for source in SCOPES:
            transfer_writer(self.repo, source, self.registry, now=self.now)

    def live(self):
        backend = oracle_live_backend(self.repo, self.registry, clock=lambda: self.now)
        self.addCleanup(backend.execution.release)
        return backend

    def food(self, backend, name, aliases=()):
        candidate = backend.foods.command('submit', dict(request_key=uuid.uuid4().hex, name=name, aliases=list(aliases)))
        return backend.foods.command('review', dict(request_key=uuid.uuid4().hex, candidate_id=candidate['candidate_id'], approved=True,
                                                    expected_revision=candidate['revision'], action='create', actor_ref='reviewer',
                                                    reason='음식 승인'))['resolution']

    def activate(self, backend, food_id, source):
        """Register and activate a tracking target on the backend's own account of the source (live: SCOPES, mock: local-default)."""
        scope = next(a.account_scope for a in backend.tracking.accounts.values() if a.source_id == source)
        target = backend.tracking.command('register', dict(request_key=uuid.uuid4().hex, food_id=food_id, source_id=source, account_scope=scope,
                                                           actor_ref='reviewer', reason='추적'))
        return backend.tracking.command('activate', dict(request_key=uuid.uuid4().hex, target_id=target['target_id'], approved=True,
                                                         expected_revision=target['revision'], actor_ref='reviewer', reason='추적'))

    def run_all(self, backend):
        results = []
        while True:
            result = backend.controller.run_once()
            if result is None:
                return results
            results.append(result)

    def rows(self, table, **where):
        return [r for r in self.repo.rows(table) if all(r[k] == v for k, v in where.items())]

    def day(self, food_id, origin, source='instagram_graph'):
        if not getattr(self, 'settled', False):
            settle_after_ddl(self.target)
            self.settled = True
        api = AppApi(FoodAnalytics(TargetAnalyticsStore(self.repo), clock=lambda: self.now, origin=origin), data_mode='database')
        today = self.now.astimezone(KST).date().isoformat()
        status, body, _ = api.handle('GET', '/api/v1/foods/%s/timeline?period=7d&end_date=%s&source=%s' % (food_id, today, source),
                                     {'Host': '127.0.0.1:8020'})
        self.assertEqual(status, 200, body)
        return {p['start_date']: p for p in body['data']['series'][0]['points']}[today]

    # Phase 3.1: closed until every gate passes

    def test_live_stays_closed_until_every_gate_passes(self):
        with patch.dict(os.environ, {'B3_LIVE_ENABLED': 'false', 'B3_EXCLUSIVE_SOURCES': ''}):
            result = oracle_live_readiness(self.repo, None)
        self.assertEqual((result['ready'], set(result['blockers'])), (False, {'B3_LIVE_DISABLED', 'REGISTRY_MISSING_OR_INVALID', 'NO_EXCLUSIVE_SOURCE'}))
        with patch.dict(os.environ, {'INSTAGRAM_COLLECTION_ENABLED': 'true', 'YOUTUBE_API_KEY': ''}):
            sources = oracle_live_readiness(self.repo, self.grants('naver_datalab'))['sources']
        self.assertEqual(sources['instagram_graph']['blockers'], ['NO_REGISTRY_GRANT', 'LEGACY_SCHEDULER_ENABLED', 'LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED'])
        self.assertEqual(sources['youtube']['blockers'], ['NO_REGISTRY_GRANT', 'NOT_CONFIGURED', 'LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED'])
        self.assertEqual(sources['naver_datalab']['blockers'], ['LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED'])
        # The writer transfer needs the grant, the legacy scheduler off and a legacy usage import of the last 15 minutes.
        with self.assertRaises(FoodError) as no_grant:
            transfer_writer(self.repo, 'youtube', self.grants('naver_datalab'), now=self.now)
        with self.assertRaises(FoodError) as stale:
            transfer_writer(self.repo, 'youtube', self.registry, now=self.now)
        with patch.dict(os.environ, {'YOUTUBE_COLLECTION_ENABLED': 'true'}), self.assertRaises(FoodError) as legacy_on:
            transfer_writer(self.repo, 'youtube', self.registry, now=self.now)
        self.assertEqual((no_grant.exception.status, stale.exception.status, legacy_on.exception.status), (403, 409, 409))
        with self.assertRaises(FoodError) as closed_backend:
            oracle_live_backend(self.repo, self.registry, clock=lambda: self.now)
        self.assertEqual(closed_backend.exception.status, 403)
        with self.assertRaises(ValueError):  # live settings without the live assembly's passed readiness
            OracleExecution(self.repo, mock_accounts(), settings=ExecutionSettings(live_enabled=True, exclusive_live_sources=('youtube',)))
        before = {t: self.repo.count(t) for t in ('quota_accounts', 'execution_jobs', 'rss_runs')}
        self.transfer_all()
        self.assertEqual({r['source_id']: r['usage_status'] for r in self.repo.rows('quota_accounts')},
                         {'instagram_graph': 'EXCLUSIVE', 'youtube': 'EXCLUSIVE', 'naver_datalab': 'EXCLUSIVE', 'google_trends_rss': 'EXCLUSIVE'})
        ready = oracle_live_readiness(self.repo, self.registry, now=self.now)
        self.assertEqual((ready['ready'], ready['http_requests'], ready['database_writes']), (True, 0, 0))
        self.assertEqual({t: self.repo.count(t) for t in ('execution_jobs', 'rss_runs')}, {'execution_jobs': 0, 'rss_runs': 0})
        self.assertGreater(self.repo.count('quota_accounts'), before['quota_accounts'])
        # Still closed on a target labelled production or owned by Shadow; no request was ever sent.
        with RuntimeOwnership(self.repo).held('SHADOW'):
            self.assertIn('TARGET_OWNED_BY_SHADOW', oracle_live_readiness(self.repo, self.registry)['blockers'])

        class Production(SQLiteTarget):
            settings = type('S', (), {'environment': 'production'})()
        production = TargetRepository(Production(Path(self.temp.name) / 'production.sqlite3'))
        production.migrate()
        self.assertIn('ORACLE_PRODUCTION_LOCKED', oracle_live_readiness(production, self.registry)['blockers'])
        self.assertEqual(self.wire.calls, [])

    def test_unmet_gates_create_no_job_and_send_nothing(self):
        """Where a live request could start (the assembly, the enqueue, a queued job's start), an unmet gate creates no job and sends
        nothing. A writer transfer withdrawn after a job was queued stops that job before its first request."""
        with self.assertRaises(FoodError):
            oracle_live_backend(self.repo, self.registry, clock=lambda: self.now)  # nothing transferred yet: no live assembly
        self.assertEqual((self.repo.count('execution_jobs'), self.wire.calls), (0, []))
        self.transfer_all()
        backend = self.live()
        food = self.food(backend, '두쫀쿠')
        self.activate(backend, food['food_id'], 'youtube')
        queued = backend.execution.schedule(mode='live')
        self.assertEqual({j['status'] for j in queued}, {'READY'})
        row = self.rows('quota_accounts', source_id='youtube')[0]
        with self.repo.db.transaction() as tx:  # the YouTube transfer withdrawn (the legacy collector may run again)
            self.repo.upsert(tx, 'quota_accounts', dict(row, usage_status='IMPORTED'))
        stopped = [backend.controller.run_once(j['job_id']) for j in queued]
        self.assertEqual({(r['status'], r['error_kind']) for r in stopped}, {('BLOCKED', 'LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED')})
        self.assertEqual([c for j in queued for c in self.rows('execution_calls', job_id=j['job_id'])], [])
        jobs = self.repo.count('execution_jobs')
        self.now += timedelta(hours=7)  # the target is due again: the enqueue itself is refused
        again = backend.execution.schedule(mode='live')
        self.assertTrue(again)
        self.assertEqual({(r['status'], 'LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED' in r['reasons']) for r in again}, {('BLOCKED', True)})
        self.assertEqual(self.repo.count('execution_jobs'), jobs)
        self.assertEqual(self.wire.calls, [])  # not one request left the runtime
        self.assertIn('LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED', oracle_live_readiness(self.repo, self.registry, now=self.now)['sources']['youtube']['blockers'])

    # Phases 2.2-2.5: the existing clients collect onto the target alone

    def test_live_collection_runs_the_existing_clients_on_the_target_alone(self):
        self.transfer_all()
        backend = self.live()
        self.assertEqual((backend.rss.origin, backend.controller.status()['available_modes']), ('live_api', ['preview', 'live']))
        with self.sqlite_refused() as opened:
            collected = backend.rss.collect()
            self.assertEqual((collected['success'], collected['entity_resolution']['candidates']), (True, 3))
            cookie = next(c for c in backend.foods.candidates() if c['surface_form'] == COOKIE[0])
            self.assertEqual((cookie['origin'], cookie['status']), ('live_api', 'NEEDS_REVIEW'))  # discovered, never auto-approved
            food = backend.foods.command('review', dict(request_key=uuid.uuid4().hex, candidate_id=cookie['candidate_id'], approved=True,
                                                        expected_revision=cookie['revision'], action='create', canonical_name='두쫀쿠',
                                                        actor_ref='reviewer', reason='Live 후보 승인'))['resolution']
            for source in ('instagram_graph', 'youtube', 'naver_datalab'):
                self.assertEqual(self.activate(backend, food['food_id'], source)['effective_status'], 'ACTIVE')
            jobs = backend.execution.schedule(mode='live')
            self.assertEqual({j['source_id'] for j in jobs}, {'instagram_graph', 'youtube', 'naver_datalab'})
            self.assertEqual({j['mode'] for j in jobs}, {'live'})
            results = self.run_all(backend)
            self.assertEqual({r['status'] for r in results}, {'SUCCEEDED'})
            oracle_maintenance(backend)  # the approval made after the RSS run links that item (B5 reprocessing)
        self.assertEqual(opened, [])
        # Every run, staged run and content row says live_api; the policy of each source holds.
        self.assertEqual({r['origin'] for r in self.repo.rows('execution_runs')}, {'live_api'})
        self.assertEqual({r['origin'] for r in self.repo.rows('ingest_runs')}, {'live_api'})
        self.assertEqual({r['origin'] for r in self.repo.rows('rss_runs')}, {'live_api'})
        self.assertEqual({r['source_code'] for r in self.repo.rows('source_contents')}, {'google_trends_rss', 'instagram_graph', 'youtube'})
        self.assertEqual({r['source_code'] for r in self.repo.rows('food_mentions')}, {'google_trends_rss', 'instagram_graph'})  # never YouTube
        self.assertEqual({r['source_code'] for r in self.repo.rows('metric_observations')}, {'google_trends_rss', 'instagram_graph'})  # no NAVER value
        self.assertTrue(self.rows('ingest_runs', source_code='naver_datalab'))  # the NAVER request is recorded, its values are not
        # Only the wire answered: the RSS feed, Instagram (usage check, lookup, media), YouTube (search, videos), NAVER (search).
        self.assertEqual(set(self.wire.calls), {'rss', 'recently_searched_hashtags', 'ig_hashtag_search', 'recent_media', 'search', 'videos'})
        self.assertEqual(self.wire.calls.count('search'), 3)  # two YouTube searches (one per alias) and one NAVER DataLab request
        # B8 shows the live observation under live_api and nothing under mock.
        live, mock = self.day(food['food_id'], 'live_api'), self.day(food['food_id'], 'mock')
        self.assertEqual((live['value'], live['value_state'], live['coverage']), (1, 'OBSERVED', 'COMPLETE'))
        self.assertEqual((mock['value'], mock['value_state']), (None, 'NOT_OBSERVED'))

    # Phases 2.3-3.2: budgets send nothing, provider errors block or retry

    def test_a_spent_budget_sends_nothing_and_provider_errors_block_or_retry(self):
        self.transfer_all()
        with patch.dict(os.environ, {'YOUTUBE_DAILY_SEARCH_LIMIT': '1'}):
            backend = self.live()
        food = self.food(backend, '두쫀쿠', ['두쫀쿠디저트'])
        self.activate(backend, food['food_id'], 'youtube')
        jobs = backend.execution.schedule(mode='live')
        self.assertEqual(len(jobs), 2)  # one search per approved alias
        first = backend.controller.run_once(jobs[0]['job_id'])
        searches = self.wire.calls.count('search')
        second = backend.controller.run_once(jobs[1]['job_id'])
        self.assertEqual((first['status'], second['status'], second['error_kind']), ('SUCCEEDED', 'RETRY_WAIT', 'budget'))
        self.assertEqual(self.wire.calls.count('search'), searches)  # the daily search budget is spent: no request
        self.assertFalse([c for c in self.rows('execution_calls', job_id=jobs[1]['job_id'])])
        # Instagram: an expired token blocks the account; later jobs of that account send nothing.
        self.wire.failures['ig_hashtag_search'] = (401, {'error': {'code': 190, 'message': 'expired'}}, {})
        self.activate(backend, food['food_id'], 'instagram_graph')
        blocked = [backend.controller.run_once(j['job_id']) for j in backend.execution.schedule(mode='live') if j.get('source_id') == 'instagram_graph']
        # The first job meets the expired token; the second (the other alias) is stopped by the block before any request.
        self.assertEqual([(r['status'], r['error_kind']) for r in blocked], [('BLOCKED', 'authentication'), ('BLOCKED', 'ACCOUNT_BLOCKED_authentication')])
        self.assertEqual(self.wire.calls.count('ig_hashtag_search'), 1)
        self.assertEqual(self.rows('execution_blocks', source_id='instagram_graph')[0]['error_kind'], 'authentication')
        self.assertIn('ACCOUNT_BLOCKED_authentication', oracle_live_readiness(self.repo, self.registry, now=self.now)['sources']['instagram_graph']['blockers'])
        calls = len(self.wire.calls)
        self.now += timedelta(hours=2)
        again = [j for j in backend.execution.schedule(mode='live') if j.get('source_id') == 'instagram_graph' or 'reasons' in j]
        self.assertTrue(all(j['status'] == 'BLOCKED' and 'ACCOUNT_BLOCKED_authentication' in j['reasons'] for j in again if 'reasons' in j))
        self.run_all(backend)
        self.assertEqual([c for c in self.wire.calls[calls:] if c in ('ig_hashtag_search', 'recent_media')], [])
        # NAVER: 429 with a long Retry-After blocks the account until then and the job waits; it is retried later, not dropped.
        self.wire.failures = {'search': (429, {}, {'Retry-After': '120'})}
        self.activate(backend, food['food_id'], 'naver_datalab')
        naver = [j for j in backend.execution.schedule(mode='live') if j.get('source_id') == 'naver_datalab']
        waited = backend.controller.run_once(naver[0]['job_id'])
        self.assertEqual((waited['status'], waited['error_kind']), ('RETRY_WAIT', 'rate_limit'))
        self.assertEqual(self.rows('execution_blocks', source_id='naver_datalab')[0]['error_kind'], 'rate_limit')
        self.wire.failures = {}
        self.now += timedelta(minutes=10)
        self.assertEqual(backend.controller.run_once(naver[0]['job_id'])['status'], 'SUCCEEDED')
        self.assertFalse(self.rows('metric_observations', source_code='naver_datalab'))

    # Phase 3.3: the RSS schedule, a revoked approval and an interrupted run

    def test_rss_schedule_revocation_and_interrupted_runs(self):
        mock = oracle_backend(self.repo, mock_accounts(), lambda: self.now, Feed(), rss_fetcher=lambda timeout: (feed(COOKIE), 200))
        self.addCleanup(mock.execution.release)
        with self.assertRaises(FoodError):
            mock.rss.start()  # a mock service never schedules
        self.transfer_all()
        backend = self.live()
        backend.rss.interval = 3600
        backend.rss.start()
        deadline = time.monotonic() + 20
        while not self.rows('rss_runs', success=1) and time.monotonic() < deadline:
            time.sleep(0.05)
        backend.rss.stop_event.set()
        backend.rss.thread.join(10)
        self.assertEqual(len(self.rows('rss_runs', success=1, origin='live_api')), 1)
        # A crashed collector left a run unfinished: a collection moments later waits, one after STALE_AFTER finishes it as failed.
        crashed = backend.rss.storage.start('live_api')
        fetches = self.wire.calls.count('rss')
        self.assertEqual(backend.rss.collect()['accepted'], False)
        self.assertEqual(self.wire.calls.count('rss'), fetches)
        self.now += timedelta(minutes=11)
        resumed = backend.rss.collect()
        self.assertEqual((resumed['success'], resumed['run_id']), (True, crashed + 1))
        interrupted = self.rows('rss_runs', run_id=crashed)[0]
        self.assertEqual(interrupted['success'], 0)
        self.assertIn('interrupted', interrupted['error'])
        self.assertEqual(self.rows('ingest_runs', run_ref='runs:%d' % crashed)[0]['coverage'], 'NONE')  # not observed, never a zero
        # Revoking the transfer stops the next live fetch before any request.
        row = self.rows('quota_accounts', source_id='google_trends_rss')[0]
        with self.repo.db.transaction() as tx:
            self.repo.upsert(tx, 'quota_accounts', dict(row, usage_status='IMPORTED'))
        fetches = self.wire.calls.count('rss')
        refused = backend.rss.collect()
        self.assertEqual((refused['accepted'], refused['error'], refused['blockers']),
                         (False, 'oracle_live_rss_blocked', ['LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED']))
        self.assertEqual(self.wire.calls.count('rss'), fetches)
        with self.assertRaises(FoodError):
            backend.rss.start()

    def test_automatic_live_execution_needs_its_own_switch(self):
        self.transfer_all()
        with self.assertRaises(FoodError):
            self.live().controller.start()  # the live approval alone does not start the B3 scheduler
        with patch.dict(os.environ, {'B3_AUTOMATIC_ENABLED': 'true'}):
            automatic = self.live()
        food = self.food(automatic, '두쫀쿠')
        self.activate(automatic, food['food_id'], 'instagram_graph')
        automatic.controller.start()
        deadline = time.monotonic() + 20
        while not self.rows('execution_jobs', status='SUCCEEDED') and time.monotonic() < deadline:
            time.sleep(0.05)
        automatic.controller.stop_event.set()
        automatic.controller.thread.join(15)
        self.assertEqual({(r['exec_mode'], r['status']) for r in self.repo.rows('execution_jobs')}, {('live', 'SUCCEEDED')})
        self.assertIn('recent_media', self.wire.calls)

    def test_mock_and_live_origins_never_mix(self):
        mock = oracle_backend(self.repo, mock_accounts(), lambda: self.now, Feed(), rss_fetcher=lambda timeout: (feed(COOKIE), 200))
        self.addCleanup(mock.execution.release)
        food = self.food(mock, '두쫀쿠')
        self.activate(mock, food['food_id'], 'instagram_graph')
        mock.execution.schedule()
        self.run_all(mock)
        mock.execution.release()
        self.transfer_all()
        backend = self.live()
        self.activate(backend, food['food_id'], 'instagram_graph')  # the live account's own target (scope ig:...)
        backend.execution.schedule(mode='live')
        self.run_all(backend)
        runs = {(r['origin'], r['source_code']) for r in self.repo.rows('execution_runs')}
        self.assertEqual(runs, {('mock', 'instagram_graph'), ('live_api', 'instagram_graph')})
        live, mocked = self.day(food['food_id'], 'live_api'), self.day(food['food_id'], 'mock')
        self.assertEqual((live['value'], live['runs']['successful'], mocked['value'], mocked['runs']['successful']), (1, 1, 1, 1))  # its own run only

    def test_the_cli_keeps_live_closed(self):
        def run(*argv):
            stderr = io.StringIO()
            with patch('sys.argv', ['food_trends', *argv]), patch.dict(os.environ, {'ORACLE_TEST_DSN': ''}), \
                    contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as stop:
                main()
            return stop.exception.code, stderr.getvalue()
        for argv, expected in ((('--backend', 'oracle-test', '--execution-mode', 'live'), '--execution-registry'),
                               (('--backend', 'oracle-test', '--writer-transfer', 'youtube'), '--execution-registry'),
                               (('--backend', 'oracle-test', '--execution-automatic'), '--execution-mode live'),
                               (('--writer-transfer', 'youtube'), '--backend oracle-test'),
                               (('--backend', 'oracle-production', '--execution-readiness', self.registry), 'oracle_production_runtime_disabled'),
                               (('--backend', 'oracle-test', '--execution-readiness', self.registry), 'oracle_not_configured')):
            code, message = run(*argv)
            self.assertEqual((code, expected in message), (2, True), argv)
        self.assertEqual(self.wire.calls, [])


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleLiveTests(OracleLiveStandInTests):
    """Every case on the real disposable Oracle test schema; no sqlite3 connection during the live flows."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db


if __name__ == '__main__':
    unittest.main()
