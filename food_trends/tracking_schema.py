"""Explicit B1 migration. Slots are scheduling metadata, never executable HTTP jobs."""
SQL = '''
CREATE TABLE tracking_commands (
 request_key TEXT PRIMARY KEY, operation TEXT NOT NULL, payload_hash TEXT NOT NULL,
 result_json TEXT NOT NULL CHECK(json_valid(result_json)), created_at TEXT NOT NULL);
CREATE TABLE tracking_targets (
 target_id TEXT PRIMARY KEY, food_id TEXT NOT NULL REFERENCES food_entities(food_id),
 source_id TEXT NOT NULL CHECK(source_id IN ('instagram_graph','youtube','naver_datalab')),
 account_scope TEXT NOT NULL, alias_scope TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PENDING','ACTIVE','PAUSED','BLOCKED','STOPPED')),
 generation INTEGER NOT NULL CHECK(generation>0), interval_seconds INTEGER NOT NULL CHECK(interval_seconds>0),
 next_due_at TEXT, last_success_at TEXT, origin TEXT NOT NULL,
 registration_method TEXT NOT NULL CHECK(registration_method IN ('MANUAL','DISCOVERY')),
 candidate_id TEXT REFERENCES food_candidates(candidate_id), food_revision INTEGER NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(food_id,source_id,account_scope));
CREATE INDEX tracking_due ON tracking_targets(status,next_due_at);
CREATE TABLE tracking_events (
 event_id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES tracking_targets(target_id),
 action TEXT NOT NULL, actor_ref TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL,
 before_json TEXT CHECK(before_json IS NULL OR json_valid(before_json)),
 after_json TEXT NOT NULL CHECK(json_valid(after_json)), request_key TEXT NOT NULL REFERENCES tracking_commands(request_key));
CREATE TABLE tracking_slots (
 slot_id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES tracking_targets(target_id), generation INTEGER NOT NULL,
 food_revision INTEGER NOT NULL, due_slot TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(target_id,generation,due_slot));
CREATE TRIGGER tracking_events_no_update BEFORE UPDATE ON tracking_events
 BEGIN SELECT RAISE(ABORT,'tracking events are immutable'); END;
CREATE TRIGGER tracking_events_no_delete BEFORE DELETE ON tracking_events
 BEGIN SELECT RAISE(ABORT,'tracking events are immutable'); END;
'''


def apply(db):
    import sqlite3
    statement = ''
    for line in SQL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement); statement = ''
    if statement.strip():
        raise ValueError('Incomplete tracking migration')
