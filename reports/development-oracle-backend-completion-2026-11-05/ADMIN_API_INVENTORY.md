# 관리 웹 서버 API 전수 점검 — Oracle 연결 전후

2026-11-05, ORACLE-BACKEND-COMPLETION-1 Phase B. 대상: `food_trends/api.py` `make_server`와 그 처리기(`food_http`·`tracking_http`·`planner_http`·`execution_http`), 화면 `static/*.js`. B8 읽기 전용 앱 API(`/api/v1/*`, `app_api.py`)는 별도 서버이며 변경하지 않았다.

점검 방법: `api.py`의 GET·POST 분기와 처리기 함수를 모두 읽고, 화면 스크립트의 `fetch`/`api()` 호출 경로를 대조했다. "이전 Oracle"은 이 작업 전(`9f3bdb6`)에 Oracle 서비스를 주입했을 때의 동작이다. 당시에는 Oracle 서비스를 주입해 서버를 띄우는 실행 경로 자체가 없었다(`make_server`가 RSS `service`를 SQLite `CollectionService`로만 받고, 나머지 서비스는 주입하지 않으면 SQLite로 만든다).

## 1. 경로별 결과

| 경로 | 화면 호출처 | 처리 서비스 | 이전 Oracle | 현재 Oracle |
| --- | --- | --- | --- | --- |
| `GET /`, 정적 파일 10개 | 브라우저 | 파일(토큰 meta 주입) | 동작 | 동작 |
| `GET /api/status` | app.js | RSS `service.status()` | **누락**(SQLite `Storage`만) | `OracleRssService.status()` |
| `GET /api/trends`, `/api/trends/{id}` | app.js | `service.storage.trends()` | **누락** | `OracleRssStore.trends()`(같은 항목 번호·모양) |
| `GET /api/export` | index.html 링크 | `service.storage.export()` | **누락** | `OracleRssStore.export()`(원본 응답은 대상에 보관하지 않음: `raw_response_retained:false`) |
| `POST /api/collect` | app.js | `service.collect()` | **누락** | `OracleRssService.collect()`: Mock 응답만 수집, Mock 없이는 409 `oracle_live_collection_disabled` |
| `GET /api/foods`, `/api/foods/{id}`, `/api/foods/resolve`, `/api/food-candidates(/{id})`, `/api/food-events` | foods.js | `OracleFoodService` | 동작(주입 시) | 동작 |
| `POST /api/foods/{submit,review,rename,alias,revert-alias,relate,merge,resolve}` | foods.js | `OracleFoodService.command` | 동작(주입 시) | 동작 |
| `POST /api/foods/import` | foods.js | `OracleFoodService._do_import` | **409 거부**(SQLite RSS 저장소 전용) | 대상 RSS 행에서 가져오기 |
| `GET /api/tracking/{accounts,targets,due,events}` | tracking.js | `OracleTrackingService` | 동작(주입 시) | 동작 |
| `POST /api/tracking/{register,activate,pause,resume,stop,interval,refresh,schedule}` | tracking.js | `OracleTrackingService.command` | 동작(주입 시) | 동작 |
| `GET /api/planner/{preview,history,plans/{id}}`, `POST /api/planner/{preview,commit}` | planner.js | `OracleQueryPlanner` | 동작(주입 시) | 동작 |
| `GET /api/execution/{status,jobs/{id}}` | execution.js | `ExecutionController.status` → `OracleExecution.status` | 동작, 수집원별 요약 없음 | **수집원별 요약 추가**(`sources`: 작업 상태별 수, 마지막 성공, 최근 실패, 차단, 최근 계획의 예산 / RSS는 회차 기준) |
| `POST /api/execution/{enqueue,schedule,run,tick}` | execution.js(schedule·run) | 같은 컨트롤러 | 동작(주입 시) | 동작 |
| `POST /api/execution/recover-rss` | (API만) | `ExecutionController.recover_rss` | **500**(SQLite `storage`·`tracking` 참조) | `OracleExecutionController`: 대상 RSS 행에서 후보 가져오기 재시도 |
| `GET /api/youtube`·`/api/naver`·`/api/instagram`·`/api/instagram/media`, `POST /api/youtube/*`·`/api/naver/*`·`/api/instagram/*` | youtube.js·naver.js·instagram.js | 레거시 직접 수집기(SQLite 전용 서비스) | 해당 없음 | **503**(서비스 미연결). Oracle에서는 추적·계획·B3 실행(Mock)과 B8 조회가 이 기능을 대신한다 |

## 2. 누락 Endpoint 목록과 처리

| 누락 | 처리(최소 구현) |
| --- | --- |
| Oracle 모드로 관리 서버를 띄우는 실행 경로 | `python -m food_trends --backend oracle-test [--rss-mock-file PATH]`(`__main__.oracle_admin_server`). 같은 `make_server`에 Oracle 서비스를 주입한다. SQLite 명령과 섞으면 거부, `oracle-production`은 연결 전에 거부 |
| `/api/status`·`/api/trends*`·`/api/export`·`POST /api/collect`(RSS) | `food_trends/oracle_rss.py`(Phase A) |
| `POST /api/foods/import` | FoodService 가져오기 판단을 저장 기본 연산으로 분리, Oracle 구현 추가(Phase A) |
| `POST /api/execution/recover-rss` | `ExecutionController`의 RSS 읽기 두 개를 분리, Oracle 컨트롤러가 대체(Phase A) |
| 수집원별 수집·실패·예산 상태 | `/api/execution/status`에 `sources` 추가(추가 필드, 기존 필드 불변). execution.js에 수집원별 한 줄 표시 |
| Oracle 유지보수(보존 정리·별칭 변경 재처리) | `python -m food_trends --backend oracle-test --maintenance`(`runtime_storage.oracle_maintenance`) |

## 3. 바꾸지 않은 것

- 인증·상태 변경 보호: 루프백 바인딩, Host 검사(DNS rebinding), 같은 Origin, 모든 POST의 `X-MZ-Admin-Token`(상수 시간 비교), 처리기별 크기·Content-Type 검사. Oracle 모드도 같은 `make_server`라 그대로다(테스트로 401·403·413·415 확인).
- 처리기 모듈(`food_http`·`tracking_http`·`planner_http`·`execution_http`)과 응답 코드 규칙.
- B8 읽기 전용 API와 OpenAPI v1.
- 웹 화면 구조(수집원별 상태 한 줄 표시 외 변경 없음).
