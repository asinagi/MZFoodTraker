"""B3 delivery metadata only. Provider bodies never enter the outbox or call ledger."""
SQL = '''
CREATE TABLE execution_jobs (
 job_id TEXT PRIMARY KEY, job_key TEXT NOT NULL UNIQUE, query_id TEXT NOT NULL REFERENCES planner_queries(query_id),
 plan_id TEXT NOT NULL REFERENCES planner_plans(plan_id), source_id TEXT NOT NULL, account_scope TEXT NOT NULL,
 due_slot TEXT NOT NULL, execution_generation INTEGER NOT NULL CHECK(execution_generation>0),
 mode TEXT NOT NULL CHECK(mode IN ('mock','live')),
 status TEXT NOT NULL CHECK(status IN ('READY','CLAIMED','RUNNING','SUCCEEDED','PARTIAL','LIMITED','RETRY_WAIT','BLOCKED','FAILED','CANCELLED')),
 attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0), max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 5),
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, next_retry_at TEXT, error_kind TEXT,
 run_id TEXT NOT NULL UNIQUE, remote_duplicate_possible INTEGER NOT NULL DEFAULT 0 CHECK(remote_duplicate_possible IN (0,1)),
 UNIQUE(query_id,due_slot,mode,execution_generation));
CREATE INDEX execution_ready ON execution_jobs(status,next_retry_at,created_at);
CREATE TABLE execution_mappings (
 job_id TEXT NOT NULL REFERENCES execution_jobs(job_id), plan_id TEXT NOT NULL REFERENCES planner_plans(plan_id),
 target_id TEXT NOT NULL REFERENCES tracking_targets(target_id), generation INTEGER NOT NULL, food_revision INTEGER NOT NULL,
 mapping_json TEXT NOT NULL CHECK(json_valid(mapping_json)), started INTEGER NOT NULL DEFAULT 0 CHECK(started IN (0,1)),
 PRIMARY KEY(job_id,target_id));
CREATE TABLE execution_outbox (
 event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES execution_jobs(job_id), created_at TEXT NOT NULL,
 delivered_at TEXT, delivery_count INTEGER NOT NULL DEFAULT 0 CHECK(delivery_count>=0));
CREATE TABLE execution_leases (
 resource_key TEXT PRIMARY KEY REFERENCES execution_jobs(job_id), owner_id TEXT NOT NULL,
 fencing_generation INTEGER NOT NULL CHECK(fencing_generation>0), acquired_at TEXT NOT NULL,
 renewed_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE execution_runs (
 run_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES execution_jobs(job_id),
 source_code TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin IN ('mock','live_api')),
 observed_at TEXT, execution_status TEXT NOT NULL, returned_count INTEGER NOT NULL DEFAULT 0,
 accepted_count INTEGER NOT NULL DEFAULT 0, result_contract_json TEXT CHECK(result_contract_json IS NULL OR json_valid(result_contract_json)));
CREATE TABLE execution_calls (
 call_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES execution_jobs(job_id),
 reservation_id TEXT NOT NULL REFERENCES quota_reservations(reservation_id), attempt_no INTEGER NOT NULL,
 step_key TEXT NOT NULL, endpoint TEXT NOT NULL, fencing_generation INTEGER NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL CHECK(status IN ('SENT','SUCCEEDED','FAILED','UNKNOWN')),
 http_status INTEGER CHECK(http_status IS NULL OR http_status BETWEEN 100 AND 599),
 returned_count INTEGER, accepted_count INTEGER, error_kind TEXT, retry_after_seconds REAL,
 remote_debit_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(remote_debit_confirmed IN (0,1)),
 UNIQUE(job_id,attempt_no,step_key));
CREATE TABLE execution_checkpoints (
 job_id TEXT PRIMARY KEY REFERENCES execution_jobs(job_id), state_json TEXT NOT NULL CHECK(json_valid(state_json)),
 updated_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE execution_results (
 run_id TEXT NOT NULL REFERENCES execution_runs(run_id), target_id TEXT NOT NULL REFERENCES tracking_targets(target_id),
 generation INTEGER NOT NULL, food_revision INTEGER NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,target_id));
CREATE TABLE execution_blocks (
 source_id TEXT NOT NULL, account_scope TEXT NOT NULL, error_kind TEXT NOT NULL,
 blocked_at TEXT NOT NULL, until_at TEXT, PRIMARY KEY(source_id,account_scope));
'''


def apply(db):
    import sqlite3
    statement = ''
    for line in SQL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ''
    for table in ('execution_outbox', 'execution_results'):
        db.execute(f'''CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT,'execution history is immutable'); END''')
