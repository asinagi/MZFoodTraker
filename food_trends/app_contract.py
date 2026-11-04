"""B8 mobile app API contract: one OpenAPI 3.0.3 document shared by the server, the mock server and the docs.

The contract is read-only and framework neutral. Values that the analytics layer and the HTTP layer
must agree on (periods, enums, error codes, source labels) live here so the published document and
the implementation cannot drift. A small schema checker validates responses and examples against
the document without third-party packages. `python -m food_trends.app_contract --docs docs/app-api`
regenerates openapi.yaml/openapi.json (with examples from docs/app-api/examples); --check verifies them.
"""
import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

API_VERSION = '1.0.0'
BASE_PATH = '/api/v1'
TIMEZONE = 'Asia/Seoul'
PERIODS = {'7d': 7, '30d': 30, '90d': 90}
ENTITY_KINDS = ('DISH', 'PRODUCT', 'INGREDIENT', 'FOOD_CONCEPT')
SOURCE_CODES = ('google_trends_rss', 'instagram_graph', 'youtube', 'naver_datalab')
SOURCE_NAMES = {'google_trends_rss': 'Google 트렌드 RSS', 'instagram_graph': 'Instagram', 'youtube': 'YouTube',
                'naver_datalab': 'NAVER 데이터랩'}
ITEM_LABELS = {'google_trends_rss': '트렌드 항목', 'instagram_graph': '게시물'}
AVAILABILITY = ('INCLUDED', 'POLICY_BLOCKED', 'NOT_STORED')
METRIC_CODE, METRIC_UNIT = 'confirmed_items', 'distinct_items'
DAY_STATES = ('OBSERVED', 'NOT_OBSERVED', 'EVIDENCE_EXPIRED')
VALUE_STATES = ('OBSERVED', 'NOT_OBSERVED', 'INSUFFICIENT_COVERAGE', 'EVIDENCE_EXPIRED')
PERIOD_STATES = ('OBSERVED', 'NOT_OBSERVED', 'INSUFFICIENT_COVERAGE')
COVERAGES = ('COMPLETE', 'LIMITED', 'PARTIAL', 'NONE')
CHANGE_REASONS = ('CURRENT_PERIOD_INSUFFICIENT', 'PREVIOUS_PERIOD_INSUFFICIENT', 'COVERAGE_LIMITED', 'PREVIOUS_ZERO')
SORTS = ('relevance', 'name', '-last_observed_at')
ERRORS = {
    'INVALID_PARAMETER': (400, '요청 파라미터를 확인하세요.'),
    'INVALID_CURSOR': (400, '페이지 커서가 올바르지 않거나 다른 조건의 커서입니다. 첫 페이지부터 다시 요청하세요.'),
    'ADMIN_TOKEN_NOT_ACCEPTED': (400, '앱 API는 관리 토큰을 받지 않습니다. 관리 토큰을 앱에 넣지 마세요.'),
    'FORBIDDEN_HOST': (403, '허용되지 않은 Host로 요청했습니다. IP 주소 또는 localhost로 접속하세요.'),
    'FOOD_NOT_FOUND': (404, '음식을 찾을 수 없습니다.'),
    'ROUTE_NOT_FOUND': (404, '지원하지 않는 경로입니다.'),
    'METHOD_NOT_ALLOWED': (405, '앱 API는 GET 요청만 지원합니다.'),
    'SOURCE_NOT_RANKABLE': (422, '이 출처는 정책상 순위를 제공하지 않습니다.'),
    'INTERNAL_ERROR': (500, '서버 내부 오류입니다. 잠시 후 다시 시도하세요.'),
    'DATA_UNAVAILABLE': (503, '데이터 저장소를 사용할 수 없습니다. 잠시 후 다시 시도하세요.'),
}
DETAIL_REASONS = ('unknown_parameter', 'repeated_parameter', 'required', 'unsupported_value', 'invalid_format', 'out_of_range',
                  'requires_parameter', 'malformed', 'mismatched_query', 'forbidden_header', 'not_ip_or_localhost', 'not_found',
                  'unknown_route', 'method_not_allowed', 'policy_blocked', 'not_stored', 'schema_not_ready', 'storage_unavailable',
                  'internal', 'mock_simulated')
DATE_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')


# Schema builders keep the document compact and every schema in the OpenAPI 3.0 subset the checker understands.

def ref(name):
    return {'$ref': '#/components/schemas/' + name}


def obj(properties, description=None, nullable=False):
    schema = {'type': 'object', 'required': sorted(properties), 'properties': properties}
    if description:
        schema['description'] = description
    if nullable:
        schema['nullable'] = True
    return schema


def scalar(kind, description=None, nullable=False, **extra):
    schema = {'type': kind, **extra}
    if description:
        schema['description'] = description
    if nullable:
        schema['nullable'] = True
    return schema


def enum(values, description=None, nullable=False):
    values = list(values) + ([None] if nullable else [])
    return scalar('string', description, nullable, enum=values)


def text(description=None, nullable=False, **extra):
    return scalar('string', description, nullable, **extra)


def count(description=None, nullable=False):
    return scalar('integer', description, nullable, minimum=0)


def day(description=None, nullable=False):
    return scalar('string', description, nullable, format='date')


def moment(description=None, nullable=False):
    note = 'UTC, 초 단위 RFC 3339 (예: 2026-11-02T03:00:00Z).'
    return scalar('string', (description + ' ' + note) if description else note, nullable, format='date-time')


def array(items, description=None):
    schema = {'type': 'array', 'items': items}
    if description:
        schema['description'] = description
    return schema


def _schemas():
    source = enum(SOURCE_CODES, '출처 코드. google_trends_rss·instagram_graph만 B8 집계에 포함됩니다.')
    availability = enum(AVAILABILITY, 'INCLUDED=B8 집계 제공, POLICY_BLOCKED=정책상 음식 분석 비활성(YouTube), '
                                      'NOT_STORED=저장하지 않는 요청 범위 지수라 집계 제외(NAVER).')
    reason = text('availability가 INCLUDED가 아닐 때의 사유. INCLUDED이면 null.', nullable=True)
    coverage = enum(COVERAGES, '수집 범위. COMPLETE=요청 전체 수신, LIMITED=페이지 상한 도달, PARTIAL=일부 회차·페이지만 성공, '
                               'NONE=성공 관측 없음. LIMITED·PARTIAL 값은 하한값입니다.')
    day_counts = obj({'observed': count('관측된 일수'), 'not_observed': count('성공 관측이 없는 일수'),
                      'evidence_expired': count('근거 보존 기한이 지나 값을 다시 만들 수 없는 일수')},
                     '기간에 포함된 일자의 상태별 개수.')
    period_value = obj({
        'value': count('기간 전체 고유 콘텐츠 수. 기간 집합을 다시 계산하며 일별 값을 더하지 않습니다. '
                       'value_state가 OBSERVED일 때만 숫자이고 0은 정상 관측 0입니다.', nullable=True),
        'value_state': enum(PERIOD_STATES, 'OBSERVED=기간 모든 일자 관측, INSUFFICIENT_COVERAGE=일부 일자 미관측·근거 만료·진행 중 일자 포함, '
                                           'NOT_OBSERVED=관측 일자 없음.'),
        'coverage': coverage, 'day_counts': day_counts,
        'includes_incomplete_day': scalar('boolean', '기간에 오늘(KST, 수집 진행 중)이 포함되면 true.')},
        '한 기간의 값과 관측 상태.')
    change = obj({
        'state': enum(('COMPARABLE', 'NOT_COMPARABLE'), '두 기간이 모두 OBSERVED이고 COMPLETE일 때만 COMPARABLE.'),
        'reason': enum(CHANGE_REASONS, 'NOT_COMPARABLE 사유, 또는 COMPARABLE이지만 ratio가 null인 사유(PREVIOUS_ZERO).', nullable=True),
        'absolute': scalar('integer', 'current.value - previous.value. COMPARABLE일 때만 숫자.', nullable=True),
        'ratio': scalar('number', '(current - previous) / previous. 이전 값이 0이면 null.', nullable=True)},
        '이전 동일 길이 기간 대비 변화. 미관측 기간을 0으로 보지 않습니다.')
    metric = obj({
        'metric_code': enum((METRIC_CODE,), 'confirmed_items=승인된 정확 별칭이 확인된 고유 콘텐츠 수(FOOD_MENTION 기준). '
                                            '검색 결과 일치(ITEM_QUERY_MATCH)는 포함하지 않습니다.'),
        'unit': enum((METRIC_UNIT,), '고유 콘텐츠 개수. 같은 콘텐츠는 별칭이 여러 개여도 한 번만 셉니다.'),
        'item_label': text('화면 표시용 콘텐츠 단위 (트렌드 항목, 게시물).')}, '지표 정의.')
    food_ref = obj({'food_id': text('안정적인 음식 ID. 병합되어도 이전 ID는 상세 조회에서 대상 음식으로 안내됩니다.'),
                    'canonical_name': text('대표 음식명'), 'entity_kind': enum(ENTITY_KINDS, '음식 유형')}, '음식 요약 참조.')
    period = obj({'start_date': day('시작일(포함, KST)'), 'end_date': day('종료일(포함, KST)'), 'days': count('일수'),
                  'timezone': enum((TIMEZONE,), '일자 계산 기준 시간대 (UTC+09:00 고정)')}, '측정 기간.')
    legacy = obj({'value': count('출처 회차가 기록되지 않은 과거 이관 데이터에서 확인된 고유 콘텐츠 수. 본 값에 더하지 마세요.'),
                  'coverage': enum(('UNKNOWN',), '항상 UNKNOWN'), 'origin': enum(('legacy_unknown',), '항상 legacy_unknown')},
                 '출처 불명 과거 데이터. 관측 범위를 알 수 없어 별도로만 표시합니다. 없으면 null.', nullable=True)
    return {
        'Meta': obj({
            'api_version': text('계약 버전 (SemVer). 1.x 안에서는 필드 추가만 합니다.'),
            'data_mode': enum(('database', 'mock'), 'database=설정된 DB에서 계산, mock=가상 데이터 Mock 서버. mock은 실제 유행이 아닙니다.'),
            'observation_origin': enum(('live_api', 'mock'), '집계에 포함한 관측 회차의 origin. 운영 기본값은 live_api.'),
            'generated_at': moment('응답 생성 시각.'),
            'timezone': enum((TIMEZONE,), '일자 계산 시간대'),
            'notice': text('Mock·테스트 데이터일 때 화면에 함께 표시할 안내문. 실제 데이터면 null.', nullable=True)},
            '모든 응답의 공통 메타데이터.'),
        'ErrorDetail': obj({'parameter': text('문제가 된 파라미터 또는 헤더 이름', nullable=True),
                            'reason': text('기계 판독용 사유: ' + ', '.join(DETAIL_REASONS)),
                            'allowed': array(text(), '허용 값 목록이 있으면 제공') | {'nullable': True}}, '오류 세부 항목.'),
        'Error': obj({'code': enum(ERRORS, '오류 코드 (docs/app-api/ERRORS.md)'), 'message': text('사용자에게 보여도 되는 한국어 설명'),
                      'details': array(ref('ErrorDetail'))}, '오류 본문.'),
        'ErrorResponse': obj({'error': ref('Error'), 'meta': ref('Meta')}, '모든 오류 응답 형식.'),
        'Page': obj({'limit': scalar('integer', '이번 요청의 최대 항목 수', minimum=1, maximum=100), 'total_items': count('조건에 맞는 전체 항목 수'),
                     'has_more': scalar('boolean', '다음 페이지 존재 여부'),
                     'next_cursor': text('다음 페이지 요청에 그대로 넣을 불투명 커서. 마지막 페이지면 null.', nullable=True)},
                    '커서 기반 페이지 정보. 커서는 같은 조건의 요청에만 사용할 수 있습니다.'),
        'FoodRef': food_ref,
        'Period': period,
        'MetricDefinition': metric,
        'SeriesMetric': dict(metric, nullable=True, description='지표 정의. availability가 INCLUDED가 아니면 null.'),
        'PeriodValue': period_value,
        'Change': change,
        'LegacyUnknown': legacy,
        'Health': obj({'status': enum(('ok', 'unavailable'), 'ok=조회 가능, unavailable=저장소 준비 안 됨'),
                       'api_version': text('계약 버전'), 'data_mode': enum(('database', 'mock')),
                       'observation_origin': enum(('live_api', 'mock')), 'schema_ready': scalar('boolean', '저장소 스키마 준비 여부')},
                      '연결 확인용 상태.'),
        'HealthResponse': obj({'data': ref('Health'), 'meta': ref('Meta')}),
        'FoodMatch': obj({'field': enum(('canonical_name', 'alias'), '일치한 필드'), 'text': text('일치한 이름'),
                          'kind': enum(('exact', 'prefix', 'contains'), '일치 방식 (공백·대소문자 무시)')},
                         '검색어(q)가 있을 때 일치 정보. q가 없으면 null.', nullable=True),
        'FoodSummary': obj({
            'food_id': food_ref['properties']['food_id'], 'canonical_name': text('대표 음식명'), 'entity_kind': enum(ENTITY_KINDS, '음식 유형'),
            'aliases': array(text(), '승인된 별칭 표기(활성만). 대표 음식명도 포함될 수 있습니다.'),
            'last_observed_at': moment('포함 출처에서 이 음식의 확인된 콘텐츠를 마지막으로 관측한 시각. 없으면 null.', nullable=True),
            'match': ref('FoodMatch')}, '음식 목록 항목.'),
        'FoodList': obj({'items': array(ref('FoodSummary')),
                         'query': obj({'q': text('정규화된 검색어', nullable=True), 'kind': enum(ENTITY_KINDS, nullable=True),
                                       'sort': enum(SORTS)}, '적용된 조건.')}),
        'FoodListResponse': obj({'data': ref('FoodList'), 'page': ref('Page'), 'meta': ref('Meta')}),
        'Alias': obj({'text': text('별칭 표기'), 'scope': text('별칭 범위 (예: food:ko)')}, '승인된 별칭.'),
        'Relation': obj({'relation_type': enum(('VARIANT_OF', 'HAS_VARIANT', 'RELATED_TO'),
                                               'VARIANT_OF=이 음식이 food의 파생, HAS_VARIANT=food가 이 음식의 파생, RELATED_TO=연관(대칭)'),
                         'food': ref('FoodRef')}, '사람이 승인한 음식 관계. 관계 음식의 수치는 합산하지 않습니다.'),
        'Redirect': obj({'requested_food_id': text('요청한 이전 음식 ID'), 'reason': enum(('MERGED',), '병합으로 대상 음식에 통합됨')},
                        '요청 ID가 병합되어 대상 음식을 반환했을 때만 제공. 아니면 null.', nullable=True),
        'SourceAvailability': obj({'source_code': source, 'source_name': text('출처 표시명'), 'availability': availability,
                                   'availability_reason': reason,
                                   'last_observed_at': moment('이 출처에서 이 음식의 확인된 콘텐츠를 마지막으로 관측한 시각.', nullable=True)},
                                  '출처별 제공 여부.'),
        'Freshness': obj({'last_observed_at': moment('포함 출처 전체에서 마지막 관측 시각.', nullable=True),
                          'reprocessing_pending': scalar('boolean', '승인된 별칭·병합 변경이 아직 관측 재처리에 반영되지 않았으면 true. '
                                                                     '반영 후 수치가 바뀔 수 있습니다.')}, '데이터 갱신 상태.'),
        'FoodDetail': obj({
            'food_id': food_ref['properties']['food_id'], 'canonical_name': text('대표 음식명'), 'entity_kind': enum(ENTITY_KINDS, '음식 유형'),
            'status': enum(('ACTIVE', 'RETIRED'), '음식 상태'), 'aliases': array(ref('Alias'), '승인된 활성 별칭'),
            'relations': array(ref('Relation')), 'merged_food_ids': array(text(), '이 음식으로 병합된 이전 음식 ID'),
            'redirect': ref('Redirect'), 'created_at': moment('음식 등록 시각.'), 'updated_at': moment('음식 정보 변경 시각.'),
            'sources': array(ref('SourceAvailability')), 'freshness': ref('Freshness')}, '음식 상세.'),
        'FoodDetailResponse': obj({'data': ref('FoodDetail'), 'meta': ref('Meta')}),
        'RunCounts': obj({'successful': count('성공(관측) 회차 수'), 'failed': count('실패(미관측) 회차 수')}, '해당 구간의 관측 범위 회차.'),
        'TimelinePoint': obj({
            'start_date': day('구간 시작일(KST)'), 'end_date': day('구간 종료일(KST). 일 단위면 start_date와 같습니다.'),
            'days': count('구간 일수 (일=1, 주=1~7)'),
            'value': count('구간 고유 콘텐츠 수. value_state가 OBSERVED일 때만 숫자.', nullable=True),
            'value_state': enum(VALUE_STATES, '일: OBSERVED·NOT_OBSERVED·EVIDENCE_EXPIRED, 주: OBSERVED·NOT_OBSERVED·INSUFFICIENT_COVERAGE.'),
            'coverage': coverage, 'observed_days': count('구간 안에서 관측된 일수'),
            'complete': scalar('boolean', 'false=오늘(수집 진행 중) 포함 또는 기간 경계로 잘린 주.'),
            'runs': ref('RunCounts'),
            'legacy_unknown_value': count('출처 불명 과거 데이터의 고유 콘텐츠 수(본 값에 더하지 않음). 없으면 null.', nullable=True)},
            '추이 그래프의 한 점.'),
        'TimelineSeries': obj({'source_code': source, 'source_name': text('출처 표시명'), 'availability': availability,
                               'availability_reason': reason, 'metric': ref('SeriesMetric'),
                               'points': array(ref('TimelinePoint'), 'availability가 INCLUDED일 때만 채워집니다.')}, '출처별 추이.'),
        'Timeline': obj({'food': ref('FoodRef'), 'period': ref('Period'), 'granularity': enum(('day', 'week'), '집계 단위'),
                         'series': array(ref('TimelineSeries'))}),
        'TimelineResponse': obj({'data': ref('Timeline'), 'meta': ref('Meta')}),
        'PeriodMetric': obj({'metric_code': metric['properties']['metric_code'], 'unit': metric['properties']['unit'],
                             'item_label': text('콘텐츠 단위 표시명'), 'origin': enum(('live_api', 'mock'), '관측 회차 origin'),
                             'current': ref('PeriodValue'), 'previous': ref('PeriodValue'), 'change': ref('Change'),
                             'rankable': scalar('boolean', '순위 API에서 사용할 수 있는 지표인지')}, '기간 지표.'),
        'ProviderValue': obj({'metric_code': enum(('rss.approx_traffic',), 'Google 트렌드 RSS가 표시한 대략적 검색량 문구'),
                              'unit': enum(('approximate_search_traffic_text',)), 'raw_value': text('제공자 표시 문자열 (예: 1,000+). 숫자로 바꾸거나 합산하지 마세요.'),
                              'keyword': text('트렌드 검색어. 음식 별칭과 정확히 같은 항목만 제공합니다.'),
                              'observed_at': moment('마지막 관측 시각.'), 'aggregatable': scalar('boolean', '항상 false')},
                             '제공자 표시값. 기간 안에 관측된 항목별 최신 값 (최대 5개, 최근순).'),
        'MetricQuality': obj({'checked_items': count('기간 안 확인된 고유 콘텐츠 수'), 'warn_items': count('품질 경고(WARN)가 있는 콘텐츠 수'),
                              'failed_runs': count('기간 안 실패(미관측) 회차 수')}, '기간 품질 요약. availability가 INCLUDED가 아니면 null.', nullable=True),
        'SourceMetrics': obj({'source_code': source, 'source_name': text('출처 표시명'), 'availability': availability,
                              'availability_reason': reason, 'metrics': array(ref('PeriodMetric'), 'INCLUDED일 때만 채워집니다.'),
                              'provider_values': array(ref('ProviderValue'), 'Google 트렌드 RSS만 제공합니다.'),
                              'quality': ref('MetricQuality'),
                              'last_observed_at': moment('이 출처의 마지막 관측 시각.', nullable=True),
                              'legacy_unknown': ref('LegacyUnknown')}, '출처별 기간 통계. 출처끼리 합산하지 않습니다.'),
        'FoodMetrics': obj({'food': ref('FoodRef'), 'period': ref('Period'), 'previous_period': ref('Period'),
                            'sources': array(ref('SourceMetrics'))}),
        'FoodMetricsResponse': obj({'data': ref('FoodMetrics'), 'meta': ref('Meta')}),
        'RankingBasis': obj({'source_code': source, 'source_name': text('출처 표시명'), 'metric': ref('MetricDefinition'),
                             'period': ref('Period'), 'previous_period': ref('Period'),
                             'scope': enum(('SINGLE_SOURCE',), '한 출처·한 단위·한 기간만 비교합니다. 종합 점수는 제공하지 않습니다.'),
                             'observation_scope': enum(('SHARED_FEED', 'PER_FOOD_QUERY'), 'SHARED_FEED=모든 음식이 같은 피드로 관측(RSS), '
                                                       'PER_FOOD_QUERY=음식별 검색 회차가 있는 날만 관측(Instagram)'),
                             'notes': array(text(), '해석 주의 문구')}, '순위 기준.'),
        'RankingEntry': obj({'rank': scalar('integer', '순위 (동점은 같은 순위, 다음 순위는 건너뜀: 1,2,2,4)', minimum=1),
                             'food': ref('FoodRef'), 'current': ref('PeriodValue'), 'previous': ref('PeriodValue'),
                             'change': ref('Change')}, '순위 항목. current.value_state는 항상 OBSERVED.'),
        'RankingExclusions': obj({'insufficient_coverage': count('기간 일부만 관측되어 제외된 음식 수'),
                                  'not_observed': count('기간 관측이 없어 제외된 음식 수'),
                                  'zero_value': count('include_zero=false로 제외된 정상 관측 0 음식 수')}, '순위에서 제외된 음식 수.'),
        'Rankings': obj({'basis': ref('RankingBasis'), 'items': array(ref('RankingEntry')), 'excluded': ref('RankingExclusions')}),
        'RankingsResponse': obj({'data': ref('Rankings'), 'page': ref('Page'), 'meta': ref('Meta')}),
        'SourcePolicy': obj({'policy_version': text('출처 정책 버전'), 'retention_days': count('근거 보존 일수. 제한 없으면 null.', nullable=True),
                             'mention_extraction': enum(('ALLOWED', 'POLICY_BLOCKED', 'NOT_APPLICABLE'), '음식 언급 추출 허용 여부'),
                             'metric_storage': enum(('STORED', 'TRANSIENT_ONLY', 'NONE'), '지표 저장 정책')}, '출처 정책.'),
        'WindowStats': obj({'runs_total': count('조회 기간 회차 수'), 'runs_successful': count('성공(관측) 회차 수'),
                            'runs_failed': count('실패 회차 수'), 'observed_days': count('성공 회차가 있는 일수'),
                            'coverage_counts': obj({c: count() for c in COVERAGES}, 'coverage별 회차 수')},
                           '조회 기간 수집 통계. INCLUDED가 아니면 null.', nullable=True),
        'SourceQuality': obj({'fail_results': count('품질 FAIL 결과 수'), 'warn_results': count('품질 WARN 결과 수'),
                              'rejected_items': count('품질 FAIL로 적재하지 않은 레코드 수')}, '조회 기간 품질 요약. INCLUDED가 아니면 null.', nullable=True),
        'LegacyStatus': obj({'runs': count('출처 불명 논리 회차 수'), 'items': count('출처 불명 콘텐츠 수'), 'coverage': enum(('UNKNOWN',))},
                            '출처 불명 과거 데이터 존재 여부. 없으면 null.', nullable=True),
        'SourceStatus': obj({'source_code': source, 'source_name': text('출처 표시명'), 'availability': availability,
                             'availability_reason': reason, 'policy': ref('SourcePolicy'),
                             'observation_scope': enum(('SHARED_FEED', 'PER_FOOD_QUERY'), nullable=True),
                             'last_run_at': moment('마지막 회차 시각.', nullable=True),
                             'last_successful_run_at': moment('마지막 성공 회차 시각.', nullable=True),
                             'latest_coverage': enum(COVERAGES, '마지막 회차 coverage', nullable=True),
                             'window_stats': ref('WindowStats'), 'quality': ref('SourceQuality'), 'legacy_unknown': ref('LegacyStatus')},
                            '출처 수집 상태. YouTube·NAVER는 정책 정보만 제공합니다.'),
        'Aggregation': obj({'mode': enum(('read_time',), '요청 시 계산 (저장 집계 없음)'),
                            'materialized': scalar('boolean', '저장 집계 사용 여부 (현재 false)'),
                            'pending_entity_changes': count('관측 재처리 대기 중인 별칭·병합 변경 수')}, '집계 방식.'),
        'SourcesStatus': obj({'data_through': moment('포함 출처의 마지막 성공 관측 시각.', nullable=True),
                              'window': ref('Period'), 'sources': array(ref('SourceStatus')), 'aggregation': ref('Aggregation')}),
        'SourcesStatusResponse': obj({'data': ref('SourcesStatus'), 'meta': ref('Meta')}),
    }


def _param(name, where, schema, description, required=False):
    return {'name': name, 'in': where, 'required': required, 'description': description, 'schema': schema}


PARAMETERS = {
    'FoodId': _param('food_id', 'path', {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,128}$'}, '음식 ID', True),
    'Period': _param('period', 'query', {'type': 'string', 'enum': list(PERIODS), 'default': '7d'}, '기간. end_date를 포함해 거슬러 올라간 일수.'),
    'TimelinePeriod': _param('period', 'query', {'type': 'string', 'enum': list(PERIODS), 'default': '30d'},
                             '기간. end_date를 포함해 거슬러 올라간 일수.'),
    'EndDate': _param('end_date', 'query', {'type': 'string', 'format': 'date'},
                      '기간 종료일(포함, KST). 기본값은 어제(마지막 완결 일자). 오늘까지 지정할 수 있습니다.'),
    'Granularity': _param('granularity', 'query', {'type': 'string', 'enum': ['day', 'week'], 'default': 'day'},
                          'day=일별, week=월요일 시작 주별(KST). 주별 값은 주 안의 콘텐츠 집합을 다시 계산합니다.'),
    'SourceFilter': _param('source', 'query', {'type': 'string', 'enum': list(SOURCE_CODES)}, '한 출처만 조회. 없으면 모든 출처.'),
    'RankingSource': _param('source', 'query', {'type': 'string', 'enum': list(SOURCE_CODES)},
                            '순위를 만들 출처. google_trends_rss·instagram_graph만 가능하며 나머지는 422.', True),
    'Metric': _param('metric', 'query', {'type': 'string', 'enum': [METRIC_CODE], 'default': METRIC_CODE}, '순위 지표'),
    'Kind': _param('kind', 'query', {'type': 'string', 'enum': list(ENTITY_KINDS)}, '음식 유형 필터'),
    'Query': _param('q', 'query', {'type': 'string', 'minLength': 1, 'maxLength': 100},
                    '대표 음식명·승인 별칭 검색어. 공백과 대소문자를 무시한 부분 일치.'),
    'Sort': _param('sort', 'query', {'type': 'string', 'enum': list(SORTS)},
                   '정렬. 기본값은 q가 있으면 relevance, 없으면 name. -last_observed_at=최근 관측순.'),
    'Limit': _param('limit', 'query', {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20}, '페이지 크기'),
    'Cursor': _param('cursor', 'query', {'type': 'string', 'maxLength': 512}, '이전 응답의 page.next_cursor'),
    'IncludeZero': _param('include_zero', 'query', {'type': 'string', 'enum': ['true', 'false'], 'default': 'false'},
                          'true면 정상 관측 0인 음식도 순위에 포함'),
}


def _response(description, schema):
    return {'description': description, 'content': {'application/json': {'schema': ref(schema)}}}


RESPONSES = {
    'BadRequest': _response('잘못된 파라미터·커서 또는 관리 토큰 포함 (INVALID_PARAMETER, INVALID_CURSOR, ADMIN_TOKEN_NOT_ACCEPTED)', 'ErrorResponse'),
    'Forbidden': _response('허용되지 않은 Host (FORBIDDEN_HOST)', 'ErrorResponse'),
    'FoodNotFound': _response('음식 없음 (FOOD_NOT_FOUND)', 'ErrorResponse'),
    'SourceNotRankable': _response('정책상 순위 미제공 출처 (SOURCE_NOT_RANKABLE)', 'ErrorResponse'),
    'InternalError': _response('서버 내부 오류 (INTERNAL_ERROR)', 'ErrorResponse'),
    'Unavailable': _response('저장소 사용 불가 (DATA_UNAVAILABLE)', 'ErrorResponse'),
}


def _operation(operation_id, tag, summary, description, parameters, schema, extra=()):
    responses = {'200': _response('성공', schema)}
    for status, name in (('400', 'BadRequest'), ('403', 'Forbidden')) + tuple(extra) + (('500', 'InternalError'), ('503', 'Unavailable')):
        responses[status] = {'$ref': '#/components/responses/' + name}
    return {'get': {'operationId': operation_id, 'tags': [tag], 'summary': summary, 'description': description,
                    'parameters': [{'$ref': '#/components/parameters/' + p} for p in parameters], 'responses': responses}}


OPERATIONS = {
    BASE_PATH + '/health': _operation(
        'getHealth', 'Health', '연결 확인', 'Base URL 설정 확인용. 데이터 모드(mock/database)와 계약 버전을 반환합니다.', (), 'HealthResponse'),
    BASE_PATH + '/foods': _operation(
        'listFoods', 'Foods', '음식 목록·검색',
        '승인된 활성 음식만 반환합니다(병합·검토 대기 후보 제외). q는 대표 음식명과 승인 별칭을 공백·대소문자 무시로 검색합니다.',
        ('Query', 'Kind', 'Sort', 'Limit', 'Cursor'), 'FoodListResponse'),
    BASE_PATH + '/foods/{food_id}': _operation(
        'getFood', 'Foods', '음식 상세',
        '병합된 이전 ID로 요청하면 대상 음식을 200으로 반환하고 redirect에 요청 ID를 넣습니다. 앱은 data.food_id를 저장하세요.',
        ('FoodId',), 'FoodDetailResponse', (('404', 'FoodNotFound'),)),
    BASE_PATH + '/foods/{food_id}/timeline': _operation(
        'getFoodTimeline', 'Analytics', '음식별 관측 추이',
        '출처별 일·주 단위 고유 콘텐츠 수. 관측이 없는 날은 value=null(NOT_OBSERVED)이며 0과 다릅니다. 출처끼리 합산하지 않습니다.',
        ('FoodId', 'TimelinePeriod', 'EndDate', 'Granularity', 'SourceFilter'), 'TimelineResponse', (('404', 'FoodNotFound'),)),
    BASE_PATH + '/foods/{food_id}/metrics': _operation(
        'getFoodMetrics', 'Analytics', '음식별 출처 관측 통계',
        '기간 전체 고유 콘텐츠 수(기간 집합 재계산), 이전 기간 대비 변화, coverage·품질·출처 불명 데이터. 기간 전체가 관측된 경우에만 값을 제공합니다.',
        ('FoodId', 'Period', 'EndDate', 'SourceFilter'), 'FoodMetricsResponse', (('404', 'FoodNotFound'),)),
    BASE_PATH + '/trends/rankings': _operation(
        'listRankings', 'Rankings', '출처별 관측 기준 순위',
        '한 출처·한 지표·한 기간의 값으로만 순위를 만듭니다. 기간 전체가 관측된 음식만 포함하며 종합 유행 점수는 제공하지 않습니다.',
        ('RankingSource', 'Metric', 'Period', 'EndDate', 'Kind', 'IncludeZero', 'Limit', 'Cursor'), 'RankingsResponse',
        (('422', 'SourceNotRankable'),)),
    BASE_PATH + '/sources/status': _operation(
        'getSourcesStatus', 'Sources', '데이터 갱신·수집 범위',
        '출처별 마지막 회차·성공 시각, 조회 기간(오늘을 끝으로 하는 period 일수) 회차·coverage·품질, 정책상 제공 여부와 출처 불명 데이터.',
        ('Period',), 'SourcesStatusResponse'),
}

DESCRIPTION = '''MZFoodTraker의 읽기 전용 앱 데이터 API입니다. 모바일·웹 프레임워크와 무관한 HTTP/JSON 계약입니다.

- 인증 없음(읽기 전용). 관리 토큰(X-MZ-Admin-Token)은 받지 않으며 앱에 넣으면 안 됩니다.
- 성공 응답은 data·meta(목록은 page 포함), 오류 응답은 error·meta 형식입니다.
- 시각은 UTC 초 단위 RFC 3339(Z), 일자는 Asia/Seoul(UTC+09:00) 달력일(YYYY-MM-DD)입니다.
- value=null은 관측되지 않았거나 계산할 수 없음을 뜻하며 0(정상 관측 0)과 다릅니다. 상태는 value_state로 확인하세요.
- 출처가 다른 수치는 합산하지 않으며 종합 유행 점수는 제공하지 않습니다.
- 알 수 없는 필드와 enum 값은 무시하거나 "알 수 없음"으로 처리하세요(1.x에서는 필드·값이 추가될 수 있습니다).
- meta.data_mode가 mock이면 가상 데이터입니다. 실제 유행 분석 결과로 표시하지 마세요.'''


def openapi(examples=None):
    """The published document. examples: {(path, status): [(name, summary, body)]} embedded as response examples."""
    paths = json.loads(json.dumps(OPERATIONS))
    components_responses = json.loads(json.dumps(RESPONSES))
    for (path, status), entries in sorted((examples or {}).items()):
        target = paths[path]['get']['responses'][status]
        if '$ref' in target:  # shared error responses carry examples per operation
            target = paths[path]['get']['responses'][status] = json.loads(json.dumps(components_responses[target['$ref'].rsplit('/', 1)[1]]))
        target['content']['application/json']['examples'] = {name: {'summary': summary, 'value': body} for name, summary, body in entries}
    return {
        'openapi': '3.0.3',
        'info': {'title': 'MZFoodTraker 앱 데이터 API', 'version': API_VERSION, 'description': DESCRIPTION},
        'servers': [{'url': 'http://127.0.0.1:8030', 'description': 'Mock 서버 (가상 데이터, python -m food_trends.app_mock)'},
                    {'url': 'http://10.0.2.2:8030', 'description': 'Android 에뮬레이터에서 호스트의 Mock 서버'},
                    {'url': 'http://127.0.0.1:8020', 'description': '로컬 실제 데이터 서버 (읽기 전용, python -m food_trends.app_api)'}],
        'tags': [{'name': 'Health', 'description': '연결 확인'}, {'name': 'Foods', 'description': '음식 목록·검색·상세'},
                 {'name': 'Analytics', 'description': '음식별 추이·출처 통계'}, {'name': 'Rankings', 'description': '출처별 순위'},
                 {'name': 'Sources', 'description': '데이터 갱신·수집 범위'}],
        'security': [],
        'paths': paths,
        'components': {'schemas': _schemas(), 'parameters': PARAMETERS, 'responses': components_responses},
    }


# Deterministic YAML: block style, every string double-quoted (JSON escapes are valid YAML 1.1/1.2 escapes),
# multi-line text as literal blocks only when that round-trips exactly.

_PLAIN_KEY = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')
_YAML11_WORDS = {'y', 'n', 'yes', 'no', 'on', 'off', 'true', 'false', 'null', '~'}


def _yaml_key(key):
    return key if _PLAIN_KEY.fullmatch(key) and key.lower() not in _YAML11_WORDS else json.dumps(key, ensure_ascii=False)


_YAML11_BREAKS = ('\u0085', ' ', ' ')  # line breaks in YAML 1.1 even inside quotes


def _yaml_scalar(value):
    if value is None or isinstance(value, bool):
        return {None: 'null', True: 'true', False: 'false'}[value]
    if isinstance(value, dict):
        return '{}'
    if isinstance(value, list):
        return '[]'
    out = json.dumps(value, ensure_ascii=False)
    for char in _YAML11_BREAKS:
        out = out.replace(char, '\\u%04x' % ord(char))
    return out


def _block_safe(value):
    return (isinstance(value, str) and '\n' in value and not value.endswith('\n') and not value[:1].isspace()
            and all(line == line.rstrip() and all(ord(c) >= 32 and c not in _YAML11_BREAKS for c in line)
                    for line in value.split('\n')) and '﻿' not in value)


def _yaml_lines(value, indent):
    pad, lines = ' ' * indent, []
    if isinstance(value, dict):
        for key, child in value.items():
            head = pad + _yaml_key(key) + ':'
            if isinstance(child, (dict, list)) and child:
                lines.append(head)
                lines.extend(_yaml_lines(child, indent + 2))
            elif _block_safe(child):
                lines.append(head + ' |-')
                lines.extend((pad + '  ' + line) if line else '' for line in child.split('\n'))
            else:
                lines.append(head + ' ' + _yaml_scalar(child))
    else:
        for child in value:
            if isinstance(child, dict) and child:
                nested = _yaml_lines(child, indent + 2)
                lines.append(pad + '- ' + nested[0].lstrip())
                lines.extend(nested[1:])
            elif isinstance(child, list) and child:
                raise ValueError('nested sequences are not used by the contract')
            else:
                lines.append(pad + '- ' + _yaml_scalar(child))
    return lines


def to_yaml(document):
    return '\n'.join(_yaml_lines(document, 0)) + '\n'


_YAML_ENTRY = re.compile(r'("(?:[^"\\]|\\.)*"|[A-Za-z_$][A-Za-z0-9_$]*):(?: (.*))?$')


class _SubsetReader:
    """Strict reader for exactly what to_yaml() emits; proves the YAML file encodes the JSON document."""

    def __init__(self, text):
        self.lines = text[:-1].split('\n') if text.endswith('\n') else text.split('\n')
        self.pos = 0

    def at(self, indent, prefix=''):
        return self.pos < len(self.lines) and self.lines[self.pos].startswith(' ' * indent + prefix)

    @staticmethod
    def scalar(raw):
        return {'{}': {}, '[]': []}[raw] if raw in ('{}', '[]') else json.loads(raw)

    def literal(self, indent):
        out = []
        while self.pos < len(self.lines) and (not self.lines[self.pos] or self.at(indent)):
            out.append(self.lines[self.pos][indent:])
            self.pos += 1
        while out and not out[-1]:  # trailing blank lines belong to whatever follows
            out.pop()
            self.pos -= 1
        return '\n'.join(out)

    def entry(self, result, indent, line):
        match = _YAML_ENTRY.match(line)
        if not match:
            raise ValueError('unexpected YAML at line %d' % self.pos)
        key = json.loads(match.group(1)) if match.group(1).startswith('"') else match.group(1)
        raw = match.group(2)
        if raw is None:
            result[key] = self.node(indent + 2)  # to_yaml() nests mappings and sequences two spaces deeper
        elif raw == '|-':
            result[key] = self.literal(indent + 2)
        else:
            result[key] = self.scalar(raw)

    def mapping(self, indent, first=None):
        result = {}
        if first is not None:
            self.entry(result, indent, first)
        while self.at(indent) and self.lines[self.pos][indent:indent + 1] not in (' ', '-'):
            self.pos += 1
            self.entry(result, indent, self.lines[self.pos - 1][indent:])
        return result

    def sequence(self, indent):
        result = []
        while self.at(indent, '- '):
            rest = self.lines[self.pos][indent + 2:]
            self.pos += 1
            result.append(self.mapping(indent + 2, rest) if _YAML_ENTRY.match(rest) else self.scalar(rest))
        return result

    def node(self, indent):
        return self.sequence(indent) if self.at(indent, '- ') else self.mapping(indent)


def load_yaml_subset(text):
    reader = _SubsetReader(text)
    document = reader.node(0)
    if reader.pos != len(reader.lines):
        raise ValueError('unparsed YAML at line %d' % (reader.pos + 1))
    return document


# Response checking against the document (OpenAPI 3.0 subset: no oneOf/anyOf/allOf).

def _resolve(document, schema):
    while '$ref' in schema:
        schema = document['components']['schemas'][schema['$ref'].rsplit('/', 1)[1]]
    return schema


def _valid_date(value):
    try:
        return bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)) and date.fromisoformat(value) is not None
    except ValueError:
        return False


def _valid_moment(value):
    try:
        return bool(DATE_TIME.fullmatch(value)) and datetime.fromisoformat(value[:-1]) is not None
    except ValueError:
        return False


_PYTHON_TYPES = {'object': dict, 'array': list, 'string': str, 'boolean': bool, 'integer': int, 'number': (int, float)}


def _check_number(instance, schema, path):
    return (['%s: below minimum' % path] if 'minimum' in schema and instance < schema['minimum'] else []) + \
           (['%s: above maximum' % path] if 'maximum' in schema and instance > schema['maximum'] else [])


def _check_string(instance, schema, path):
    problems = {'date': not _valid_date(instance), 'date-time': not _valid_moment(instance)}
    errors = ['%s: invalid %s' % (path, schema['format'])] if problems.get(schema.get('format')) else []
    if 'pattern' in schema and not re.search(schema['pattern'], instance):
        errors.append('%s: pattern mismatch' % path)
    if not schema.get('minLength', 0) <= len(instance) <= schema.get('maxLength', len(instance)):
        errors.append('%s: length out of range' % path)
    return errors


def _check_object(document, instance, schema, path, strict):
    properties = schema.get('properties', {})
    errors = ['%s: missing %s' % (path, name) for name in schema.get('required', []) if name not in instance]
    for name, child in instance.items():
        if name in properties:
            errors.extend(check(document, child, properties[name], '%s.%s' % (path, name), strict))
        elif strict:
            errors.append('%s: undeclared property %s' % (path, name))
    return errors


def check(document, instance, schema, path='$', strict=True):
    """List of violations. strict additionally rejects properties the document does not declare."""
    schema = _resolve(document, schema)
    if instance is None:
        return [] if schema.get('nullable') else ['%s: null is not allowed' % path]
    kind = schema.get('type')
    if isinstance(instance, bool) and kind in ('integer', 'number') or not isinstance(instance, _PYTHON_TYPES.get(kind, object)):
        return ['%s: expected %s' % (path, kind)]
    errors = ['%s: %r not in enum' % (path, instance)] if 'enum' in schema and instance not in schema['enum'] else []
    if kind in ('integer', 'number'):
        errors.extend(_check_number(instance, schema, path))
    elif kind == 'string':
        errors.extend(_check_string(instance, schema, path))
    elif kind == 'object':
        errors.extend(_check_object(document, instance, schema, path, strict))
    elif kind == 'array':
        for index, child in enumerate(instance):
            errors.extend(check(document, child, schema['items'], '%s[%d]' % (path, index), strict))
    return errors


def operation_for(document, path):
    """(template, operation) for a concrete request path, or (None, None)."""
    for template, item in document['paths'].items():
        pattern = '^' + re.sub(r'\\\{[a-z_]+\\\}', '[^/]+', re.escape(template)) + '$'
        if re.match(pattern, path):
            return template, item['get']
    return None, None


def response_schema(document, operation, status):
    response = operation['responses'][str(status)]
    if '$ref' in response:
        response = document['components']['responses'][response['$ref'].rsplit('/', 1)[1]]
    return response['content']['application/json']['schema']


def load_examples(directory):
    """docs/app-api/examples/*.json -> {(template, status): [(name, summary, body)]}."""
    document, examples = openapi(), {}
    for file in sorted(Path(directory).glob('*.json')):
        example = json.loads(file.read_text(encoding='utf-8'))
        template, operation = operation_for(document, example['request']['path'])
        if (not example.get('in_spec', True) or template is None or example['request']['method'] != 'GET'
                or str(example['status']) not in operation['responses']):
            continue  # large series and transport-level errors stay in the example files and ERRORS.md
        examples.setdefault((template, str(example['status'])), []).append((file.stem, example['summary'], example['response']))
    return examples


def render(directory):
    """(yaml text, json text) of the document with the examples found under directory/examples."""
    examples = Path(directory) / 'examples'
    document = openapi(load_examples(examples) if examples.is_dir() else None)
    return to_yaml(document), json.dumps(document, ensure_ascii=False, indent=2) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description='앱 API OpenAPI 문서 생성·검사 (외부 호출·DB 접근 없음)')
    parser.add_argument('--docs', default='docs/app-api', help='openapi.yaml·openapi.json을 둘 디렉터리')
    parser.add_argument('--check', action='store_true', help='파일이 생성 결과와 같은지 검사만 합니다')
    args = parser.parse_args(argv)
    directory = Path(args.docs)
    yaml_text, json_text = render(directory)
    targets = {directory / 'openapi.yaml': yaml_text, directory / 'openapi.json': json_text}
    if args.check:
        stale = [str(p) for p, content in targets.items() if not p.is_file() or p.read_text(encoding='utf-8') != content]
        print(json.dumps({'up_to_date': not stale, 'stale': stale}, ensure_ascii=False))
        return 1 if stale else 0
    directory.mkdir(parents=True, exist_ok=True)
    for file, content in targets.items():
        file.write_text(content, encoding='utf-8', newline='\n')
    print(json.dumps({'written': [str(p) for p in targets]}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
