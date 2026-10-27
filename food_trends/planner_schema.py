"""B2 immutable plans and explicit local quota reservations; no provider response storage."""
SQL = '''
CREATE TABLE planner_queries (
 query_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, account_scope TEXT NOT NULL, query_kind TEXT NOT NULL,
 canonical_json TEXT NOT NULL CHECK(json_valid(canonical_json)), spec_hash TEXT NOT NULL,
 planner_version TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(source_id,account_scope,planner_version,spec_hash));
CREATE TABLE planner_plans (
 plan_id TEXT PRIMARY KEY, query_id TEXT NOT NULL REFERENCES planner_queries(query_id), due_slot TEXT NOT NULL,
 mapping_hash TEXT NOT NULL, account_fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 created_at TEXT NOT NULL, UNIQUE(query_id,due_slot,mapping_hash,account_fingerprint));
CREATE TABLE planner_query_targets (
 query_id TEXT NOT NULL REFERENCES planner_queries(query_id), target_id TEXT NOT NULL REFERENCES tracking_targets(target_id),
 generation INTEGER NOT NULL, food_revision INTEGER NOT NULL, mapping_json TEXT NOT NULL CHECK(json_valid(mapping_json)),
 PRIMARY KEY(query_id,target_id,generation,food_revision));
CREATE TABLE planner_plan_targets (
 plan_id TEXT NOT NULL REFERENCES planner_plans(plan_id), target_id TEXT NOT NULL REFERENCES tracking_targets(target_id),
 generation INTEGER NOT NULL, food_revision INTEGER NOT NULL, due_at TEXT NOT NULL,
 mapping_json TEXT NOT NULL CHECK(json_valid(mapping_json)), PRIMARY KEY(plan_id,target_id));
CREATE TABLE planner_commands (
 request_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result_json TEXT NOT NULL CHECK(json_valid(result_json)), created_at TEXT NOT NULL);
CREATE TABLE quota_reservations (
 reservation_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES planner_plans(plan_id),
 query_id TEXT NOT NULL REFERENCES planner_queries(query_id), due_slot TEXT NOT NULL,
 attempt_key TEXT NOT NULL UNIQUE, attempt_no INTEGER NOT NULL CHECK(attempt_no>0),
 state TEXT NOT NULL CHECK(state IN ('RESERVED','ATTEMPTED','RELEASED')),
 reserved_at TEXT NOT NULL, expires_at TEXT NOT NULL, attempted_at TEXT,
 target_snapshot_json TEXT NOT NULL CHECK(json_valid(target_snapshot_json)),
 UNIQUE(query_id,due_slot,attempt_no));
CREATE TABLE quota_charges (
 reservation_id TEXT NOT NULL REFERENCES quota_reservations(reservation_id), resource TEXT NOT NULL,
 cost INTEGER NOT NULL CHECK(cost>=0), distinct_key TEXT, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
 reset_timezone TEXT NOT NULL, local_limit INTEGER, provider_limit INTEGER,
 PRIMARY KEY(reservation_id,resource));
CREATE INDEX quota_charge_window ON quota_charges(resource,window_start,window_end);
CREATE TABLE quota_events (
 event_id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL REFERENCES quota_reservations(reservation_id),
 action TEXT NOT NULL, occurred_at TEXT NOT NULL, metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)));
'''


def apply(db):
    import sqlite3
    statement = ''
    for line in SQL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement); statement = ''
    for table in ('planner_queries','planner_plans','planner_query_targets','planner_plan_targets','quota_events'):
        for action in ('UPDATE','DELETE'):
            db.execute(f'''CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'planner history is immutable'); END''')
