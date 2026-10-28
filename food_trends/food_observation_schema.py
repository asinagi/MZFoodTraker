"""B5 food-centred links. Search evidence, confirmed mentions and metrics are separate facts."""
ORIGINS = "('live_api','mock','user','legacy_unknown')"
SQL = f'''
CREATE TABLE item_query_matches (
 match_id TEXT PRIMARY KEY, ingest_run_id TEXT NOT NULL, item_key TEXT NOT NULL,
 query_ref TEXT NOT NULL, query_id TEXT REFERENCES planner_queries(query_id), matched_at TEXT NOT NULL,
 verification_status TEXT NOT NULL CHECK(verification_status IN ('SEARCH_ONLY')),
 origin TEXT NOT NULL CHECK(origin IN {ORIGINS}), evidence_ref TEXT NOT NULL, policy_version TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','EXPIRED')), expires_at TEXT,
 FOREIGN KEY(ingest_run_id,item_key) REFERENCES run_items(ingest_run_id,item_key),
 UNIQUE(ingest_run_id,item_key,query_ref));
CREATE INDEX item_query_match_query ON item_query_matches(query_id,status,item_key);
CREATE INDEX item_query_match_ref ON item_query_matches(query_ref,matched_at);
CREATE INDEX item_query_match_item ON item_query_matches(item_key);
CREATE INDEX item_query_match_expiry ON item_query_matches(expires_at);
CREATE TABLE food_mentions (
 mention_id TEXT PRIMARY KEY, mention_key TEXT NOT NULL UNIQUE, item_key TEXT NOT NULL REFERENCES source_items(item_key),
 snapshot_id TEXT REFERENCES item_snapshots(snapshot_id), food_id TEXT NOT NULL REFERENCES food_entities(food_id),
 alias_id TEXT NOT NULL REFERENCES food_aliases(alias_id), field_code TEXT NOT NULL, span_start INTEGER, span_end INTEGER,
 resolution_version TEXT NOT NULL,
 verification_status TEXT NOT NULL CHECK(verification_status IN ('EXACT_ALIAS_FIELD','EXACT_ALIAS_TEXT','EXACT_ALIAS_HASHTAG')),
 origin TEXT NOT NULL CHECK(origin IN {ORIGINS}), policy_version TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','INVALIDATED','EXPIRED')), expires_at TEXT, created_at TEXT NOT NULL,
 invalidated_at TEXT, invalidated_by TEXT,
 CHECK((span_start IS NULL AND span_end IS NULL) OR (span_start>=0 AND span_end>span_start)),
 CHECK(status!='INVALIDATED' OR invalidated_at IS NOT NULL));
CREATE INDEX food_mention_food ON food_mentions(food_id,status,item_key);
CREATE INDEX food_mention_item ON food_mentions(item_key,status);
CREATE INDEX food_mention_alias ON food_mentions(alias_id);
CREATE INDEX food_mention_snapshot ON food_mentions(snapshot_id);
CREATE INDEX food_mention_expiry ON food_mentions(expires_at);
CREATE TABLE metric_observations (
 observation_id TEXT PRIMARY KEY, observation_key TEXT NOT NULL UNIQUE,
 ingest_run_id TEXT NOT NULL REFERENCES ingest_runs(ingest_run_id),
 -- Last line of defence: NAVER ratios and YouTube statistics are never persisted here.
 source_code TEXT NOT NULL CHECK(source_code IN ('google_trends_rss','instagram_graph')),
 metric_code TEXT NOT NULL, unit TEXT NOT NULL, subject_kind TEXT NOT NULL CHECK(subject_kind IN ('FOOD','ITEM')),
 subject_key TEXT NOT NULL, food_id TEXT REFERENCES food_entities(food_id), item_key TEXT REFERENCES source_items(item_key),
 observed_at TEXT NOT NULL, period_start TEXT, period_end TEXT, provider_period TEXT,
 comparison_context_json TEXT NOT NULL CHECK(json_valid(comparison_context_json)), context_hash TEXT NOT NULL,
 value_state TEXT NOT NULL CHECK(value_state IN ('OBSERVED','NOT_PROVIDED','NOT_OBSERVED')),
 count_value INTEGER CHECK(count_value IS NULL OR count_value>=0), decimal_value REAL, raw_value TEXT,
 coverage TEXT NOT NULL CHECK(coverage IN ('COMPLETE','PARTIAL','LIMITED','NONE','UNKNOWN')),
 origin TEXT NOT NULL CHECK(origin IN {ORIGINS}), policy_version TEXT NOT NULL, expires_at TEXT,
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','EXPIRED')),
 CHECK(value_state='OBSERVED' OR (count_value IS NULL AND decimal_value IS NULL AND raw_value IS NULL)),
 CHECK(value_state!='OBSERVED' OR count_value IS NOT NULL OR decimal_value IS NOT NULL OR raw_value IS NOT NULL),
 CHECK(subject_kind!='FOOD' OR food_id IS NOT NULL), CHECK(subject_kind!='ITEM' OR item_key IS NOT NULL));
CREATE INDEX metric_food ON metric_observations(food_id,metric_code,observed_at);
CREATE INDEX metric_item ON metric_observations(item_key,metric_code,observed_at);
CREATE INDEX metric_run ON metric_observations(ingest_run_id);
CREATE TABLE entity_change_effects (
 effect_id TEXT PRIMARY KEY, food_event_id TEXT NOT NULL UNIQUE REFERENCES food_events(event_id),
 change_kind TEXT NOT NULL, affected_food_ids_json TEXT NOT NULL CHECK(json_valid(affected_food_ids_json)),
 invalidated_mentions INTEGER NOT NULL CHECK(invalidated_mentions>=0), created_mentions INTEGER NOT NULL CHECK(created_mentions>=0),
 not_reprocessable_items INTEGER NOT NULL CHECK(not_reprocessable_items>=0),
 resolution_version TEXT NOT NULL, processed_at TEXT NOT NULL);
CREATE TABLE aggregate_invalidations (
 invalidation_id TEXT PRIMARY KEY, invalidation_key TEXT NOT NULL UNIQUE, food_id TEXT NOT NULL REFERENCES food_entities(food_id),
 source_code TEXT, reason TEXT NOT NULL, cause_ref TEXT NOT NULL, created_at TEXT NOT NULL, consumed_at TEXT);
CREATE INDEX aggregate_invalidation_open ON aggregate_invalidations(consumed_at,food_id);
'''


def apply(db):
    for statement in SQL.split(';'):
        if statement.strip() and not statement.strip().startswith('--'):
            db.execute(statement)
