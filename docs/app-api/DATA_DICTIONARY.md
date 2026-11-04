# 앱 데이터 API 데이터 사전 (v1)

[openapi.yaml](openapi.yaml)의 필드가 무엇을 뜻하고 어떻게 계산되는지 설명합니다. 앱 화면 구현에 필요한 의미만 다루며 백엔드 코드를 읽지 않아도 되도록 작성했습니다.

## 1. 공통 규칙

| 규칙 | 내용 |
| --- | --- |
| 필드 존재 | 스키마에 있는 필드는 **항상** 응답에 있습니다. 값이 없으면 `null`이며 필드를 생략하지 않습니다. |
| 시각 | UTC, 초 단위 RFC 3339, `Z` 접미사 (`2026-11-02T03:00:00Z`). 소수 초를 쓰지 않습니다. 화면에는 기기 시간대로 바꿔 표시하세요. |
| 일자 | `Asia/Seoul` 달력일 `YYYY-MM-DD`. 한국은 일광절약시간이 없어 UTC+09:00으로 고정 계산합니다. KST 00:00 = 전날 UTC 15:00. |
| `null`과 `0` | `value: 0`은 **관측했고 결과가 0**인 정상 값입니다. `value: null`은 관측하지 못했거나 계산할 수 없다는 뜻입니다. 이유는 항상 `value_state`에 있습니다. `null`을 0으로 바꿔 그래프에 그리지 마세요. |
| 출처 합산 금지 | 출처마다 콘텐츠 종류와 관측 방식이 다릅니다. 출처 값끼리 더하거나 평균 내지 마세요. 종합 유행 점수는 제공하지 않습니다. |
| 하위 호환 | 1.x 안에서는 필드·enum 값이 추가될 수 있습니다. 모르는 필드는 무시하고, 모르는 enum 값은 "알 수 없음"으로 처리하세요. 기존 필드의 의미 변경·삭제는 `/api/v2`에서만 합니다. |
| mock 표시 | `meta.data_mode = "mock"`이면 가상 데이터입니다. `meta.notice`를 화면에 함께 보여 실제 유행으로 오해되지 않게 하세요. |

## 2. 응답 형식

| 위치 | 필드 | 설명 |
| --- | --- | --- |
| 성공 | `data` | 엔드포인트별 본문 |
| 성공(목록) | `page` | 페이지 정보 (§12) |
| 모든 응답 | `meta.api_version` | 계약 버전 `1.0.0` |
| | `meta.data_mode` | `database`(설정된 DB에서 계산) / `mock`(가상 데이터 Mock 서버) |
| | `meta.observation_origin` | 집계에 넣은 관측 회차의 origin. 운영 서버 기본값 `live_api`. 테스트 DB를 읽는 서버는 `mock` |
| | `meta.generated_at` | 응답을 계산한 시각 |
| | `meta.timezone` | 항상 `Asia/Seoul` |
| | `meta.notice` | mock·테스트 데이터 안내문. 실제 데이터면 `null` |
| 오류 | `error.code`, `error.message`, `error.details[]` | [ERRORS.md](ERRORS.md) |

## 3. 식별자와 음식

| 필드 | 형식 | 설명 |
| --- | --- | --- |
| `food_id` | `^[A-Za-z0-9_-]{1,128}$` | 영구 음식 ID. 내용을 해석하지 말고 불투명 문자열로 저장하세요. 음식이 다른 음식에 병합되면 이전 ID로 상세를 조회할 때 대상 음식이 반환되고 `redirect.requested_food_id`에 이전 ID가 들어갑니다. 앱은 응답의 `data.food_id`로 저장값을 바꾸세요. |
| `canonical_name` | 문자열 1~200자 | 사람이 승인한 대표 음식명 |
| `entity_kind` | `DISH` 요리 · `PRODUCT` 상품 · `INGREDIENT` 재료 · `FOOD_CONCEPT` 음식 개념 | 음식 유형 |
| `aliases` (목록) | 문자열 배열 | 승인된 활성 별칭 표기. 대표 음식명도 별칭으로 승인되어 있으면 포함됩니다. 취소된 별칭은 나오지 않습니다. |
| `aliases` (상세) | `{text, scope}` 배열 | `scope`는 별칭이 유효한 범위(예: `food:ko`) |
| `status` | `ACTIVE` / `RETIRED` | 상세에만 있습니다. 목록·순위에는 `ACTIVE`만 나옵니다. |
| `relations[]` | `VARIANT_OF` / `HAS_VARIANT` / `RELATED_TO` | 사람이 승인한 관계. `VARIANT_OF`=이 음식이 `food`의 파생, `HAS_VARIANT`=`food`가 이 음식의 파생, `RELATED_TO`=연관(대칭). **관계 음식의 수치는 서로 합산하지 않습니다.** |
| `merged_food_ids` | 문자열 배열 | 이 음식으로 병합된 이전 음식 ID. 병합된 음식의 관측은 이 음식에 포함됩니다. |
| `created_at`, `updated_at` | 시각 | 음식 등록·정보 변경 시각 (관측 갱신 시각이 아님) |
| `last_observed_at` | 시각 / `null` | 포함 출처에서 이 음식의 확인된 콘텐츠를 마지막으로 관측한 시각 |
| `freshness.reprocessing_pending` | 불리언 | 승인된 별칭 추가·취소·병합이 아직 관측 재처리에 반영되지 않았으면 `true`. 반영 후 수치가 바뀔 수 있습니다. |

### 검색 (`GET /api/v1/foods?q=`)

- 대표 음식명과 승인된 활성 별칭을 검색합니다. 검토 대기 후보·취소된 별칭·병합된 음식은 검색되지 않습니다(병합된 음식의 별칭은 대상 음식으로 옮겨져 있습니다).
- 비교 전에 유니코드 NFC 정규화, 연속 공백 정리, 대소문자 무시(casefold)를 하고, 공백을 모두 뺀 형태로도 비교합니다. `두바이쫀득` → `두바이 쫀득 쿠키` 일치.
- `match.kind`: `exact`(전체 일치) > `prefix`(앞부분 일치) > `contains`(부분 일치). `sort=relevance`(q가 있을 때 기본값)는 이 순서, 같으면 대표명 일치 우선, 그다음 대표 음식명 가나다순입니다.
- `match.text`는 일치한 이름입니다. 여러 별칭이 같은 등급으로 일치하면 별칭 정렬 순서상 첫 번째를 보여 줍니다.

## 4. 출처와 제공 여부

| `source_code` | 표시명 | `availability` | 관측 범위 (`observation_scope`) | 콘텐츠 단위 | 근거 보존 |
| --- | --- | --- | --- | --- | --- |
| `google_trends_rss` | Google 트렌드 RSS | `INCLUDED` | `SHARED_FEED`: 한국 급상승 피드 하나를 모든 음식이 공유. 피드 수집에 성공한 날은 모든 음식이 관측된 날입니다. | 트렌드 항목 | 제한 없음 |
| `instagram_graph` | Instagram | `INCLUDED` | `PER_FOOD_QUERY`: 음식별 해시태그 검색 회차가 성공한 날만 그 음식이 관측된 날입니다. 추적하지 않는 음식은 미관측입니다. | 게시물 | 캡션 30일 |
| `youtube` | YouTube | `POLICY_BLOCKED` | 없음 | - | - |
| `naver_datalab` | NAVER 데이터랩 | `NOT_STORED` | 없음 | - | - |

- `POLICY_BLOCKED`: YouTube 영상에서 음식 언급을 추출하거나 파생 지표를 만드는 것이 현재 정책상 비활성입니다. 수치를 제공하지 않습니다.
- `NOT_STORED`: NAVER 지수는 요청마다 0~100으로 다시 매겨지는 상대값이라 다른 요청과 비교·누적할 수 없고, 정책상 저장하지 않습니다. B8 집계에 포함하지 않습니다.
- 차단 출처도 응답 목록에 `availability`와 `availability_reason`을 담아 나타나므로 "제공하지 않음"을 화면에 안내할 수 있습니다.

## 5. 지표 `confirmed_items` (단위 `distinct_items`)

**승인된 정확 별칭이 확인된 고유 콘텐츠 수**입니다.

| 규칙 | 설명 |
| --- | --- |
| 확인 기준 | Google 트렌드 RSS는 트렌드 검색어·뉴스 제목에 승인 별칭이 단어 경계(조사 허용)로 정확히 나타난 항목, Instagram은 캡션 해시태그가 승인 별칭과 정확히 같은 게시물입니다. 유사어·띄어쓰기 변형 추정은 하지 않습니다. |
| 검색 결과 제외 | 해시태그 검색이 돌려준 게시물이라도 캡션에서 별칭이 확인되지 않으면 세지 않습니다(검색 결과 일치는 언급이 아님). |
| 중복 제거 | 같은 콘텐츠는 음식·출처·구간마다 한 번만 셉니다. 한 게시물이 여러 별칭(예: `#두쫀쿠`, `#두바이쫀득쿠키`)을 가져도, 여러 별칭 검색에서 반환되어도, 하루 여러 회차에 관측되어도 1입니다. |
| 관측일 | 콘텐츠를 관측한 회차의 관측 시각을 KST 일자로 바꾼 날에 셉니다. 여러 날 관측된 콘텐츠는 각 날에 1씩 셉니다. |
| 기간·주 값 | 일별 값을 더하지 않고 해당 기간에 관측된 콘텐츠 **집합을 다시 계산**합니다. 그래서 7일 값은 일별 값 합계보다 작거나 같습니다. |
| 병합·관계 | 병합된 음식의 관측은 대상 음식에 포함됩니다. 파생(`VARIANT_OF`)·연관 음식은 각자 따로 셉니다. |
| 출처 분리 | 출처가 다른 콘텐츠는 같은 내용이어도 합치지 않습니다. |
| 제외 데이터 | 검토 대기·미확인 음식 언급, 취소된 별칭의 언급, origin이 다른 회차(운영 서버는 `live_api`만 포함), 출처 불명 과거 데이터(§10)는 값에 넣지 않습니다. |

## 6. 관측 상태 `value_state`

| 값 | `value` | 의미 | 쓰이는 곳 |
| --- | --- | --- | --- |
| `OBSERVED` | 0 이상 정수 | 관측 완료. 0은 정상 관측 0 | 일·주·기간 |
| `NOT_OBSERVED` | `null` | 성공한 관측 회차가 없음 (수집 실패, 추적 안 함, 수집 전) | 일·주·기간 |
| `EVIDENCE_EXPIRED` | `null` | 그날 관측한 콘텐츠의 근거가 보존 기한(Instagram 캡션 30일) 경과·출처 삭제로 사라져 값을 다시 만들 수 없음 | 일 |
| `INSUFFICIENT_COVERAGE` | `null` | 구간 안에 미관측·근거 만료·진행 중인 날이 섞여 있어 구간 전체 값을 제공하지 않음 | 주·기간 |

기간 값은 **기간의 모든 일자가 `OBSERVED`이고 오늘(수집 진행 중)이 포함되지 않을 때만** 숫자를 제공합니다. 일부만 관측된 기간의 부분 합계는 제공하지 않습니다. 상태별 일수는 `day_counts`(`observed`, `not_observed`, `evidence_expired`)에 있습니다.

## 7. 관측 범위 `coverage`

| 값 | 의미 |
| --- | --- |
| `COMPLETE` | 요청 결과를 끝까지 받음 |
| `LIMITED` | 페이지 상한에 도달해 더 있는 결과를 받지 않음. 값은 하한값 |
| `PARTIAL` | 일부 페이지·일부 검색 조건만 성공. 값은 하한값 |
| `NONE` | 성공한 관측 없음 (`value_state`가 `NOT_OBSERVED` 등일 때) |

- **하루의 coverage**: 같은 날 같은 검색 조건(Instagram은 해시태그 질의, RSS는 피드)의 회차 중 가장 좋은 결과를 그 조건의 결과로 봅니다. 음식의 검색 조건이 여럿이면 가장 낮은 결과가 그날의 coverage입니다. 성공한 조건과 하루 종일 실패한 조건이 섞이면 `PARTIAL`입니다.
- **구간·기간의 coverage**: 포함된 일자 중 가장 낮은 값 (`COMPLETE` > `LIMITED` > `PARTIAL` > `NONE`).
- 출처 불명 과거 데이터의 coverage `UNKNOWN`은 본 값에 쓰지 않고 `legacy_unknown`에만 나옵니다.

## 8. 기간

| 파라미터 | 값 | 설명 |
| --- | --- | --- |
| `period` | `7d`, `30d`, `90d` | `end_date`를 포함해 거슬러 올라간 일수. 기본값: 추이 `30d`, 통계·순위·상태 `7d` |
| `end_date` | `YYYY-MM-DD` (KST) | 기본값 **어제**(마지막 완결 일자). 오늘까지 지정 가능. 오늘이 포함되면 기간 값은 `INSUFFICIENT_COVERAGE`, 오늘 점은 `complete: false` |

- `period`: `start_date` = `end_date` − (일수 − 1).
- `previous_period`: `period` 바로 앞의 같은 길이 기간. 예: 7일 기간 10-27~11-02 → 이전 10-20~10-26.
- 데이터 상태(`/sources/status`)의 `window`는 **오늘을 포함**합니다(수집 현황 확인용).

## 9. 추이·변화·순위

### 추이 점 (`TimelinePoint`)

| 필드 | 설명 |
| --- | --- |
| `start_date`, `end_date`, `days` | 점이 대표하는 구간. 일 단위는 하루, 주 단위는 월~일(KST)이며 기간 경계에서 잘리면 1~6일 |
| `value`, `value_state`, `coverage` | §5~§7 |
| `observed_days` | 구간 안 관측 일수 |
| `complete` | `false` = 오늘(수집 진행 중) 포함 또는 기간 경계로 잘린 주. 다른 점과 직접 비교하지 마세요 |
| `runs.successful`, `runs.failed` | 그 구간의 관측 범위 회차 수 (RSS는 피드 수집, Instagram은 그 음식의 검색 회차) |
| `legacy_unknown_value` | 출처 불명 과거 데이터의 고유 콘텐츠 수. 본 값에 더하지 않습니다. 없으면 `null` |

### 변화 (`Change`)

| 조건 | `state` | `reason` | `absolute` | `ratio` |
| --- | --- | --- | --- | --- |
| 현재 기간이 `OBSERVED`가 아님 | `NOT_COMPARABLE` | `CURRENT_PERIOD_INSUFFICIENT` | `null` | `null` |
| 이전 기간이 `OBSERVED`가 아님 | `NOT_COMPARABLE` | `PREVIOUS_PERIOD_INSUFFICIENT` | `null` | `null` |
| 둘 중 하나라도 coverage가 `COMPLETE`가 아님 | `NOT_COMPARABLE` | `COVERAGE_LIMITED` | `null` | `null` |
| 이전 값이 0 | `COMPARABLE` | `PREVIOUS_ZERO` | 현재 값 | `null` |
| 그 외 | `COMPARABLE` | `null` | 현재 − 이전 | (현재 − 이전) / 이전, 소수 4자리 |

미관측 기간을 0으로 보고 증가율을 만들지 않습니다. `ratio` 0.25는 25% 증가입니다.

### 순위 (`GET /api/v1/trends/rankings`)

- 반드시 `source`를 지정합니다. 한 출처·한 지표(`confirmed_items`)·한 기간의 값만 비교하며 출처를 합친 순위·종합 점수는 없습니다(`basis.scope = SINGLE_SOURCE`).
- 기간 값이 `OBSERVED`인 `ACTIVE` 음식만 포함합니다. 제외된 음식 수는 `excluded`에 있습니다.
- 기본(`include_zero=false`)은 정상 관측 0인 음식을 제외하고 `excluded.zero_value`로 셉니다.
- 정렬: 값 내림차순, 같으면 대표 음식명·`food_id` 순. 동점은 같은 순위이며 다음 순위는 건너뜁니다(1, 2, 2, 4).
- Instagram은 음식별 검색 회차로 관측하므로 추적 해시태그 수·페이지 상한에 따라 값의 범위가 다릅니다. 같은 출처 안의 상대 비교로만 사용하세요(`basis.notes`).

## 10. 출처 불명 과거 데이터 (`legacy_unknown`)

이관 전 데이터 중 어떤 API 회차에서 수집됐는지 기록되지 않은 데이터(origin `legacy_unknown`, coverage `UNKNOWN`)입니다. 관측 범위를 알 수 없어 미관측·0과 구분할 수 없으므로 **본 값에 더하지 않고** `legacy_unknown`(통계)과 `legacy_unknown_value`(추이 점)로만 보여 줍니다. 없으면 `null`입니다.

## 11. 기타 필드

| 필드 | 설명 |
| --- | --- |
| `provider_values[]` (RSS) | Google 트렌드 RSS가 표시한 대략적 검색량 문구(`raw_value`, 예: `2,000+`). **트렌드 검색어 자체가 음식 별칭과 같은 항목만** 제공합니다(뉴스 제목에만 언급된 항목의 검색량은 다른 검색어의 값이므로 제외). 숫자로 바꾸거나 합산하지 마세요. 기간 안 항목별 최신 값, 최근순 최대 5개 |
| `quality` (통계) | `checked_items`=기간 안 확인 콘텐츠 수, `warn_items`=품질 경고가 있는 콘텐츠 수(일부 필드 누락 등, 값에는 포함), `failed_runs`=기간 안 실패 회차 수 |
| `quality` (상태) | 조회 기간의 품질 FAIL·WARN 결과 수와 품질 FAIL로 적재하지 않은 레코드 수 |
| `policy` (상태) | 출처 정책 버전, 근거 보존 일수, 언급 추출 허용 여부, 지표 저장 정책 |
| `data_through` (상태) | 포함 출처의 마지막 성공 관측 시각 |
| `aggregation` (상태) | `read_time`=요청 시 계산(저장 집계 없음), `pending_entity_changes`=관측 재처리 대기 중인 별칭·병합 변경 수 |

## 12. 페이지네이션

- `limit` 1~100 (기본 20), 다음 페이지는 응답의 `page.next_cursor`를 `cursor`에 그대로 넣습니다. 커서를 만들거나 해석하지 마세요.
- 커서는 같은 조건(`q`, `kind`, `sort`, `source`, `period`, `end_date` 등)의 요청에만 쓸 수 있고, 조건이 다르면 `400 INVALID_CURSOR`입니다. `limit`은 페이지마다 바꿀 수 있습니다.
- `end_date`를 생략한 순위 요청의 커서는 첫 페이지의 기준일을 기억해 자정이 지나도 같은 기간으로 이어집니다.
- 값은 요청 시 계산하므로 페이지 사이에 데이터가 갱신되면 순서가 바뀔 수 있습니다. 새로고침 시 첫 페이지부터 다시 요청하세요.
- `page.total_items`는 조건에 맞는 전체 항목 수입니다.
