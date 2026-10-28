"""B4 common ingestion: existing source stores -> Source Envelope -> policy gate -> staging -> DQ.

Adapters read what each collector already persisted under its own policy. No provider call
happens here and no provider body is copied: staging keeps lineage, hashes, field states and a
reference into the existing store. NAVER values are processed in memory only. Deterministic keys
make re-ingesting the same input a no-op.
"""
import hashlib
import json
import logging
from datetime import datetime, timezone
from . import data_quality as dq
from .contracts import envelope, validate_metadata
from .execution import TERMINAL
from .food_service import dump, identity
from .tracking_service import instant, stamp

INGESTION_VERSION = 'common-ingestion-v1'
KEY_KINDS = {'google_trends_rss': 'rss_identity_sha256', 'instagram_graph': 'ig_media_id', 'youtube': 'youtube_video_id'}
# Persistable after B5 food linking only where the source policy already stores the value.
METRIC_DEFINITIONS = {
    'rss.approx_traffic': {'source': 'google_trends_rss', 'unit': 'approximate_search_traffic_text',
                           'subject_kind': 'ITEM', 'aggregation': 'RAW_TEXT', 'storable': True},
    'instagram.observed_posts': {'source': 'instagram_graph', 'unit': 'unique_observed_post',
                                 'subject_kind': 'FOOD', 'aggregation': 'DISTINCT_ITEM', 'storable': True},
    'youtube.view_count': {'source': 'youtube', 'unit': 'video_view_count_snapshot',
                           'subject_kind': 'ITEM', 'aggregation': 'SNAPSHOT', 'storable': False},
    'naver.relative_search_index': {'source': 'naver_datalab', 'unit': 'relative_search_index_0_100',
                                    'subject_kind': 'QUERY_GROUP', 'aggregation': 'RATIO', 'storable': False},
}
METRIC_STORAGE = {'google_trends_rss': 'STORED', 'instagram_graph': 'STORED', 'youtube': 'NONE', 'naver_datalab': 'TRANSIENT_ONLY'}


class PolicyBlocked(ValueError):
    pass


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def item_key(source, external_key):
    return 'ITEM_' + digest([source, external_key])[:32]


def field_states(source, origin, fields, collected_at):
    return envelope(source, origin, collected_at, fields=fields)['field_states']


class IngestionService:
    def __init__(self, storage, clock=None):
        self.storage = storage
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self):
        return instant(self.clock())

    def _quality(self, db, run_id, item, subject, rule):
        key = digest([run_id, item or subject, rule['rule_code'], dq.RULE_VERSION])
        db.execute('INSERT OR IGNORE INTO quality_results VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (identity('DQ'), key, run_id, item, subject, rule['rule_code'], dq.RULE_VERSION, rule['severity'],
                    rule['outcome'], dq.details_json(rule['details']), stamp(self.now())))

    def ingest(self, run, contents=(), metrics=()):
        """One transaction per logical run. Returns storable and transient metric records for B5."""
        source = run['source_code']
        try:
            validate_metadata(run['request_context'])
        except ValueError:
            raise PolicyBlocked('Credential-shaped or non-JSON request metadata is refused') from None
        if source == 'naver_datalab' and contents:
            raise PolicyBlocked('NAVER responses are metrics only and are never staged as content')
        contract = envelope(source, run['origin'], run['observed_at'], request=run['request_context'])
        now = self.now()
        run_id = 'INGEST_' + digest([source, run['run_ref']])[:32]
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            created = not db.execute('SELECT 1 FROM ingest_runs WHERE ingest_run_id=?', (run_id,)).fetchone()
            if created:
                db.execute('''INSERT INTO ingest_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?)''',
                    (run_id, source, run['run_ref'], run['origin'], run.get('job_id'), run.get('query_id'),
                     contract['contract_version'], contract['adapter_version'], run['source_schema_version'],
                     contract['policy_version'], INGESTION_VERSION, run['observed_at'], run.get('execution_status'),
                     run['coverage'], run.get('error_kind'), dump(run['request_context']), digest(run['request_context']),
                     run.get('returned_count'), run.get('accepted_count'), METRIC_STORAGE[source], stamp(now)))
            for rule in dq.run_rules(run):
                self._quality(db, run_id, None, None, rule)
            batch = {}
            for record in contents:
                previous = batch.get(record.get('external_key'))
                batch[record.get('external_key')] = (record, previous[1] + 1 if previous else 1)
            rejected = expired = absent = 0
            for record, count in batch.values():
                rules = dq.content_rules(source, record)
                if count > 1:
                    rules.append(dq.result('duplicate.batch', 'WARN', multiplicity=count))
                subject = digest([source, str(record.get('external_key'))])
                if dq.status(rules) == 'FAIL':
                    for rule in rules:
                        self._quality(db, run_id, None, subject, rule)
                    rejected += 1
                    continue
                if record.get('source_absent') or record.get('expires_at') and instant(record['expires_at']) <= now:
                    # Expired or deleted upstream: never staged, never revived from a later re-ingest.
                    state = 'source_row_absent' if record.get('source_absent') else 'expired_not_staged'
                    self._quality(db, run_id, None, subject, dq.result('retention.active', 'NOT_APPLICABLE', state=state))
                    absent += bool(record.get('source_absent')); expired += not record.get('source_absent')
                    continue
                self._stage(db, run_id, run, source, record, count, rules)
            stored, transient = [], []
            for metric in metrics:
                definition = METRIC_DEFINITIONS.get(metric['metric_code'])
                storable = bool(definition and definition['storable'] and definition['source'] == source
                                and METRIC_STORAGE[source] == 'STORED')
                rules = dq.metric_rules(metric, METRIC_DEFINITIONS, storable)
                subject = digest([source, metric['metric_code'], metric['subject_key'], metric.get('provider_period')])
                for rule in rules:
                    self._quality(db, run_id, None, subject, rule)
                checked = dict(metric, quality=dq.status(rules))
                (stored if storable and checked['quality'] != 'FAIL' else transient).append(checked)
            staged = db.execute('SELECT count(*) FROM run_items WHERE ingest_run_id=?', (run_id,)).fetchone()[0]
            if created:
                db.execute('UPDATE ingest_runs SET staged_items=?,rejected_items=? WHERE ingest_run_id=?', (staged, rejected, run_id))
            quality = dict(db.execute('SELECT outcome,count(*) FROM quality_results WHERE ingest_run_id=? GROUP BY outcome', (run_id,)).fetchall())
        return {'ingest_run_id': run_id, 'created': created, 'staged_items': staged, 'rejected_items': rejected,
                'expired_items': expired, 'source_absent_items': absent, 'quality': quality,
                'storable_metrics': stored, 'transient_metrics': transient, 'http_requests': 0}

    def _stage(self, db, run_id, run, source, record, count, rules):
        key = item_key(source, record['external_key'])
        seen = record.get('observed_at', run['observed_at'])
        first = record.get('first_seen', seen)
        db.execute('''INSERT INTO source_items VALUES (?,?,?,?,?,?,?,'AVAILABLE',?,?,?)
            ON CONFLICT(item_key) DO UPDATE SET first_seen=min(first_seen,excluded.first_seen),
            last_seen=max(last_seen,excluded.last_seen), published_at=coalesce(source_items.published_at,excluded.published_at),
            payload_ref=excluded.payload_ref, expires_at=excluded.expires_at, availability='AVAILABLE' ''',
            (key, source, record['external_key'], KEY_KINDS[source], record.get('published_at'), first, seen,
             record.get('origin', run['origin']), record['payload_ref'], record.get('expires_at')))
        adapter = envelope(source, run['origin'], run['observed_at'])['adapter_version']
        snapshot = 'SNAP_' + digest([key, record['payload_hash'], adapter])[:32]
        db.execute('INSERT OR IGNORE INTO item_snapshots VALUES (?,?,?,?,?,?,?,?,?,NULL)',
                   (snapshot, key, record['payload_hash'], adapter, run['source_schema_version'],
                    dump(record.get('field_states', {})), record['payload_ref'], seen, record.get('expires_at')))
        db.execute('INSERT OR IGNORE INTO run_items VALUES (?,?,?,?,?,?,?)',
                   (run_id, key, snapshot, seen, record.get('origin', run['origin']), count, dq.status(rules)))
        for rule in rules:
            self._quality(db, run_id, key, None, rule)

    # Source adapters over existing stores.

    def rss_run(self, run_id):
        with self.storage.reader() as db:
            run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if run is None or run['finished_at'] is None:
                return None
            rows = db.execute('''SELECT o.payload AS observed,o.contract_json,i.id,i.source_id,i.published_at
                FROM observations o JOIN items i ON i.id=o.item_id WHERE o.run_id=? ORDER BY i.id''', (run_id,)).fetchall()
        request = (json.loads(run['contract_json']) if run['contract_json'] else {}).get('request_conditions') or {'geo': 'KR', 'endpoint': 'trending/rss'}
        contents, metrics = [], []
        for row in rows:
            try:
                payload = json.loads(row['observed']); valid = isinstance(payload, dict)
            except ValueError:
                payload, valid = {}, False
            payload = payload if valid else {}
            observed = json.loads(row['contract_json']) if row['contract_json'] else {}
            news = payload.get('news') if isinstance(payload.get('news'), list) else []
            contents.append(dict(external_key=row['source_id'], published_at=row['published_at'], payload_valid=valid,
                fields={'keyword': payload.get('keyword'), 'traffic': payload.get('traffic'),
                        'news_title': [n.get('title') for n in news if isinstance(n, dict)]},
                field_states=observed.get('field_states') or {}, payload_ref='items:%d' % row['id'],
                payload_hash=hashlib.sha256(row['observed'].encode()).hexdigest(), expires_at=None))
            traffic = payload.get('traffic')
            metrics.append(dict(metric_code='rss.approx_traffic', unit='approximate_search_traffic_text', subject_kind='ITEM',
                subject_key=row['source_id'], observed_at=run['finished_at'], period_start=None, period_end=None,
                provider_period=None, comparison_context=request, value_state='OBSERVED' if traffic else 'NOT_PROVIDED',
                count_value=None, decimal_value=None, raw_value=traffic or None))
        return self.ingest(dict(source_code='google_trends_rss', run_ref='runs:%d' % run_id, origin=run['origin'],
            execution_status='SUCCEEDED' if run['success'] else 'FAILED', coverage='COMPLETE' if run['success'] else 'NONE',
            error_kind=None if run['success'] else 'collection_failed', request_context=request, observed_at=run['finished_at'],
            returned_count=run['item_count'], accepted_count=run['new_count'],
            source_schema_version=dq.KNOWN_SCHEMAS['google_trends_rss']), contents, metrics)

    def _youtube_record(self, row, origin):
        if row is None:
            return None
        payload = json.loads(row['payload'])
        return dict(external_key=row['video_id'], published_at=payload.get('published_at'), origin=origin,
            fields={k: payload.get(k) for k in ('title', 'description', 'channel_title', 'view_count', 'like_count', 'comment_count')},
            field_states=field_states('youtube', origin, {k: v for k, v in payload.items()
                                                           if k in ('video_id', 'title', 'description', 'published_at', 'channel_id',
                                                                    'channel_title', 'thumbnail_url', 'view_count', 'like_count',
                                                                    'comment_count', 'duration', 'privacy_status')}, row['refreshed_at']),
            payload_ref='youtube_videos:' + row['video_id'], payload_hash=hashlib.sha256(row['payload'].encode()).hexdigest(),
            expires_at=row['expires_at'], first_seen=row['collected_at'], observed_at=row['refreshed_at'])

    def _instagram_record(self, row, origin):
        if row is None:
            return None
        caption = row['caption']
        visible = {'id': row['id'], 'published_at': row['published_at'], 'media_type': row['media_type'], 'permalink': row['permalink']}
        if row['caption_purged_at'] is None:
            visible['caption'] = caption
        states = field_states('instagram_graph', origin, visible, row['last_seen'])
        if row['caption_purged_at'] is not None:
            states['caption'] = 'expired'
        caption_hash = hashlib.sha256(caption.encode()).hexdigest() if caption else None
        return dict(external_key=row['id'], published_at=row['published_at'], origin=origin,
            fields={'caption': caption, 'media_type': row['media_type']}, field_states=states,
            payload_ref='instagram_media:' + row['id'],
            payload_hash=digest([row['id'], row['published_at'], row['media_type'], row['permalink'], caption_hash]),
            expires_at=None, first_seen=row['first_seen'], observed_at=row['last_seen'])

    def execution_run(self, run_id):
        """Stage a terminal B3 run from its job receipts. Non-terminal runs return None."""
        now = stamp(self.now())
        with self.storage.reader() as db:
            row = db.execute('''SELECT r.*,j.query_id,j.plan_id,j.source_id,j.status AS job_status,j.error_kind AS job_error
                FROM execution_runs r JOIN execution_jobs j USING(job_id) WHERE r.run_id=?''', (run_id,)).fetchone()
            if row is None or row['job_status'] not in TERMINAL:
                return None
            plan = json.loads(db.execute('SELECT payload_json FROM planner_plans WHERE plan_id=?', (row['plan_id'],)).fetchone()[0])
            receipts = [r[0] for r in db.execute('SELECT item_id FROM execution_item_receipts WHERE job_id=? AND expires_at>? ORDER BY item_id',
                                                  (row['job_id'], now))]
            source, contents = row['source_id'], []
            for item in receipts:
                if source == 'youtube':
                    record = self._youtube_record(db.execute('SELECT * FROM youtube_videos WHERE video_id=?', (item,)).fetchone(), row['origin'])
                else:
                    record = self._instagram_record(db.execute('SELECT * FROM instagram_media WHERE id=?', (item,)).fetchone(), row['origin'])
                contents.append(record or {'external_key': item, 'source_absent': True})
        return self.ingest(dict(source_code=source, run_ref='execution_runs:' + run_id, origin=row['origin'],
            job_id=row['job_id'], query_id=row['query_id'], execution_status=row['job_status'],
            coverage=row['result_coverage'] or 'NONE', error_kind=row['job_error'],
            request_context=plan['contract']['request_conditions'], observed_at=row['observed_at'] or now,
            returned_count=row['returned_count'], accepted_count=row['accepted_count'],
            source_schema_version=dq.KNOWN_SCHEMAS[source]), contents)

    def legacy_backfill(self, source):
        """Origin-unknown rows become one labelled logical run; never presented as a recorded API run."""
        now = stamp(self.now())
        with self.storage.reader() as db:
            if source == 'youtube':
                rows = db.execute('''SELECT v.* FROM youtube_videos v LEFT JOIN youtube_video_provenance p USING(video_id)
                    WHERE (p.video_id IS NULL OR p.origin='legacy_unknown') AND v.expires_at>? ORDER BY v.video_id''', (now,)).fetchall()
                contents = [self._youtube_record(r, 'legacy_unknown') for r in rows]
            elif source == 'instagram_graph':
                rows = db.execute("SELECT * FROM instagram_media WHERE origin='legacy_unknown' ORDER BY id").fetchall()
                contents = [self._instagram_record(r, 'legacy_unknown') for r in rows]
            else:
                raise ValueError('Legacy backfill covers stored YouTube and Instagram content only')
        return self.ingest(dict(source_code=source, run_ref='legacy:%s:origin-unknown' % source, origin='legacy_unknown',
            execution_status=None, coverage='UNKNOWN', error_kind=None, request_context={}, observed_at=now,
            returned_count=None, accepted_count=None, source_schema_version=dq.KNOWN_SCHEMAS[source]), contents)

    def ingest_pending(self, limit=100):
        """Explicit local catch-up for finished runs not yet staged. No HTTP."""
        with self.storage.reader() as db:
            rss = [r[0] for r in db.execute('''SELECT id FROM runs WHERE finished_at IS NOT NULL AND NOT EXISTS
                (SELECT 1 FROM ingest_runs g WHERE g.run_ref='runs:'||runs.id) ORDER BY id LIMIT ?''', (limit,))]
            runs = [r[0] for r in db.execute(f'''SELECT r.run_id FROM execution_runs r JOIN execution_jobs j USING(job_id)
                WHERE j.status IN {TERMINAL} AND NOT EXISTS (SELECT 1 FROM ingest_runs g WHERE g.run_ref='execution_runs:'||r.run_id)
                ORDER BY j.finished_at LIMIT ?''', (limit,))]
        results = [self.rss_run(r) for r in rss] + [self.execution_run(r) for r in runs]
        return {'ingested': len([r for r in results if r]), 'http_requests': 0}

    def purge(self, at=None):
        """Mirror source expiry and deletion; staged lineage never outlives the source row."""
        now = stamp(instant(at) if at else self.now())
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            expired = db.execute("""UPDATE source_items SET availability='EXPIRED'
                WHERE availability='AVAILABLE' AND expires_at IS NOT NULL AND expires_at<=?""", (now,)).rowcount
            deleted = db.execute("""UPDATE source_items SET availability='DELETED' WHERE availability='AVAILABLE' AND
                ((source_code='youtube' AND NOT EXISTS (SELECT 1 FROM youtube_videos v WHERE v.video_id=source_items.external_key)) OR
                 (source_code='instagram_graph' AND NOT EXISTS (SELECT 1 FROM instagram_media m WHERE m.id=source_items.external_key)))""").rowcount
            snapshots = db.execute("""UPDATE item_snapshots SET purged_at=? WHERE purged_at IS NULL AND
                (expires_at<=? OR item_key IN (SELECT item_key FROM source_items WHERE availability!='AVAILABLE'))""", (now, now)).rowcount
        return {'expired_items': expired, 'deleted_items': deleted, 'purged_snapshots': snapshots, 'http_requests': 0}


def naver_metrics(body, results, observed_at):
    """Request-scoped NAVER relative index records, held in memory only.

    The comparison context is the whole request: every group, period, timeUnit and filter. Values
    from different contexts are never comparable. A group without points is NOT_PROVIDED, not zero.
    """
    context = {k: body[k] for k in sorted(body)}
    context_hash = digest(context)
    base = dict(metric_code='naver.relative_search_index', unit='relative_search_index_0_100', subject_kind='QUERY_GROUP',
                observed_at=observed_at, period_start=None, period_end=None, comparison_context=context,
                context_hash=context_hash, count_value=None, raw_value=None)
    out = []
    for group in results:
        food = group['title'] if group['title'].startswith('FOOD_') else None
        if not group['data']:
            out.append(dict(base, subject_key=group['title'], food_id=food, provider_period=None,
                            value_state='NOT_PROVIDED', decimal_value=None))
        for point in group['data']:
            out.append(dict(base, subject_key=group['title'], food_id=food, value_state='OBSERVED',
                            provider_period='%s:%s' % (body['timeUnit'], point['period']), decimal_value=point['ratio']))
    return out


def ingest_safely(callback, *args):
    """Ingestion never rewrites a completed collection; failures stay pending for ingest_pending()."""
    try:
        return callback(*args)
    except Exception:
        logging.warning('Common ingestion pending retry; collected source rows are preserved')
        return {'state': 'pending_retry'}
