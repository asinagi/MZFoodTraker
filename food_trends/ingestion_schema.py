"""B4 common staging: lineage and metadata only. Provider bodies stay in their existing policy-bound stores."""
SOURCES = "('google_trends_rss','instagram_graph','youtube','naver_datalab')"
ORIGINS = "('live_api','mock','user','legacy_unknown')"
COVERAGE = "('COMPLETE','PARTIAL','LIMITED','NONE','UNKNOWN')"
SQL = f'''
CREATE TABLE ingest_runs (
 ingest_run_id TEXT PRIMARY KEY, source_code TEXT NOT NULL CHECK(source_code IN {SOURCES}), run_ref TEXT NOT NULL,
 origin TEXT NOT NULL CHECK(origin IN {ORIGINS}), job_id TEXT REFERENCES execution_jobs(job_id),
 query_id TEXT REFERENCES planner_queries(query_id), contract_version TEXT NOT NULL, adapter_version TEXT NOT NULL,
 source_schema_version TEXT NOT NULL, policy_version TEXT NOT NULL, ingestion_version TEXT NOT NULL,
 observed_at TEXT NOT NULL, execution_status TEXT, coverage TEXT NOT NULL CHECK(coverage IN {COVERAGE}), error_kind TEXT,
 request_context_json TEXT NOT NULL CHECK(json_valid(request_context_json)), context_hash TEXT NOT NULL,
 returned_count INTEGER CHECK(returned_count IS NULL OR returned_count>=0),
 accepted_count INTEGER CHECK(accepted_count IS NULL OR accepted_count>=0),
 staged_items INTEGER NOT NULL DEFAULT 0 CHECK(staged_items>=0), rejected_items INTEGER NOT NULL DEFAULT 0 CHECK(rejected_items>=0),
 metric_storage TEXT NOT NULL CHECK(metric_storage IN ('STORED','TRANSIENT_ONLY','NONE')), ingested_at TEXT NOT NULL,
 UNIQUE(source_code,run_ref));
CREATE INDEX ingest_run_job ON ingest_runs(job_id);
CREATE INDEX ingest_run_query ON ingest_runs(query_id);
CREATE INDEX ingest_run_source_time ON ingest_runs(source_code,observed_at);
CREATE TABLE source_items (
 item_key TEXT PRIMARY KEY, source_code TEXT NOT NULL CHECK(source_code IN {SOURCES} AND source_code!='naver_datalab'),
 external_key TEXT NOT NULL CHECK(length(external_key) BETWEEN 1 AND 256), key_kind TEXT NOT NULL,
 published_at TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 availability TEXT NOT NULL CHECK(availability IN ('AVAILABLE','EXPIRED','DELETED')),
 first_origin TEXT NOT NULL CHECK(first_origin IN {ORIGINS}), payload_ref TEXT NOT NULL, expires_at TEXT,
 UNIQUE(source_code,external_key), CHECK(first_seen<=last_seen));
CREATE INDEX source_item_seen ON source_items(source_code,last_seen);
CREATE INDEX source_item_expiry ON source_items(expires_at);
CREATE TABLE item_snapshots (
 snapshot_id TEXT PRIMARY KEY, item_key TEXT NOT NULL REFERENCES source_items(item_key),
 payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64), adapter_version TEXT NOT NULL, source_schema_version TEXT NOT NULL,
 field_states_json TEXT NOT NULL CHECK(json_valid(field_states_json)), payload_ref TEXT NOT NULL,
 captured_at TEXT NOT NULL, expires_at TEXT, purged_at TEXT,
 UNIQUE(item_key,payload_hash,adapter_version));
CREATE INDEX item_snapshot_time ON item_snapshots(item_key,captured_at);
CREATE INDEX item_snapshot_expiry ON item_snapshots(expires_at);
CREATE TABLE run_items (
 ingest_run_id TEXT NOT NULL REFERENCES ingest_runs(ingest_run_id), item_key TEXT NOT NULL REFERENCES source_items(item_key),
 snapshot_id TEXT REFERENCES item_snapshots(snapshot_id), observed_at TEXT NOT NULL,
 origin TEXT NOT NULL CHECK(origin IN {ORIGINS}), multiplicity INTEGER NOT NULL CHECK(multiplicity>=1),
 quality_status TEXT NOT NULL CHECK(quality_status IN ('PASS','WARN')), PRIMARY KEY(ingest_run_id,item_key));
CREATE INDEX run_item_seen ON run_items(item_key,observed_at);
CREATE INDEX run_item_snapshot ON run_items(snapshot_id);
CREATE TABLE quality_results (
 result_id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, ingest_run_id TEXT NOT NULL REFERENCES ingest_runs(ingest_run_id),
 item_key TEXT REFERENCES source_items(item_key), subject_hash TEXT, rule_code TEXT NOT NULL, rule_version TEXT NOT NULL,
 severity TEXT NOT NULL CHECK(severity IN ('INFO','WARNING','ERROR')),
 outcome TEXT NOT NULL CHECK(outcome IN ('PASS','WARN','FAIL','NOT_APPLICABLE')),
 details_json TEXT NOT NULL CHECK(json_valid(details_json)), checked_at TEXT NOT NULL);
CREATE INDEX quality_run_outcome ON quality_results(ingest_run_id,outcome);
CREATE INDEX quality_item ON quality_results(item_key,checked_at);
'''


def apply(db):
    for statement in SQL.split(';'):
        if statement.strip():
            db.execute(statement)
