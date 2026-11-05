"""ORACLE-LIVE-INTEGRATION-1: the live collection path of the Oracle runtime, fail-closed and off by default.

The existing provider clients (instagram_graph.GraphClient, youtube.YouTubeClient, naver_datalab.NaverClient) and the
RSS collector (collector.fetch) run unchanged through the Oracle executors and the Oracle RSS service; only the
assembly differs from the mock one: the real clients, live accounts carrying a reviewed registry grant, live
execution settings, verified external usage, and every run labelled live_api. Nothing here sends a request:
oracle_live_readiness() only reads, transfer_writer() writes one account row, and oracle_live_backend() refuses to
assemble anything unless readiness passes.

A live source needs all of: (1) a grant in the reviewed registry (permission and policy verified, approver, evidence);
(2) B3_LIVE_ENABLED=true and the source listed in B3_EXCLUSIVE_SOURCES; (3) credentials configured (never echoed); (4) its
legacy SQLite scheduler off (its *_ENABLED flag false), so the SQLite collector does not run beside it; (5) a writer
transfer on the target (quota_accounts EXCLUSIVE, written by transfer_writer right after a fresh legacy usage import, so
the legacy collector's last usage is counted); (6) no active block. The target itself must not be production nor owned by
Shadow. The RSS feed (public, no credentials, scope 'public-feed') follows the same gates except (3) and (4); the legacy
RSS scheduler has no flag, so its stop is what the writer transfer records. The same gates run again at every enqueue,
before every first request of a job and before every live RSS collection.
"""
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from .execution import ExecutionSettings
from .execution_runtime import LEGACY_SCHEDULER_FLAGS, _configured_accounts, _grants
from .food_service import FoodError
from .target_db import iso, utc
from .target_schema import TARGET_SCHEMA_VERSION

RSS_SOURCE, RSS_SCOPE = 'google_trends_rss', 'public-feed'
LIVE_SOURCES = ('instagram_graph', 'youtube', 'naver_datalab', RSS_SOURCE)
FRESH_IMPORT = timedelta(minutes=15)  # the runtime's own limit on how old known legacy usage may be (OracleQuota.LEGACY_FRESH)


def _env(environ):
    return os.environ if environ is None else environ


def _exclusive(env):
    return tuple(s for s in env.get('B3_EXCLUSIVE_SOURCES', '').split(',') if s)


def _scope(source, accounts):
    if source == RSS_SOURCE:
        return RSS_SCOPE
    account = next((a for a in accounts if a.source_id == source), None)
    return account.account_scope if account else None


def _account_rows(repo):
    accounts = {(r['source_id'], r['account_scope']): r for r in repo.rows('quota_accounts')}
    blocks = {(r['source_id'], r['account_scope']): r for r in repo.rows('execution_blocks')}
    return accounts, blocks


def oracle_live_readiness(repo, registry_path, environ=None, now=None):
    """Read-only, fail-closed Live gate for the Oracle runtime. Builds no client, sends no HTTP, writes nothing.
    Error codes only; credential values and registry evidence are never echoed."""
    from .runtime_ownership import RuntimeOwnership
    env, now = _env(environ), utc(now or datetime.now(timezone.utc))
    blockers = []
    settings = getattr(repo.db, 'settings', None)
    if settings is not None and settings.environment == 'production':
        blockers.append('ORACLE_PRODUCTION_LOCKED')
    version = repo.schema_version()
    if version != TARGET_SCHEMA_VERSION:
        blockers.append('TARGET_SCHEMA_VERSION_MISMATCH')
    elif RuntimeOwnership(repo).status()['mode'] == 'SHADOW':
        blockers.append('TARGET_OWNED_BY_SHADOW')
    if env.get('B3_LIVE_ENABLED', 'false').lower() != 'true':
        blockers.append('B3_LIVE_DISABLED')
    try:
        grants = {(g['source_id'], g['account_scope']) for g in _grants(registry_path)} if registry_path else None
    except (OSError, ValueError, FoodError):
        grants = None
    if grants is None:
        blockers.append('REGISTRY_MISSING_OR_INVALID')
    exclusive = _exclusive(env)
    if not exclusive:
        blockers.append('NO_EXCLUSIVE_SOURCE')
    try:
        _, accounts = _configured_accounts()
    except (ValueError, OverflowError):  # a malformed provider setting: no account of it is usable
        blockers.append('PROVIDER_CONFIG_INVALID')
        accounts = []
    rows, blocks = _account_rows(repo) if version == TARGET_SCHEMA_VERSION else ({}, {})
    sources = {}
    for source in exclusive:
        scope = _scope(source, accounts) if source in LIVE_SOURCES else None
        if scope is None:
            sources[source] = {'ready': False, 'blockers': ['UNKNOWN_SOURCE']}
            continue
        reasons = []
        if not grants or (source, scope) not in grants:
            reasons.append('NO_REGISTRY_GRANT')
        if source != RSS_SOURCE:
            if not next(a for a in accounts if a.source_id == source).configured:
                reasons.append('NOT_CONFIGURED')
            if env.get(LEGACY_SCHEDULER_FLAGS[source], 'true').lower() not in ('false', '0', 'no'):
                reasons.append('LEGACY_SCHEDULER_ENABLED')
        account = rows.get((source, scope))
        if account is None or account['usage_status'] != 'EXCLUSIVE':
            reasons.append('LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED')
        block = blocks.get((source, scope))
        if block and (block['until_at'] is None or utc(block['until_at']) > now):
            reasons.append('ACCOUNT_BLOCKED_' + block['error_kind'])
        sources[source] = {'account_scope': scope, 'ready': not reasons, 'blockers': reasons}
    ready = not blockers and bool(sources) and all(s['ready'] for s in sources.values())
    return {'ready': ready, 'blockers': blockers, 'sources': sources, 'schema_version': version, 'required_version': TARGET_SCHEMA_VERSION,
            'automatic_requested': env.get('B3_AUTOMATIC_ENABLED', 'false').lower() == 'true',
            'storage': 'oracle_' + settings.environment if settings is not None else 'sqlite_target_standin',
            'http_requests': 0, 'database_writes': 0}


def transfer_writer(repo, source, registry_path, environ=None, now=None):
    """Record that the Oracle runtime is now the only writer of one source account (its legacy SQLite collector stopped).

    Refused unless the registry grants the account, the legacy scheduler flag is off and, for an API account, the
    legacy usage was imported within the last 15 minutes (run --target-load or the usage import first), so the last
    usage the legacy collector made is on the target. Writes one quota_accounts row under the runtime's ownership;
    no HTTP. This is a G-OPS writer transfer: run it only with that approval."""
    from .runtime_ownership import RuntimeOwnership
    from .target_migration import writer_for
    env, now = _env(environ), utc(now or datetime.now(timezone.utc))
    if source not in LIVE_SOURCES:
        raise FoodError('알 수 없는 수집원입니다.')
    _, accounts = _configured_accounts()
    scope = _scope(source, accounts)
    grant = next((g for g in _grants(registry_path) if (g['source_id'], g['account_scope']) == (source, scope)), None) if registry_path else None
    if grant is None:
        raise FoodError('검토된 registry 승인이 없는 수집원은 Writer를 이전할 수 없습니다.', 403)
    if source != RSS_SOURCE and env.get(LEGACY_SCHEDULER_FLAGS[source], 'true').lower() not in ('false', '0', 'no'):
        raise FoodError('기존 SQLite 수집기 스케줄러를 먼저 끄세요(%s=false).' % LEGACY_SCHEDULER_FLAGS[source], 409)
    rows, _ = _account_rows(repo)
    current = rows.get((source, scope))
    if source != RSS_SOURCE and (current is None or current['usage_status'] == 'UNVERIFIED' or current['legacy_observed_through'] is None
                                 or utc(current['legacy_observed_through']) < now - FRESH_IMPORT):
        raise FoodError('기존 수집기의 마지막 사용량을 15분 이내에 가져온 뒤 이전하세요.', 409)
    ownership = RuntimeOwnership(repo)
    with ownership.held('RUNTIME_TEST') as lease, writer_for(repo, ownership, lease)() as tx:
        repo.upsert(tx, 'quota_accounts', dict(source_id=source, account_scope=scope, usage_status='EXCLUSIVE',
                                               legacy_observed_through=current['legacy_observed_through'] if current else iso(now),
                                               evidence_ref=('writer_transfer:' + grant['approved_by'])[:1000], updated_at=iso(now)))
    return {'source_id': source, 'account_scope': scope, 'usage_status': 'EXCLUSIVE', 'http_requests': 0}


def oracle_live_backend(repo, registry_path, environ=None, clock=None, rss_interval=1800, rss_timeout=20):
    """The live assembly of the Oracle runtime: oracle_backend's services with the real clients of the ready sources.
    Refuses (FoodError 403, codes only) unless oracle_live_readiness passes. Starts nothing and sends nothing."""
    from .collector import fetch
    from .instagram_graph import GraphClient
    from .naver_datalab import NaverClient
    from .oracle_execution import OracleExecution, oracle_adapters
    from .oracle_foods import OracleFoodService
    from .oracle_ingestion import OracleIngestionService
    from .oracle_observations import OracleObservationService
    from .oracle_planner import OracleQueryPlanner
    from .oracle_rss import OracleExecutionController, OracleRssService
    from .oracle_tracking import OracleTrackingService
    from .youtube import YouTubeClient
    readiness = oracle_live_readiness(repo, registry_path, environ)
    if not readiness['ready']:
        codes = sorted(set(readiness['blockers'] + [c for s in readiness['sources'].values() for c in s['blockers']]))
        raise FoodError('Oracle Live 실행 준비 조건 미충족: ' + ', '.join(codes), 403)
    configs, accounts = _configured_accounts()
    live = {(s, v['account_scope']) for s, v in readiness['sources'].items() if s != RSS_SOURCE}
    # The legacy scheduler flags are off for these sources, so enablement comes from the reviewed grant.
    accounts = [replace(a, enabled=True, permission_verified=True, policy_allowed=True) if (a.source_id, a.account_scope) in live else a
                for a in accounts]
    settings = ExecutionSettings(live_enabled=True, automatic=readiness['automatic_requested'], exclusive_live_sources=tuple(readiness['sources']))
    execution = OracleExecution(repo, accounts, clock, settings, require_verified_usage=True, live_readiness=readiness)
    OracleQueryPlanner(execution)
    classes = {'instagram_graph': GraphClient, 'youtube': YouTubeClient, 'naver_datalab': NaverClient}
    clients = {(a.source_id, a.account_scope): classes[a.source_id](configs[a.source_id]) for a in accounts if (a.source_id, a.account_scope) in live}
    ingestion, observations = OracleIngestionService(execution), OracleObservationService(execution)
    controller = OracleExecutionController(execution, oracle_adapters(clients), ingestion, observations)
    foods = OracleFoodService(execution)
    if RSS_SOURCE in readiness['sources']:
        def gate():
            again = oracle_live_readiness(repo, registry_path, environ, execution.now())
            return again['blockers'] + again['sources'].get(RSS_SOURCE, {'blockers': ['NOT_EXCLUSIVE']})['blockers']
        controller.rss = OracleRssService(execution, foods, observations, rss_interval, rss_timeout, fetch, 'live_api', gate)
    else:
        controller.rss = OracleRssService(execution, foods, observations, rss_interval, rss_timeout)  # refuses to fetch
    return SimpleNamespace(foods=foods, tracking=OracleTrackingService(execution), planner=execution.planner, rss=controller.rss,
                           execution=execution, ingestion=ingestion, observations=observations, controller=controller, readiness=readiness)
