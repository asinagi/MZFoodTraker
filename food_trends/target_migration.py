"""B7 staged SQLite -> target migration and reconciliation.

Read-only export -> policy/TTL filter -> mapping -> conversion -> validation -> batched load ->
reconciliation. The source is opened read-only and never written; no provider API is called.
Expiry is copied, never recomputed: rows past their source retention are excluded, children of
excluded parents are excluded with them, and Instagram captions past their window lose the field
only. Every exclusion and rejection is recorded by table, disposition and reason code with a
subject hash, never with provider values. Batches resume from the last committed key and
re-running is a no-op.
"""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .contracts import SOURCES
from .ingestion import item_key
from .target_db import ConversionError, PolicyRefused, canonical_json, iso, logical, to_target, utc
from .target_repository import policy
from .target_schema import BY_NAME, TABLES

MIGRATION_VERSION = 'sqlite-to-target-v1'
CAPTION_DAYS = SOURCES['instagram_graph']['policy']['retention_days']
TTL_EXEMPT = ('quota_reservations',)  # expires_at there is a reservation lease, not retention
ORDER = [t.name for t in TABLES if t.source_table or t.name == 'source_contents']
LOAD, POLICY, EXPIRED, INVALID = 'LOAD', 'EXCLUDED_POLICY', 'EXCLUDED_EXPIRED', 'REJECTED_INVALID'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def open_source(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    db.row_factory = sqlite3.Row
    return db


def _rss_key(source_id):
    return item_key('google_trends_rss', source_id)


class Plan:
    """All candidate rows for one run, with dispositions and legacy key mappings."""

    def __init__(self, src, now):
        self.src, self.now = src, now
        self.present, self.excluded, self.rows, self.legacy = {}, {}, {}, []

    def _raw(self, table):
        spec = BY_NAME[table]
        for r in self.src.execute('SELECT * FROM %s' % spec.source_table):
            r = dict(r)
            yield {c.name: r.get(c.src) for c in spec.columns}, r

    def _source_rows(self, table):
        if table == 'food_candidates':
            ids = {r['id']: r['source_id'] for r in self.src.execute('SELECT id,source_id FROM items')}
            for row, raw in self._raw(table):
                if raw['source_item_id'] is not None:
                    row['source_item_key'] = _rss_key(ids[raw['source_item_id']]) if raw['source_item_id'] in ids else '#missing'
                yield row
        elif table == 'food_mentions':
            sources = dict(self.src.execute('SELECT item_key,source_code FROM source_items').fetchall())
            for row, _ in self._raw(table):
                row['source_code'] = sources.get(row['item_key'])
                yield row
        elif table == 'rss_classifications':
            for r in self.src.execute('SELECT c.*,i.source_id FROM candidates c JOIN items i ON i.id=c.item_id'):
                r = dict(r)
                key = _rss_key(r['source_id'])
                self.legacy.append(('candidates', str(r['item_id']), table, key, digest(r)))
                yield dict(item_key=key, normalized_name=r['normalized_name'], classification=r['classification'], reasons_json=r['reasons'],
                           classifier_version=r['classifier_version'], entity_type=r['entity_type'], rule_score=r['rule_score'],
                           score_basis=r['score_basis'], evidence_news_json=r['evidence_news'], classified_at=r['classified_at'])
        elif table == 'rss_classification_history':
            for r in self.src.execute('SELECT h.*,i.source_id FROM classification_history h LEFT JOIN items i ON i.id=h.item_id'):
                r = dict(r)
                key = _rss_key(r['source_id']) if r['source_id'] else None
                history = 'CLSH_' + digest([key, r['recorded_at'], r['result_json']])[:32]
                self.legacy.append(('classification_history', str(r['id']), table, history, digest(r)))
                yield dict(history_key=history, item_key=key, recorded_at=r['recorded_at'], result_json=r['result_json'])
        elif table == 'source_contents':
            yield from self._contents()
        else:
            for row, _ in self._raw(table):
                yield row

    def _contents(self):
        """Allowed source content keyed by the existing payload_ref; original times and expiry kept."""
        staged = dict(self.src.execute('SELECT payload_ref,first_origin FROM source_items').fetchall())
        for r in self.src.execute('SELECT * FROM items ORDER BY id'):
            ref, key = 'items:%d' % r['id'], _rss_key(r['source_id'])
            self.legacy.append(('items', str(r['id']), 'source_contents', ref, hashlib.sha256(r['payload'].encode()).hexdigest()))
            yield dict(payload_ref=ref, item_key=key, source_code='google_trends_rss', origin=staged.get(ref, 'legacy_unknown'),
                       content_json=r['payload'], content_hash=hashlib.sha256(canonical_json(r['payload']).encode()).hexdigest(),
                       captured_at=r['first_seen'], refreshed_at=r['last_seen'], expires_at=None, field_expires_at=None, field_purged_at=None)
        provenance = dict(self.src.execute('SELECT video_id,origin FROM youtube_video_provenance').fetchall())
        for r in self.src.execute('SELECT * FROM youtube_videos ORDER BY video_id'):
            yield dict(payload_ref='youtube_videos:' + r['video_id'], item_key=item_key('youtube', r['video_id']), source_code='youtube',
                       origin=provenance.get(r['video_id'], 'legacy_unknown'), content_json=r['payload'],
                       content_hash=hashlib.sha256(canonical_json(r['payload']).encode()).hexdigest(), captured_at=r['collected_at'],
                       refreshed_at=r['refreshed_at'], expires_at=r['expires_at'], field_expires_at=None, field_purged_at=None)
        for r in self.src.execute('SELECT * FROM instagram_media ORDER BY id'):
            content = {k: r[k] for k in ('id', 'published_at', 'media_type', 'permalink', 'source')}
            field_expires = field_purged = None
            if r['caption_purged_at']:
                field_purged = r['caption_purged_at']
            elif r['caption'] is not None:
                captured = utc(r['caption_captured_at'] or r['first_seen'])
                field_expires = captured + timedelta(days=CAPTION_DAYS)
                if field_expires > self.now:
                    content['caption'] = r['caption']
                    content['caption_tags'] = [t[0] for t in self.src.execute(
                        'SELECT normalized_name FROM instagram_caption_tags WHERE media_id=? AND active=1 ORDER BY normalized_name', (r['id'],))]
                else:
                    field_purged = iso(field_expires)  # the caption window already closed: metadata only
                field_expires = iso(field_expires)
            yield dict(payload_ref='instagram_media:' + r['id'], item_key=item_key('instagram_graph', r['id']), source_code='instagram_graph',
                       origin=r['origin'], content_json=json.dumps(content, ensure_ascii=False),
                       content_hash=hashlib.sha256(canonical_json(content).encode()).hexdigest(), captured_at=r['first_seen'],
                       refreshed_at=r['last_seen'], expires_at=None, field_expires_at=field_expires, field_purged_at=field_purged)

    def classify(self, table, row):
        spec = BY_NAME[table]
        pk = tuple(row.get(k) for k in spec.pk)
        if 'expires_at' in row and table not in TTL_EXEMPT and row['expires_at'] and utc(row['expires_at']) <= self.now:
            return EXPIRED, 'source_retention_elapsed'
        for cols, parent, _ in spec.fks:
            value = tuple(row.get(c) for c in cols)
            if None in value or parent not in self.present:
                continue
            if value in self.excluded.get(parent, {}):
                return self.excluded[parent][value], 'parent_excluded:' + parent
            if value not in self.present[parent]:
                return INVALID, 'missing_parent:' + parent
        if table == 'food_candidates' and row.get('source_item_key') == '#missing':
            return INVALID, 'missing_parent:items'
        try:
            policy(table, row, self.now)
            to_target(table, row, 'oracle')  # strictest representation decides validity
        except PolicyRefused as exc:
            return (EXPIRED if exc.code == 'expired_content' else POLICY), exc.code
        except ConversionError as exc:
            return INVALID, exc.code
        if pk in self.present.setdefault(table, set()):
            return INVALID, 'duplicate_key'
        return LOAD, None

    def build(self):
        for table in ORDER:
            spec = BY_NAME[table]
            self.present.setdefault(table, set())
            entries = []
            for row in self._source_rows(table):
                disposition, reason = self.classify(table, row)
                pk = tuple(row.get(k) for k in spec.pk)
                if disposition == LOAD:
                    self.present[table].add(pk)
                else:
                    self.excluded.setdefault(table, {})[pk] = disposition
                entries.append((json.dumps([row.get(k) for k in spec.key], ensure_ascii=False, default=str), row, disposition, reason))
            self.rows[table] = sorted(entries, key=lambda e: e[0])
        return self


def fingerprint(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def migrate(source_path, repo, now=None, batch_size=500, fault=None):
    """Load eligible rows; record exclusions; resume per table from the last committed key."""
    now = utc(now or datetime.now(timezone.utc))
    fault = fault or (lambda stage, table, n: None)
    source_fp = fingerprint(source_path)
    batch = 'B7_' + source_fp[:24]
    with closing(open_source(source_path)) as src:
        plan = Plan(src, now).build()
    report = {'batch_key': batch, 'source_fingerprint': source_fp, 'migration_version': MIGRATION_VERSION, 'tables': {}}
    with repo.db.transaction() as tx:
        for legacy_table, legacy_key, target_table, target_key, source_hash in plan.legacy:
            repo.upsert(tx, 'legacy_key_map', dict(legacy_table=legacy_table, legacy_key=legacy_key, target_table=target_table,
                                                   target_key=target_key, source_hash=source_hash, batch_key=batch, mapped_at=iso(now)))
    for table in ORDER:
        state = repo.get('migration_batches', {'batch_key': batch, 'table_name': table}) or {}
        counts = {'loaded_rows': state.get('loaded_rows', 0), 'excluded_rows': state.get('excluded_rows', 0),
                  'rejected_rows': state.get('rejected_rows', 0)}
        results = {'inserted': 0, 'updated': 0, 'unchanged': 0}
        if state.get('status') != 'COMPLETED':
            pending = [e for e in plan.rows[table] if state.get('last_key') is None or e[0] > state['last_key']]
            for start in range(0, len(pending), batch_size):
                chunk = pending[start:start + batch_size]
                with repo.db.transaction() as tx:
                    for n, (key, row, disposition, reason) in enumerate(chunk):
                        fault('row', table, start + n)
                        if disposition == LOAD:
                            results[repo.upsert(tx, table, row)] += 1
                            counts['loaded_rows'] += 1
                        else:
                            repo.upsert(tx, 'migration_rejections', dict(
                                rejection_key=digest([batch, table, key]), batch_key=batch, table_name=table,
                                subject_hash=digest([table, key]), disposition=disposition, reason=reason, recorded_at=iso(now)))
                            counts['rejected_rows' if disposition == INVALID else 'excluded_rows'] += 1
                    repo.upsert(tx, 'migration_batches', dict(batch_key=batch, table_name=table, source_fingerprint=source_fp,
                        status='RUNNING', last_key=chunk[-1][0], started_at=state.get('started_at') or iso(now), finished_at=None, **counts))
                fault('after_commit', table, start + len(chunk))
            with repo.db.transaction() as tx:
                repo.upsert(tx, 'migration_batches', dict(batch_key=batch, table_name=table, source_fingerprint=source_fp, status='COMPLETED',
                    last_key=plan.rows[table][-1][0] if plan.rows[table] else None, started_at=state.get('started_at') or iso(now),
                    finished_at=iso(now), **counts))
        dispositions = {}
        for _, _, disposition, reason in plan.rows[table]:
            if disposition != LOAD:
                dispositions['%s:%s' % (disposition, reason)] = dispositions.get('%s:%s' % (disposition, reason), 0) + 1
        report['tables'][table] = dict(candidates=len(plan.rows[table]), **counts, writes=results, dispositions=dispositions)
    return report


def _canonical(table, row):
    return json.dumps(logical(table, row), ensure_ascii=False, sort_keys=True, default=str)


def reconcile(source_path, repo, now=None, source_storage=None):
    """Logical comparison after normalization: keys, values, hashes, nulls, references, TTL, policy, FOOD_ID view."""
    now = utc(now or datetime.now(timezone.utc))
    with closing(open_source(source_path)) as src:
        plan = Plan(src, now).build()
    out = {'tables': {}, 'checks': {}}
    for table in ORDER:
        spec = BY_NAME[table]
        expected = {k: _canonical(table, row) for k, row, d, _ in plan.rows[table] if d == LOAD}
        actual = {json.dumps([r[c] for c in spec.key], ensure_ascii=False, default=str): json.dumps(r, ensure_ascii=False, sort_keys=True, default=str)
                  for r in repo.rows(table)}
        missing, extra = sorted(set(expected) - set(actual)), sorted(set(actual) - set(expected))
        changed = [k for k in expected if k in actual and expected[k] != actual[k]]
        nulls = {}
        for c in spec.columns:
            source_null = sum(json.loads(v)[c.name] is None for v in expected.values())
            target_null = sum(json.loads(v)[c.name] is None for v in actual.values())
            if source_null != target_null:
                nulls[c.name] = (source_null, target_null)
        out['tables'][table] = {
            'eligible': len(expected), 'target': len(actual), 'missing': len(missing), 'extra': len(extra), 'changed': len(changed),
            'null_mismatch': nulls, 'excluded': sum(d in (POLICY, EXPIRED) for _, _, d, _ in plan.rows[table]),
            'rejected': sum(d == INVALID for _, _, d, _ in plan.rows[table]),
            'source_hash': hashlib.sha256('\n'.join(expected[k] for k in sorted(expected)).encode()).hexdigest(),
            'target_hash': hashlib.sha256('\n'.join(actual[k] for k in sorted(actual)).encode()).hexdigest()}
    with repo.db.transaction() as tx:
        if repo.db.dialect == 'sqlite':
            out['checks']['foreign_key_errors'] = len(tx.rows('PRAGMA foreign_key_check'))
        else:
            out['checks']['foreign_key_errors'] = 'enforced_by_deferred_constraints'
        refs = {r['payload_ref'] for r in tx.rows('SELECT payload_ref FROM source_contents')}
        out['checks']['unresolved_payload_refs'] = (
            sum(r['payload_ref'] not in refs for r in tx.rows("SELECT payload_ref FROM source_items WHERE availability='AVAILABLE'")) +
            sum(r['payload_ref'] not in refs for r in tx.rows('SELECT payload_ref FROM item_snapshots WHERE purged_at IS NULL')))
        out['checks']['blocked_policy_rows'] = tx.scalar("""SELECT count(*) FROM metric_observations WHERE source_code NOT IN ('google_trends_rss','instagram_graph')""") + \
            tx.scalar("""SELECT count(*) FROM food_mentions WHERE source_code NOT IN ('google_trends_rss','instagram_graph')""")
        out['checks']['distinct_items_by_source'] = {r['source_code']: int(r['n']) for r in tx.rows(
            'SELECT source_code,count(DISTINCT external_key) AS n FROM source_items GROUP BY source_code')}
        out['checks']['legacy_unknown_items'] = int(tx.scalar("SELECT count(*) FROM source_items WHERE first_origin='legacy_unknown'"))
        out['checks']['not_observed_metrics_with_null_value'] = int(tx.scalar(
            "SELECT count(*) FROM metric_observations WHERE value_state='NOT_OBSERVED' AND count_value IS NULL"))
        foods = [r['food_id'] for r in tx.rows("SELECT food_id FROM food_entities ORDER BY food_id")]
    retained_expired = 0
    for table in ORDER:
        if 'expires_at' in [c.name for c in BY_NAME[table].columns] and table not in TTL_EXEMPT:
            retained_expired += sum(1 for r in repo.rows(table) if r['expires_at'] and utc(r['expires_at']) <= now)
    out['checks']['expired_rows_in_target'] = retained_expired
    if source_storage is not None:
        from .food_observations import FoodObservationService
        service, mismatches = FoodObservationService(source_storage, lambda: now), []
        for food_id in foods:
            left, right = service.food_view(food_id), repo.food_view(food_id)
            for source, view in left['sources'].items():
                theirs = right['sources'][source]
                ours = {k: view[k] for k in ('confirmed_items', 'mentions', 'search_matched_items', 'search_matches')}
                if ours != {k: theirs[k] for k in ours} or len(view['metrics']) != len(theirs['metrics']):
                    mismatches.append([food_id, source])
        out['checks']['food_views_compared'] = len(foods)
        out['checks']['food_view_mismatches'] = mismatches
    totals = out['tables'].values()
    out['summary'] = {'missing': sum(t['missing'] for t in totals), 'extra': sum(t['extra'] for t in totals),
                      'changed': sum(t['changed'] for t in totals),
                      'hash_mismatch_tables': sorted(n for n, t in out['tables'].items() if t['source_hash'] != t['target_hash']),
                      'null_mismatch_tables': sorted(n for n, t in out['tables'].items() if t['null_mismatch'])}
    out['consistent'] = (not any(out['summary'][k] for k in ('missing', 'extra', 'changed')) and not out['summary']['hash_mismatch_tables']
                         and not out['summary']['null_mismatch_tables'] and out['checks']['unresolved_payload_refs'] == 0
                         and out['checks']['blocked_policy_rows'] == 0 and out['checks']['expired_rows_in_target'] == 0
                         and out['checks']['foreign_key_errors'] in (0, 'enforced_by_deferred_constraints')
                         and not out['checks'].get('food_view_mismatches'))
    return out
