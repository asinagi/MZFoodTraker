"""ORACLE-RUNTIME-1: explicit storage selection for the backend services (B3 execution, B4 ingestion, B5 observations;
since ORACLE-CUTOVER-PREP-1 also food identity and tracking commands, since ORACLE-BACKEND-COMPLETION-1 RSS discovery,
through oracle_backend).

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


def oracle_backend(repo, accounts, clock=None, transport=None, settings=None, rss_fetcher=None, rss_interval=1800):
    """Every write path of the backend on one target and nothing else: RSS discovery and candidate import, food identity
    and alias review, tracking commands, planning, due slots, mock execution, B4 staging/DQ and B5 observations. Opens no
    SQLite database (B8 reads the same target through analytics_reader.TargetAnalyticsStore); checks the schema version,
    never migrates. RSS collects only through an injected mock fetcher (rss_fetcher); without one it refuses to fetch."""
    from types import SimpleNamespace
    from .oracle_execution import oracle_mock_controller
    from .oracle_foods import OracleFoodService
    from .oracle_rss import OracleRssService
    from .oracle_tracking import OracleTrackingService
    controller = oracle_mock_controller(repo, accounts, clock, transport, settings)
    execution = controller.execution
    foods = OracleFoodService(execution)
    controller.rss = OracleRssService(execution, foods, controller.observations, interval=rss_interval, fetcher=rss_fetcher)
    return SimpleNamespace(foods=foods, tracking=OracleTrackingService(execution), planner=execution.planner, rss=controller.rss,
                           execution=execution, ingestion=controller.ingestion, observations=controller.observations, controller=controller)


def oracle_maintenance(backend, at=None):
    """What --maintenance does on SQLite, for the target: B4 source retention and staging expiry, B5 link expiry, then the
    reprocessing of approved alias and food decisions (so alias changes reach B8). No HTTP, no DDL."""
    staging = backend.ingestion.purge(at)
    links = backend.observations.purge(at)
    effects = backend.observations.apply_entity_changes()
    return {'success': True, 'staging': staging, 'links': links, 'entity_changes_reprocessed': len(effects), 'api_requests': 0}


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
