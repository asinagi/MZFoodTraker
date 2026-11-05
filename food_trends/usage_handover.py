"""ORACLE-WRITER-RELEASE-1: the Oracle period's provider usage handed back to the legacy SQLite collectors.

After a writer release (oracle_live.release_writer) a legacy collector may become the writer again. The legacy collectors count
their budget only from their own call tables (youtube_calls per Pacific day, naver_calls per KST day, instagram_searches over a
rolling 7 days; the SQLite B3 runtime reads the same tables), so every request the Oracle runtime sent for the account in a
window still open has to be there first, or the two writers together could pass a provider limit.

- handover() writes one legacy row per Oracle live call (YouTube search/videos, the NAVER DataLab request) and one per hashtag
  the Oracle runtime newly charged (Instagram), marked as the Oracle's (youtube_calls.run_id and naver_calls.fingerprint
  'oracle:<call id>'; a hashtag row is its user, name and search time, the latest instant the provider can have seen the
  search), so a re-run inserts nothing. A call with an unknown outcome (a crashed worker) becomes 'unknown' and counts like the
  others: nothing is under-counted, nothing is made PROVIDER_CONFIRMED, and no request body, keyword or value is copied. Only
  the account's own rows are added; other sources and accounts keep their state. Each handover that added rows is recorded as
  a USAGE_HANDOVER event on the target. RSS has no provider budget; its discovery returns with reverse recovery.
- resume_readiness() decides, reading both stores only, whether the legacy collector of a source may be switched on again: the
  Oracle writer released with no live job left, every open-window Oracle usage present in SQLite, the legacy usage the target
  imported before the transfer still present there, no active provider limit or credential block, the target's RSS runs
  recovered into SQLite (reverse recovery) with none unfinished, and SQLite ownership not held by B3. Usage missing from SQLite
  keeps the answer at no until its window ends (resume_not_before): the legacy collector stays off rather than guess.
- The operational database is refused unless the separately approved switch is given, as for reverse recovery. No HTTP.
"""
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .execution_runtime import LEGACY_SCHEDULER_FLAGS
from .food_service import FoodError
from .oracle_execution import _Tx, utc_value
from .oracle_live import RSS_SOURCE, _event, account_scope, writer_state
from .quota import daily_window
from .quota_ledger import UNIQUE_TAG_DAYS
from .target_db import utc
from .tracking_service import instant, stamp

TABLES = {'youtube': 'youtube_calls', 'naver_datalab': 'naver_calls', 'instagram_graph': 'instagram_searches'}
ZONES = {'youtube': 'America/Los_Angeles', 'naver_datalab': 'Asia/Seoul'}
ENDPOINTS = {'search': 'youtube.search_calls', 'videos': 'youtube.video_calls', 'naver_search': 'naver.calls'}
CALL_STATUS = {'SUCCEEDED': 'success', 'SENT': 'unknown', 'UNKNOWN': 'unknown'}  # a FAILED call keeps its error kind
LEGACY_TIME = {'youtube_calls': 'started_at', 'naver_calls': 'requested_at', 'instagram_searches': 'searched_at'}  # quota_ledger's observed_at
MARK = 'oracle:'


def oracle_usage(t, source, scope, now):
    """The Oracle runtime's live provider usage of one account whose window is still open, as the legacy rows that carry it.
    Mock jobs used no provider and an ended window cannot affect a budget. Reads only."""
    if source not in TABLES:
        return []
    calls = t.rows('''SELECT c.call_id,c.reservation_id,c.endpoint,c.started_at,c.finished_at,c.status,c.http_status,c.error_kind,
        c.returned_count FROM execution_calls c JOIN execution_jobs j ON j.job_id=c.job_id WHERE j.source_id=:b_s AND j.account_scope=:b_a
        AND j.exec_mode='live' ORDER BY c.call_id''', b_s=source, b_a=scope)
    return _call_usage(calls, ZONES[source], instant(now)) if source in ZONES else _tag_usage(t, calls, scope, instant(now))


def _number(value):
    return int(value) if value is not None else None


def _call_usage(calls, zone, now):
    """YouTube and NAVER: one row per call sent, in the provider's daily window (Pacific or KST) of its start."""
    out = []
    for c in calls:
        started = utc_value(c['started_at'])
        window = daily_window(started, zone)
        if c['endpoint'] not in ENDPOINTS or instant(window['end']) <= now:
            continue
        out.append(dict(key=MARK + c['call_id'], quota_type=ENDPOINTS[c['endpoint']], window_end=window['end'], day=window['day'],
                        endpoint=c['endpoint'], started_at=stamp(started),
                        finished_at=stamp(utc_value(c['finished_at'])) if c['finished_at'] is not None else None,
                        status=CALL_STATUS.get(c['status']) or c['error_kind'] or 'failed', http_status=_number(c['http_status']),
                        result_count=_number(c['returned_count'])))
    return out


def _tag_usage(t, calls, scope, now):
    """Instagram: one row per hashtag a reservation newly charged (cost > 0), in the 7 days from its search."""
    out, moments = [], {}
    for c in calls:
        seen = [utc_value(v) for v in (c['started_at'], c['finished_at']) if v is not None]
        entry = moments.setdefault(c['reservation_id'], {'lookup': [], 'any': []})
        entry['any'] += seen
        if c['endpoint'] == 'hashtag_lookup':
            entry['lookup'] += seen
    for reservation, entry in sorted(moments.items()):
        for charge in t.rows("""SELECT distinct_key FROM quota_charges WHERE reservation_id=:b_r AND resource_code='instagram.unique_tags'
                AND cost>0""", b_r=reservation):
            searched = max(entry['lookup'] or entry['any'])  # the latest instant the provider can have seen it: the window never ends early
            end = searched + timedelta(days=UNIQUE_TAG_DAYS)
            if end <= now:
                continue
            name = unicodedata.normalize('NFC', charge['distinct_key'])
            out.append(dict(key='%s%s:%s' % (MARK, reservation, name), quota_type='instagram.unique_tags', window_end=stamp(end),
                            user_id=scope[3:], name=name, searched_at=stamp(searched)))
    return out


def summary(usage):
    """Counts per quota type, how many have an unknown outcome and when the last open window ends."""
    by_type = {}
    for row in usage:
        by_type[row['quota_type']] = by_type.get(row['quota_type'], 0) + 1
    return {'rows': len(usage), 'by_quota_type': by_type, 'uncertain': sum(r.get('status') == 'unknown' for r in usage),
            'open_until': max((r['window_end'] for r in usage), key=instant, default=None)}


def legacy_known(t, source, scope, now):
    """Legacy usage the target imported before the transfer (quota_usage_ledger) whose window is still open."""
    rows = t.rows('SELECT * FROM quota_usage_ledger WHERE source_id=:b_s AND account_scope=:b_a', 'quota_usage_ledger', b_s=source, b_a=scope)
    return [r for r in rows if instant(r['window_end']) > instant(now)]


def _present(db, source, row):
    if source == 'youtube':
        return db.execute('SELECT 1 FROM youtube_calls WHERE run_id=?', (row['key'],)).fetchone() is not None
    if source == 'naver_datalab':
        return db.execute('SELECT 1 FROM naver_calls WHERE fingerprint=?', (row['key'],)).fetchone() is not None
    # A row of the same hashtag in the 7 days up to this search: the provider's window for it was already open, and stays as is.
    at = instant(row['searched_at'])
    return any(at - timedelta(days=UNIQUE_TAG_DAYS) < instant(r[0]) <= at
               for r in db.execute('SELECT searched_at FROM instagram_searches WHERE user_id=? AND name=?', (row['user_id'], row['name'])))


def _insert(db, source, row):
    if source == 'youtube':
        db.execute('''INSERT INTO youtube_calls(day,endpoint,query_id,started_at,finished_at,status,http_status,origin,run_id,result_count)
            VALUES (?,?,NULL,?,?,?,?,'live_api',?,?)''', (row['day'], row['endpoint'], row['started_at'], row['finished_at'], row['status'],
                                                         row['http_status'], row['key'], row['result_count']))
    elif source == 'naver_datalab':
        db.execute('''INSERT INTO naver_calls(day,fingerprint,requested_at,finished_at,status,http_status,request_json,candidate_ids,origin)
            VALUES (?,?,?,?,?,?,'{}','[]','live_api')''', (row['day'], row['key'], row['started_at'], row['finished_at'], row['status'],
                                                          row['http_status']))
    else:
        db.execute('INSERT INTO instagram_searches(user_id,name,instagram_id,searched_at) VALUES (?,?,NULL,?)',
                   (row['user_id'], row['name'], row['searched_at']))


def _legacy_present(db, row):
    """The SQLite row a ledger entry was imported from (source_ref table:id), still there with the same time: an id alone could
    be another file's row."""
    table, _, number = row['source_ref'].partition(':')
    if table not in LEGACY_TIME or not number.isdigit():
        return False
    found = db.execute('SELECT %s FROM %s WHERE id=?' % (LEGACY_TIME[table], table), (int(number),)).fetchone()
    try:
        return found is not None and instant(found[0]) == instant(row['observed_at'])
    except FoodError:  # a time without a zone is not the row that was imported
        return False


def _period(state):
    """When the Oracle runtime was the writer in the current cycle (transfer to release), if both are recorded."""
    if state['transfer'] and state['released']:
        return instant(state['transfer']['occurred_at']), instant(state['released']['occurred_at'])
    return None


def _legacy_during(db, source, scope, period, handed):
    """Rows the legacy collector wrote while the Oracle runtime was the writer: its stop did not hold (reported, never merged)."""
    if source == 'youtube':
        times = [r[0] for r in db.execute("SELECT started_at FROM youtube_calls WHERE run_id IS NULL OR run_id NOT LIKE 'oracle:%'")]
    elif source == 'naver_datalab':
        times = [r[0] for r in db.execute("SELECT requested_at FROM naver_calls WHERE fingerprint NOT LIKE 'oracle:%'")]
    else:
        times = [r[1] for r in db.execute('SELECT name,searched_at FROM instagram_searches WHERE user_id=?', (scope[3:],))
                 if (r[0], r[1]) not in handed]
    return sum(period[0] <= instant(v) <= period[1] for v in times if v)


def _destination(destination, allow_operational):
    from .migrations import LATEST_VERSION, schema_version
    from .reverse_recovery import RecoveryRefused, _operational
    destination = Path(destination).resolve()
    if not allow_operational and _operational(destination):
        raise RecoveryRefused('operational_database_requires_approval')  # the real rollback needs the separately approved switch
    if not destination.is_file() or schema_version(str(destination)) != LATEST_VERSION:
        raise FoodError('SQLite v%d 데이터베이스가 필요합니다(DESTINATION_SCHEMA_OUTDATED).' % LATEST_VERSION, 409)
    return destination


def handover(repo, source, destination, now=None, allow_operational=False):
    """Write the Oracle period's open-window provider usage of one released source account into the legacy call tables of a
    SQLite v11 database (see the module docstring). Refused while the Oracle runtime is still (or again) the writer or a live job
    of the account is open, for the operational database without allow_operational, and on a production or Shadow-owned target,
    before any write. Idempotent; the SQLite rows commit together. Counts only."""
    from .runtime_ownership import RuntimeOwnership
    from .target_migration import writer_for
    scope = account_scope(source)
    destination = _destination(destination, allow_operational)
    now = utc(now or datetime.now(timezone.utc))
    ownership = RuntimeOwnership(repo)
    with ownership.held('RUNTIME_TEST') as lease:  # production and Shadow-owned targets are refused here
        writer = writer_for(repo, ownership, lease)
        with writer() as tx:
            t = _Tx(tx)
            state = writer_state(t, source, scope)
            usage, legacy = oracle_usage(t, source, scope, now), legacy_known(t, source, scope, now)
        if state['state'] in ('ORACLE', 'RELEASING') or state['active_live_jobs']:
            raise FoodError('Oracle Writer 철회를 먼저 끝내세요(ORACLE_WRITER_NOT_RELEASED).', 409)
        result = {'source_id': source, 'account_scope': scope, 'destination': destination.name, 'http_requests': 0}
        if source not in TABLES:
            return dict(result, handed_over=0, already_present=0, open_usage=summary([]), legacy_missing=0,
                        legacy_writes_during_oracle_period=0, resume_not_before=None)
        handed = {(r['name'], r['searched_at']) for r in usage} if source == 'instagram_graph' else set()
        with closing(sqlite3.connect(str(destination), timeout=30)) as db:
            db.execute('BEGIN IMMEDIATE')
            missing = [row for row in legacy if not _legacy_present(db, row)]
            fresh = [row for row in usage if not _present(db, source, row)]
            for row in fresh:
                _insert(db, source, row)
            period = _period(state)
            during = _legacy_during(db, source, scope, period, handed) if period else 0
            db.commit()
        recorded = any(e['cycle_no'] == state['cycle'] and e['action'] == 'USAGE_HANDOVER' for e in state['history'])
        if usage and (fresh or not recorded):
            status = state['account']['usage_status'] if state['account'] else 'NONE'
            approved_by = state['released']['approved_by'] if state['released'] else 'operator'
            with writer() as tx:
                _event(_Tx(tx), source, scope, state['cycle'], 'USAGE_HANDOVER', now, approved_by, status, status,
                       dict(summary(usage), handed_over=len(fresh), destination=destination.name),
                       salt=sorted(r['key'] for r in fresh or usage))
    return dict(result, handed_over=len(fresh), already_present=len(usage) - len(fresh), open_usage=summary(usage),
                legacy_missing=len(missing), legacy_writes_during_oracle_period=during,
                resume_not_before=max((r['window_end'] for r in missing), key=instant, default=None))


def _recovered(db, runs):
    """Every target RSS run is in SQLite runs under its number and start time (reverse recovery wrote it back)."""
    found = {r[0]: r[1] for r in db.execute('SELECT id,started_at FROM runs')}
    try:
        return all(r['run_id'] in found and instant(found[r['run_id']]) == instant(r['started_at']) for r in runs)
    except FoodError:  # a start time without a zone is not the target's run
        return False


def resume_readiness(repo, source, destination, now=None):
    """Read-only: may the legacy SQLite collector of `source` be switched on again over `destination`? (See the module docstring.)
    Codes only; resume_not_before is when the time-bound blockers lapse by themselves. No write, no HTTP."""
    from .migrations import LATEST_VERSION, schema_version
    from .target_migration import open_source
    scope = account_scope(source)
    destination = Path(destination).resolve()
    now = utc(now or datetime.now(timezone.utc))
    blockers, waits, missing = [], [], []
    with repo.db.transaction() as tx:
        t = _Tx(tx)
        state = writer_state(t, source, scope)
        usage, legacy = oracle_usage(t, source, scope, now), legacy_known(t, source, scope, now)
        block = t.one('SELECT * FROM execution_blocks WHERE source_id=:b_s AND account_scope=:b_a', 'execution_blocks', b_s=source, b_a=scope)
        runs = t.rows('SELECT run_id,started_at,finished_at FROM rss_runs ORDER BY run_id', 'rss_runs') if source == RSS_SOURCE else []
    if state['state'] in ('ORACLE', 'RELEASING'):
        blockers.append('ORACLE_WRITER_NOT_RELEASED')
    if state['active_live_jobs']:
        blockers.append('ORACLE_LIVE_JOBS_ACTIVE')
    if block and block['until_at'] is None:
        blockers.append('ACCOUNT_BLOCKED_' + block['error_kind'])  # credentials or permission to review before any collector runs
    elif block and instant(block['until_at']) > now:
        blockers.append('PROVIDER_LIMIT_ACTIVE')
        waits.append(block['until_at'])
    if any(r['finished_at'] is None for r in runs):
        blockers.append('ORACLE_RSS_RUN_UNFINISHED')
    if not destination.is_file() or schema_version(str(destination)) != LATEST_VERSION:
        blockers.append('DESTINATION_SCHEMA_OUTDATED')
    else:
        with closing(open_source(destination)) as db:
            missing = [r for r in usage if not _present(db, source, r)]
            lost = [r for r in legacy if not _legacy_present(db, r)]
            owner = db.execute('SELECT owner FROM execution_source_ownership WHERE source_id=? AND account_scope=?', (source, scope)).fetchone()
            recovered = _recovered(db, runs)
        if missing:
            blockers.append('ORACLE_USAGE_NOT_HANDED_OVER')
            waits += [r['window_end'] for r in missing]
        if lost:
            blockers.append('DESTINATION_MISSING_LEGACY_USAGE')
            waits += [r['window_end'] for r in lost]
        if owner and owner[0] == 'B3':
            blockers.append('SQLITE_OWNED_BY_B3')
        if not recovered:
            blockers.append('ORACLE_RSS_NOT_RECOVERED')
    return {'source_id': source, 'account_scope': scope, 'ready': not blockers, 'blockers': blockers,
            'resume_not_before': stamp(max(waits, key=instant)) if waits else None, 'writer': state['state'],
            'oracle_usage': {'open': len(usage), 'not_in_sqlite': len(missing)},
            'legacy_switch': LEGACY_SCHEDULER_FLAGS.get(source, 'sqlite_server_rss_scheduler'),
            'checked_at': stamp(now), 'http_requests': 0, 'database_writes': 0}
