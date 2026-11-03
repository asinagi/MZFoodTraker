"""ORACLE-RUNTIME-1 S4: B5 food observations on the target, and SQLite/Oracle cross-database checks (21-28).

The same mock runs (S2/S3 harness) feed both pipelines; B5 tables, entity-change effects and the
FOOD_ID view are compared as logical values. Food and alias decisions are made on the SQLite system of
record and copied to the target with B7's own row derivation, as Shadow would deliver them.
"""
import json
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from food_trends.food_observations import FoodObservationService
from food_trends.ingestion import IngestionService
from food_trends.oracle_execution import OracleExecution
from food_trends.oracle_ingestion import OracleIngestionService
from food_trends.oracle_observations import OracleObservationService
from food_trends.service import CollectionService
from food_trends.target_db import PolicyRefused, logical
from food_trends.target_migration import Plan, integrity_checks, migrate, open_source
from food_trends.target_schema import BY_NAME
from food_trends.tracking_service import instant
import test_oracle_execution as s2
import test_oracle_ingestion as s3
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema
from test_food_observations import rss_xml

LINKS = ('item_query_matches', 'food_mentions', 'metric_observations')
EFFECTS = ('aggregate_invalidations', 'entity_change_effects')
RUN_SPECIFIC = dict(s3.RUN_SPECIFIC, item_query_matches=('match_id', 'ingest_run_id', 'evidence_ref'),
                    food_mentions=('mention_id', 'source_code'), metric_observations=('observation_id', 'observation_key', 'ingest_run_id'),
                    aggregate_invalidations=('invalidation_id',), entity_change_effects=('effect_id',))
FOOD_TABLES = ('food_entities', 'food_aliases', 'food_relations', 'food_candidates', 'food_events', 'food_commands')
NATURAL_KEYS = {'source_items': ('source_code', 'external_key'), 'item_snapshots': ('item_key', 'payload_hash', 'adapter_version'),
                'run_items': ('ingest_run_id', 'item_key'), 'quality_results': ('event_key',),
                'item_query_matches': ('ingest_run_id', 'item_key', 'query_ref'), 'food_mentions': ('mention_key',),
                'metric_observations': ('observation_key',), 'execution_item_receipts': ('job_id', 'item_id'),
                'execution_calls': ('job_id', 'attempt_no', 'step_key'), 'ingest_runs': ('source_code', 'run_ref')}


class InstagramEmpty(s2.MockTransport):
    """The hashtag resolves but has no recent media: an observed zero."""
    def __call__(self, request, timeout=None):
        if urlsplit(request.full_url).path.endswith('recent_media'):
            self.calls.append(request.full_url)
            return s2.MockResponse(b'{"data": [], "paging": {}}')
        return super().__call__(request, timeout)


class OracleObservationStandInTests(unittest.TestCase):
    setUp_base = s3.OracleIngestionStandInTests.setUp_base
    tearDown = s3.OracleIngestionStandInTests.tearDown
    make_target = s3.OracleIngestionStandInTests.make_target
    create_food = s3.OracleIngestionStandInTests.create_food
    cmd = s3.OracleIngestionStandInTests.cmd
    register = s3.OracleIngestionStandInTests.register
    active = s3.OracleIngestionStandInTests.active
    change = s3.OracleIngestionStandInTests.change
    advance = s3.OracleIngestionStandInTests.advance
    plans = s3.OracleIngestionStandInTests.plans
    plan = s3.OracleIngestionStandInTests.plan
    oracle = s3.OracleIngestionStandInTests.oracle
    sqlite = s3.OracleIngestionStandInTests.sqlite
    project = s3.OracleIngestionStandInTests.project
    parity = s3.OracleIngestionStandInTests.parity
    run_both = s3.OracleIngestionStandInTests.run_both

    def setUp(self):
        s3.OracleIngestionStandInTests.setUp(self)

    # Helpers

    def target_execution(self):
        return OracleExecution(self.repo, self.tracking.accounts, lambda: self.now)

    def services(self):
        execution = self.target_execution()
        return (FoodObservationService(self.storage, lambda: self.now), OracleObservationService(execution),
                IngestionService(self.storage, lambda: self.now), OracleIngestionService(execution))

    def rows(self, table, side, drop=()):
        if side == 'sqlite':
            with self.storage.reader() as db:
                rows = [logical(table, dict(r)) for r in db.execute('SELECT * FROM %s' % table)]
        else:
            rows = self.repo.rows(table)
        skip = set(RUN_SPECIFIC.get(table, ())) | set(drop)
        return sorted(json.dumps({k: v for k, v in r.items() if k not in skip}, sort_keys=True, ensure_ascii=False) for r in rows)

    def assertTablesEqual(self, tables, drop=()):
        for table in tables:
            with self.subTest(table=table):
                self.assertEqual(self.rows(table, 'sqlite', drop), self.rows(table, 'target', drop))

    def sync_foods(self):
        """Food decisions reach the target as Shadow delivers them: B7 row derivation + idempotent upserts."""
        with closing(open_source(self.storage.path)) as src:
            plan = Plan(src, self.now)
            with self.repo.db.transaction() as tx:
                for table in FOOD_TABLES:
                    for row in plan._source_rows(table):
                        self.repo.upsert(tx, table, row)

    def views(self):
        sqlite, oracle, _, _ = self.services()
        foods = [r['food_id'] for r in self.repo.rows('food_entities')]
        return {f: sqlite.food_view(f) for f in foods}, {f: oracle.food_view(f) for f in foods}

    def assertViewsEqual(self):
        left, right = self.views()
        self.assertEqual(json.loads(json.dumps(left, default=str)), json.loads(json.dumps(right, default=str)))

    def snapshot(self):
        return {t.name: self.rows(t.name, 'target') for t in BY_NAME.values()}

    # Scenarios 15-17: search evidence, approved mentions, permitted metrics.

    def test_mock_runs_link_identically(self):
        self.run_both()
        self.assertTablesEqual(s3.STAGING + LINKS)
        self.assertViewsEqual()
        matches = self.repo.rows('item_query_matches')
        self.assertEqual({m['verification_status'] for m in matches}, {'SEARCH_ONLY'})
        mentions = self.repo.rows('food_mentions')
        self.assertEqual([(m['source_code'], m['verification_status']) for m in mentions], [('instagram_graph', 'EXACT_ALIAS_HASHTAG')])
        metrics = self.repo.rows('metric_observations')
        self.assertEqual([(m['metric_code'], m['value_state'], m['count_value']) for m in metrics], [('instagram.observed_posts', 'OBSERVED', 1)])
        view = self.views()[1][self.food['food_id']]['sources']
        self.assertEqual((view['youtube']['confirmed_items'], view['youtube']['search_matched_items'], view['youtube']['metric_storage']),
                         (0, 1, 'POLICY_BLOCKED'))
        self.assertEqual(view['naver_datalab']['metrics'], [])

    def test_rss_evidence_relinks_identically_on_the_target(self):
        CollectionService(self.storage, fetcher=lambda _: (rss_xml(('두쫀쿠', ['두바이 쫀득 쿠키 열풍, 두쫀쿠 품절']), ('두바이쫀득쿠키', [])), 200)).collect()
        migrate(self.storage.path, self.repo, now=self.now)
        with self.repo.db.transaction() as tx:
            for table in LINKS:
                tx.execute('DELETE FROM %s' % table)
        _, oracle, _, _ = self.services()
        self.assertGreaterEqual(oracle.link_pending()['linked_runs'], 1)
        self.assertTablesEqual(LINKS, drop=('created_at',))  # the collector linked on the wall clock
        self.assertEqual(len(self.repo.rows('food_mentions')), 4)
        self.assertEqual({m['source_code'] for m in self.repo.rows('food_mentions')}, {'google_trends_rss'})
        self.assertEqual(sorted(m['value_state'] for m in self.repo.rows('metric_observations')), ['NOT_PROVIDED', 'OBSERVED'])

    # Failure is NOT_OBSERVED, never a zero; an empty success is an observed zero.

    def test_failed_and_empty_instagram_runs_are_distinct(self):
        self.run_both(sources=('instagram_graph',), transport=s2.Down, rounds=3)
        self.assertTablesEqual(LINKS)
        failed = self.repo.rows('metric_observations')
        self.assertEqual([(m['value_state'], m['count_value'], m['coverage']) for m in failed], [('NOT_OBSERVED', None, 'NONE')])

    def test_empty_instagram_run_is_an_observed_zero(self):
        self.run_both(sources=('instagram_graph',), transport=InstagramEmpty)
        self.assertTablesEqual(LINKS)
        zero = self.repo.rows('metric_observations')
        self.assertEqual([(m['value_state'], m['count_value'], m['coverage']) for m in zero], [('OBSERVED', 0, 'COMPLETE')])

    # Scenario 19: entity changes and reprocessing.

    def test_alias_approval_and_revocation_reprocess_identically(self):
        CollectionService(self.storage, fetcher=lambda _: (rss_xml(('두쫀 디저트', ['두쫀 신메뉴'])), 200)).collect()
        migrate(self.storage.path, self.repo, now=self.now)
        sqlite, oracle, _, _ = self.services()
        alias = self.foods.command('alias', dict(request_key='alias-approve', food_id=self.food['food_id'], surface_form='두쫀', action='approve',
                                                 expected_revision=self.foods.food(self.food['food_id'])['revision'], approved=True,
                                                 actor_ref='reviewer', reason='별칭 추가'))
        self.sync_foods()
        left, right = sqlite.apply_entity_changes(), oracle.apply_entity_changes()
        self.assertEqual(right, left)
        self.assertEqual(sum(e['created_mentions'] for e in right), 2)
        self.foods.command('alias', dict(request_key='alias-revoke', alias_id=alias['alias_id'], action='revoke', expected_revision=alias['revision'],
                                         approved=True, actor_ref='reviewer', reason='별칭 철회'))
        self.sync_foods()
        left, right = sqlite.apply_entity_changes(), oracle.apply_entity_changes()
        self.assertEqual(right, left)
        self.assertEqual(right[-1]['invalidated_mentions'], 2)
        self.assertEqual(oracle.apply_entity_changes(), [])  # each decision is processed once
        self.assertTablesEqual(LINKS + EFFECTS, drop=('created_at',))
        self.assertViewsEqual()

    def test_merge_and_split_reprocess_identically(self):
        other = self.create_food('두바이 디저트')
        CollectionService(self.storage, fetcher=lambda _: (rss_xml(('두바이 디저트', [])), 200)).collect()
        migrate(self.storage.path, self.repo, now=self.now)
        sqlite, oracle, _, _ = self.services()
        self.assertEqual(oracle.apply_entity_changes(), sqlite.apply_entity_changes())  # pending food registrations
        self.foods.command('merge', dict(request_key='merge', food_id=other['food_id'], target_food_id=self.food['food_id'],
                                         expected_revision=self.foods.food(other['food_id'])['revision'],
                                         expected_target_revision=self.foods.food(self.food['food_id'])['revision'],
                                         approved=True, actor_ref='reviewer', reason='병합'))
        self.sync_foods()
        self.assertEqual(oracle.apply_entity_changes(), sqlite.apply_entity_changes())
        third = self.create_food('두바이 디저트 세트')
        alias = next(a for a in self.foods.food(self.food['food_id'])['approved_aliases'] if a['surface_form'] == '두바이 디저트')
        self.foods.command('alias', dict(request_key='split', alias_id=alias['alias_id'], action='reassign', food_id=third['food_id'],
                                         expected_revision=alias['revision'], approved=True, actor_ref='reviewer', reason='분리'))
        self.sync_foods()
        self.assertEqual(oracle.apply_entity_changes(), sqlite.apply_entity_changes())
        self.assertTablesEqual(LINKS + EFFECTS, drop=('created_at',))
        self.assertViewsEqual()
        self.assertTrue(oracle.food_view(other['food_id'])['redirected'])

    # Scenario 20 for links; expired evidence is never regenerated.

    def test_expired_caption_evidence_is_not_regenerated(self):
        self.run_both(sources=('instagram_graph',))
        sqlite, oracle, sqlite_ingest, oracle_ingest = self.services()
        self.now += timedelta(days=31)
        with self.storage.connect() as db:  # SQLite's analysis purge drops the caption at the window's end
            db.execute('UPDATE instagram_media SET caption=NULL,caption_purged_at=?', (self.now.isoformat(),))
        sqlite_ingest.purge()
        oracle_ingest.purge()
        self.assertEqual(oracle.purge(), sqlite.purge())
        self.assertEqual([m['status'] for m in self.repo.rows('food_mentions')], ['EXPIRED'])
        self.foods.command('alias', dict(request_key='late-alias', food_id=self.food['food_id'], surface_form='디저트', action='approve',
                                         expected_revision=self.foods.food(self.food['food_id'])['revision'], approved=True,
                                         actor_ref='reviewer', reason='늦은 별칭'))
        self.sync_foods()
        left, right = sqlite.apply_entity_changes(), oracle.apply_entity_changes()
        self.assertEqual(right, left)
        self.assertEqual((right[-1]['created_mentions'], right[-1]['not_reprocessable_items']), (0, 1))
        oracle.link_pending()
        self.assertFalse([m for m in self.repo.rows('food_mentions') if m['status'] == 'ACTIVE'])

    # Policies on every target write.

    def test_target_refuses_blocked_links_and_origin_upgrades(self):
        self.run_both(sources=('youtube',))
        _, oracle, _, _ = self.services()
        item = self.repo.rows('source_items')[0]
        mention = dict(mention_id='MENTION_x', mention_key='k' * 64, item_key=item['item_key'], source_code='youtube', snapshot_id=None,
                       food_id=self.food['food_id'], alias_id='ALIAS_x', field_code='title', span_start=0, span_end=1,
                       resolution_version='v', verification_status='EXACT_ALIAS_TEXT', origin='mock', policy_version='p', status='ACTIVE',
                       expires_at=None, created_at=self.now.isoformat(), invalidated_at=None, invalidated_by=None)
        metric = dict(observation_id='METRIC_x', observation_key='m' * 64, ingest_run_id=self.repo.rows('ingest_runs')[0]['ingest_run_id'],
                      source_code='youtube', metric_code='youtube.view_count', unit='u', subject_kind='ITEM', subject_key='s', food_id=None,
                      item_key=item['item_key'], observed_at=self.now.isoformat(), period_start=None, period_end=None, provider_period=None,
                      comparison_context_json='{}', context_hash='c' * 64, value_state='OBSERVED', count_value=5, decimal_value=None,
                      raw_value=None, coverage='COMPLETE', origin='mock', policy_version='p', expires_at=None, status='ACTIVE')
        with oracle.execution.transaction() as t:
            with self.assertRaises(PolicyRefused):
                oracle._put_mention(t, mention)
            with self.assertRaises(PolicyRefused):
                oracle._put_metric(t, metric)
            with self.assertRaises(PolicyRefused):
                oracle._put_metric(t, dict(metric, source_code='naver_datalab', metric_code='naver.relative_search_index'))
        self.assertEqual(self.repo.count('food_mentions') + self.repo.count('metric_observations'), 0)
        legacy = dict(item, first_origin='live_api')
        with self.repo.db.transaction() as tx:
            tx.execute("UPDATE source_items SET first_origin='legacy_unknown'")
        with self.assertRaises(PolicyRefused):
            with self.repo.db.transaction() as tx:
                self.repo.upsert(tx, 'source_items', legacy)
        self.assertEqual(self.repo.rows('source_items')[0]['first_origin'], 'legacy_unknown')

    # Scenarios 21-28: cross-database reconciliation of the whole pipeline.

    def test_cross_database_reconciliation(self):
        # A NAVER window across KST midnight: 15:30 UTC is 00:30 KST of the next day.
        self.now = datetime(2026, 10, 27, 15, 30, tzinfo=timezone.utc)
        CollectionService(self.storage, fetcher=lambda _: (rss_xml(('두쫀쿠', ['두쫀쿠 리뷰'])), 200)).collect()
        self.run_both(sources=('youtube', 'instagram_graph', 'naver_datalab'))
        sqlite, oracle, sqlite_ingest, oracle_ingest = self.services()
        # 21, 22, 23: per table, the same logical rows: counts, deterministic keys and values incl. NULL / 0 / NOT_OBSERVED.
        # (RSS rows are SQLite-owned and reach the target through the B7 load; the other sources ran on each side.)
        self.assertTablesEqual(s3.STAGING + LINKS)
        self.assertEqual({r['source_code'] for r in self.repo.rows('source_items')}, {'google_trends_rss', 'youtube', 'instagram_graph'})
        keys = {t: {json.dumps([r[c] for c in NATURAL_KEYS[t]]) for r in self.repo.rows(t)} for t in ('source_items', 'item_snapshots', 'food_mentions')}
        with self.storage.reader() as db:
            sqlite_keys = {t: {json.dumps([dict(r)[c] for c in NATURAL_KEYS[t]]) for r in db.execute('SELECT * FROM %s' % t)} for t in keys}
        self.assertEqual(keys, sqlite_keys)
        # 24: UTC instants and the KST daily window agree.
        with self.storage.reader() as db:
            sqlite_windows = sorted((r['resource'], instant(r['window_start']), instant(r['window_end']))
                                    for r in db.execute('SELECT * FROM quota_charges'))
        target_windows = sorted((r['resource_code'], instant(r['window_start']), instant(r['window_end'])) for r in self.repo.rows('quota_charges'))
        self.assertEqual(sqlite_windows, target_windows)
        naver = [w for w in target_windows if w[0] == 'naver.calls'][0]
        self.assertEqual((naver[1].isoformat(), naver[2].isoformat()), ('2026-10-27T15:00:00+00:00', '2026-10-28T15:00:00+00:00'))
        # 25, 28: no FK orphans, every payload_ref resolves, no policy-blocked or expired rows (B7 integrity checks).
        checks, _ = integrity_checks(self.repo, self.now)
        self.assertEqual({k: checks[k] for k in ('foreign_key_errors', 'unresolved_payload_refs', 'blocked_policy_rows', 'expired_rows_in_target')},
                         dict(foreign_key_errors=0, unresolved_payload_refs=0, blocked_policy_rows=0, expired_rows_in_target=0))
        self.assertFalse(checks.get('foreign_keys_not_enabled_validated'))
        # 26: no duplicate logical rows.
        for table, key in NATURAL_KEYS.items():
            values = [tuple(r[c] for c in key) for r in self.repo.rows(table)]
            self.assertEqual(len(values), len(set(values)), table)
        # 27: re-running every stage changes nothing (pending food registrations are processed once, first).
        self.assertEqual(oracle.apply_entity_changes(), sqlite.apply_entity_changes())
        before = self.snapshot()
        controller = self.oracle()
        self.assertIsNone(controller.run_once())
        self.assertEqual(oracle_ingest.ingest_pending()['ingested'], 0)
        oracle.link_pending()
        self.assertEqual(oracle.apply_entity_changes(), [])
        self.assertEqual(self.snapshot(), before)
        self.assertViewsEqual()


class RuntimeStorageSelectionTests(unittest.TestCase):
    """Storage modes are explicit; production stays disabled; status never creates or writes anything."""

    def test_modes(self):
        import tempfile
        from pathlib import Path
        from food_trends import runtime_storage
        from food_trends.target_db import OracleUnavailable
        self.assertIsNone(runtime_storage.select('sqlite'))
        with self.assertRaises(ValueError):
            runtime_storage.select('oracle')
        production = {'ORACLE_TEST_DSN': 'h/p', 'ORACLE_TEST_USER': 'u', 'ORACLE_TEST_PASSWORD': 'synthetic', 'ORACLE_TEST_ENVIRONMENT': 'production',
                      'ORACLE_ALLOW_PRODUCTION': 'true'}
        for mode, env, code in (('oracle-production', production, 'oracle_production_runtime_disabled'),
                                ('oracle-test', production, 'oracle_test_environment_required'),
                                ('oracle-test', dict(production, ORACLE_TEST_ENVIRONMENT='development'), 'oracle_test_environment_required'),
                                ('oracle-test', {}, 'oracle_not_configured')):
            with self.subTest(mode=mode, code=code):
                with self.assertRaises(OracleUnavailable) as error:
                    runtime_storage.select(mode, env)
                self.assertEqual(error.exception.code, code)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'absent.sqlite3'
            self.assertEqual(runtime_storage.status('sqlite', path)['ready'], False)
            self.assertFalse(path.exists())


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleObservationTests(OracleObservationStandInTests):
    """Every case above on the real disposable Oracle test schema."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db
