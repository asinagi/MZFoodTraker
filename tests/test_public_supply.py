"""식품안전나라 ↔ FOOD_ID 연결과 앱 API(/foods/{food_id}/public-supply) 오프라인 테스트.

외부 접속 없음: 식품안전나라는 test_foodsafety.FakeAPI로, 음식 목록은 Mock 데이터셋(실제 B4·B5 서비스로 만든 SQLite)으로 대신한다.
"""
import json
import tempfile
import unittest
from pathlib import Path

from food_trends import app_contract as contract
from food_trends import app_mock, foodsafety
from food_trends import public_supply as ps
from food_trends.analytics_reader import SQLiteAnalyticsStore
from food_trends.analytics_service import FoodAnalytics
from food_trends.app_api import AppApi

from test_foodsafety import DUJJON_MATERIALS, DUJJON_PRODUCTS, FakeAPI

HOST = {'Host': '127.0.0.1:8020'}
REF = {'food_id': 'FOOD_X', 'canonical_name': '두쫀쿠', 'entity_kind': 'PRODUCT', 'aliases': ['두쫀쿠', '두바이쫀득쿠키']}


def dujjon_report():
    return foodsafety.build_report('두쫀쿠', ['두바이쫀득쿠키'], DUJJON_PRODUCTS, DUJJON_MATERIALS, fetched_at='2026-11-10T01:00:00Z')


class FakeAnalytics:
    def __init__(self, foods):
        self._foods = foods

    def foods(self, q=None, kind=None, sort='name'):
        return [dict(f) for f in self._foods]


class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_keeps_supply_and_ingredients_in_api_shape(self):
        record = ps.record_from_report(REF, dujjon_report())
        self.assertEqual(record['counts'], {'products': 3, 'core_products': 3, 'derived_products': 0, 'with_raw_materials': 3})
        self.assertEqual((record['first_permitted_on'], record['latest_permitted_on']), ('2026-01-13', '2026-03-18'))
        self.assertEqual([m['month'] for m in record['monthly_permits']], ['2026-01', '2026-02', '2026-03'])
        self.assertEqual(record['recent_products'][0]['permitted_on'], '2026-03-18')
        estimated = {i['ingredient']: i['estimated'] for i in record['top_ingredients']}
        self.assertTrue(estimated['면류(카다이프 추정)'])
        self.assertFalse(estimated['카카오'])
        self.assertEqual(record['searched_names'], ['두쫀쿠', '두바이쫀득쿠키'])

    def test_file_store_round_trip_and_rejects_bad_ids_and_stale_files(self):
        store = ps.FilePublicSupply(self.tmp.name)
        self.assertIsNone(store.get('FOOD_X'))
        store.put(ps.record_from_report(REF, dujjon_report()))
        self.assertEqual(store.get('FOOD_X')['counts']['products'], 3)
        for bad in ('../etc', 'a/b', '', 'x' * 129):
            with self.assertRaises(ValueError):
                store.get(bad)
        path = Path(self.tmp.name) / 'FOOD_X.json'
        path.write_text('{broken', encoding='utf-8')
        self.assertIsNone(store.get('FOOD_X'))                 # 깨진 파일은 '미수집'
        path.write_text(json.dumps({'record_version': 99, 'food_id': 'FOOD_X'}), encoding='utf-8')
        self.assertIsNone(store.get('FOOD_X'))                 # 다른 버전 기록은 읽지 않음

    def test_not_collected_differs_from_zero(self):
        empty = ps.api_data(REF, None)
        self.assertEqual((empty['state'], empty['counts'], empty['fetched_at']), ('NOT_COLLECTED', None, None))
        zero = ps.api_data(REF, ps.record_from_report(REF, foodsafety.build_report('두쫀쿠', [], [], [], fetched_at='2026-11-10T01:00:00Z')))
        self.assertEqual((zero['state'], zero['counts']['products']), ('AVAILABLE', 0))


class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keys = {'I1250': 'k1', 'C002': 'k2'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_sync_uses_approved_names_and_aliases_per_food_id(self):
        api = FakeAPI({('I1250', '두쫀쿠'): DUJJON_PRODUCTS, ('I1250', '두바이쫀득쿠키'): DUJJON_PRODUCTS,
                       ('C002', '두쫀쿠'): DUJJON_MATERIALS})
        foods = [REF, {'food_id': 'FOOD_Y', 'canonical_name': '마라탕', 'entity_kind': 'DISH', 'aliases': ['마라탕']}]
        store = ps.FilePublicSupply(Path(self.tmp.name) / 'by_food')
        results = ps.sync(FakeAnalytics(foods), store, client=foodsafety.FoodSafetyClient(self.keys, fetch_json=api),
                          cache_dir=Path(self.tmp.name) / 'cache')
        self.assertEqual([(r['food_id'], r['ok'], r['products']) for r in results], [('FOOD_X', True, 3), ('FOOD_Y', True, 0)])
        searched = sorted({(s, n) for s, n, _, _ in api.calls})
        self.assertEqual(searched, [('C002', '두바이쫀득쿠키'), ('C002', '두쫀쿠'), ('C002', '마라탕'),
                                    ('I1250', '두바이쫀득쿠키'), ('I1250', '두쫀쿠'), ('I1250', '마라탕')])
        self.assertEqual(store.get('FOOD_X')['counts']['products'], 3)   # 별칭 두 개로 찾은 같은 제품은 한 번만
        self.assertEqual(store.get('FOOD_Y')['counts']['products'], 0)

    def test_one_failing_food_does_not_stop_the_rest(self):
        def flaky(url, timeout):
            if '%EB%A7%88%EB%9D%BC%ED%83%95' in url:       # 마라탕
                return {'RESULT': {'CODE': 'INFO-100', 'MSG': '인증키가 유효하지 않습니다.'}}
            return FakeAPI({('I1250', '두쫀쿠'): DUJJON_PRODUCTS})(url, timeout)
        foods = [{'food_id': 'FOOD_Y', 'canonical_name': '마라탕', 'entity_kind': 'DISH', 'aliases': []}, dict(REF, aliases=[])]
        store = ps.FilePublicSupply(Path(self.tmp.name) / 'by_food')
        results = ps.sync(FakeAnalytics(foods), store, client=foodsafety.FoodSafetyClient(self.keys, fetch_json=flaky),
                          cache_dir=Path(self.tmp.name) / 'cache')
        self.assertEqual([r['ok'] for r in results], [False, True])
        self.assertNotIn('k1', results[0]['error'])
        self.assertIsNone(store.get('FOOD_Y'))
        self.assertIsNotNone(store.get('FOOD_X'))

    def test_food_filter(self):
        foods = [REF, {'food_id': 'FOOD_Y', 'canonical_name': '마라탕', 'entity_kind': 'DISH', 'aliases': []}]
        self.assertEqual([f['food_id'] for f in ps.approved_foods(FakeAnalytics(foods), ['FOOD_Y'])], ['FOOD_Y'])


class AppApiPublicSupply(unittest.TestCase):
    """실제 B8 계층(FoodAnalytics + SQLite 저장소)에 파일 기록을 붙였을 때의 응답."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        storage = app_mock.cached_dataset(cls.temp.name)
        cls.records = ps.FilePublicSupply(Path(cls.temp.name) / 'by_food')
        ref = {'food_id': 'FOOD_MOCK_DUJJONKU', 'canonical_name': '[MOCK] 두쫀쿠', 'entity_kind': 'PRODUCT'}
        cls.records.put(ps.record_from_report(ref, dujjon_report()))
        analytics = FoodAnalytics(SQLiteAnalyticsStore(storage), clock=lambda: app_mock.MOCK_NOW, origin='mock')
        cls.api = AppApi(analytics, public_supply=cls.records)
        cls.bare = AppApi(analytics)                                  # 기록 저장소 없이 띄운 서버
        cls.doc = contract.openapi()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def get(self, api, food_id):
        path = '/api/v1/foods/%s/public-supply' % food_id
        status, body, _ = api.handle('GET', path, HOST)
        _, operation = contract.operation_for(self.doc, path)
        schema = contract.response_schema(self.doc, operation, str(status)) if str(status) in operation['responses'] \
            else {'$ref': '#/components/schemas/ErrorResponse'}
        self.assertEqual(contract.check(self.doc, body, schema), [])
        return status, body

    def test_available_record_follows_the_contract(self):
        status, body = self.get(self.api, 'FOOD_MOCK_DUJJONKU')
        data = body['data']
        self.assertEqual((status, data['state'], data['provider']['origin']), (200, 'AVAILABLE', 'live_api'))
        self.assertEqual(data['food']['food_id'], 'FOOD_MOCK_DUJJONKU')
        self.assertEqual(data['counts']['products'], 3)

    def test_merged_id_reads_the_target_food_record(self):
        status, body = self.get(self.api, 'FOOD_MOCK_DUBAI_COOKIE')
        self.assertEqual((status, body['data']['food']['food_id'], body['data']['state']), (200, 'FOOD_MOCK_DUJJONKU', 'AVAILABLE'))

    def test_not_collected_and_no_store(self):
        self.assertEqual(self.get(self.api, 'FOOD_MOCK_MALATANG')[1]['data']['state'], 'NOT_COLLECTED')
        self.assertEqual(self.get(self.bare, 'FOOD_MOCK_DUJJONKU')[1]['data']['state'], 'NOT_COLLECTED')

    def test_errors(self):
        self.assertEqual(self.get(self.api, 'FOOD_UNKNOWN')[0], 404)
        status, body, _ = self.api.handle('GET', '/api/v1/foods/FOOD_MOCK_DUJJONKU/public-supply?x=1', HOST)
        self.assertEqual((status, body['error']['code']), (400, 'INVALID_PARAMETER'))
        status, _, _ = self.api.handle('POST', '/api/v1/foods/FOOD_MOCK_DUJJONKU/public-supply', HOST)
        self.assertEqual(status, 405)

    def test_existing_food_detail_is_unchanged(self):
        status, body, _ = self.api.handle('GET', '/api/v1/foods/FOOD_MOCK_DUJJONKU', HOST)
        self.assertEqual(status, 200)
        self.assertNotIn('public_supply', body['data'])               # 상세 응답에는 필드를 추가하지 않음


if __name__ == '__main__':
    unittest.main()
