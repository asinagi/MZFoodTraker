"""ORACLE-PRODUCTION-CUTOVER-1: when an Oracle target labelled production may be written or served.

ORACLE_ENVIRONMENT=production with ORACLE_ALLOW_PRODUCTION=true only opens the connection (target_db.OracleSettings).
Any use of the target needs, on top, an explicit and checkable production access:
- the connected account is the declared production schema (ORACLE_PRODUCTION_SCHEMA), never the test schema
  (ORACLE_TEST_USER or any *_TEST name), a system account or an account holding DBA;
- a production approval record (ORACLE_PRODUCTION_APPROVAL: a JSON file kept out of Git) naming that schema, who approved
  the cut-over, when, and the evidence of every gate the production runtime depends on (REQUIRED_GATES);
- the ownership mode of the use (runtime_ownership): SHADOW for the B7 load, Shadow and recovery, RUNTIME_PRODUCTION for
  the runtime; RUNTIME_TEST is refused on production and RUNTIME_PRODUCTION everywhere else.
Live collection keeps its own per-source gates (registry, writer transfer, usage) on top of this. Codes only: no
credential, DSN or approval text is ever echoed.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from .target_db import TargetError

REQUIRED_GATES = ('G-OPS-1', 'G-OPS-2', 'G-OPS-3', 'G-OPS-6')  # backup, legacy writers stopped, SQLite migration path, cut-over
SYSTEM_ACCOUNTS = {'SYS', 'SYSTEM', 'SYSBACKUP', 'SYSDG', 'SYSKM', 'SYSRAC', 'AUDSYS', 'OUTLN', 'DBSNMP', 'XDB', 'PDBADMIN'}


class ProductionRefused(TargetError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _session(db):
    """The connected account and its roles (the stand-in used by tests reports its configured user and no role)."""
    if getattr(db, 'dialect', None) == 'oracle':
        with db.transaction() as tx:
            return tx.scalar('SELECT user FROM dual').upper(), {r['granted_role'] for r in tx.rows('SELECT granted_role FROM user_role_privs')}
    return str(getattr(db.settings, 'user', '')).upper(), set()


def approval(environ=None):
    """The production approval record named by ORACLE_PRODUCTION_APPROVAL, validated (ProductionRefused otherwise)."""
    env = os.environ if environ is None else environ
    path = env.get('ORACLE_PRODUCTION_APPROVAL', '').strip()
    if not path:
        raise ProductionRefused('production_approval_missing')
    try:
        record = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise ProductionRefused('production_approval_unreadable') from None
    gates = record.get('gates') if isinstance(record, dict) else None
    if (not isinstance(record, dict) or set(record) != {'schema', 'approved_by', 'approved_at', 'gates'} or not isinstance(gates, dict)
            or not all(isinstance(record[k], str) and record[k].strip() for k in ('schema', 'approved_by', 'approved_at'))
            or any(not isinstance(gates.get(g), str) or not gates[g].strip() for g in REQUIRED_GATES)):
        raise ProductionRefused('production_approval_invalid')
    try:
        datetime.fromisoformat(record['approved_at'])
    except ValueError:
        raise ProductionRefused('production_approval_invalid') from None
    return record


def verify(db, environ=None):
    """Raise ProductionRefused unless `db` is the approved production schema; returns the checked facts (no secret)."""
    env = os.environ if environ is None else environ
    settings = getattr(db, 'settings', None)
    if settings is None or settings.environment != 'production':
        raise ProductionRefused('not_a_production_target')
    expected = env.get('ORACLE_PRODUCTION_SCHEMA', '').strip().upper()
    if not expected:
        raise ProductionRefused('production_schema_not_declared')
    if expected.endswith('_TEST') or expected == env.get('ORACLE_TEST_USER', '').strip().upper() or expected in SYSTEM_ACCOUNTS:
        raise ProductionRefused('production_schema_is_a_test_or_system_account')
    record = approval(env)
    if record['schema'].strip().upper() != expected:
        raise ProductionRefused('production_approval_for_another_schema')
    user, roles = _session(db)
    if user != expected:
        raise ProductionRefused('production_schema_mismatch')
    if 'DBA' in roles:
        raise ProductionRefused('production_account_holds_dba')
    return {'schema': user, 'approved_by': record['approved_by'], 'approved_at': record['approved_at'], 'gates': sorted(record['gates'])}


def approved(db, environ=None):
    """True when verify() passes (cached on the connection object: the check reads the account once per process)."""
    if getattr(db, '_production_verified', False):
        return True
    try:
        verify(db, environ)
    except ProductionRefused:
        return False
    db._production_verified = True
    return True
