"""Oracle cut-over steps on an isolated copy of the operational SQLite database (rehearsal and G-OPS runbook).

The operational database is opened only read-only (mode=ro, query_only) and copied with the SQLite backup
API; schema migration, local staging, retention, loading into the target and comparison all run on the copy.
Nothing here calls a provider API, approves a food, assigns a FOOD_ID, starts a collector or writes the
operational database. Results carry counts, reason codes, hashes and timings, never provider content.
isolate() makes such a copy for rehearsals and marks it (the header's application_id), and isolated() accepts only a marked
copy that is not an operational database: the CLI's --isolated-database writes there (usage handover, reverse recovery)
without the operational switch, and never into the operational file.
"""
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .contracts import SOURCES
from .ingestion import item_key
from .target_schema import TABLES

ISOLATION_MARK = 0x4D5A4953  # 'MZIS' as the header's application_id: an isolated rehearsal copy (the operational file keeps 0)
COPY_SUFFIXES = ('.sqlite3', '.sqlite', '.db')  # names the repository's .gitignore keeps out of Git, with their -wal/-shm files
CAPTION_DAYS = SOURCES['instagram_graph']['policy']['retention_days']
YOUTUBE_MATCH_DAYS = 29  # YouTubeStore.purge: match provenance is API data with the videos' retention
# Source stores B7 reads into source_contents (Plan._contents) rather than through a target table's source_table.
CONTENT_TABLES = {'items': 'source_contents (items:N)', 'youtube_videos': 'source_contents (youtube_videos:ID, original expiry)',
                  'instagram_media': 'source_contents (instagram_media:ID, caption only inside its window)'}
DERIVED = ('EXCLUDED', 'tag-level derived aggregate; B8 recomputes from B5 links')
RUNTIME_STATE = ('RUNTIME', 'in-flight runtime state, rebuilt by the Oracle runtime')
SHADOW_CAPTURE = ('EXCLUDED', 'SQLite change capture for Shadow')
# Every other SQLite table and what represents it on the target. MIGRATED tables come from the target specification.
OTHER_TABLES = {
    'runs': ('STAGED', 'ingest_runs (run_ref runs:N) through B4; the raw RSS response is not carried'),
    'observations': ('STAGED', 'run_items and quality_results through B4, rss.approx_traffic metric_observations through B5'),
    'youtube_matches': ('STAGED', 'item_query_matches (query_ref youtube_query:N, SEARCH_ONLY) of staged videos only'),
    'instagram_hashtag_media': ('STAGED', 'item_query_matches (query_ref instagram_hashtag:N, SEARCH_ONLY)'),
    'youtube_video_provenance': ('EMBEDDED', 'origin of the YouTube source_contents and source_items'),
    'instagram_caption_tags': ('EMBEDDED', 'caption_tags inside source_contents while the caption window is open'),
    'youtube_calls': ('LEDGER', 'open-window usage in quota_usage_ledger; the call log itself is not carried'),
    'naver_calls': ('LEDGER', 'open-window usage in quota_usage_ledger; no NAVER value is stored anywhere'),
    'instagram_searches': ('LEDGER', '7-day unique hashtag usage in quota_usage_ledger'),
    'youtube_runs': ('EXCLUDED', 'legacy collector run log: no B4 adapter stages it (budget usage comes from youtube_calls)'),
    'instagram_runs': ('EXCLUDED', 'legacy collector run log: its media are staged as the origin-unknown logical run'),
    'youtube_queries': ('EXCLUDED', 'legacy search definitions, not tracking targets of approved foods'),
    'instagram_hashtags': ('EXCLUDED', 'legacy hashtag registrations, not approved food aliases'),
    'youtube_foods': ('EXCLUDED', 'legacy food mapping; YouTube food extraction is policy-blocked'),
    'youtube_candidate_foods': ('EXCLUDED', 'legacy food mapping; YouTube food extraction is policy-blocked'),
    'instagram_candidate_hashtags': ('EXCLUDED', 'legacy candidate mapping without an approved FOOD_ID'),
    'naver_targets': ('EXCLUDED', 'legacy NAVER query targets; NAVER values are never stored'),
    'instagram_metrics': DERIVED,
    'instagram_analysis_observations': DERIVED,
    'instagram_caption_analysis': ('EXCLUDED', 'caption-derived analysis; B5 links replace it'),
    'instagram_observation_tags': DERIVED,
    'instagram_leases': ('EXCLUDED', 'collector-local lock'),
    'instagram_state': ('EXCLUDED', 'collector-local state'),
    'youtube_state': ('EXCLUDED', 'collector-local state'),
    'naver_guards': ('EXCLUDED', 'collector-local guard'),
    'execution_source_ownership': ('EXCLUDED', 'SQLite collector writer ownership; the target has runtime_ownership'),
    'tracking_commands': ('EXCLUDED', 'SQLite command log; the target keeps its own (v7, runtime-owned); B7 copies no history'),
    'tracking_events': ('EXCLUDED', 'SQLite lifecycle history; the target keeps its own (v7, runtime-owned); B7 copies no history'),
    'tracking_slots': ('RUNTIME', 'SQLite slots; the target schedules its own tracking_slots'),
    'planner_commands': ('RUNTIME', 'SQLite planner command log; the target keeps the runtime copy'),
    'planner_plan_targets': ('RUNTIME', 'SQLite planner copy; the target keeps the runtime copy'),
    'quota_events': ('EXCLUDED', 'reservation transition history; the target keeps the reservation state'),
    'quota_charges': RUNTIME_STATE,
    'execution_mappings': RUNTIME_STATE,
    'execution_leases': RUNTIME_STATE,
    'execution_checkpoints': RUNTIME_STATE,
    'execution_item_receipts': RUNTIME_STATE,
    'execution_blocks': RUNTIME_STATE,
    'shadow_settings': SHADOW_CAPTURE,
    'shadow_outbox': SHADOW_CAPTURE,
    'schema_migrations': ('EXCLUDED', 'SQLite migration history'),
    'sqlite_sequence': ('EXCLUDED', 'SQLite bookkeeping'),
}


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    db.row_factory = sqlite3.Row
    return db


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _tables(db):
    return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def inspect(path):
    """Read-only facts: file hash, size, modification time and WAL size, schema version, integrity and rows per table."""
    from .migrations import schema_version
    path = Path(path).resolve()
    wal = Path(str(path) + '-wal')
    facts = {'sha256': _sha256(path), 'bytes': path.stat().st_size,
             'modified_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
             'wal_bytes': wal.stat().st_size if wal.exists() else None, 'schema_version': schema_version(path)}
    with closing(readonly(path)) as db:
        facts.update(journal_mode=db.execute('PRAGMA journal_mode').fetchone()[0], integrity=db.execute('PRAGMA integrity_check').fetchone()[0],
                     foreign_key_errors=len(db.execute('PRAGMA foreign_key_check').fetchall()),
                     tables={t: db.execute('SELECT count(*) FROM "%s"' % t).fetchone()[0] for t in _tables(db)})
    return facts


def backup(source, destination):
    """A consistent copy through the SQLite backup API, read from a read-only connection to the source."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(str(destination))
    with closing(readonly(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
    return inspect(destination)


def isolate(source, destination, now=None):
    """An isolated v11 copy of the operational database for rehearsals (ORACLE-LIVE-VERIFICATION-READY-1): backup() of the
    read-only source, the copy marked as isolated (ISOLATION_MARK), then prepare() on the copy alone. Refused before anything
    is copied for an operational destination (by name, path, DATABASE_PATH or the same file), an existing one (never
    overwritten) or a name the repository does not keep out of Git. Counts, codes and hashes only."""
    from .reverse_recovery import RecoveryRefused, _operational
    destination = Path(destination).resolve()
    if _operational(destination):
        raise RecoveryRefused('isolated_copy_is_operational')
    if destination.suffix.lower() not in COPY_SUFFIXES:
        raise RecoveryRefused('isolated_copy_name_not_ignored')
    if any(Path(str(destination) + suffix).exists() for suffix in ('', '-wal', '-shm', '-journal')):
        raise RecoveryRefused('isolated_copy_exists')
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    before = inspect(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied = backup(source, destination)
    with closing(sqlite3.connect(destination)) as db:
        db.execute('PRAGMA application_id=%d' % ISOLATION_MARK)
    prepared = prepare(destination, now)
    after, facts = inspect(source), inspect(destination)
    keys = ('sha256', 'bytes', 'modified_at', 'wal_bytes', 'schema_version', 'integrity', 'tables')
    return {'source': Path(source).name, 'destination': destination.name, 'prepared_at': now.isoformat(), 'isolated': isolated(destination) == destination,
            'source_facts': {k: before[k] for k in ('sha256', 'schema_version', 'integrity', 'wal_bytes', 'modified_at')},
            'source_tables': len(before['tables']), 'source_rows': sum(before['tables'].values()),
            'source_unchanged': {k: before[k] == after[k] for k in keys},
            'copy_equal_source_before_migration': copied['tables'] == before['tables'],
            'copy': {'sha256': facts['sha256'], 'schema_version': facts['schema_version'], 'integrity': facts['integrity'],
                     'foreign_key_errors': facts['foreign_key_errors'], 'tables': len(facts['tables']), 'rows': sum(facts['tables'].values()),
                     'food_entities': facts['tables'].get('food_entities', 0), 'wal_bytes': facts['wal_bytes']},
            'prepare': prepared, 'http_requests': 0}


def isolated(path):
    """The copy --isolated-database names: an existing file isolate() marked, never an operational database (as
    reverse_recovery decides it: the operational name, path or DATABASE_PATH, or the same file under another name)."""
    from .reverse_recovery import RecoveryRefused, _operational
    path = Path(path).resolve()
    if _operational(path):
        raise RecoveryRefused('isolated_database_is_operational')
    if not path.is_file():
        raise RecoveryRefused('isolated_database_missing')
    with closing(readonly(path)) as db:
        mark = db.execute('PRAGMA application_id').fetchone()[0]
    if mark != ISOLATION_MARK:
        raise RecoveryRefused('isolated_database_not_marked')
    return path


def digests(path, columns=None):
    """Per table, rows and a SHA-256 over the given (default: all) columns in rowid order. Returns (digests, columns)."""
    out, seen = {}, {}
    with closing(readonly(path)) as db:
        for table in (list(columns) if columns is not None else _tables(db)):
            names = columns[table] if columns is not None else [r[1] for r in db.execute('PRAGMA table_info("%s")' % table)]
            seen[table] = names
            digest, count = hashlib.sha256(), 0
            for row in db.execute('SELECT %s FROM "%s" ORDER BY rowid' % (','.join('"%s"' % n for n in names), table)):
                digest.update(repr(tuple(row)).encode('utf-8'))
                count += 1
            out[table] = {'rows': count, 'sha256': digest.hexdigest()}
    return out, seen


def _source_retention(storage, now):
    """The collectors' own retention as `--maintenance` applies it (HashtagAnalysis.purge_expired, YouTubeStore.purge), at `now`."""
    with storage.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('PRAGMA secure_delete=ON')
        captions = db.execute('''UPDATE instagram_media SET caption=NULL,caption_purged_at=? WHERE caption IS NOT NULL
            AND coalesce(caption_captured_at,first_seen)<?''', (now.isoformat(), (now - timedelta(days=CAPTION_DAYS)).isoformat())).rowcount
        videos = db.execute('DELETE FROM youtube_videos WHERE expires_at<=?', (now.isoformat(),)).rowcount
        matches = db.execute('DELETE FROM youtube_matches WHERE matched_at<=?', ((now - timedelta(days=YOUTUBE_MATCH_DAYS)).isoformat(),)).rowcount
    return {'purged_captions': captions, 'purged_videos': videos, 'purged_youtube_matches': matches}


def prepare(copy, now):
    """v0 -> v11 on the copy, then the documented local follow-ups (G-OPS-3; no API call): B4 staging of finished
    runs and of origin-unknown legacy rows, B5 links, and the source retention as of `now`. No food is approved."""
    from .execution_maintenance import purge as purge_execution
    from .food_observations import FoodObservationService
    from .ingestion import IngestionService
    from .migrations import migrate
    from .storage import Storage
    before, columns = digests(copy)
    migration = migrate(copy)
    after, _ = digests(copy, columns)
    storage = Storage(copy)
    clock = lambda: now
    ingestion, links = IngestionService(storage, clock), FoodObservationService(storage, clock)
    staged = ingestion.ingest_pending(limit=10 ** 6)
    legacy = {}
    for source in ('youtube', 'instagram_graph'):
        result = ingestion.legacy_backfill(source)
        legacy[source] = {k: result[k] for k in ('staged_items', 'rejected_items', 'expired_items', 'source_absent_items')}
    linked = links.link_pending(limit=10 ** 6)
    retention = dict(_source_retention(storage, now), execution=purge_execution(storage, now), staging=ingestion.purge(now), links=links.purge(now))
    effects = links.apply_entity_changes()
    with closing(sqlite3.connect(copy)) as db:
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return {'migration': migration, 'original_tables': len(before),
            'original_rows_and_columns_preserved': before == after,
            'changed_original_tables': sorted(t for t in before if before[t] != after[t]),
            'staged_runs': staged['ingested'], 'legacy_backfill': legacy, 'linked_runs': linked['linked_runs'],
            'retention': retention, 'entity_changes_reprocessed': len(effects), 'foods_approved': 0, 'http_requests': 0}


def table_dispositions(path):
    """Every SQLite table of the copy with its rows and what represents it on the target."""
    loaded = {}
    for spec in TABLES:
        if spec.source_table:
            loaded.setdefault(spec.source_table, []).append(spec.name)
    out = {}
    with closing(readonly(path)) as db:
        for table in [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]:
            if table in loaded:
                kind, note = 'MIGRATED', ', '.join(loaded[table])
            elif table in CONTENT_TABLES:
                kind, note = 'MIGRATED', CONTENT_TABLES[table]
            else:
                kind, note = OTHER_TABLES.get(table, ('UNCLASSIFIED', ''))
            out[table] = {'rows': db.execute('SELECT count(*) FROM "%s"' % table).fetchone()[0], 'disposition': kind, 'target': note}
    return out


def _instant(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc) if value else None


def lineage(path):
    """Source-store rows against their B4/B5 staging on the copy: rows, relations, states, times, origins and expiry.

    Each check lists expected rows, staged rows and mismatches by reason code (no values).
    """
    checks = {}
    with closing(readonly(path)) as db:
        q = lambda sql, args=(): [dict(r) for r in db.execute(sql, args)]
        runs = {r['run_ref']: r for r in q("SELECT * FROM ingest_runs WHERE source_code='google_trends_rss'")}
        problems = Counter()
        for run in q('SELECT * FROM runs WHERE finished_at IS NOT NULL'):
            staged = runs.get('runs:%d' % run['id'])
            if staged is None:
                problems['not_staged'] += 1
                continue
            problems['origin_changed'] += staged['origin'] != run['origin']
            problems['observed_at_changed'] += _instant(staged['observed_at']) != _instant(run['finished_at'])
            problems['coverage_wrong'] += staged['coverage'] != ('COMPLETE' if run['success'] else 'NONE')
        checks['rss_runs'] = {'expected': len(q('SELECT id FROM runs WHERE finished_at IS NOT NULL')), 'staged': len(runs),
                              'mismatches': {k: v for k, v in problems.items() if v}}
        items = {r['item_key']: r for r in q("SELECT * FROM source_items WHERE source_code='google_trends_rss'")}
        seen = {r['id']: r for r in q('''SELECT i.id,i.source_id,i.published_at,min(r.finished_at) AS first,max(r.finished_at) AS last
            FROM items i JOIN observations o ON o.item_id=i.id JOIN runs r ON r.id=o.run_id GROUP BY i.id''')}
        problems = Counter()
        for row in seen.values():
            staged = items.get(item_key('google_trends_rss', row['source_id']))
            if staged is None:
                problems['not_staged'] += 1
                continue
            problems['payload_ref_changed'] += staged['payload_ref'] != 'items:%d' % row['id']
            problems['published_at_changed'] += staged['published_at'] != row['published_at']
            problems['seen_window_changed'] += (_instant(staged['first_seen']), _instant(staged['last_seen'])) != (_instant(row['first']), _instant(row['last']))
        checks['rss_items'] = {'expected': len(seen), 'staged': len(items), 'mismatches': {k: v for k, v in problems.items() if v}}
        observed = q("""SELECT count(*) AS n FROM observations o JOIN runs r ON r.id=o.run_id""")[0]['n']
        run_items = q("""SELECT count(*) AS n FROM run_items x JOIN ingest_runs g ON g.ingest_run_id=x.ingest_run_id
            WHERE g.source_code='google_trends_rss'""")[0]['n']
        metrics = q("SELECT count(*) AS n FROM metric_observations WHERE metric_code='rss.approx_traffic'")[0]['n']
        checks['rss_observations'] = {'expected': observed, 'run_items': run_items, 'traffic_metrics': metrics}
        provenance = dict((r['video_id'], r['origin']) for r in q('SELECT video_id,origin FROM youtube_video_provenance'))
        staged = {r['external_key']: r for r in q("SELECT * FROM source_items WHERE source_code='youtube'")}
        problems, unstaged = Counter(), Counter()
        videos = q('SELECT video_id,expires_at FROM youtube_videos')
        for video in videos:
            row = staged.get(video['video_id'])
            if row is None:
                unstaged['no_staging_path_for_origin_' + provenance.get(video['video_id'], 'legacy_unknown')] += 1
                continue
            problems['expiry_changed'] += _instant(row['expires_at']) != _instant(video['expires_at'])
            problems['origin_changed'] += row['first_origin'] != provenance.get(video['video_id'], 'legacy_unknown')
        checks['youtube_videos'] = {'expected': len(videos), 'staged': len(staged), 'unstaged': dict(unstaged),
                                    'mismatches': {k: v for k, v in problems.items() if v}}
        staged = {r['external_key']: r for r in q("SELECT * FROM source_items WHERE source_code='instagram_graph'")}
        problems = Counter()
        media = q('SELECT id,published_at,first_seen,last_seen,origin FROM instagram_media')
        for row in media:
            item = staged.get(row['id'])
            if item is None:
                problems['not_staged'] += 1
                continue
            problems['origin_changed'] += item['first_origin'] != row['origin']
            problems['published_at_changed'] += item['published_at'] != row['published_at']
            problems['seen_window_changed'] += (_instant(item['first_seen']), _instant(item['last_seen'])) != (_instant(row['first_seen']), _instant(row['last_seen']))
            problems['expiry_set'] += item['expires_at'] is not None
        checks['instagram_media'] = {'expected': len(media), 'staged': len(staged), 'mismatches': {k: v for k, v in problems.items() if v}}
        refs = Counter(r['query_ref'].split(':')[0] for r in q('SELECT query_ref FROM item_query_matches'))
        checks['search_evidence'] = {
            'youtube_matches': len(q('SELECT 1 FROM youtube_matches')),
            'youtube_matches_of_staged_videos': len(q("""SELECT 1 FROM youtube_matches m JOIN source_items s
                ON s.source_code='youtube' AND s.external_key=m.video_id""")),
            'instagram_hashtag_media': len(q('SELECT 1 FROM instagram_hashtag_media')),
            'item_query_matches_by_ref': dict(refs)}
    return checks


def _rows(db, table):
    return [dict(r) for r in db.execute('SELECT * FROM %s' % table)]


def categories(rows):
    """Comparable figures per area of the cut-over checklist; `rows(table)` returns that table's rows."""
    def by(table, *columns):
        counts = Counter(tuple(str(r[c]) for c in columns) for r in rows(table))
        return {'|'.join(key): counts[key] for key in sorted(counts)}
    return {
        'food_identity': {t: len(rows(t)) for t in ('food_entities', 'food_aliases', 'food_relations')},
        'candidates_and_decisions': dict({t: len(rows(t)) for t in ('food_candidates', 'food_events', 'food_commands')},
                                         rss_classifications=by('rss_classifications', 'classification')),
        'tracking_targets': len(rows('tracking_targets')),
        'query_planner': {t: len(rows(t)) for t in ('planner_queries', 'planner_plans', 'planner_query_targets', 'quota_reservations')},
        'execution': {t: len(rows(t)) for t in ('execution_jobs', 'execution_runs', 'execution_calls', 'execution_results')},
        'runs_by_source_origin_coverage': by('ingest_runs', 'source_code', 'origin', 'coverage'),
        'items_by_source_origin_availability': by('source_items', 'source_code', 'first_origin', 'availability'),
        'run_items_by_quality': by('run_items', 'quality_status'),
        'snapshots': len(rows('item_snapshots')),
        'search_evidence_by_status': by('item_query_matches', 'status', 'verification_status'),
        'food_mentions_by_status': by('food_mentions', 'status'),
        'metrics_by_code_state_coverage_origin': by('metric_observations', 'metric_code', 'value_state', 'coverage', 'origin'),
        'quality_by_outcome': by('quality_results', 'outcome'),
        'entity_change_effects': len(rows('entity_change_effects')), 'aggregate_invalidations': len(rows('aggregate_invalidations')),
    }


def source_categories(path):
    """categories() over the SQLite copy (B7 maps rss_classifications from candidates)."""
    with closing(readonly(path)) as db:
        def rows(table):
            return _rows(db, 'candidates' if table == 'rss_classifications' else table)
        return categories(rows)


def target_categories(repo):
    cache = {}

    def rows(table):
        if table not in cache:
            cache[table] = repo.rows(table)
        return cache[table]
    out = categories(rows)
    out['source_contents_by_source_origin'] = {'|'.join(k): n for k, n in Counter((r['source_code'], r['origin']) for r in rows('source_contents')).items()}
    out['captions_held'] = sum('caption' in json.loads(r['content_json']) for r in rows('source_contents') if r['source_code'] == 'instagram_graph')
    out['rejections_by_table_disposition_reason'] = {'|'.join(k): n for k, n in Counter(
        (r['table_name'], r['disposition'], r['reason']) for r in rows('migration_rejections')).items()}
    out['quota_usage_ledger'] = len(rows('quota_usage_ledger'))
    out['quota_accounts_by_status'] = dict(Counter(r['usage_status'] for r in rows('quota_accounts')))
    return out


def key_checks(repo):
    """Deterministic key formats and natural-key duplicates on the target (both must be clean)."""
    prefixes = {'source_items': ('item_key', 'ITEM_'), 'item_snapshots': ('snapshot_id', 'SNAP_'), 'ingest_runs': ('ingest_run_id', 'INGEST_'),
                'metric_observations': ('observation_id', 'METRIC_'), 'item_query_matches': ('match_id', 'MATCH_')}
    out = {'bad_key_format': {}, 'natural_key_duplicates': {}}
    for spec in TABLES:
        rows = repo.rows(spec.name)
        keys = [tuple(r[c] for c in spec.key) for r in rows]
        out['natural_key_duplicates'][spec.name] = len(keys) - len(set(keys))
        if spec.name in prefixes:
            column, prefix = prefixes[spec.name]
            out['bad_key_format'][spec.name] = sum(not str(r[column]).startswith(prefix) for r in rows)
    out['clean'] = not any(out['bad_key_format'].values()) and not any(out['natural_key_duplicates'].values())
    return out


def api_targets(api):
    """Every request an app could make against the loaded data: health, status, lists and rankings page by page, each food."""
    from urllib.parse import urlencode
    from . import app_contract as contract
    host = {'Host': '127.0.0.1:8020'}
    targets = ['/api/v1/health'] + ['/api/v1/sources/status?period=%s' % p for p in contract.PERIODS]
    lists = ['/api/v1/foods?limit=100'] + ['/api/v1/trends/rankings?' + urlencode(dict(source=s, period=p, include_zero=z, limit=100))
                                          for s in ('google_trends_rss', 'instagram_graph') for p in contract.PERIODS for z in ('true', 'false')]
    for first in lists:
        page = first
        while page:
            targets.append(page)
            status, body, _ = api.handle('GET', page, dict(host))
            cursor = (body.get('page') or {}).get('next_cursor') if status == 200 else None
            page = first + '&' + urlencode({'cursor': cursor}) if cursor else None
    status, body, _ = api.handle('GET', '/api/v1/foods?limit=100', dict(host))
    for food in (body.get('data') or {}).get('items', []) if status == 200 else []:
        base = '/api/v1/foods/' + food['food_id']
        targets += [base] + ['%s/%s?period=%s' % (base, kind, p) for kind in ('timeline', 'metrics') for p in contract.PERIODS]
    return targets


def api_parity(storage, repo, clock):
    """B8 answers over the SQLite copy and over the target, per observation origin: status, body and headers compared."""
    from .analytics_reader import SQLiteAnalyticsStore, TargetAnalyticsStore
    from .analytics_service import FoodAnalytics
    from .app_api import AppApi
    host = {'Host': '127.0.0.1:8020'}
    out = {}
    for origin in ('live_api', 'mock'):
        left = AppApi(FoodAnalytics(SQLiteAnalyticsStore(storage), clock=clock, origin=origin))
        right = AppApi(FoodAnalytics(TargetAnalyticsStore(repo), clock=clock, origin=origin))
        targets, mismatches = api_targets(left), []
        for target in targets:
            a, b = left.handle('GET', target, dict(host)), right.handle('GET', target, dict(host))
            if json.dumps([a[0], a[1], sorted(a[2].items())], ensure_ascii=False, sort_keys=True) != \
                    json.dumps([b[0], b[1], sorted(b[2].items())], ensure_ascii=False, sort_keys=True):
                mismatches.append(target)
        out[origin] = {'requests': len(targets), 'mismatches': mismatches}
    return out
