"""B6 target schema and repository basics: DDL portability checks and the SQLite stand-in."""
import re
import tempfile
import unittest
from pathlib import Path
from food_trends.storage import Storage
from food_trends.target_db import OracleSettings, OracleUnavailable, SQLiteTarget
from food_trends.target_repository import TargetRepository
from food_trends.target_schema import BY_NAME, ORACLE_RESERVED, TABLES, ddl, identifiers


class TargetSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = TargetRepository(SQLiteTarget(Path(self.temp.name) / 'target.sqlite3'))

    def tearDown(self):
        self.temp.cleanup()

    def test_identifiers_avoid_oracle_reserved_words_and_fit_length_limits(self):
        for name in identifiers():
            self.assertNotIn(name.upper(), ORACLE_RESERVED, name)
            self.assertLessEqual(len(name), 30, name)
        for statement in ddl('oracle'):
            for name in re.findall(r'(?:CONSTRAINT|INDEX) (\w+)', statement):
                self.assertLessEqual(len(name), 30, name)

    def test_oracle_ddl_has_no_sqlite_only_syntax(self):
        text = '\n'.join(ddl('oracle'))
        for forbidden in ('json_valid', 'INSERT OR', 'AUTOINCREMENT', ' TEXT', ' INTEGER', ' REAL', 'ON CONFLICT'):
            self.assertNotIn(forbidden, text)
        self.assertFalse([s for s in ddl('oracle') if s.startswith('CREATE') and 'INDEX' in s.split('(')[0] and ' WHERE ' in s])
        for required in ('IS JSON', 'TIMESTAMP(6) WITH TIME ZONE', 'DEFERRABLE INITIALLY DEFERRED', 'CASE WHEN', 'NUMBER(19)', 'CLOB'):
            self.assertIn(required, text)

    def test_every_foreign_key_has_a_leading_index(self):
        for dialect in ('oracle', 'sqlite'):
            statements = ddl(dialect)
            for table in TABLES:
                for cols, _, _ in table.fks:
                    leading = [table.pk] + list(table.unique) + list(table.indexes) + [c for c, _ in table.partial_unique]
                    indexed = any(tuple(x[:len(cols)]) == cols for x in leading) or any(
                        s.startswith('CREATE INDEX') and (' ON %s (%s' % (table.name, ','.join(cols))) in s for s in statements)
                    self.assertTrue(indexed, (dialect, table.name, cols))

    def test_policy_constraints_exist_in_both_dialects(self):
        for dialect in ('oracle', 'sqlite'):
            text = '\n'.join(ddl(dialect))
            metric = next(s for s in ddl(dialect) if s.startswith('CREATE TABLE metric_observations'))
            mention = next(s for s in ddl(dialect) if s.startswith('CREATE TABLE food_mentions'))
            for statement in (metric, mention):
                self.assertIn("source_code IN ('google_trends_rss','instagram_graph')", statement)
            self.assertIn("origin IN ('live_api','mock','user','legacy_unknown')", text)

    def test_target_covers_every_source_column_with_matching_nullability(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage.create(Path(directory) / 'v10.sqlite3')
            with storage.reader() as db:
                for table in TABLES:
                    if not table.source_table or table.name in ('rss_classifications', 'rss_classification_history'):
                        continue
                    source = {r[1]: bool(r[3]) or bool(r[5]) for r in db.execute('PRAGMA table_info(%s)' % table.source_table)}
                    mapped = {c.src: c for c in table.columns}
                    extra = {'food_mentions': {'source_code'}}.get(table.name, set())
                    self.assertEqual(set(source), set(mapped) - extra, table.name)
                    for name, required in source.items():
                        column = mapped[name]
                        self.assertEqual(required, not column.null, (table.name, name))

    def test_migration_is_explicit_repeatable_and_checksummed(self):
        self.assertEqual(self.repo.schema_version(), 0)  # construction created nothing
        first = self.repo.migrate()
        self.assertEqual((first['version'], first['applied']), (1, [1]))
        self.assertGreater(first['created_objects'], len(TABLES))
        self.assertEqual(self.repo.migrate(), {'version': 1, 'applied': [], 'created_objects': 0})
        with self.repo.db.transaction() as tx:
            tx.execute("UPDATE target_schema_migrations SET checksum='tampered'")
        with self.assertRaises(Exception) as error:
            self.repo.migrate()
        self.assertEqual(error.exception.code, 'target_schema_checksum_mismatch')

    def test_upsert_is_idempotent_and_normalizes_time_and_json(self):
        self.repo.migrate()
        row = dict(request_key='k1', operation='submit', payload_hash='a' * 64, result_json='{"b":1,"a":"두쫀쿠"}',
                   created_at='2026-10-27T09:00:00+09:00')
        with self.repo.db.transaction() as tx:
            self.assertEqual([self.repo.upsert(tx, 'food_commands', row) for _ in range(3)], ['inserted', 'unchanged', 'unchanged'])
        stored = self.repo.get('food_commands', row)
        self.assertEqual(stored['created_at'], '2026-10-27T00:00:00.000000+00:00')
        self.assertEqual(stored['result_json'], '{"a":"두쫀쿠","b":1}')
        self.assertEqual(self.repo.count('food_commands'), 1)

    def test_oracle_settings_fail_closed_without_configuration_or_production_approval(self):
        with self.assertRaises(OracleUnavailable) as missing:
            OracleSettings.from_env({})
        self.assertEqual(missing.exception.code, 'oracle_not_configured')
        env = {'ORACLE_DSN': 'dbhost/FREEPDB1', 'ORACLE_USER': 'mz', 'ORACLE_PASSWORD': 'synthetic-secret', 'ORACLE_ENVIRONMENT': 'production'}
        with self.assertRaises(OracleUnavailable) as production:
            OracleSettings.from_env(env)
        self.assertEqual(production.exception.code, 'oracle_production_not_approved')
        settings = OracleSettings.from_env(dict(env, ORACLE_ENVIRONMENT='development'))
        self.assertNotIn('synthetic-secret', repr(settings))

    def test_cli_refuses_the_application_database_as_target(self):
        from food_trends.__main__ import target_repository
        app = Path(self.temp.name) / 'app.sqlite3'
        with self.assertRaises(ValueError):
            target_repository('sqlite', str(app), str(app))
        with self.assertRaises(OracleUnavailable):
            target_repository('oracle', None, str(app), environ={})

    def test_spec_lookup(self):
        self.assertEqual(BY_NAME['execution_jobs'].column('exec_mode').src, 'mode')
