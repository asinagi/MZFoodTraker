"""ORACLE-RUNTIME-1 S3: B4 Source Envelope -> policy gate -> staging -> DQ on the target schema.

IngestionService.ingest() (envelope, credential/NAVER refusal, batch dedupe, DQ rules, metric gating)
runs unchanged; only its staging primitives are re-pointed at target tables through the Oracle
runtime's transactions. Records come from the runtime's source_contents and job receipts through the
existing YouTube/Instagram record builders; payload_hash is the stored source_payload_hash, so
snapshot keys equal the SQLite path's. NAVER values stay in memory. An RSS run collected on the target
is staged inside its own collection transaction (oracle_rss, through rss_observed); SQLite RSS runs and
the legacy backfill read SQLite-only stores, so their rows reach the target via B7 or Shadow. Expiry and
deletion are mirrored here; the target never revives an expired item.
"""
import hashlib
import json
from . import data_quality as dq
from .execution import TERMINAL
from .ingestion import IngestionService
from .oracle_execution import _states, utc_value
from .target_db import canonical_json
from .target_repository import policy
from .tracking_service import instant, stamp

CONTENT_PREFIX = {'youtube': 'youtube_videos:', 'instagram_graph': 'instagram_media:'}


def _instant_text(value):
    return None if value is None else stamp(utc_value(value))


class OracleIngestionService(IngestionService):
    def __init__(self, execution, clock=None):
        super().__init__(None, clock or execution.clock)
        self.execution = execution

    # Staging primitives over the target (insert-if-absent replaces INSERT OR IGNORE / ON CONFLICT).

    def _transaction(self):
        return self.execution.transaction()

    def _run_exists(self, t, run_id):
        return bool(t.scalar('SELECT count(*) AS c FROM ingest_runs WHERE ingest_run_id=:b_r', b_r=run_id))

    def _insert_run(self, t, row):
        t.insert('ingest_runs', row)

    def _finish_run(self, t, run_id, staged, rejected):
        t.update('ingest_runs', {'ingest_run_id': run_id}, staged_items=staged, rejected_items=rejected)

    def _staged_count(self, t, run_id):
        return int(t.scalar('SELECT count(*) AS c FROM run_items WHERE ingest_run_id=:b_r', b_r=run_id))

    def _quality_counts(self, t, run_id):
        return {r['outcome']: int(r['n']) for r in t.rows('SELECT outcome,count(*) AS n FROM quality_results WHERE ingest_run_id=:b_r GROUP BY outcome',
                                                         b_r=run_id)}

    def _put_quality(self, t, row):
        if not t.scalar('SELECT count(*) AS c FROM quality_results WHERE event_key=:b_k', b_k=row['event_key']):
            t.insert('quality_results', row)

    def _put_item(self, t, row):
        policy('source_items', row, self.now())
        old = t.one('SELECT * FROM source_items WHERE item_key=:b_k', 'source_items', b_k=row['item_key'])
        if old is None:
            t.insert('source_items', row)
            return
        # Same merge as the SQLite upsert: widen the seen window, keep the first published_at and origin.
        first = min((old['first_seen'], row['first_seen']), key=instant)
        last = max((old['last_seen'], row['last_seen']), key=instant)
        t.update('source_items', {'item_key': row['item_key']}, first_seen=first, last_seen=last,
                 published_at=old['published_at'] if old['published_at'] is not None else row['published_at'],
                 payload_ref=row['payload_ref'], expires_at=row['expires_at'], availability='AVAILABLE')

    def _put_snapshot(self, t, row):
        if not t.scalar('SELECT count(*) AS c FROM item_snapshots WHERE snapshot_id=:b_s', b_s=row['snapshot_id']):
            t.insert('item_snapshots', row)

    def _put_run_item(self, t, row):
        if not t.scalar('SELECT count(*) AS c FROM run_items WHERE ingest_run_id=:b_r AND item_key=:b_k', b_r=row['ingest_run_id'], b_k=row['item_key']):
            t.insert('run_items', row)

    # Source adapters over the Oracle runtime's stores.

    def _content_record(self, source, content, origin):
        """The SQLite record builders applied to the stored content; the hash is the source store's."""
        if content is None:
            return None
        body = json.loads(content['content_json'])
        if source == 'youtube':
            record = self._youtube_record(dict(video_id=body['video_id'], payload=content['content_json'], refreshed_at=content['refreshed_at'],
                                               expires_at=content['expires_at'], collected_at=content['captured_at']), origin)
            record['payload_hash'] = content['source_payload_hash'] or hashlib.sha256(content['content_json'].encode()).hexdigest()
            return record
        return self._instagram_record(dict(id=body['id'], published_at=body.get('published_at'), media_type=body.get('media_type'),
                                           permalink=body.get('permalink'), caption=body.get('caption'), caption_purged_at=content['field_purged_at'],
                                           first_seen=content['captured_at'], last_seen=content['refreshed_at']), origin)

    def execution_run(self, run_id):
        """Stage a terminal Oracle-runtime run from its job receipts. Non-terminal runs return None."""
        now = self.now()
        with self.execution.reader() as t:
            row = t.one('''SELECT r.run_id,r.job_id,r.origin,r.observed_at,r.result_coverage,r.returned_count,r.accepted_count,
                j.query_id,j.plan_id,j.source_id,j.status AS job_status,j.error_kind AS job_error
                FROM execution_runs r JOIN execution_jobs j ON j.job_id=r.job_id WHERE r.run_id=:b_r''', b_r=run_id)
            if row is None or row['job_status'] not in TERMINAL:
                return None
            plan = self.execution._plan(t, row['plan_id'])
            source, contents = row['source_id'], []
            receipts = [r['item_id'] for r in t.rows('''SELECT item_id FROM execution_item_receipts WHERE job_id=:b_j AND expires_at>:b_now
                ORDER BY item_id''', b_j=row['job_id'], b_now=now)]
            for item in receipts:
                record = self._content_record(source, self.execution.content(t, CONTENT_PREFIX[source] + item), row['origin'])
                contents.append(record or {'external_key': item, 'source_absent': True})
        count = (lambda v: None if v is None else int(v))
        return self.ingest(dict(source_code=source, run_ref='execution_runs:' + run_id, origin=row['origin'],
            job_id=row['job_id'], query_id=row['query_id'], execution_status=row['job_status'],
            coverage=row['result_coverage'] or 'NONE', error_kind=row['job_error'],
            request_context=plan['contract']['request_conditions'], observed_at=_instant_text(row['observed_at']) or stamp(now),
            returned_count=count(row['returned_count']), accepted_count=count(row['accepted_count']),
            source_schema_version=dq.KNOWN_SCHEMAS[source]), contents)

    def ingest_pending(self, limit=100):
        """Explicit local catch-up for terminal Oracle-runtime runs not yet staged. No HTTP."""
        with self.execution.reader() as t:
            fetch = 'FETCH FIRST %d ROWS ONLY' % limit if t.oracle else 'LIMIT %d' % limit
            runs = [r['run_id'] for r in t.rows(f'''SELECT r.run_id FROM execution_runs r JOIN execution_jobs j ON j.job_id=r.job_id
                WHERE j.status IN {_states(TERMINAL)} AND NOT EXISTS (SELECT 1 FROM ingest_runs g WHERE g.run_ref='execution_runs:'||r.run_id)
                ORDER BY j.finished_at,r.run_id {fetch}''')]
        results = [self.execution_run(r) for r in runs]
        return {'ingested': len([r for r in results if r]), 'http_requests': 0}

    def rss_run(self, run_id):
        raise ValueError('RSS runs collected on the target are staged in their own collection transaction (oracle_rss); '
                         'SQLite runs reach the target through B7 or Shadow')

    def legacy_backfill(self, source):
        raise ValueError('Legacy backfill reads SQLite-only stores; use B7 migration for the target')

    def purge(self, at=None):
        """Mirror source expiry and deletion, then apply the runtime's own source retention."""
        now = instant(at) if at else self.now()
        with self.execution.transaction() as t:
            expired = t.run("""UPDATE source_items SET availability='EXPIRED'
                WHERE availability='AVAILABLE' AND expires_at IS NOT NULL AND expires_at<=:b_now""", b_now=now).rowcount
            deleted = t.run("""UPDATE source_items SET availability='DELETED' WHERE availability='AVAILABLE'
                AND source_code IN ('youtube','instagram_graph')
                AND NOT EXISTS (SELECT 1 FROM source_contents c WHERE c.payload_ref=source_items.payload_ref)""").rowcount
            snapshots = t.run("""UPDATE item_snapshots SET purged_at=:b_now WHERE purged_at IS NULL AND
                (expires_at<=:b_now OR item_key IN (SELECT item_key FROM source_items WHERE availability<>'AVAILABLE'))""", b_now=now).rowcount
            # Runtime-owned retention (YouTubeStore.purge, the Instagram caption window): never longer than the source allows.
            contents = t.run("DELETE FROM source_contents WHERE source_code='youtube' AND expires_at<=:b_now", b_now=now).rowcount
            captions = 0
            for row in t.rows('''SELECT * FROM source_contents WHERE source_code='instagram_graph' AND field_purged_at IS NULL
                    AND field_expires_at IS NOT NULL AND field_expires_at<=:b_now''', 'source_contents', b_now=now):
                body = {k: v for k, v in json.loads(row['content_json']).items() if k not in ('caption', 'caption_tags')}
                t.update('source_contents', {'payload_ref': row['payload_ref']}, content_json=json.dumps(body, ensure_ascii=False),
                         content_hash=hashlib.sha256(canonical_json(body).encode()).hexdigest(), field_purged_at=now)
                captions += 1
        return {'expired_items': expired, 'deleted_items': deleted, 'purged_snapshots': snapshots,
                'purged_contents': contents, 'purged_captions': captions, 'http_requests': 0}
