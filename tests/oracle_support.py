"""Guarded access to a disposable Oracle test schema (ORACLE_TEST_*). Never used against production.

reset_target_schema() re-verifies the connected account before dropping anything: it must be the
configured test user, not a built-in or DBA account, and may own only target-schema objects.
Anything else stops the run instead of deleting.
"""
import os
from food_trends.target_db import OracleSettings, OracleTarget
from food_trends.target_schema import TABLES

ORACLE_READY = bool(os.environ.get('ORACLE_TEST_DSN')) and os.environ.get('ORACLE_TEST_ALLOW_RESET') == 'true'
SKIP_REASON = 'Real Oracle test schema not configured (ORACLE_TEST_*): Oracle compatibility NOT_VERIFIED'
PROTECTED = {'SYS', 'SYSTEM', 'SYSBACKUP', 'SYSDG', 'SYSKM', 'SYSRAC', 'AUDSYS', 'OUTLN', 'DBSNMP', 'XDB', 'PDBADMIN'}
OWNED = {t.name.upper() for t in TABLES} | {'TARGET_SCHEMA_MIGRATIONS'}


class UnsafeSchema(RuntimeError):
    pass


def oracle_target():
    settings = OracleSettings.from_env(prefix='ORACLE_TEST_')
    if settings.environment != 'test':
        raise UnsafeSchema('ORACLE_TEST_ENVIRONMENT must be test')
    return OracleTarget(settings)


def reset_target_schema(db):
    expected = os.environ.get('ORACLE_TEST_USER', '').upper()
    with db.transaction() as tx:
        user = tx.scalar('SELECT user FROM dual')
        roles = {r['granted_role'] for r in tx.rows('SELECT granted_role FROM user_role_privs')}
        tables = {r['table_name'] for r in tx.rows('SELECT table_name FROM user_tables')}
        if user != expected or user in PROTECTED or 'DBA' in roles:
            raise UnsafeSchema('connected account is not the disposable test schema')
        if tables - OWNED:
            raise UnsafeSchema('test schema owns objects outside the target specification')
        for name in sorted(tables, key=lambda n: n != 'TARGET_SCHEMA_MIGRATIONS'):
            tx.execute('DROP TABLE %s CASCADE CONSTRAINTS PURGE' % name)
    return user
