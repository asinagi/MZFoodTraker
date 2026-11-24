"""식품안전나라 공공데이터를 승인된 음식(FOOD_ID)에 연결하는 모듈.

foodsafety.py가 '음식 이름 하나'를 조회했다면, 이 모듈은 기존 B8 분석 계층이 보는 승인 음식과 활성 별칭을
그대로 가져와 음식 ID 단위로 조회하고, 앱 API(GET /api/v1/foods/{food_id}/public-supply)가 읽을 기록을 만든다.

- 기존 DB는 읽기만 한다. 음식 목록은 app_api.open_store()가 여는 읽기 전용 저장소(SQLite, Oracle 테스트·운영)에서
  FoodAnalytics.foods()로 가져온다. 스키마·마이그레이션·Oracle 테이블은 바꾸지 않는다.
- 결과는 음식 ID별 JSON 파일(data/foodsafety/by_food/<FOOD_ID>.json)에만 저장한다(data/는 git 제외 대상).
  Oracle 저장은 팀 합의 후 별도 작업으로 남긴다.
- 실제 외부 호출은 sync를 명시적으로 실행할 때만 한다. 앱 API 서버는 파일을 읽기만 하고 외부 호출을 하지 않는다.

    python -m food_trends.public_supply --store sqlite --database data/trends.sqlite3            # 승인 음식 전체
    python -m food_trends.public_supply --store oracle-production --food FOOD_...                 # 한 음식만
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import foodsafety
from .paths import PROJECT_ROOT

RECORD_DIR = PROJECT_ROOT / 'data' / 'foodsafety' / 'by_food'
RECORD_VERSION = 1
FOOD_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
PROVIDER = '식품안전나라 (식품의약품안전처)'
DATASETS = ('I1250', 'C002')
TOP_INGREDIENTS = 10
RECENT_PRODUCTS = 5
NOTES = (
    '품목제조보고 대상 제품만 포함합니다. 매장에서 직접 만들어 파는 메뉴(즉석판매)는 대부분 없습니다.',
    '이름 포함 검색이라 다른 음식이 섞일 수 있어 derived(파생)로 구분합니다(이름 규칙 기반 추정).',
    'share는 그 재료를 쓴 제품의 비율이며 함량이 아닙니다. estimated=true는 식품유형 표기로 미루어 본 해석입니다.',
    'permitted_on은 품목제조보고 허가(신고)일이며 실제 출시일과 다를 수 있습니다.',
)


def _day(yyyymmdd):
    s = re.sub(r'\D', '', str(yyyymmdd or ''))
    return f'{s[:4]}-{s[4:6]}-{s[6:8]}' if len(s) >= 8 else None


def record_from_report(food_ref, report):
    """foodsafety.build_report() 결과를 앱 API용 기록으로 줄인다. food_ref: {food_id, canonical_name, entity_kind}."""
    months = sorted(set(report['monthly_permits']) | set(report['monthly_permits_core']))
    core = [p for p in report['products'] if p['match'] == 'core']
    return {
        'record_version': RECORD_VERSION,
        'food_id': food_ref['food_id'],
        'canonical_name': food_ref['canonical_name'],
        'searched_names': list(report['aliases_searched']),
        'fetched_at': report['fetched_at'],
        'counts': {
            'products': report['counts']['products'],
            'core_products': report['counts']['core'],
            'derived_products': report['counts']['derived'],
            'with_raw_materials': report['counts']['with_raw_materials'],
        },
        'first_permitted_on': _day(report['first_permitted_on']),
        'latest_permitted_on': _day(report['latest_permitted_on']),
        'monthly_permits': [{'month': m, 'products': report['monthly_permits'].get(m, 0),
                             'core_products': report['monthly_permits_core'].get(m, 0)} for m in months],
        'ingredient_basis': 'core' if report['top_ingredients_basis'] == 'core' else 'all',
        'top_ingredients': [{'ingredient': i['ingredient'], 'products': i['products'], 'share': i['share'],
                             'estimated': '추정' in i['ingredient']} for i in report['top_ingredients'][:TOP_INGREDIENTS]],
        'recent_products': [{'name': p['name'] or '', 'maker': p['maker'] or '', 'food_type': p['food_type'] or '',
                             'permitted_on': _day(p['permitted_on']), 'match': p['match']}
                            for p in (core or report['products'])[:RECENT_PRODUCTS]],
    }


def api_data(food_ref, record, origin='live_api'):
    """앱 API 응답의 data. record가 없으면 NOT_COLLECTED (0건과 다름: 아직 조회하지 않았다는 뜻)."""
    ref = {'food_id': food_ref['food_id'], 'canonical_name': food_ref['canonical_name'], 'entity_kind': food_ref['entity_kind']}
    provider = {'name': PROVIDER, 'datasets': list(DATASETS), 'origin': origin}
    if record is None:
        return {'food': ref, 'state': 'NOT_COLLECTED', 'provider': provider, 'fetched_at': None, 'searched_names': [],
                'counts': None, 'first_permitted_on': None, 'latest_permitted_on': None, 'monthly_permits': [],
                'ingredient_basis': None, 'top_ingredients': [], 'recent_products': [], 'notes': list(NOTES)}
    data = {k: record[k] for k in ('fetched_at', 'searched_names', 'counts', 'first_permitted_on', 'latest_permitted_on',
                                   'monthly_permits', 'ingredient_basis', 'top_ingredients', 'recent_products')}
    return dict(data, food=ref, state='AVAILABLE', provider=provider, notes=list(NOTES))


class FilePublicSupply:
    """음식 ID별 JSON 파일 저장소. 앱 API는 get()만 쓴다(읽기 전용)."""

    def __init__(self, directory=RECORD_DIR):
        self.directory = Path(directory)

    def _path(self, food_id):
        if not FOOD_ID.fullmatch(food_id or ''):
            raise ValueError('invalid food_id')
        return self.directory / (food_id + '.json')

    def get(self, food_id):
        path = self._path(food_id)
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None  # 깨진 파일은 '수집 안 됨'으로 보고 다시 sync하게 한다
        if record.get('record_version') != RECORD_VERSION or record.get('food_id') != food_id:
            return None
        return record

    def put(self, record):
        path = self._path(record['food_id'])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)


class StaticPublicSupply:
    """메모리 저장소 (Mock 서버·테스트용)."""

    def __init__(self, records=()):
        self.records = {r['food_id']: r for r in records}

    def get(self, food_id):
        return self.records.get(food_id)


def approved_foods(analytics, food_ids=None):
    """승인된 활성 음식 [{food_id, canonical_name, entity_kind, aliases}] (B8 목록과 같은 기준)."""
    foods = analytics.foods(None, None, 'name')
    if food_ids:
        wanted = set(food_ids)
        foods = [f for f in foods if f['food_id'] in wanted]
    return foods


def sync(analytics, store, client=None, food_ids=None, refresh=False, max_rows=foodsafety.DEFAULT_MAX_ROWS,
         cache_dir=foodsafety.CACHE_DIR):
    """승인 음식마다 대표명+별칭으로 식품안전나라를 조회해 store에 기록. 음식 하나가 실패해도 나머지는 계속한다."""
    results = []
    for food in approved_foods(analytics, food_ids):
        aliases = [a for a in food['aliases'] if a != food['canonical_name']]
        try:
            report, cached = foodsafety.lookup(food['canonical_name'], aliases, client=client, max_rows=max_rows,
                                               refresh=refresh, cache_dir=cache_dir)
        except foodsafety.FoodSafetyError as exc:
            results.append({'food_id': food['food_id'], 'canonical_name': food['canonical_name'], 'ok': False, 'error': str(exc)})
            continue
        store.put(record_from_report(food, report))
        results.append({'food_id': food['food_id'], 'canonical_name': food['canonical_name'], 'ok': True, 'cached': cached,
                        'products': report['counts']['products']})
    return results


def main(argv=None):
    from .analytics_service import FoodAnalytics
    from .app_api import STORES, StartRefused, open_store
    parser = argparse.ArgumentParser(description='승인된 음식(FOOD_ID)별로 식품안전나라 공급·원재료 정보를 조회해 앱 API용 파일로 저장')
    parser.add_argument('--store', choices=STORES, default='sqlite', help='음식 목록을 읽을 저장소 (읽기 전용)')
    parser.add_argument('--database', help='sqlite 저장소의 DB 경로')
    parser.add_argument('--food', action='append', default=[], help='이 음식 ID만 조회 (여러 번 사용 가능)')
    parser.add_argument('--refresh', action='store_true', help='식품안전나라 캐시를 무시하고 다시 조회')
    parser.add_argument('--max-rows', type=int, default=foodsafety.DEFAULT_MAX_ROWS, help='검색어당 최대 조회 건수')
    parser.add_argument('--out', default=str(RECORD_DIR), help='기록 폴더 (기본 data/foodsafety/by_food)')
    args = parser.parse_args(argv)
    if args.store == 'sqlite' and not args.database:
        parser.error('sqlite 저장소에는 --database가 필요합니다')
    if not 1 <= args.max_rows <= 20000:
        parser.error('--max-rows는 1~20000')
    try:
        store, close = open_store(args.store, args.database)
    except StartRefused as exc:
        print(json.dumps({'synced': False, 'store': args.store, 'error': exc.reason}, ensure_ascii=False))
        return 2
    try:
        results = sync(FoodAnalytics(store), FilePublicSupply(args.out), food_ids=args.food, refresh=args.refresh,
                       max_rows=args.max_rows)
    finally:
        close()
    print(json.dumps({'synced': True, 'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'foods': results},
                     ensure_ascii=False, indent=2))
    return 0 if all(r['ok'] for r in results) else 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
