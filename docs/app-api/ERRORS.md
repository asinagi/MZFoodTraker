# 앱 데이터 API 오류 코드 (v1)

모든 오류는 같은 형식입니다. HTTP 상태 코드로 큰 분류(요청 문제인지 서버 문제인지, 재시도할지)를 정하고 `error.code`로 세부 처리를 정하세요. 같은 400에도 여러 코드가 있고 1.x에서 코드가 추가될 수 있으므로, 앱이 모르는 코드는 상태 코드 분류(4xx 요청 문제, 5xx 서버 문제)로 처리합니다. 처리 순서는 [README.md의 오류 처리](README.md#오류-처리)에 있습니다. `error.message`는 사용자에게 그대로 보여 줘도 되는 한국어 설명이지만 분기 조건으로는 쓰지 마세요.

```json
{
  "error": {
    "code": "INVALID_PARAMETER",
    "message": "요청 파라미터를 확인하세요.",
    "details": [{"parameter": "period", "reason": "unsupported_value", "allowed": ["7d", "30d", "90d"]}]
  },
  "meta": {"api_version": "1.0.0", "data_mode": "mock", "observation_origin": "mock",
           "generated_at": "2026-11-03T03:00:00Z", "timezone": "Asia/Seoul", "notice": "가상 데이터입니다. 실제 유행 분석 결과가 아닙니다."}
}
```

## 코드 목록

| HTTP | `code` | 발생 조건 | 앱 처리 권장 |
| --- | --- | --- | --- |
| 400 | `INVALID_PARAMETER` | 모르는 파라미터, 같은 파라미터 반복, 필수 파라미터 누락, 허용 값 밖, 형식 오류(날짜·숫자), 범위 밖(`end_date`가 오늘 이후 등), `sort=relevance`인데 `q` 없음 | 개발 중 버그로 보고 `details`를 로그로 남기세요. 사용자 입력(검색어 길이 등)이면 입력 안내 |
| 400 | `INVALID_CURSOR` | 커서가 손상되었거나 다른 조건의 요청에서 받은 커서 | 커서를 버리고 첫 페이지부터 다시 요청 |
| 400 | `ADMIN_TOKEN_NOT_ACCEPTED` | 요청에 `X-MZ-Admin-Token` 헤더가 있음 | 앱 코드에서 관리 토큰을 제거하세요. 앱 API에는 인증 헤더가 필요 없습니다 ([ACCESS.md](ACCESS.md)) |
| 403 | `FORBIDDEN_HOST` | `Host`가 IP 주소·`localhost`가 아님 (DNS 재바인딩 방어) | Base URL을 IP 주소(`127.0.0.1`, `10.0.2.2`, LAN IP) 또는 `localhost`로 설정 |
| 404 | `FOOD_NOT_FOUND` | 등록되지 않은 `food_id` | "음식을 찾을 수 없음" 화면. 저장된 ID라면 목록을 새로 받으세요 |
| 404 | `ROUTE_NOT_FOUND` | 지원하지 않는 경로 | Base URL·경로(`/api/v1/...`) 확인 |
| 405 | `METHOD_NOT_ALLOWED` | POST·PUT·PATCH·DELETE (GET·HEAD·OPTIONS만 지원. TRACE 같은 기타 메서드는 HTTP 서버 기본 501) | 읽기 전용 API입니다. 응답 `Allow` 헤더 참고 |
| 422 | `SOURCE_NOT_RANKABLE` | 순위에 `youtube`(정책상 비활성) 또는 `naver_datalab`(저장하지 않는 지수) 지정 | 순위 출처는 `google_trends_rss`, `instagram_graph`만 사용 |
| 500 | `INTERNAL_ERROR` | 처리 중 예기치 않은 오류 | 잠시 후 재시도, 반복되면 백엔드 담당자에게 시각과 함께 전달 |
| 503 | `DATA_UNAVAILABLE` | 저장소 스키마가 준비되지 않았거나 DB를 열 수 없음 | 점검 안내 후 지수 백오프로 재시도 |

## `details[].reason` 값

| `reason` | 의미 |
| --- | --- |
| `unknown_parameter` | 계약에 없는 쿼리 파라미터. 오타를 조기에 잡기 위해 무시하지 않고 거부합니다 |
| `repeated_parameter` | 같은 파라미터를 두 번 이상 보냄 |
| `required` | 필수 파라미터 누락 (`source` 등) |
| `unsupported_value` | 허용 목록에 없는 값. `allowed`에 허용 값이 있습니다 |
| `invalid_format` | 날짜·정수 형식 오류 또는 경로의 `food_id` 형식 오류 |
| `out_of_range` | 범위를 벗어남 (`limit` 1~100, `end_date` 2000-01-01~오늘, `q` 1~100자) |
| `requires_parameter` | 다른 파라미터가 있어야만 쓸 수 있는 값 (`sort=relevance`는 `q` 필요) |
| `malformed`, `mismatched_query` | 커서 손상, 다른 조건의 커서 |
| `forbidden_header` | 받지 않는 헤더 (`X-MZ-Admin-Token`) |
| `not_ip_or_localhost` | 허용되지 않은 Host |
| `not_found` | 대상 없음 |
| `unknown_route`, `method_not_allowed` | 경로·메서드 오류 |
| `policy_blocked`, `not_stored` | 순위를 만들 수 없는 출처 (`allowed`에 순위 가능 출처) |
| `schema_not_ready`, `storage_unavailable` | 저장소 사용 불가 |
| `internal` | 내부 오류 (세부 내용은 응답에 넣지 않습니다) |
| `mock_simulated` | Mock 서버에서 오류 화면 개발용으로 일부러 만든 오류 |

`reason` 목록은 1.x에서 늘어날 수 있습니다. 모르는 값은 `code` 기준으로 처리하세요.

모든 코드의 실제 응답은 [examples/](examples/)의 `error-*.json`에 있습니다. Mock 서버에서는 요청 헤더 `X-Mock-Error: 503` 또는 `500`으로 `DATA_UNAVAILABLE`·`INTERNAL_ERROR` 화면을 재현할 수 있습니다(실제 서버는 이 헤더를 무시).

## 데이터가 없는 것은 오류가 아닙니다

다음은 모두 **200 정상 응답**입니다. 오류 화면이 아니라 빈 상태·미관측 상태로 그리세요.

| 상황 | 응답 |
| --- | --- |
| 검색 결과 없음 | `data.items: []`, `page.total_items: 0` |
| 관측되지 않은 날 | 점의 `value: null`, `value_state: "NOT_OBSERVED"` |
| 기간 일부만 관측 | 기간 `value: null`, `value_state: "INSUFFICIENT_COVERAGE"`, `day_counts`로 관측 일수 안내 |
| 정책상 제공하지 않는 출처 | `availability: "POLICY_BLOCKED"` 또는 `"NOT_STORED"`와 `availability_reason` |
| 순위 대상 없음 | `data.items: []`, `excluded`에 제외 사유별 수 |
