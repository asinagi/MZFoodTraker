"""ORACLE-RUNTIME-2 R4: budget-relevant usage of the legacy collectors, copied to the target with its uncertainty.

The SQLite collectors record their own calls (youtube_calls, naver_calls, instagram_searches); the Oracle
runtime cannot see them otherwise. This importer copies only what budget safety needs: which source and
account scope, which quota and window, one unit per recorded call or unique hashtag search, the
original origin label and how certain the debit is. No request body, query text or response is copied.

Evidence is never upgraded: nothing here is PROVIDER_CONFIRMED (no provider reports usage to us);
- LEGACY_LOCAL_COUNT     a completed call the collector counted (provider debit not confirmed)
- LEGACY_SENT_UNCERTAIN  a call left 'running' (sent or about to be sent, outcome unknown)
- LEGACY_UNKNOWN_ORIGIN  a row whose origin is legacy_unknown
Every kind is counted against the budget (conservative). Usage of a different account scope (another
Instagram user) is kept under that scope, never merged. Each import stamps quota_accounts with the
time through which legacy usage is known; the runtime refuses to rely on older knowledge.
Only rows whose window is still open are copied (older ones cannot affect a budget).
"""
import hashlib
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from .quota import KST, daily_window
from .target_db import iso, utc
from .tracking_service import instant, stamp

LEGACY_SCOPES = (('youtube', 'local-default'), ('naver_datalab', 'local-default'))
EVIDENCE = ('PROVIDER_CONFIRMED', 'LEGACY_LOCAL_COUNT', 'LEGACY_SENT_UNCERTAIN', 'LEGACY_UNKNOWN_ORIGIN')
UNIQUE_TAG_DAYS = 7


def day_window(day, zone):
    """The provider/local daily window of a recorded day string ('YYYY-MM-DD') in its reset time zone."""
    y, m, d = (int(v) for v in day.split('-'))
    probe = datetime(y, m, d, 12, tzinfo=KST) if zone == 'Asia/Seoul' else datetime(y, m, d, 20, tzinfo=timezone.utc)
    return daily_window(probe, zone)


def _key(table, row_id):
    return hashlib.sha256(json.dumps(['legacy_usage', table, row_id]).encode()).hexdigest()


def _evidence(status, finished, origin):
    if origin == 'legacy_unknown':
        return 'LEGACY_UNKNOWN_ORIGIN', 'UNKNOWN'
    if status == 'running' or finished is None:
        return 'LEGACY_SENT_UNCERTAIN', 'UNCERTAIN'
    return 'LEGACY_LOCAL_COUNT', 'COUNTED'


def legacy_rows(src, now):
    """Ledger rows from the SQLite legacy call tables (open windows only)."""
    now = utc(now)
    out = []
    columns = {t: {r[1] for r in src.execute('PRAGMA table_info(%s)' % t)} for t in ('youtube_calls', 'naver_calls', 'instagram_searches')}
    for r in src.execute('SELECT * FROM youtube_calls ORDER BY id'):
        window = day_window(r['day'], 'America/Los_Angeles')
        if instant(window['end']) <= now:
            continue
        evidence, confidence = _evidence(r['status'], r['finished_at'], r['origin'] if 'origin' in columns['youtube_calls'] else 'legacy_unknown')
        out.append(dict(ledger_key=_key('youtube_calls', r['id']), source_id='youtube', account_scope='local-default',
                        quota_type='youtube.search_calls' if r['endpoint'] == 'search' else 'youtube.video_calls', window_start=window['start'],
                        window_end=window['end'], provider_timezone='America/Los_Angeles', usage=1, distinct_key=None, evidence=evidence,
                        confidence=confidence, origin=r['origin'] if 'origin' in columns['youtube_calls'] else 'legacy_unknown',
                        source_ref='youtube_calls:%d' % r['id'], observed_at=r['started_at']))
    for r in src.execute('SELECT * FROM naver_calls ORDER BY id'):
        window = day_window(r['day'], 'Asia/Seoul')
        if instant(window['end']) <= now:
            continue
        origin = r['origin'] if 'origin' in columns['naver_calls'] else 'legacy_unknown'
        evidence, confidence = _evidence(r['status'], r['finished_at'], origin)
        out.append(dict(ledger_key=_key('naver_calls', r['id']), source_id='naver_datalab', account_scope='local-default', quota_type='naver.calls',
                        window_start=window['start'], window_end=window['end'], provider_timezone='Asia/Seoul', usage=1, distinct_key=None,
                        evidence=evidence, confidence=confidence, origin=origin, source_ref='naver_calls:%d' % r['id'], observed_at=r['requested_at']))
    for r in src.execute('SELECT * FROM instagram_searches ORDER BY id'):
        start = instant(r['searched_at'])
        end = start + timedelta(days=UNIQUE_TAG_DAYS)
        if end <= now:
            continue
        # The table has no origin column: the tag search is certain, who triggered it is not.
        out.append(dict(ledger_key=_key('instagram_searches', r['id']), source_id='instagram_graph', account_scope='ig:' + r['user_id'],
                        quota_type='instagram.unique_tags', window_start=stamp(start), window_end=stamp(end), provider_timezone='rolling-7d',
                        usage=1, distinct_key=r['name'].lstrip('#').casefold(), evidence='LEGACY_UNKNOWN_ORIGIN', confidence='UNKNOWN',
                        origin='legacy_unknown', source_ref='instagram_searches:%d' % r['id'], observed_at=r['searched_at']))
    return out


def import_legacy_usage(source_path, repo, now=None, scopes=None, ownership=None):
    """Copy open-window legacy usage and mark each covered account scope as known through `now`.

    Part of the runtime's budget safety, so it writes as a runtime participant (R3; RUNTIME_PRODUCTION on an approved production target): it keeps running
    beside runtime workers and is refused while Shadow owns the target. Account rows are written first, so
    an import and a reservation on the same account serialize on that row. Idempotent: deterministic keys;
    a re-import only refreshes evidence of the same rows. `scopes` adds account scopes the legacy
    collectors may use but have no row for yet.
    """
    from .runtime_ownership import RuntimeOwnership
    from .target_migration import open_source, writer_for
    now = utc(now or datetime.now(timezone.utc))
    with closing(open_source(source_path)) as src:
        rows = legacy_rows(src, now)
    covered = set(LEGACY_SCOPES) | set(scopes or ()) | {(r['source_id'], r['account_scope']) for r in rows}
    ownership = ownership or RuntimeOwnership(repo)
    counts = {}
    with ownership.held(ownership.runtime_mode()) as lease, writer_for(repo, ownership, lease)() as tx:
        for source, scope in sorted(covered):
            repo.upsert(tx, 'quota_accounts', dict(source_id=source, account_scope=scope, usage_status='IMPORTED',
                                                   legacy_observed_through=iso(now), evidence_ref='legacy_call_tables', updated_at=iso(now)))
        for row in rows:
            result = repo.upsert(tx, 'quota_usage_ledger', dict(row, imported_at=iso(now)))
            counts[result] = counts.get(result, 0) + 1
    return {'rows': len(rows), 'writes': counts, 'scopes': sorted('%s/%s' % s for s in covered),
            'by_evidence': {e: sum(r['evidence'] == e for r in rows) for e in EVIDENCE}, 'http_requests': 0}
