# MZFoodTraker 앱 데이터 API 연동 가이드 (v1)

모바일·웹 앱 개발자를 위한 읽기 전용 HTTP/JSON API입니다. Flutter·Kotlin·Swift·React Native·웹 어디서나 같은 계약을 씁니다. 백엔드 Python 코드를 읽지 않고 이 문서와 [openapi.yaml](openapi.yaml)만으로 화면을 구현할 수 있도록 작성했습니다.

## 문서 구성

| 문서 | 내용 |
| --- | --- |
| [openapi.yaml](openapi.yaml) / [openapi.json](openapi.json) | OpenAPI 3.0.3 계약 (스키마·파라미터·예시). 코드 생성기(openapi-generator 등)에 그대로 사용 |
| [DATA_DICTIONARY.md](DATA_DICTIONARY.md) | 필드 의미, 계산 규칙, `null`과 0, coverage, 기간·순위 규칙 |
| [ERRORS.md](ERRORS.md) | 오류 코드와 처리 방법 |
| [ACCESS.md](ACCESS.md) | 인증·접근 범위, 관리 API와의 분리, 외부 공개 전 과제 |
| [examples/](examples/) | 요청·응답 예시 JSON (`request`, `status`, `response`) |

## 빠른 시작

앱 API는 저장소의 기본 브랜치 `main`에 있습니다. Python 3.12만 있으면 되고 추가 패키지는 설치하지 않습니다. 설치 방법과 버전별 검증 상태는 [Python 버전](#python-버전)에 있습니다.

Windows (PowerShell)

```powershell
git clone https://github.com/asinagi/MZFoodTraker.git
cd MZFoodTraker
py -3.12 -m food_trends.app_mock
```

macOS (터미널)

```bash
git clone https://github.com/asinagi/MZFoodTraker.git
cd MZFoodTraker
python3.12 -m food_trends.app_mock
```

1. 처음 한 번은 가상 데이터셋을 만드느라 15~30초 걸립니다. `Mock app API (synthetic data): http://127.0.0.1:8030/api/v1/health`가 출력되면 준비된 것이고, 다음 실행부터는 1초 안에 시작합니다.
2. 같은 PC의 브라우저로 `http://127.0.0.1:8030/api/v1/health`를 열어 `data_mode`가 `mock`인지 확인합니다.
3. 앱의 Base URL을 앱이 실행되는 환경에 맞게 설정합니다. Android 에뮬레이터, iOS 시뮬레이터, 실기기마다 주소가 다릅니다([Base URL 설정](#base-url-설정)).
4. 서버는 `Ctrl+C`로 종료합니다.

이미 `feat/b8-app-api` 브랜치로 받았다면 `git switch main` 후 `git pull`로 바꾸세요. 계약(경로·필드·예시)은 같습니다. git이 없으면 GitHub 저장소 화면(`main`)의 **Code → Download ZIP**으로 받아, 압축을 푼 폴더(저장소 루트)에서 마지막 줄 명령만 실행합니다.

## 응답 형식 기본

- Base URL 뒤에 `/api/v1/...` 경로를 붙입니다. 예: `http://127.0.0.1:8030/api/v1/health`
- GET만 사용합니다(HEAD·OPTIONS도 응답). 인증 헤더가 필요 없고, 관리 토큰(`X-MZ-Admin-Token`)을 보내면 거부됩니다([ACCESS.md](ACCESS.md)).
- 성공은 `{"data": ..., "meta": ...}`(목록은 `page` 추가), 오류는 `{"error": ..., "meta": ...}`입니다.
- 시각은 UTC 초 단위(`2026-11-02T03:00:00Z`), 일자는 KST(`Asia/Seoul`) 기준 `YYYY-MM-DD`입니다. 시각을 화면에 보여 줄 때는 기기 시간대로 바꾸세요.
- 스키마의 필드는 항상 존재합니다(값이 없으면 `null`). 1.x에서 필드·enum 값이 추가될 수 있으니 모르는 필드·enum 값은 무시하세요.
- 목록의 다음 페이지는 `page.has_more`가 `true`일 때 `page.next_cursor`를 `cursor`로 그대로 보냅니다. `limit` 말고 다른 파라미터는 첫 요청과 같아야 합니다(다르면 `400 INVALID_CURSOR`).

## 꼭 지켜야 할 규칙

### 1. `null`과 `0`은 다릅니다

| 응답 | 의미 | 화면 |
| --- | --- | --- |
| `value: 0`, `value_state: "OBSERVED"` | 정상적으로 수집했고 확인된 콘텐츠가 0건 | `0`으로 표시 |
| `value: null` | 수집하지 못했거나 계산할 수 없음. 이유는 `value_state`에 있음 | 0으로 표시하지 않음(아래 2번) |

- 그래프에서 `null`을 0으로 이어 그리면 "수집 실패"가 "인기 없음"으로 보입니다. 선을 끊거나 회색 구간으로 그리세요.
- 앱에서 합계·평균을 낼 때도 `null`을 0으로 바꾸지 마세요. 기간 값은 서버가 고유 콘텐츠 기준으로 따로 계산해 줍니다(일별 값을 더하면 같은 콘텐츠가 중복으로 셉니다).
- 변화율 `change.ratio`가 `null`이면 비교할 수 없다는 뜻입니다(이유는 `change.state`, `change.reason`). 0%로 표시하지 마세요.

### 2. `value_state`별 표시

| `value_state` | 나오는 곳 | 의미 | 권장 표시 |
| --- | --- | --- | --- |
| `OBSERVED` | 일·주 점, 기간 값 | 관측 완료. `value`는 숫자(0 포함) | 값 그대로 |
| `NOT_OBSERVED` | 일·주 점, 기간 값 | 성공한 수집이 없음 | 선 끊기·회색, "수집되지 않음" |
| `EVIDENCE_EXPIRED` | 일 점 | 근거 보존 기간(예: Instagram 캡션 30일)이 지나 값을 다시 만들 수 없음 | "보존 기간 경과" |
| `INSUFFICIENT_COVERAGE` | 주 점, 기간 값 | 일부 날짜만 관측됨(미관측·근거 만료·진행 중인 오늘 포함) | 숫자 대신 "7일 중 6일 관측" (기간 값은 `day_counts.observed`/`period.days`, 주 점은 `observed_days`/`days`) |

`OBSERVED`여도 `coverage`가 `LIMITED`·`PARTIAL`이면 하한값입니다(점선·표식 권장). 그래프의 나머지 규칙은 [화면 구현 시 주의](#화면-구현-시-주의)에 있습니다.

### 3. 순위는 출처별로만 제공합니다

- `GET /api/v1/trends/rankings`는 `source`가 필수이며 `google_trends_rss`·`instagram_graph`만 순위를 만들 수 있습니다. `youtube`(정책상 비활성)·`naver_datalab`(저장하지 않는 지수)은 `422 SOURCE_NOT_RANKABLE`입니다.
- 출처 탭(Google 트렌드 / Instagram)으로 나눠 보여 주세요. 두 출처의 값이나 순위를 더하거나 평균 내서 종합 순위·유행 점수를 만들지 마세요. 단위가 다르고(트렌드 항목 수와 게시물 수) 확정된 종합 공식이 없습니다.
- 순위 기준(출처·지표·기간)은 `data.basis`에 있고, `basis.scope`는 항상 `SINGLE_SOURCE`입니다.
- 동점은 같은 순위이고 다음 순위는 건너뜁니다(예: 1, 2, 2, 4).
- 기간 값이 `OBSERVED`가 아닌 음식은 순위에서 빠지고 `data.excluded`에 사유별 수(`insufficient_coverage`, `not_observed`, `zero_value`)로 나옵니다. 값이 0인 음식은 기본으로 빠지며 `include_zero=true`로 포함할 수 있습니다.

### 4. Mock 데이터 표시

Mock 서버의 데이터는 모두 가상이며 실제 유행 분석 결과가 아닙니다. 다음 값으로 구분합니다.

| 구분 | Mock 서버 | 실제 데이터 서버 |
| --- | --- | --- |
| `meta.data_mode` | `mock` | `database` |
| `meta.notice` | `가상 데이터입니다. 실제 유행 분석 결과가 아닙니다.` | `null` (Mock 수집으로 만든 테스트 DB면 안내문) |
| 음식 이름 | `[MOCK] `으로 시작 (예: `[MOCK] 두쫀쿠`) | 실제 이름 |
| `food_id` | `FOOD_MOCK_`으로 시작 | `FOOD_<영숫자>` |

- `meta.notice`가 `null`이 아니면 배너 등으로 화면에 항상 보여 주세요. 앱 시작 시 `/api/v1/health`의 `data_mode`로 Mock 연결 여부를 확인해 개발 빌드 표시에 쓰는 것을 권장합니다.
- 음식 이름의 `[MOCK]`을 지우거나 가리지 말고, Mock 화면 캡처를 실제 유행 결과처럼 발표·배포하지 마세요.
- `food_id`는 코드에 고정하지 말고 목록·검색 응답에서 받아 쓰세요. 실제 서버의 ID는 다릅니다.

### 5. 명세에 없는 파라미터와 값은 거부합니다

오타를 일찍 발견하도록 서버는 모르는 입력을 무시하지 않고 `400 INVALID_PARAMETER`로 돌려줍니다. 원인은 `error.details[]`의 `parameter`, `reason`, `allowed`(허용 값 목록)에 있습니다.

| 보낸 요청 | `details[].reason` | 고치는 방법 |
| --- | --- | --- |
| 명세에 없는 파라미터 (캐시 방지용 `_t=123`, 오타 `perod=7d`) | `unknown_parameter` | 명세에 있는 파라미터만 보내기 |
| 같은 파라미터 반복 (`q=a&q=b`) | `repeated_parameter` | 한 번만 보내기 |
| 빈 값 (`q=`, `kind=`, `limit=`) | `out_of_range`, `unsupported_value`, `invalid_format` | 값이 없으면 파라미터를 아예 빼기 |
| 대소문자가 다른 값 (`period=7D`, `kind=dish`, `include_zero=True`) | `unsupported_value` | 값은 대소문자를 구분. 불리언은 `true`·`false` 소문자만 |
| 형식이 다른 날짜·숫자 (`end_date=2026-1-3`, `limit=abc`) | `invalid_format` | 날짜는 `YYYY-MM-DD`, 숫자는 정수 |
| 범위 밖 (`limit=0`, 오늘 이후 `end_date`) | `out_of_range` | `limit`은 1~100, `end_date`는 오늘(KST)까지 |
| `sort=relevance`인데 `q` 없음 | `requires_parameter` | `q`와 함께 보내기 |
| 순위 요청에 `source` 없음 | `required` | `source` 지정 |

- HTTP 클라이언트가 캐시 방지 파라미터를 자동으로 붙이거나 `null` 값을 빈 문자열(`q=`)로 보내지 않도록 설정하세요.
- 경로도 정확해야 합니다. 끝에 `/`를 붙이면(`/api/v1/foods/`) `404 ROUTE_NOT_FOUND`이고, `food_id`도 대소문자를 구분합니다.

### 6. 날짜와 `end_date`

- Mock 서버의 시계는 **2026-11-03 12:00 KST로 고정**되어 있습니다. 실제 날짜와 상관없이 기본 기간은 2026-11-02(어제)까지이고, `end_date`는 2026-11-03까지만 받습니다.
- **`end_date`는 생략하는 것이 안전합니다.** 생략하면 서버가 기준일(KST 어제)을 정하므로 Mock과 실제 서버에서 똑같이 동작합니다. 기기 날짜로 계산한 `end_date`(예: 2026-11-04 이후)를 Mock에 보내면 `400 INVALID_PARAMETER`(`end_date`, `out_of_range`)입니다.
- 화면에 기준 기간을 표시할 때는 기기 날짜가 아니라 응답의 기간을 쓰세요. 추이·통계는 `data.period`(통계는 `data.previous_period`도), 순위는 `data.basis.period`입니다.
- 이전 기간으로 넘기는 화면처럼 `end_date`가 꼭 필요하면 기기 날짜가 아니라 응답 날짜(예: `period.start_date`의 하루 전)를 기준으로 계산하세요.

## 오류 처리

HTTP 상태 코드로 **큰 분류**(요청 문제인지 서버 문제인지, 재시도할지)를 정하고, `error.code`로 **세부 처리**를 정하세요. 같은 400에도 여러 `code`가 있고 1.x에서 `code`가 추가될 수 있으므로, 앱이 모르는 `code`는 상태 코드 분류에 따라 처리합니다.

| HTTP 상태 | 분류와 기본 처리 (모르는 `code` 포함) | `error.code`별 세부 처리 |
| --- | --- | --- |
| 응답 없음 | 타임아웃·연결 실패. 연결 오류 화면과 재시도 버튼, Base URL과 서버 실행 여부 확인 | - |
| 200 | 정상. 빈 목록·`null` 값도 오류가 아님 | - |
| 400 | 요청 문제. 자동 재시도하지 않고 `details`를 로그로 남김 | `INVALID_CURSOR`: 커서를 버리고 첫 페이지부터 다시 요청 · `INVALID_PARAMETER`: 앱 버그로 수정, 사용자 입력이면 입력 안내(검색어 1~100자) · `ADMIN_TOKEN_NOT_ACCEPTED`: 요청에서 관리 토큰 헤더 제거 |
| 403 | 접근 설정 문제 | `FORBIDDEN_HOST`: Base URL을 IP 주소나 `localhost`로 변경 |
| 404 | 대상 없음 | `FOOD_NOT_FOUND`: "음식을 찾을 수 없음" 화면, 저장해 둔 ID면 목록을 새로 받기 · `ROUTE_NOT_FOUND`: Base URL·경로 확인(앱 버그) |
| 405 | 앱 버그 | `METHOD_NOT_ALLOWED`: GET으로 요청 |
| 422 | 앱 버그 | `SOURCE_NOT_RANKABLE`: 순위 `source`는 `google_trends_rss`·`instagram_graph`만 |
| 500 | 서버 오류. "잠시 후 다시 시도", 자동 재시도는 1~2회까지 | `INTERNAL_ERROR` |
| 503 | 일시적 사용 불가. 점검 안내 후 지수 백오프로 재시도 | `DATA_UNAVAILABLE` |
| 그 밖의 상태, JSON이 아닌 본문 | 4xx는 요청 문제, 5xx는 서버 문제로 처리 | 앱 API가 아닌 곳(프록시, 잘못된 Base URL)에서 온 응답일 수 있음 |

처리 순서 (프레임워크와 무관한 의사 코드)

```text
응답 = GET(baseUrl + 경로)
응답이 없으면                  → 연결 오류 화면 (재시도 버튼)
상태가 200이면                 → data 표시 (빈 목록·null은 정상 상태로 표시)
그 밖이면:
    code = 본문이 JSON이고 error가 있으면 error.code, 아니면 없음
    code에 정해 둔 처리가 있으면 → 그 처리 (예: INVALID_CURSOR → 첫 페이지부터 다시)
    상태가 4xx이면             → 요청 오류 화면, details 로그, 자동 재시도 안 함
    상태가 5xx이면             → 서버 오류 화면, 재시도 (503은 지수 백오프)
```

- `error.message`는 사용자에게 그대로 보여 줘도 되는 한국어 문장이지만 분기 조건으로는 쓰지 마세요. 분기는 `error.code`로 합니다.
- Mock 서버에서는 요청 헤더 `X-Mock-Error: 503` 또는 `X-Mock-Error: 500`으로 오류 화면을 재현할 수 있습니다(실제 서버는 이 헤더를 무시).
- 전체 코드와 `details[].reason` 목록은 [ERRORS.md](ERRORS.md)에 있습니다.

## 엔드포인트와 화면

| 화면 | 요청 | 주요 필드 |
| --- | --- | --- |
| 앱 시작·연결 확인 | `GET /api/v1/health` | `data.status`, `data.data_mode` |
| 유행 음식 목록 (출처별 탭) | `GET /api/v1/trends/rankings?source=instagram_graph&period=7d` | `items[].rank`, `food`, `current.value`, `change` |
| 음식 검색 | `GET /api/v1/foods?q=두쫀쿠` | `items[].canonical_name`, `match`, `aliases` |
| 음식 목록 (가나다·최근 관측순) | `GET /api/v1/foods?sort=-last_observed_at` | `items[].last_observed_at` |
| 음식 상세 | `GET /api/v1/foods/{food_id}` | `aliases`, `relations`, `sources[].availability`, `redirect`, `freshness` |
| 일별 추이 그래프 | `GET /api/v1/foods/{food_id}/timeline?period=30d` | `series[].points[]` (`value`, `value_state`, `coverage`) |
| 주별 추이 그래프 | `GET /api/v1/foods/{food_id}/timeline?period=90d&granularity=week` | 주 단위 점 (`complete`) |
| 플랫폼별 지표 | `GET /api/v1/foods/{food_id}/metrics?period=7d` | `sources[].metrics[].current/previous/change`, `provider_values` |
| 데이터 갱신 상태 | `GET /api/v1/sources/status` | `data_through`, `sources[].last_successful_run_at`, `window_stats` |

### 파라미터

| 파라미터 | 사용처 | 값 |
| --- | --- | --- |
| `q` | 음식 목록 | 1~100자, 대표명·승인 별칭 부분 일치(공백·대소문자 무시) |
| `kind` | 음식 목록, 순위 | `DISH`, `PRODUCT`, `INGREDIENT`, `FOOD_CONCEPT` |
| `sort` | 음식 목록 | `relevance`(q 필요, q가 있으면 기본), `name`(기본), `-last_observed_at` |
| `period` | 추이(기본 `30d`), 통계·순위·상태(기본 `7d`) | `7d`, `30d`, `90d` |
| `end_date` | 추이·통계·순위 | `YYYY-MM-DD`(KST), 기본 어제, 최대 오늘. 생략 권장(규칙 6번) |
| `granularity` | 추이 | `day`(기본), `week`(월요일 시작) |
| `source` | 추이·통계(선택), 순위(필수) | `google_trends_rss`, `instagram_graph` (순위에 `youtube`·`naver_datalab`은 422) |
| `metric` | 순위 | `confirmed_items`(기본, 유일) |
| `include_zero` | 순위 | `false`(기본) / `true` |
| `limit`, `cursor` | 음식 목록, 순위 | `limit` 1~100(기본 20), `cursor`는 `page.next_cursor` 그대로 |

## 화면 구현 시 주의

### 그래프

- `value_state`별 표시는 위 [꼭 지켜야 할 규칙](#꼭-지켜야-할-규칙)의 1·2번을 따르세요.
- `coverage`가 `LIMITED`·`PARTIAL`인 점은 하한값입니다(점선·표식 권장).
- `complete: false`인 점(오늘, 기간 경계로 잘린 주)은 다른 점과 높이를 직접 비교하지 마세요.
- `legacy_unknown_value`는 별도 표식으로만 보여 주고 본 값에 더하지 마세요.

### 통계·순위

- 기간 값은 기간의 모든 날이 관측되었을 때만 숫자입니다. `INSUFFICIENT_COVERAGE`이면 `day_counts.observed`/`period.days`로 "7일 중 6일 관측" 같은 안내를 보여 주세요.
- 증가율은 `change.state == "COMPARABLE"`이고 `ratio`가 숫자일 때만 표시하세요. `PREVIOUS_ZERO`는 "신규 관측"처럼 표현할 수 있습니다.
- 순위 화면은 규칙 3번(출처별로만, 종합 점수 없음)을 따르세요.

### 음식 ID

- `GET /api/v1/foods/{food_id}` 응답의 `redirect`가 있으면 병합된 이전 ID입니다. 북마크 등에 저장한 ID를 `data.food_id`로 바꾸세요.

## 예시

[examples/](examples/)의 각 파일은 `{"summary", "request": {"method", "path", "query", "headers"}, "status", "in_spec", "response"}` 형식이며 **Mock 서버가 실제로 돌려준 응답**입니다(`python -m food_trends.app_mock --write-examples docs/app-api/examples`로 다시 만들 수 있고, 테스트가 Mock 서버 출력과 같은지 검사합니다). `in_spec: true`인 예시는 [openapi.yaml](openapi.yaml)의 응답 `examples`에도 들어 있어 Swagger UI·Redoc 같은 도구에서 바로 볼 수 있습니다.

| 화면 | 예시 파일 |
| --- | --- |
| 유행 음식 목록 | `rankings-instagram-7d`, `rankings-rss-7d`, `rankings-rss-30d-zero` |
| 음식 검색·목록 | `foods-search`, `foods-search-empty`, `foods-list`, `foods-recent-page-1`, `foods-recent-page-2` |
| 음식 상세 | `food-detail`, `food-detail-merged-redirect` |
| 일별·주별 추이 그래프 | `food-timeline-7d`, `food-timeline-30d`, `food-timeline-90d-week`, `food-timeline-today` |
| 플랫폼별 지표 | `food-metrics-7d`, `food-metrics-insufficient`, `food-metrics-legacy` |
| 미관측·부분 관측 | `food-timeline-not-observed`, `food-timeline-partial` |
| 데이터 갱신 상태 | `sources-status`, `health` |
| 오류 | `error-*` (400·403·404·405·422·500·503) |

## 버전 정책

- 경로의 `v1`은 주 버전입니다. 1.x에서는 필드·enum 값 추가만 하며 기존 의미를 바꾸지 않습니다. `meta.api_version`으로 세부 버전을 확인할 수 있습니다.
- 파괴적 변경은 `/api/v2`로 별도 제공합니다.

## 실행 환경

저장소 루트에서 실행합니다. 표준 라이브러리만 쓰므로 추가 패키지는 필요 없고, 두 서버 모두 외부 API를 호출하지 않습니다.

### Python 버전

| 버전 | 검증 상태 |
| --- | --- |
| **3.12 (권장)** | 실행 검증 완료. Windows 11 + Python 3.12에서 Push된 커밋을 새로 받아 추가 패키지 없이 Mock 서버를 실행했고, 계약 테스트가 통과했습니다 |
| 3.11 | 프로젝트의 최소 요구 버전입니다. Mock 서버가 불러오는 모듈 전체를 코드 검사해 3.12 전용 문법·표준 라이브러리 기능이 없음을 확인했지만, **3.11로 실제 실행 검증은 하지 않았습니다** |
| 3.13 이상 | 검증하지 않았습니다 |
| 3.10 이하 | 지원하지 않습니다 |

- Windows: [python.org](https://www.python.org/downloads/) 설치 프로그램으로 3.12를 설치하면 `py -3.12`를 쓸 수 있습니다(`py -0`으로 설치된 버전 확인). `py`가 없으면 3.12의 `python`으로 `python -m food_trends.app_mock`을 실행합니다.
- macOS: `brew install python@3.12` 또는 python.org 설치 프로그램으로 설치한 뒤 `python3.12`를 씁니다. `python3 --version`이 3.12이면 `python3`도 됩니다.
- macOS·Linux 실행은 아직 직접 검증하지 않았습니다(Windows와 같은 표준 라이브러리 코드). 문제가 생기면 터미널 출력 전체를 백엔드 담당자에게 보내 주세요.

### Mock 서버 (가상 데이터) — 백엔드 없이 화면 개발

| 목적 | Windows (PowerShell) | macOS (터미널) |
| --- | --- | --- |
| 기본 실행 (`http://127.0.0.1:8030`) | `py -3.12 -m food_trends.app_mock` | `python3.12 -m food_trends.app_mock` |
| 실기기·다른 PC에서 접속 허용 | `py -3.12 -m food_trends.app_mock --host 0.0.0.0` | `python3.12 -m food_trends.app_mock --host 0.0.0.0` |
| 포트 변경 | `py -3.12 -m food_trends.app_mock --port 8031` | `python3.12 -m food_trends.app_mock --port 8031` |

Linux는 macOS와 같은 명령입니다.

- 실제 백엔드 코드(같은 계산 규칙)를 가상 데이터셋에 실행하므로 검색·필터·정렬·페이지·오류가 실제 서버와 같은 계약으로 동작합니다.
- 첫 실행은 가상 데이터셋을 만드느라 15~30초(PC 성능에 따라 다름) 걸리고, 이후에는 1초 안에 시작합니다. 백엔드 코드가 바뀌면(최신 `main`을 받으면) 다시 만듭니다. 데이터는 OS 임시 폴더에만 있습니다.
- 시계가 **2026-11-03 12:00 KST로 고정**되어 있어 응답이 항상 같습니다. 기본 기간은 2026-11-02까지이고 `end_date`는 2026-11-03까지만 받습니다(규칙 6번).
- 가상 데이터 표시(`[MOCK]`, `meta.data_mode`, `meta.notice`)는 규칙 4번을 따르세요.
- 브라우저 웹앱을 위해 모든 Origin에 CORS를 허용합니다.
- 오류 화면 개발: 요청 헤더 `X-Mock-Error: 503`(저장소 사용 불가) 또는 `X-Mock-Error: 500`(내부 오류)을 보내면 해당 오류를 돌려줍니다(Mock 서버 전용).
- 서버 없이 정적 응답이 필요하면 [examples/](examples/)의 JSON을 쓰거나, 예시가 들어 있는 [openapi.yaml](openapi.yaml)을 Prism 같은 OpenAPI Mock 도구에 넣을 수 있습니다(선택 사항, 이 저장소의 의존성이 아님).

| Mock `food_id` | 이름 | 보여 주는 상태 |
| --- | --- | --- |
| `FOOD_MOCK_DUJJONKU` | [MOCK] 두쫀쿠 | 대표 사례. 별칭 여러 개, Instagram 상승(7일 +27%), 두 해시태그 질의에 함께 잡힌 게시물 중복 제거, 병합된 이전 ID, 파생 음식, 10-15 한 질의 실패(`PARTIAL`), RSS 제공자 표시값 |
| `FOOD_MOCK_MALATANG` | [MOCK] 마라탕 | 꾸준한 관측, 출처 불명 과거 데이터(`legacy_unknown`) |
| `FOOD_MOCK_TANGHULU` | [MOCK] 탕후루 | 하락(7일 −20%) |
| `FOOD_MOCK_YAKGWA` | [MOCK] 약과 | 10-30 수집 실패(`NOT_OBSERVED`), 11-01 페이지 상한(`LIMITED`) → 7일 순위 제외 |
| `FOOD_MOCK_BUTTERTTEOK` | [MOCK] 버터떡 | 10-31 신규 등록: Instagram 7일 `INSUFFICIENT_COVERAGE`, RSS 급상승, 오늘 포함 점 |
| `FOOD_MOCK_PISTACHIO` | [MOCK] 피스타치오 크림 | RSS만 관측 (Instagram 미추적 → `NOT_OBSERVED`) |
| `FOOD_MOCK_HEUGIMJA` | [MOCK] 흑임자 라떼 | 정상 관측 0(RSS)과 미관측(Instagram)의 차이 |
| `FOOD_MOCK_JJAPJJONKU` | [MOCK] 짭쫀쿠 | 두쫀쿠의 파생 음식(`VARIANT_OF`), 수치는 따로 셈 |
| `FOOD_MOCK_DUBAI_COOKIE` | [MOCK] 두바이 쿠키 | 두쫀쿠로 병합된 이전 ID → 상세에서 `redirect` |

데이터셋 특성: Google 트렌드 RSS는 180일(07-20 수집 장애 → 90일 변화 `NOT_COMPARABLE`), Instagram은 40일 추적입니다. Instagram 캡션 근거는 30일 보존이라 **30일·90일 Instagram 기간 값은 가장 오래된 날의 근거 만료로 대부분 `INSUFFICIENT_COVERAGE`**입니다. 실제 운영에서도 같은 제약이므로 화면이 이 상태를 처리해야 합니다.

### Base URL 설정

`127.0.0.1`(`localhost`)은 **요청을 보내는 기기 자기 자신**을 뜻합니다. 그래서 같은 서버라도 앱이 어디서 실행되는지에 따라 Base URL이 달라집니다.

**Mock 서버와 앱(에뮬레이터·시뮬레이터)이 같은 PC에 있을 때**

| 앱 실행 위치 | Base URL | 이유 |
| --- | --- | --- |
| iOS 시뮬레이터 (Mac) | `http://127.0.0.1:8030` | 시뮬레이터는 Mac의 네트워크를 그대로 쓰므로 `127.0.0.1`이 Mac 자신입니다 |
| Android 에뮬레이터 | `http://10.0.2.2:8030` | 에뮬레이터 안의 `127.0.0.1`은 에뮬레이터 자신입니다. `10.0.2.2`가 에뮬레이터를 실행한 PC를 가리킵니다 |
| 데스크톱 앱, 같은 PC의 브라우저 웹앱 | `http://127.0.0.1:8030` | |
| Android 실기기 (USB 연결) | `http://127.0.0.1:8030` | PC에서 `adb reverse tcp:8030 tcp:8030`을 먼저 실행합니다(휴대폰의 8030 포트를 PC로 연결) |
| iOS·Android 실기기 (Wi-Fi) | `http://<서버 PC의 IP>:8030` | 실기기의 `127.0.0.1`은 휴대폰 자신입니다. 아래 방법을 따르세요 |

**Mock 서버가 다른 PC에 있거나 실기기에서 Wi-Fi로 접속할 때** (예: 백엔드 담당자 PC의 Mock 서버를 함께 쓰는 경우)

- 에뮬레이터·시뮬레이터·실기기 모두 `127.0.0.1`이나 `10.0.2.2`가 아니라 **서버 PC에 접근할 수 있는 IP 주소**를 씁니다. 예: `http://192.168.0.23:8030`
- 서버 PC에서 Mock 서버를 `--host 0.0.0.0`으로 실행해야 합니다. 기본값(`127.0.0.1`)으로 실행하면 같은 PC에서만 접속할 수 있습니다.
- 서버 PC 방화벽에서 TCP 8030 수신을 허용합니다(Windows는 첫 실행 때 뜨는 방화벽 창에서 허용).
- 두 기기가 같은 네트워크에 있어야 합니다. 학교·회사 Wi-Fi는 기기끼리의 통신을 막는 경우가 많으니, 연결되지 않으면 휴대폰 핫스팟 같은 개인 네트워크를 쓰세요.
- 서버 PC의 IP 확인: Windows는 `ipconfig`의 "IPv4 주소", macOS는 `ipconfig getifaddr en0`.
- 연결 확인: 앱을 실행할 기기의 브라우저에서 `http://<서버 PC의 IP>:8030/api/v1/health`가 열리는지 먼저 봅니다.
- iOS 14 이상 실기기는 LAN 주소에 처음 접속할 때 "로컬 네트워크" 접근 허용을 묻습니다. 허용해야 하며, Info.plist에 `NSLocalNetworkUsageDescription`을 넣어 두세요.

**공통**

- 도메인 이름(예: `my-pc.local`, `backend-pc`)으로 접속하면 `403 FORBIDDEN_HOST`입니다. 항상 IP 주소나 `localhost`를 쓰세요(웹 페이지의 DNS 재바인딩 공격 방어).
- 평문 HTTP입니다. Android는 개발 빌드에만 cleartext 허용, iOS는 개발 빌드에만 ATS 예외(`NSAllowsLocalNetworking` 등)를 두고 배포 빌드에는 넣지 마세요([ACCESS.md](ACCESS.md) §4).
- 실제 데이터 서버는 현재 루프백 전용이라 다른 PC나 Wi-Fi 기기에서 접속할 수 없습니다. 위의 다른 PC 접속 방식은 Mock 서버에만 해당합니다.
- Base URL은 빌드 설정(환경별 설정 파일·빌드 플래그)으로 주입하고 코드에 하드코딩하지 마세요. 앱 시작 시 `/api/v1/health`로 연결과 `data_mode`를 확인하는 것을 권장합니다.

### 실제 데이터 서버 (읽기 전용, 로컬)

Windows (PowerShell)

```powershell
py -3.12 -m food_trends.app_api --database <최신 스키마의 SQLite DB 경로>                 # http://127.0.0.1:8020
py -3.12 -m food_trends.app_api --database <경로> --cors-origin http://localhost:3000  # 브라우저 웹앱 개발 시
py -3.12 -m food_trends.app_api --database <경로> --observation-origin mock            # B3 Mock 수집으로 만든 테스트 DB
```

macOS (터미널)

```bash
python3.12 -m food_trends.app_api --database <최신 스키마의 SQLite DB 경로>                 # http://127.0.0.1:8020
python3.12 -m food_trends.app_api --database <경로> --cors-origin http://localhost:3000  # 브라우저 웹앱 개발 시
python3.12 -m food_trends.app_api --database <경로> --observation-origin mock            # B3 Mock 수집으로 만든 테스트 DB
```

- DB를 읽기 전용으로 열고 쓰기·마이그레이션·외부 호출을 하지 않습니다. 스키마가 최신(v11)이 아니면 시작하지 않습니다. 운영 SQLite 원본(v0)은 보관용이라 이 서버로 열 수 없습니다. **운영 데이터는 아래 Oracle 운영 서버(`--store oracle-production`)가 제공합니다.**
- 루프백(`127.0.0.1`)에만 바인딩합니다. 실기기는 `adb reverse tcp:8020 tcp:8020`으로만 접속할 수 있고, 다른 기기 공개는 [ACCESS.md](ACCESS.md) §6의 별도 과제입니다.
- 기본으로 `live_api` 회차만 집계합니다. Mock 수집 회차가 섞인 DB에서도 Mock 행은 값에 들어가지 않습니다.
- 읽을 저장소는 `--store`로 명시합니다(기본 `sqlite`). 어느 저장소든 응답 계약은 같으므로 앱 코드는 바꿀 필요가 없습니다.

| `--store` | 읽는 곳 | 용도 |
| --- | --- | --- |
| `sqlite` (기본) | `--database`로 지정한 최신 스키마(v11) SQLite DB | 개발·검증 |
| `oracle-test` | 백엔드 개발 PC의 `ORACLE_TEST_*` 환경변수가 가리키는 개발용 Oracle 테스트 스키마. 요청마다 읽기 전용 트랜잭션 (`python-oracledb` 필요) | 백엔드의 Oracle 동등성 확인 |
| `oracle-production` | 운영 Oracle 스키마 `MZ_APP`(백엔드 운영 PC의 `data\oracle-prod\oracle-prod.env`: 선언된 운영 스키마와 승인 기록이 있어야 열림). 요청마다 읽기 전용 트랜잭션 | **실제 운영 데이터** (아래 절) |

Oracle 테스트 스키마의 접속 정보는 앱 개발자에게 제공하지 않습니다. 화면 개발은 Mock 서버 또는 SQLite 서버로 충분합니다.

### 실제 운영 서버 (Oracle 운영 스키마, 읽기 전용)

ORACLE-PRODUCTION-CUTOVER-1부터 운영 데이터의 주 저장소는 Oracle 운영 스키마 `MZ_APP`입니다(기존 SQLite 데이터 이관 + 실제 수집 데이터). 관리 서버와 앱 API가 **같은 스키마**를 읽고, 앱 API는 읽기 전용 트랜잭션만 씁니다(DML·DDL 없음).

백엔드 운영 PC(Windows, Oracle은 WSL2·Podman 컨테이너)에서:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\start-oracle.ps1    # Oracle 시작·운영 스키마 응답 확인
powershell -ExecutionPolicy Bypass -File scripts\ops\start-backend.ps1   # 관리 서버 :8000 + 앱 API :8020 (중복 실행 거부)
powershell -ExecutionPolicy Bypass -File scripts\ops\status.ps1          # 상태 확인
powershell -ExecutionPolicy Bypass -File scripts\ops\verify.ps1          # 비파괴 점검(앱 API 응답을 OpenAPI로 검증)
powershell -ExecutionPolicy Bypass -File scripts\ops\stop-backend.ps1    # 정지 → 재기동은 start-backend.ps1
```

앱 API만 직접 띄울 때(운영 환경변수를 불러온 셸에서): `py -3.12 -m food_trends.app_api --store oracle-production --port 8020`.

| 항목 | Mock 서버 | 실제 운영 서버 |
| --- | --- | --- |
| Base URL (백엔드 PC 자신) | `http://127.0.0.1:8030/api/v1` | `http://127.0.0.1:8020/api/v1` |
| Android 에뮬레이터(같은 PC) | `http://10.0.2.2:8030/api/v1` | `http://10.0.2.2:8020/api/v1` |
| Android 실기기(USB) | `adb reverse tcp:8030 tcp:8030` 후 `127.0.0.1:8030` | `adb reverse tcp:8020 tcp:8020` 후 `http://127.0.0.1:8020/api/v1` |
| 다른 PC·Mac·Wi-Fi 실기기 | `--host 0.0.0.0`으로 접속 가능 | **접속 불가**(루프백 전용). Mac·iOS 개발은 Mock 서버를 쓰고, 실데이터 확인은 백엔드 PC의 에뮬레이터·USB 실기기로 합니다 |
| `meta.data_mode` | `mock` | `database`(`observation_origin: live_api`) |

실데이터 확인:

1. `GET /api/v1/health` → 200, `data_mode`가 `database`.
2. `GET /api/v1/foods` → 승인된 음식 목록(현재 `두쫀쿠` 1개). `food_id`는 목록 응답에서 받으세요.
3. `GET /api/v1/foods/{food_id}/timeline?period=7d&source=instagram_graph` → 기본 기간은 어제(KST)까지입니다. 오늘 수집분은 `end_date`에 오늘(KST)을 주면 보입니다.
4. `GET /api/v1/sources/status` → 출처별 마지막 성공 수집 시각.

연결 오류 대응: `503 STORAGE_UNAVAILABLE`이면 Oracle이 내려간 상태입니다(백엔드 담당자가 `start-oracle.ps1`; 앱 API는 재시작 없이 다시 연결됩니다). 연결 거부면 앱 API 프로세스가 없는 상태입니다(`start-backend.ps1`). `403 FORBIDDEN_HOST`면 도메인 이름 대신 IP·`localhost`를 쓰세요. 관리 서버(:8000)의 관리 토큰은 앱에 넣지 않습니다([ACCESS.md](ACCESS.md) §2).

### Mock에서 실제 서버로 바꿀 때

| 항목 | Mock | 실제 서버 |
| --- | --- | --- |
| Base URL 포트 | 8030 | 8020 |
| `meta.data_mode` / `meta.notice` | `mock` / 안내문 | `database` / `null` (테스트 DB면 `observation_origin: mock`과 안내문) |
| 날짜 | 2026-11-03 고정 (`end_date` 최대 2026-11-03) | 현재 날짜 (기본 기간은 어제까지) |
| `food_id` | `FOOD_MOCK_...` | `FOOD_<영숫자>` — 어떤 ID도 코드에 고정하지 말고 목록·검색 응답에서 받으세요 |
| 데이터 양 | 시나리오별로 고르게 채움 | MVP 단계라 음식·관측이 적고 `NOT_OBSERVED`가 많을 수 있음. 빈 상태·미관측 화면이 기본 상태일 수 있습니다 |
| 오류 시뮬레이션 | `X-Mock-Error` 헤더 | 없음 (헤더는 무시) |

계약(경로·필드·enum·오류 형식)은 같으므로 앱 코드는 Base URL만 바꾸면 됩니다.

## 아직 제공하지 않는 기능

| 기능 | 상태 |
| --- | --- |
| 인터넷 공개 배포, TLS, API 키·속도 제한 | 별도 과제 ([ACCESS.md](ACCESS.md) §6) |
| 사용자 계정·로그인·즐겨찾기 동기화·푸시 알림 | 범위 밖. 앱 로컬 저장으로 구현하세요 |
| 출처를 합친 종합 유행 점수 | 제공하지 않음. 공식이 확정되기 전에는 활성화하지 않습니다 |
| YouTube 음식 언급·지표 | 정책상 비활성 (`POLICY_BLOCKED`) |
| NAVER 검색 지수 | 저장·집계하지 않음 (`NOT_STORED`). 앱 API로 실시간 조회도 제공하지 않습니다 |
| 개별 콘텐츠(게시물·기사) 원문·링크·이미지 | 제공하지 않음 (출처 정책·재배포 권한 미확인) |
| 음식 대표 이미지·설명문 | 없음. 앱에서 기본 이미지·아이콘을 쓰세요 |
| 사용자 지정 기간(start_date~end_date), 시간 단위 추이 | v1은 7·30·90일, 일·주 단위만 |
| 저장 집계(빠른 대용량 조회) | 읽기 시 계산. 설계만 있음 |
| 운영 Oracle 저장소로 서비스 | 미승인 (`--store oracle-production`은 시작 거부). 개발용 Oracle 테스트 스키마(`--store oracle-test`)만 지원 |
