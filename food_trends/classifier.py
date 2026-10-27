"""Target-bound food evidence. Rule scores are not probabilities."""
import re
import unicodedata

VERSION = 'target-context-rules-v2'
SCORE_BASIS = '규칙 점수 0–100; 확률 또는 보정된 신뢰도가 아님'
# Category / product-form expressions, not a catalog of trend names.
FOOD = r'디저트|베이커리|음료|식재료|식품|간식|쿠키|케이크|초콜릿|아이스크림|라떼|커피|맥주|와인|치즈|라면|김치|김밥|밀키트|젤리|도넛|샌드위치|주스|빵|떡'
CATEGORY = r'디저트|음료|식재료|식품|간식|과자|채소|과일|농산물|요리|쿠키|케이크|빵|젤리|도넛|맥주|와인|커피'
CONSUMPTION = r'레시피|시식|식감|풍미|달콤|쫀득|바삭|먹는|먹을|먹어|먹었다|맛보|맛있|구매|판매|품절|신메뉴|출시'
NON_PRODUCT = r'전기차|자동차|세단|하이브리드|스마트홈|스마트폰|태블릿|폴더블폰|반도체|보험|청바지|의류|안경|냉장고|노트북|소프트웨어|게임|앱|AI 모델'
CORPORATE = r'주가|증시|상장|배당|실적|주식|매각|인수|인사|채용|영업익'
PERSON_ROLE = r'배우|가수|선수|감독|의원|대통령|장관|총리|셰프|요리사|회장|대표|처장|청장'


def normalize(name):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', name)).strip()


def target_pattern(keyword):
    chars = [re.escape(c) for c in keyword if not c.isspace()]
    return (r'(?<![\w])' + r'\s*'.join(chars) +
            r'(?=$|[^\w]|(?:은|는|이|가|의|을|를|와|과|에서|에|으로|로|도|만)(?=$|[^\w]))')


def classify(item):
    keyword = normalize(item['keyword'])
    target = target_pattern(keyword)
    news = item.get('news') or []
    reasons, evidence = [], []
    entity, excluded = 'UNKNOWN', None
    if re.search(r'(?:식당|음식점|맛집|카페|공항|백화점|특구|수영장|저수지)$', keyword) or re.search(r'[가-힣]{2,}역$|[가-힣]{2,}(?:시|군|구)$', keyword):
        entity, excluded = 'LOCATION', '검색어의 장소/시설 형태'
    elif re.search(r'(?:처장|청장|장관|총리|의원|대통령|셰프|요리사)$', keyword) or re.match(r'(?:'+PERSON_ROLE+r')\s+', keyword):
        entity, excluded = 'PERSON', '검색어의 인물 직책 형태'
    elif re.search(r'(?:재단|공단|위원회|연구소|재판소|교육청|소방서|협회|항공사|행정기관|금융지주|은행|대학교)$', keyword):
        entity, excluded = 'ORGANIZATION', '검색어의 기관/조직 형태'
    elif re.search(NON_PRODUCT, keyword, re.I):
        entity, excluded = 'NON_FOOD_PRODUCT', '검색어가 비식품 상품 범주'
    elif re.search(r'축제|선거|대회|(?:\s대\s|\svs\s)|여행|돌잔치', keyword, re.I):
        entity, excluded = 'EVENT', '검색어가 행사/경기/활동'
    direct = bool(re.search(r'(?:' + FOOD + r')(?:\s*(?:신상|신제품|레시피|만들기))?$', keyword))
    bound_food, definitions, food_brands, produce = [], [], [], []
    role_news, non_product_news, finance_news = [], [], []
    for n in news:
        title = normalize(n.get('title') or '')
        matches = list(re.finditer(target, title, re.I)) if keyword else []
        relation, signals = 'background', []
        if matches:
            windows = [title[max(0,m.start()-20):m.end()+24] for m in matches]
            role = bool(re.search(r'(?:'+PERSON_ROLE+r')\s*[‘\'"“]?'+target, title, re.I) or re.search(target+r'[’\'"”]?\s*(?:'+PERSON_ROLE+r')', title, re.I))
            if role:
                role_news.append(n)
            if any(re.search(NON_PRODUCT, window, re.I) for window in windows):
                non_product_news.append(n)
            if re.search(CORPORATE, title):
                finance_news.append(n)
            definition = bool(re.search(target+r'[’\'"”]?\s*(?:은|는|이란|란|이라는|이|가)?\s*(?:새로운\s*|신상\s*|신종\s*|쫀득한\s*|달콤한\s*)?(?:'+CATEGORY+r')(?![가-힣a-zA-Z])', title, re.I) or re.search(r'(?:'+CATEGORY+r')\s*[‘\'"“]?'+target, title, re.I))
            attached = bool(re.search(target+r'[’\'"”]?(?:의|을|를|은|는)?\s*(?:레시피|식감|풍미|맛|시식|먹는\s*법)', title, re.I))
            if definition and not role:
                definitions.append(n); relation = 'food_definition'; signals.append('검색어에 직접 붙는 음식 범주 설명')
            elif attached and not role:
                bound_food.append(n); relation = 'food_attribute'; signals.append('검색어의 맛/섭취/조리 속성')
            brand = bool(re.search(target+r'[’\'"”]?(?:이|가|은|는|에서)?[,:，]?\s*.{0,16}(?:'+CATEGORY+r').{0,12}(?:출시|신메뉴|판매)', title, re.I))
            if brand and not role and not re.search(CORPORATE, title):
                food_brands.append(n); relation='food_brand_product'; signals.append('검색어와 식품/메뉴 출시의 연결')
            horticultural = bool(re.search(target+r'\s*한\s*개\s*무게', title, re.I) or re.search(r'최대\s*'+target+r'\s*선발대회', title, re.I))
            if horticultural:
                produce.append(n); relation='possible_produce'; signals.append('개체 무게/선발대회: 농산물 가능성, 식용 여부 미확정')
            if not signals and any(re.search(FOOD, w, re.I) and re.search(CONSUMPTION,w) for w in windows) and not role:
                bound_food.append(n); relation='nearby_food_consumption'; signals.append('검색어 인근의 음식·소비 문맥; 개체 관계 미확정')
            if role:
                relation='person_role'; signals.append('검색어에 인물 역할이 직접 연결됨')
        if signals or re.search(FOOD+r'|먹거리|신제품|출시|품절', title, re.I):
            evidence.append({'title':n.get('title'), 'url':n.get('url'), 'source':n.get('source'), 'target_mentioned':bool(matches), 'relation':relation, 'signals':signals})
    if not excluded and role_news:
        entity, excluded = 'PERSON', '검색어와 직접 연결된 인물 역할'
    if not excluded and non_product_news and not direct and not definitions:
        entity, excluded = 'NON_FOOD_PRODUCT', '검색어 인근 뉴스가 비식품 상품을 설명'
    if excluded:
        label, score = 'NON_FOOD', 0
        reasons.append(excluded + '; 뉴스의 음식 언급을 검색어의 음식성으로 전이하지 않음')
    elif finance_news and not definitions and not food_brands and not bound_food:
        label, score, entity = 'NON_FOOD', 0, 'ORGANIZATION'
        reasons.append('금융/기업 활동 문맥만 확인; 음식 상품 자체에 대한 직접 근거 없음')
    elif direct or definitions:
        label, score, entity = 'FOOD', 70 if direct else 85, 'FOOD_PRODUCT'
        reasons.append('검색어 자체의 음식 상품 형태' if direct else '뉴스가 검색어 자체를 음식 범주로 직접 설명')
    elif food_brands:
        label, score, entity = 'FOOD', 75, 'FOOD_BRAND'
        reasons.append('검색어와 식품/메뉴 출시가 직접 연결됨; 금융/인사 뉴스 제외')
    elif bound_food or len(produce) >= 2:
        label, score = 'REVIEW', 40 if bound_food else 25
        reasons.append('검색어에 연결된 음식 속성 단서가 있으나 음식 개체라는 정의 부족' if bound_food else '농산물 선발/무게 맥락; 식재료 여부와 유행성은 추가 검토 필요')
    else:
        label, score = 'NON_FOOD', 0
        reasons.append('검색어 자체가 음식임을 뒷받침하는 직접 근거 없음; UNKNOWN을 자동 REVIEW로 처리하지 않음')
    background = sum(e['relation']=='background' for e in evidence)
    if background:
        reasons.append(f'음식/출시 표현이 있는 배경 뉴스 {background}개는 직접 음식 근거에서 제외')
    return {'normalized_name':keyword, 'entity_type':entity, 'classification':label, 'rule_score':score, 'score_basis':SCORE_BASIS, 'reasons':reasons, 'evidence_news':evidence, 'classifier_version':VERSION}
