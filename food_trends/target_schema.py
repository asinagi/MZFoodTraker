"""B6 target schema: one specification, DDL for Oracle and for a SQLite stand-in.

The target mirrors the SQLite v10 contract (same deterministic keys) for the tables that carry
food, tracking, execution history, B4 staging and B5 links, plus source_contents so payload_ref
stays resolvable after SQLite is retired. Value conversion and validation are dialect independent
(see target_db), so the SQLite stand-in rejects what Oracle would reject (for example '' in a
NOT NULL column). Constraint names are short and stable for Oracle error mapping.
"""
import hashlib
from dataclasses import dataclass, field

TARGET_SCHEMA_VERSION = 7
ORIGINS = "('live_api','mock','user','legacy_unknown')"
# Character limits are enforced before binding so both dialects reject the same values.
KINDS = {
    'id': ('VARCHAR2(128 CHAR)', 'TEXT', 128), 'key': ('VARCHAR2(512 CHAR)', 'TEXT', 512),
    'code': ('VARCHAR2(64 CHAR)', 'TEXT', 64), 'text': ('VARCHAR2(1000 CHAR)', 'TEXT', 1000),
    'hash': ('VARCHAR2(64 CHAR)', 'TEXT', 64), 'clob': ('CLOB', 'TEXT', None), 'json': ('CLOB', 'TEXT', None),
    'ts': ('TIMESTAMP(6) WITH TIME ZONE', 'TEXT', None), 'int': ('NUMBER(19)', 'INTEGER', None),
    'real': ('BINARY_DOUBLE', 'REAL', None),
}
# Oracle reserved words (SQL Language Reference) that must not be used as identifiers.
ORACLE_RESERVED = frozenset('''ACCESS ADD ALL ALTER AND ANY AS ASC AUDIT BETWEEN BY CHAR CHECK CLUSTER COLUMN COMMENT
COMPRESS CONNECT CREATE CURRENT DATE DECIMAL DEFAULT DELETE DESC DISTINCT DROP ELSE EXCLUSIVE EXISTS FILE FLOAT FOR FROM
GRANT GROUP HAVING IDENTIFIED IMMEDIATE IN INCREMENT INDEX INITIAL INSERT INTEGER INTERSECT INTO IS LEVEL LIKE LOCK LONG
MAXEXTENTS MINUS MLSLABEL MODE MODIFY NOAUDIT NOCOMPRESS NOT NOWAIT NULL NUMBER OF OFFLINE ON ONLINE OPTION OR ORDER
PCTFREE PRIOR PRIVILEGES PUBLIC RAW RENAME RESOURCE REVOKE ROW ROWID ROWNUM ROWS SELECT SESSION SET SHARE SIZE SMALLINT
START SUCCESSFUL SYNONYM SYSDATE TABLE THEN TO TRIGGER UID UNION UNIQUE UPDATE USER VALIDATE VALUES VARCHAR VARCHAR2 VIEW
WHENEVER WHERE WITH'''.split())


@dataclass(frozen=True)
class Column:
    name: str
    kind: str
    null: bool = True
    source: str = None          # source column when the target name differs
    empty_as_null: bool = False  # '' is legal in the source; Oracle stores NULL, reads back ''
    check: str = None

    @property
    def src(self):
        return self.source or self.name


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple
    pk: tuple
    natural_key: tuple = None   # deterministic logical key used for idempotent upserts
    unique: tuple = ()
    fks: tuple = ()             # ((cols,), parent, (parent_cols,))
    checks: tuple = ()
    indexes: tuple = ()
    mutable: tuple = ()         # columns a later state may change; everything else is immutable
    source_table: str = None    # SQLite table read by B7, None for derived/target-only tables
    partial_unique: tuple = ()  # ((cols,), predicate) unique only where predicate holds
    meta: dict = field(default_factory=dict)

    @property
    def key(self):
        return self.natural_key or self.pk

    def column(self, name):
        return next(c for c in self.columns if c.name == name)


def C(name, kind, null=True, **kw):
    return Column(name, kind, null, **kw)


def NN(name, kind, **kw):
    return Column(name, kind, False, **kw)


def _status(col, values):
    return "%s IN (%s)" % (col, ','.join("'%s'" % v for v in values))


TABLES = (
    Table('food_commands', (NN('request_key', 'key'), NN('operation', 'code'), NN('payload_hash', 'hash'),
          NN('result_json', 'json'), NN('created_at', 'ts')), ('request_key',), source_table='food_commands'),
    Table('food_entities', (NN('food_id', 'id'), NN('canonical_name', 'text'), NN('entity_kind', 'code'), NN('status', 'code'),
          NN('revision', 'int'), NN('default_scope', 'code'), C('merged_into', 'id'), NN('created_at', 'ts'), NN('updated_at', 'ts')),
          ('food_id',), fks=((('merged_into',), 'food_entities', ('food_id',)),),
          checks=(_status('entity_kind', ('DISH', 'PRODUCT', 'INGREDIENT', 'FOOD_CONCEPT')), _status('status', ('ACTIVE', 'MERGED', 'RETIRED')),
                  'revision>0', "(status='MERGED' AND merged_into IS NOT NULL) OR (status<>'MERGED' AND merged_into IS NULL)"),
          mutable=('canonical_name', 'status', 'revision', 'merged_into', 'updated_at'), source_table='food_entities'),
    Table('food_aliases', (NN('alias_id', 'id'), NN('food_id', 'id'), NN('surface_form', 'text'), NN('normalized_form', 'text'),
          NN('scope', 'code'), NN('normalization_version', 'code'), NN('status', 'code'), NN('revision', 'int'),
          NN('created_at', 'ts'), NN('updated_at', 'ts')), ('alias_id',),
          fks=((('food_id',), 'food_entities', ('food_id',)),), checks=(_status('status', ('ACTIVE', 'REVOKED')),),
          partial_unique=((('normalized_form', 'scope', 'normalization_version'), "status='ACTIVE'"),),
          mutable=('food_id', 'status', 'revision', 'updated_at'), source_table='food_aliases'),
    Table('food_relations', (NN('relation_id', 'id'), NN('from_food_id', 'id'), NN('to_food_id', 'id'), NN('relation_type', 'code'),
          NN('status', 'code'), NN('revision', 'int'), NN('created_at', 'ts'), NN('updated_at', 'ts')), ('relation_id',),
          fks=((('from_food_id',), 'food_entities', ('food_id',)), (('to_food_id',), 'food_entities', ('food_id',))),
          checks=(_status('relation_type', ('VARIANT_OF', 'RELATED_TO')), _status('status', ('ACTIVE', 'REVOKED')), 'from_food_id<>to_food_id'),
          mutable=('status', 'revision', 'updated_at'), source_table='food_relations'),
    Table('food_candidates', (NN('candidate_id', 'id'), NN('candidate_key', 'key'), NN('source_code', 'code'),
          C('source_item_key', 'id', source='source_item_id'), NN('origin', 'code'), NN('surface_form', 'text'),
          NN('normalized_form', 'text'), NN('scope', 'code'), NN('proposed_kind', 'code'), NN('proposed_aliases_json', 'json'),
          NN('description', 'clob', empty_as_null=True), NN('proposed_relations_json', 'json'), NN('status', 'code'), NN('revision', 'int'),
          C('resolved_food_id', 'id'), C('resolution_alias_id', 'id'), C('resolution_method', 'code'), NN('resolution_version', 'code'),
          NN('type_gate', 'code'), NN('contract_json', 'json'), NN('created_at', 'ts'), NN('updated_at', 'ts')), ('candidate_id',),
          unique=(('candidate_key',),), fks=((('resolved_food_id',), 'food_entities', ('food_id',)),
                                             (('resolution_alias_id',), 'food_aliases', ('alias_id',))),
          checks=('origin IN ' + ORIGINS, _status('status', ('NEW', 'NEEDS_REVIEW', 'RESOLVED', 'REJECTED'))),
          mutable=('status', 'revision', 'resolved_food_id', 'resolution_alias_id', 'resolution_method', 'resolution_version', 'type_gate', 'updated_at'),
          source_table='food_candidates'),
    Table('food_events', (NN('event_id', 'id'), NN('subject_type', 'code'), NN('subject_id', 'id'), NN('action', 'code'),
          NN('actor_ref', 'text'), NN('reason', 'text'), NN('occurred_at', 'ts'), C('before_json', 'json'), C('after_json', 'json'),
          NN('request_key', 'key'), C('reverses_event_id', 'id'),
          # v7: append order (wall-clock times can tie): the SQLite rowid when loaded, the next number when the runtime appends.
          C('event_sequence', 'int', source='rowid')), ('event_id',),
          fks=((('request_key',), 'food_commands', ('request_key',)), (('reverses_event_id',), 'food_events', ('event_id',))),
          indexes=(('subject_type', 'subject_id', 'occurred_at'), ('event_sequence',)),
          mutable=('event_sequence',), source_table='food_events'),  # filled by a re-load of a target upgraded from v6
    Table('tracking_targets', (NN('target_id', 'id'), NN('food_id', 'id'), NN('source_id', 'code'), NN('account_scope', 'code'),
          NN('alias_scope', 'code'), NN('status', 'code'), NN('generation', 'int'), NN('interval_seconds', 'int'), C('next_due_at', 'ts'),
          C('last_success_at', 'ts'), NN('origin', 'code'), NN('registration_method', 'code'), C('candidate_id', 'id'),
          NN('food_revision', 'int'), NN('revision', 'int'), NN('created_at', 'ts'), NN('updated_at', 'ts')), ('target_id',),
          unique=(('food_id', 'source_id', 'account_scope'),),
          fks=((('food_id',), 'food_entities', ('food_id',)), (('candidate_id',), 'food_candidates', ('candidate_id',))),
          checks=(_status('status', ('PENDING', 'ACTIVE', 'PAUSED', 'BLOCKED', 'STOPPED')),),
          mutable=('status', 'generation', 'interval_seconds', 'next_due_at', 'last_success_at', 'food_revision', 'revision', 'updated_at', 'alias_scope'),
          source_table='tracking_targets'),
    Table('planner_queries', (NN('query_id', 'id'), NN('source_id', 'code'), NN('account_scope', 'code'), NN('query_kind', 'code'),
          NN('canonical_json', 'json'), NN('spec_hash', 'hash'), NN('planner_version', 'code'), NN('created_at', 'ts')), ('query_id',),
          source_table='planner_queries'),
    Table('planner_plans', (NN('plan_id', 'id'), NN('query_id', 'id'), NN('due_slot', 'code'), NN('mapping_hash', 'hash'),
          NN('account_fingerprint', 'hash'), NN('payload_json', 'json'), NN('created_at', 'ts')), ('plan_id',),
          fks=((('query_id',), 'planner_queries', ('query_id',)),), source_table='planner_plans'),
    Table('planner_query_targets', (NN('query_id', 'id'), NN('target_id', 'id'), NN('generation', 'int'), NN('food_revision', 'int'),
          NN('mapping_json', 'json')), ('query_id', 'target_id', 'generation', 'food_revision'),
          fks=((('query_id',), 'planner_queries', ('query_id',)), (('target_id',), 'tracking_targets', ('target_id',))),
          source_table='planner_query_targets'),
    Table('quota_reservations', (NN('reservation_id', 'id'), NN('plan_id', 'id'), NN('query_id', 'id'), NN('due_slot', 'code'),
          NN('attempt_key', 'key'), NN('attempt_no', 'int'), NN('state', 'code'), NN('reserved_at', 'ts'), NN('expires_at', 'ts'),
          C('attempted_at', 'ts'), NN('target_snapshot_json', 'json')), ('reservation_id',),
          unique=(('attempt_key',), ('query_id', 'due_slot', 'attempt_no')),
          fks=((('plan_id',), 'planner_plans', ('plan_id',)), (('query_id',), 'planner_queries', ('query_id',))),
          checks=(_status('state', ('RESERVED', 'ATTEMPTED', 'RELEASED', 'EXPIRED')),),
          mutable=('state', 'attempted_at', 'target_snapshot_json'), source_table='quota_reservations',
          meta={'expiry_semantics': 'reservation_lease'}),
    Table('execution_jobs', (NN('job_id', 'id'), NN('job_key', 'hash'), NN('query_id', 'id'), NN('plan_id', 'id'), NN('source_id', 'code'),
          NN('account_scope', 'code'), NN('due_slot', 'code'), NN('execution_generation', 'int'), NN('exec_mode', 'code', source='mode'),
          NN('status', 'code'), NN('attempt_count', 'int'), NN('max_attempts', 'int'), NN('created_at', 'ts'), C('started_at', 'ts'),
          C('finished_at', 'ts'), C('next_retry_at', 'ts'), C('error_kind', 'code'), NN('run_id', 'id'), NN('remote_duplicate_possible', 'int')),
          ('job_id',), unique=(('job_key',), ('run_id',), ('query_id', 'due_slot', 'exec_mode', 'execution_generation')),
          fks=((('query_id',), 'planner_queries', ('query_id',)), (('plan_id',), 'planner_plans', ('plan_id',))),
          checks=(_status('exec_mode', ('mock', 'live')), 'remote_duplicate_possible IN (0,1)',
                  _status('status', ('READY', 'CLAIMED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'LIMITED', 'RETRY_WAIT', 'BLOCKED', 'FAILED', 'CANCELLED'))),
          mutable=('status', 'attempt_count', 'started_at', 'finished_at', 'next_retry_at', 'error_kind', 'remote_duplicate_possible'),
          source_table='execution_jobs'),
    Table('execution_runs', (NN('run_id', 'id'), NN('job_id', 'id'), NN('source_code', 'code'), NN('origin', 'code'), C('observed_at', 'ts'),
          NN('execution_status', 'code'), NN('returned_count', 'int'), NN('accepted_count', 'int'), C('result_contract_json', 'json'),
          C('result_coverage', 'code'), C('committed_pages', 'int')), ('run_id',), unique=(('job_id',),),
          fks=((('job_id',), 'execution_jobs', ('job_id',)),),
          checks=("origin IN ('mock','live_api')", "result_coverage IS NULL OR result_coverage IN ('COMPLETE','PARTIAL','LIMITED','NONE')"),
          mutable=('observed_at', 'execution_status', 'returned_count', 'accepted_count', 'result_contract_json', 'result_coverage', 'committed_pages'),
          source_table='execution_runs'),
    Table('execution_calls', (NN('call_id', 'id'), NN('job_id', 'id'), NN('reservation_id', 'id'), NN('attempt_no', 'int'), NN('step_key', 'code'),
          NN('endpoint', 'code'), NN('fencing_generation', 'int'), NN('started_at', 'ts'), C('finished_at', 'ts'), NN('status', 'code'),
          C('http_status', 'int'), C('returned_count', 'int'), C('accepted_count', 'int'), C('error_kind', 'code'), C('retry_after_seconds', 'real'),
          NN('remote_debit_confirmed', 'int')), ('call_id',), unique=(('job_id', 'attempt_no', 'step_key'),),
          fks=((('job_id',), 'execution_jobs', ('job_id',)), (('reservation_id',), 'quota_reservations', ('reservation_id',))),
          checks=(_status('status', ('SENT', 'SUCCEEDED', 'FAILED', 'UNKNOWN')), 'remote_debit_confirmed IN (0,1)'),
          mutable=('finished_at', 'status', 'http_status', 'returned_count', 'accepted_count', 'error_kind', 'retry_after_seconds'),
          source_table='execution_calls'),
    Table('execution_outbox', (NN('event_id', 'id'), NN('job_id', 'id'), NN('created_at', 'ts'), C('delivered_at', 'ts'), NN('delivery_count', 'int')),
          ('event_id',), unique=(('job_id',),), fks=((('job_id',), 'execution_jobs', ('job_id',)),),
          mutable=('delivered_at', 'delivery_count'), source_table='execution_outbox'),
    Table('execution_results', (NN('run_id', 'id'), NN('target_id', 'id'), NN('generation', 'int'), NN('food_revision', 'int'), NN('created_at', 'ts')),
          ('run_id', 'target_id'), fks=((('run_id',), 'execution_runs', ('run_id',)), (('target_id',), 'tracking_targets', ('target_id',))),
          source_table='execution_results'),
    # Allowed source content so payload_ref stays resolvable; never longer than the source keeps it.
    Table('source_contents', (NN('payload_ref', 'key'), NN('item_key', 'id'), NN('source_code', 'code'), NN('origin', 'code'),
          NN('content_json', 'json'), NN('content_hash', 'hash'), NN('captured_at', 'ts'), NN('refreshed_at', 'ts'), C('expires_at', 'ts'),
          C('field_expires_at', 'ts'), C('field_purged_at', 'ts'),
          # v2: SHA-256 of the payload exactly as the source store serialized it (B4 snapshot key parity).
          C('source_payload_hash', 'hash')), ('payload_ref',),
          checks=("source_code IN ('google_trends_rss','instagram_graph','youtube')", 'origin IN ' + ORIGINS),
          indexes=(('item_key',), ('expires_at',), ('field_expires_at',)),
          mutable=('content_json', 'content_hash', 'refreshed_at', 'expires_at', 'field_expires_at', 'field_purged_at', 'source_payload_hash')),
    Table('rss_classifications', (NN('item_key', 'id'), NN('normalized_name', 'text'), NN('classification', 'code'), NN('reasons_json', 'json', source='reasons'),
          NN('classifier_version', 'code'), NN('entity_type', 'code'), C('rule_score', 'int'), C('score_basis', 'code'),
          NN('evidence_news_json', 'json', source='evidence_news'), C('classified_at', 'ts')), ('item_key',),
          checks=(_status('classification', ('FOOD', 'REVIEW', 'NON_FOOD')),),
          mutable=('normalized_name', 'classification', 'reasons_json', 'classifier_version', 'entity_type', 'rule_score', 'score_basis',
                   'evidence_news_json', 'classified_at'), source_table='candidates'),
    Table('rss_classification_history', (NN('history_key', 'id'), NN('item_key', 'id'), NN('recorded_at', 'ts'), NN('result_json', 'json')),
          ('history_key',), indexes=(('item_key', 'recorded_at'),), source_table='classification_history'),
    Table('ingest_runs', (NN('ingest_run_id', 'id'), NN('source_code', 'code'), NN('run_ref', 'key'), NN('origin', 'code'), C('job_id', 'id'),
          C('query_id', 'id'), NN('contract_version', 'code'), NN('adapter_version', 'code'), NN('source_schema_version', 'code'),
          NN('policy_version', 'code'), NN('ingestion_version', 'code'), NN('observed_at', 'ts'), C('execution_status', 'code'), NN('coverage', 'code'),
          C('error_kind', 'code'), NN('request_context_json', 'json'), NN('context_hash', 'hash'), C('returned_count', 'int'),
          C('accepted_count', 'int'), NN('staged_items', 'int'), NN('rejected_items', 'int'), NN('metric_storage', 'code'), NN('ingested_at', 'ts')),
          ('ingest_run_id',), unique=(('source_code', 'run_ref'),),
          fks=((('job_id',), 'execution_jobs', ('job_id',)), (('query_id',), 'planner_queries', ('query_id',))),
          checks=('origin IN ' + ORIGINS, "coverage IN ('COMPLETE','PARTIAL','LIMITED','NONE','UNKNOWN')",
                  "metric_storage IN ('STORED','TRANSIENT_ONLY','NONE')"), source_table='ingest_runs'),
    Table('source_items', (NN('item_key', 'id'), NN('source_code', 'code'), NN('external_key', 'key'), NN('key_kind', 'code'), C('published_at', 'ts'),
          NN('first_seen', 'ts'), NN('last_seen', 'ts'), NN('availability', 'code'), NN('first_origin', 'code'), NN('payload_ref', 'key'),
          C('expires_at', 'ts')), ('item_key',), unique=(('source_code', 'external_key'),),
          checks=("source_code IN ('google_trends_rss','instagram_graph','youtube')", _status('availability', ('AVAILABLE', 'EXPIRED', 'DELETED')),
                  'first_origin IN ' + ORIGINS, 'first_seen<=last_seen'), indexes=(('expires_at',),),
          mutable=('published_at', 'first_seen', 'last_seen', 'availability', 'payload_ref', 'expires_at'), source_table='source_items'),
    Table('item_snapshots', (NN('snapshot_id', 'id'), NN('item_key', 'id'), NN('payload_hash', 'hash'), NN('adapter_version', 'code'),
          NN('source_schema_version', 'code'), NN('field_states_json', 'json'), NN('payload_ref', 'key'), NN('captured_at', 'ts'),
          C('expires_at', 'ts'), C('purged_at', 'ts')), ('snapshot_id',), unique=(('item_key', 'payload_hash', 'adapter_version'),),
          fks=((('item_key',), 'source_items', ('item_key',)),), mutable=('purged_at',), source_table='item_snapshots'),
    Table('run_items', (NN('ingest_run_id', 'id'), NN('item_key', 'id'), C('snapshot_id', 'id'), NN('observed_at', 'ts'), NN('origin', 'code'),
          NN('multiplicity', 'int'), NN('quality_status', 'code')), ('ingest_run_id', 'item_key'),
          fks=((('ingest_run_id',), 'ingest_runs', ('ingest_run_id',)), (('item_key',), 'source_items', ('item_key',)),
               (('snapshot_id',), 'item_snapshots', ('snapshot_id',))),
          checks=('origin IN ' + ORIGINS, 'multiplicity>=1', "quality_status IN ('PASS','WARN')"), source_table='run_items'),
    Table('quality_results', (NN('result_id', 'id'), NN('event_key', 'hash'), NN('ingest_run_id', 'id'), C('item_key', 'id'), C('subject_hash', 'hash'),
          NN('rule_code', 'code'), NN('rule_version', 'code'), NN('severity', 'code'), NN('outcome', 'code'), NN('details_json', 'json'),
          NN('checked_at', 'ts')), ('result_id',), natural_key=('event_key',), unique=(('event_key',),),
          fks=((('ingest_run_id',), 'ingest_runs', ('ingest_run_id',)), (('item_key',), 'source_items', ('item_key',))),
          checks=("outcome IN ('PASS','WARN','FAIL','NOT_APPLICABLE')", "severity IN ('INFO','WARNING','ERROR')"), source_table='quality_results'),
    Table('item_query_matches', (NN('match_id', 'id'), NN('ingest_run_id', 'id'), NN('item_key', 'id'), NN('query_ref', 'key'), C('query_id', 'id'),
          NN('matched_at', 'ts'), NN('verification_status', 'code'), NN('origin', 'code'), NN('evidence_ref', 'key'), NN('policy_version', 'code'),
          NN('status', 'code'), C('expires_at', 'ts')), ('match_id',), natural_key=('ingest_run_id', 'item_key', 'query_ref'),
          unique=(('ingest_run_id', 'item_key', 'query_ref'),),
          fks=((('ingest_run_id', 'item_key'), 'run_items', ('ingest_run_id', 'item_key')), (('query_id',), 'planner_queries', ('query_id',))),
          checks=("verification_status='SEARCH_ONLY'", 'origin IN ' + ORIGINS, _status('status', ('ACTIVE', 'EXPIRED'))),
          indexes=(('item_key',),), mutable=('status',), source_table='item_query_matches'),
    Table('food_mentions', (NN('mention_id', 'id'), NN('mention_key', 'hash'), NN('item_key', 'id'), NN('source_code', 'code'), C('snapshot_id', 'id'),
          NN('food_id', 'id'), NN('alias_id', 'id'), NN('field_code', 'code'), C('span_start', 'int'), C('span_end', 'int'),
          NN('resolution_version', 'code'), NN('verification_status', 'code'), NN('origin', 'code'), NN('policy_version', 'code'), NN('status', 'code'),
          C('expires_at', 'ts'), NN('created_at', 'ts'), C('invalidated_at', 'ts'), C('invalidated_by', 'id')), ('mention_id',),
          natural_key=('mention_key',), unique=(('mention_key',),),
          fks=((('item_key',), 'source_items', ('item_key',)), (('snapshot_id',), 'item_snapshots', ('snapshot_id',)),
               (('food_id',), 'food_entities', ('food_id',)), (('alias_id',), 'food_aliases', ('alias_id',))),
          # Policy as a constraint: YouTube and NAVER content never yields a stored mention.
          checks=("source_code IN ('google_trends_rss','instagram_graph')", 'origin IN ' + ORIGINS,
                  _status('status', ('ACTIVE', 'INVALIDATED', 'EXPIRED')),
                  "verification_status IN ('EXACT_ALIAS_FIELD','EXACT_ALIAS_TEXT','EXACT_ALIAS_HASHTAG')",
                  '(span_start IS NULL AND span_end IS NULL) OR (span_start>=0 AND span_end>span_start)',
                  "status<>'INVALIDATED' OR invalidated_at IS NOT NULL"),
          indexes=(('food_id', 'status', 'item_key'),), mutable=('status', 'invalidated_at', 'invalidated_by'), source_table='food_mentions'),
    Table('metric_observations', (NN('observation_id', 'id'), NN('observation_key', 'hash'), NN('ingest_run_id', 'id'), NN('source_code', 'code'),
          NN('metric_code', 'code'), NN('unit', 'code'), NN('subject_kind', 'code'), NN('subject_key', 'key'), C('food_id', 'id'), C('item_key', 'id'),
          NN('observed_at', 'ts'), C('period_start', 'ts'), C('period_end', 'ts'), C('provider_period', 'code'), NN('comparison_context_json', 'json'),
          NN('context_hash', 'hash'), NN('value_state', 'code'), C('count_value', 'int'), C('decimal_value', 'real'), C('raw_value', 'text'),
          NN('coverage', 'code'), NN('origin', 'code'), NN('policy_version', 'code'), C('expires_at', 'ts'), NN('status', 'code')),
          ('observation_id',), natural_key=('observation_key',), unique=(('observation_key',),),
          fks=((('ingest_run_id',), 'ingest_runs', ('ingest_run_id',)), (('food_id',), 'food_entities', ('food_id',)),
               (('item_key',), 'source_items', ('item_key',))),
          # Policy as a constraint: NAVER ratios and YouTube statistics are never stored.
          checks=("source_code IN ('google_trends_rss','instagram_graph')", "subject_kind IN ('FOOD','ITEM')",
                  "value_state IN ('OBSERVED','NOT_PROVIDED','NOT_OBSERVED')", 'count_value IS NULL OR count_value>=0',
                  "value_state='OBSERVED' OR (count_value IS NULL AND decimal_value IS NULL AND raw_value IS NULL)",
                  "value_state<>'OBSERVED' OR count_value IS NOT NULL OR decimal_value IS NOT NULL OR raw_value IS NOT NULL",
                  "subject_kind<>'FOOD' OR food_id IS NOT NULL", "subject_kind<>'ITEM' OR item_key IS NOT NULL",
                  "coverage IN ('COMPLETE','PARTIAL','LIMITED','NONE','UNKNOWN')", 'origin IN ' + ORIGINS, _status('status', ('ACTIVE', 'EXPIRED'))),
          indexes=(('food_id', 'metric_code', 'observed_at'),), mutable=('status',), source_table='metric_observations'),
    Table('entity_change_effects', (NN('effect_id', 'id'), NN('food_event_id', 'id'), NN('change_kind', 'code'), NN('affected_food_ids_json', 'json'),
          NN('invalidated_mentions', 'int'), NN('created_mentions', 'int'), NN('not_reprocessable_items', 'int'), NN('resolution_version', 'code'),
          NN('processed_at', 'ts')), ('effect_id',), natural_key=('food_event_id',), unique=(('food_event_id',),),
          fks=((('food_event_id',), 'food_events', ('event_id',)),), source_table='entity_change_effects'),
    Table('aggregate_invalidations', (NN('invalidation_id', 'id'), NN('invalidation_key', 'hash'), NN('food_id', 'id'), C('source_code', 'code'),
          NN('reason', 'code'), NN('cause_ref', 'key'), NN('created_at', 'ts'), C('consumed_at', 'ts')), ('invalidation_id',),
          natural_key=('invalidation_key',), unique=(('invalidation_key',),), fks=((('food_id',), 'food_entities', ('food_id',)),),
          mutable=('consumed_at',), source_table='aggregate_invalidations'),
    # v2 Oracle execution runtime state (owned by the Oracle runtime; never migrated from SQLite).
    Table('execution_mappings', (NN('job_id', 'id'), NN('plan_id', 'id'), NN('target_id', 'id'), NN('generation', 'int'),
          NN('food_revision', 'int'), NN('mapping_json', 'json'), NN('started', 'int')), ('job_id', 'target_id'),
          fks=((('job_id',), 'execution_jobs', ('job_id',)), (('plan_id',), 'planner_plans', ('plan_id',)),
               (('target_id',), 'tracking_targets', ('target_id',))), checks=('started IN (0,1)',), mutable=('started',), meta={'since': 2}),
    Table('execution_leases', (NN('resource_key', 'id'), NN('owner_id', 'text'), NN('fencing_generation', 'int'), NN('acquired_at', 'ts'),
          NN('renewed_at', 'ts'), NN('expires_at', 'ts')), ('resource_key',), fks=((('resource_key',), 'execution_jobs', ('job_id',)),),
          checks=('fencing_generation>0',), mutable=('owner_id', 'fencing_generation', 'acquired_at', 'renewed_at', 'expires_at'), meta={'since': 2}),
    Table('execution_checkpoints', (NN('job_id', 'id'), NN('state_json', 'json'), NN('updated_at', 'ts'), NN('expires_at', 'ts')),
          ('job_id',), fks=((('job_id',), 'execution_jobs', ('job_id',)),), mutable=('state_json', 'updated_at', 'expires_at'), meta={'since': 2}),
    Table('execution_item_receipts', (NN('job_id', 'id'), NN('item_id', 'key'), NN('observed_at', 'ts'), NN('expires_at', 'ts')),
          ('job_id', 'item_id'), fks=((('job_id',), 'execution_jobs', ('job_id',)),), indexes=(('expires_at',),), meta={'since': 2}),
    Table('execution_blocks', (NN('source_id', 'code'), NN('account_scope', 'code'), NN('error_kind', 'code'), NN('blocked_at', 'ts'),
          C('until_at', 'ts')), ('source_id', 'account_scope'), mutable=('error_kind', 'blocked_at', 'until_at'), meta={'since': 2}),
    Table('quota_charges', (NN('reservation_id', 'id'), NN('resource_code', 'code', source='resource'), NN('cost', 'int'), C('distinct_key', 'text'),
          NN('window_start', 'ts'), NN('window_end', 'ts'), NN('reset_timezone', 'code'), C('local_limit', 'int'), C('provider_limit', 'int')),
          ('reservation_id', 'resource_code'), fks=((('reservation_id',), 'quota_reservations', ('reservation_id',)),),
          checks=('cost>=0',), indexes=(('resource_code', 'window_start', 'window_end'),),
          mutable=('cost', 'distinct_key', 'window_start', 'window_end', 'reset_timezone', 'local_limit', 'provider_limit'), meta={'since': 2}),
    # v3 Oracle planner history (runtime-owned like v2: the SQLite planner keeps its own copies).
    Table('planner_plan_targets', (NN('plan_id', 'id'), NN('target_id', 'id'), NN('generation', 'int'), NN('food_revision', 'int'),
          NN('due_at', 'ts'), NN('mapping_json', 'json')), ('plan_id', 'target_id'),
          fks=((('plan_id',), 'planner_plans', ('plan_id',)), (('target_id',), 'tracking_targets', ('target_id',))),
          indexes=(('target_id',),), meta={'since': 3}),
    Table('planner_commands', (NN('request_key', 'key'), NN('payload_hash', 'hash'), NN('result_json', 'json'), NN('created_at', 'ts')),
          ('request_key',), meta={'since': 3}),
    # v4 tracking slots: the SQLite slot contract (one row per target, generation and due slot) plus its lifecycle.
    # due_slot is the normalized UTC stamp text (as execution_jobs.due_slot): Oracle cannot key a TSTZ column (ORA-02329).
    Table('tracking_slots', (NN('slot_id', 'id'), NN('target_id', 'id'), C('plan_id', 'id'), C('job_id', 'id'), NN('generation', 'int'),
          NN('food_revision', 'int'), NN('due_slot', 'code'), NN('status', 'code'), NN('created_at', 'ts'), C('claimed_at', 'ts'),
          C('completed_at', 'ts'), C('cancellation_reason', 'code')), ('slot_id',), unique=(('target_id', 'generation', 'due_slot'),),
          fks=((('target_id',), 'tracking_targets', ('target_id',)), (('plan_id',), 'planner_plans', ('plan_id',)),
               (('job_id',), 'execution_jobs', ('job_id',))),
          checks=(_status('status', ('SCHEDULED', 'CLAIMED', 'COMPLETED', 'FAILED', 'CANCELLED')),),
          indexes=(('plan_id',), ('job_id',), ('status', 'due_slot')),
          mutable=('status', 'claimed_at', 'completed_at', 'cancellation_reason'), meta={'since': 4}),
    # v5 exclusive write ownership (runtime_ownership): one mode row and one lease row per writing process.
    Table('runtime_ownership', (NN('owner_scope', 'code'), NN('owner_mode', 'code'), NN('mode_epoch', 'int'), NN('changed_at', 'ts'),
          NN('changed_by', 'id')), ('owner_scope',),
          checks=(_status('owner_mode', ('IDLE', 'SHADOW', 'RUNTIME_TEST', 'RUNTIME_PRODUCTION')), 'mode_epoch>=0'),
          mutable=('owner_mode', 'mode_epoch', 'changed_at', 'changed_by'), meta={'since': 5}),
    Table('runtime_participants', (NN('participant_id', 'id'), NN('owner_mode', 'code'), NN('mode_epoch', 'int'), NN('environment', 'code'),
          NN('joined_at', 'ts'), NN('renewed_at', 'ts'), NN('expires_at', 'ts'), C('released_at', 'ts')), ('participant_id',),
          checks=(_status('owner_mode', ('SHADOW', 'RUNTIME_TEST', 'RUNTIME_PRODUCTION')),), indexes=(('owner_mode', 'mode_epoch'),),
          mutable=('owner_mode', 'mode_epoch', 'environment', 'renewed_at', 'expires_at', 'released_at'), meta={'since': 5}),
    # v6 budget safety: legacy collector usage with its evidence, and per account scope what usage is known through when.
    Table('quota_usage_ledger', (NN('ledger_key', 'hash'), NN('source_id', 'code'), NN('account_scope', 'code'), NN('quota_type', 'code'),
          NN('window_start', 'ts'), NN('window_end', 'ts'), NN('provider_timezone', 'code'), NN('usage', 'int'), C('distinct_key', 'text'),
          NN('evidence', 'code'), NN('confidence', 'code'), NN('origin', 'code'), NN('source_ref', 'key'), NN('observed_at', 'ts'),
          NN('imported_at', 'ts')), ('ledger_key',),
          checks=('usage>=0', 'origin IN ' + ORIGINS, 'window_start<window_end',
                  _status('evidence', ('PROVIDER_CONFIRMED', 'LEGACY_LOCAL_COUNT', 'LEGACY_SENT_UNCERTAIN', 'LEGACY_UNKNOWN_ORIGIN')),
                  _status('confidence', ('CONFIRMED', 'COUNTED', 'UNCERTAIN', 'UNKNOWN'))),
          indexes=(('source_id', 'account_scope', 'quota_type', 'window_start'),),
          mutable=('usage', 'evidence', 'confidence', 'imported_at'), meta={'since': 6}),
    Table('quota_accounts', (NN('source_id', 'code'), NN('account_scope', 'code'), NN('usage_status', 'code'), C('legacy_observed_through', 'ts'),
          C('evidence_ref', 'key'), NN('updated_at', 'ts')), ('source_id', 'account_scope'),
          checks=(_status('usage_status', ('UNVERIFIED', 'IMPORTED', 'EXCLUSIVE')),),
          mutable=('usage_status', 'legacy_observed_through', 'evidence_ref', 'updated_at'), meta={'since': 6}),
    # v7 tracking commands on the target (OracleTrackingService): the SQLite command log and lifecycle history contract.
    # Runtime-owned like v2-v6: SQLite keeps its own history, which B7 does not copy (Shadow captures no command log).
    Table('tracking_commands', (NN('request_key', 'key'), NN('operation', 'code'), NN('payload_hash', 'hash'),
          NN('result_json', 'json'), NN('created_at', 'ts')), ('request_key',), meta={'since': 7}),
    Table('tracking_events', (NN('event_id', 'id'), NN('target_id', 'id'), NN('action', 'code'), NN('actor_ref', 'text'),
          NN('reason', 'text'), NN('occurred_at', 'ts'), C('before_json', 'json'), NN('after_json', 'json'), NN('request_key', 'key'),
          C('event_sequence', 'int')), ('event_id',),
          fks=((('target_id',), 'tracking_targets', ('target_id',)), (('request_key',), 'tracking_commands', ('request_key',))),
          indexes=(('target_id', 'event_sequence'),), meta={'since': 7}),
    # Target-only bookkeeping.
    Table('legacy_key_map', (NN('legacy_table', 'code'), NN('legacy_key', 'key'), NN('target_table', 'code'), NN('target_key', 'key'),
          NN('source_hash', 'hash'), NN('batch_key', 'id'), NN('mapped_at', 'ts')), ('legacy_table', 'legacy_key', 'target_table'),
          indexes=(('target_table', 'target_key'),), mutable=('source_hash', 'batch_key', 'mapped_at')),
    Table('migration_batches', (NN('batch_key', 'id'), NN('table_name', 'code'), NN('source_fingerprint', 'hash'), NN('status', 'code'),
          C('last_key', 'key'), NN('loaded_rows', 'int'), NN('excluded_rows', 'int'), NN('rejected_rows', 'int'), NN('started_at', 'ts'),
          C('finished_at', 'ts')), ('batch_key', 'table_name'), checks=(_status('status', ('RUNNING', 'COMPLETED', 'FAILED')),),
          mutable=('status', 'last_key', 'loaded_rows', 'excluded_rows', 'rejected_rows', 'finished_at')),
    Table('migration_rejections', (NN('rejection_key', 'hash'), NN('batch_key', 'id'), NN('table_name', 'code'), NN('subject_hash', 'hash'),
          NN('disposition', 'code'), NN('reason', 'code'), NN('recorded_at', 'ts')), ('rejection_key',),
          checks=(_status('disposition', ('EXCLUDED_POLICY', 'EXCLUDED_EXPIRED', 'REJECTED_INVALID')),), indexes=(('batch_key', 'table_name'),),
          mutable=('disposition', 'reason', 'recorded_at')),
    Table('shadow_applied', (NN('event_id', 'int'), NN('entity_table', 'code'), NN('entity_key', 'key'), NN('operation', 'code'),
          NN('applied_at', 'ts')), ('event_id',), checks=(_status('operation', ('UPSERT', 'DELETE')),)),
)
BY_NAME = {t.name: t for t in TABLES}


def _short(prefix, *parts):
    """Stable constraint/index names within Oracle's historical 30-character limit."""
    return (prefix + '_' + hashlib.sha256('|'.join(parts).encode()).hexdigest()[:20]).upper()


def _fk_indexed(table, cols):
    leading = [table.pk] + list(table.unique) + list(table.indexes) + [c for c, _ in table.partial_unique]
    return any(tuple(x[:len(cols)]) == tuple(cols) for x in leading)


def ddl(dialect):
    """Ordered DDL statements for one dialect ('oracle' or 'sqlite')."""
    oracle = dialect == 'oracle'
    out = []
    for t in TABLES:
        lines = []
        for c in t.columns:
            kind = KINDS[c.kind][0 if oracle else 1]
            # Oracle stores '' as NULL, so an empty-as-null column is nullable there and read back as ''.
            nullable = c.null or (oracle and c.empty_as_null)
            lines.append('%s %s%s' % (c.name, kind, '' if nullable else ' NOT NULL'))
        for c in t.columns:
            if c.kind == 'json':
                lines.append('CONSTRAINT %s CHECK (%s)' % (_short('CK', t.name, c.name, 'json'),
                             '%s IS JSON' % c.name if oracle else 'json_valid(%s)' % c.name))
        lines.append('CONSTRAINT %s PRIMARY KEY (%s)' % (_short('PK', t.name), ','.join(t.pk)))
        for u in t.unique:
            lines.append('CONSTRAINT %s UNIQUE (%s)' % (_short('UQ', t.name, *u), ','.join(u)))
        for check in t.checks:
            lines.append('CONSTRAINT %s CHECK (%s)' % (_short('CK', t.name, check), check))
        for cols, parent, pcols in t.fks:
            lines.append('CONSTRAINT %s FOREIGN KEY (%s) REFERENCES %s (%s) DEFERRABLE INITIALLY DEFERRED'
                         % (_short('FK', t.name, *cols), ','.join(cols), parent, ','.join(pcols)))
        out.append('CREATE TABLE %s (\n  %s\n)' % (t.name, ',\n  '.join(lines)))
        for cols, predicate in t.partial_unique:
            if oracle:
                expr = ','.join('CASE WHEN %s THEN %s END' % (predicate, c) for c in cols)
                out.append('CREATE UNIQUE INDEX %s ON %s (%s)' % (_short('UX', t.name, *cols), t.name, expr))
            else:
                out.append('CREATE UNIQUE INDEX %s ON %s (%s) WHERE %s' % (_short('UX', t.name, *cols), t.name, ','.join(cols), predicate))
        indexes = list(t.indexes) + [cols for cols, _, _ in t.fks if not _fk_indexed(t, cols)]
        for cols in dict.fromkeys(tuple(i) for i in indexes):
            out.append('CREATE INDEX %s ON %s (%s)' % (_short('IX', t.name, *cols), t.name, ','.join(cols)))
    return out


def add_column(dialect, table, column):
    """ALTER statement that brings an older target table up to the specification."""
    kind = KINDS[column.kind][0 if dialect == 'oracle' else 1]
    return ('ALTER TABLE %s ADD (%s %s)' if dialect == 'oracle' else 'ALTER TABLE %s ADD COLUMN %s %s') % (table, column.name, kind)


def identifiers():
    names = []
    for t in TABLES:
        names.append(t.name)
        names += [c.name for c in t.columns]
    return names
