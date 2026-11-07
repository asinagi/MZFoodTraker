"""ORACLE-WRITER-RELEASE-1: a source's writer handed to the Oracle runtime and back, verified without a real request.

The live path of ORACLE-LIVE-INTEGRATION-1 runs over a recording wire (LiveWire): nothing leaves the process. A release stops
new requests of the account at once, waits while one is in flight (DRAINING), then closes what is left with WRITER_RELEASED and
returns the account to IMPORTED; its history is append-only and a re-run changes nothing. The Oracle period's usage goes to the
legacy SQLite call tables (usage_handover), where the unchanged legacy budget code counts it, and a legacy collector may run
again only once resume_readiness says so; a recovery after the handover still applies the Oracle period's updates. The stand-in
runs by default; the Oracle subclass repeats every case on MZ_TEST.
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from food_trends import usage_handover
from food_trends.__main__ import main
from food_trends.config import InstagramConfig, NaverConfig, YouTubeConfig
from food_trends.food_service import FoodError
from food_trends.instagram_graph import GraphError
from food_trends.instagram_service import InstagramService
from food_trends.naver_service import NaverService
from food_trends.oracle_execution import OracleExecution
from food_trends.oracle_live import ACTIONS, oracle_live_readiness, release_writer, transfer_writer, writer_status
from food_trends.oracle_rss import Blocked
from food_trends.quota import daily_window
from food_trends.quota_ledger import import_legacy_usage
from food_trends.reverse_recovery import Recovery, RecoveryRefused, recover
from food_trends.runtime_ownership import OwnershipRefused, RuntimeOwnership
from food_trends.service import CollectionService
from food_trends.storage import Storage
from food_trends.target_db import SQLiteTarget
from food_trends.target_migration import migrate
from food_trends.target_repository import TargetRepository
from food_trends.tracking_service import instant, stamp
from food_trends.usage_handover import handover, resume_readiness
from food_trends.youtube import YouTubeError
from food_trends.youtube_storage import YouTubeStore
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema
import test_oracle_live as live_tests
from test_oracle_live import KST, LIVE_ENV, NOW, SCOPES, LiveWire
from test_oracle_rss import COOKIE, feed

API_SOURCES = ('instagram_graph', 'youtube', 'naver_datalab')


class Wire(LiveWire):
    """LiveWire with hooks that run while a request is on the wire, before its answer."""

    def __init__(self):
        super().__init__()
        self.hooks = []

    def open(self, request, timeout=None):
        url = request.full_url if hasattr(request, 'full_url') else request
        for hook in list(self.hooks):
            hook(url)
        return super().open(request, timeout)


class Crash(BaseException):
    """A worker process dying mid-request: no handler of the runtime catches it."""


def frozen(case):
    """datetime whose now() is the test clock, for the legacy collectors' own wall-clock windows."""
    from datetime import datetime

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.now.astimezone(tz) if tz else case.now.replace(tzinfo=None)
    return Frozen


class WriterReleaseStandInTests(unittest.TestCase):
    make_target = live_tests.OracleLiveStandInTests.make_target
    grants = live_tests.OracleLiveStandInTests.grants
    live = live_tests.OracleLiveStandInTests.live
    food = live_tests.OracleLiveStandInTests.food
    activate = live_tests.OracleLiveStandInTests.activate
    run_all = live_tests.OracleLiveStandInTests.run_all
    rows = live_tests.OracleLiveStandInTests.rows

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.target = self.make_target()
        self.repo = TargetRepository(self.target, clock=lambda: self.now)
        self.repo.migrate()
        self.wire = Wire()
        self.wire.install(self)
        env = patch.dict(os.environ, LIVE_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.registry = self.grants(*SCOPES)
        self.legacy = Storage.create(Path(self.temp.name) / 'legacy.sqlite3')  # the legacy collectors' database, never the operational one

    def transfer(self):
        """The approved transfer: a fresh usage import from the legacy database, then every source handed to the runtime."""
        import_legacy_usage(self.legacy.path, self.repo, now=self.now, scopes=[('instagram_graph', SCOPES['instagram_graph'])])
        for source in SCOPES:
            transfer_writer(self.repo, source, self.registry, now=self.now)

    def collect(self, backend, *sources):
        """One approved food tracked on the sources and every due live job run: the Oracle period's provider usage."""
        food = self.food(backend, '두쫀쿠')
        for source in sources:
            self.activate(backend, food['food_id'], source)
        backend.execution.schedule(mode='live')
        self.assertEqual({r['status'] for r in self.run_all(backend)}, {'SUCCEEDED'})
        return food

    def events(self, source):
        rows = self.rows('writer_transfer_events', source_id=source)
        return [(e['cycle_no'], e['action']) for e in sorted(rows, key=lambda e: (e['cycle_no'], ACTIONS.index(e['action']), e['occurred_at']))]

    def legacy_rows(self, sql, path=None):
        with closing(sqlite3.connect(path or self.legacy.path)) as db:
            return db.execute(sql).fetchall()

    # A. The release command; D. its history

    def test_release_stops_the_account_at_once_and_keeps_the_other_writers(self):
        """Transfer, then release one source: its queued job is closed with WRITER_RELEASED, the account is IMPORTED again, nothing of
        it is sent afterwards, the history has the three steps, and every other source keeps the runtime as its writer."""
        self.transfer()
        backend = self.live()
        food = self.food(backend, '두쫀쿠')
        for source in ('youtube', 'naver_datalab'):
            self.activate(backend, food['food_id'], source)
        queued = backend.execution.schedule(mode='live')
        youtube = [j for j in queued if j['source_id'] == 'youtube']
        naver = [j for j in queued if j['source_id'] == 'naver_datalab']
        self.assertEqual((len(youtube), len(naver)), (1, 1))
        result = release_writer(self.repo, 'youtube', self.registry)
        self.assertEqual((result['status'], result['closed_jobs'], result['interrupted_calls'], result['unchanged'], result['http_requests']),
                         ('RELEASED', 1, 0, False, 0))
        account = self.rows('quota_accounts', source_id='youtube')[0]
        self.assertEqual((account['usage_status'], account['evidence_ref']), ('IMPORTED', 'writer_release:fixture-operator'))
        self.assertIsNotNone(account['release_requested_at'])
        closed = self.rows('execution_jobs', job_id=youtube[0]['job_id'])[0]
        self.assertEqual((closed['status'], closed['error_kind']), ('CANCELLED', 'WRITER_RELEASED'))
        self.assertEqual({s['cancellation_reason'] for s in self.rows('tracking_slots', job_id=youtube[0]['job_id'])}, {'WRITER_RELEASED'})
        self.assertEqual(self.events('youtube'), [(1, 'TRANSFER'), (1, 'RELEASE_REQUESTED'), (1, 'RELEASED')])
        # Right after: the gates name the release, the plan cannot be queued again and the closed job never starts; NAVER, still
        # the runtime's, runs. The only request that left is NAVER's.
        self.assertEqual(oracle_live_readiness(self.repo, self.registry, now=self.now)['sources']['youtube']['blockers'],
                         ['WRITER_RELEASED', 'LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED'])
        refused = backend.execution.enqueue([youtube[0]['plan_id']], 'live')
        self.assertEqual((refused[0]['status'], refused[0]['reasons'][0]), ('BLOCKED', 'WRITER_RELEASED'))
        self.assertIsNone(backend.controller.run_once(youtube[0]['job_id']))
        self.assertEqual(backend.controller.run_once(naver[0]['job_id'])['status'], 'SUCCEEDED')
        self.assertEqual(self.wire.calls, ['search'])  # the NAVER DataLab request; not one YouTube request
        status = writer_status(self.repo, now=self.now)
        self.assertEqual({s: v['writer'] for s, v in status['sources'].items()},
                         {'instagram_graph': 'ORACLE', 'youtube': 'RELEASED', 'naver_datalab': 'ORACLE', 'google_trends_rss': 'ORACLE'})
        self.assertEqual((status['http_requests'], status['database_writes']), (0, 0))
        self.assertEqual({r['source_id']: r['usage_status'] for r in self.repo.rows('quota_accounts')},
                         {'instagram_graph': 'EXCLUSIVE', 'youtube': 'IMPORTED', 'naver_datalab': 'EXCLUSIVE', 'google_trends_rss': 'EXCLUSIVE'})
        # A later transfer starts a new cycle (after a fresh usage import); the earlier history stays as it was.
        import_legacy_usage(self.legacy.path, self.repo, now=self.now)
        self.assertEqual(transfer_writer(self.repo, 'youtube', self.registry, now=self.now)['cycle'], 2)
        self.assertEqual(self.events('youtube'), [(1, 'TRANSFER'), (1, 'RELEASE_REQUESTED'), (1, 'RELEASED'), (2, 'TRANSFER')])
        self.assertIsNone(self.rows('quota_accounts', source_id='youtube')[0]['release_requested_at'])

    # B. Jobs and leases while releasing

    def test_a_request_in_flight_lands_and_holds_the_release_until_its_job_ends(self):
        """A release that starts while a request is on the wire answers DRAINING (that job holds its lease) and stops everything new
        at once; the request in flight lands its page, the job's next request meets the release, and the release then finishes."""
        self.transfer()
        backend = self.live()
        food = self.food(backend, '두쫀쿠')
        self.activate(backend, food['food_id'], 'youtube')
        job = backend.execution.schedule(mode='live')[0]
        seen = {}

        def release_while_on_the_wire(url):
            if 'googleapis.com' in url and urlsplit(url).path.endswith('/search') and not seen:
                seen['release'] = release_writer(self.repo, 'youtube', self.registry)
                seen['gate'] = oracle_live_readiness(self.repo, self.registry, now=self.now)['sources']['youtube']['blockers']
                seen['writer'] = writer_status(self.repo, now=self.now)['sources']['youtube']['writer']
        self.wire.hooks.append(release_while_on_the_wire)
        result = backend.controller.run_once(job['job_id'])
        self.assertEqual((seen['release']['status'], [j['job_id'] for j in seen['release']['in_flight']['jobs']]), ('DRAINING', [job['job_id']]))
        self.assertEqual((seen['gate'], seen['writer']), (['WRITER_RELEASE_IN_PROGRESS'], 'RELEASING'))
        self.assertEqual((result['status'], result['error_kind']), ('CANCELLED', 'WRITER_RELEASED'))
        self.assertEqual([(c['endpoint'], c['status']) for c in self.rows('execution_calls', job_id=job['job_id'])], [('search', 'SUCCEEDED')])
        self.assertEqual(self.wire.calls, ['search'])  # the videos request after it was never sent
        done = release_writer(self.repo, 'youtube', self.registry)
        self.assertEqual((done['status'], done['closed_jobs'], done['open_usage']['by_quota_type']), ('RELEASED', 0, {'youtube.search_calls': 1}))
        self.assertEqual(self.events('youtube'), [(1, 'TRANSFER'), (1, 'RELEASE_REQUESTED'), (1, 'RELEASED')])

    def test_a_crashed_worker_holds_the_release_only_until_its_lease_ends(self):
        """A worker dies after sending (its call stays SENT, its lease runs on): the release waits for the lease, then counts the call
        as UNKNOWN, closes the job with WRITER_RELEASED and hands the uncertain call over as usage, never as nothing."""
        self.transfer()
        backend = self.live()
        food = self.food(backend, '두쫀쿠')
        self.activate(backend, food['food_id'], 'youtube')
        job = backend.execution.schedule(mode='live')[0]

        def die(stage):
            if stage == 'after_http':
                raise Crash()
        backend.controller.worker.fault = die
        with self.assertRaises(Crash):
            backend.controller.run_once(job['job_id'])
        self.assertEqual([c['status'] for c in self.rows('execution_calls', job_id=job['job_id'])], ['SENT'])
        self.assertEqual(release_writer(self.repo, 'youtube', self.registry)['status'], 'DRAINING')
        self.now += timedelta(seconds=150)  # past the job lease (120 seconds)
        released = release_writer(self.repo, 'youtube', self.registry)
        self.assertEqual((released['status'], released['closed_jobs'], released['interrupted_calls']), ('RELEASED', 1, 1))
        call = self.rows('execution_calls', job_id=job['job_id'])[0]
        self.assertEqual((call['status'], call['error_kind']), ('UNKNOWN', 'INTERRUPTED_AFTER_SEND'))
        row = self.rows('execution_jobs', job_id=job['job_id'])[0]
        self.assertEqual((row['status'], row['error_kind'], row['remote_duplicate_possible']), ('CANCELLED', 'WRITER_RELEASED', 1))
        handed = handover(self.repo, 'youtube', self.legacy.path, now=self.now)
        self.assertEqual((handed['handed_over'], handed['open_usage']['uncertain']), (1, 1))
        self.assertEqual(self.legacy_rows("SELECT endpoint,status,finished_at,origin FROM youtube_calls WHERE run_id LIKE 'oracle:%'"),
                         [('search', 'unknown', None, 'live_api')])
        self.assertTrue(resume_readiness(self.repo, 'youtube', self.legacy.path, now=self.now)['ready'])
        self.assertEqual(self.wire.calls, ['search'])

    def test_an_enqueue_racing_a_release_waits_for_it_and_is_refused(self):
        """While the release holds the account (paused inside its last step), a live enqueue of that account waits for it instead
        of slipping a new job in, and is then refused; the queued job is closed and nothing is sent."""
        self.transfer()
        backend = self.live()
        food = self.food(backend, '두쫀쿠')
        self.activate(backend, food['food_id'], 'youtube')
        job = backend.execution.schedule(mode='live')[0]
        holding, proceed, results = threading.Event(), threading.Event(), {}
        original = OracleExecution.close_released_account

        def paused(execution, t, source, scope):
            holding.set()  # inside the release transaction, after the account lock
            proceed.wait(20)
            return original(execution, t, source, scope)

        def release():
            results['release'] = release_writer(self.repo, 'youtube', self.registry)

        def enqueue():
            results['enqueue'] = backend.execution.enqueue([job['plan_id']], 'live')
        with patch.object(OracleExecution, 'close_released_account', paused):
            releaser = threading.Thread(target=release)
            releaser.start()
            self.assertTrue(holding.wait(20))
            enqueuer = threading.Thread(target=enqueue)
            enqueuer.start()
            enqueuer.join(1.5)
            waited = enqueuer.is_alive()
            proceed.set()
            releaser.join(30)
            enqueuer.join(30)
        self.assertTrue(waited)
        self.assertEqual(results['release']['status'], 'RELEASED')
        self.assertEqual((results['enqueue'][0]['status'], results['enqueue'][0]['reasons'][0]), ('BLOCKED', 'WRITER_RELEASED'))
        self.assertEqual({(r['status'], r['error_kind']) for r in self.rows('execution_jobs', source_id='youtube')}, {('CANCELLED', 'WRITER_RELEASED')})
        self.assertEqual(self.wire.calls, [])

    # C. Usage handover

    def test_handed_over_usage_keeps_the_legacy_collectors_within_their_budgets(self):
        """The Oracle period's Instagram tag, YouTube calls and NAVER request reach the legacy call tables, and the unchanged legacy
        budget code counts them: where the two writers together would pass the limit, the legacy collector stops before a
        request, while a copy taken before the handover would have sent."""
        self.transfer()
        backend = self.live()
        self.collect(backend, *API_SOURCES)
        for source in API_SOURCES:
            self.assertEqual(release_writer(self.repo, source, self.registry)['status'], 'RELEASED')
        before = Path(self.temp.name) / 'legacy-before-handover.sqlite3'
        shutil.copyfile(self.legacy.path, before)
        for source in API_SOURCES:
            self.assertEqual(resume_readiness(self.repo, source, self.legacy.path, now=self.now)['blockers'], ['ORACLE_USAGE_NOT_HANDED_OVER'])
        handed = {source: handover(self.repo, source, self.legacy.path, now=self.now) for source in API_SOURCES}
        self.assertEqual({s: r['open_usage']['by_quota_type'] for s, r in handed.items()},
                         {'instagram_graph': {'instagram.unique_tags': 1}, 'youtube': {'youtube.search_calls': 1, 'youtube.video_calls': 1},
                          'naver_datalab': {'naver.calls': 1}})
        self.assertEqual({s: (r['handed_over'], r['legacy_writes_during_oracle_period'], r['http_requests']) for s, r in handed.items()},
                         {'instagram_graph': (1, 0, 0), 'youtube': (2, 0, 0), 'naver_datalab': (1, 0, 0)})
        self.assertEqual({s: resume_readiness(self.repo, s, self.legacy.path, now=self.now)['ready'] for s in API_SOURCES},
                         dict.fromkeys(API_SOURCES, True))
        # NAVER: no request body or value is copied, only the call itself.
        self.assertEqual(self.legacy_rows("SELECT request_json,candidate_ids,origin,status FROM naver_calls WHERE fingerprint LIKE 'oracle:%'"),
                         [('{}', '[]', 'live_api', 'success')])
        day = daily_window(self.now, 'America/Los_Angeles')['day']
        with patch.dict(os.environ, {'YOUTUBE_DAILY_SEARCH_LIMIT': '1'}), patch('food_trends.youtube_storage.pacific_day', lambda: day):
            config = YouTubeConfig.from_env()
            for path, refused in ((before, False), (self.legacy.path, True)):
                store = YouTubeStore(Storage(path))
                with store.operation() as owner:
                    if refused:
                        with self.assertRaises(YouTubeError) as stop:
                            store.reserve('search', None, owner, config)
                        self.assertEqual(stop.exception.kind, 'budget')
                    else:
                        store.reserve('search', None, owner, config)
        with patch('food_trends.naver_service.today', lambda: self.now.astimezone(KST).date()):
            daily = [NaverService(Storage(path), NaverConfig.from_env()).status()['daily_requests'] for path in (before, self.legacy.path)]
        self.assertEqual(daily, [0, 1])
        clock = frozen(self)
        with patch.dict(os.environ, {'INSTAGRAM_SEARCH_SOFT_LIMIT': '1'}), patch('food_trends.instagram_storage.datetime', clock), \
                patch('food_trends.instagram_service.datetime', clock):
            for path, refused in ((before, False), (self.legacy.path, True)):
                service = InstagramService(Storage(path), InstagramConfig.from_env())
                service.store.state('search_snapshot', {'ids': [], 'checked_at': self.now.isoformat()})  # a fresh provider check, no request
                self.assertEqual(service.budget()['local_unique_7d'], int(refused))
                if refused:
                    with self.assertRaises(GraphError) as stop:
                        service.reserve({'name': '새디저트', 'instagram_id': None})
                    self.assertEqual(stop.exception.kind, 'budget')
                else:
                    service.reserve({'name': '새디저트', 'instagram_id': None})
        self.assertEqual(self.wire.calls.count('ig_hashtag_search'), 1)  # only the Oracle period's lookup ever reached the wire

    def test_usage_not_in_sqlite_keeps_the_legacy_collector_off_until_its_window_ends(self):
        """Fail-closed: before the handover, into a database that cannot take it, or into one restored from before the legacy
        collector's own usage, the legacy collector stays off until the provider window ends; nothing claims a provider debit."""
        window = daily_window(self.now, 'America/Los_Angeles')
        with closing(sqlite3.connect(self.legacy.path)) as db, db:  # the legacy collector's own search earlier that Pacific day
            db.execute("""INSERT INTO youtube_calls(day,endpoint,started_at,finished_at,status,http_status,origin)
                VALUES (?,'search',?,?,'success',200,'live_api')""", (window['day'], stamp(self.now - timedelta(hours=1)), stamp(self.now - timedelta(hours=1))))
        self.transfer()
        backend = self.live()
        self.collect(backend, 'youtube')
        released = release_writer(self.repo, 'youtube', self.registry)
        self.assertEqual((released['usage_handover'], released['open_usage']['open_until']), ('REQUIRED', window['end']))
        check = resume_readiness(self.repo, 'youtube', self.legacy.path, now=self.now)
        self.assertEqual((check['ready'], check['blockers'], check['resume_not_before']), (False, ['ORACLE_USAGE_NOT_HANDED_OVER'], window['end']))
        empty = Path(self.temp.name) / 'empty.sqlite3'
        sqlite3.connect(empty).close()
        with self.assertRaises(FoodError) as outdated:
            handover(self.repo, 'youtube', empty, now=self.now)
        self.assertEqual(outdated.exception.status, 409)
        self.assertEqual(self.legacy_rows("SELECT count(*) FROM sqlite_master WHERE type='table'", empty), [(0,)])
        older = Storage.create(Path(self.temp.name) / 'restored-older.sqlite3')
        partial = handover(self.repo, 'youtube', older.path, now=self.now)
        self.assertEqual((partial['handed_over'], partial['legacy_missing'], partial['resume_not_before']), (2, 1, window['end']))
        check = resume_readiness(self.repo, 'youtube', older.path, now=self.now)
        self.assertEqual((check['blockers'], check['resume_not_before']), (['DESTINATION_MISSING_LEGACY_USAGE'], window['end']))
        self.assertFalse([r for r in self.repo.rows('quota_usage_ledger') if r['evidence'] == 'PROVIDER_CONFIRMED'])
        self.now = instant(window['end']) + timedelta(seconds=1)  # the window is over: nothing open is left to count
        self.assertEqual([resume_readiness(self.repo, 'youtube', p, now=self.now)['ready'] for p in (self.legacy.path, older.path)], [True, True])

    # D. Idempotence and resumption

    def test_release_and_handover_converge_when_run_again_or_after_a_failure(self):
        """A release that fails after its first step leaves the account stopped and finishes when run again; a second release changes
        nothing; a handover that fails midway commits nothing, the next writes every row and a third writes and records nothing."""
        self.transfer()
        backend = self.live()
        self.collect(backend, 'youtube', 'naver_datalab')
        with patch.object(OracleExecution, 'close_released_account', side_effect=RuntimeError('interrupted')), self.assertRaises(RuntimeError):
            release_writer(self.repo, 'youtube', self.registry)
        self.assertEqual(writer_status(self.repo, now=self.now)['sources']['youtube']['writer'], 'RELEASING')
        self.assertEqual(oracle_live_readiness(self.repo, self.registry, now=self.now)['sources']['youtube']['blockers'], ['WRITER_RELEASE_IN_PROGRESS'])
        with self.assertRaises(FoodError) as reopened:
            transfer_writer(self.repo, 'youtube', self.registry, now=self.now)  # no new transfer while a release is open
        self.assertEqual(reopened.exception.status, 409)
        first = release_writer(self.repo, 'youtube', self.registry)
        events, account = self.events('youtube'), self.rows('quota_accounts', source_id='youtube')[0]
        second = release_writer(self.repo, 'youtube', self.registry)
        self.assertEqual((first['status'], first['unchanged'], second['status'], second['unchanged']), ('RELEASED', False, 'RELEASED', True))
        self.assertEqual((self.events('youtube'), self.rows('quota_accounts', source_id='youtube')[0]), (events, account))
        self.assertEqual(events, [(1, 'TRANSFER'), (1, 'RELEASE_REQUESTED'), (1, 'RELEASED')])
        real, attempts = usage_handover._insert, []

        def failing(db, source, row):
            attempts.append(row['key'])
            if len(attempts) == 2:
                raise sqlite3.OperationalError('disk I/O error')
            real(db, source, row)
        with patch.object(usage_handover, '_insert', failing), self.assertRaises(sqlite3.OperationalError):
            handover(self.repo, 'youtube', self.legacy.path, now=self.now)
        oracle_rows = "SELECT count(*) FROM youtube_calls WHERE run_id LIKE 'oracle:%'"
        self.assertEqual(self.legacy_rows(oracle_rows), [(0,)])
        self.assertEqual(handover(self.repo, 'youtube', self.legacy.path, now=self.now)['handed_over'], 2)
        after = self.events('youtube')
        again = handover(self.repo, 'youtube', self.legacy.path, now=self.now)
        self.assertEqual((again['handed_over'], again['already_present'], self.events('youtube')), (0, 2, after))
        self.assertEqual(after[-1], (1, 'USAGE_HANDOVER'))
        self.assertEqual(self.legacy_rows(oracle_rows), [(2,)])
        self.assertEqual(self.rows('quota_accounts', source_id='naver_datalab')[0]['usage_status'], 'EXCLUSIVE')  # another writer untouched

    # RSS: no two collectors of the feed

    def test_rss_release_waits_for_a_run_in_flight_and_then_nothing_fetches_the_feed(self):
        """An RSS run another process has in flight holds the release (DRAINING) while every new fetch is refused; after the release
        the gate, the schedule and even a run start under the RSS lock refuse, and the legacy scheduler stays off until the Oracle
        runs are recovered into SQLite (their numbers would collide)."""
        self.transfer()
        backend = self.live()
        self.assertTrue(backend.rss.collect()['success'])
        in_flight = backend.rss.storage.start('live_api')
        held = release_writer(self.repo, 'google_trends_rss', self.registry)
        self.assertEqual((held['status'], held['in_flight']), ('DRAINING', {'rss_runs': [in_flight]}))
        fetches = self.wire.calls.count('rss')
        refused = backend.rss.collect()
        self.assertEqual((refused['accepted'], refused['error'], refused['blockers']), (False, 'oracle_live_rss_blocked', ['WRITER_RELEASE_IN_PROGRESS']))
        backend.rss.storage.finish(in_flight, error='fetch failed')  # the other process's run ends with its own result
        self.assertEqual(release_writer(self.repo, 'google_trends_rss', self.registry)['status'], 'RELEASED')
        self.assertEqual(backend.rss.collect()['blockers'], ['WRITER_RELEASED', 'LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED'])
        with self.assertRaises(FoodError):
            backend.rss.start()
        with self.assertRaises(Blocked) as blocked:
            backend.rss.storage.start('live_api')
        self.assertEqual(blocked.exception.args[0], ['WRITER_RELEASED', 'LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED'])
        self.assertEqual(self.wire.calls.count('rss'), fetches)
        check = resume_readiness(self.repo, 'google_trends_rss', self.legacy.path, now=self.now)
        self.assertEqual((check['ready'], check['blockers'], check['legacy_switch']), (False, ['ORACLE_RSS_NOT_RECOVERED'], 'sqlite_server_rss_scheduler'))

    # E. Recovery: the whole way back

    def test_released_sources_return_to_sqlite_with_their_usage_and_their_data(self):
        """Every source released, its usage handed over and the Oracle data recovered with the existing reverse recovery into the
        same SQLite file: converged, consistent, a re-run writes nothing, and only then may each legacy collector resume."""
        self.transfer()
        backend = self.live()
        self.assertTrue(backend.rss.collect()['success'])
        self.collect(backend, *API_SOURCES)
        backend.execution.release()  # this runtime process stops; the release closes what it left
        for source in SCOPES:
            self.assertEqual(release_writer(self.repo, source, self.registry)['status'], 'RELEASED')
        self.assertEqual({s: resume_readiness(self.repo, s, self.legacy.path, now=self.now)['blockers'] for s in SCOPES},
                         {'instagram_graph': ['ORACLE_USAGE_NOT_HANDED_OVER'], 'youtube': ['ORACLE_USAGE_NOT_HANDED_OVER'],
                          'naver_datalab': ['ORACLE_USAGE_NOT_HANDED_OVER'], 'google_trends_rss': ['ORACLE_RSS_NOT_RECOVERED']})
        for source in SCOPES:
            handover(self.repo, source, self.legacy.path, now=self.now)
        self.assertEqual(RuntimeOwnership(self.repo).status()['mode'], 'IDLE')
        recovered = recover(self.legacy.path, self.repo, now=self.now)
        self.assertTrue(recovered['converged'], recovered['plan_after']['summary'])
        self.assertEqual(recovered['plan_after']['not_recovered']['writer_transfer_events']['rows'], len(SCOPES) * 3 + len(API_SOURCES))
        self.assertEqual({s: resume_readiness(self.repo, s, self.legacy.path, now=self.now)['ready'] for s in SCOPES}, dict.fromkeys(SCOPES, True))
        with closing(sqlite3.connect(self.legacy.path)) as db:
            self.assertEqual((db.execute('PRAGMA integrity_check').fetchone()[0], db.execute('PRAGMA foreign_key_check').fetchall()), ('ok', []))
            self.assertEqual(db.execute("SELECT count(*) FROM youtube_calls WHERE run_id LIKE 'oracle:%'").fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM execution_calls').fetchone()[0], self.repo.count('execution_calls'))
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], self.repo.count('rss_runs'))
        again = recover(self.legacy.path, self.repo, now=self.now)
        self.assertEqual(again['writes'], {'inserted': 0, 'content_rows': 0, 'updated': 0, 'ignored': 0})
        self.assertEqual({s: handover(self.repo, s, self.legacy.path, now=self.now)['handed_over'] for s in API_SOURCES}, dict.fromkeys(API_SOURCES, 0))

    def test_recovery_after_the_handover_still_applies_the_oracle_periods_updates(self):
        """The documented order (release, handover, recovery) on the file a B7 load read: the handover changes the file, yet the
        recovery may still apply the Oracle period's mutable updates, because each handover records the file before and after its
        rows. An RSS item the legacy collector stored, loaded and the runtime observed again comes back updated (found by the
        isolated rehearsal of the operational database); a file anyone else changed stays unauthorized."""
        self.now = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)  # after the legacy collection below
        CollectionService(self.legacy, fetcher=lambda _: (feed(COOKIE), 200)).collect()
        with closing(sqlite3.connect(self.legacy.path)) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        migrate(self.legacy.path, self.repo, now=self.now)  # the B7 load records the file it read
        self.transfer()
        backend = self.live()
        self.assertTrue(backend.rss.collect()['success'])  # the wire's feed carries the same item again
        self.collect(backend, 'youtube')
        backend.execution.release()
        for source in SCOPES:
            self.assertEqual(release_writer(self.repo, source, self.registry)['status'], 'RELEASED')
        self.assertEqual(handover(self.repo, 'youtube', self.legacy.path, now=self.now)['handed_over'], 2)  # the file changes here
        [event] = [e for e in self.rows('writer_transfer_events', source_id='youtube') if e['action'] == 'USAGE_HANDOVER']
        step = json.loads(event['details_json'])['destination_fingerprint']
        self.assertTrue(step['before'] and step['after'] and step['before'] != step['after'])
        elsewhere = Path(self.temp.name) / 'changed-elsewhere.sqlite3'  # the same file after another writer's change
        shutil.copyfile(self.legacy.path, elsewhere)
        with closing(sqlite3.connect(elsewhere)) as db:
            db.execute("UPDATE items SET last_seen=? WHERE id=1", (stamp(self.now),))
            db.commit()
        self.assertFalse(Recovery(elsewhere, self.repo, self.now).baseline()['verified'])
        base = Recovery(self.legacy.path, self.repo, self.now).baseline()
        self.assertEqual((base['verified'], base['matches_completed_load'], base['matches_recorded_handover']), (True, False, True))
        result = recover(self.legacy.path, self.repo, now=self.now, apply_updates=True)
        self.assertGreater(result['plan_before']['summary']['mutable_updates'], 0)
        self.assertEqual((result['updates_applied'], result['converged'], result['plan_after']['summary']['mutable_updates']), (True, True, 0))
        self.assertTrue(resume_readiness(self.repo, 'youtube', self.legacy.path, now=self.now)['ready'])

    # Unapproved and production access

    def test_unapproved_production_and_operational_access_is_refused(self):
        with self.assertRaises(FoodError) as never:
            release_writer(self.repo, 'youtube', self.registry)  # never transferred
        self.assertEqual(never.exception.status, 409)
        self.transfer()
        with self.assertRaises(FoodError) as ungranted:
            release_writer(self.repo, 'youtube', self.grants('naver_datalab'))
        self.assertEqual(ungranted.exception.status, 403)
        with RuntimeOwnership(self.repo).held('SHADOW'), self.assertRaises(OwnershipRefused):
            release_writer(self.repo, 'youtube', self.registry)
        self.assertEqual(self.rows('quota_accounts', source_id='youtube')[0]['usage_status'], 'EXCLUSIVE')  # nothing changed

        class Production(SQLiteTarget):
            settings = type('S', (), {'environment': 'production'})()
        production = TargetRepository(Production(Path(self.temp.name) / 'production.sqlite3'))
        production.migrate()
        with self.assertRaises(FoodError) as locked:
            release_writer(production, 'youtube', self.registry)
        self.assertEqual(locked.exception.status, 403)
        with self.assertRaises(OwnershipRefused) as target_refused:
            handover(production, 'youtube', self.legacy.path, now=self.now)
        self.assertEqual(target_refused.exception.code, 'production_target_requires_approval')
        operational = Path(self.temp.name) / 'trends.sqlite3'  # the operational file's name: refused without the approved switch
        shutil.copyfile(self.legacy.path, operational)
        with self.assertRaises(RecoveryRefused) as file_refused:
            handover(self.repo, 'youtube', operational, now=self.now)
        self.assertEqual(file_refused.exception.code, 'operational_database_requires_approval')
        with self.assertRaises(FoodError) as busy:
            handover(self.repo, 'youtube', self.legacy.path, now=self.now)  # the runtime is still the writer
        self.assertEqual(busy.exception.status, 409)
        self.assertEqual(self.legacy_rows("SELECT count(*) FROM youtube_calls"), [(0,)])

        def run(*argv):
            stderr = io.StringIO()
            with patch('sys.argv', ['food_trends', *argv]), patch.dict(os.environ, {'ORACLE_TEST_DSN': ''}), \
                    contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as stop:
                main()
            return stop.exception.code, stderr.getvalue()
        for argv, expected in ((('--backend', 'oracle-production', '--writer-release', 'youtube', '--execution-registry', self.registry),
                                'oracle_production_runtime_disabled'),
                               (('--backend', 'oracle-test', '--writer-release', 'youtube'), '--execution-registry'),
                               (('--backend', 'oracle-test', '--release-wait', '5'), '--writer-release'),
                               (('--backend', 'oracle-test', '--writer-status', '--maintenance'), '한 번에 하나만'),
                               (('--writer-release', 'youtube'), '--backend oracle-test'),
                               (('--writer-handover', 'youtube'), '--recovery-target'),
                               (('--legacy-resume-check', 'youtube', '--recovery', 'plan'), '한 번에 하나만'),
                               (('--backend', 'oracle-test', '--writer-handover', 'youtube'), '--writer-handover')):
            code, message = run(*argv)
            self.assertEqual((code, expected in message), (2, True), argv)
        self.assertEqual(self.wire.calls, [])


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class WriterReleaseOracleTests(WriterReleaseStandInTests):
    """Every case on the real disposable Oracle test schema (row locks instead of the stand-in's serialized transactions)."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db


if __name__ == '__main__':
    unittest.main()
