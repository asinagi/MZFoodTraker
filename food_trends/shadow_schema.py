"""SQLite v11: change capture for the B7 Oracle shadow. Disabled by default.

Triggers record only (table, key, operation) in the same SQLite transaction as the write, so only
committed changes are ever delivered. They fire only while shadow_settings.enabled=1; enabling is
an explicit operator step, so existing writers are unaffected until then. No values are copied.
"""
# Source table -> key columns recorded in the event (the deliverer re-reads the current row).
CAPTURED = {
    'food_commands': ('request_key',), 'food_entities': ('food_id',), 'food_aliases': ('alias_id',),
    'food_relations': ('relation_id',), 'food_candidates': ('candidate_id',), 'food_events': ('event_id',),
    'tracking_targets': ('target_id',), 'planner_queries': ('query_id',), 'planner_plans': ('plan_id',),
    'planner_query_targets': ('query_id', 'target_id', 'generation', 'food_revision'), 'quota_reservations': ('reservation_id',),
    'execution_jobs': ('job_id',), 'execution_runs': ('run_id',), 'execution_calls': ('call_id',), 'execution_outbox': ('event_id',),
    'execution_results': ('run_id', 'target_id'), 'ingest_runs': ('ingest_run_id',), 'source_items': ('item_key',),
    'item_snapshots': ('snapshot_id',), 'run_items': ('ingest_run_id', 'item_key'), 'quality_results': ('result_id',),
    'item_query_matches': ('match_id',), 'food_mentions': ('mention_id',), 'metric_observations': ('observation_id',),
    'entity_change_effects': ('effect_id',), 'aggregate_invalidations': ('invalidation_id',),
    'items': ('id',), 'candidates': ('item_id',), 'classification_history': ('id',), 'youtube_videos': ('video_id',),
    'youtube_video_provenance': ('video_id',), 'instagram_media': ('id',), 'instagram_caption_tags': ('media_id',),
}
SQL = '''
CREATE TABLE shadow_settings (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), updated_at TEXT NOT NULL);
INSERT INTO shadow_settings VALUES (1,0,'1970-01-01T00:00:00+00:00');
CREATE TABLE shadow_outbox (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, entity_table TEXT NOT NULL, entity_key TEXT NOT NULL CHECK(json_valid(entity_key)),
 operation TEXT NOT NULL CHECK(operation IN ('UPSERT','DELETE')), created_at TEXT NOT NULL, delivered_at TEXT,
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0), next_attempt_at TEXT, last_error_code TEXT);
CREATE INDEX shadow_pending ON shadow_outbox(delivered_at,next_attempt_at,event_id);
'''


def triggers():
    out = []
    for table, keys in CAPTURED.items():
        for event, row in (('INSERT', 'NEW'), ('UPDATE', 'NEW'), ('DELETE', 'OLD')):
            out.append('''CREATE TRIGGER shadow_%s_%s AFTER %s ON %s
 WHEN (SELECT enabled FROM shadow_settings WHERE singleton=1)=1
 BEGIN INSERT INTO shadow_outbox(entity_table,entity_key,operation,created_at)
 VALUES ('%s',json_array(%s),'%s',strftime('%%Y-%%m-%%dT%%H:%%M:%%fZ','now')); END''' % (
                table, event.lower(), event, table, table, ','.join('%s.%s' % (row, k) for k in keys),
                'DELETE' if event == 'DELETE' else 'UPSERT'))
    return out


def apply(db):
    for statement in SQL.split(';'):
        if statement.strip():
            db.execute(statement)
    for statement in triggers():
        db.execute(statement)
