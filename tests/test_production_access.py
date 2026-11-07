"""ORACLE-PRODUCTION-CUTOVER-1: the production runtime opens only through an explicit, checkable production access.

A stand-in labelled production (settings environment production, user MZ_APP) stands for the production schema; nothing
here touches a real production account. ORACLE_ALLOW_PRODUCTION alone is not enough: the connected account must be the
declared production schema (never a *_TEST, test or system account) with a valid approval record covering the cut-over
gates, and the use must be SHADOW (load, Shadow, recovery) or RUNTIME_PRODUCTION (runtime), never RUNTIME_TEST, while
RUNTIME_PRODUCTION stays refused on every other target. On production every job must be live, so mock responses never
enter production data, and the collector-free production assembly keeps management on the configured accounts. The
test helper's reset refuses any account that is not a *_TEST schema.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from food_trends.__main__ import main
from food_trends.oracle_execution import OracleExecution
from food_trends.oracle_live import oracle_live_readiness
from food_trends.production_access import REQUIRED_GATES, ProductionRefused, approved, verify
from food_trends.runtime_ownership import OwnershipRefused, RuntimeOwnership
from food_trends.runtime_storage import oracle_maintenance, production_backend, select
from food_trends.storage import Storage
from food_trends.target_db import OracleUnavailable, SQLiteTarget
from food_trends.target_migration import migrate
from food_trends.target_repository import TargetRepository
from food_trends.tracking_access import mock_accounts
import oracle_support
from test_oracle_live import NOW


def labelled(environment, user):
    class Labelled(SQLiteTarget):
        settings = SimpleNamespace(environment=environment, user=user)
    return Labelled


class ProductionAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.approval = self.dir / 'production-approval.json'
        self.write(schema='MZ_APP', approved_by='project owner', approved_at='2026-11-08T09:00:00+09:00',
                   gates={g: 'evidence of %s' % g for g in REQUIRED_GATES})
        self.env = {'ORACLE_PRODUCTION_SCHEMA': 'MZ_APP', 'ORACLE_PRODUCTION_APPROVAL': str(self.approval), 'ORACLE_TEST_USER': 'MZ_TEST'}
        patcher = patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, **record):
        self.approval.write_text(json.dumps(record), encoding='utf-8')

    def target(self, environment='production', user='MZ_APP'):
        repo = TargetRepository(labelled(environment, user)(self.dir / ('%s.sqlite3' % uuid.uuid4().hex[:8])), clock=lambda: NOW)
        repo.migrate()
        return repo

    def test_only_the_declared_schema_with_a_valid_approval_is_production(self):
        repo = self.target()
        self.assertEqual(verify(repo.db)['schema'], 'MZ_APP')
        cases = {
            'not_a_production_target': (self.target('test', 'MZ_TEST').db, {}),
            'production_schema_not_declared': (repo.db, {'ORACLE_PRODUCTION_SCHEMA': ''}),
            'production_schema_is_a_test_or_system_account': (repo.db, {'ORACLE_PRODUCTION_SCHEMA': 'MZ_TEST'}),
            'production_approval_missing': (repo.db, {'ORACLE_PRODUCTION_APPROVAL': ''}),
            'production_approval_unreadable': (repo.db, {'ORACLE_PRODUCTION_APPROVAL': str(self.dir / 'absent.json')}),
            'production_schema_mismatch': (self.target(user='OTHER_APP').db, {}),
        }
        for code, (db, env) in cases.items():
            with self.subTest(code=code), patch.dict(os.environ, env):
                with self.assertRaises(ProductionRefused) as refused:
                    verify(db)
                self.assertEqual(refused.exception.code, code)
        for broken in (dict(schema='MZ_APP', approved_by='owner', approved_at='2026-11-08T09:00:00+09:00', gates={'G-OPS-1': 'x'}),
                       dict(schema='MZ_APP', approved_by='', approved_at='2026-11-08T09:00:00+09:00', gates={g: 'x' for g in REQUIRED_GATES}),
                       dict(schema='MZ_APP', approved_by='owner', approved_at='yesterday', gates={g: 'x' for g in REQUIRED_GATES}),
                       dict(schema='MZ_APP', approved_by='owner', approved_at='2026-11-08T09:00:00+09:00', gates={g: 'x' for g in REQUIRED_GATES}, extra=1)):
            self.write(**broken)
            with self.subTest(broken=sorted(broken)), self.assertRaises(ProductionRefused) as refused:
                verify(repo.db)
            self.assertEqual(refused.exception.code, 'production_approval_invalid')
        self.write(schema='MZ_OTHER', approved_by='owner', approved_at='2026-11-08T09:00:00+09:00', gates={g: 'x' for g in REQUIRED_GATES})
        with self.assertRaises(ProductionRefused) as refused:
            verify(repo.db)
        self.assertEqual(refused.exception.code, 'production_approval_for_another_schema')

    def test_ownership_modes_follow_the_target_and_its_approval(self):
        with patch.dict(os.environ, {'ORACLE_PRODUCTION_APPROVAL': ''}):
            locked = RuntimeOwnership(self.target())
            for mode in ('SHADOW', 'RUNTIME_PRODUCTION'):
                with self.assertRaises(OwnershipRefused) as refused:
                    locked.acquire(mode)
                self.assertEqual(refused.exception.code, 'production_target_requires_approval')
        owner = RuntimeOwnership(self.target())
        self.assertEqual(owner.runtime_mode(), 'RUNTIME_PRODUCTION')
        with self.assertRaises(OwnershipRefused) as refused:
            owner.acquire('RUNTIME_TEST')
        self.assertEqual(refused.exception.code, 'runtime_test_refused_on_production')
        owner.release(owner.acquire('SHADOW'))
        lease = owner.acquire('RUNTIME_PRODUCTION')
        self.assertEqual(owner.status()['mode'], 'RUNTIME_PRODUCTION')
        owner.release(lease)
        standin = RuntimeOwnership(self.target('standin', ''))
        standin.db.settings = None  # the plain stand-in
        with self.assertRaises(OwnershipRefused) as refused:
            standin.acquire('RUNTIME_PRODUCTION')
        self.assertEqual((refused.exception.code, standin.runtime_mode()), ('runtime_production_disabled', 'RUNTIME_TEST'))
        with self.assertRaises(OwnershipRefused) as refused:
            RuntimeOwnership(self.target('test', 'MZ_APP')).acquire('RUNTIME_TEST')  # a test label on another schema
        self.assertEqual(refused.exception.code, 'test_environment_requires_test_schema')

    def test_the_production_runtime_writes_under_its_own_mode_and_never_runs_mock_jobs(self):
        repo = self.target()
        legacy = Storage.create(self.dir / 'legacy.sqlite3')
        self.assertTrue(migrate(legacy.path, repo, now=NOW)['batch_key'].startswith('B7_'))  # the B7 load holds SHADOW on production
        backend = production_backend(repo, clock=lambda: NOW)
        self.addCleanup(backend.execution.release)
        food = backend.foods.command('submit', dict(request_key=uuid.uuid4().hex, name='운영검증 음식', aliases=[]))
        self.assertIn('candidate_id', food)
        self.assertEqual(RuntimeOwnership(repo).status()['mode'], 'RUNTIME_PRODUCTION')
        accounts = {a.account_scope for a in backend.tracking.accounts.values()}
        self.assertNotIn('mock', {a.mode for a in backend.tracking.accounts.values()})
        self.assertTrue(accounts)
        self.assertFalse(backend.rss.collect()['accepted'])  # no fetcher: production never fetches outside the live assembly
        self.assertEqual(oracle_maintenance(backend)['api_requests'], 0)
        backend.execution.release()
        mock = OracleExecution(repo, mock_accounts(), lambda: NOW)
        self.addCleanup(mock.release)
        with mock.reader() as t:
            plan = {'source_id': 'youtube', 'account_scope': 'local-default'}
            self.assertIn('MOCK_EXECUTION_REFUSED_ON_PRODUCTION', mock._gate(t, plan, 'mock'))
        self.assertNotIn('ORACLE_PRODUCTION_LOCKED', oracle_live_readiness(repo, None)['blockers'])
        with patch.dict(os.environ, {'ORACLE_PRODUCTION_APPROVAL': ''}):
            unapproved = self.target()
            with self.assertRaises(OracleUnavailable) as refused:
                OracleExecution(unapproved, mock_accounts(), lambda: NOW)
            self.assertEqual(refused.exception.code, 'oracle_production_runtime_disabled')
            self.assertIn('ORACLE_PRODUCTION_LOCKED', oracle_live_readiness(unapproved, None)['blockers'])
            self.assertFalse(approved(unapproved.db))

    def test_selection_cli_and_test_reset_keep_test_and_production_apart(self):
        base = {'ORACLE_DSN': 'h/p', 'ORACLE_USER': 'MZ_APP', 'ORACLE_PASSWORD': 'synthetic'}
        for env, code in ((dict(base, ORACLE_ENVIRONMENT='test'), 'oracle_production_runtime_disabled'),
                          (dict(base, ORACLE_ENVIRONMENT='production'), 'oracle_production_runtime_disabled')):
            with self.subTest(env=env.get('ORACLE_ENVIRONMENT')), self.assertRaises(OracleUnavailable) as refused:
                select('oracle-production', env)
            self.assertEqual(refused.exception.code, code)
        with self.assertRaises(OracleUnavailable) as refused:
            select('oracle-test', {'ORACLE_TEST_DSN': 'h/p', 'ORACLE_TEST_USER': 'MZ_APP', 'ORACLE_TEST_PASSWORD': 'synthetic',
                                   'ORACLE_TEST_ENVIRONMENT': 'test'})
        self.assertEqual(refused.exception.code, 'oracle_test_schema_required')

        class Account:
            def __init__(self, user):
                self.user = user

            @contextlib.contextmanager
            def transaction(self):
                yield SimpleNamespace(scalar=lambda sql: self.user, rows=lambda sql: [], execute=self.fail)

            def fail(self, sql):
                raise AssertionError('nothing may be dropped')
        for user in ('MZ_APP', 'MZ_TEST'):
            with self.subTest(user=user), patch.dict(os.environ, {'ORACLE_TEST_USER': user, 'ORACLE_PRODUCTION_SCHEMA': 'MZ_TEST' if user == 'MZ_TEST' else 'MZ_APP'}):
                with self.assertRaises(oracle_support.UnsafeSchema):
                    oracle_support.reset_target_schema(Account(user))
        err = io.StringIO()
        with patch('sys.argv', ['food_trends', '--backend', 'oracle-production', '--rss-mock-file', 'feed.xml']), \
                patch('food_trends.__main__.load_env'), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
            main()
        self.assertEqual((stop.exception.code, 'Mock RSS' in err.getvalue()), (2, True))


if __name__ == '__main__':
    unittest.main()
