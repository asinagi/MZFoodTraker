"""B5 food-centred links over B4 staging.

Three separate facts. A query returned an item (ITEM_QUERY_MATCH, always SEARCH_ONLY). An approved
exact alias occurs in item evidence the source policy lets us analyse (FOOD_MENTION). A permitted
numeric observation (METRIC_OBSERVATION). A search hit never becomes a mention, YouTube content
extraction stays off, NAVER ratios stay in memory, and nothing is fuzzily merged.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from . import data_quality as dq
from .classifier import normalize
from .contracts import SOURCES, require_operation
from .food_service import NORMALIZATION_VERSION, RESOLUTION_VERSION, dump, identity
from .ingestion import METRIC_DEFINITIONS, PolicyBlocked, digest
from .instagram_analysis import normalize_tag
from .tracking_service import instant, stamp

LINK_VERSION = 'food-observation-v1'
# Mention extraction rides on derived operations each source policy already grants; none is added.
MENTION_POLICY = {'google_trends_rss': 'existing_food_rules', 'instagram_graph': 'existing_caption_hashtags'}
PARTICLES = '(?:은|는|이|가|의|을|를|와|과|에서|에|으로|로|도|만)'


def alias_pattern(alias):
    """Exact approved alias with word boundaries (Korean particles allowed); no space or fuzzy variants."""
    return re.compile(r'(?<![\w])' + re.escape(alias) + r'(?=$|[^\w]|' + PARTICLES + r'(?=$|[^\w]))')


def mention_policy(source):
    if source == 'naver_datalab':
        return 'NOT_APPLICABLE'  # metric-only source: there is no content to mention a food
    operation = MENTION_POLICY.get(source)
    if operation is None:
        return 'POLICY_BLOCKED'
    try:
        require_operation(source, operation)
    except ValueError:
        return 'POLICY_BLOCKED'
    return 'ALLOWED'


class FoodObservationService:
    def __init__(self, storage, clock=None):
        self.storage = storage
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self):
        return instant(self.clock())

    @staticmethod
    def _aliases(db):
        return [dict(r) for r in db.execute('''SELECT a.alias_id,a.food_id,a.normalized_form FROM food_aliases a
            JOIN food_entities f ON f.food_id=a.food_id WHERE a.status='ACTIVE' AND f.status='ACTIVE'
            AND a.normalization_version=? ORDER BY a.alias_id''', (NORMALIZATION_VERSION,))]

    def _evidence(self, db, item):
        """(fields, expires_at) from the item's existing store, or None when not analysable now."""
        if item['availability'] != 'AVAILABLE' or mention_policy(item['source_code']) != 'ALLOWED':
            return None
        kind, _, ref = item['payload_ref'].partition(':')
        if kind == 'items':
            row = db.execute('SELECT payload FROM items WHERE id=?', (int(ref),)).fetchone()
            payload = json.loads(row[0]) if row else None
            if not isinstance(payload, dict):
                return None
            fields = [('keyword', normalize(payload.get('keyword') or ''), 'TEXT')]
            for index, news in enumerate(payload.get('news') or []):
                if isinstance(news, dict) and isinstance(news.get('title'), str):
                    fields.append(('news_title:%d' % index, normalize(news['title']), 'TEXT'))
            return fields, None
        if kind == 'instagram_media':
            media = db.execute('SELECT * FROM instagram_media WHERE id=?', (ref,)).fetchone()
            if media is None or media['caption_purged_at'] is not None or media['caption'] is None:
                return None  # Derived tags are not re-read once the caption they came from is gone.
            captured = instant(media['caption_captured_at'] or media['first_seen'])
            expires = captured + timedelta(days=SOURCES['instagram_graph']['policy']['retention_days'])
            if expires <= self.now():
                return None
            tags = [r[0] for r in db.execute('SELECT normalized_name FROM instagram_caption_tags WHERE media_id=? AND active=1 ORDER BY normalized_name', (ref,))]
            return [('caption_hashtag', tag, 'HASHTAG') for tag in tags], stamp(expires)
        return None

    def _extract(self, db, item, aliases):
        evidence = self._evidence(db, item)
        if evidence is None:
            return None
        fields, expires = evidence
        snapshot = db.execute('SELECT snapshot_id FROM item_snapshots WHERE item_key=? AND purged_at IS NULL ORDER BY captured_at DESC LIMIT 1',
                              (item['item_key'],)).fetchone()
        found = {}
        for alias in aliases:
            for field, value, kind in fields:
                if kind == 'HASHTAG':
                    spans = [(None, None, 'EXACT_ALIAS_HASHTAG')] if normalize_tag(alias['normalized_form']) == value else []
                else:
                    spans = [(m.start(), m.end(), 'EXACT_ALIAS_FIELD' if (m.start(), m.end()) == (0, len(value)) else 'EXACT_ALIAS_TEXT')
                             for m in alias_pattern(alias['normalized_form']).finditer(value)]
                for start, end, status in spans:
                    key = digest([item['item_key'], alias['food_id'], alias['alias_id'], field, start, end, RESOLUTION_VERSION])
                    found[key] = dict(item_key=item['item_key'], snapshot_id=snapshot[0] if snapshot else None, food_id=alias['food_id'],
                                      alias_id=alias['alias_id'], field_code=field, span_start=start, span_end=end,
                                      verification_status=status, expires_at=expires)
        return found

    def _insert_mentions(self, db, found, origin, policy):
        created = 0
        for key, m in found.items():
            created += db.execute('''INSERT OR IGNORE INTO food_mentions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,NULL,NULL)''',
                (identity('MENTION'), key, m['item_key'], m['snapshot_id'], m['food_id'], m['alias_id'], m['field_code'],
                 m['span_start'], m['span_end'], RESOLUTION_VERSION, m['verification_status'], origin, policy,
                 m['expires_at'], stamp(self.now()))).rowcount
        return created

    def _insert_metric(self, db, run, metric):
        definition = METRIC_DEFINITIONS.get(metric['metric_code'])
        if not definition or not definition['storable'] or definition['source'] != run['source_code']:
            raise PolicyBlocked('This metric may only be processed in memory under its source policy')
        if dq.status(dq.metric_rules(metric, METRIC_DEFINITIONS, True)) == 'FAIL':
            return 0
        key = digest([run['ingest_run_id'], metric['metric_code'], metric['subject_key'], metric.get('provider_period')])
        return db.execute('''INSERT OR IGNORE INTO metric_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE')''',
            ('METRIC_' + key[:32], key, run['ingest_run_id'], run['source_code'], metric['metric_code'], definition['unit'],
             definition['subject_kind'], metric['subject_key'], metric.get('food_id'), metric.get('item_key'), metric['observed_at'],
             metric.get('period_start'), metric.get('period_end'), metric.get('provider_period'),
             dump(metric['comparison_context']), digest(metric['comparison_context']), metric['value_state'],
             metric.get('count_value'), metric.get('decimal_value'), metric.get('raw_value'), run['coverage'], run['origin'],
             run['policy_version'], metric.get('expires_at'))).rowcount

    def link_run(self, ingest_run_id):
        """Idempotent: matches, mentions and permitted metrics for one staged run. No HTTP."""
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT * FROM ingest_runs WHERE ingest_run_id=?', (ingest_run_id,)).fetchone()
            if run is None:
                return None
            items = [dict(r) for r in db.execute('''SELECT s.*,r.observed_at AS run_observed_at FROM run_items r
                JOIN source_items s USING(item_key) WHERE r.ingest_run_id=? ORDER BY s.item_key''', (ingest_run_id,))]
            aliases = self._aliases(db)
            matches = mentions = metrics = 0
            for item in items:
                for query_ref, query_id in self._query_refs(db, run, item):
                    matches += db.execute('''INSERT OR IGNORE INTO item_query_matches VALUES (?,?,?,?,?,?,'SEARCH_ONLY',?,?,?,'ACTIVE',?)''',
                        ('MATCH_' + digest([ingest_run_id, item['item_key'], query_ref])[:32], ingest_run_id, item['item_key'],
                         query_ref, query_id, item['run_observed_at'], run['origin'], 'run_items:' + ingest_run_id,
                         run['policy_version'], item['expires_at'])).rowcount
                found = self._extract(db, item, aliases)
                if found:
                    mentions += self._insert_mentions(db, found, run['origin'], run['policy_version'])
            for metric in self._metrics(db, run, items):
                metrics += self._insert_metric(db, run, metric)
        return {'ingest_run_id': ingest_run_id, 'matches': matches, 'mentions': mentions, 'metrics': metrics,
                'link_version': LINK_VERSION, 'http_requests': 0}

    def link_pending(self, limit=200):
        """Re-link staged runs; idempotent, so already linked runs are unchanged. No HTTP."""
        with self.storage.reader() as db:
            runs = [r[0] for r in db.execute('SELECT ingest_run_id FROM ingest_runs ORDER BY ingested_at DESC LIMIT ?', (limit,))]
        return {'linked_runs': len([r for r in runs if self.link_run(r)]), 'http_requests': 0}

    @staticmethod
    def _query_refs(db, run, item):
        if run['query_id']:
            return [('planner:' + run['query_id'], run['query_id'])]
        if run['source_code'] == 'youtube':
            return [('youtube_query:%d' % r[0], None) for r in db.execute('SELECT query_id FROM youtube_matches WHERE video_id=?', (item['external_key'],))]
        if run['source_code'] == 'instagram_graph':
            return [('instagram_hashtag:%d' % r[0], None) for r in db.execute('SELECT hashtag_id FROM instagram_hashtag_media WHERE media_id=?', (item['external_key'],))]
        return []  # The RSS feed is a shared discovery feed, not a food query.

    def _metrics(self, db, run, items):
        if run['source_code'] == 'google_trends_rss':
            out = []
            for item in items:
                kind, _, ref = item['payload_ref'].partition(':')
                row = db.execute('SELECT payload FROM items WHERE id=?', (int(ref),)).fetchone() if kind == 'items' else None
                traffic = json.loads(row[0]).get('traffic') if row else None
                out.append(dict(metric_code='rss.approx_traffic', unit='approximate_search_traffic_text', subject_key=item['external_key'],
                    item_key=item['item_key'], observed_at=item['run_observed_at'], comparison_context=json.loads(run['request_context_json']),
                    value_state='OBSERVED' if traffic else 'NOT_PROVIDED', raw_value=traffic or None, count_value=None, decimal_value=None))
            return out
        if run['source_code'] != 'instagram_graph' or not run['job_id']:
            return []
        foods = [r[0] for r in db.execute('''SELECT DISTINCT t.food_id FROM execution_mappings m JOIN tracking_targets t USING(target_id)
            WHERE m.job_id=? ORDER BY t.food_id''', (run['job_id'],))]
        observed = run['coverage'] in ('COMPLETE', 'PARTIAL', 'LIMITED')
        context = {'query_id': run['query_id'], 'request': json.loads(run['request_context_json']),
                   'basis': 'distinct_items_returned_by_this_query', 'window': 'provider_recent_media'}
        # A failed run is NOT_OBSERVED with no value; a complete run with no posts is an observed zero.
        return [dict(metric_code='instagram.observed_posts', unit='unique_observed_post', subject_key=food, food_id=food,
                     observed_at=run['observed_at'], comparison_context=context,
                     value_state='OBSERVED' if observed else 'NOT_OBSERVED', count_value=len(items) if observed else None,
                     decimal_value=None, raw_value=None) for food in foods]

    def apply_entity_changes(self):
        """Reprocess allowed evidence after alias/merge/split decisions; never edits provider values."""
        with self.storage.reader() as db:
            events = [dict(r) for r in db.execute('''SELECT * FROM food_events e WHERE subject_type IN ('alias','food')
                AND action IN ('APPROVED','CHANGED','REVERTED','MERGED')
                AND NOT EXISTS (SELECT 1 FROM entity_change_effects x WHERE x.food_event_id=e.event_id)
                ORDER BY occurred_at,event_id''')]
        return [self._apply(event) for event in events]

    def _apply(self, event):
        before = json.loads(event['before_json']) if event['before_json'] else {}
        after = json.loads(event['after_json']) if event['after_json'] else {}
        foods = sorted({v for v in (before.get('food_id'), after.get('food_id'), after.get('merged_into')) if v})
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            aliases = self._aliases(db)
            if event['subject_type'] == 'alias':
                old = {r[0] for r in db.execute("SELECT DISTINCT item_key FROM food_mentions WHERE alias_id=? AND status='ACTIVE'", (event['subject_id'],))}
                scan = after.get('status') == 'ACTIVE'
            else:
                old = {r[0] for r in db.execute("SELECT DISTINCT item_key FROM food_mentions WHERE food_id=? AND status='ACTIVE'", (event['subject_id'],))}
                scan = False
            keys = old | ({r[0] for r in db.execute("SELECT item_key FROM source_items WHERE availability='AVAILABLE' AND source_code IN (%s)"
                                                     % ','.join('?' * len(MENTION_POLICY)), tuple(MENTION_POLICY))} if scan else set())
            created = invalidated = blocked = 0
            for key in sorted(keys):
                item = dict(db.execute('SELECT * FROM source_items WHERE item_key=?', (key,)).fetchone())
                current = {r['mention_key']: dict(r) for r in db.execute("SELECT * FROM food_mentions WHERE item_key=? AND status='ACTIVE'", (key,))}
                found = self._extract(db, item, aliases)
                if found is None:
                    # Evidence expired or not analysable: only withdraw links whose alias mapping is no longer current.
                    blocked += 1
                    valid = {(a['alias_id'], a['food_id']) for a in aliases}
                    stale = [k for k, m in current.items() if (m['alias_id'], m['food_id']) not in valid]
                else:
                    stale = [k for k in current if k not in found]
                    run = db.execute('''SELECT g.origin,g.policy_version FROM run_items r JOIN ingest_runs g USING(ingest_run_id)
                        WHERE r.item_key=? ORDER BY r.observed_at DESC LIMIT 1''', (key,)).fetchone()
                    created += self._insert_mentions(db, {k: v for k, v in found.items() if k not in current}, run[0], run[1])
                for k in stale:
                    invalidated += db.execute("UPDATE food_mentions SET status='INVALIDATED',invalidated_at=?,invalidated_by=? WHERE mention_key=?",
                                              (stamp(self.now()), event['event_id'], k)).rowcount
                    foods.append(current[k]['food_id'])
            for food in sorted(set(foods)):
                db.execute('INSERT OR IGNORE INTO aggregate_invalidations VALUES (?,?,?,NULL,?,?,?,NULL)',
                           (identity('INVALIDATE'), digest([food, event['event_id']]), food, 'ENTITY_' + event['action'],
                            'food_events:' + event['event_id'], stamp(self.now())))
            db.execute('INSERT INTO entity_change_effects VALUES (?,?,?,?,?,?,?,?,?)',
                       (identity('EFFECT'), event['event_id'], event['subject_type'] + ':' + event['action'], dump(sorted(set(foods))),
                        invalidated, created, blocked, RESOLUTION_VERSION, stamp(self.now())))
        return {'food_event_id': event['event_id'], 'affected_food_ids': sorted(set(foods)), 'created_mentions': created,
                'invalidated_mentions': invalidated, 'not_reprocessable_items': blocked}

    def purge(self, at=None):
        """Links never outlive their evidence or the source item."""
        now = stamp(instant(at) if at else self.now())
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            gone = "item_key IN (SELECT item_key FROM source_items WHERE availability!='AVAILABLE')"
            matches = db.execute(f"UPDATE item_query_matches SET status='EXPIRED' WHERE status='ACTIVE' AND (expires_at<=? OR {gone})", (now,)).rowcount
            mentions = db.execute(f"UPDATE food_mentions SET status='EXPIRED' WHERE status='ACTIVE' AND (expires_at<=? OR {gone})", (now,)).rowcount
            metrics = db.execute(f"UPDATE metric_observations SET status='EXPIRED' WHERE status='ACTIVE' AND (expires_at<=? OR {gone})", (now,)).rowcount
        return {'expired_matches': matches, 'expired_mentions': mentions, 'expired_metrics': metrics, 'http_requests': 0}

    def food_view(self, food_id):
        """FOOD_ID-centred read: per source, distinct items and metrics with units; never summed across sources."""
        with self.storage.reader() as db:
            food = db.execute('SELECT * FROM food_entities WHERE food_id=?', (food_id,)).fetchone()
            if food is None:
                return None
            if food['status'] == 'MERGED':
                return dict(self.food_view(food['merged_into']), requested_food_id=food_id, redirected=True)
            ids = [food_id] + [r[0] for r in db.execute("SELECT food_id FROM food_entities WHERE merged_into=?", (food_id,))]
            marks = ','.join('?' * len(ids))
            sources = {}
            for source in ('google_trends_rss', 'instagram_graph', 'youtube', 'naver_datalab'):
                confirmed = db.execute(f'''SELECT count(DISTINCT m.item_key),count(*) FROM food_mentions m JOIN source_items s USING(item_key)
                    WHERE s.source_code=? AND m.status='ACTIVE' AND m.food_id IN ({marks})''', (source, *ids)).fetchone()
                searched = db.execute(f'''SELECT count(DISTINCT x.item_key),count(*) FROM item_query_matches x
                    JOIN planner_query_targets q ON q.query_id=x.query_id JOIN tracking_targets t ON t.target_id=q.target_id
                    JOIN source_items s ON s.item_key=x.item_key
                    WHERE s.source_code=? AND x.status='ACTIVE' AND t.food_id IN ({marks})''', (source, *ids)).fetchone()
                metrics = [dict(r) for r in db.execute(f'''SELECT metric_code,unit,observed_at,value_state,count_value,decimal_value,raw_value,
                    coverage,context_hash,origin FROM metric_observations WHERE source_code=? AND status='ACTIVE'
                    AND (food_id IN ({marks}) OR item_key IN (SELECT item_key FROM food_mentions WHERE status='ACTIVE' AND food_id IN ({marks})))
                    ORDER BY observed_at,metric_code''', (source, *ids, *ids))]
                sources[source] = {'mention_policy': mention_policy(source), 'confirmed_items': confirmed[0], 'mentions': confirmed[1],
                                   'search_matched_items': searched[0], 'search_matches': searched[1], 'metrics': metrics,
                                   'metric_storage': 'TRANSIENT_ONLY' if source == 'naver_datalab' else
                                                     'POLICY_BLOCKED' if source == 'youtube' else 'STORED'}
            return {'food_id': food_id, 'canonical_name': food['canonical_name'], 'merged_food_ids': ids[1:], 'sources': sources,
                    'semantics': {'confirmed_items': 'distinct items with an active exact-alias mention',
                                  'search_matched_items': 'distinct items returned by this food\'s queries (not a mention)',
                                  'metrics': 'per source, unit and comparison context; never summed across sources'},
                    'link_version': LINK_VERSION}
