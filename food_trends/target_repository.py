"""B6 repository over the target schema (Oracle or the SQLite stand-in).

Portable SQL only: named binds, plain INSERT/UPDATE/DELETE/SELECT. An upsert reads the logical key,
then inserts; a unique-key race (ORA-00001 / SQLite UNIQUE) re-reads and updates. Immutable
columns never change, origin labels are never upgraded, B4/B5 policy refusals are applied here
and again by table constraints. The schema is created only by migrate(), never by a constructor.
"""
import hashlib
from datetime import datetime, timezone
from .ingestion import METRIC_DEFINITIONS
from .target_db import IntegrityViolation, PolicyRefused, TargetError, UniqueViolation, convert, logical, to_target, utc
from .target_schema import BY_NAME, TARGET_SCHEMA_VERSION, ddl

ORIGIN_VALUES = ('live_api', 'mock', 'user', 'legacy_unknown')
MENTION_SOURCES = ('google_trends_rss', 'instagram_graph')
PROTECTED = ('origin', 'first_origin')
# Rows whose ACTIVE life ends at expires_at; an already expired one is never (re)created.
RETENTION = {'source_contents': None, 'source_items': ('availability', 'AVAILABLE'), 'item_snapshots': ('purged_at', None),
             'item_query_matches': ('status', 'ACTIVE'), 'food_mentions': ('status', 'ACTIVE'),
             'metric_observations': ('status', 'ACTIVE')}


def policy(table, row, now):
    for column in PROTECTED:
        if column in row and row[column] is not None and row[column] not in ORIGIN_VALUES:
            raise PolicyRefused('origin_not_allowed')
    if table == 'metric_observations':
        definition = METRIC_DEFINITIONS.get(row.get('metric_code'))
        if not definition or not definition['storable'] or definition['source'] != row.get('source_code'):
            raise PolicyRefused('metric_storage_blocked')
    if table == 'food_mentions' and row.get('source_code') not in MENTION_SOURCES:
        raise PolicyRefused('mention_source_blocked')
    if table in ('source_items', 'source_contents') and row.get('source_code') == 'naver_datalab':
        raise PolicyRefused('naver_content_blocked')
    if table in RETENTION and row.get('expires_at'):
        state = RETENTION[table]
        active = state is None or row.get(state[0]) == state[1]
        if active and utc(row['expires_at']) <= now:
            raise PolicyRefused('expired_content')


class TargetRepository:
    def __init__(self, db, clock=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.before_write = lambda table, key: None  # test hook for race injection

    def now(self):
        return utc(self.clock())

    # Schema

    def _exists(self, tx, name):
        if self.db.dialect == 'oracle':
            return bool(tx.scalar('SELECT count(*) FROM user_objects WHERE object_name=UPPER(:b_name)', {'b_name': name}))
        return bool(tx.scalar('SELECT count(*) FROM sqlite_master WHERE lower(name)=lower(:b_name)', {'b_name': name}))

    def checksum(self):
        return hashlib.sha256('\n'.join(ddl(self.db.dialect)).encode()).hexdigest()

    def migrate(self):
        """Explicit target DDL. Oracle DDL auto-commits, so each object is created only if missing (resumable)."""
        oracle = self.db.dialect == 'oracle'
        with self.db.transaction() as tx:
            if not self._exists(tx, 'target_schema_migrations'):
                tx.execute('CREATE TABLE target_schema_migrations (version %s PRIMARY KEY, checksum %s NOT NULL, applied_at %s NOT NULL)'
                           % (('NUMBER(10)', 'VARCHAR2(64 CHAR)', 'TIMESTAMP(6) WITH TIME ZONE') if oracle else ('INTEGER', 'TEXT', 'TEXT')))
            current = tx.scalar('SELECT max(version) FROM target_schema_migrations') or 0
            if current > TARGET_SCHEMA_VERSION:
                raise IntegrityViolation('target_schema_newer_than_application')
            if current == TARGET_SCHEMA_VERSION:
                recorded = tx.scalar('SELECT checksum FROM target_schema_migrations WHERE version=:b_v', {'b_v': current})
                if recorded != self.checksum():
                    raise IntegrityViolation('target_schema_checksum_mismatch')
                return {'version': current, 'applied': [], 'created_objects': 0}
            created = 0
            for statement in ddl(self.db.dialect):
                words = statement.split()
                name = words[2] if words[1] in ('TABLE', 'INDEX') else words[3]
                if not self._exists(tx, name):
                    tx.execute(statement)
                    created += 1
            stamp = self.now() if oracle else self.now().isoformat(timespec='microseconds')
            tx.execute('INSERT INTO target_schema_migrations VALUES (:b_v,:b_c,:b_t)',
                       {'b_v': TARGET_SCHEMA_VERSION, 'b_c': self.checksum(), 'b_t': stamp}, ts_binds=('b_t',) if oracle else ())
        return {'version': TARGET_SCHEMA_VERSION, 'applied': [TARGET_SCHEMA_VERSION], 'created_objects': created}

    def schema_version(self):
        with self.db.transaction() as tx:
            if not self._exists(tx, 'target_schema_migrations'):
                return 0
            return tx.scalar('SELECT max(version) FROM target_schema_migrations') or 0

    # Rows

    @staticmethod
    def _where(spec):
        return ' AND '.join('%s=:k_%s' % (k, k) for k in spec.key)

    def _key_binds(self, spec, row):
        return {'k_' + k: convert(spec.column(k), row.get(k), self.db.dialect) for k in spec.key}

    def _read(self, tx, spec, keys):
        rows = tx.rows('SELECT * FROM %s WHERE %s' % (spec.name, self._where(spec)), keys)
        return logical(spec.name, rows[0]) if rows else None

    def _ts(self, spec, names):
        return tuple(n for n in names if self.db.dialect == 'oracle' and spec.column(n[2:]).kind == 'ts')

    def upsert(self, tx, table, row):
        """Idempotent by logical key. Returns 'inserted', 'updated' or 'unchanged'."""
        spec = BY_NAME[table]
        policy(table, row, self.now())
        binds = to_target(table, row, self.db.dialect)
        new = logical(table, {c.name: row.get(c.name) for c in spec.columns})
        keys = self._key_binds(spec, row)
        for attempt in (1, 2):
            old = self._read(tx, spec, keys)
            if old is None:
                self.before_write(table, keys)
                columns = [c.name for c in spec.columns]
                try:
                    tx.execute('INSERT INTO %s (%s) VALUES (%s)' % (table, ','.join(columns), ','.join(':b_' + c for c in columns)),
                               binds, ts_binds=self._ts(spec, ['b_' + c for c in columns]))
                    return 'inserted'
                except Exception as exc:
                    mapped = exc if isinstance(exc, TargetError) else self.db.map_error(exc)
                    if isinstance(mapped, UniqueViolation) and attempt == 1:
                        continue  # a concurrent writer won the race: re-read the same logical key and update
                    raise mapped from None
            for column in spec.columns:
                if column.name in spec.mutable or column.name in spec.key or old[column.name] == new[column.name]:
                    continue
                if column.name in PROTECTED:
                    raise PolicyRefused('origin_change_refused')
                if not (spec.natural_key and column.name in spec.pk):  # surrogate id of a natural-key row
                    raise IntegrityViolation('immutable_column_changed:' + column.name)
            changed = [m for m in spec.mutable if old[m] != new[m]]
            if not changed:
                return 'unchanged'
            sql = 'UPDATE %s SET %s WHERE %s' % (table, ','.join('%s=:b_%s' % (m, m) for m in changed), self._where(spec))
            tx.execute(sql, {**{'b_' + m: binds['b_' + m] for m in changed}, **keys}, ts_binds=self._ts(spec, ['b_' + m for m in changed]))
            return 'updated'

    def delete(self, tx, table, row):
        spec = BY_NAME[table]
        return tx.execute('DELETE FROM %s WHERE %s' % (table, self._where(spec)), self._key_binds(spec, row)).rowcount

    def get(self, table, row):
        spec = BY_NAME[table]
        with self.db.transaction() as tx:
            return self._read(tx, spec, self._key_binds(spec, row))

    def rows(self, table):
        with self.db.transaction() as tx:
            return [logical(table, r) for r in tx.rows('SELECT * FROM %s' % table)]

    def count(self, table):
        with self.db.transaction() as tx:
            return tx.scalar('SELECT count(*) FROM %s' % table)

    # Domain reads shared with SQLite services (portable SQL)

    def resolve_alias(self, normalized_form, scope, version='food-nfc-space-v1'):
        with self.db.transaction() as tx:
            rows = tx.rows('''SELECT f.food_id,f.canonical_name,a.alias_id FROM food_aliases a JOIN food_entities f ON f.food_id=a.food_id
                WHERE a.normalized_form=:b_n AND a.scope=:b_s AND a.normalization_version=:b_v AND a.status='ACTIVE' AND f.status='ACTIVE' ''',
                           {'b_n': normalized_form, 'b_s': scope, 'b_v': version})
        return rows[0] if rows else None

    def food_view(self, food_id):
        """Same per-source counts as FoodObservationService.food_view, for SQLite/Oracle comparison."""
        with self.db.transaction() as tx:
            food = tx.rows('SELECT food_id,canonical_name,status,merged_into FROM food_entities WHERE food_id=:b_f', {'b_f': food_id})
            if not food:
                return None
            if food[0]['status'] == 'MERGED':
                return dict(self.food_view(food[0]['merged_into']), requested_food_id=food_id, redirected=True)
            ids = [food_id] + [r['food_id'] for r in tx.rows('SELECT food_id FROM food_entities WHERE merged_into=:b_f ORDER BY food_id', {'b_f': food_id})]
            marks = ','.join(':i%d' % n for n in range(len(ids)))
            binds = {'i%d' % n: v for n, v in enumerate(ids)}
            sources = {}
            for source in ('google_trends_rss', 'instagram_graph', 'youtube', 'naver_datalab'):
                b = dict(binds, b_s=source)
                confirmed = tx.rows(f'''SELECT count(DISTINCT m.item_key) AS c1,count(*) AS c2 FROM food_mentions m JOIN source_items s ON s.item_key=m.item_key
                    WHERE s.source_code=:b_s AND m.status='ACTIVE' AND m.food_id IN ({marks})''', b)[0]
                searched = tx.rows(f'''SELECT count(DISTINCT x.item_key) AS c1,count(*) AS c2 FROM item_query_matches x
                    JOIN planner_query_targets q ON q.query_id=x.query_id JOIN tracking_targets t ON t.target_id=q.target_id
                    JOIN source_items s ON s.item_key=x.item_key WHERE s.source_code=:b_s AND x.status='ACTIVE' AND t.food_id IN ({marks})''', b)[0]
                metrics = tx.rows(f'''SELECT metric_code,unit,observed_at,value_state,count_value,decimal_value,raw_value,coverage,context_hash,origin
                    FROM metric_observations WHERE source_code=:b_s AND status='ACTIVE' AND (food_id IN ({marks}) OR item_key IN
                    (SELECT item_key FROM food_mentions WHERE status='ACTIVE' AND food_id IN ({marks}))) ORDER BY observed_at,metric_code''', b)
                sources[source] = {'confirmed_items': int(confirmed['c1']), 'mentions': int(confirmed['c2']),
                                   'search_matched_items': int(searched['c1']), 'search_matches': int(searched['c2']),
                                   'metrics': sorted((m['metric_code'], m['unit'], m['value_state'], None if m['count_value'] is None else int(m['count_value']),
                                                      m['decimal_value'], m['raw_value'], m['coverage'], m['context_hash'], m['origin']) for m in metrics)}
            return {'food_id': food_id, 'canonical_name': food[0]['canonical_name'], 'merged_food_ids': ids[1:], 'sources': sources}

