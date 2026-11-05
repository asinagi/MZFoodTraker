"""ORACLE-RUNTIME-1 S4: B5 food observations (matches, mentions, permitted metrics, entity changes) on the target.

FoodObservationService's decisions run unchanged: exact approved aliases only, SEARCH_ONLY query
matches, mentions only where the source policy grants caption/feed analysis (never YouTube), NAVER
metrics never stored, failed runs NOT_OBSERVED rather than zero. Only its storage primitives are
re-pointed at target tables. Evidence comes from source_contents (RSS rows arrive through B7/Shadow or oracle_rss,
YouTube/Instagram from the Oracle runtime); the caption window is the content's field_expires_at.
The target's own policy checks (mention sources, storable metrics, origin labels) apply on every write.
Food and alias decisions stay on the SQLite system of record and reach the target through B7 or Shadow.
"""
import json
from .food_observations import LINK_VERSION, MENTION_POLICY, METRIC_VIEW, VIEW_SEMANTICS, FoodObservationService, mention_policy
from .food_service import NORMALIZATION_VERSION
from .oracle_execution import utc_value
from .target_repository import policy
from .tracking_service import instant, stamp


class OracleObservationService(FoodObservationService):
    def __init__(self, execution, clock=None):
        super().__init__(None, clock or execution.clock)
        self.execution = execution

    def _transaction(self):
        return self.execution.transaction()

    @staticmethod
    def _first(t, sql, table=None, order='', **binds):
        limit = ' FETCH FIRST 1 ROWS ONLY' if t.oracle else ' LIMIT 1'
        return t.one(sql + order + limit, table, **binds)

    def _aliases(self, t):
        return t.rows('''SELECT a.alias_id,a.food_id,a.normalized_form FROM food_aliases a
            JOIN food_entities f ON f.food_id=a.food_id WHERE a.status='ACTIVE' AND f.status='ACTIVE'
            AND a.normalization_version=:b_v ORDER BY a.alias_id''', b_v=NORMALIZATION_VERSION)

    def _rss_payload(self, t, ref):
        content = self.execution.content(t, 'items:' + ref)
        return content['content_json'] if content else None

    def _caption_evidence(self, t, ref):
        content = self.execution.content(t, 'instagram_media:' + ref)
        if content is None or content['field_purged_at'] is not None or content['field_expires_at'] is None:
            return None
        body = json.loads(content['content_json'])
        if body.get('caption') is None:
            return None  # Derived tags are not re-read once the caption they came from is gone.
        return sorted(body.get('caption_tags') or []), instant(content['field_expires_at'])

    def _latest_snapshot(self, t, item_key):
        row = self._first(t, 'SELECT snapshot_id FROM item_snapshots WHERE item_key=:b_k AND purged_at IS NULL', order=' ORDER BY captured_at DESC',
                          b_k=item_key)
        return row['snapshot_id'] if row else None

    def _absent(self, t, table, **where):
        clause = ' AND '.join('%s=:b_%s' % (k, k) for k in where)
        return not t.scalar('SELECT count(*) AS c FROM %s WHERE %s' % (table, clause), **{'b_' + k: v for k, v in where.items()})

    def _put_mention(self, t, row):
        policy('food_mentions', row, self.now())
        if not self._absent(t, 'food_mentions', mention_key=row['mention_key']):
            return 0
        t.insert('food_mentions', row)
        return 1

    def _put_metric(self, t, row):
        policy('metric_observations', row, self.now())
        if not self._absent(t, 'metric_observations', observation_key=row['observation_key']):
            return 0
        t.insert('metric_observations', row)
        return 1

    def _put_match(self, t, row):
        policy('item_query_matches', row, self.now())
        if not self._absent(t, 'item_query_matches', ingest_run_id=row['ingest_run_id'], item_key=row['item_key'], query_ref=row['query_ref']):
            return 0
        t.insert('item_query_matches', row)
        return 1

    def _run(self, t, ingest_run_id):
        return t.one('SELECT * FROM ingest_runs WHERE ingest_run_id=:b_r', 'ingest_runs', b_r=ingest_run_id)

    def _run_items(self, t, ingest_run_id):
        rows = t.rows('''SELECT r.item_key,r.observed_at FROM run_items r JOIN source_items s ON s.item_key=r.item_key
            WHERE r.ingest_run_id=:b_r ORDER BY s.item_key''', b_r=ingest_run_id)
        return [dict(self._item(t, r['item_key']), run_observed_at=stamp(utc_value(r['observed_at']))) for r in rows]

    def _recent_runs(self, limit):
        with self.execution.reader() as t:
            fetch = ' FETCH FIRST %d ROWS ONLY' % limit if t.oracle else ' LIMIT %d' % limit
            return [r['ingest_run_id'] for r in t.rows('SELECT ingest_run_id FROM ingest_runs ORDER BY ingested_at DESC,ingest_run_id' + fetch)]

    def _legacy_query_refs(self, t, run, item):
        return []  # youtube_matches / instagram_hashtag_media are SQLite-only; their links reach the target through B7

    def _job_foods(self, t, job_id):
        return [r['food_id'] for r in t.rows('''SELECT DISTINCT g.food_id FROM execution_mappings m JOIN tracking_targets g ON g.target_id=m.target_id
            WHERE m.job_id=:b_j ORDER BY g.food_id''', b_j=job_id)]

    def _pending_events(self):
        with self.execution.reader() as t:
            return t.rows('''SELECT * FROM food_events e WHERE subject_type IN ('alias','food')
                AND action IN ('APPROVED','CHANGED','REVERTED','MERGED')
                AND NOT EXISTS (SELECT 1 FROM entity_change_effects x WHERE x.food_event_id=e.event_id)
                ORDER BY occurred_at,event_id''', 'food_events')

    def _mentioned_items(self, t, column, value):
        return {r['item_key'] for r in t.rows("SELECT DISTINCT item_key FROM food_mentions WHERE %s=:b_v AND status='ACTIVE'" % column, b_v=value)}

    def _mentionable_items(self, t):
        sources = ','.join("'%s'" % s for s in MENTION_POLICY)
        return {r['item_key'] for r in t.rows("SELECT item_key FROM source_items WHERE availability='AVAILABLE' AND source_code IN (%s)" % sources)}

    def _item(self, t, item_key):
        return t.one('SELECT * FROM source_items WHERE item_key=:b_k', 'source_items', b_k=item_key)

    def _active_mentions(self, t, item_key):
        return {r['mention_key']: r for r in t.rows("SELECT * FROM food_mentions WHERE item_key=:b_k AND status='ACTIVE'", 'food_mentions', b_k=item_key)}

    def _latest_run(self, t, item_key):
        row = self._first(t, '''SELECT g.origin,g.policy_version FROM run_items r JOIN ingest_runs g ON g.ingest_run_id=r.ingest_run_id
            WHERE r.item_key=:b_k''', order=' ORDER BY r.observed_at DESC', b_k=item_key)
        return row['origin'], row['policy_version']

    def _invalidate(self, t, mention_key, event_id):
        return t.update('food_mentions', {'mention_key': mention_key}, status='INVALIDATED', invalidated_at=self.now(), invalidated_by=event_id)

    def _put_invalidation(self, t, row):
        if self._absent(t, 'aggregate_invalidations', invalidation_key=row['invalidation_key']):
            t.insert('aggregate_invalidations', row)

    def _put_effect(self, t, row):
        t.insert('entity_change_effects', row)

    def purge(self, at=None):
        """Links never outlive their evidence or the source item."""
        now = instant(at) if at else self.now()
        gone = "item_key IN (SELECT item_key FROM source_items WHERE availability<>'AVAILABLE')"
        with self.execution.transaction() as t:
            counts = [t.run(f"UPDATE {table} SET status='EXPIRED' WHERE status='ACTIVE' AND (expires_at<=:b_now OR {gone})", b_now=now).rowcount
                      for table in ('item_query_matches', 'food_mentions', 'metric_observations')]
        return {'expired_matches': counts[0], 'expired_mentions': counts[1], 'expired_metrics': counts[2], 'http_requests': 0}

    def food_view(self, food_id):
        """FoodObservationService.food_view over the target: same shape and per-source semantics."""
        with self.execution.reader() as t:
            food = t.one('SELECT * FROM food_entities WHERE food_id=:b_f', 'food_entities', b_f=food_id)
            if food is None:
                return None
            if food['status'] == 'MERGED':
                merged = food['merged_into']
            else:
                merged = None
                ids = [food_id] + [r['food_id'] for r in t.rows('SELECT food_id FROM food_entities WHERE merged_into=:b_f ORDER BY food_id', b_f=food_id)]
                marks = ','.join(':i%d' % n for n in range(len(ids)))
                binds = {'i%d' % n: v for n, v in enumerate(ids)}
                sources = {}
                for source in ('google_trends_rss', 'instagram_graph', 'youtube', 'naver_datalab'):
                    confirmed = t.one(f'''SELECT count(DISTINCT m.item_key) AS c1,count(*) AS c2 FROM food_mentions m JOIN source_items s ON s.item_key=m.item_key
                        WHERE s.source_code=:b_s AND m.status='ACTIVE' AND m.food_id IN ({marks})''', b_s=source, **binds)
                    searched = t.one(f'''SELECT count(DISTINCT x.item_key) AS c1,count(*) AS c2 FROM item_query_matches x
                        JOIN planner_query_targets q ON q.query_id=x.query_id JOIN tracking_targets g ON g.target_id=q.target_id
                        JOIN source_items s ON s.item_key=x.item_key WHERE s.source_code=:b_s AND x.status='ACTIVE' AND g.food_id IN ({marks})''',
                                     b_s=source, **binds)
                    metrics = t.rows(f'''SELECT * FROM metric_observations WHERE source_code=:b_s AND status='ACTIVE'
                        AND (food_id IN ({marks}) OR item_key IN (SELECT item_key FROM food_mentions WHERE status='ACTIVE' AND food_id IN ({marks})))
                        ORDER BY observed_at,metric_code,context_hash''', 'metric_observations', b_s=source, **binds)
                    sources[source] = {'mention_policy': mention_policy(source), 'confirmed_items': int(confirmed['c1']), 'mentions': int(confirmed['c2']),
                                       'search_matched_items': int(searched['c1']), 'search_matches': int(searched['c2']),
                                       'metrics': [{k: m[k] for k in METRIC_VIEW} for m in metrics],
                                       'metric_storage': 'TRANSIENT_ONLY' if source == 'naver_datalab' else
                                                         'POLICY_BLOCKED' if source == 'youtube' else 'STORED'}
        if merged is not None:
            return dict(self.food_view(merged), requested_food_id=food_id, redirected=True)
        return {'food_id': food_id, 'canonical_name': food['canonical_name'], 'merged_food_ids': ids[1:], 'sources': sources,
                'semantics': VIEW_SEMANTICS, 'link_version': LINK_VERSION}
