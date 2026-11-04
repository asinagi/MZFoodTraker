"""Mock app API: the real /api/v1 implementation over a deterministic synthetic dataset.

Every food is named "[MOCK] ...", every observation has origin mock and every response says
data_mode "mock" with a notice; nothing here is a real trend. The dataset is written through the real
B4 ingestion and B5 linking services on a fixed clock (2026-11-03 12:00 KST), into a temporary database
that never touches the configured one, so responses are byte-stable for app development and tests.
Building takes seconds (hundreds of real service transactions), so the result is cached once per code
version in the user's temp directory and every server or test works on its own copy.

    python -m food_trends.app_mock [--port 8030] [--host 0.0.0.0]   serve (LAN binding is allowed: synthetic data only)
    python -m food_trends.app_mock --write-examples docs/app-api/examples
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from .analytics_reader import SQLiteAnalyticsStore
from .analytics_service import FoodAnalytics
from .app_api import AppApi, make_app_server
from .observation_writer import ObservationWriter, ScratchStorage
from .storage import Storage

DATASET_VERSION = 'mock-dataset-v1'
MOCK_NOW = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
KST = timezone(timedelta(hours=9))
TODAY = date(2026, 11, 3)
RSS_START, IG_START = TODAY - timedelta(days=180), TODAY - timedelta(days=40)
FOODS = (  # food_id suffix, name, aliases, kind, created (KST date)
    ('DUJJONKU', '두쫀쿠', ('두쫀쿠', '두바이쫀득쿠키', '두바이 쫀득 쿠키'), 'PRODUCT', date(2026, 5, 1)),
    ('MALATANG', '마라탕', ('마라탕',), 'DISH', date(2026, 5, 1)),
    ('TANGHULU', '탕후루', ('탕후루',), 'DISH', date(2026, 5, 1)),
    ('YAKGWA', '약과', ('약과',), 'PRODUCT', date(2026, 5, 1)),
    ('PISTACHIO', '피스타치오 크림', ('피스타치오 크림', '피스타치오크림'), 'INGREDIENT', date(2026, 5, 1)),
    ('HEUGIMJA', '흑임자 라떼', ('흑임자 라떼', '흑임자라떼'), 'DISH', date(2026, 5, 1)),
    ('DUBAI_COOKIE', '두바이 쿠키', ('두바이쿠키',), 'PRODUCT', date(2026, 5, 1)),
    ('JJAPJJONKU', '짭쫀쿠', ('짭쫀쿠',), 'PRODUCT', date(2026, 9, 1)),
    ('BUTTERTTEOK', '버터떡', ('버터떡',), 'PRODUCT', date(2026, 10, 31)),
)
QUERIES = (  # query id, hashtag, food, numeric media prefix, first tracked day
    ('Q_MOCK_DUJJONKU', '두쫀쿠', 'DUJJONKU', '11', IG_START), ('Q_MOCK_DUBAI_JJONDEUK', '두바이쫀득쿠키', 'DUJJONKU', '12', IG_START),
    ('Q_MOCK_MALATANG', '마라탕', 'MALATANG', '21', IG_START), ('Q_MOCK_TANGHULU', '탕후루', 'TANGHULU', '31', IG_START),
    ('Q_MOCK_YAKGWA', '약과', 'YAKGWA', '41', IG_START), ('Q_MOCK_BUTTERTTEOK', '버터떡', 'BUTTERTTEOK', '51', date(2026, 10, 31)),
)


def food_id(suffix):
    return 'FOOD_MOCK_' + suffix


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), KST)


def rss_entries(day):
    """Deterministic Google Trends RSS feed of one day: food trends, food news titles and non-food filler."""
    j, published = (day - RSS_START).days, at(day, 6).isoformat()
    entries = [{'keyword': '[MOCK] 프로야구 결과', 'traffic': '20,000+'},
               {'keyword': '[MOCK] 아이돌 컴백', 'traffic': '50,000+', 'news': ['컴백 무대 뒤 마라탕 먹방 화제'] if j % 4 == 1 else []}]
    if j >= 100 and j % 2 == 0:
        entries.append({'keyword': '두쫀쿠', 'traffic': '10,000+' if j >= 170 else '5,000+' if j >= 150 else '2,000+'})
    if j >= 120 and j % 3 == 0:
        entries.append({'keyword': '[MOCK] 편의점 신상', 'traffic': '5,000+', 'news': ['두바이 쫀득 쿠키 품절 대란']})
    if 60 <= j <= 140 and j % 5 == 0:
        entries.append({'keyword': '두바이쿠키', 'traffic': '1,000+'})
    if j % 7 == 0:
        entries.append({'keyword': '마라탕', 'traffic': '1,000+'})
    if (j < 90 and j % 2 == 1) or (j >= 90 and j % 9 == 1):
        entries.append({'keyword': '탕후루', 'traffic': '2,000+' if j < 90 else '500+'})
    if j >= 140 and j % 6 == 2:
        entries.append({'keyword': '피스타치오 크림', 'traffic': '1,000+'})
    if j >= 150 and j % 10 == 3:
        entries.append({'keyword': '짭쫀쿠', 'traffic': '500+'})
    if j >= 172:
        entries.append({'keyword': '버터떡', 'traffic': '1,000+' if j >= 177 else '500+'})
    if j % 15 == 7:
        entries.append({'keyword': '약과', 'traffic': '500+'})
    return [dict(e, published_at=published) for e in entries]


def instagram_posts(query, day, evening=False):
    """Posts a hashtag search returns: today's posts plus two of yesterday's (still recent), deterministic counts."""
    query_id, hashtag, food, prefix, first = query
    i = (day - IG_START).days
    count = {'DUJJONKU': 4 + i // 3, 'MALATANG': 10 + i % 3, 'TANGHULU': max(1, 14 - i // 3), 'YAKGWA': 5 + i % 4,
             'BUTTERTTEOK': 3 * ((day - first).days + 1)}[food]
    if prefix == '12':
        count = 2 + i // 6

    def post(n, k):
        tags = [hashtag, '디저트' if food in ('DUJJONKU', 'YAKGWA', 'BUTTERTTEOK') else hashtag + '맛집']
        if prefix == '11' and k == 0:
            tags.append('두바이쫀득쿠키')  # one post per day carries both aliases and is found by both queries
        if food == 'MALATANG' and k % 5 == 4:
            tags.append('탕후루')
        return {'id': '%s%03d%03d' % (prefix, n, k), 'tags': tags}
    posts = [post(i, k) for k in range(count + (1 if evening else 0))]
    if day > first:
        posts += [post(i - 1, k) for k in range(2)]
    if prefix == '12':
        posts += [{'id': '11%03d000' % i, 'tags': ['두쫀쿠', '디저트', '두바이쫀득쿠키']},
                  {'id': '19%03d000' % i, 'tags': ['디저트'], 'caption': '두바이 쿠키 먹방 #디저트 (검색 결과지만 별칭 태그 없음)'}]
    return posts


def coverage(query, day):
    if query[2] == 'YAKGWA' and day == date(2026, 10, 30):
        return 'NONE'
    if query[2] == 'YAKGWA' and day == date(2026, 11, 1):
        return 'LIMITED'
    if query[0] == 'Q_MOCK_DUBAI_JJONDEUK' and day == date(2026, 10, 15):
        return 'NONE'
    return 'COMPLETE'


def events():
    """(instant, kind, payload) in time order."""
    out = []
    for n in range((TODAY - RSS_START).days + 1):
        day = RSS_START + timedelta(days=n)
        out.append((at(day, 9), 'rss', (day, day != date(2026, 7, 20))))
        if TODAY - timedelta(days=14) <= day < TODAY:
            out.append((at(day, 21), 'rss', (day, day != date(2026, 10, 29))))
    for query in QUERIES:
        for n in range((TODAY - query[4]).days):
            day = query[4] + timedelta(days=n)
            out.append((at(day, 13), 'instagram', (query, day, False)))
            if day >= TODAY - timedelta(days=7):
                out.append((at(day, 21, 30), 'instagram', (query, day, True)))
    out.append((at(date(2026, 10, 1), 0), 'merge', ('DUBAI_COOKIE', 'DUJJONKU')))
    out.append((at(date(2026, 10, 21), 9, 30), 'legacy', None))
    return sorted(out, key=lambda e: (e[0], e[1]))


def build(path):
    """Write the synthetic dataset into a new database at path. Returns the Storage."""
    storage = ScratchStorage.create(path)
    # One idle connection held open keeps each service connection from checkpointing the WAL on close.
    with closing(sqlite3.connect(storage.path)):
        _write_dataset(storage)
    return storage


def _write_dataset(storage):
    writer = ObservationWriter(storage, at=at(date(2026, 5, 1), 0))
    for suffix, name, aliases, kind, created in FOODS:
        writer.food(food_id(suffix), '[MOCK] ' + name, aliases, kind, at=at(created, 0))
    writer.relate(food_id('JJAPJJONKU'), food_id('DUJJONKU'), 'VARIANT_OF', at=at(date(2026, 9, 1), 0))
    for query_id, hashtag, *_ in QUERIES:
        writer.query(query_id, hashtag, at=at(date(2026, 9, 20), 0))
    for moment, kind, payload in events():
        if kind == 'rss':
            day, success = payload
            writer.rss_run(moment, rss_entries(day), success=success)
        elif kind == 'instagram':
            query, day, evening = payload
            state = coverage(query, day)
            posts = instagram_posts(query, day, evening) if state != 'NONE' else []
            writer.instagram_run(moment, query[0], [food_id(query[2])], posts, coverage=state)
        elif kind == 'merge':
            writer.merge(food_id(payload[0]), food_id(payload[1]), at=moment)
        else:
            writer.legacy_instagram(moment, [{'id': '29%03d001' % n, 'tags': ['마라탕', '마라탕맛집'],
                                              'seen_at': at(date(2026, 10, 8) + timedelta(days=n), 18)} for n in range(10)])
    writer.purge(MOCK_NOW)


def dataset_key():
    """Changes whenever any package module changes, so a cached dataset never outlives the code that wrote it."""
    digest = hashlib.sha256(DATASET_VERSION.encode())
    for file in sorted(Path(__file__).resolve().parent.glob('*.py')):
        digest.update(file.name.encode() + b'\0' + file.read_bytes())
    return digest.hexdigest()[:20]


def cached_dataset(directory):
    """A private copy of the dataset in directory, built at most once per code version."""
    cache = Path(tempfile.gettempdir()) / 'mzfoodtraker-mock-cache'
    cache.mkdir(exist_ok=True)
    target = cache / ('%s-%s.sqlite3' % (DATASET_VERSION, dataset_key()))
    if not target.is_file():
        partial = cache / ('building-%s.sqlite3' % uuid.uuid4().hex)
        try:
            build(partial)
            with closing(sqlite3.connect(partial)) as db:
                db.execute('PRAGMA journal_mode=DELETE')  # one self-contained file that read-only copies can open anywhere
            os.replace(partial, target)
            for old in cache.glob(DATASET_VERSION + '-*.sqlite3'):
                if old != target:
                    old.unlink(missing_ok=True)
        finally:
            for leftover in cache.glob(partial.name + '*'):
                leftover.unlink(missing_ok=True)
    copy = Path(directory) / 'mock.sqlite3'
    shutil.copyfile(target, copy)
    return Storage(copy)


def mock_api(storage):
    return AppApi(FoodAnalytics(SQLiteAnalyticsStore(storage), clock=lambda: MOCK_NOW, origin='mock'), data_mode='mock')


# Screen-by-screen examples (docs/app-api/examples): (file, summary, method, path, query, headers, in_spec)
D, F = '/api/v1', '/api/v1/foods/'
EXAMPLES = (
    ('health', '연결 확인', 'GET', D + '/health', {}, {}, True),
    ('foods-list', '음식 목록 (가나다순)', 'GET', D + '/foods', {}, {}, True),
    ('foods-search', '음식 검색 (별칭 부분 일치)', 'GET', D + '/foods', {'q': '쿠키'}, {}, True),
    ('foods-search-empty', '검색 결과 없음 (200, 빈 목록)', 'GET', D + '/foods', {'q': '없는음식'}, {}, True),
    ('foods-recent-page-1', '최근 관측순 첫 페이지 (커서 포함)', 'GET', D + '/foods', {'sort': '-last_observed_at', 'limit': '3'}, {}, True),
    ('food-detail', '음식 상세 (별칭·관계·병합 ID·출처 제공 여부)', 'GET', F + 'FOOD_MOCK_DUJJONKU', {}, {}, True),
    ('food-detail-merged-redirect', '병합된 이전 ID로 상세 요청', 'GET', F + 'FOOD_MOCK_DUBAI_COOKIE', {}, {}, True),
    ('food-timeline-7d', '일별 추이 7일 (Instagram 상승, RSS 관측)', 'GET', F + 'FOOD_MOCK_DUJJONKU/timeline', {'period': '7d'}, {}, True),
    ('food-timeline-30d', '일별 추이 30일 (기본값)', 'GET', F + 'FOOD_MOCK_DUJJONKU/timeline', {}, {}, False),
    ('food-timeline-90d-week', '주별 추이 90일 (근거 만료·미관측 주 포함)', 'GET', F + 'FOOD_MOCK_DUJJONKU/timeline',
     {'period': '90d', 'granularity': 'week'}, {}, False),
    ('food-timeline-partial', '부분 관측 (수집 실패일 NOT_OBSERVED, 페이지 상한 LIMITED)', 'GET', F + 'FOOD_MOCK_YAKGWA/timeline',
     {'period': '7d', 'source': 'instagram_graph'}, {}, True),
    ('food-timeline-not-observed', '미관측과 정상 0 (RSS 0, Instagram 미추적)', 'GET', F + 'FOOD_MOCK_HEUGIMJA/timeline', {'period': '7d'}, {}, True),
    ('food-timeline-today', '오늘 포함 (complete=false)', 'GET', F + 'FOOD_MOCK_BUTTERTTEOK/timeline',
     {'period': '7d', 'end_date': '2026-11-03', 'source': 'google_trends_rss'}, {}, True),
    ('food-metrics-7d', '플랫폼별 7일 통계 (증가율, 제공자 표시값)', 'GET', F + 'FOOD_MOCK_DUJJONKU/metrics', {'period': '7d'}, {}, True),
    ('food-metrics-insufficient', '신규 추적 음식 (기간 일부만 관측)', 'GET', F + 'FOOD_MOCK_BUTTERTTEOK/metrics', {'period': '7d'}, {}, True),
    ('food-metrics-legacy', '출처 불명 과거 데이터 별도 표시', 'GET', F + 'FOOD_MOCK_MALATANG/metrics',
     {'period': '30d', 'source': 'instagram_graph'}, {}, True),
    ('rankings-instagram-7d', 'Instagram 7일 순위', 'GET', D + '/trends/rankings', {'source': 'instagram_graph', 'period': '7d'}, {}, True),
    ('rankings-rss-7d', 'Google 트렌드 RSS 7일 순위', 'GET', D + '/trends/rankings', {'source': 'google_trends_rss', 'period': '7d'}, {}, True),
    ('rankings-rss-30d-zero', 'Google 트렌드 RSS 30일 순위 (정상 관측 0 포함)', 'GET', D + '/trends/rankings',
     {'source': 'google_trends_rss', 'period': '30d', 'include_zero': 'true', 'limit': '5'}, {}, False),
    ('sources-status', '데이터 갱신·수집 범위', 'GET', D + '/sources/status', {}, {}, True),
    ('error-invalid-parameter', '지원하지 않는 기간', 'GET', F + 'FOOD_MOCK_DUJJONKU/timeline', {'period': '14d'}, {}, True),
    ('error-unknown-parameter', '계약에 없는 파라미터 (오타)', 'GET', D + '/trends/rankings', {'source': 'instagram_graph', 'perod': '7d'}, {}, True),
    ('error-invalid-cursor', '손상된 커서', 'GET', D + '/foods', {'cursor': 'not-a-cursor'}, {}, False),
    ('error-food-not-found', '없는 음식', 'GET', F + 'FOOD_UNKNOWN', {}, {}, True),
    ('error-source-not-rankable', '정책상 순위 미제공 출처', 'GET', D + '/trends/rankings', {'source': 'youtube'}, {}, True),
    ('error-admin-header-rejected', '관리 토큰을 보낸 요청', 'GET', D + '/health', {}, {'X-MZ-Admin-Token': '<관리 토큰 값>'}, False),
    ('error-forbidden-host', '도메인 이름 Host (DNS 재바인딩 방어)', 'GET', D + '/health', {}, {'Host': 'attacker.example:8030'}, False),
    ('error-method-not-allowed', 'GET 외 메서드', 'POST', D + '/foods', {}, {}, False),
    ('error-route-not-found', '없는 경로', 'GET', '/api/v1/trends', {}, {}, False),
    ('error-unavailable-mock', '저장소 사용 불가 (Mock 서버 X-Mock-Error: 503)', 'GET', D + '/sources/status', {}, {'X-Mock-Error': '503'}, True),
    ('error-internal-mock', '서버 내부 오류 (Mock 서버 X-Mock-Error: 500)', 'GET', D + '/sources/status', {}, {'X-Mock-Error': '500'}, False),
)


def examples(api):
    """[(file stem, document)] for EXAMPLES; the page-2 example follows the page-1 cursor."""
    from urllib.parse import urlencode
    out = []
    for stem, summary, method, path, query, headers, in_spec in EXAMPLES:
        request = dict(headers, **({'Host': headers['Host']} if 'Host' in headers else {'Host': '127.0.0.1:8030'}))
        status, body, _ = api.handle(method, path + ('?' + urlencode(query) if query else ''), request)
        out.append((stem, {'summary': summary, 'request': {'method': method, 'path': path, 'query': query, 'headers': headers},
                           'status': status, 'in_spec': in_spec, 'response': body}))
        if stem == 'foods-recent-page-1':
            follow = dict(query, cursor=body['page']['next_cursor'])
            status, body, _ = api.handle('GET', path + '?' + urlencode(follow), {'Host': '127.0.0.1:8030'})
            out.append(('foods-recent-page-2', {'summary': '최근 관측순 두 번째 페이지 (next_cursor 사용)',
                                                'request': {'method': 'GET', 'path': path, 'query': follow, 'headers': {}},
                                                'status': status, 'in_spec': True, 'response': body}))
    return out


def write_examples(directory, api):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for stem, document in examples(api):
        (directory / (stem + '.json')).write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
        written.append(stem)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description='앱 API Mock 서버 (가상 데이터, 외부 호출·운영 DB 없음)')
    parser.add_argument('--host', default='127.0.0.1', help='실기기 테스트 시 0.0.0.0 또는 PC의 LAN IP (가상 데이터만 제공)')
    parser.add_argument('--port', type=int, default=8030)
    parser.add_argument('--write-examples', metavar='DIR', help='화면별 예시 JSON을 DIR에 쓰고 종료')
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix='mzfoodtraker-mock-') as directory:
        print('Preparing %s (fixed clock %s; the first run builds it, about 15 s)...' % (DATASET_VERSION, MOCK_NOW.isoformat()), flush=True)
        api = mock_api(cached_dataset(directory))
        if args.write_examples:
            print(json.dumps({'written': write_examples(args.write_examples, api)}, ensure_ascii=False))
            return 0
        server = make_app_server(api, host=args.host, port=args.port, any_origin=True, allow_lan=True)
        shown = '127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host
        print('Mock app API (synthetic data): http://%s:%d/api/v1/health' % (shown, server.server_port), flush=True)
        if args.host not in ('127.0.0.1', 'localhost', '::1'):
            print('LAN binding: same-network devices can reach this mock server (synthetic data only).', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
