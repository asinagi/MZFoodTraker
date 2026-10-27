"""A4-A5 behavior at the approval, transaction, policy and HTTP boundaries."""
import json
import re
import sqlite3
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from food_trends.api import make_server
from food_trends.config import InstagramConfig, NaverConfig, YouTubeConfig
from food_trends.contracts import require_operation, policy_for
from food_trends.food_service import FoodService, FoodError
from food_trends.instagram_graph import GraphError
from food_trends.instagram_service import InstagramService
from food_trends.migrations import migrate, schema_version, MIGRATIONS, LATEST_VERSION
from food_trends.naver_service import NaverService
from food_trends.service import CollectionService
from food_trends.storage import Storage
from food_trends.youtube_storage import YouTubeStore
from food_trends.migration_definitions import BASE_SQL
from test_mvp import fixture


def key():
    return uuid.uuid4().hex


def original_rows(storage):
    original_tables = set(re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)', BASE_SQL))
    with storage.reader() as db:
        return {r[0]: [tuple(row) for row in db.execute('SELECT * FROM "' + r[0] + '" ORDER BY rowid')]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] in original_tables}


def snapshot(storage):
    with storage.reader() as db:
        return list(db.iterdump())


class FoodEntityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage.create(Path(self.temp.name) / 'food.sqlite3')
        self.foods = FoodService(self.storage)
        self.rss = CollectionService(self.storage, fetcher=lambda _: (fixture('쿠키'), 200))

    def tearDown(self):
        self.temp.cleanup()

    def cmd(self, operation, **data):
        data.setdefault('request_key', key())
        if operation not in ('submit', 'import', 'resolve'):
            data.setdefault('approved', True)
            data.setdefault('actor_ref', 'fixture-reviewer')
            data.setdefault('reason', '사람이 음식 및 명칭 근거를 확인함')
        return self.foods.command(operation, data)

    def submit(self, name='두쫀쿠', **data):
        return self.cmd('submit', name=name, **data)

    def approve(self, candidate, **data):
        return self.cmd('review', candidate_id=candidate['candidate_id'], expected_revision=candidate['revision'],
                        action=data.pop('action', 'create'), **data)

    def food(self, name='두쫀쿠', **data):
        return self.approve(self.submit(name, **data))['resolution']

    def alias(self, food, name, **data):
        return self.cmd('alias', action='approve', food_id=food['food_id'], surface_form=name,
                        expected_revision=self.foods.food(food['food_id'])['revision'], **data)

    def test_zero_rss_food_manual_registration_and_required_descriptor(self):
        self.assertEqual(self.storage.status()['food_count'], 0)
        before = original_rows(self.storage)
        c = self.submit(aliases=['두바이 쫀득 쿠키', '두바이쫀득쿠키'])
        self.assertEqual(c['status'], 'NEEDS_REVIEW')
        self.assertEqual(c['contract']['origin'], 'user')
        self.assertEqual(c['contract']['field_states']['description'], 'not_provided')
        f = self.approve(c, canonical_name='두바이 쫀득 쿠키')['resolution']
        self.assertEqual(len(f['approved_aliases']), 3)
        for name in ('두쫀쿠', '두바이 쫀득 쿠키', '두바이쫀득쿠키'):
            result = self.foods.resolve_name(name)
            self.assertEqual(result['resolution']['food_id'], f['food_id'])
        for field in ('food_id', 'canonical_name', 'approved_aliases', 'entity_kind', 'entity_status', 'resolution_version', 'approval_status'):
            self.assertIn(field, f)
        self.assertTrue(f['tracking_eligible'])
        self.assertEqual(original_rows(self.storage), before)

    def test_proposed_alias_fuzzy_spacing_and_variant_are_not_automatic(self):
        self.submit('첫 후보', aliases=['아직 미승인'])
        self.assertEqual(self.submit('아직 미승인')['status'], 'NEEDS_REVIEW')
        self.food('두쫀쿠', aliases=['두바이 쫀득 쿠키'])
        for name in ('짭쫀쿠', '두바이쫀득쿠키', '두 쫀 쿠', '두쫀쿠 신상'):
            self.assertEqual(self.submit(name)['status'], 'NEEDS_REVIEW')
            self.assertEqual(self.foods.resolve_name(name)['status'], 'NEEDS_REVIEW')
        self.assertEqual(self.submit('두바이 쫀득 쿠키')['status'], 'RESOLVED')

    def test_nfc_whitespace_normalization_does_not_remove_spaces_or_case(self):
        f = self.food('Cafe Cookie')
        self.assertEqual(self.foods.resolve_name('  Cafe   Cookie  ')['resolution']['food_id'], f['food_id'])
        self.assertIsNone(self.foods.resolve_name('CafeCookie')['resolution'])
        self.assertIsNone(self.foods.resolve_name('cafe cookie')['resolution'])

    def test_brand_store_gate_and_separate_brand_product(self):
        self.food('브랜드명')
        for kind in ('BRAND', 'STORE', 'NON_FOOD'):
            candidate = self.submit(kind + ' 브랜드', entity_kind=kind)
            with self.assertRaises(FoodError):
                self.approve(candidate, entity_kind='PRODUCT')
            self.assertEqual(self.foods.candidate(candidate['candidate_id'])['status'], 'NEEDS_REVIEW')
        blocked = self.submit('브랜드명', scope='food:brand', entity_kind='BRAND')
        self.assertNotEqual(blocked['status'], 'RESOLVED')
        product = self.food('브랜드명 초코 쿠키', entity_kind='PRODUCT')
        self.assertEqual(product['entity_kind'], 'PRODUCT')

    def test_repeated_name_and_request_idempotency_and_conflicting_replay(self):
        request_key = key()
        c = self.submit(request_key=request_key)
        self.assertEqual(self.submit(request_key=request_key), c)
        self.assertEqual(self.submit(entity_kind='DISH')['candidate_id'], c['candidate_id'])
        review_key = key()
        approved = self.approve(c, request_key=review_key)
        self.assertEqual(self.approve(c, request_key=review_key), approved)
        repeated = self.submit()
        self.assertEqual(repeated['resolved_food_id'], approved['resolved_food_id'])
        self.assertEqual(len(self.foods.candidates()), 1)
        self.assertEqual(len(self.foods.foods()), 1)
        with self.assertRaises(FoodError) as error:
            self.submit('다른 음식', request_key=request_key)
        self.assertEqual(error.exception.status, 409)

    def test_concurrent_same_name_registration_and_same_request_replay(self):
        request_key = key()
        with ThreadPoolExecutor(max_workers=4) as workers:
            candidates = list(workers.map(lambda _: self.submit(request_key=request_key), range(4)))
        self.assertEqual(len({c['candidate_id'] for c in candidates}), 1)
        self.assertEqual(len(self.foods.candidates()), 1)

    def test_approval_requires_human_reason_and_optimistic_revision(self):
        c = self.submit()
        for overrides in ({'approved': False}, {'reason': ''}, {'actor_ref': ''}, {'expected_revision': 0}):
            body = dict(candidate_id=c['candidate_id'], action='create', expected_revision=c['revision'],
                        approved=True, reason='fixture', actor_ref='reviewer', request_key=key())
            body.update(overrides)
            with self.assertRaises(FoodError):
                self.foods.command('review', body)
        self.assertEqual(self.foods.foods(), [])

    def test_approval_and_rejection_events_are_append_only(self):
        approved = self.approve(self.submit())
        rejected = self.approve(self.submit('비음식'), action='reject')
        self.assertEqual(rejected['status'], 'REJECTED')
        for candidate in (approved, rejected):
            history = self.foods.events(candidate['candidate_id'])
            self.assertEqual([h['action'] for h in history], ['SUBMITTED', 'NEEDS_REVIEW', candidate['status']])
            self.assertEqual(history[-1]['actor_ref'], 'fixture-reviewer')
            self.assertTrue(history[-1]['reason'])
        for sql in ('DELETE FROM food_events', "UPDATE food_events SET reason='changed'"):
            with self.assertRaises(sqlite3.IntegrityError), self.storage.connect() as db:
                db.execute(sql)

    def test_audit_append_order_is_stable_when_timestamps_tie(self):
        with patch('food_trends.food_service.utcnow', return_value='2026-10-27T00:00:00+00:00'):
            c = self.approve(self.submit())
        history = self.foods.events(c['candidate_id'])
        self.assertEqual([h['action'] for h in history], ['SUBMITTED', 'NEEDS_REVIEW', 'RESOLVED'])
        self.assertEqual([h['event_sequence'] for h in history], sorted(h['event_sequence'] for h in history))

    def test_rename_preserves_id_and_old_alias_and_audit(self):
        old = self.food()
        renamed = self.cmd('rename', food_id=old['food_id'], expected_revision=old['revision'], canonical_name='두바이 쫀득 쿠키')
        self.assertEqual(renamed['food_id'], old['food_id'])
        self.assertEqual(self.foods.resolve_name('두쫀쿠')['resolution']['food_id'], old['food_id'])
        event = self.foods.events(old['food_id'])[-1]
        self.assertEqual(event['before']['canonical_name'], '두쫀쿠')
        self.assertEqual(event['after']['canonical_name'], '두바이 쫀득 쿠키')

    def test_homonyms_need_explicit_scopes_canonical_names_can_duplicate(self):
        first = self.food('밤', scope='food:chestnut')
        second = self.food('밤', scope='food:dessert')
        self.assertNotEqual(first['food_id'], second['food_id'])
        self.assertIsNone(self.foods.resolve_name('밤')['resolution'])
        self.assertEqual(self.foods.resolve_name('밤', 'food:chestnut')['resolution']['food_id'], first['food_id'])
        before = snapshot(self.storage)
        with self.assertRaises(FoodError):
            self.alias(first, '밤', scope='food:dessert')
        self.assertEqual(snapshot(self.storage), before)

    def test_alias_correction_invalidates_automatic_links_and_can_be_reverted(self):
        first, second = self.food(), self.food('케이크')
        alias = self.alias(first, '잘못된 별칭')
        c = self.submit('잘못된 별칭')
        self.assertEqual(c['resolution_method'], 'APPROVED_EXACT_ALIAS')
        changed = self.cmd('alias', alias_id=alias['alias_id'], action='reassign', food_id=second['food_id'], expected_revision=alias['revision'])
        self.assertEqual(self.foods.candidate(c['candidate_id'])['status'], 'NEEDS_REVIEW')
        self.assertEqual(self.foods.resolve_name('잘못된 별칭')['resolution']['food_id'], second['food_id'])
        event = self.foods.events(alias['alias_id'])[-1]
        restored = self.cmd('revert-alias', event_id=event['event_id'], expected_revision=changed['revision'])
        self.assertEqual(restored['food_id'], first['food_id'])
        self.assertEqual(self.foods.events(alias['alias_id'])[-1]['reverses_event_id'], event['event_id'])
        pending = self.foods.candidate(c['candidate_id'])
        resolved = self.cmd('resolve', candidate_id=c['candidate_id'], expected_revision=pending['revision'])
        self.assertEqual(resolved['resolved_food_id'], first['food_id'])

    def test_revoke_restore_and_stale_revert_cannot_overwrite_later_approvals(self):
        f = self.food()
        alias = self.alias(f, '복구할 별칭')
        event = self.foods.events(alias['alias_id'])[-1]
        revoked = self.cmd('alias', alias_id=alias['alias_id'], action='revoke', expected_revision=alias['revision'])
        self.assertIsNone(self.foods.resolve_name('복구할 별칭')['resolution'])
        with self.assertRaises(FoodError):
            self.cmd('revert-alias', event_id=event['event_id'], expected_revision=revoked['revision'])
        restored = self.cmd('alias', alias_id=alias['alias_id'], action='restore', expected_revision=revoked['revision'])
        self.assertEqual(restored['alias_id'], alias['alias_id'])
        self.assertEqual(self.foods.resolve_name('복구할 별칭')['resolution']['food_id'], f['food_id'])

    def test_human_link_does_not_implicitly_approve_proposed_aliases(self):
        f = self.food()
        c = self.submit('동일 음식 별명', aliases=['또 다른 별명'])
        result = self.approve(c, action='link', food_id=f['food_id'])
        self.assertEqual(result['resolved_food_id'], f['food_id'])
        self.assertIsNone(self.foods.resolve_name('동일 음식 별명')['resolution'])
        self.assertIsNone(self.foods.resolve_name('또 다른 별명')['resolution'])

    def test_variant_is_distinct_and_cycles_self_edges_and_duplicates_are_guarded(self):
        parent = self.food()
        child = self.approve(self.submit('짭쫀쿠'), action='create_variant', parent_food_id=parent['food_id'])['resolution']
        self.assertNotEqual(child['food_id'], parent['food_id'])
        self.assertEqual(self.foods.resolve_name('짭쫀쿠')['resolution']['food_id'], child['food_id'])
        def relate(source, target, kind='VARIANT_OF'):
            return self.cmd('relate', from_food_id=source['food_id'], to_food_id=target['food_id'], relation_type=kind,
                            expected_revision=self.foods.food(source['food_id'])['revision'])
        for target in (parent, child):
            with self.assertRaises(FoodError):
                relate(parent, target)
        same = relate(child, parent)
        self.assertEqual(same['relation_id'], child['relations'][0]['relation_id'])
        related = relate(child, parent, 'RELATED_TO')
        self.assertEqual(relate(parent, child, 'RELATED_TO')['relation_id'], related['relation_id'])
        with self.assertRaises(sqlite3.IntegrityError), self.storage.connect() as db:
            db.execute("INSERT INTO food_relations VALUES ('bad',?,?,'VARIANT_OF','ACTIVE',1,'now','now')", (parent['food_id'], child['food_id']))

    def test_human_merge_preserves_ids_and_audit_and_blocks_related_foods(self):
        first, second = self.food(), self.food('두바이 쿠키')
        before = original_rows(self.storage)
        result = self.cmd('merge', food_id=first['food_id'], target_food_id=second['food_id'],
                          expected_revision=first['revision'], expected_target_revision=second['revision'])
        self.assertEqual(result['source']['entity_status'], 'MERGED')
        self.assertFalse(result['source']['tracking_eligible'])
        self.assertEqual(result['source']['merged_into'], second['food_id'])
        self.assertEqual(self.foods.resolve_name('두쫀쿠')['resolution']['food_id'], second['food_id'])
        self.assertEqual(self.foods.events(first['food_id'])[-1]['action'], 'MERGED')
        self.assertEqual(original_rows(self.storage), before)
        third = self.food('새로운 쿠키')
        self.cmd('relate', from_food_id=third['food_id'], to_food_id=second['food_id'], relation_type='RELATED_TO', expected_revision=third['revision'])
        with self.assertRaises(FoodError):
            self.cmd('merge', food_id=third['food_id'], target_food_id=second['food_id'],
                     expected_revision=self.foods.food(third['food_id'])['revision'], expected_target_revision=self.foods.food(second['food_id'])['revision'])

    def test_explicit_rss_import_reuses_common_resolution_and_preserves_origin_history(self):
        self.rss.collect()
        f = self.food('쿠키')
        before = original_rows(self.storage)
        c = self.cmd('import', source_code='google_trends_rss', source_item_id=1)
        self.assertEqual(c['origin'], 'mock')
        self.assertEqual(c['resolution']['food_id'], f['food_id'])
        self.assertEqual(self.cmd('import', source_code='google_trends_rss', source_item_id=1)['candidate_id'], c['candidate_id'])
        self.assertEqual(original_rows(self.storage), before)

    def test_review_alias_collision_rolls_back_food_alias_events_and_command(self):
        self.food('이미 승인된 음식')
        c = self.submit('새 이름', aliases=['이미 승인된 음식'])
        before = snapshot(self.storage)
        with self.assertRaises(FoodError):
            self.approve(c)
        self.assertEqual(snapshot(self.storage), before)
        self.assertEqual(self.foods.candidate(c['candidate_id'])['status'], 'NEEDS_REVIEW')

    def test_unknown_detected_type_requires_human_type_confirmation(self):
        food = self.food()
        self.alias(food, '미확인 유형 명칭')
        c = self.submit('미확인 유형 명칭', entity_kind='UNKNOWN')
        self.assertEqual(c['status'], 'NEEDS_REVIEW')
        with self.assertRaises(FoodError):
            self.approve(c, action='link', food_id=food['food_id'])
        approved = self.approve(c, action='link', food_id=food['food_id'], entity_kind='PRODUCT')
        self.assertEqual(approved['resolution']['entity_kind'], 'PRODUCT')

    def test_unknown_legacy_origin_is_never_promoted(self):
        self.rss.collect()
        with self.storage.connect() as db:
            db.execute('UPDATE observations SET contract_json=NULL')
        c = self.cmd('import', source_code='google_trends_rss', source_item_id=1)
        self.assertEqual(c['origin'], 'legacy_unknown')
        self.assertEqual(c['contract']['origin'], 'legacy_unknown')

    def test_existing_brand_classification_is_preserved_and_legacy_targets_are_filtered(self):
        self.rss.collect()
        with self.storage.connect() as db:
            db.execute("UPDATE candidates SET entity_type='FOOD_BRAND',classification='FOOD'")
        before = original_rows(self.storage)
        c = self.cmd('import', source_code='google_trends_rss', source_item_id=1)
        with self.assertRaises(FoodError):
            self.approve(c, entity_kind='PRODUCT')
        naver = NaverService(self.storage, NaverConfig())
        self.assertEqual(naver.targets(), [])
        instagram = InstagramService(self.storage, InstagramConfig(token='fixture-token'))
        with self.assertRaises(GraphError):
            instagram.start_tracking('쿠키', 1)
        self.assertEqual(original_rows(self.storage), before)
        naver.sync_targets()
        queries = YouTubeStore(self.storage).sync_queries(YouTubeConfig())
        self.assertFalse([q for q in queries if q['mode'] == 'tracking'])

    def test_forbidden_extraction_remains_disabled_and_provider_policy_unchanged(self):
        before = snapshot(self.storage)
        for source in ('youtube', 'instagram_graph', 'naver_datalab'):
            with self.assertRaises(FoodError) as error:
                self.cmd('import', source_code=source, source_item_id=1)
            self.assertEqual(error.exception.status, 403)
            with self.assertRaises(ValueError):
                require_operation(source, 'food_extraction')
        self.assertEqual(snapshot(self.storage), before)
        self.assertEqual(policy_for('youtube')['retention_days'], 29)
        self.assertEqual(policy_for('naver_datalab')['retention_days'], 0)
        self.assertFalse(policy_for('instagram_graph')['expand_retention_allowed'])

    def test_all_thirty_original_tables_unchanged_by_registration_and_review(self):
        self.rss.collect()
        before = original_rows(self.storage)
        self.assertEqual(len(before), 30)
        c = self.submit(aliases=['두바이 쫀득 쿠키'], description='사람이 입력한 설명')
        f = self.approve(c)['resolution']
        self.cmd('rename', food_id=f['food_id'], canonical_name='두바이 쫀득 쿠키', expected_revision=f['revision'])
        self.assertEqual(original_rows(self.storage), before)

    def test_v2_migration_preserves_original_columns_and_is_idempotent(self):
        path = Path(self.temp.name) / 'v2.sqlite3'
        with patch('food_trends.migrations.LATEST_VERSION', 2), patch('food_trends.migrations.MIGRATIONS', MIGRATIONS[:2]):
            migrate(path)
        with closing(sqlite3.connect(path)) as db:
            before = list(db.iterdump())
        self.assertEqual(migrate(path)['applied'], list(range(3, LATEST_VERSION + 1)))
        upgraded = Storage(path)
        self.assertEqual(schema_version(path), LATEST_VERSION)
        self.assertEqual(len(original_rows(upgraded)), 30)
        with upgraded.reader() as db:
            for statement in before:
                if statement.startswith('CREATE TABLE') and not statement.startswith('CREATE TABLE schema_migrations'):
                    self.assertIn(statement, list(db.iterdump()))
        after = snapshot(upgraded)
        self.assertEqual(migrate(path)['applied'], [])
        self.assertEqual(snapshot(upgraded), after)

    def start_server(self):
        server = make_server(self.rss, port=0, foods=self.foods)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        return f'http://127.0.0.1:{server.server_port}'

    def test_get_routes_and_ui_do_not_open_write_connections_or_create_candidates(self):
        base = self.start_server()
        f = self.food()
        c = self.submit('검토 후보')
        before = snapshot(self.storage)
        connect = self.storage.connect
        def reader_only(*args, **kwargs):
            if not kwargs.get('readonly'):
                raise AssertionError('GET attempted writable connection')
            return connect(*args, **kwargs)
        routes = ['/api/foods', '/api/food-candidates', '/api/foods/' + f['food_id'],
                  '/api/food-candidates/' + c['candidate_id'], '/api/food-events?subject_id=' + c['candidate_id'],
                  '/api/foods/resolve?' + urlencode({'name': '없는 음식'}), '/', '/foods.js', '/style.css']
        with patch.object(self.storage, 'connect', side_effect=reader_only):
            for route in routes:
                with urlopen(base + route) as response:
                    self.assertEqual(response.status, 200)
        self.assertEqual(snapshot(self.storage), before)

    def test_http_registration_review_and_exact_lookup_end_to_end(self):
        base = self.start_server()
        def post(operation, body):
            with urlopen(Request(base + '/api/foods/' + operation, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})) as response:
                return json.load(response)
        c = post('submit', {'name': '두쫀쿠', 'aliases': ['두바이 쫀득 쿠키'], 'request_key': key()})
        result = post('review', {'candidate_id': c['candidate_id'], 'expected_revision': c['revision'], 'action': 'create',
                                'canonical_name': '두바이 쫀득 쿠키', 'approved': True, 'actor_ref': 'human', 'reason': '검증한 음식', 'request_key': key()})
        with urlopen(base + '/api/foods/resolve?' + urlencode({'name': '두쫀쿠'})) as response:
            self.assertEqual(json.load(response)['resolution']['food_id'], result['resolved_food_id'])
        self.assertEqual(self.storage.status()['total_keywords'], 0)

    def test_http_origin_content_type_unknown_fields_and_approval_validation(self):
        base = self.start_server()
        cases = [({'Origin': 'https://other.example'}, {'request_key': key(), 'name': '쿠키'}, 403),
                 ({'Host': 'attacker.example'}, {'request_key': key(), 'name': '쿠키'}, 403),
                 ({'Content-Type': 'text/plain'}, {'request_key': key(), 'name': '쿠키'}, 415),
                 ({}, {'request_key': key(), 'name': '쿠키', 'api_key': 'fixture'}, 400),
                 ({}, {'request_key': key(), 'name': '쿠키', 'description': False}, 400),
                 ({}, {'request_key': key(), 'name': '쿠키', 'entity_kind': []}, 400)]
        for headers, body, status in cases:
            request = Request(base + '/api/foods/submit', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', **headers})
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, status)
        self.assertEqual(self.foods.candidates(), [])
