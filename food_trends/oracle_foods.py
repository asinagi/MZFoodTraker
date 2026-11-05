"""ORACLE-CUTOVER-PREP-1: reviewed food identity on the target schema (Oracle, or its SQLite stand-in).

FoodService's decisions run unchanged: explicit approval for every review command, exact-alias resolution,
the brand/store gate, scoped alias uniqueness, refused variant cycles, kept merge redirects, one transaction
and a replayable result per request key. Only its storage primitives read and write target tables, inside
the Oracle runtime's ownership-guarded transactions (refused while Shadow owns the target). Identity commands
are serialized among themselves by an exclusive lock on food_commands, as BEGIN IMMEDIATE serializes them on
SQLite, so a revision check and its write never interleave with another command. The RSS candidate import reads
the target's own discovery rows (oracle_rss): items are source_contents 'items:N' (N is the SQLite item id B7 loaded
or the next one), classifications rss_classifications and observations rss_observations; a candidate refers to its
item by source_item_key, so candidate keys and replay request keys stay the ones the SQLite bridge used.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from .food_service import NORMALIZATION_VERSION, FoodError, FoodService, dump, text
from .target_db import IntegrityViolation, UniqueViolation


def _now():
    return datetime.now(timezone.utc)


class OracleFoodService(FoodService):
    def __init__(self, execution):
        super().__init__(None)
        self.execution = execution

    def _reader(self):
        return self.execution.reader()

    @contextmanager
    def _writer(self):
        try:
            with self.execution.transaction() as t:
                if t.oracle:
                    t.run('LOCK TABLE food_commands IN EXCLUSIVE MODE')
                yield t
        except (UniqueViolation, IntegrityViolation) as exc:  # the target's own constraints (active alias index, FKs)
            raise FoodError('별칭·관계·이력 제약에 충돌했습니다. 새로 조회 후 검토하세요.', 409) from exc

    def _row(self, t, table, column, value):
        if type(value) not in (str, int):
            raise FoodError('대상 ID 형식을 확인하세요.')
        row = t.one('SELECT * FROM %s WHERE %s=:b_v' % (table, column), table, b_v=value)
        if row is None:
            raise FoodError('대상을 찾을 수 없습니다.', 404)
        return row

    @staticmethod
    def _command(t, key):
        return t.one('SELECT * FROM food_commands WHERE request_key=:b_k', 'food_commands', b_k=key)

    @staticmethod
    def _put_command(t, key, operation, fingerprint):
        t.insert('food_commands', dict(request_key=key, operation=operation, payload_hash=fingerprint, result_json='{}', created_at=_now()))

    @staticmethod
    def _command_result(t, key, result):
        t.update('food_commands', {'request_key': key}, result_json=dump(result))

    @staticmethod
    def _put_event(t, row):
        # Append order is authoritative (the SQLite rowid); the food_commands lock serializes every appender here.
        sequence = int(t.scalar('SELECT coalesce(max(event_sequence),0)+1 AS n FROM food_events'))
        t.insert('food_events', dict(row, event_sequence=sequence))

    @staticmethod
    def _touch(t, food_id):
        t.run('UPDATE food_entities SET revision=revision+1,updated_at=:b_now WHERE food_id=:b_f', b_now=_now(), b_f=food_id)

    @staticmethod
    def _aliases_of(t, food_id, status):
        return t.rows('SELECT * FROM food_aliases WHERE food_id=:b_f AND status=:b_s ORDER BY scope,normalized_form,alias_id', 'food_aliases',
                      b_f=food_id, b_s=status)

    @staticmethod
    def _relations_of(t, food_id):
        return t.rows("""SELECT * FROM food_relations WHERE status='ACTIVE' AND (from_food_id=:b_f OR to_food_id=:b_f)
            ORDER BY relation_id""", 'food_relations', b_f=food_id)

    @staticmethod
    def _food_ids(t):
        return [r['food_id'] for r in t.rows('SELECT food_id FROM food_entities ORDER BY created_at,food_id')]

    @staticmethod
    def _candidate_ids(t):
        return [r['candidate_id'] for r in t.rows('SELECT candidate_id FROM food_candidates ORDER BY created_at,candidate_id')]

    @staticmethod
    def _event_rows(t, subject_id):
        # Rows of a target upgraded from v6 may lack a sequence: they come first, in time order (NULL order differs by DBMS).
        return t.rows('''SELECT * FROM food_events WHERE subject_id=:b_s
            ORDER BY CASE WHEN event_sequence IS NULL THEN 0 ELSE 1 END,event_sequence,occurred_at,event_id''', 'food_events', b_s=subject_id)

    @staticmethod
    def _exact(t, name, scope):
        return t.one("""SELECT a.* FROM food_aliases a JOIN food_entities f ON f.food_id=a.food_id
            WHERE a.normalized_form=:b_n AND a.scope=:b_s AND a.normalization_version=:b_v AND a.status='ACTIVE' AND f.status='ACTIVE'""",
                     'food_aliases', b_n=text(name), b_s=scope, b_v=NORMALIZATION_VERSION)

    @staticmethod
    def _candidate_by_key(t, candidate_key):
        row = t.one('SELECT candidate_id FROM food_candidates WHERE candidate_key=:b_k', b_k=candidate_key)
        return row['candidate_id'] if row else None

    @staticmethod
    def _put_candidate(t, row):
        row = dict(row)
        item = row.pop('source_item_id')  # an RSS item id (checked by the import) -> its item key, as B7 loads it
        key = t.scalar('SELECT item_key AS k FROM source_contents WHERE payload_ref=:b_p', b_p='items:%d' % item) if item is not None else None
        t.insert('food_candidates', dict(row, source_item_key=key, status='NEW', revision=1, resolved_food_id=None,
                                         resolution_alias_id=None, resolution_method=None))

    @staticmethod
    def _set_candidate(t, candidate_id, status, food_id, alias_id, method):
        t.run("""UPDATE food_candidates SET status=:b_s,resolved_food_id=:b_f,resolution_alias_id=:b_a,resolution_method=:b_m,
            revision=revision+1,updated_at=:b_now WHERE candidate_id=:b_c""",
              b_s=status, b_f=food_id, b_a=alias_id, b_m=method, b_now=_now(), b_c=candidate_id)

    @staticmethod
    def _put_alias(t, row):
        t.insert('food_aliases', dict(row, revision=1))

    @staticmethod
    def _put_food(t, row):
        t.insert('food_entities', dict(row, status='ACTIVE', revision=1, merged_into=None))

    @staticmethod
    def _rename(t, food_id, name):
        t.run('UPDATE food_entities SET canonical_name=:b_n,revision=revision+1,updated_at=:b_now WHERE food_id=:b_f',
              b_n=name, b_now=_now(), b_f=food_id)

    @staticmethod
    def _resolved_by_alias(t, alias_id):
        return [r['candidate_id'] for r in t.rows("""SELECT candidate_id FROM food_candidates WHERE resolution_alias_id=:b_a
            AND status='RESOLVED'""", b_a=alias_id)]

    @staticmethod
    def _set_alias(t, alias_id, food_id, status):
        t.run('UPDATE food_aliases SET food_id=:b_f,status=:b_s,revision=revision+1,updated_at=:b_now WHERE alias_id=:b_a',
              b_f=food_id, b_s=status, b_now=_now(), b_a=alias_id)

    @staticmethod
    def _variant_cycle(t, source, target):
        """The SQLite recursive check in Python (portable): does target already reach source through active VARIANT_OF?"""
        edges = {}
        for r in t.rows("SELECT from_food_id,to_food_id FROM food_relations WHERE status='ACTIVE' AND relation_type='VARIANT_OF'"):
            edges.setdefault(r['from_food_id'], []).append(r['to_food_id'])
        seen, stack = {target}, [target]
        while stack:
            for parent in edges.get(stack.pop(), ()):
                if parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        return source in seen

    @staticmethod
    def _active_relation(t, source, target, kind):
        return t.one("""SELECT * FROM food_relations WHERE from_food_id=:b_f AND to_food_id=:b_t AND relation_type=:b_k
            AND status='ACTIVE'""", 'food_relations', b_f=source, b_t=target, b_k=kind)

    @staticmethod
    def _put_relation(t, row):
        t.insert('food_relations', dict(row, status='ACTIVE', revision=1))

    @staticmethod
    def _has_relation(t, food_id):
        return bool(t.scalar("""SELECT count(*) AS c FROM food_relations WHERE status='ACTIVE'
            AND (from_food_id=:b_f OR to_food_id=:b_f)""", b_f=food_id))

    @staticmethod
    def _merge_aliases(t, food_id):
        return t.rows("SELECT * FROM food_aliases WHERE food_id=:b_f AND status='ACTIVE' ORDER BY alias_id", 'food_aliases', b_f=food_id)

    @staticmethod
    def _mark_merged(t, food_id, target_id):
        t.run("UPDATE food_entities SET status='MERGED',merged_into=:b_t,revision=revision+1,updated_at=:b_now WHERE food_id=:b_f",
              b_t=target_id, b_now=_now(), b_f=food_id)

    # RSS discovery reads over the target's own discovery rows (written by oracle_rss or loaded by B7).

    def _rss_item(self, t, item_id):
        content = t.one("SELECT * FROM source_contents WHERE payload_ref=:b_p AND source_code='google_trends_rss'", 'source_contents',
                        b_p='items:%d' % item_id)
        if content is None:
            raise FoodError('대상을 찾을 수 없습니다.', 404)
        body = json.loads(content['content_json'])
        return {'id': item_id, 'item_key': content['item_key'], 'keyword': body.get('keyword'), 'published_at': body.get('published_at')}

    def _rss_classification(self, t, item):
        return self._row(t, 'rss_classifications', 'item_key', item['item_key'])

    @staticmethod
    def _rss_prior(t, item):
        limit = ' FETCH FIRST 1 ROWS ONLY' if t.oracle else ' LIMIT 1'
        row = t.one('SELECT contract_json FROM rss_observations WHERE item_key=:b_k ORDER BY run_id DESC' + limit, 'rss_observations',
                    b_k=item['item_key'])
        return json.loads(row['contract_json']) if row else None

    @staticmethod
    def _rss_candidate(t, source, item):
        return t.one('SELECT * FROM food_candidates WHERE source_code=:b_s AND source_item_key=:b_k', 'food_candidates',
                     b_s=source, b_k=item['item_key'])

    @staticmethod
    def _retype(t, candidate_id, kind, gate):
        t.run('UPDATE food_candidates SET proposed_kind=:b_k,type_gate=:b_g,revision=revision+1,updated_at=:b_now WHERE candidate_id=:b_c',
              b_k=kind, b_g=gate, b_now=_now(), b_c=candidate_id)

    def _rss_run_items(self, t, run_id):
        if not self._row(t, 'rss_runs', 'run_id', run_id)['success']:
            return None
        refs = t.rows("""SELECT o.payload_ref FROM rss_observations o JOIN rss_classifications c ON c.item_key=o.item_key
            WHERE o.run_id=:b_r AND c.classification IN ('FOOD','REVIEW')""", b_r=run_id)
        return sorted(int(r['payload_ref'].partition(':')[2]) for r in refs)
