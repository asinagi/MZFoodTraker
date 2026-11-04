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

저장소 루트에서 실행합니다. Python 3.11 이상만 있으면 되고 추가 패키지는 필요 없습니다. 두 서버 모두 외부 API를 호출하지 않습니다.

### Mock 서버 (가상 데이터) — 백엔드 없이 화면 개발

```powershell
py -3.12 -m food_trends.app_mock                 # http://127.0.0.1:8030
py -3.12 -m food_trends.app_mock --host 0.0.0.0  # 같은 Wi-Fi의 실기기에서 접속 (가상 데이터만 제공)
py -3.12 -m food_trends.app_mock --port 8031     # 포트 변경
```

- 실제 백엔드 코드(같은 계산 규칙)를 가상 데이터셋에 실행하므로 검색·필터·정렬·페이지·오류가 실제 서버와 같은 계약으로 동작합니다.
- 첫 실행은 가상 데이터셋을 만드느라 약 15초 걸리고, 이후에는 바로 시작합니다(백엔드 코드가 바뀌면 다시 만듭니다). 데이터는 OS 임시 폴더에만 있습니다.
- 시계가 **2026-11-03 12:00 KST로 고정**되어 있어 응답이 항상 같습니다. 기본 기간은 2026-11-02까지입니다.
- 모든 음식명은 `[MOCK] `으로 시작하고 모든 응답의 `meta.data_mode`가 `mock`, `meta.notice`가 "가상 데이터입니다…"입니다. 화면 캡처가 실제 유행으로 오해되지 않게 하세요.
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

| 앱 실행 위치 | Mock 서버 Base URL | 비고 |
| --- | --- | --- |
| iOS 시뮬레이터, 데스크톱, 같은 PC의 웹 | `http://127.0.0.1:8030` | |
| Android 에뮬레이터 | `http://10.0.2.2:8030` | 에뮬레이터에서 본 호스트 PC 주소 |
| Android 실기기(USB) | `http://127.0.0.1:8030` | PC에서 `adb reverse tcp:8030 tcp:8030` 먼저 실행 |
| 실기기(같은 Wi-Fi) | `http://<PC의 LAN IP>:8030` | Mock 서버를 `--host 0.0.0.0`으로 실행, PC 방화벽 허용 필요 |

- 도메인 이름(예: `my-pc.local`)으로 접속하면 `403 FORBIDDEN_HOST`입니다. IP 주소나 `localhost`를 쓰세요.
- 평문 HTTP입니다. Android는 개발 빌드에만 cleartext 허용, iOS는 개발 빌드에만 ATS 예외(`NSAllowsLocalNetworking` 등)를 두세요([ACCESS.md](ACCESS.md) §4).
- Base URL은 빌드 설정(환경별 설정 파일·빌드 플래그)으로 주입하고 코드에 하드코딩하지 마세요. 앱 시작 시 `/api/v1/health`로 연결과 `data_mode`를 확인하는 것을 권장합니다.

### 실제 데이터 서버 (읽기 전용, 로컬)

```powershell
py -3.12 -m food_trends.app_api --database <최신 스키마의 SQLite DB 경로>                 # http://127.0.0.1:8020
py -3.12 -m food_trends.app_api --database <경로> --cors-origin http://localhost:3000  # 브라우저 웹앱 개발 시
py -3.12 -m food_trends.app_api --database <경로> --observation-origin mock            # B3 Mock 수집으로 만든 테스트 DB
```

- DB를 읽기 전용으로 열고 쓰기·마이그레이션·외부 호출을 하지 않습니다. 스키마가 최신(v11)이 아니면 시작하지 않습니다. **현재 운영 DB는 마이그레이션 전(v0)이라 이 서버로 열 수 없으며, 운영 DB 마이그레이션은 이번 작업 범위가 아닙니다.**
- 루프백(`127.0.0.1`)에만 바인딩합니다. 실기기는 `adb reverse tcp:8020 tcp:8020`으로만 접속할 수 있고, 다른 기기 공개는 [ACCESS.md](ACCESS.md) §6의 별도 과제입니다.
- 기본으로 `live_api` 회차만 집계합니다. Mock 수집 회차가 섞인 DB에서도 Mock 행은 값에 들어가지 않습니다.

### Mock에서 실제 서버로 바꿀 때

| 항목 | Mock | 실제 서버 |
| --- | --- | --- |
| Base URL 포트 | 8030 | 8020 |
| `meta.data_mode` / `meta.notice` | `mock` / 안내문 | `database` / `null` (테스트 DB면 `observation_origin: mock`과 안내문) |
| 날짜 | 2026-11-03 고정 | 현재 날짜 (기본 기간은 어제까지) |
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
| 앱 API의 Oracle 저장소 연결 | 읽기 계층은 준비됨, 서버 실행 옵션은 Oracle 통합 이후 |
