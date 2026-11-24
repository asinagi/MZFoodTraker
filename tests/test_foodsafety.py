"""식품안전나라 모듈 오프라인 테스트. 실제 2026-11-09 응답 일부를 고정 데이터로 사용한다(외부 접속 없음)."""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from food_trends import foodsafety as fs

OK = {'MSG': '정상처리되었습니다.', 'CODE': 'INFO-000'}

DUJJON_PRODUCTS = [
    {'PRDLST_REPORT_NO': '20200280351531', 'PRMS_DT': '20260113', 'PRDLST_NM': '꼬랑지 두쫀쿠',
     'BSSH_NM': '꼬랑지마카롱', 'PRDLST_DCNM': '과자', 'USAGE': '디저트'},
    {'PRDLST_REPORT_NO': '2021036314847', 'PRMS_DT': '20260318', 'PRDLST_NM': '내가진짜두쫀쿠',
     'BSSH_NM': '금강베이커리', 'PRDLST_DCNM': '과자', 'USAGE': '식용및 간식'},
    {'PRDLST_REPORT_NO': '20110275040697', 'PRMS_DT': '20260219', 'PRDLST_NM': '대왕 두쫀쿠',
     'BSSH_NM': '파파브레드', 'PRDLST_DCNM': '과자', 'USAGE': '간식 및 식사대용'},
]
DUJJON_MATERIALS = [
    {'PRDLST_REPORT_NO': '20200280351531', 'PRMS_DT': '20260113', 'PRDLST_NM': '꼬랑지 두쫀쿠', 'BSSH_NM': '꼬랑지마카롱',
     'PRDLST_DCNM': '과자', 'RAWMTRL_NM': '땅콩 또는 견과류가공품, 백설탕, 건면, 물엿, 화이트컴파운드, 카카오분말, 탈지분유, 버터, 젤라틴, D-소르비톨혼합제제, 분말결정포도당'},
    {'PRDLST_REPORT_NO': '2021036314847', 'PRMS_DT': '20260318', 'PRDLST_NM': '내가진짜두쫀쿠', 'BSSH_NM': '금강베이커리',
     'PRDLST_DCNM': '과자', 'RAWMTRL_NM': '탈지분유, 기타코코아가공품, 화이트초콜릿, 피스타치오견과, 가공버터, 캔디류, 곡류가공품'},
    {'PRDLST_REPORT_NO': '20110275040697', 'PRMS_DT': '20260219', 'PRDLST_NM': '대왕 두쫀쿠', 'BSSH_NM': '파파브레드',
     'PRDLST_DCNM': '과자', 'RAWMTRL_NM': '과자류, 식품첨가물, 설탕, 물엿, 식물성크림, 코코아분말, 정제수, 마가린'},
]
YAKGWA_PRODUCTS = [
    {'PRDLST_REPORT_NO': '20170383088145', 'PRMS_DT': '20181212', 'PRDLST_NM': '(차차커피코)냉동완제_약과크림치즈쿠키',
     'BSSH_NM': '로로에프엔비주식회사', 'PRDLST_DCNM': '과자'},
    {'PRDLST_REPORT_NO': '2016035539611', 'PRMS_DT': '20250718', 'PRDLST_NM': '80겹 페스츄리 꿀약과',
     'BSSH_NM': '소미한과', 'PRDLST_DCNM': '과자'},
]


def response(service, rows, total=None, result=OK):
    return {service: {'total_count': str(len(rows) if total is None else total), 'row': rows, 'RESULT': result}}


class FakeAPI:
    """URL을 해석해 서비스·검색어·범위별로 고정 응답을 돌려준다."""

    def __init__(self, data):
        self.data = data          # {(service, name): rows}
        self.calls = []

    def __call__(self, url, timeout):
        parts = url.split('/api/', 1)[1].split('/')
        key, service, _, start, end, cond = parts
        name = unquote(cond.split('=', 1)[1])
        self.calls.append((service, name, int(start), int(end)))
        rows = self.data.get((service, name), [])
        if not rows:
            return {service: {'total_count': '0', 'RESULT': {'CODE': 'INFO-200', 'MSG': '해당하는 데이터가 없습니다.'}}}
        return response(service, rows[int(start) - 1:int(end)], total=len(rows))


class FoodSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keys = {'I1250': 'k1', 'C002': 'k2'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_joins_by_report_no_and_ranks_ingredients(self):
        report = fs.build_report('두쫀쿠', [], DUJJON_PRODUCTS, DUJJON_MATERIALS, fetched_at='T')
        self.assertEqual(report['counts'], {'products': 3, 'core': 3, 'derived': 0, 'with_raw_materials': 3})
        self.assertEqual(report['monthly_permits'], {'2026-01': 1, '2026-02': 1, '2026-03': 1})
        self.assertEqual(report['first_permitted_on'], '20260113')
        self.assertEqual(report['products'][0]['name'], '내가진짜두쫀쿠')      # 최신순
        top = {i['ingredient']: i['products'] for i in report['top_ingredients']}
        self.assertEqual(top['카카오'], 3)          # 카카오분말·코코아가공품·코코아분말
        self.assertEqual(top['버터'], 2)            # 버터·가공버터 (마가린은 별도)
        self.assertEqual(top['초콜릿'], 2)          # 화이트컴파운드·화이트초콜릿
        self.assertEqual(top['피스타치오'], 1)
        self.assertEqual(top['견과류'], 1)                 # 땅콩 또는 견과류가공품
        self.assertEqual(top['면류(카다이프 추정)'], 1)     # 건면
        for dropped in ('정제수', '식품첨가물', '과자류', '캔디류', 'D-소르비톨', '곡류가공품'):   # 캔디류는 '캔디류(마시멜로 추정)'으로 바뀜
            self.assertNotIn(dropped, top)

    def test_core_vs_derived_match(self):
        self.assertEqual(fs.match_kind('80겹 페스츄리 꿀약과', ['약과']), 'core')
        self.assertEqual(fs.match_kind('(차차커피코)냉동완제_약과크림치즈쿠키', ['약과']), 'derived')
        self.assertEqual(fs.match_kind('대왕 두바이 쫀득 쿠키', ['두쫀쿠', '두바이쫀득쿠키']), 'core')
        dj = ['두쫀쿠', '두바이쫀득쿠키']
        # 2026-11-09 실제 조회 결과의 제품명: 맛·포장 변형은 음식 자체
        for name in ('화정당 두바이쫀득쿠키 우베', '두바이쫀득쿠키6개입', '두바이쫀득쿠키 호두맛',
                     '흑임자 두쫀쿠(냉동)', '카다이프 두쫀쿠', '저스트쿠킷_두쫀쿠'):
            self.assertEqual(fs.match_kind(name, dj), 'core', name)
        # 그 음식 맛/스타일의 다른 제품은 파생
        for name in ('두쫀쿠케익', '하루단백바 두쫀쿠맛', '두쫀쿠스타일 헤이즐넛스프레드', '두쫀쿠 만들기 키트',
                     '두쫀쿠향에이슬', '두쫀쿠 만드는 쫀득찰떡', '두쫀쿠마시멜로', '두쫀쿠 피스타치오 스프레드'):
            self.assertEqual(fs.match_kind(name, dj), 'derived', name)
        self.assertEqual(fs.match_kind('마라탕', dj), 'other')
        report = fs.build_report('약과', [], YAKGWA_PRODUCTS, [])
        self.assertEqual((report['counts']['core'], report['counts']['derived']), (1, 1))
        self.assertEqual(report['top_ingredients'], [])

    def test_split_keeps_commas_inside_parentheses(self):
        self.assertEqual(fs.split_ingredients('콩기름(대두유, 국산), 물엿,밀가루'), ['콩기름(대두유, 국산)', '물엿', '밀가루'])
        self.assertEqual(fs.canonical_ingredient('콩기름(대두유)'), '식물성 기름')
        self.assertEqual(fs.canonical_ingredient('탄산수소나트륨'), None)

    def test_lookup_dedupes_aliases_paginates_and_caches(self):
        many = [dict(DUJJON_PRODUCTS[0], PRDLST_REPORT_NO=str(i), PRDLST_NM=f'두쫀쿠 {i}') for i in range(1500)]
        api = FakeAPI({('I1250', '두쫀쿠'): many + DUJJON_PRODUCTS,
                       ('I1250', '두바이쫀득쿠키'): DUJJON_PRODUCTS,       # 같은 품목보고번호 → 중복 제거
                       ('C002', '두쫀쿠'): DUJJON_MATERIALS})
        client = fs.FoodSafetyClient(self.keys, fetch_json=api)
        report, cached = fs.lookup('두쫀쿠', ['두바이쫀득쿠키'], client=client, cache_dir=self.tmp.name)
        self.assertFalse(cached)
        self.assertEqual(report['counts']['products'], 1503)
        pages = [(s, a, b) for s, n, a, b in api.calls if s == 'I1250' and n == '두쫀쿠']
        self.assertEqual(pages, [('I1250', 1, 1000), ('I1250', 1001, 1503)])
        calls = len(api.calls)
        again, cached = fs.lookup('두쫀쿠', ['두바이쫀득쿠키'], client=client, cache_dir=self.tmp.name)
        self.assertTrue(cached)
        self.assertEqual(len(api.calls), calls)        # 캐시 사용 시 API 호출 없음
        self.assertEqual(again['counts'], report['counts'])

    def test_max_rows_caps_requests(self):
        api = FakeAPI({('I1250', '약과'): YAKGWA_PRODUCTS * 10})
        rows, total = fs.FoodSafetyClient(self.keys, fetch_json=api).search('I1250', '약과', max_rows=5)
        self.assertEqual((len(rows), total), (5, 20))
        self.assertEqual(api.calls, [('I1250', '약과', 1, 5)])

    def test_errors_never_contain_key(self):
        secret = 'SECRET-KEY-123'

        def bad(url, timeout):
            return {'RESULT': {'CODE': 'INFO-100', 'MSG': '인증키가 유효하지 않습니다.'}}
        with self.assertRaises(fs.FoodSafetyError) as ctx:
            fs.FoodSafetyClient({'I1250': secret}, fetch_json=bad).search('I1250', '약과')
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIn('INFO-100', str(ctx.exception))
        with self.assertRaises(fs.FoodSafetyError):
            fs.FoodSafetyClient({}, fetch_json=bad).search('C002', '약과')

    def test_no_data_and_api_error_codes(self):
        api = FakeAPI({})
        self.assertEqual(fs.FoodSafetyClient(self.keys, fetch_json=api).search('C002', '없는음식'), ([], 0))

        def err(url, timeout):
            return response('I1250', [], result={'CODE': 'ERROR-500', 'MSG': '서버 오류'})
        with self.assertRaises(fs.FoodSafetyError):
            fs.FoodSafetyClient(self.keys, fetch_json=err).search('I1250', '약과')

    def test_recent_months_fills_gaps(self):
        months = fs.recent_months({'2023-03': 1, '2026-01': 34, '2026-03': 15}, 4)
        self.assertEqual(months, [('2025-12', 0), ('2026-01', 34), ('2026-02', 0), ('2026-03', 15)])
        self.assertEqual(fs.canonical_ingredient('당류가공품'), None)
        self.assertEqual(fs.canonical_ingredient('피스타치오향'), None)       # 향료는 재료 아님
        self.assertEqual(fs.canonical_ingredient('초콜릿향'), None)
        self.assertEqual(fs.canonical_ingredient('캔디류'), '캔디류(마시멜로 추정)')
        self.assertEqual(fs.canonical_ingredient('마시멜로'), '마시멜로')
        self.assertEqual(fs.canonical_ingredient('채종유'), '식물성 기름')

    def test_ingredient_use_filters_finished_desserts(self):
        rows = [
            {'PRDLST_REPORT_NO': '1', 'PRDLST_NM': '피스타치오 페이스트', 'BSSH_NM': 'A식품', 'PRDLST_DCNM': '땅콩 또는 견과류가공품', 'PRMS_DT': '20260110'},
            {'PRDLST_REPORT_NO': '2', 'PRDLST_NM': '구운 피스타치오 분태', 'BSSH_NM': 'A식품', 'PRDLST_DCNM': '땅콩 또는 견과류가공품', 'PRMS_DT': '20260201'},
            {'PRDLST_REPORT_NO': '3', 'PRDLST_NM': '피스타치오', 'BSSH_NM': 'B상사', 'PRDLST_DCNM': '땅콩 또는 견과류가공품', 'PRMS_DT': '20250501'},
            {'PRDLST_REPORT_NO': '4', 'PRDLST_NM': '피스타치오 크림 쿠키', 'BSSH_NM': 'C베이커리', 'PRDLST_DCNM': '과자', 'PRMS_DT': '20260301'},
            {'PRDLST_REPORT_NO': '5', 'PRDLST_NM': '피스타치오 쫀득볼', 'BSSH_NM': 'D', 'PRDLST_DCNM': '과자', 'PRMS_DT': '20260302', 'USAGE': '간식용'},
            {'PRDLST_REPORT_NO': '6', 'PRDLST_NM': '피스타치오 스프레드S', 'BSSH_NM': 'E', 'PRDLST_DCNM': '초콜릿가공품', 'PRMS_DT': '20260303', 'USAGE': '제과제빵 원료용'},
            {'PRDLST_REPORT_NO': '1', 'PRDLST_NM': '피스타치오 페이스트', 'BSSH_NM': 'A식품', 'PRDLST_DCNM': '땅콩 또는 견과류가공품', 'PRMS_DT': '20260110'},
        ]
        r = fs.build_ingredient_report('피스타치오', '피스타치오', rows)
        self.assertEqual(r['counts'], {'products': 6, 'ingredient_use': 4, 'suppliers': 3, 'fetched_rows': 7, 'total_in_source': 7})
        self.assertEqual(r['suppliers'][0]['maker'], 'A식품')
        self.assertEqual(r['suppliers'][0]['products'], 2)
        names = {p['name'] for p in r['products']}
        self.assertNotIn('피스타치오 크림 쿠키', names)
        self.assertNotIn('피스타치오 쫀득볼', names)
        self.assertIn('피스타치오 스프레드S', names)       # 용도가 원료용
        self.assertIn('피스타치오', fs.format_ingredient_summary(r))
        # 2026-11-10 실제 조회 결과에서 확인한 사례
        def use(name, food_type='땅콩 또는 견과류가공품', usage=''):
            return fs.ingredient_use({'PRDLST_NM': name, 'PRDLST_DCNM': food_type, 'USAGE': usage}, '피스타치오')
        for name, ft in (('세척탈각피스타치오', '땅콩 또는 견과류가공품'), ('피스타치오컴파운드-HT', '식물성크림'),
                         ('피스타치오 스프레드 스무스', '땅콩 또는 견과류가공품'), ('YW 피스타치오파우더', '당류가공품')):
            self.assertIsNotNone(use(name, ft), name)
        for name, ft in (('오예스 피스타치오', '과자'), ('피스타치오 크림 카스텔라', '빵류'),
                         ('탑티어커피 피스타치오향 파우더', '당류가공품'), ('PF 반)피스타치오 파운드 슬라이스', '빵류')):
            self.assertIsNone(use(name, ft, '식품 원료로 사용'), name)
        r2 = fs.build_ingredient_report('피스타치오', '피스타치오', rows, total_count=1500)
        self.assertIn('전체 1500건 중 7건만', fs.format_ingredient_summary(r2))

    def test_search_terms_and_supplier_lookup_cache(self):
        self.assertEqual(fs.search_term_for('면류(카다이프 추정)'), ('카다이프',))
        self.assertEqual(fs.search_term_for('카카오'), ('카카오분말', '코코아분말'))
        self.assertEqual(fs.search_term_for('캔디류(마시멜로 추정)'), ('마시멜로',))
        for generic in ('식물성 기름', '버터', '분유', '초콜릿'):
            self.assertIsNone(fs.search_term_for(generic), generic)
        self.assertEqual(fs.search_term_for('피스타치오'), ('피스타치오',))
        api = FakeAPI({('I1250', '카다이프'): [{'PRDLST_REPORT_NO': '9', 'PRDLST_NM': '냉동 카다이프', 'BSSH_NM': 'K',
                                                'PRDLST_DCNM': '면류', 'PRMS_DT': '20260105'}]})
        client = fs.FoodSafetyClient(self.keys, fetch_json=api)
        r, cached = fs.lookup_ingredient('면류(카다이프 추정)', '카다이프', client=client, cache_dir=self.tmp.name)
        self.assertFalse(cached)
        self.assertEqual(r['suppliers'][0]['maker'], 'K')
        _, cached = fs.lookup_ingredient('면류(카다이프 추정)', '카다이프', client=client, cache_dir=self.tmp.name)
        self.assertTrue(cached)
        self.assertEqual(len(api.calls), 1)

    def test_multi_term_ingredient_and_excluded_types(self):
        api = FakeAPI({
            ('I1250', '카카오분말'): [{'PRDLST_REPORT_NO': '1', 'PRDLST_NM': '카카오분말', 'BSSH_NM': 'P', 'PRDLST_DCNM': '기타 코코아가공품'},
                                    {'PRDLST_REPORT_NO': '2', 'PRDLST_NM': '천연카카오분말색소', 'BSSH_NM': 'Q', 'PRDLST_DCNM': '착색료'}],
            ('I1250', '코코아분말'): [{'PRDLST_REPORT_NO': '3', 'PRDLST_NM': '알칼리 코코아분말', 'BSSH_NM': 'P', 'PRDLST_DCNM': '기타 코코아가공품', 'USAGE': '제과제빵 원료'},
                                    {'PRDLST_REPORT_NO': '1', 'PRDLST_NM': '카카오분말', 'BSSH_NM': 'P', 'PRDLST_DCNM': '기타 코코아가공품'}],
        })
        client = fs.FoodSafetyClient(self.keys, fetch_json=api)
        r, _ = fs.lookup_ingredient('카카오', ('카카오분말', '코코아분말'), client=client, cache_dir=self.tmp.name)
        self.assertEqual(r['search_term'], '카카오분말, 코코아분말')
        self.assertEqual(r['counts']['products'], 3)               # 품목보고번호 중복 제거
        self.assertEqual(r['counts']['ingredient_use'], 2)         # 착색료 제외
        self.assertEqual(r['suppliers'], [{'maker': 'P', 'products': 2, 'examples': ['카카오분말', '알칼리 코코아분말'],
                                           'latest_permitted_on': None}])
        self.assertNotIn('※', fs.format_ingredient_summary(r))   # 겹친 건수 때문에 '덜 조회' 경고가 뜨면 안 됨

    def test_keys_from_env(self):
        self.assertEqual(fs.load_keys({'FOODSAFETY_API_KEY': 'c', 'FOODSAFETY_C002_KEY': 'm'}), {'I1250': 'c', 'C002': 'm'})

    def test_summary_text(self):
        text = fs.format_summary(fs.build_report('두쫀쿠', [], DUJJON_PRODUCTS, DUJJON_MATERIALS))
        self.assertIn('신고 제품 3건', text)
        self.assertIn('2026-03', text)
        self.assertIn('카카오', text)
        json.dumps(fs.build_report('두쫀쿠', [], DUJJON_PRODUCTS, DUJJON_MATERIALS), ensure_ascii=False)


if __name__ == '__main__':
    unittest.main()
