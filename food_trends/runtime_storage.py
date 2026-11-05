"""ORACLE-RUNTIME-1: explicit storage selection for the B3 execution, B4 ingestion and B5 observation services.

Three modes, never inferred:
  sqlite             the existing SQLite services on DATABASE_PATH (development and operations compatibility)
  oracle-test        the Oracle runtime on the isolated test schema (ORACLE_TEST_*, ORACLE_TEST_ENVIRONMENT=test)
  oracle-production  refused until the separately approved Oracle cut-over (G-OPS-6)

Selecting a backend opens nothing on the other one: the Oracle services never touch the SQLite
database and the SQLite services never open Oracle, so there is no implicit dual write (Shadow is the
only, explicit, copy path). Nothing here runs DDL or migrations; status() only reads versions.
"""
from .target_db import OracleSettings, OracleTarget, OracleUnavailable
from .target_repository import TargetRepository
from .target_schema import TARGET_SCHEMA_VERSION

MODES = ('sqlite', 'oracle-test', 'oracle-production')


def oracle_test_repository(environ=None):
    settings = OracleSettings.from_env(environ, prefix='ORACLE_TEST_')
    if settings.environment != 'test':
        raise OracleUnavailable('oracle_test_environment_required')
    return TargetRepository(OracleTarget(settings))


def select(mode, environ=None):
    """The target repository for an Oracle mode; None for sqlite (callers keep using Storage)."""
    if mode not in MODES:
        raise ValueError('unknown runtime storage mode')
    if mode == 'oracle-production':
        raise OracleUnavailable('oracle_production_runtime_disabled')
    if mode == 'oracle-test':
        return oracle_test_repository(environ)
    return None


def oracle_services(repo, accounts, clock=None, settings=None):
    """(execution, ingestion, observations) over one target; checks the schema version, never migrates."""
    from .oracle_execution import OracleExecution
    from .oracle_ingestion import OracleIngestionService
    from .oracle_observations import OracleObservationService
    execution = OracleExecution(repo, accounts, clock, settings)
    return execution, OracleIngestionService(execution), OracleObservationService(execution)


def status(mode, database_path, environ=None):
    """Read-only: which backend a mode selects and whether its schema is ready. No DDL, no writes, no API calls."""
    if mode == 'sqlite':
        from .migrations import LATEST_VERSION, schema_version
        version = schema_version(database_path)
        return {'mode': mode, 'backend': 'sqlite', 'schema_version': version, 'required_version': LATEST_VERSION,
                'ready': version == LATEST_VERSION, 'database_writes': 0, 'http_requests': 0}
    repo = select(mode, environ)
    try:
        version = repo.schema_version()
        owner = None
        if version == TARGET_SCHEMA_VERSION:
            from .runtime_ownership import RuntimeOwnership
            owner = RuntimeOwnership(repo).status()
    finally:
        repo.db.close()
    return {'mode': mode, 'backend': 'oracle', 'environment': repo.db.settings.environment, 'schema_version': version,
            'required_version': TARGET_SCHEMA_VERSION, 'ready': version == TARGET_SCHEMA_VERSION, 'ownership': owner,
            'database_writes': 0, 'http_requests': 0}
