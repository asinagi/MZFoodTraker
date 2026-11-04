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

## 공통 규칙 요약

- **Base URL** 뒤에 `/api/v1/...` 경로를 붙입니다. 예: `http://127.0.0.1:8030/api/v1/health`.
- **GET만** 사용합니다. 인증 헤더가 필요 없고, 관리 토큰(`X-MZ-Admin-Token`)을 보내면 거부됩니다.
- **응답 형식**: 성공 `{"data": ..., "meta": ...}`(목록은 `page` 추가), 오류 `{"error": ..., "meta": ...}`.
- **시각**은 UTC 초 단위 `2026-11-02T03:00:00Z`, **일자**는 KST `2026-11-02`입니다.
- **`value: null`은 0이 아닙니다.** 관측하지 못했거나 계산할 수 없다는 뜻이며 이유는 `value_state`에 있습니다.
- **출처끼리 합산하지 않습니다.** 출처별 값을 따로 보여 주세요. 종합 유행 점수는 없습니다.
- 스키마의 필드는 항상 존재합니다(값이 없으면 `null`). 모르는 필드·enum 값은 무시하세요.
- `meta.data_mode`가 `mock`이면 `meta.notice`를 함께 표시하세요.
- 모르는 쿼리 파라미터는 `400 INVALID_PARAMETER`입니다(오타 조기 발견용).

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
| `end_date` | 추이·통계·순위 | `YYYY-MM-DD`(KST), 기본 어제, 최대 오늘 |
| `granularity` | 추이 | `day`(기본), `week`(월요일 시작) |
| `source` | 추이·통계(선택), 순위(필수) | `google_trends_rss`, `instagram_graph` (순위에 `youtube`·`naver_datalab`은 422) |
| `metric` | 순위 | `confirmed_items`(기본, 유일) |
| `include_zero` | 순위 | `false`(기본) / `true` |
| `limit`, `cursor` | 음식 목록, 순위 | `limit` 1~100(기본 20), `cursor`는 `page.next_cursor` 그대로 |

## 화면 구현 시 주의

### 그래프

- `value_state`별로 다르게 그리세요. `OBSERVED`는 값(0 포함), `NOT_OBSERVED`는 선을 끊거나 회색 표시, `EVIDENCE_EXPIRED`는 "보존 기간 경과" 표시. `null`을 0으로 이어 그리면 "관측 실패"가 "인기 없음"으로 잘못 보입니다.
- `coverage`가 `LIMITED`·`PARTIAL`인 점은 하한값입니다(점선·표식 권장).
- `complete: false`인 점(오늘, 기간 경계로 잘린 주)은 다른 점과 높이를 직접 비교하지 마세요.
- `legacy_unknown_value`는 별도 표식으로만 보여 주고 본 값에 더하지 마세요.

### 통계·순위

- 기간 값은 기간의 모든 날이 관측되었을 때만 숫자입니다. `INSUFFICIENT_COVERAGE`이면 `day_counts.observed`/`period.days`로 "7일 중 6일 관측" 같은 안내를 보여 주세요.
- 증가율은 `change.state == "COMPARABLE"`이고 `ratio`가 숫자일 때만 표시하세요. `PREVIOUS_ZERO`는 "신규 관측"처럼 표현할 수 있습니다.
- 순위는 출처별로만 있습니다. 출처 탭(Google 트렌드 / Instagram)으로 나눠 보여 주세요.

### 음식 ID

- `GET /api/v1/foods/{food_id}` 응답의 `redirect`가 있으면 병합된 이전 ID입니다. 북마크 등에 저장한 ID를 `data.food_id`로 바꾸세요.

## 예시

[examples/](examples/)의 각 파일은 `{"summary", "request": {"method", "path", "query"}, "status", "response"}` 형식입니다. 같은 예시가 [openapi.yaml](openapi.yaml)의 응답 `examples`에도 들어 있어 Swagger UI·Redoc·Prism 같은 도구에서 바로 볼 수 있습니다.

## 버전 정책

- 경로의 `v1`은 주 버전입니다. 1.x에서는 필드·enum 값 추가만 하며 기존 의미를 바꾸지 않습니다. `meta.api_version`으로 세부 버전을 확인할 수 있습니다.
- 파괴적 변경은 `/api/v2`로 별도 제공합니다.

## 실행 환경

Mock 서버(가상 데이터)와 실제 데이터 서버의 실행 방법, Base URL 설정은 다음 단계(Mock·실제 API 구현)에서 이 문서에 추가합니다.
