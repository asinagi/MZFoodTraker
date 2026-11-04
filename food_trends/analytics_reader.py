"""B8 read input: DBMS-independent read sessions over the B4/B5 observation rows.

One portable SQL text per question (named binds, no LIMIT, no SQLite functions, no AS on table aliases,
no DISTINCT over LOBs), so the same statements run on the SQLite application database and on the B6
target schema (Oracle, or its SQLite stand-in). Timestamps are returned as UTC instants and compared in
Python; KST days belong to the analytics layer. Nothing here writes: the SQLite store reads through the
read-only, query_only connection and the target store opens a read-only transaction on Oracle.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

INSTAGRAM_SCOPE_METRIC = 'instagram.observed_posts'


class AnalyticsUnavailable(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def instant(value):
    """Stored timestamp -> aware UTC datetime. Naive datetimes are UTC, as the target stores them."""
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _text(value):
    return value.read() if hasattr(value, 'read') else value  # Oracle CLOB


def _in(column, values, prefix):
    marks = ','.join(':%s%d' % (prefix, n) for n in range(len(values)))
    return ' AND %s IN (%s)' % (column, marks), {'%s%d' % (prefix, n): v for n, v in enumerate(values)}


class ReadSession:
    """Questions the B8 read models ask. rows(sql, binds) -> list of dicts with lower-case keys."""

    def __init__(self, rows):
        self._rows = rows

    def _query(self, sql, binds=None, times=(), texts=()):
        out = []
        for row in self._rows(sql, binds or {}):
            row = dict(row)
            for name in times:
                row[name] = instant(row[name])
            for name in texts:
                row[name] = _text(row[name])
            out.append(row)
        return out

    def foods(self):
        return self._query('SELECT food_id,canonical_name,entity_kind,status,merged_into,created_at,updated_at FROM food_entities',
                           times=('created_at', 'updated_at'))

    def aliases(self):
        return self._query("SELECT alias_id,food_id,surface_form,normalized_form,scope FROM food_aliases WHERE status='ACTIVE'")

    def relations(self):
        return self._query("SELECT from_food_id,to_food_id,relation_type FROM food_relations WHERE status='ACTIVE'")

    def confirmed(self, source, food_ids=None):
        """Item observations of confirmed mentions (not search matches), one row per mention state and run."""
        where, binds = _in('m.food_id', food_ids, 'f') if food_ids else ('', {})
        return self._query('''SELECT DISTINCT m.food_id AS food_id, m.item_key AS item_key, m.status AS mention_status,
                m.expires_at AS mention_expires_at, s.availability AS availability, r.observed_at AS observed_at,
                r.quality_status AS quality_status, g.ingest_run_id AS ingest_run_id, g.origin AS origin, g.coverage AS coverage
            FROM food_mentions m JOIN source_items s ON s.item_key=m.item_key
            JOIN run_items r ON r.item_key=m.item_key JOIN ingest_runs g ON g.ingest_run_id=r.ingest_run_id
            WHERE s.source_code=:b_source AND g.source_code=:b_source AND m.status IN ('ACTIVE','EXPIRED')''' + where,
                           dict(binds, b_source=source), times=('mention_expires_at', 'observed_at'))

    def runs(self, source):
        return self._query('''SELECT ingest_run_id,observed_at,origin,coverage,query_id,rejected_items FROM ingest_runs
            WHERE source_code=:b_source''', {'b_source': source}, times=('observed_at',))

    def food_scope(self, source, food_ids=None):
        """Per-food observation attempts B5 records for every food a tracking run served (failed runs included)."""
        where, binds = _in('mo.food_id', food_ids, 'f') if food_ids else ('', {})
        return self._query('''SELECT mo.food_id AS food_id, mo.ingest_run_id AS ingest_run_id, mo.observed_at AS observed_at,
                mo.origin AS origin, mo.coverage AS coverage, mo.value_state AS value_state, g.query_id AS query_id
            FROM metric_observations mo JOIN ingest_runs g ON g.ingest_run_id=mo.ingest_run_id
            WHERE mo.source_code=:b_source AND mo.metric_code=:b_metric AND mo.subject_kind='FOOD' AND mo.status='ACTIVE' ''' + where,
                           dict(binds, b_source=source, b_metric=INSTAGRAM_SCOPE_METRIC), times=('observed_at',))

    def quality(self, source):
        return self._query('''SELECT q.ingest_run_id AS ingest_run_id, q.outcome AS outcome, count(*) AS n
            FROM quality_results q JOIN ingest_runs g ON g.ingest_run_id=q.ingest_run_id
            WHERE g.source_code=:b_source GROUP BY q.ingest_run_id, q.outcome''', {'b_source': source})

    def legacy_summary(self, source):
        rows = self._query('''SELECT count(DISTINCT g.ingest_run_id) AS runs, count(DISTINCT r.item_key) AS items
            FROM ingest_runs g LEFT JOIN run_items r ON r.ingest_run_id=g.ingest_run_id
            WHERE g.source_code=:b_source AND g.origin='legacy_unknown' ''', {'b_source': source})
        return {k: int(v or 0) for k, v in rows[0].items()}

    def keyword_traffic(self, food_ids):
        """Google Trends display traffic of items whose trend keyword itself is an approved alias of the food."""
        where, binds = _in('m.food_id', food_ids, 'f')
        return self._query('''SELECT m.food_id AS food_id, mo.item_key AS item_key, mo.observed_at AS observed_at,
                mo.raw_value AS raw_value, mo.origin AS origin, a.surface_form AS keyword
            FROM metric_observations mo JOIN food_mentions m ON m.item_key=mo.item_key JOIN food_aliases a ON a.alias_id=m.alias_id
            WHERE mo.source_code='google_trends_rss' AND mo.metric_code='rss.approx_traffic' AND mo.status='ACTIVE'
              AND mo.value_state='OBSERVED' AND m.status='ACTIVE' AND m.field_code='keyword'
              AND m.verification_status='EXACT_ALIAS_FIELD' ''' + where, binds, times=('observed_at',))

    def pending_entity_events(self):
        """Approved alias/food decisions B5 has not reprocessed yet (same selection as apply_entity_changes)."""
        return self._query('''SELECT e.event_id AS event_id, e.subject_type AS subject_type, e.subject_id AS subject_id,
                e.action AS action, e.before_json AS before_json, e.after_json AS after_json FROM food_events e
            WHERE e.subject_type IN ('alias','food') AND e.action IN ('APPROVED','CHANGED','REVERTED','MERGED')
              AND NOT EXISTS (SELECT 1 FROM entity_change_effects x WHERE x.food_event_id=e.event_id)''',
                           texts=('before_json', 'after_json'))


class SQLiteAnalyticsStore:
    """The application's SQLite database through Storage's read-only connection (mode=ro, query_only)."""

    def __init__(self, storage):
        self.storage = storage

    def ready(self):
        from .migrations import LATEST_VERSION, schema_version
        try:
            return schema_version(self.storage.path) == LATEST_VERSION
        except (sqlite3.Error, RuntimeError, OSError):
            return False

    @contextmanager
    def session(self):
        if not self.ready():
            raise AnalyticsUnavailable('schema_not_ready')
        try:
            with self.storage.reader() as db:
                yield ReadSession(lambda sql, binds: db.execute(sql, binds).fetchall())
        except sqlite3.Error:
            raise AnalyticsUnavailable('storage_unavailable') from None


class TargetAnalyticsStore:
    """The B6 target schema through TargetRepository's adapter: Oracle (read-only transaction) or the SQLite stand-in."""

    def __init__(self, repo):
        self.repo = repo

    def ready(self):
        from .target_schema import TARGET_SCHEMA_VERSION
        try:
            return self.repo.schema_version() == TARGET_SCHEMA_VERSION
        except Exception:
            return False

    @contextmanager
    def session(self):
        if not self.ready():
            raise AnalyticsUnavailable('schema_not_ready')
        from .target_db import TargetError
        try:
            with self.repo.db.transaction() as tx:
                if self.repo.db.dialect == 'oracle':
                    tx.execute('SET TRANSACTION READ ONLY')  # one consistent snapshot for every statement of a request
                yield ReadSession(tx.rows)
        except TargetError:
            raise AnalyticsUnavailable('storage_unavailable') from None
