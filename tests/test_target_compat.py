"""B6 compatibility contract. Runs on the SQLite stand-in always and on a real Oracle test schema only
when ORACLE_TEST_DSN/USER/PASSWORD and ORACLE_TEST_ALLOW_RESET=true are set (it drops target objects).
Without that environment the Oracle class is skipped and Oracle compatibility stays NOT_VERIFIED.
"""
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from food_trends.target_db import (ConversionError, IntegrityViolation, OracleSettings, OracleTarget, PolicyRefused,
                                   SQLiteTarget, to_target)
from food_trends.target_repository import TargetRepository
from food_trends.target_schema import BY_NAME, TABLES

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
ORACLE_READY = bool(os.environ.get('ORACLE_TEST_DSN')) and os.environ.get('ORACLE_TEST_ALLOW_RESET') == 'true'


def food(food_id='FOOD_a', name='두쫀쿠', **kw):
    return dict(dict(food_id=food_id, canonical_name=name, entity_kind='PRODUCT', status='ACTIVE', revision=1, default_scope='food:ko',
                     merged_into=None, created_at='2026-10-27T00:00:00+00:00', updated_at='2026-10-27T00:00:00+00:00'), **kw)


def alias(alias_id='ALIAS_a', food_id='FOOD_a', form='두쫀쿠', **kw):
    return dict(dict(alias_id=alias_id, food_id=food_id, surface_form=form, normalized_form=form, scope='food:ko',
                     normalization_version='food-nfc-space-v1', status='ACTIVE', revision=1,
                     created_at='2026-10-27T00:00:00+00:00', updated_at='2026-10-27T00:00:00+00:00'), **kw)


def item(key='ITEM_a', source='instagram_graph', origin='legacy_unknown', **kw):
    return dict(dict(item_key=key, source_code=source, external_key='900001', key_kind='ig_media_id', published_at=None,
                     first_seen='2026-10-27T00:00:00+00:00', last_seen='2026-10-27T00:00:00+00:00', availability='AVAILABLE',
                     first_origin=origin, payload_ref='instagram_media:900001', expires_at=None), **kw)


def run(run_id='INGEST_a', source='instagram_graph'):
    return dict(ingest_run_id=run_id, source_code=source, run_ref='manual:' + run_id, origin='mock', job_id=None, query_id=None,
                contract_version='source-envelope-v1', adapter_version='a', source_schema_version='s', policy_version='p',
                ingestion_version='i', observed_at='2026-10-27T00:00:00+00:00', execution_status=None, coverage='COMPLETE',
                error_kind=None, request_context_json='{}', context_hash='h' * 64, returned_count=None, accepted_count=None,
                staged_items=0, rejected_items=0, metric_storage='STORED', ingested_at='2026-10-27T00:00:00+00:00')


def metric(source='instagram_graph', code='instagram.observed_posts', **kw):
    return dict(dict(observation_id='METRIC_a', observation_key='k' * 64, ingest_run_id='INGEST_a', source_code=source, metric_code=code,
                     unit='unique_observed_post', subject_kind='FOOD', subject_key='FOOD_a', food_id='FOOD_a', item_key=None,
                     observed_at='2026-10-27T00:00:00+00:00', period_start=None, period_end=None, provider_period=None,
                     comparison_context_json='{}', context_hash='c' * 64, value_state='OBSERVED', count_value=0, decimal_value=None,
                     raw_value=None, coverage='COMPLETE', origin='mock', policy_version='p', expires_at=None, status='ACTIVE'), **kw)


def mention(source='instagram_graph', **kw):
    return dict(dict(mention_id='MENTION_a', mention_key='m' * 64, item_key='ITEM_a', source_code=source, snapshot_id=None, food_id='FOOD_a',
                     alias_id='ALIAS_a', field_code='caption_hashtag', span_start=None, span_end=None, resolution_version='r',
                     verification_status='EXACT_ALIAS_HASHTAG', origin='mock', policy_version='p', status='ACTIVE', expires_at=None,
                     created_at='2026-10-27T00:00:00+00:00', invalidated_at=None, invalidated_by=None), **kw)


class ContractCases:
    """Shared B6 compatibility cases; subclasses provide make_db()."""

    def setUp(self):
        self.repo = TargetRepository(self.make_db(), clock=lambda: NOW)
        self.repo.migrate()

    def raw_insert(self, tx, table, row):
        """Bypass the application policy gate to prove the table constraint itself refuses."""
        binds = to_target(table, row, self.repo.db.dialect)
        columns = [c.name for c in BY_NAME[table].columns]
        tx.execute('INSERT INTO %s (%s) VALUES (%s)' % (table, ','.join(columns), ','.join(':b_' + c for c in columns)), binds,
                   ts_binds=self.repo._ts(BY_NAME[table], ['b_' + c for c in columns]))

    def put(self, *pairs):
        with self.repo.db.transaction() as tx:
            return [self.repo.upsert(tx, table, row) for table, row in pairs]

    def base(self):
        self.put(('food_entities', food()), ('food_aliases', alias()), ('source_items', item()), ('ingest_runs', run()))

    # 1, 11: transactions
    def test_commit_and_rollback(self):
        self.put(('food_entities', food()))
        with self.assertRaises(RuntimeError):
            with self.repo.db.transaction() as tx:
                self.repo.upsert(tx, 'food_entities', food('FOOD_b'))
                raise RuntimeError('fail mid-transaction')
        self.assertEqual(self.repo.count('food_entities'), 1)

    # 2, 3: migration repeat
    def test_migration_repeat_is_a_no_op(self):
        self.assertEqual(self.repo.migrate()['created_objects'], 0)
        self.assertEqual(self.repo.schema_version(), 1)

    # 4: PK/FK/UNIQUE/CHECK enforced by the database itself
    def test_constraints_are_enforced_by_the_database(self):
        with self.assertRaises(IntegrityViolation) as fk:
            with self.repo.db.transaction() as tx:
                self.raw_insert(tx, 'food_aliases', alias(food_id='FOOD_missing'))
        self.assertEqual(fk.exception.code, 'foreign_key_violation')
        self.put(('food_entities', food()), ('food_aliases', alias()))
        for table, row, code in (('food_aliases', alias('ALIAS_dup'), 'unique_violation'),
                                 ('food_entities', food('FOOD_x', status='DELETED'), 'check_violation'),
                                 ('food_entities', food('FOOD_a'), 'unique_violation')):
            with self.subTest(table=table, code=code), self.assertRaises(Exception) as error:
                with self.repo.db.transaction() as tx:
                    self.raw_insert(tx, table, row)
            self.assertEqual(error.exception.code, code)
        self.put(('food_aliases', alias('ALIAS_old', form='두쫀', status='REVOKED')), ('food_aliases', alias('ALIAS_new', form='두쫀')))
        with self.assertRaises(Exception):  # only one ACTIVE alias per normalized form/scope/version
            self.put(('food_aliases', alias('ALIAS_third', form='두쫀')))

    # 5: Unicode
    def test_korean_emoji_and_combining_characters_round_trip(self):
        names = ['두바이 쫀득 쿠키 🍪', '각 조합형', 'é́ mixed']
        self.put(*[('food_entities', food('FOOD_%d' % n, name)) for n, name in enumerate(names)])
        self.assertEqual(sorted(r['canonical_name'] for r in self.repo.rows('food_entities')), sorted(names))

    # 6: instants across offsets and DST
    def test_timestamps_round_trip_as_instants(self):
        cases = {'2026-10-27T09:00:00+09:00': '2026-10-27T00:00:00.000000+00:00',
                 '2026-03-08T01:30:00.123456-08:00': '2026-03-08T09:30:00.123456+00:00',
                 '2026-11-01T01:30:00-07:00': '2026-11-01T08:30:00.000000+00:00'}
        for n, (given, expected) in enumerate(cases.items()):
            self.put(('food_entities', food('FOOD_%d' % n, created_at=given)))
            self.assertEqual(self.repo.get('food_entities', {'food_id': 'FOOD_%d' % n})['created_at'], expected)
        with self.assertRaises(ConversionError):
            self.put(('food_entities', food('FOOD_naive', created_at='2026-10-27T09:00:00')))

    # 7: '' versus NULL
    def test_empty_string_and_null_keep_their_meaning(self):
        self.put(('food_entities', food()))
        candidate = dict(candidate_id='CAND_a', candidate_key='ck', source_code='user_input', source_item_key=None, origin='user',
                         surface_form='두쫀쿠', normalized_form='두쫀쿠', scope='food:ko', proposed_kind='PRODUCT', proposed_aliases_json='[]',
                         description='', proposed_relations_json='[]', status='NEW', revision=1, resolved_food_id=None,
                         resolution_alias_id=None, resolution_method=None, resolution_version='v', type_gate='FOOD', contract_json='{}',
                         created_at='2026-10-27T00:00:00+00:00', updated_at='2026-10-27T00:00:00+00:00')
        self.put(('food_candidates', candidate))
        stored = self.repo.get('food_candidates', candidate)
        self.assertEqual((stored['description'], stored['resolved_food_id']), ('', None))
        with self.assertRaises(ConversionError) as error:
            self.put(('food_entities', food('FOOD_e', '')))
        self.assertTrue(error.exception.code.startswith('empty_string_not_representable'))

    # 8: JSON
    def test_json_is_validated_and_canonical(self):
        with self.assertRaises(ConversionError):
            self.put(('food_commands', dict(request_key='k', operation='o', payload_hash='a' * 64, result_json='{bad',
                                            created_at='2026-10-27T00:00:00+00:00')))
        self.put(('food_commands', dict(request_key='k', operation='o', payload_hash='a' * 64, result_json='{"b": [1, 2], "a": "값"}',
                                        created_at='2026-10-27T00:00:00+00:00')))
        self.assertEqual(self.repo.get('food_commands', {'request_key': 'k'})['result_json'], '{"a":"값","b":[1,2]}')

    # 9: large integers
    def test_large_integers_and_exact_count_strings(self):
        self.put(('food_entities', food(revision=10 ** 18 - 1)))
        self.assertEqual(self.repo.get('food_entities', {'food_id': 'FOOD_a'})['revision'], 10 ** 18 - 1)
        with self.assertRaises(ConversionError):
            self.put(('food_entities', food('FOOD_b', revision=10 ** 19)))
        content = dict(payload_ref='youtube_videos:abcdefghijk', item_key='ITEM_y', source_code='youtube', origin='mock',
                       content_json='{"view_count": "12345678901234567890"}', content_hash='h' * 64,
                       captured_at='2026-10-27T00:00:00+00:00', refreshed_at='2026-10-27T00:00:00+00:00',
                       expires_at='2026-11-20T00:00:00+00:00', field_expires_at=None, field_purged_at=None)
        self.put(('source_contents', content))
        self.assertIn('"12345678901234567890"', self.repo.get('source_contents', content)['content_json'])

    # 10: concurrent upserts and the unique-race retry path (ORA-00001 equivalent)
    def test_concurrent_upserts_store_one_row(self):
        errors = []
        def worker():
            try:
                self.put(('food_entities', food()))
            except Exception as exc:  # pragma: no cover - surfaced by the assertion below
                errors.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual((errors, self.repo.count('food_entities')), ([], 1))

    def test_unique_race_rereads_and_updates(self):
        self.put(('food_entities', food()))
        raced = {}
        def race(table, keys):
            if table == 'food_aliases' and not raced:
                raced['done'] = True
                self.raw_insert(self.tx, 'food_aliases', alias(revision=1))
        self.repo.before_write = race
        with self.repo.db.transaction() as tx:
            self.tx = tx
            result = self.repo.upsert(tx, 'food_aliases', alias(revision=2))
        self.assertEqual((raced, result), ({'done': True}, 'updated'))
        self.assertEqual(self.repo.get('food_aliases', {'alias_id': 'ALIAS_a'})['revision'], 2)

    # 12: outbox redelivery
    def test_redelivered_state_is_idempotent(self):
        rows = [dict(event_id='OUTBOX_a', job_id='JOB_a', created_at='2026-10-27T00:00:00+00:00', delivered_at=None, delivery_count=n)
                for n in (1, 1, 2, 2)]
        spec = BY_NAME['execution_outbox']
        self.assertIn('delivery_count', spec.mutable)
        results = []
        with self.repo.db.transaction() as tx:  # foreign keys are deferred to commit in both dialects
            for row in rows:
                results.append(self.repo.upsert(tx, 'execution_outbox', row))
            tx.execute('DELETE FROM execution_outbox')  # no parent job in this isolated case
        self.assertEqual(results, ['inserted', 'unchanged', 'updated', 'unchanged'])

    # 13: origin labels
    def test_invalid_or_upgraded_origin_is_refused(self):
        self.base()
        with self.assertRaises(PolicyRefused) as invalid:
            self.put(('source_items', item('ITEM_b', origin='scraped')))
        self.assertEqual(invalid.exception.code, 'origin_not_allowed')
        with self.assertRaises(PolicyRefused) as upgrade:
            self.put(('source_items', item(origin='live_api')))
        self.assertEqual(upgrade.exception.code, 'origin_change_refused')
        with self.assertRaises(IntegrityViolation):
            with self.repo.db.transaction() as tx:
                self.raw_insert(tx, 'source_items', item('ITEM_c', origin='scraped'))

    # 14, 15: NAVER and YouTube storage blocked twice (application gate and table constraint)
    def test_naver_and_youtube_metrics_and_mentions_are_blocked(self):
        self.base()
        for row, code in ((metric('naver_datalab', 'naver.relative_search_index', unit='relative_search_index_0_100', count_value=None,
                                  decimal_value=50.0), 'metric_storage_blocked'),
                          (metric('youtube', 'youtube.view_count', unit='video_view_count_snapshot'), 'metric_storage_blocked')):
            with self.assertRaises(PolicyRefused) as gate:
                self.put(('metric_observations', row))
            self.assertEqual(gate.exception.code, code)
            with self.assertRaises(IntegrityViolation) as constraint:
                with self.repo.db.transaction() as tx:
                    self.raw_insert(tx, 'metric_observations', row)
            self.assertEqual(constraint.exception.code, 'check_violation')
        with self.assertRaises(PolicyRefused):
            self.put(('food_mentions', mention('youtube')))
        with self.assertRaises(IntegrityViolation):
            with self.repo.db.transaction() as tx:
                self.raw_insert(tx, 'food_mentions', mention('youtube'))
        self.assertEqual(self.put(('metric_observations', metric()), ('food_mentions', mention())), ['inserted', 'inserted'])

    def test_expired_content_and_immutable_changes_are_refused(self):
        self.base()
        expired = item('ITEM_old', source='youtube', expires_at=(NOW - timedelta(seconds=1)).isoformat(), payload_ref='youtube_videos:x')
        with self.assertRaises(PolicyRefused) as error:
            self.put(('source_items', expired))
        self.assertEqual(error.exception.code, 'expired_content')
        with self.assertRaises(IntegrityViolation) as immutable:
            self.put(('ingest_runs', dict(run(), adapter_version='changed')))
        self.assertTrue(immutable.exception.code.startswith('immutable_column_changed'))

    def test_food_view_reads_through_the_repository(self):
        self.base()
        self.put(('food_mentions', mention()), ('metric_observations', metric()))
        view = self.repo.food_view('FOOD_a')['sources']['instagram_graph']
        self.assertEqual((view['confirmed_items'], view['mentions'], len(view['metrics'])), (1, 1, 1))
        self.assertEqual(self.repo.resolve_alias('두쫀쿠', 'food:ko')['food_id'], 'FOOD_a')


class PortableSqlTests(unittest.TestCase):
    def test_repository_sql_avoids_sqlite_only_constructs(self):
        import re
        import food_trends.target_repository as module
        source = Path(module.__file__).read_text(encoding='utf-8')
        statements = ' '.join(re.findall(r"'''(.*?)'''|'((?:SELECT|INSERT|UPDATE|DELETE)[^']*)'", source, re.S).__str__().split())
        for required in ('SELECT count(DISTINCT', 'INSERT INTO', 'UPDATE %s SET', 'DELETE FROM'):
            self.assertIn(required, statements)  # the scan really saw the repository's SQL
        for forbidden in ('LIMIT', 'ON CONFLICT', 'INSERT OR', 'PRAGMA', 'rowid', 'json_valid', 'group_concat', 'BEGIN IMMEDIATE'):
            self.assertNotIn(forbidden, statements)


class SQLiteContractTests(ContractCases, unittest.TestCase):
    def make_db(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        return SQLiteTarget(Path(self.temp.name) / 'target.sqlite3')


@unittest.skipUnless(ORACLE_READY, 'Real Oracle test schema not configured (ORACLE_TEST_*): Oracle compatibility NOT_VERIFIED')
class OracleContractTests(ContractCases, unittest.TestCase):
    def make_db(self):
        db = OracleTarget(OracleSettings.from_env(prefix='ORACLE_TEST_'))
        with db.transaction() as tx:  # isolated test schema only (ORACLE_TEST_ALLOW_RESET=true)
            for name in ['target_schema_migrations'] + [t.name for t in reversed(TABLES)]:
                tx.execute("BEGIN EXECUTE IMMEDIATE 'DROP TABLE %s CASCADE CONSTRAINTS PURGE'; EXCEPTION WHEN OTHERS THEN NULL; END;" % name)
        self.addCleanup(db.close)
        return db
