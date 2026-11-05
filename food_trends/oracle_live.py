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

ORACLE-WRITER-RELEASE-1: release_writer() undoes a transfer under the same registry grant. The account is first marked
release-requested under its row lock, which stops every new enqueue, job start, request and live RSS fetch of it at once (the
runtime reads that row under the same lock); requests already sent finish and land their pages. Once no job of the account
holds a live lease and no RSS run is in flight, its remaining live jobs end CANCELLED with WRITER_RELEASED and the account
returns to IMPORTED. Every transfer, release request, release and usage handover is appended to writer_transfer_events
(deterministic ids, so a re-run adds nothing). The Oracle period's provider usage still inside a window goes to the legacy
SQLite call tables with usage_handover, which also decides when a legacy collector may run again; writer_status() shows it all.
"""
import json
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from .execution import ExecutionSettings
from .execution_runtime import LEGACY_SCHEDULER_FLAGS, _configured_accounts, _grants
from .food_service import FoodError, dump
from .oracle_execution import ACTIVE_STATES, WriterDraining, _states, _Tx, release_reasons, utc_value
from .query_planner import digest
from .target_db import iso, utc
from .target_schema import TARGET_SCHEMA_VERSION
from .tracking_service import stamp

RSS_SOURCE, RSS_SCOPE = 'google_trends_rss', 'public-feed'
LIVE_SOURCES = ('instagram_graph', 'youtube', 'naver_datalab', RSS_SOURCE)
FRESH_IMPORT = timedelta(minutes=15)  # the runtime's own limit on how old known legacy usage may be (OracleQuota.LEGACY_FRESH)
ACTIONS = ('TRANSFER', 'RELEASE_REQUESTED', 'RELEASED', 'USAGE_HANDOVER')  # the order of one transfer cycle


def _env(environ):
    return os.environ if environ is None else environ


def _exclusive(env):
    return tuple(s for s in env.get('B3_EXCLUSIVE_SOURCES', '').split(',') if s)


def _scope(source, accounts):
    if source == RSS_SOURCE:
        return RSS_SCOPE
    account = next((a for a in accounts if a.source_id == source), None)
    return account.account_scope if account else None


def account_scope(source):
    """The account scope a source writes under here (the configured Instagram user, local-default, or the RSS feed's)."""
    if source not in LIVE_SOURCES:
        raise FoodError('알 수 없는 수집원입니다.')
    return _scope(source, _configured_accounts()[1])


def _account_rows(repo):
    accounts = {(r['source_id'], r['account_scope']): r for r in repo.rows('quota_accounts')}
    blocks = {(r['source_id'], r['account_scope']): r for r in repo.rows('execution_blocks')}
    return accounts, blocks


def _grant(registry_path, source, scope, verb):
    grant = next((g for g in _grants(registry_path) if (g['source_id'], g['account_scope']) == (source, scope)), None) if registry_path else None
    if grant is None:
        raise FoodError('검토된 registry 승인이 없는 수집원은 Writer를 %s할 수 없습니다.' % verb, 403)
    return grant


def _history(t, source, scope):
    """The account's writer events, oldest first: by cycle, then the action's place in a cycle, then time."""
    rows = t.rows('SELECT * FROM writer_transfer_events WHERE source_id=:b_s AND account_scope=:b_a', 'writer_transfer_events', b_s=source, b_a=scope)
    return sorted(rows, key=lambda r: (r['cycle_no'], ACTIONS.index(r['action']), utc_value(r['occurred_at'])))


def _event(t, source, scope, cycle, action, now, approved_by, before, after, details, salt=()):
    """Append one writer event. Its id is deterministic, so the same step written twice stays one row."""
    event_id = 'WTE_' + digest([source, scope, cycle, action, *salt])[:40]
    t.insert_absent('writer_transfer_events', dict(event_id=event_id, source_id=source, account_scope=scope, cycle_no=cycle, action=action,
                                                   occurred_at=now, approved_by=approved_by[:1000], status_before=before, status_after=after,
                                                   details_json=dump(details)))
    return event_id


def writer_state(t, source, scope):
    """Where one account's writer is (reads only): LEGACY (never transferred), ORACLE (this runtime), RELEASING (a release requested
    and not finished, or a transfer withdrawn without the release command, which must still close it) or RELEASED; with the
    account row, the history, the current cycle and the account's live jobs still open."""
    row = t.one('SELECT * FROM quota_accounts WHERE source_id=:b_s AND account_scope=:b_a', 'quota_accounts', b_s=source, b_a=scope)
    history = _history(t, source, scope)
    last = history[-1]['action'] if history else None
    cycle = history[-1]['cycle_no'] if history else 1
    if row is not None and row['usage_status'] == 'EXCLUSIVE':
        state = 'ORACLE' if row['release_requested_at'] is None else 'RELEASING'
        if state == 'ORACLE' and last in ('RELEASED', 'USAGE_HANDOVER'):
            cycle += 1  # made exclusive again outside transfer_writer: a new cycle
    elif last in ('TRANSFER', 'RELEASE_REQUESTED'):
        state = 'RELEASING'
    else:
        state = 'RELEASED' if last else 'LEGACY'
    jobs = {r['status']: int(r['n']) for r in t.rows(f'''SELECT status,count(*) AS n FROM execution_jobs WHERE source_id=:b_s
        AND account_scope=:b_a AND exec_mode='live' AND status IN {_states(ACTIVE_STATES)} GROUP BY status''', b_s=source, b_a=scope)}
    events = {(e['cycle_no'], e['action']): e for e in history}
    return dict(state=state, account=row, history=history, cycle=cycle, active_live_jobs=jobs,
                transfer=events.get((cycle, 'TRANSFER')), released=events.get((cycle, 'RELEASED')))


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
        reasons += release_reasons(account)
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
    usage the legacy collector made is on the target. Writes one quota_accounts row under the runtime's ownership and
    appends a TRANSFER event (a new cycle) to writer_transfer_events; an account already transferred stays unchanged, and
    one whose release has not finished is refused. No HTTP. This is a G-OPS writer transfer: run it only with that approval."""
    from .runtime_ownership import RuntimeOwnership
    from .target_migration import writer_for
    env, now = _env(environ), utc(now or datetime.now(timezone.utc))
    if source not in LIVE_SOURCES:
        raise FoodError('알 수 없는 수집원입니다.')
    _, accounts = _configured_accounts()
    scope = _scope(source, accounts)
    grant = _grant(registry_path, source, scope, '이전')
    if source != RSS_SOURCE and env.get(LEGACY_SCHEDULER_FLAGS[source], 'true').lower() not in ('false', '0', 'no'):
        raise FoodError('기존 SQLite 수집기 스케줄러를 먼저 끄세요(%s=false).' % LEGACY_SCHEDULER_FLAGS[source], 409)
    ownership = RuntimeOwnership(repo)
    with ownership.held('RUNTIME_TEST') as lease, writer_for(repo, ownership, lease)() as tx:
        t = _Tx(tx)
        state = writer_state(t, source, scope)
        if state['state'] == 'RELEASING':
            raise FoodError('이 수집원의 Writer 철회가 끝나지 않았습니다. --writer-release로 철회를 마친 뒤 이전하세요.', 409)
        if state['state'] == 'ORACLE':
            return {'source_id': source, 'account_scope': scope, 'usage_status': 'EXCLUSIVE', 'cycle': state['cycle'], 'unchanged': True,
                    'http_requests': 0}
        current = state['account']
        if source != RSS_SOURCE and (current is None or current['usage_status'] == 'UNVERIFIED' or current['legacy_observed_through'] is None
                                     or utc(current['legacy_observed_through']) < now - FRESH_IMPORT):
            raise FoodError('기존 수집기의 마지막 사용량을 15분 이내에 가져온 뒤 이전하세요.', 409)
        observed = current['legacy_observed_through'] if current else iso(now)
        repo.upsert(tx, 'quota_accounts', dict(source_id=source, account_scope=scope, usage_status='EXCLUSIVE', legacy_observed_through=observed,
                                               evidence_ref=('writer_transfer:' + grant['approved_by'])[:1000], updated_at=iso(now),
                                               release_requested_at=None))
        cycle = state['history'][-1]['cycle_no'] + 1 if state['history'] else 1
        _event(t, source, scope, cycle, 'TRANSFER', now, grant['approved_by'], current['usage_status'] if current else 'NONE', 'EXCLUSIVE',
               {'legacy_observed_through': stamp(observed) if observed else None})
    return {'source_id': source, 'account_scope': scope, 'usage_status': 'EXCLUSIVE', 'cycle': cycle, 'http_requests': 0}


def _result(source, scope, state, status, unchanged=False):
    released = state['released']
    details = json.loads(released['details_json']) if released else {}
    return {'source_id': source, 'account_scope': scope, 'status': status, 'cycle': state['cycle'], 'unchanged': unchanged,
            'closed_jobs': len(details.get('closed_jobs', [])), 'interrupted_calls': details.get('interrupted_calls', 0),
            'open_usage': details.get('open_usage'), 'usage_handover': details.get('usage_handover'), 'http_requests': 0}


def _request_release(execution, source, scope, approved_by):
    """Release step 1, under the account lock: mark the release (no new request of the account starts from this commit on) and
    record it. Nothing else changes; a repeated request leaves the mark and its event as they are."""
    now = execution.now()
    with execution.transaction() as t:
        execution.quota.lock_row(t, source, scope)
        state = writer_state(t, source, scope)
        if state['state'] == 'RELEASED':
            return _result(source, scope, state, 'RELEASED', unchanged=True)
        if state['state'] == 'LEGACY':
            raise FoodError('Oracle 런타임으로 Writer를 이전한 적이 없는 수집원입니다.', 409)
        account = state['account']
        status = account['usage_status'] if account else 'NONE'
        if account is not None and account['release_requested_at'] is None:
            t.update('quota_accounts', {'source_id': source, 'account_scope': scope}, release_requested_at=now, updated_at=now)
        if (state['cycle'], 'RELEASE_REQUESTED') not in {(e['cycle_no'], e['action']) for e in state['history']}:
            _event(t, source, scope, state['cycle'], 'RELEASE_REQUESTED', now, approved_by, status, status,
                   {'active_live_jobs': state['active_live_jobs']})
    return {'source_id': source, 'account_scope': scope, 'status': 'RELEASE_REQUESTED', 'cycle': state['cycle'], 'http_requests': 0}


def _finish_release(execution, source, scope, approved_by):
    """Release steps 2-3 in one transaction under the account lock: DRAINING (nothing written) while a request of the account may
    be in flight; otherwise its remaining live jobs closed, the account IMPORTED and the open-window usage listed."""
    from .oracle_rss import OracleRssStore
    from .usage_handover import oracle_usage, summary
    now = execution.now()
    try:
        with execution.transaction() as t:
            execution.quota.lock_row(t, source, scope)
            state = writer_state(t, source, scope)
            if state['state'] == 'RELEASED':
                return _result(source, scope, state, 'RELEASED', unchanged=True)
            if source == RSS_SOURCE:
                OracleRssStore._lock(t)  # a live RSS start checks its writer under this lock
                running = [r['run_id'] for r in t.rows('SELECT run_id FROM rss_runs WHERE finished_at IS NULL ORDER BY run_id')]
                if running:
                    raise WriterDraining({'rss_runs': running})
                closed = {'closed_jobs': [], 'interrupted_calls': 0}
            else:
                closed = execution.close_released_account(t, source, scope)
            usage = oracle_usage(t, source, scope, now)
            account = state['account']
            if account is not None:
                t.update('quota_accounts', {'source_id': source, 'account_scope': scope}, usage_status='IMPORTED',
                         evidence_ref=('writer_release:' + approved_by)[:512], updated_at=now,
                         release_requested_at=account['release_requested_at'] or now)
            _event(t, source, scope, state['cycle'], 'RELEASED', now, approved_by, account['usage_status'] if account else 'NONE', 'IMPORTED',
                   dict(closed, open_usage=summary(usage), usage_handover='REQUIRED' if usage else 'NOT_NEEDED'))
            state = writer_state(t, source, scope)
    except WriterDraining as draining:
        return {'source_id': source, 'account_scope': scope, 'status': 'DRAINING', 'in_flight': draining.args[0], 'http_requests': 0}
    return _result(source, scope, state, 'RELEASED')


def release_writer(repo, source, registry_path, clock=None, wait_seconds=0, sleep=time.sleep):
    """Hand one source account's writer back from the Oracle runtime: the reverse of transfer_writer, fail-closed and resumable.

    1. Request: the account is marked release-requested under its row lock; from that commit on, no enqueue, job start, request
       or live RSS fetch of it begins, while a request already sent completes and lands its page.
    2. Drain: while a live job of the account holds a live lease, or an RSS run is unfinished (one older than STALE_AFTER is
       finished as interrupted first), the result is DRAINING and nothing else is written; run the command again, or pass
       wait_seconds to poll. An expired lease (a crashed worker) does not hold the release back.
    3. Release: in one transaction under the account lock, every live job of the account still open ends CANCELLED with
       WRITER_RELEASED (a call its crashed worker left SENT becomes UNKNOWN) and the account returns to IMPORTED with evidence
       writer_release:<approver>; legacy_observed_through keeps the last import, so a new transfer needs a fresh import.
    Each step appends to writer_transfer_events; a re-run of a finished release changes nothing. Committed pages and observations
    stay. The open-window provider usage of the Oracle period is listed in the RELEASED event: the legacy collector of the source
    must not resume until usage_handover has handed it over or its windows have ended. Requires the registry grant of the account
    (stopping needs no live switch); refused on a production target and while Shadow owns the target. No HTTP."""
    from .oracle_execution import OracleExecution
    from .oracle_rss import OracleRssStore
    if source not in LIVE_SOURCES:
        raise FoodError('알 수 없는 수집원입니다.')
    settings = getattr(repo.db, 'settings', None)
    if settings is not None and settings.environment == 'production':
        raise FoodError('운영 Oracle의 Writer는 별도 승인 전까지 바꿀 수 없습니다(ORACLE_PRODUCTION_LOCKED).', 403)
    _, accounts = _configured_accounts()
    scope = _scope(source, accounts)
    approved_by = _grant(registry_path, source, scope, '철회')['approved_by']
    # The released account's jobs are closed on the account the live assembly used (granted), so their slots name the release.
    accounts = [replace(a, enabled=True, permission_verified=True, policy_allowed=True) if (a.source_id, a.account_scope) == (source, scope)
                else a for a in accounts]
    execution = OracleExecution(repo, accounts, clock)  # mock settings: it closes jobs and writes bookkeeping, never a request
    try:
        result = _request_release(execution, source, scope, approved_by)
        deadline = time.monotonic() + max(0, wait_seconds)
        while result['status'] != 'RELEASED':
            if source == RSS_SOURCE:
                OracleRssStore(execution).recover_interrupted()
            result = _finish_release(execution, source, scope, approved_by)
            if result['status'] == 'RELEASED' or time.monotonic() >= deadline:
                break
            sleep(1)
        return result
    finally:
        execution.release()


def writer_status(repo, now=None):
    """Read-only, per source account: where its writer is (LEGACY, ORACLE, RELEASING, RELEASED), the transfer history, live jobs,
    leases and calls still open, RSS runs in flight and the Oracle usage in open windows a legacy collector would have to count.
    No write, no HTTP."""
    from .usage_handover import oracle_usage, summary
    now = utc(now or datetime.now(timezone.utc))
    _, accounts = _configured_accounts()
    sources = {}
    with repo.db.transaction() as cursor:
        t = _Tx(cursor)
        for source in LIVE_SOURCES:
            scope = _scope(source, accounts)
            state = writer_state(t, source, scope)
            account = state['account']
            where = dict(b_s=source, b_a=scope)
            leases = t.scalar(f'''SELECT count(*) AS c FROM execution_leases l JOIN execution_jobs j ON j.job_id=l.resource_key
                WHERE j.source_id=:b_s AND j.account_scope=:b_a AND j.exec_mode='live' AND j.status IN {_states(ACTIVE_STATES)}
                AND l.expires_at>:b_now''', b_now=now, **where)
            sent = t.scalar('''SELECT count(*) AS c FROM execution_calls c JOIN execution_jobs j ON j.job_id=c.job_id
                WHERE j.source_id=:b_s AND j.account_scope=:b_a AND c.status='SENT' ''', **where)
            sources[source] = {'account_scope': scope, 'writer': state['state'], 'cycle': state['cycle'],
                               'usage_status': account['usage_status'] if account else None,
                               'release_requested_at': account['release_requested_at'] if account else None,
                               'events': [{'cycle': e['cycle_no'], 'action': e['action'], 'occurred_at': e['occurred_at'],
                                           'approved_by': e['approved_by']} for e in state['history']],
                               'active_live_jobs': state['active_live_jobs'], 'live_leases': int(leases), 'unfinished_calls': int(sent),
                               'open_usage': summary(oracle_usage(t, source, scope, now))}
        sources[RSS_SOURCE]['unfinished_rss_runs'] = [r['run_id'] for r in t.rows('SELECT run_id FROM rss_runs WHERE finished_at IS NULL ORDER BY run_id')]
    settings = getattr(repo.db, 'settings', None)
    return {'sources': sources, 'storage': 'oracle_' + settings.environment if settings is not None else 'sqlite_target_standin',
            'checked_at': stamp(now), 'http_requests': 0, 'database_writes': 0}


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
