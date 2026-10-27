"""A4-A5 schema; only explicit migration version 3 executes these statements."""
ENTITY_SQL = '''
CREATE TABLE food_commands (
 request_key TEXT PRIMARY KEY, operation TEXT NOT NULL, payload_hash TEXT NOT NULL,
 result_json TEXT NOT NULL CHECK(json_valid(result_json)), created_at TEXT NOT NULL);
CREATE TABLE food_entities (
 food_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL CHECK(length(canonical_name) BETWEEN 1 AND 200),
 entity_kind TEXT NOT NULL CHECK(entity_kind IN ('DISH','PRODUCT','INGREDIENT','FOOD_CONCEPT')),
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','MERGED','RETIRED')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0), default_scope TEXT NOT NULL,
 merged_into TEXT REFERENCES food_entities(food_id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(merged_into IS NULL OR merged_into!=food_id),
 CHECK((status='MERGED' AND merged_into IS NOT NULL) OR (status!='MERGED' AND merged_into IS NULL)));
CREATE INDEX food_entity_name ON food_entities(canonical_name,status);
CREATE INDEX food_entity_redirect ON food_entities(merged_into);
CREATE TABLE food_aliases (
 alias_id TEXT PRIMARY KEY, food_id TEXT NOT NULL REFERENCES food_entities(food_id),
 surface_form TEXT NOT NULL, normalized_form TEXT NOT NULL, scope TEXT NOT NULL,
 normalization_version TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX food_alias_active ON food_aliases(normalized_form,scope,normalization_version) WHERE status='ACTIVE';
CREATE INDEX food_alias_entity ON food_aliases(food_id,status);
CREATE TABLE food_relations (
 relation_id TEXT PRIMARY KEY, from_food_id TEXT NOT NULL REFERENCES food_entities(food_id),
 to_food_id TEXT NOT NULL REFERENCES food_entities(food_id),
 relation_type TEXT NOT NULL CHECK(relation_type IN ('VARIANT_OF','RELATED_TO')),
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')), revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(from_food_id!=to_food_id), CHECK(relation_type!='RELATED_TO' OR from_food_id<to_food_id));
CREATE UNIQUE INDEX food_relation_active ON food_relations(from_food_id,to_food_id,relation_type) WHERE status='ACTIVE';
CREATE INDEX food_relation_to ON food_relations(to_food_id,relation_type,status);
CREATE TRIGGER food_variant_cycle_insert BEFORE INSERT ON food_relations
 WHEN NEW.status='ACTIVE' AND NEW.relation_type='VARIANT_OF'
 BEGIN
 SELECT RAISE(ABORT,'cyclic food variant') WHERE EXISTS (
 WITH RECURSIVE ancestors(id) AS (SELECT NEW.to_food_id UNION
 SELECT r.to_food_id FROM food_relations r JOIN ancestors a ON r.from_food_id=a.id
 WHERE r.status='ACTIVE' AND r.relation_type='VARIANT_OF')
 SELECT 1 FROM ancestors WHERE id=NEW.from_food_id);
 END;
CREATE TRIGGER food_variant_cycle_update BEFORE UPDATE ON food_relations
 WHEN NEW.status='ACTIVE' AND NEW.relation_type='VARIANT_OF'
 BEGIN
 SELECT RAISE(ABORT,'cyclic food variant') WHERE EXISTS (
 WITH RECURSIVE ancestors(id) AS (SELECT NEW.to_food_id UNION
 SELECT r.to_food_id FROM food_relations r JOIN ancestors a ON r.from_food_id=a.id
 WHERE r.status='ACTIVE' AND r.relation_type='VARIANT_OF' AND r.relation_id!=OLD.relation_id)
 SELECT 1 FROM ancestors WHERE id=NEW.from_food_id);
 END;
CREATE TABLE food_candidates (
 candidate_id TEXT PRIMARY KEY, candidate_key TEXT NOT NULL UNIQUE,
 source_code TEXT NOT NULL, source_item_id INTEGER REFERENCES items(id),
 origin TEXT NOT NULL CHECK(origin IN ('live_api','mock','user','legacy_unknown')),
 surface_form TEXT NOT NULL, normalized_form TEXT NOT NULL, scope TEXT NOT NULL,
 proposed_kind TEXT NOT NULL, proposed_aliases_json TEXT NOT NULL CHECK(json_valid(proposed_aliases_json)),
 description TEXT NOT NULL DEFAULT '', proposed_relations_json TEXT NOT NULL CHECK(json_valid(proposed_relations_json)),
 status TEXT NOT NULL CHECK(status IN ('NEW','NEEDS_REVIEW','RESOLVED','REJECTED')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0), resolved_food_id TEXT REFERENCES food_entities(food_id),
 resolution_alias_id TEXT REFERENCES food_aliases(alias_id), resolution_method TEXT,
 resolution_version TEXT NOT NULL, type_gate TEXT NOT NULL CHECK(type_gate IN ('FOOD','REQUIRES_TYPE_APPROVAL','BLOCKED_NON_FOOD')),
 contract_json TEXT NOT NULL CHECK(json_valid(contract_json)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(status!='RESOLVED' OR type_gate!='BLOCKED_NON_FOOD'),
 CHECK((status='RESOLVED' AND resolved_food_id IS NOT NULL) OR (status!='RESOLVED' AND resolved_food_id IS NULL)));
CREATE INDEX food_candidate_review ON food_candidates(status,created_at);
CREATE INDEX food_candidate_entity ON food_candidates(resolved_food_id);
CREATE INDEX food_candidate_alias ON food_candidates(resolution_alias_id);
CREATE INDEX food_candidate_item ON food_candidates(source_item_id);
CREATE TABLE food_events (
 event_id TEXT PRIMARY KEY, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, action TEXT NOT NULL,
 actor_ref TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL,
 before_json TEXT CHECK(before_json IS NULL OR json_valid(before_json)),
 after_json TEXT CHECK(after_json IS NULL OR json_valid(after_json)),
 request_key TEXT NOT NULL REFERENCES food_commands(request_key),
 reverses_event_id TEXT REFERENCES food_events(event_id));
CREATE INDEX food_event_subject ON food_events(subject_type,subject_id,occurred_at);
CREATE INDEX food_event_command ON food_events(request_key);
CREATE INDEX food_event_reverse ON food_events(reverses_event_id);
CREATE TRIGGER food_events_no_update BEFORE UPDATE ON food_events
 BEGIN SELECT RAISE(ABORT,'food audit events are immutable'); END;
CREATE TRIGGER food_events_no_delete BEFORE DELETE ON food_events
 BEGIN SELECT RAISE(ABORT,'food audit events are immutable'); END;
'''


def apply_entities(db):
    # complete_statement handles triggers containing semicolons without executescript's implicit commit.
    import sqlite3
    statement = ''
    for line in ENTITY_SQL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete entity migration statement')
