"""Reviewed food identity. No collection, query planning or implicit writes on reads."""
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager

from .classifier import normalize
from .contracts import envelope, encode, require_operation
from .entity_types import FOOD_KINDS, CANDIDATE_KINDS, candidate_kind
from .storage import utcnow

NORMALIZATION_VERSION = 'food-nfc-space-v1'
RESOLUTION_VERSION = 'approved-exact-alias-v1'
DEFAULT_SCOPE = 'food:ko'


class FoodError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def text(value, limit=200):
    if not isinstance(value, str) or any(unicodedata.category(c).startswith('C') for c in value):
        raise FoodError('문자열 형식을 확인하세요.')
    value = normalize(value)
    if not 1 <= len(value) <= limit:
        raise FoodError(f'입력은 1~{limit}자여야 합니다.')
    return value


def identity(prefix):
    return prefix + '_' + uuid.uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class FoodService:
    def __init__(self, storage):
        self.storage = storage

    # Storage primitives. The Oracle runtime (oracle_foods) overrides only these; the decisions below stay shared.

    def _reader(self):
        return self.storage.reader()

    @contextmanager
    def _writer(self):
        """One command transaction; a constraint conflict surfaces as a reviewable 409."""
        try:
            with self.storage.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                yield db
        except sqlite3.IntegrityError as exc:
            raise FoodError('별칭·관계·이력 제약에 충돌했습니다. 새로 조회 후 검토하세요.', 409) from exc

    @staticmethod
    def _command(db, key):
        row = db.execute('SELECT * FROM food_commands WHERE request_key=?', (key,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _put_command(db, key, operation, fingerprint):
        db.execute('INSERT INTO food_commands VALUES (?,?,?,?,?)', (key, operation, fingerprint, '{}', utcnow()))

    @staticmethod
    def _command_result(db, key, result):
        db.execute('UPDATE food_commands SET result_json=? WHERE request_key=?', (dump(result), key))

    @staticmethod
    def _put_event(db, row):
        db.execute('INSERT INTO food_events VALUES (?,?,?,?,?,?,?,?,?,?,?)', tuple(row.values()))

    @staticmethod
    def _touch(db, food_id):
        db.execute('UPDATE food_entities SET revision=revision+1,updated_at=? WHERE food_id=?', (utcnow(), food_id))

    @staticmethod
    def _aliases_of(db, food_id, status):
        return [dict(r) for r in db.execute('''SELECT * FROM food_aliases
            WHERE food_id=? AND status=? ORDER BY scope,normalized_form,alias_id''', (food_id, status))]

    @staticmethod
    def _relations_of(db, food_id):
        return [dict(r) for r in db.execute('''SELECT * FROM food_relations WHERE status='ACTIVE'
            AND (from_food_id=? OR to_food_id=?) ORDER BY relation_id''', (food_id, food_id))]

    @staticmethod
    def _food_ids(db):
        return [r[0] for r in db.execute('SELECT food_id FROM food_entities ORDER BY created_at,food_id')]

    @staticmethod
    def _candidate_ids(db):
        return [r[0] for r in db.execute('SELECT candidate_id FROM food_candidates ORDER BY created_at,candidate_id')]

    @staticmethod
    def _event_rows(db, subject_id):
        # Wall-clock timestamps can tie on Windows; append order is authoritative.
        return [dict(r) for r in db.execute('SELECT rowid AS event_sequence,* FROM food_events WHERE subject_id=? ORDER BY rowid', (subject_id,))]

    @staticmethod
    def _exact(db, name, scope):
        return db.execute('''SELECT a.* FROM food_aliases a JOIN food_entities f ON f.food_id=a.food_id
            WHERE a.normalized_form=? AND a.scope=? AND a.normalization_version=?
            AND a.status='ACTIVE' AND f.status='ACTIVE' ''', (text(name), scope, NORMALIZATION_VERSION)).fetchone()

    @staticmethod
    def _candidate_by_key(db, candidate_key):
        row = db.execute('SELECT candidate_id FROM food_candidates WHERE candidate_key=?', (candidate_key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _put_candidate(db, row):
        db.execute('''INSERT INTO food_candidates(candidate_id,candidate_key,source_code,source_item_id,origin,
            surface_form,normalized_form,scope,proposed_kind,proposed_aliases_json,description,proposed_relations_json,
            status,resolution_version,type_gate,contract_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'NEW',?,?,?,?,?)''', tuple(row.values()))

    @staticmethod
    def _set_candidate(db, candidate_id, status, food_id, alias_id, method):
        db.execute('''UPDATE food_candidates SET status=?,resolved_food_id=?,resolution_alias_id=?,resolution_method=?,
            revision=revision+1,updated_at=? WHERE candidate_id=?''', (status, food_id, alias_id, method, utcnow(), candidate_id))

    @staticmethod
    def _put_alias(db, row):
        db.execute('INSERT INTO food_aliases VALUES (?,?,?,?,?,?,?,1,?,?)', tuple(row.values()))

    @staticmethod
    def _put_food(db, row):
        db.execute("INSERT INTO food_entities VALUES (?,?,?,'ACTIVE',1,?,NULL,?,?)", tuple(row.values()))

    @staticmethod
    def _rename(db, food_id, name):
        db.execute('UPDATE food_entities SET canonical_name=?,revision=revision+1,updated_at=? WHERE food_id=?', (name, utcnow(), food_id))

    @staticmethod
    def _resolved_by_alias(db, alias_id):
        return [r[0] for r in db.execute("SELECT candidate_id FROM food_candidates WHERE resolution_alias_id=? AND status='RESOLVED'", (alias_id,))]

    @staticmethod
    def _set_alias(db, alias_id, food_id, status):
        db.execute('UPDATE food_aliases SET food_id=?,status=?,revision=revision+1,updated_at=? WHERE alias_id=?',
                   (food_id, status, utcnow(), alias_id))

    @staticmethod
    def _variant_cycle(db, source, target):
        """Whether target already reaches source through active VARIANT_OF relations."""
        return db.execute('''WITH RECURSIVE ancestors(id) AS (SELECT ? UNION
                SELECT r.to_food_id FROM food_relations r JOIN ancestors a ON r.from_food_id=a.id
                WHERE r.status='ACTIVE' AND r.relation_type='VARIANT_OF') SELECT 1 FROM ancestors WHERE id=?''', (target, source)).fetchone() is not None

    @staticmethod
    def _active_relation(db, source, target, kind):
        row = db.execute("SELECT * FROM food_relations WHERE from_food_id=? AND to_food_id=? AND relation_type=? AND status='ACTIVE'", (source, target, kind)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _put_relation(db, row):
        db.execute("INSERT INTO food_relations VALUES (?,?,?,?,'ACTIVE',1,?,?)", tuple(row.values()))

    @staticmethod
    def _has_relation(db, food_id):
        return db.execute("SELECT 1 FROM food_relations WHERE status='ACTIVE' AND (from_food_id=? OR to_food_id=?)", (food_id, food_id)).fetchone() is not None

    @staticmethod
    def _merge_aliases(db, food_id):
        return [dict(r) for r in db.execute("SELECT * FROM food_aliases WHERE food_id=? AND status='ACTIVE'", (food_id,))]

    @staticmethod
    def _mark_merged(db, food_id, target_id):
        db.execute("UPDATE food_entities SET status='MERGED',merged_into=?,revision=revision+1,updated_at=? WHERE food_id=?", (target_id, utcnow(), food_id))

    # RSS discovery reads for the import bridge. The Oracle runtime reads its own RSS tables instead (oracle_foods).

    def _rss_item(self, db, item_id):
        return self._row(db, 'items', 'id', item_id)

    def _rss_classification(self, db, item):
        return self._row(db, 'candidates', 'item_id', item['id'])

    @staticmethod
    def _rss_prior(db, item):
        """The item's latest observation contract, or None."""
        row = db.execute('SELECT contract_json FROM observations WHERE item_id=? ORDER BY run_id DESC LIMIT 1', (item['id'],)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    @staticmethod
    def _rss_candidate(db, source, item):
        row = db.execute('SELECT * FROM food_candidates WHERE source_code=? AND source_item_id=?', (source, item['id'])).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _retype(db, candidate_id, kind, gate):
        db.execute('UPDATE food_candidates SET proposed_kind=?,type_gate=?,revision=revision+1,updated_at=? WHERE candidate_id=?',
                   (kind, gate, utcnow(), candidate_id))

    def _rss_run_items(self, db, run_id):
        """FOOD/REVIEW item ids observed in a successful run; None for a failed run."""
        if not self._row(db, 'runs', 'id', run_id)['success']:
            return None
        return [r[0] for r in db.execute('''SELECT o.item_id FROM observations o JOIN candidates c ON c.item_id=o.item_id
            WHERE o.run_id=? AND c.classification IN ('FOOD','REVIEW') ORDER BY o.item_id''', (run_id,))]

    # Decisions

    def _row(self, db, table, column, value):
        if type(value) not in (str, int):
            raise FoodError('대상 ID 형식을 확인하세요.')
        row = db.execute(f'SELECT * FROM {table} WHERE {column}=?', (value,)).fetchone()
        if row is None:
            raise FoodError('대상을 찾을 수 없습니다.', 404)
        return dict(row)

    def _active(self, db, food_id):
        row = self._row(db, 'food_entities', 'food_id', food_id)
        if row['status'] != 'ACTIVE':
            raise FoodError('활성 음식만 변경하거나 연결할 수 있습니다.', 409)
        return row

    @staticmethod
    def _revision(row, expected):
        if type(expected) is not int or expected != row['revision']:
            raise FoodError('변경 버전이 다릅니다. 새로 조회한 뒤 다시 검토하세요.', 409)

    def _event(self, db, kind, subject, action, before, after, ctx, reverses=None):
        event_id = identity('EVT')
        self._put_event(db, dict(event_id=event_id, subject_type=kind, subject_id=subject, action=action, actor_ref=ctx['actor_ref'],
                                 reason=ctx['reason'], occurred_at=utcnow(), before_json=dump(before) if before is not None else None,
                                 after_json=dump(after) if after is not None else None, request_key=ctx['request_key'],
                                 reverses_event_id=reverses))
        return event_id

    def _descriptor(self, db, food_id):
        food = self._row(db, 'food_entities', 'food_id', food_id)
        food['entity_status'] = food['status']
        food['resolution_version'] = RESOLUTION_VERSION
        food['approval_status'] = 'APPROVED' if food['status'] == 'ACTIVE' else 'INACTIVE'
        food['tracking_eligible'] = food['status'] == 'ACTIVE'
        food['approved_aliases'] = self._aliases_of(db, food_id, 'ACTIVE')
        food['revoked_aliases'] = self._aliases_of(db, food_id, 'REVOKED')
        food['relations'] = self._relations_of(db, food_id)
        return food

    def food(self, food_id):
        with self._reader() as db:
            return self._descriptor(db, food_id)

    def foods(self):
        with self._reader() as db:
            return [self._descriptor(db, food_id) for food_id in self._food_ids(db)]

    def _candidate(self, db, candidate_id):
        row = self._row(db, 'food_candidates', 'candidate_id', candidate_id)
        for stored, public in (('contract_json', 'contract'), ('proposed_aliases_json', 'proposed_aliases'),
                               ('proposed_relations_json', 'proposed_relations')):
            row[public] = json.loads(row.pop(stored))
        row['resolution'] = self._descriptor(db, row['resolved_food_id']) if row['resolved_food_id'] else None
        return row

    def candidate(self, candidate_id):
        with self._reader() as db:
            return self._candidate(db, candidate_id)

    def candidates(self):
        with self._reader() as db:
            return [self._candidate(db, candidate_id) for candidate_id in self._candidate_ids(db)]

    def events(self, subject_id):
        with self._reader() as db:
            result = []
            for row in self._event_rows(db, subject_id):
                item = dict(row)
                item['before'] = json.loads(item.pop('before_json')) if item['before_json'] is not None else None
                item['after'] = json.loads(item.pop('after_json')) if item['after_json'] is not None else None
                result.append(item)
            return result

    def _scope(self, value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value):
            raise FoodError('별칭 범위를 확인하세요.')
        return value

    def resolve_name(self, name, scope=DEFAULT_SCOPE):
        with self._reader() as db:
            alias = self._exact(db, name, self._scope(scope))
            return {'status': 'RESOLVED' if alias else 'NEEDS_REVIEW',
                    'resolution': self._descriptor(db, alias['food_id']) if alias else None,
                    'resolution_version': RESOLUTION_VERSION}

    def command(self, operation, body):
        """One transaction and durable replay result per request key. All review commands require approval."""
        common = {'request_key'}
        review = {'approved', 'actor_ref', 'reason', 'expected_revision'}
        fields = {
            'submit': {'name', 'aliases', 'entity_kind', 'description', 'relations', 'scope'},
            'import': {'source_code', 'source_item_id'},
            'resolve': {'candidate_id', 'expected_revision'},
            'review': review | {'candidate_id', 'action', 'canonical_name', 'food_id', 'entity_kind', 'aliases',
                                'approve_aliases', 'parent_food_id'},
            'rename': review | {'food_id', 'canonical_name'},
            'alias': review | {'food_id', 'surface_form', 'scope', 'alias_id', 'action'},
            'revert-alias': review | {'event_id'},
            'relate': review | {'from_food_id', 'to_food_id', 'relation_type'},
            'merge': review | {'food_id', 'target_food_id', 'expected_target_revision'},
        }
        if operation not in fields or not isinstance(body, dict) or set(body) - (fields[operation] | common):
            raise FoodError('지원하지 않는 작업 또는 입력 필드입니다.')
        ctx = dict(body)
        key = text(ctx.get('request_key'), 128)
        ctx['request_key'] = key
        if operation not in ('submit', 'import', 'resolve'):
            if ctx.get('approved') is not True:
                raise FoodError('검토자의 명시적 승인이 필요합니다.')
            ctx['actor_ref'] = text(ctx.get('actor_ref'), 100)
            ctx['reason'] = text(ctx.get('reason'), 1000)
        else:
            ctx.update(actor_ref='local-user' if operation == 'submit' else 'resolver', reason='명시적 후보 등록·정확 별칭 확인')
        fingerprint = hashlib.sha256(dump(body).encode()).hexdigest()
        with self._writer() as db:
            prior = self._command(db, key)
            if prior:
                if prior['operation'] != operation or prior['payload_hash'] != fingerprint:
                    raise FoodError('같은 request_key에 다른 요청을 사용할 수 없습니다.', 409)
                return json.loads(prior['result_json'])
            self._put_command(db, key, operation, fingerprint)
            handler = getattr(self, '_do_' + operation.replace('-', '_'))
            result = handler(db, ctx)
            self._command_result(db, key, result)
            return result

    def _names(self, value):
        if not isinstance(value, list) or len(value) > 30:
            raise FoodError('별칭은 30개 이하의 목록이어야 합니다.')
        return list(dict.fromkeys(text(v) for v in value))

    def _relations(self, value):
        if not isinstance(value, list) or len(value) > 20:
            raise FoodError('관계는 20개 이하의 목록이어야 합니다.')
        for rel in value:
            if not isinstance(rel, dict) or set(rel) != {'food_id', 'relation_type'}:
                raise FoodError('관계에는 food_id와 relation_type이 필요합니다.')
            if rel['relation_type'] not in ('VARIANT_OF', 'RELATED_TO'):
                raise FoodError('관계 유형을 확인하세요.')
            text(rel['food_id'], 100)
        return value

    def _submit(self, db, ctx, *, source, item_id, origin, name, kind, aliases, description, relations, scope, contract):
        if not isinstance(kind, str) or kind not in CANDIDATE_KINDS:
            raise FoodError('음식 유형을 확인하세요.')
        material = [source, item_id] if item_id is not None else [source, name, scope]
        candidate_key = hashlib.sha256(dump(material).encode()).hexdigest()
        old = self._candidate_by_key(db, candidate_key)
        if old:
            self._resolve(db, old, ctx)
            return self._candidate(db, old)
        candidate_id, stamp = identity('CAND'), utcnow()
        gate = 'BLOCKED_NON_FOOD' if kind in ('BRAND', 'STORE', 'NON_FOOD') else 'REQUIRES_TYPE_APPROVAL' if kind == 'UNKNOWN' else 'FOOD'
        self._put_candidate(db, dict(candidate_id=candidate_id, candidate_key=candidate_key, source_code=source, source_item_id=item_id,
            origin=origin, surface_form=name, normalized_form=name, scope=scope, proposed_kind=kind, proposed_aliases_json=dump(aliases),
            description=description, proposed_relations_json=dump(relations), resolution_version=RESOLUTION_VERSION, type_gate=gate,
            contract_json=encode(contract), created_at=stamp, updated_at=stamp))
        self._event(db, 'candidate', candidate_id, 'SUBMITTED', None, self._row(db, 'food_candidates', 'candidate_id', candidate_id), ctx)
        self._transition(db, candidate_id, 'NEEDS_REVIEW', ctx)
        self._resolve(db, candidate_id, ctx)
        return self._candidate(db, candidate_id)

    def _do_submit(self, db, ctx):
        require_operation('user_input', 'candidate_resolution')
        name, scope = text(ctx.get('name')), self._scope(ctx.get('scope', DEFAULT_SCOPE))
        aliases = self._names(ctx.get('aliases', []))
        kind = ctx.get('entity_kind', 'PRODUCT')
        if 'description' in ctx and not isinstance(ctx['description'], str):
            raise FoodError('설명은 문자열이어야 합니다.')
        description = text(ctx['description'], 2000) if ctx.get('description') else ''
        relations = self._relations(ctx.get('relations', []))
        contract = envelope('user_input', 'user', utcnow(),
                            fields={k: ctx[k] for k in ('name', 'aliases', 'entity_kind', 'description', 'relations') if k in ctx})
        return self._submit(db, ctx, source='user_input', item_id=None, origin='user', name=name, kind=kind,
                            aliases=aliases, description=description, relations=relations, scope=scope, contract=contract)

    def _do_import(self, db, ctx):
        source = ctx.get('source_code')
        if source != 'google_trends_rss':
            try:
                require_operation(source, 'food_extraction')
            except (KeyError, ValueError):
                raise FoodError('이 출처의 음식 추출은 승인되지 않았습니다.', 403) from None
            raise FoodError('이 출처의 어댑터는 제공하지 않습니다.', 403)
        require_operation(source, 'existing_food_rules')
        if type(ctx.get('source_item_id')) is not int:
            raise FoodError('기존 RSS 항목 ID가 필요합니다.')
        item = self._rss_item(db, ctx['source_item_id'])
        classification = self._rss_classification(db, item)
        prior = self._rss_prior(db, item)
        origin = prior['origin'] if prior else 'legacy_unknown'
        contract = envelope(source, origin, utcnow(), published_at=item['published_at'],
                            fields={'keyword': item['keyword']}, request={'geo': 'KR', 'endpoint': 'trending/rss'},
                            observation={'source_item_id': item['id'], 'classification_version': classification['classifier_version'],
                                         'resolution_input': True, 'source_collected_at': prior['collected_at'] if prior else None})
        kind = candidate_kind(classification['entity_type'], classification['classification'])
        existing = self._rss_candidate(db, source, item)
        if existing and existing['proposed_kind'] != kind and existing['status'] != 'REJECTED':
            before = dict(existing)
            if existing['status'] == 'RESOLVED':
                self._transition(db, existing['candidate_id'], 'NEEDS_REVIEW', ctx)
            gate = 'BLOCKED_NON_FOOD' if kind in ('BRAND','STORE','NON_FOOD') else 'REQUIRES_TYPE_APPROVAL' if kind == 'UNKNOWN' else 'FOOD'
            self._retype(db, existing['candidate_id'], kind, gate)
            after = self._row(db, 'food_candidates', 'candidate_id', existing['candidate_id'])
            self._event(db, 'candidate', existing['candidate_id'], 'SOURCE_TYPE_CHANGED', before,
                        dict(after, source_classifier_version=classification['classifier_version']), ctx)
        return self._submit(db, ctx, source=source, item_id=item['id'], origin=origin, name=text(item['keyword']),
                            kind=kind,
                            aliases=[], description='', relations=[], scope=DEFAULT_SCOPE, contract=contract)

    def import_rss_run(self, run_id):
        """Local post-collection bridge. Each item is replayable; never creates foods or tracking targets."""
        require_operation('google_trends_rss', 'existing_food_rules')
        with self._reader() as db:
            ids = self._rss_run_items(db, run_id)
        if ids is None:
            return {'state': 'skipped_failed_run', 'candidates': 0}
        for item_id in ids:
            self.command('import', {'request_key': f'rss-resolution:{run_id}:{item_id}',
                                   'source_code': 'google_trends_rss', 'source_item_id': item_id})
        return {'state': 'completed', 'candidates': len(ids)}

    def _transition(self, db, candidate_id, status, ctx, food_id=None, alias_id=None, method=None):
        before = self._row(db, 'food_candidates', 'candidate_id', candidate_id)
        self._set_candidate(db, candidate_id, status, food_id, alias_id, method)
        self._event(db, 'candidate', candidate_id, status, before, self._row(db, 'food_candidates', 'candidate_id', candidate_id), ctx)

    def _resolve(self, db, candidate_id, ctx):
        row = self._row(db, 'food_candidates', 'candidate_id', candidate_id)
        if row['status'] != 'NEEDS_REVIEW' or row['proposed_kind'] not in FOOD_KINDS:
            return
        alias = self._exact(db, row['normalized_form'], row['scope'])
        if alias:
            self._transition(db, candidate_id, 'RESOLVED', ctx, alias['food_id'], alias['alias_id'], 'APPROVED_EXACT_ALIAS')

    def _do_resolve(self, db, ctx):
        row = self._row(db, 'food_candidates', 'candidate_id', ctx.get('candidate_id'))
        self._revision(row, ctx.get('expected_revision'))
        self._resolve(db, row['candidate_id'], ctx)
        return self._candidate(db, row['candidate_id'])

    def _approve_alias(self, db, food_id, surface, scope, ctx):
        self._active(db, food_id)
        surface, scope = text(surface), self._scope(scope)
        old = self._exact(db, surface, scope)
        if old:
            if old['food_id'] != food_id:
                raise FoodError('이미 다른 음식에 승인된 별칭입니다. 모호한 명칭은 별도 scope로 검토하세요.', 409)
            return dict(old)
        stamp, alias_id = utcnow(), identity('ALIAS')
        self._put_alias(db, dict(alias_id=alias_id, food_id=food_id, surface_form=surface, normalized_form=surface, scope=scope,
                                 normalization_version=NORMALIZATION_VERSION, status='ACTIVE', created_at=stamp, updated_at=stamp))
        self._touch(db, food_id)
        after = self._row(db, 'food_aliases', 'alias_id', alias_id)
        self._event(db, 'alias', alias_id, 'APPROVED', None, after, ctx)
        return after

    def _do_review(self, db, ctx):
        candidate = self._row(db, 'food_candidates', 'candidate_id', ctx.get('candidate_id'))
        self._revision(candidate, ctx.get('expected_revision'))
        if candidate['status'] != 'NEEDS_REVIEW':
            raise FoodError('검토 대기 후보만 승인·거절할 수 있습니다.', 409)
        action = ctx.get('action')
        if action == 'reject':
            self._transition(db, candidate['candidate_id'], 'REJECTED', ctx)
            return self._candidate(db, candidate['candidate_id'])
        if candidate['type_gate'] == 'BLOCKED_NON_FOOD':
            raise FoodError('브랜드·매장·비음식은 음식으로 승격할 수 없습니다. 특정 상품을 별도 등록하세요.', 409)
        kind = ctx.get('entity_kind', candidate['proposed_kind'])
        if not isinstance(kind, str) or kind not in FOOD_KINDS:
            raise FoodError('검토자가 음식 유형을 명시해야 합니다.')
        if action in ('create', 'create_variant'):
            food_id, stamp = identity('FOOD'), utcnow()
            name = text(ctx.get('canonical_name', candidate['surface_form']))
            self._put_food(db, dict(food_id=food_id, canonical_name=name, entity_kind=kind, default_scope=candidate['scope'],
                                    created_at=stamp, updated_at=stamp))
            self._event(db, 'food', food_id, 'CREATED', None, self._row(db, 'food_entities', 'food_id', food_id), ctx)
            names = [name, candidate['surface_form']] + json.loads(candidate['proposed_aliases_json']) + self._names(ctx.get('aliases', []))
            for alias in dict.fromkeys(names):
                self._approve_alias(db, food_id, alias, candidate['scope'], ctx)
            if action == 'create_variant':
                self._relate(db, food_id, ctx.get('parent_food_id'), 'VARIANT_OF', ctx)
            for relation in json.loads(candidate['proposed_relations_json']):
                self._relate(db, food_id, relation['food_id'], relation['relation_type'], ctx)
        elif action == 'link':
            food_id = self._active(db, ctx.get('food_id'))['food_id']
            if ctx.get('approve_aliases', False) is not False and ctx.get('approve_aliases') is not True:
                raise FoodError('별칭 승인 여부는 참 또는 거짓이어야 합니다.')
            if ctx.get('approve_aliases') is True:
                for alias in [candidate['surface_form']] + json.loads(candidate['proposed_aliases_json']) + self._names(ctx.get('aliases', [])):
                    self._approve_alias(db, food_id, alias, candidate['scope'], ctx)
        else:
            raise FoodError('검토 작업을 확인하세요.')
        self._transition(db, candidate['candidate_id'], 'RESOLVED', ctx, food_id, method='HUMAN_APPROVAL')
        return self._candidate(db, candidate['candidate_id'])

    def _do_rename(self, db, ctx):
        before = self._active(db, ctx.get('food_id'))
        self._revision(before, ctx.get('expected_revision'))
        name = text(ctx.get('canonical_name'))
        self._approve_alias(db, before['food_id'], name, before['default_scope'], ctx)
        self._rename(db, before['food_id'], name)
        self._event(db, 'food', before['food_id'], 'RENAMED', before, self._row(db, 'food_entities', 'food_id', before['food_id']), ctx)
        return self._descriptor(db, before['food_id'])

    def _invalidate(self, db, alias_id, ctx):
        for candidate_id in self._resolved_by_alias(db, alias_id):
            self._transition(db, candidate_id, 'NEEDS_REVIEW', ctx)

    def _change_alias(self, db, before, food_id, status, ctx, reverses=None):
        self._active(db, food_id)
        self._set_alias(db, before['alias_id'], food_id, status)
        if before['status'] != status or before['food_id'] != food_id:
            self._invalidate(db, before['alias_id'], ctx)
        for affected in set((food_id, before['food_id'])):
            self._touch(db, affected)
        after = self._row(db, 'food_aliases', 'alias_id', before['alias_id'])
        self._event(db, 'alias', before['alias_id'], 'REVERTED' if reverses else 'CHANGED', before, after, ctx, reverses)
        return after

    def _do_alias(self, db, ctx):
        if ctx.get('action') == 'approve':
            food = self._active(db, ctx.get('food_id'))
            self._revision(food, ctx.get('expected_revision'))
            return self._approve_alias(db, food['food_id'], ctx.get('surface_form'), ctx.get('scope', food['default_scope']), ctx)
        before = self._row(db, 'food_aliases', 'alias_id', ctx.get('alias_id'))
        self._revision(before, ctx.get('expected_revision'))
        if ctx.get('action') not in ('revoke', 'restore', 'reassign'):
            raise FoodError('별칭 작업을 확인하세요.')
        return self._change_alias(db, before, ctx.get('food_id', before['food_id']),
                                  'REVOKED' if ctx['action'] == 'revoke' else 'ACTIVE', ctx)

    def _do_revert_alias(self, db, ctx):
        event = self._row(db, 'food_events', 'event_id', ctx.get('event_id'))
        if event['subject_type'] != 'alias' or event['after_json'] is None:
            raise FoodError('복구할 별칭 이력을 선택하세요.')
        before = self._row(db, 'food_aliases', 'alias_id', event['subject_id'])
        self._revision(before, ctx.get('expected_revision'))
        if before['revision'] != json.loads(event['after_json'])['revision']:
            raise FoodError('이후 변경이 있어 이 이력을 바로 되돌릴 수 없습니다.', 409)
        old = json.loads(event['before_json']) if event['before_json'] else None
        return self._change_alias(db, before, old['food_id'] if old else before['food_id'],
                                  old['status'] if old else 'REVOKED', ctx, event['event_id'])

    def _relate(self, db, source, target, kind, ctx):
        self._active(db, source); self._active(db, target)
        if source == target or kind not in ('VARIANT_OF', 'RELATED_TO'):
            raise FoodError('자기 참조 또는 잘못된 관계 유형입니다.', 409)
        if kind == 'RELATED_TO':
            source, target = sorted((source, target))
        elif self._variant_cycle(db, source, target):
            raise FoodError('파생 음식 관계에 순환을 만들 수 없습니다.', 409)
        prior = self._active_relation(db, source, target, kind)
        if prior:
            return prior
        stamp, relation_id = utcnow(), identity('REL')
        self._put_relation(db, dict(relation_id=relation_id, from_food_id=source, to_food_id=target, relation_type=kind,
                                    created_at=stamp, updated_at=stamp))
        self._touch(db, source); self._touch(db, target)
        after = self._row(db, 'food_relations', 'relation_id', relation_id)
        self._event(db, 'relation', relation_id, 'APPROVED', None, after, ctx)
        return after

    def _do_relate(self, db, ctx):
        source = self._active(db, ctx.get('from_food_id'))
        self._revision(source, ctx.get('expected_revision'))
        return self._relate(db, source['food_id'], ctx.get('to_food_id'), ctx.get('relation_type'), ctx)

    def _do_merge(self, db, ctx):
        source, target = self._active(db, ctx.get('food_id')), self._active(db, ctx.get('target_food_id'))
        self._revision(source, ctx.get('expected_revision'))
        self._revision(target, ctx.get('expected_target_revision'))
        if source['food_id'] == target['food_id']:
            raise FoodError('같은 음식으로 병합할 수 없습니다.', 409)
        if self._has_relation(db, source['food_id']):
            raise FoodError('관계가 있는 음식의 병합은 지원하지 않습니다. 관계를 먼저 검토하세요.', 409)
        for alias in self._merge_aliases(db, source['food_id']):
            self._change_alias(db, alias, target['food_id'], 'ACTIVE', ctx)
        # Keep historical resolved IDs and make their redirect explicit; no destructive rewrites.
        self._mark_merged(db, source['food_id'], target['food_id'])
        self._event(db, 'food', source['food_id'], 'MERGED', source, self._row(db, 'food_entities', 'food_id', source['food_id']), ctx)
        return {'source': self._descriptor(db, source['food_id']), 'target': self._descriptor(db, target['food_id'])}
