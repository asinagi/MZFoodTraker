"""ORACLE-BACKEND-COMPLETION-1 Phase A: Google Trends RSS discovery and candidate import on the target alone.

The existing collector and classifier, fed Mock responses (no HTTP), write runs, items, classifications and
observations to the target; B4 stages each run in its own transaction, FOOD/REVIEW items become review candidates
and B5 links them, while every other sqlite3 connection is refused. The same feeds through the SQLite
CollectionService give the same item ids, classifications, candidate keys, B4 keys and status. Re-runs and duplicate
entries add nothing, brand names are never approved as foods, a run without a FOOD item is a normal result, failures
are recorded as failures, live fetching is refused, and run/item numbers continue after a B7-loaded SQLite period.
The stand-in runs by default; the Oracle subclass repeats every case on the disposable MZ_TEST schema.
"""
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from xml.sax.saxutils import escape
from food_trends.food_service import FoodError, FoodService
from food_trends.ingestion import item_key
from food_trends.oracle_foods import OracleFoodService
from food_trends.runtime_ownership import OwnershipRefused, RuntimeOwnership
from food_trends.runtime_storage import oracle_backend
from food_trends.service import CollectionService
from food_trends.storage import Storage
from food_trends.target_db import SQLiteTarget
from food_trends.target_migration import migrate
from food_trends.target_repository import TargetRepository
from food_trends.tracking_access import mock_accounts
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema
import test_oracle_backend as backend_tests
from test_oracle_backend import Feed

NOW = datetime(2026, 11, 2, 3, tzinfo=timezone.utc)
DATE = 'Tue, 27 Oct 2026 10:00:00 +0900'
COOKIE = ('두바이 쫀득쿠키', ['두바이 쫀득쿠키 디저트 맛집 인기'])  # FOOD / FOOD_PRODUCT
BAGEL = ('성수 베이글', ['성수 베이글 맛 후기 줄서는'])  # REVIEW / UNKNOWN
BRAND = ('노티드', ['노티드, 디저트 신메뉴 출시'])  # FOOD / FOOD_BRAND -> a BRAND candidate behind the type gate
PERSON = ('흑백요리사 셰프', ['흑백요리사 셰프 맛집 방문'])  # NON_FOOD / PERSON
ASSEMBLY = ('국회 본회의', ['국회 본회의 개최'])  # NON_FOOD
# Values that depend on the wall clock (the SQLite store) or the storage label, dropped before comparing the stores.
VOLATILE = {'first_seen', 'last_seen', 'keyword_first_seen', 'keyword_last_seen', 'classified_at', 'collected_at', 'started_at',
            'finished_at', 'last_success_at', 'last_discovery_at', 'feed_age_seconds', 'storage', 'live_enabled', 'candidate_id',
            'created_at', 'updated_at', 'source_item_id', 'source_item_key', 'source_collected_at'}


def feed(*entries):
    """entries: (keyword, [news titles]) or (keyword, [news titles], pubDate); the first entry carries a traffic value."""
    items = []
    for n, entry in enumerate(entries):
        keyword, news, date = (entry + (DATE,))[:3]
        items.append('<item><title>%s</title><pubDate>%s</pubDate>%s%s</item>' % (
            escape(keyword), date, '<ht:approx_traffic>1000+</ht:approx_traffic>' if n == 0 else '',
            ''.join('<ht:news_item><ht:news_item_title>%s</ht:news_item_title></ht:news_item>' % escape(t) for t in news)))
    return ('<rss xmlns:ht="https://trends.google.com/trending/rss"><channel>%s</channel></rss>' % ''.join(items)).encode()


def stable(value):
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


class OracleRssStandInTests(unittest.TestCase):
    sqlite_refused = backend_tests.OracleBackendStandInTests.sqlite_refused

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.target = self.make_target()
        self.repo = TargetRepository(self.target, clock=lambda: self.now)
        self.repo.migrate()
        self.xml, self.fetches = feed(COOKIE), []
        self.backend = self.assemble()

    def make_target(self):
        return SQLiteTarget(Path(self.temp.name) / 'target.sqlite3')

    def assemble(self, fetcher='mock'):
        backend = oracle_backend(self.repo, mock_accounts(), lambda: self.now, Feed(), rss_fetcher=self.fetch if fetcher == 'mock' else fetcher)
        self.addCleanup(backend.execution.release)
        return backend

    def fetch(self, timeout):
        self.fetches.append(timeout)
        if isinstance(self.xml, Exception):
            raise self.xml
        return self.xml, 200

    def collect(self, *entries):
        self.xml = feed(*entries) if entries else self.xml
        return self.backend.rss.collect()

    def rows(self, table, **where):
        return [r for r in self.repo.rows(table) if all(r[k] == v for k, v in where.items())]

    def review(self, candidate, **body):
        return self.backend.foods.command('review', dict(request_key=uuid.uuid4().hex, candidate_id=candidate['candidate_id'],
                                                         expected_revision=candidate['revision'], approved=True, actor_ref='reviewer',
                                                         reason='후보 검토', **body))

    def sqlite_collect(self, storage, *feeds):
        results = []
        for entries in feeds:
            results.append(CollectionService(storage, fetcher=lambda _, x=feed(*entries): (x, 200)).collect())
        return results

    # Phase A

    def test_collection_writes_the_target_alone_and_matches_the_sqlite_store(self):
        feeds = [(COOKIE, BAGEL, BRAND, ASSEMBLY), (COOKIE, PERSON, (COOKIE[0], COOKIE[1], 'Wed, 28 Oct 2026 09:00:00 +0900'))]
        with self.sqlite_refused() as opened:
            results = []
            for entries in feeds:
                results.append(self.collect(*entries))
                self.now += timedelta(minutes=30)  # the SQLite store stamps each run with the wall clock: keep the run order
            status, trends, candidates = self.backend.rss.status(), self.backend.rss.storage.trends(), self.backend.foods.candidates()
            detail = self.backend.rss.storage.trends(item_id=1)
            exported = self.backend.rss.storage.export()
        self.assertEqual(opened, [])
        storage = Storage.create(Path(self.temp.name) / 'sqlite-rss.sqlite3')
        expected = self.sqlite_collect(storage, *feeds)
        sqlite_status = CollectionService(storage).status()
        # Collection summaries, status, list and detail rows, and the export are the SQLite store's, value for value.
        self.assertEqual([stable(r) for r in results], [stable(r) for r in expected])
        self.assertEqual(stable(dict(status)), stable(sqlite_status))
        self.assertEqual(stable(trends), stable(storage.trends()))
        self.assertEqual(stable(detail), stable(storage.trends(item_id=1)))
        self.assertIn('raw_xml', detail[0])
        self.assertFalse([r for r in trends if 'raw_xml' in r])
        sqlite_export = storage.export()
        for table in ('items', 'candidates', 'classification_history'):
            self.assertEqual(len(exported[table]), len(sqlite_export[table]), table)
        self.assertEqual([r['id'] for r in exported['runs']], [r['id'] for r in sqlite_export['runs']])
        self.assertTrue(all(r['raw_response_base64'] is None for r in exported['runs']))  # the raw body is not kept on the target
        # Candidates: same keys, kinds, gates and review states; each refers to its item by the item key B7 would load.
        with storage.reader() as db:
            source_ids = dict(db.execute('SELECT id,source_id FROM items').fetchall())
            staged = {tuple(r) for r in db.execute('SELECT ingest_run_id,item_key,snapshot_id FROM run_items')}
            metrics = db.execute('SELECT count(*) FROM metric_observations').fetchone()[0]
        mine = FoodService(storage).candidates()
        self.assertEqual(sorted((c['candidate_key'], c['proposed_kind'], c['type_gate'], c['status'], c['origin']) for c in candidates),
                         sorted((c['candidate_key'], c['proposed_kind'], c['type_gate'], c['status'], c['origin']) for c in mine))
        self.assertEqual(sorted(c['source_item_key'] for c in candidates),
                         sorted(item_key('google_trends_rss', source_ids[c['source_item_id']]) for c in mine))
        self.assertEqual(stable(sorted(candidates, key=lambda c: c['candidate_key'])), stable(sorted(mine, key=lambda c: c['candidate_key'])))
        # B4 staging and B5 metrics: the same deterministic keys as the SQLite path, so a B7 load and this runtime agree.
        self.assertEqual({(r['ingest_run_id'], r['item_key'], r['snapshot_id']) for r in self.repo.rows('run_items')}, staged)
        self.assertEqual(self.repo.count('metric_observations'), metrics)
        self.assertEqual([r['run_id'] for r in self.repo.rows('rss_runs')], [1, 2])
        self.assertEqual(self.fetches, [20, 20])

    def test_reruns_and_duplicate_entries_add_nothing(self):
        first = self.collect(COOKIE, BAGEL, COOKIE)  # the same entry twice in one response is one item
        self.assertEqual((first['item_count'], first['new_count'], first['entity_resolution']), (3, 2, {'state': 'completed', 'candidates': 2}))
        counts = {t: self.repo.count(t) for t in ('source_contents', 'rss_classifications', 'food_candidates', 'food_commands', 'item_snapshots',
                                                    'food_mentions')}
        second = self.collect(COOKIE, BAGEL)
        self.assertEqual((second['success'], second['new_count'], second['entity_resolution']['candidates']), (True, 0, 2))
        self.assertEqual({t: self.repo.count(t) for t in counts}, dict(counts, food_commands=counts['food_commands'] + 2))
        self.assertEqual(self.repo.count('rss_observations'), 4)  # one observation per run and item
        self.assertEqual(self.repo.count('ingest_runs'), 2)
        self.assertEqual(sorted(r['payload_ref'] for r in self.repo.rows('source_contents')), ['items:1', 'items:2'])
        # Replaying a run's bridge is a no-op; the same keyword published again is a new item counted once per publication.
        self.assertEqual(self.backend.foods.import_rss_run(1), {'state': 'completed', 'candidates': 2})
        self.assertEqual(self.repo.count('food_candidates'), 2)
        self.collect((COOKIE[0], COOKIE[1], 'Wed, 28 Oct 2026 09:00:00 +0900'))
        rows = [r for r in self.backend.rss.storage.trends() if r['keyword'] == COOKIE[0]]
        self.assertEqual(([r['id'] for r in rows], {r['occurrence_count'] for r in rows}), ([3, 1], {2}))
        self.assertEqual(self.backend.controller.recover_rss(), {'results': [], 'http_requests': 0})

    def test_brands_are_not_approved_as_foods_and_a_run_without_food_is_normal(self):
        result = self.collect(BRAND, PERSON, ASSEMBLY)
        self.assertEqual((result['success'], result['food_count'], result['review_count']), (True, 1, 0))
        [brand] = self.backend.foods.candidates()
        self.assertEqual((brand['surface_form'], brand['proposed_kind'], brand['type_gate'], brand['status'], brand['resolved_food_id']),
                         ('노티드', 'BRAND', 'BLOCKED_NON_FOOD', 'NEEDS_REVIEW', None))
        with self.assertRaises(FoodError):
            self.review(brand, action='create', canonical_name='노티드', entity_kind='PRODUCT')
        with self.assertRaises(FoodError):
            self.review(brand, action='create', canonical_name='노티드')
        self.assertEqual([self.repo.count(t) for t in ('food_entities', 'food_aliases', 'tracking_targets', 'food_mentions')], [0, 0, 0, 0])
        rejected = self.review(brand, action='reject')
        self.assertEqual(rejected['status'], 'REJECTED')
        self.assertEqual(self.backend.foods.import_rss_run(1)['candidates'], 1)
        self.assertEqual(self.backend.foods.candidate(brand['candidate_id'])['status'], 'REJECTED')  # a re-import never revives it
        # No FOOD item at all: a successful, observed run with zero candidates, not a failure.
        none = self.collect(PERSON, ASSEMBLY)
        self.assertEqual((none['success'], none['new_count'], none['food_count'], none['entity_resolution']),
                         (True, 0, 0, {'state': 'completed', 'candidates': 0}))
        latest = self.backend.rss.status()['latest_run']
        self.assertEqual((latest['discovery_state'], latest['current_food_count'], latest['current_review_count']), ('NO_NEW_ITEMS', 0, 0))
        self.assertEqual(self.rows('ingest_runs', run_ref='runs:2')[0]['coverage'], 'COMPLETE')

    def test_an_approved_alias_resolves_new_candidates_and_links_mentions(self):
        submitted = self.backend.foods.command('submit', dict(request_key=uuid.uuid4().hex, name='두쫀쿠', aliases=['두바이 쫀득쿠키']))
        food = self.review(submitted, action='create')['resolution']
        result = self.collect(COOKIE, BAGEL)
        self.assertEqual(result['ingestion']['linking']['mentions'], 2)  # the keyword and the news title of one item
        by_name = {c['surface_form']: c for c in self.backend.foods.candidates() if c['source_code'] == 'google_trends_rss'}
        self.assertEqual((by_name['두바이 쫀득쿠키']['status'], by_name['두바이 쫀득쿠키']['resolved_food_id'],
                          by_name['두바이 쫀득쿠키']['resolution_method']), ('RESOLVED', food['food_id'], 'APPROVED_EXACT_ALIAS'))
        self.assertEqual((by_name['성수 베이글']['status'], by_name['성수 베이글']['proposed_kind']), ('NEEDS_REVIEW', 'UNKNOWN'))
        mentions = self.repo.rows('food_mentions')
        self.assertEqual({(m['food_id'], m['source_code'], m['item_key']) for m in mentions},
                         {(food['food_id'], 'google_trends_rss', by_name['두바이 쫀득쿠키']['source_item_key'])})
        self.assertEqual(sorted(m['field_code'] for m in mentions), ['keyword', 'news_title:0'])
        self.assertEqual({r['food_id'] for r in self.repo.rows('food_entities')}, {food['food_id']})  # no food was created by collection

    def test_failures_are_recorded_as_failures_and_live_fetching_is_refused(self):
        self.xml = URLError('fixture outage')
        failed = self.collect()
        self.assertEqual((failed['accepted'], failed['success'], failed['run_id']), (True, False, 1))
        self.assertIn('URLError', failed['error'])
        run = self.rows('rss_runs', run_id=1)[0]
        self.assertEqual((run['success'], run['item_count'], run['food_count']), (0, 0, None))
        [staged] = self.rows('ingest_runs', run_ref='runs:1')
        self.assertEqual((staged['execution_status'], staged['coverage'], staged['staged_items']), ('FAILED', 'NONE', 0))
        self.assertEqual(self.repo.count('metric_observations'), 0)  # a failed run is not observed, never a measured zero
        self.assertEqual(self.backend.rss.status()['latest_run']['discovery_state'], 'COLLECTION_FAILED')
        self.xml = b'<rss><channel><item><title></title></item></channel></rss>'
        self.assertFalse(self.collect()['success'])  # a malformed response is a failed run as well
        self.assertEqual(self.backend.controller.recover_rss()['results'], [])
        # Without an injected Mock fetcher the target collects nothing: no run row, no request.
        live = self.assemble(fetcher=None).rss
        before = self.repo.count('rss_runs')
        refused = live.collect()
        self.assertEqual((refused['accepted'], refused['error']), (False, 'oracle_live_collection_disabled'))
        self.assertEqual(self.repo.count('rss_runs'), before)

    def test_reclassification_keeps_history_and_the_bridge_retries_locally(self):
        with patch.object(OracleFoodService, 'import_rss_run', side_effect=RuntimeError('fixture bridge failure')):
            result = self.collect(BAGEL)
        self.assertEqual(result['entity_resolution'], {'state': 'pending_retry', 'run_id': 1})
        self.assertEqual(self.repo.count('food_candidates'), 0)
        self.assertEqual(self.backend.controller.recover_rss()['results'], [{'run_id': 1, 'state': 'completed', 'candidates': 1}])
        self.assertEqual(self.backend.controller.recover_rss()['results'], [])
        [candidate] = self.backend.foods.candidates()
        self.assertEqual(candidate['proposed_kind'], 'UNKNOWN')
        # The same item (same title and date) with new news becomes FOOD: history keeps the previous result, the candidate is retyped.
        self.collect((BAGEL[0], ['성수 베이글 디저트 맛집 인기']))
        [history] = self.repo.rows('rss_classification_history')
        self.assertTrue(history['history_key'].startswith('CLSH_'))
        self.assertEqual(self.backend.rss.storage.trends()[0]['classification'], 'FOOD')
        retyped = self.backend.foods.candidate(candidate['candidate_id'])
        self.assertEqual((retyped['proposed_kind'], retyped['type_gate'], retyped['status']), ('PRODUCT', 'FOOD', 'NEEDS_REVIEW'))
        self.assertIn('SOURCE_TYPE_CHANGED', [e['action'] for e in self.backend.foods.events(candidate['candidate_id'])])

    def test_runs_and_items_continue_after_a_loaded_sqlite_period_and_shadow_blocks_collection(self):
        path = Path(self.temp.name) / 'sqlite-period.sqlite3'
        storage = Storage.create(path)
        self.sqlite_collect(storage, (COOKIE, BAGEL), (COOKIE, ASSEMBLY))
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.backend.execution.release()
        migrate(path, self.repo, now=self.now)
        loaded = {t: self.repo.count(t) for t in ('food_candidates', 'source_contents', 'rss_classifications', 'ingest_runs')}
        backend = self.backend = self.assemble()
        with self.sqlite_refused() as opened:
            result = self.collect(COOKIE, PERSON)
        self.assertEqual(opened, [])
        self.assertEqual((result['run_id'], result['new_count']), (3, 1))  # SQLite runs 1-2 were loaded as runs:1, runs:2
        self.assertEqual(backend.rss.storage.trends(item_id=1)[0]['keyword'], COOKIE[0])  # the loaded item keeps its id
        self.assertEqual(sorted(r['payload_ref'] for r in self.repo.rows('source_contents')), ['items:1', 'items:2', 'items:3', 'items:4'])
        self.assertEqual(self.repo.count('food_candidates'), loaded['food_candidates'])  # the loaded candidate is found again, not duplicated
        self.assertEqual(self.repo.count('ingest_runs'), loaded['ingest_runs'] + 1)
        # Shadow (or a B7 load) owns the target: RSS collection writes nothing.
        backend.execution.release()
        before = {t: self.repo.count(t) for t in ('rss_runs', 'source_contents', 'rss_observations')}
        with RuntimeOwnership(self.repo).held('SHADOW'):
            blocked = self.assemble().rss.collect()
        self.assertEqual((blocked['success'], blocked['run_id']), (False, None))
        self.assertIn(OwnershipRefused.__name__, blocked['error'])
        self.assertEqual({t: self.repo.count(t) for t in before}, before)


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleRssTests(OracleRssStandInTests):
    """Every case on the real disposable Oracle test schema; no sqlite3 connection during the Oracle flows."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db


if __name__ == '__main__':
    unittest.main()
