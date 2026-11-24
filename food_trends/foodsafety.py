"""식품안전나라 공공데이터(식약처) 조회·분석 모듈.

유행 음식 이름(별칭)으로 다음 두 공공데이터를 조회해 '공급' 쪽 정보를 만든다.
  - I1250 식품(첨가물)품목제조보고: 제품명, 제조사, 식품유형, 허가(신고)일
  - C002  식품(첨가물)품목제조보고(원재료): 제품별 원재료 목록
두 데이터는 품목보고번호(PRDLST_REPORT_NO)로 연결한다.

기존 수집·추적·Oracle 파이프라인과 독립된 모듈이며 기존 DB에 쓰지 않는다.
결과는 data/foodsafety/ 아래 JSON 캐시로만 저장한다(data/는 git 제외 대상).

사용 예:
    python -m food_trends.foodsafety 두쫀쿠 --alias 두바이쫀득쿠키
    python -m food_trends.foodsafety 두쫀쿠 --alias 두바이쫀득쿠키 --suppliers 3
    python -m food_trends.foodsafety 피스타치오 --ingredient
    python -m food_trends.foodsafety 약과 --json
"""
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .paths import PROJECT_ROOT

BASE_URL = 'https://openapi.foodsafetykorea.go.kr/api'
SERVICE_PRODUCTS = 'I1250'
SERVICE_MATERIALS = 'C002'
PAGE_SIZE = 1000            # 식품안전나라 1회 요청 최대 건수
DEFAULT_MAX_ROWS = 5000     # 음식(검색어) 하나당 최대 조회 건수
CACHE_DIR = PROJECT_ROOT / 'data' / 'foodsafety'
CODE_OK = 'INFO-000'
CODE_NO_DATA = 'INFO-200'

# 원재료 집계에서 제외할 항목(의미 있는 '재료'가 아닌 것)
INGREDIENT_STOPWORDS = {
    # 첨가물·물
    '정제수', '물', '식품첨가물', '혼합제제', '기타', '향료', '합성향료', '천연향료', '착색료',
    '산도조절제', '유화제', '팽창제', '팽창', '주정',
    # 재료가 아니라 식품유형(분류)명인 묶음 표기
    '과자', '과자류', '곡류가공품', '기타가공품', '당류가공품', '기타농산가공품',
    '전분가공품', '식용유지류', '식용유지가공품', '기타식용유지', '고형차', '빵류', '떡류',
}
# 부분 문자열 → 대표 재료명 (위에서부터 먼저 일치하는 규칙 적용)
INGREDIENT_SYNONYMS = (
    ('마시멜로', '마시멜로'),
    ('마쉬멜로', '마시멜로'),
    ('캔디류', '캔디류(마시멜로 추정)'),   # 마시멜로는 식품유형상 캔디류로 신고되는 경우가 많음(추정)
    ('피스타치오', '피스타치오'),
    ('카다이프', '카다이프'),
    ('카다이피', '카다이프'),
    ('건면', '면류(카다이프 추정)'),     # 카다이프는 식품유형상 면류로 신고되는 경우가 많음(추정)
    ('숙면', '면류(카다이프 추정)'),
    ('면류', '면류(카다이프 추정)'),
    ('견과류', '견과류'),
    ('아몬드', '아몬드'),
    ('식물성크림', '식물성크림'),
    ('채종유', '식물성 기름'),
    ('해바라기유', '식물성 기름'),
    ('올리브유', '식물성 기름'),
    ('코코넛오일', '식물성 기름'),
    ('혼합식용유', '식물성 기름'),
    ('분유', '분유'),
    ('땅콩버터', '땅콩버터'),
    ('코코아', '카카오'),
    ('카카오', '카카오'),
    ('초콜릿', '초콜릿'),
    ('초콜렛', '초콜릿'),
    ('컴파운드', '초콜릿'),
    ('버터', '버터'),
    ('마가린', '마가린'),
    ('중력분', '밀가루'),
    ('박력분', '밀가루'),
    ('강력분', '밀가루'),
    ('소맥분', '밀가루'),
    ('밀가루', '밀가루'),
    ('설탕', '설탕'),
    ('물엿', '물엿'),
    ('대두유', '식물성 기름'),
    ('콩기름', '식물성 기름'),
    ('전란', '계란'),
    ('계란', '계란'),
    ('달걀', '계란'),
    ('정제소금', '소금'),
    ('천일염', '소금'),
    ('계피', '계피'),
)


class FoodSafetyError(Exception):
    """식품안전나라 호출 실패. 메시지에 인증키를 넣지 않는다."""


# ---------------------------------------------------------------- 설정

def load_keys(environ=None):
    """서비스별 인증키. FOODSAFETY_API_KEY 하나만 있으면 두 서비스에 공통 사용."""
    if environ is None:
        try:
            from .config import load_env
            load_env()
        except Exception:
            pass
        environ = os.environ
    common = environ.get('FOODSAFETY_API_KEY', '').strip()
    return {
        SERVICE_PRODUCTS: environ.get('FOODSAFETY_I1250_KEY', '').strip() or common,
        SERVICE_MATERIALS: environ.get('FOODSAFETY_C002_KEY', '').strip() or common,
    }


# ---------------------------------------------------------------- 조회

def _http_get_json(url, timeout):
    req = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'MZFoodTraker-foodsafety/1.0'})
    with urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode('utf-8'))


class FoodSafetyClient:
    """식품안전나라 OpenAPI 클라이언트. fetch_json을 바꿔 끼우면 오프라인 테스트가 가능하다."""

    def __init__(self, keys, timeout=15, fetch_json=None):
        self.keys = keys
        self.timeout = timeout
        self.fetch_json = fetch_json or _http_get_json

    def _url(self, service, start, end, name):
        key = self.keys.get(service)
        if not key:
            raise FoodSafetyError(f'{service} 인증키가 없습니다 (.env의 FOODSAFETY_{service}_KEY)')
        return f'{BASE_URL}/{quote(key, safe="")}/{service}/json/{start}/{end}/PRDLST_NM={quote(name, safe="")}'

    def search(self, service, name, max_rows=DEFAULT_MAX_ROWS):
        """제품명에 name이 포함된 행 전체를 페이지 단위로 가져온다. (rows, total_count)"""
        rows, start, total = [], 1, None
        while True:
            end = min(start + PAGE_SIZE - 1, max_rows)
            if total is not None:
                end = min(end, total)
            try:
                data = self.fetch_json(self._url(service, start, end, name), self.timeout)
            except HTTPError as e:
                raise FoodSafetyError(f'{service} HTTP {e.code}') from None
            except (URLError, TimeoutError, OSError) as e:
                raise FoodSafetyError(f'{service} 연결 실패: {type(e).__name__}') from None
            except json.JSONDecodeError:
                raise FoodSafetyError(f'{service} 응답이 JSON이 아닙니다(인증키·서비스ID 확인)') from None
            body = data.get(service) if isinstance(data, dict) else None
            if not isinstance(body, dict):
                code = (data.get('RESULT') or {}).get('CODE') if isinstance(data, dict) else None
                raise FoodSafetyError(f'{service} 예상하지 못한 응답 형식 (코드: {code})')
            code = (body.get('RESULT') or {}).get('CODE')
            if code == CODE_NO_DATA:
                return [], 0
            if code != CODE_OK:
                raise FoodSafetyError(f'{service} 오류 {code}: {(body.get("RESULT") or {}).get("MSG")}')
            total = int(body.get('total_count') or 0)
            rows.extend(body.get('row') or [])
            start = end + 1
            if start > min(total, max_rows) or not body.get('row'):
                return rows, total


# ---------------------------------------------------------------- 정규화·분석

def normalize_name(text):
    text = unicodedata.normalize('NFC', str(text or '')).casefold()
    text = re.sub(r'\([^)]*\)|\[[^\]]*\]', '', text)      # (브랜드) 같은 괄호 제거
    return re.sub(r'[\s_\-·.,]+', '', text)


# 음식명 바로 뒤에 오면 '그 음식 맛/스타일의 다른 제품'이 되는 말
DERIVED_SUFFIX_START = ('맛', '향', '스타일', '만들기', '만드는', '풍')
# 음식명 뒤 어디에든 있으면 다른 종류의 제품으로 보는 말
DERIVED_PRODUCT_NOUNS = (
    '케익', '케이크', '도넛', '도너츠', '스프레드', '잼', '키트', '마시멜로', '찰떡', '떡',
    '라떼', '음료', '아이스크림', '빙수', '소스', '시럽', '파우더', '크림', '쿠키', '빵', '바',
    '초콜릿', '젤리', '캔디', '타르트', '마카롱', '머핀', '와플', '푸딩', '우유',
)


def match_kind(product_name, aliases):
    """제품명이 음식명(별칭)을 어떻게 포함하는지로 분류한다(이름 규칙 기반 추정).
       'core'   : 그 음식 자체 또는 맛·포장 변형 (예: '대왕 두쫀쿠', '화정당 두바이쫀득쿠키 말차', '두바이쫀득쿠키6개입')
       'derived': 그 음식 맛/스타일의 다른 제품 (예: '두쫀쿠케익', '하루단백바 두쫀쿠맛', '약과크림치즈쿠키')
       'other'  : 음식명이 들어 있지 않음"""
    name = normalize_name(product_name)
    norm = sorted({normalize_name(a) for a in aliases if normalize_name(a)}, key=len, reverse=True)
    found = False
    for alias in norm:
        idx = name.find(alias)
        if idx < 0:
            continue
        found = True
        suffix = name[idx + len(alias):]
        if suffix.startswith(DERIVED_SUFFIX_START):
            continue
        if any(noun in suffix for noun in DERIVED_PRODUCT_NOUNS):
            continue
        return 'core'
    return 'derived' if found else 'other'


def split_ingredients(raw):
    """'콩기름(대두유), 물엿, 밀가루' → ['콩기름(대두유)', '물엿', '밀가루'] (괄호 안 쉼표는 무시)"""
    parts, depth, buf = [], 0, ''
    for ch in str(raw or ''):
        if ch in '([':
            depth += 1
        elif ch in ')]' and depth:
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(buf)
            buf = ''
        else:
            buf += ch
    parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def canonical_ingredient(raw):
    """원재료 하나를 대표 이름으로 바꾼다. 집계에서 뺄 항목이면 None."""
    base = re.sub(r'\([^)]*\)|\[[^\]]*\]', '', raw)
    base = re.sub(r'\s+', '', unicodedata.normalize('NFC', base))
    base = re.sub(r'(?:혼합제제|제제)$', '', base) if base not in INGREDIENT_STOPWORDS else base
    if not base or base in INGREDIENT_STOPWORDS:
        return None
    if base.endswith('향') or base.endswith('향료'):
        return None   # '피스타치오향', '초콜릿향'은 향료이지 재료가 아님
    for needle, canonical in INGREDIENT_SYNONYMS:
        if needle in base:
            return canonical
    if base.startswith('D-') or base.startswith('L-') or base.endswith('나트륨') or base.endswith('산'):
        return None   # 화학명 첨가물(탄산수소나트륨, D-소르비톨 등)은 제외
    return base


def to_month(prms_dt):
    s = re.sub(r'\D', '', str(prms_dt or ''))
    return f'{s[:4]}-{s[4:6]}' if len(s) >= 6 else None


def build_report(food, aliases, product_rows, material_rows, fetched_at=None):
    """조회 결과를 앱·발표에 쓸 요약으로 만든다. 같은 품목보고번호는 한 번만 센다."""
    all_names = [food] + [a for a in aliases if a != food]
    materials = {}
    for r in material_rows:
        no = r.get('PRDLST_REPORT_NO')
        if no and no not in materials:
            materials[no] = r.get('RAWMTRL_NM') or ''

    products = {}
    for r in product_rows:
        no = r.get('PRDLST_REPORT_NO')
        if not no or no in products:
            continue
        products[no] = {
            'report_no': no,
            'name': r.get('PRDLST_NM'),
            'maker': r.get('BSSH_NM'),
            'food_type': r.get('PRDLST_DCNM'),
            'permitted_on': r.get('PRMS_DT'),
            'usage': r.get('USAGE') or None,
            'match': match_kind(r.get('PRDLST_NM'), all_names),
            'raw_materials': materials.get(no),
        }
    # 원재료 데이터에만 있는 제품도 포함 (제품 정보는 최소한으로)
    for r in material_rows:
        no = r.get('PRDLST_REPORT_NO')
        if no and no not in products:
            products[no] = {
                'report_no': no, 'name': r.get('PRDLST_NM'), 'maker': r.get('BSSH_NM'),
                'food_type': r.get('PRDLST_DCNM'), 'permitted_on': r.get('PRMS_DT'), 'usage': None,
                'match': match_kind(r.get('PRDLST_NM'), all_names), 'raw_materials': materials.get(no),
            }

    items = sorted(products.values(), key=lambda p: (p['permitted_on'] or ''), reverse=True)
    core = [p for p in items if p['match'] == 'core']

    def ingredient_rank(group):
        counter, with_materials = Counter(), 0
        for p in group:
            if not p['raw_materials']:
                continue
            with_materials += 1
            names = {canonical_ingredient(x) for x in split_ingredients(p['raw_materials'])}
            counter.update(n for n in names if n)
        return with_materials, [
            {'ingredient': n, 'products': c, 'share': round(c / with_materials, 3)}
            for n, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    core_with, core_rank = ingredient_rank(core)
    all_with, all_rank = ingredient_rank(items)
    monthly = Counter(m for m in (to_month(p['permitted_on']) for p in items) if m)
    monthly_core = Counter(m for m in (to_month(p['permitted_on']) for p in core) if m)

    return {
        'food': food,
        'aliases_searched': all_names,
        'source': '식품안전나라 I1250 품목제조보고 + C002 원재료',
        'fetched_at': fetched_at or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'counts': {
            'products': len(items),
            'core': len(core),
            'derived': sum(p['match'] == 'derived' for p in items),
            'with_raw_materials': all_with,
        },
        'first_permitted_on': min((p['permitted_on'] for p in items if p['permitted_on']), default=None),
        'latest_permitted_on': max((p['permitted_on'] for p in items if p['permitted_on']), default=None),
        'monthly_permits': dict(sorted(monthly.items())),
        'monthly_permits_core': dict(sorted(monthly_core.items())),
        # 핵심 재료는 '그 음식 자체'로 보이는 제품 기준. 없으면 전체 기준.
        'top_ingredients': (core_rank if core_with else all_rank)[:15],
        'top_ingredients_basis': 'core' if core_with else 'all',
        'products': items,
        'notes': [
            '이름 포함 검색이라 다른 음식이 섞일 수 있음: match=derived로 구분(추정 규칙).',
            'share는 원재료 정보가 있는 제품 중 그 재료를 쓴 제품 비율(함량 아님).',
            'permitted_on은 품목제조보고 허가(신고)일이며 실제 출시일과 다를 수 있음.',
        ],
    }


# ---------------------------------------------------------------- 재료별 제품·공급업체

# 핵심 재료 대표명 → 식품안전나라 검색어. None이면 너무 포괄적이라 자동 조회에서 제외.
INGREDIENT_SEARCH_TERMS = {
    '면류(카다이프 추정)': ('카다이프',),
    '카카오': ('카카오분말', '코코아분말'),
    '캔디류(마시멜로 추정)': ('마시멜로',),        # '카카오'만 쓰면 색소·그래놀라·음료가 섞임
    # 너무 흔하거나(검색 결과가 소스·향료로 넘침), 축산물이라 이 데이터에 거의 없는 재료
    '식물성 기름': None, '견과류': None, '설탕': None, '소금': None, '물엿': None,
    '버터': None, '분유': None, '마가린': None, '초콜릿': None, '계란': None, '밀가루': None,
}
# 재료용에서 제외하는 식품유형 (향료·색소·혼합 첨가물)
EXCLUDED_FOOD_TYPES = {'향료', '천연향료', '합성향료', '착색료', '혼합제제', '식품첨가물', '복합조미식품'}
# 제품명에 있으면 '재료로 쓰는 형태'로 보는 말
INGREDIENT_FORM_WORDS = (
    '페이스트', '분태', '분말', '파우더', '가루', '슬라이스', '다이스', '스프레드', '퓨레', '퓌레',
    '시럽', '커널', '원물', '필링', '베이스', '컴파운드', '커버춰', '커버처', '칩', '반태', '크림',
    '소스', '버터', '원료', '크럼블', '크런치', '코팅',
)
# 완제품 식품유형이라도 이 형태어가 있으면 재료로 인정
STRONG_FORM_WORDS = ('페이스트', '스프레드', '분말', '파우더', '분태', '필링', '크럼블', '퓨레', '퓌레',
                     '컴파운드', '코팅', '베이스')
# 제품명(재료명 제외 부분)에 있으면 완제품·맛 변형·향료로 보고 재료용에서 제외하는 말
FINISHED_PRODUCT_WORDS = (
    '쿠키', '케이크', '케익', '마카롱', '도넛', '빵', '젤리', '캔디', '아이스크림', '음료', '라떼',
    '타르트', '머핀', '와플', '쫀쿠', '쫀득', '볼', '파이', '떡', '과자', '카스텔라', '크루아상',
    '다쿠아즈', '티라미수', '단백', '프로틴', '쉐이크', '카라멜', '캬라멜', '빙수', '스낵', '바크',
    '선물세트', '파운드', '피낭시에', '휘낭시에', '샌드', '산도', '마들렌', '스콘', '롤', '카롱',
)
# 이 식품유형은 완제품으로 보고, 용도에 원료·반제품이 명시된 경우만 재료용으로 인정
FINISHED_FOOD_TYPES = {
    '빵류', '과자', '떡류', '캔디류', '빙과', '아이스크림', '아이스크림류', '음료류', '기타음료',
    '즉석섭취식품', '즉석조리식품', '껌', '추잉껌', '초콜릿',
}
# 용도(USAGE)에 있으면 업소·가공용으로 보는 말
INGREDIENT_USE_WORDS = ('원료', '제과', '제빵', '가공용', '업소용', '업무용', '베이커리', '조리용', '토핑', '부재료')
RAW_MATERIAL_USE_WORDS = ('원료', '반제품', '부원료', '원재료', '속재료', '충전')
# 재료명 앞에 붙어도 '재료 그 자체'로 보는 가공·상태 표현 (예: 구운가염피스타치오, 냉동 카다이프)
PROCESS_WORDS = (
    '껍질째', '껍질', '구운', '볶은', '볶음', '가염', '무염', '냉동', '냉장', '생', '건조', '깐',
    '미국산', '이란산', '터키산', '튀르키예', '수입', '국산', '홀', '분태', '슬라이스', '다이스', '원물',
    '갓구운', '탈각', '100%', '프리미엄', '세척', '저온', '로스팅', '로스티드', '솔티드', '&', '점보', '구움',
    '당일', '가미', '유기농', '천연',
)


def _only_process_words(text):
    for w in sorted(PROCESS_WORDS, key=len, reverse=True):
        text = text.replace(w, '')
    return text == ''


def ingredient_use(row, term=''):
    """재료(원료)로 쓰는 제품인지 이름·유형·용도로 추정. 반환: 판단 근거('usage'/'name') 또는 None.
       term은 검색어 하나 또는 여러 개(tuple). 제품명에 들어 있는 검색어를 기준으로 판단한다."""
    usage = str(row.get('USAGE') or '')
    name = normalize_name(row.get('PRDLST_NM'))
    if row.get('PRDLST_DCNM') in EXCLUDED_FOOD_TYPES:
        return None
    terms = [normalize_name(t) for t in ((term,) if isinstance(term, str) else term) if t]
    term = next((t for t in sorted(terms, key=len, reverse=True) if t in name), terms[0] if terms else '')
    rest = name.replace(term, '') if term else name
    if any(w in rest.replace('컴파운드', '') for w in FINISHED_PRODUCT_WORDS):   # '컴파운드'의 '파운드' 오인 방지
        return None
    if term and (term + '향' in name or term + '맛' in name):
        return None                                # '피스타치오향 파우더'는 향료, 재료 아님
    if row.get('PRDLST_DCNM') in FINISHED_FOOD_TYPES and not (
            any(w in rest for w in STRONG_FORM_WORDS)
            or (any(w in usage for w in RAW_MATERIAL_USE_WORDS) and any(w in rest for w in INGREDIENT_FORM_WORDS))):
        return None                                # 완제품 유형은 확실한 재료 형태어, 또는 원료용 용도+형태어가 있어야 인정
    if term and term in name and _only_process_words(rest):
        return 'name'                              # '피스타치오', '구운가염피스타치오', '냉동 카다이프'
    if any(w in usage for w in INGREDIENT_USE_WORDS):
        return 'usage'
    if any(w in rest for w in INGREDIENT_FORM_WORDS):
        return 'name'
    return None


def build_ingredient_report(ingredient, search_term, product_rows, fetched_at=None, example_limit=3, total_count=None):
    """재료 하나로 검색한 품목제조보고에서 '재료로 쓰는 제품'과 공급업체(제조사)를 정리한다."""
    seen, products = set(), []
    for r in product_rows:
        no = r.get('PRDLST_REPORT_NO')
        if not no or no in seen:
            continue
        seen.add(no)
        basis = ingredient_use(r, search_term)
        products.append({
            'report_no': no, 'name': r.get('PRDLST_NM'), 'maker': r.get('BSSH_NM'),
            'food_type': r.get('PRDLST_DCNM'), 'permitted_on': r.get('PRMS_DT'),
            'usage': r.get('USAGE') or None, 'ingredient_use': basis is not None, 'basis': basis,
        })
    products.sort(key=lambda p: p['permitted_on'] or '', reverse=True)
    usable = [p for p in products if p['ingredient_use']]
    by_maker = {}
    for p in usable:
        m = by_maker.setdefault(p['maker'] or '(제조사 미상)', {'maker': p['maker'], 'products': 0, 'examples': [],
                                                                'latest_permitted_on': p['permitted_on']})
        m['products'] += 1
        if len(m['examples']) < example_limit:
            m['examples'].append(p['name'])
    suppliers = sorted(by_maker.values(), key=lambda m: (-m['products'], m['maker'] or ''))
    types = Counter(p['food_type'] for p in usable if p['food_type'])
    return {
        'ingredient': ingredient,
        'search_term': search_term if isinstance(search_term, str) else ', '.join(search_term),
        'source': '식품안전나라 I1250 품목제조보고',
        'fetched_at': fetched_at or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'counts': {'products': len(products), 'ingredient_use': len(usable), 'suppliers': len(suppliers),
                   'fetched_rows': len(product_rows),
                   'total_in_source': total_count if total_count is not None else len(product_rows)},
        'food_types': [{'food_type': t, 'products': n} for t, n in types.most_common(10)],
        'suppliers': suppliers,
        'products': usable,
        'notes': [
            '제품명에 재료명이 들어간 신고 제품 중 이름(페이스트·분말·분태 등)이나 용도(제과·제빵·원료용 등)로 '
            '재료용으로 추정한 것만 남김.',
            '제조사 = 신고한 제조업체. 가격·판매처(온라인몰)는 이 데이터에 없음.',
        ],
    }


def lookup_ingredient(ingredient, search_term=None, client=None, max_rows=DEFAULT_MAX_ROWS, refresh=False, cache_dir=CACHE_DIR):
    """재료 하나를 검색어 하나 이상으로 조회. search_term: 문자열 또는 문자열 tuple (없으면 재료명)."""
    terms = (search_term,) if isinstance(search_term, str) else tuple(search_term or (ingredient,))
    terms = tuple(t.strip() for t in terms if t and t.strip())
    path = Path(cache_dir) / ('ingredient-' + cache_path(terms[0], list(terms[1:]), cache_dir).name)
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding='utf-8')), True
    client = client or FoodSafetyClient(load_keys())
    rows, total = [], 0
    for t in terms:
        r, n = client.search(SERVICE_PRODUCTS, t, max_rows)
        rows.extend(r)
        total += n
    report = build_ingredient_report(ingredient, terms if len(terms) > 1 else terms[0], rows, total_count=total)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report, False


def search_term_for(ingredient):
    """핵심 재료 대표명을 검색어 tuple로. 자동 조회에서 뺄 재료는 None."""
    if ingredient in INGREDIENT_SEARCH_TERMS:
        return INGREDIENT_SEARCH_TERMS[ingredient]
    term = re.sub(r'\(.*?\)', '', ingredient).strip()
    return (term,) if term else None


def format_ingredient_summary(report, supplier_limit=8):
    c = report['counts']
    lines = [
        f"[재료: {report['ingredient']}] 검색어 '{report['search_term']}'",
        f"신고 제품 {c['products']}건 중 재료용 추정 {c['ingredient_use']}건, 공급업체(제조사) {c['suppliers']}곳",
    ]
    total, fetched = c.get('total_in_source', c['products']), c.get('fetched_rows', c['products'])
    if total > fetched:
        lines.append(f"  ※ 전체 {total}건 중 {fetched}건만 조회함 (--max-rows로 늘릴 수 있음)")
    if report['food_types']:
        lines.append('식품유형: ' + ', '.join(f"{t['food_type']} {t['products']}" for t in report['food_types'][:5]))
    lines.append('주요 공급업체:')
    for m in report['suppliers'][:supplier_limit]:
        lines.append(f"  - {m['maker']} ({m['products']}개): {', '.join(m['examples'])}")
    if not report['suppliers']:
        lines.append('  (재료용으로 추정되는 제품 없음)')
    return '\n'.join(lines)


# ---------------------------------------------------------------- 캐시·실행

def cache_path(food, aliases, cache_dir=CACHE_DIR):
    key = '|'.join([food] + sorted(set(aliases)))
    digest = hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]
    safe = re.sub(r'[^0-9A-Za-z가-힣]+', '_', food)[:40]
    return Path(cache_dir) / f'{safe}-{digest}.json'


def lookup(food, aliases=(), client=None, max_rows=DEFAULT_MAX_ROWS, refresh=False, cache_dir=CACHE_DIR):
    """음식 하나에 대해 조회 → 요약 → 캐시 저장. 캐시가 있으면 API를 부르지 않는다."""
    aliases = [a.strip() for a in aliases if a and a.strip()]
    path = cache_path(food, aliases, cache_dir)
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding='utf-8')), True
    client = client or FoodSafetyClient(load_keys())
    names = list(dict.fromkeys([food] + aliases))
    product_rows, material_rows = [], []
    for name in names:
        rows, _ = client.search(SERVICE_PRODUCTS, name, max_rows)
        product_rows.extend(rows)
        rows, _ = client.search(SERVICE_MATERIALS, name, max_rows)
        material_rows.extend(rows)
    report = build_report(food, aliases, product_rows, material_rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report, False


def recent_months(monthly, count=12):
    """마지막 신고월부터 거꾸로 count개월(신고 0건인 달 포함)."""
    if not monthly:
        return []
    y, m = map(int, max(monthly).split('-'))
    out = []
    for _ in range(count):
        key = f'{y:04d}-{m:02d}'
        out.append((key, monthly.get(key, 0)))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def format_summary(report, limit=5):
    c = report['counts']
    lines = [
        f"[{report['food']}] 검색어: {', '.join(report['aliases_searched'])}",
        f"신고 제품 {c['products']}건 (음식 자체 {c['core']} / 파생 {c['derived']}), 원재료 정보 {c['with_raw_materials']}건",
        f"허가(신고)일 범위: {report['first_permitted_on']} ~ {report['latest_permitted_on']}",
        '',
    ]
    months = recent_months(report['monthly_permits'], 12)
    shown = sum(n for _, n in months)
    lines.append(f'월별 신고 건수 (공급 추이, 최근 12개월{"" if not months else f": {months[0][0]} ~ {months[-1][0]}"}):')
    peak = max((n for _, n in months), default=0) or 1
    for month, n in months:
        bar = '#' * max(1, round(n / peak * 30)) if n else ''
        lines.append(f'  {month}  {bar} {n}')
    if c['products'] - shown > 0:
        lines.append(f"  (그 이전 {c['products'] - shown}건)")
    lines += ['', f"핵심 원재료 (기준: {'음식 자체 제품' if report['top_ingredients_basis'] == 'core' else '전체 제품'}):"]
    for i, ing in enumerate(report['top_ingredients'][:10], 1):
        lines.append(f"  {i:>2}. {ing['ingredient']}  {ing['products']}개 제품 ({ing['share'] * 100:.0f}%)")
    lines += ['', f'최근 신고 제품 {limit}개:']
    for p in report['products'][:limit]:
        tag = '' if p['match'] == 'core' else ' [파생]'
        lines.append(f"  {p['permitted_on']}  {p['name']}{tag} - {p['maker']} ({p['food_type']})")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='식품안전나라 품목제조보고로 유행 음식의 공급·원재료·재료 공급업체 분석')
    parser.add_argument('food', help='대표 음식명 (예: 두쫀쿠). --ingredient와 함께 쓰면 재료명 (예: 피스타치오)')
    parser.add_argument('--alias', action='append', default=[], help='추가 검색 별칭, 여러 번 사용 가능')
    parser.add_argument('--ingredient', action='store_true', help='음식 대신 재료 하나의 제품·공급업체 조회')
    parser.add_argument('--suppliers', type=int, default=0, metavar='N',
                        help='음식 분석 후 핵심 재료 상위 N개의 공급업체도 조회 (재료당 API 1회 이상)')
    parser.add_argument('--max-rows', type=int, default=DEFAULT_MAX_ROWS, help='검색어당 최대 조회 건수')
    parser.add_argument('--refresh', action='store_true', help='캐시를 무시하고 다시 조회')
    parser.add_argument('--json', action='store_true', help='요약 대신 전체 JSON 출력')
    args = parser.parse_args(argv)
    if not 1 <= args.max_rows <= 20000:
        parser.error('--max-rows는 1~20000')
    if not 0 <= args.suppliers <= 10:
        parser.error('--suppliers는 0~10')
    try:
        if args.ingredient:
            report, cached = lookup_ingredient(args.food, max_rows=args.max_rows, refresh=args.refresh)
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_ingredient_summary(report))
            return 0
        report, cached = lookup(args.food, args.alias, max_rows=args.max_rows, refresh=args.refresh)
        supplier_reports = []
        for ing in report['top_ingredients']:
            if len(supplier_reports) >= args.suppliers:
                break
            term = search_term_for(ing['ingredient'])
            if term:
                supplier_reports.append(lookup_ingredient(ing['ingredient'], term, max_rows=args.max_rows,
                                                          refresh=args.refresh)[0])
    except FoodSafetyError as e:
        print(f'조회 실패: {e}', file=sys.stderr)
        return 1
    if args.json:
        out = dict(report, ingredient_suppliers=supplier_reports) if args.suppliers else report
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(format_summary(report))
        for r in supplier_reports:
            print('\n' + format_ingredient_summary(r))
        print(f"\n({'캐시 사용' if cached else '새로 조회'}: {cache_path(args.food, args.alias).relative_to(PROJECT_ROOT)})")
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
