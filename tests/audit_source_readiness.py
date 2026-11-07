"""ORACLE-LIVE-VERIFICATION-READY-1 Phase B: the readiness of each source for the live pilot, from local facts only.

Read-only and offline: the environment and .env are read for presence only (process variables win over .env, as the
application loads them), the operational SQLite file only through a mode=ro, query_only connection, the local verification
records only for dates and booleans, and the Oracle test schema (with --oracle) only through read-only status calls. No
provider is called, so no credential is ever judged valid here: a present credential stays "validity not verified", and a
source is at best REQUIRES_APPROVAL. Nothing secret is printed: names, booleans, counts, dates and reason codes only.
"""
import argparse
import json
import os
import socket
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('google_trends_rss', 'instagram_graph', 'youtube', 'naver_datalab')
CREDENTIALS = {'instagram_graph': ('INSTAGRAM_ACCESS_TOKEN', 'INSTAGRAM_USER_ID', 'FACEBOOK_PAGE_ID'),
               'youtube': ('YOUTUBE_API_KEY|YOUTUBE_DATA_API_V3_KEY',),
               'naver_datalab': ('NAVER_DATALAB_CLIENT_ID', 'NAVER_DATALAB_CLIENT_SECRET')}
OPTIONAL = {'instagram_graph': ('META_APP_ID', 'META_APP_SECRET')}  # token exchange only, not collection
PLANNED = {'google_trends_rss': 1, 'instagram_graph': 5, 'youtube': 2, 'naver_datalab': 1}
# Settings the pilot needs in its process (legacy collectors off, budgets that hold the request caps).
PILOT_SETTINGS = {'instagram_graph': {'INSTAGRAM_COLLECTION_ENABLED': 'false', 'INSTAGRAM_MAX_PAGES': '1', 'INSTAGRAM_SEARCH_SOFT_LIMIT': '1'},
                  'youtube': {'YOUTUBE_COLLECTION_ENABLED': 'false', 'YOUTUBE_DAILY_SEARCH_LIMIT': '1', 'YOUTUBE_DAILY_VIDEO_LIMIT': '1',
                              'YOUTUBE_MAX_PAGES': '1'},
                  'naver_datalab': {'NAVER_DATALAB_ENABLED': 'false', 'NAVER_DATALAB_DAILY_LIMIT': '1'}, 'google_trends_rss': {}}
COMMON = {'B3_LIVE_ENABLED': 'true', 'B3_AUTOMATIC_ENABLED': 'false'}
DEFAULTS = {'B3_LIVE_ENABLED': 'false', 'B3_AUTOMATIC_ENABLED': 'false', 'INSTAGRAM_COLLECTION_ENABLED': 'true', 'INSTAGRAM_MAX_PAGES': '20',
            'INSTAGRAM_SEARCH_SOFT_LIMIT': '28', 'YOUTUBE_COLLECTION_ENABLED': 'true', 'YOUTUBE_DAILY_SEARCH_LIMIT': '40',
            'YOUTUBE_DAILY_VIDEO_LIMIT': '1000', 'YOUTUBE_MAX_PAGES': '1', 'NAVER_DATALAB_ENABLED': 'true', 'NAVER_DATALAB_DAILY_LIMIT': '100'}
PORTS = (8000, 8002, 8010, 8020, 8030, 8031, 1521)


def present(name):
    return any(os.environ.get(n, '').strip() for n in name.split('|'))


def origin(name, process):
    names = [n for n in name.split('|') if os.environ.get(n, '').strip()]
    return None if not names else 'process' if names[0] in process else '.env'


def listening(port):
    try:
        with closing(socket.create_connection(('127.0.0.1', port), timeout=0.3)):
            return True
    except OSError:
        return False


def local_records():
    """Dates and booleans of the last live verifications kept under data/ (gitignored); never a token or a key."""
    def load(name):
        path = ROOT / 'data' / name
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    approved, metadata = load('instagram-approved-token-verification.json'), load('instagram-long-lived-validated.metadata.json')
    youtube, naver = load('youtube-live-verification.json'), load('naver-live-verification.json')
    token = approved.get('token') or {}
    return {
        'instagram_graph': {'last_verified_at': approved.get('finished_at'), 'token_status_then': token.get('status'),
                            'permissions_then': token.get('permissions'), 'hashtag_lookup_then': (approved.get('hashtag_lookup') or {}).get('success'),
                            'recorded_expiry': (metadata.get('replacement') or {}).get('expires_at') or token.get('expires_at'),
                            'env_token_matches_record': 'UNVERIFIABLE_WITHOUT_PROVIDER_CALL'},
        'youtube': {'last_verified_at': youtube.get('verified_at'), 'passed_then': youtube.get('verification_passed')},
        'naver_datalab': {'last_verified_on_kst': naver.get('date_kst'), 'http_200_then': naver.get('http_200_responses')},
        'google_trends_rss': {}}


def legacy_usage(production, user_id, now):
    """Open-window usage the legacy collectors left in the operational file (read-only), against their local budgets."""
    from food_trends import cutover
    from food_trends.config import InstagramConfig, NaverConfig, YouTubeConfig
    from food_trends.quota import daily_window
    pacific, kst = daily_window(now, 'America/Los_Angeles')['day'], daily_window(now, 'Asia/Seoul')['day']
    week = (now - timedelta(days=7)).isoformat()
    with closing(cutover.readonly(production)) as db:
        q = lambda sql, *a: db.execute(sql, a).fetchone()[0]
        users = [r[0] for r in db.execute('SELECT DISTINCT user_id FROM instagram_searches')]
        out = {
            'youtube': {'calls': q('SELECT count(*) FROM youtube_calls'), 'last_call_at': q('SELECT max(started_at) FROM youtube_calls'),
                        'open_window_searches': q("SELECT count(*) FROM youtube_calls WHERE day=? AND endpoint='search'", pacific),
                        'open_window_video_calls': q("SELECT count(*) FROM youtube_calls WHERE day=? AND endpoint='videos'", pacific)},
            'naver_datalab': {'calls': q('SELECT count(*) FROM naver_calls'), 'last_call_at': q('SELECT max(requested_at) FROM naver_calls'),
                              'open_window_calls': q('SELECT count(*) FROM naver_calls WHERE day=?', kst)},
            'instagram_graph': {'searches': q('SELECT count(*) FROM instagram_searches'), 'last_search_at': q('SELECT max(searched_at) FROM instagram_searches'),
                                'open_window_unique_tags': q('SELECT count(DISTINCT lower(name)) FROM instagram_searches WHERE searched_at>?', week),
                                'accounts_in_history': len(users), 'history_account_is_configured_account': users == [user_id] if users else None},
            'google_trends_rss': {'runs': q('SELECT count(*) FROM runs'), 'last_run_at': q('SELECT max(finished_at) FROM runs')}}
    try:
        ig, yt, nv = InstagramConfig.from_env(), YouTubeConfig.from_env(), NaverConfig.from_env()
    except (ValueError, OverflowError):
        return out, None
    limits = {'instagram_graph': {'local_unique_tags_7d': ig.search_soft_limit, 'provider_unique_tags_7d': 30},
              'youtube': {'local_searches_per_pacific_day': yt.daily_search_limit, 'local_video_calls_per_pacific_day': yt.daily_video_limit,
                          'provider': 'daily units of the Google Cloud project (search 100, videos 1)'},
              'naver_datalab': {'local_calls_per_kst_day': nv.daily_limit, 'provider': 'daily calls of the NAVER application'},
              'google_trends_rss': {'provider': 'none (public feed)'}}
    remaining = {'instagram_graph': ig.search_soft_limit - out['instagram_graph']['open_window_unique_tags'],
                 'youtube': {'searches': yt.daily_search_limit - out['youtube']['open_window_searches'],
                             'video_calls': yt.daily_video_limit - out['youtube']['open_window_video_calls']},
                 'naver_datalab': nv.daily_limit - out['naver_datalab']['open_window_calls'], 'google_trends_rss': None}
    return out, {'limits': limits, 'remaining_by_local_records': remaining}


def registry_status(path):
    from food_trends.execution_runtime import _grants
    from food_trends.oracle_live import account_scope
    if not path:
        return {s: 'ABSENT' for s in SOURCES}, 'ABSENT'
    try:
        grants = {(g['source_id'], g['account_scope']) for g in _grants(path)}
    except Exception as error:  # the reason code only; evidence text is never echoed
        return {s: 'INVALID' for s in SOURCES}, 'INVALID:' + type(error).__name__
    return {s: 'GRANTED_IN_FILE' if (s, account_scope(s)) in grants else 'NOT_GRANTED' for s in SOURCES}, 'PRESENT'


def oracle_state(registry):
    """Read-only state of the Oracle test schema: version, writers, readiness reasons (no write, no HTTP)."""
    from food_trends.oracle_live import oracle_live_readiness, writer_status
    from food_trends.target_repository import TargetRepository
    from food_trends.target_schema import TARGET_SCHEMA_VERSION
    from oracle_support import oracle_target
    repo = TargetRepository(oracle_target())
    try:
        version = repo.schema_version()
        out = {'schema_version': version, 'required_version': TARGET_SCHEMA_VERSION}
        if version == TARGET_SCHEMA_VERSION:
            out['writers'] = {s: v.get('writer') for s, v in writer_status(repo)['sources'].items()}
            readiness = oracle_live_readiness(repo, registry)
            out['readiness'] = {'ready': readiness['ready'], 'blockers': readiness['blockers'],
                                'sources': {s: v['blockers'] for s, v in readiness['sources'].items()}}
        return out
    finally:
        repo.db.close()


def audit(production, registry, with_oracle, now):
    from food_trends import cutover
    from food_trends.config import InstagramConfig, NaverConfig, YouTubeConfig, load_env
    from food_trends.contracts import SOURCES as CONTRACTS
    from food_trends.execution_runtime import LEGACY_SCHEDULER_FLAGS
    process = set(os.environ)
    load_env()  # values stay in this process; only their presence is reported
    facts = cutover.inspect(production)
    try:
        configs = {'instagram_graph': InstagramConfig.from_env(), 'youtube': YouTubeConfig.from_env(), 'naver_datalab': NaverConfig.from_env()}
    except (ValueError, OverflowError):
        configs = None
    usage, budget = legacy_usage(production, configs['instagram_graph'].user_id if configs else None, now)
    records, (grants, registry_file) = local_records(), registry_status(registry)
    oracle = oracle_state(registry) if with_oracle else None
    report = {'checked_at': now.isoformat(), 'http_requests': 0, 'database_writes': 0, 'environment_precedence': 'process variables over .env',
              'env_file_present': (ROOT / '.env').exists(), 'registry_file': registry_file,
              'operational_database': {'schema_version': facts['schema_version'], 'tables': len(facts['tables']), 'rows': sum(facts['tables'].values()),
                                       'integrity': facts['integrity'], 'wal_bytes': facts['wal_bytes'], 'sha256': facts['sha256'],
                                       'legacy_server_can_start': facts['schema_version'] == 11,
                                       'why': None if facts['schema_version'] == 11 else 'MigrationRequired: v%d, G-OPS-3 not approved' % facts['schema_version']},
              'listening_ports': {p: listening(p) for p in PORTS}, 'oracle_test_schema': oracle, 'sources': {}}
    for source in SOURCES:
        names = CREDENTIALS.get(source, ())
        flag = LEGACY_SCHEDULER_FLAGS.get(source)
        flag_value = os.environ.get(flag, 'true').strip().lower() if flag else None
        wanted = dict(COMMON, **PILOT_SETTINGS[source])
        mismatch = sorted(k for k, v in wanted.items() if (os.environ.get(k, '').strip() or DEFAULTS[k]).lower() != v)  # as the code reads it
        blockers = ['CREDENTIAL_MISSING:' + n for n in names if not present(n)]
        if source in CREDENTIALS and configs is None:
            blockers.append('PROVIDER_CONFIG_INVALID')
        expiry = records['instagram_graph']['recorded_expiry'] if source == 'instagram_graph' else None
        days_left = (datetime.fromisoformat(expiry) - now).days if expiry else None
        if source == 'instagram_graph' and (days_left is None or days_left < 7):
            blockers.append('TOKEN_EXPIRY_UNKNOWN_OR_WITHIN_7_DAYS')
        if facts['integrity'] != 'ok':
            blockers.append('OPERATIONAL_DATABASE_INTEGRITY')
        if source == 'instagram_graph' and usage['instagram_graph']['history_account_is_configured_account'] is False:
            blockers.append('LEGACY_HISTORY_OF_ANOTHER_INSTAGRAM_ACCOUNT')
        approvals = ['G-OPS-4/5 writer transfer and live pilot of this source', 'reviewed registry entry (permission_verified, policy_allowed, '
                     'approved_by, evidence)' if grants[source] != 'GRANTED_IN_FILE' else 'registry entry granted in file: its evidence reviewed by a person']
        if source in CREDENTIALS:
            approvals.append('one approved provider request proves the credential (validity is not checked locally)')
        policy = CONTRACTS[source]['policy']
        report['sources'][source] = {
            'source': source,
            'credentials_present': {n: present(n) for n in names + OPTIONAL.get(source, ())} if names else 'NOT_REQUIRED',
            'credential_origin': {n: origin(n, process) for n in names} if names else None,
            'credential_validity_verified': False if names else 'NOT_REQUIRED',
            'last_local_verification': records[source] or None,
            'token_days_left_by_record': days_left,
            'registry_status': grants[source],
            'policy_status': {'response_storage': policy['response_storage'], 'retention_days': policy['retention_days'],
                              'derived_operations': policy['derived_operations'], 'human_review': 'registry policy_allowed'},
            'writer_state': {'operational_sqlite': 'legacy collectors own it, but none can start (v%d)' % facts['schema_version']
                             if facts['schema_version'] != 11 else 'legacy', 'oracle_test': (oracle or {}).get('writers', {}).get(source, 'NOT_CHECKED')},
            'legacy_collector_state': {'switch': flag or 'sqlite_server_rss_scheduler', 'switch_value': flag_value,
                                       'server_can_start': facts['schema_version'] == 11, 'server_listening': listening(8000)},
            'quota_coverage': {'legacy_records': usage[source], 'provider_usage': 'UNKNOWN (no provider call)' if names else 'NONE (public feed)'},
            'estimated_remaining_budget': (budget or {}).get('remaining_by_local_records', {}).get(source),
            'local_limits': (budget or {}).get('limits', {}).get(source),
            'pilot_requests_planned': PLANNED[source], 'pilot_settings_mismatch': mismatch,
            'blockers': blockers, 'required_approval': approvals,
            'decision': 'BLOCKED' if blockers else 'REQUIRES_APPROVAL'}  # never READY without an approval and a provider check
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--registry', help='the reviewed registry the pilot would use (validated, its evidence never printed)')
    parser.add_argument('--oracle', action='store_true', help='also read the Oracle test schema (ORACLE_TEST_*) read-only')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'tests'))
    import rehearse_cutover
    production = (ROOT / 'data' / 'trends.sqlite3').resolve()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with rehearse_cutover.guarded(production) as attempts:
        report = audit(production, args.registry, args.oracle, now)
    report['guard_attempts'] = list(attempts)
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({s: (v['decision'], v['blockers'], v['pilot_settings_mismatch']) for s, v in report['sources'].items()}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
