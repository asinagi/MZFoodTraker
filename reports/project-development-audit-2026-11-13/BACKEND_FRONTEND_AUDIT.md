# 백엔드·프론트엔드 감사 — MZFoodTraker

- 감사일: 2026-11-13 / 기준 커밋: `main` `363ea0f` (작업 트리 clean, 커밋 71개, 최초 커밋 2026-10-27)
- 방식: **읽기 전용 코드·문서·증거 파일 대조**. 서버 기동·테스트 실행·외부 API 호출·DB 접속은 하지 않았다. 그래서 "동작한다"는 판정은 모두 저장소에 남은 과거 증거(보고서·JSON·스크린샷)를 근거로 하며, 이번 감사에서 직접 재현하지 않았다.
- 표기: **실데이터** = DB(SQLite 또는 Oracle) 저장값을 읽음 / **Mock** = 가상 데이터 또는 Mock 응답 / **미확인** = 증거로 확인할 수 없음.

---

## 1. 요약

| 항목 | 결론 |
| --- | --- |
| 언어·프레임워크 | Python 3.12 **표준 라이브러리만** 사용(`http.server`, `sqlite3`, `urllib`, `threading`, `argparse`, `logging`). 웹 프레임워크(Flask·FastAPI·Django) 없음. 외부 패키지는 `python-oracledb`(Thin 모드) 하나이며, `target_db.py` 안에서 필요할 때만 import하고 없으면 `python_oracledb_not_installed`로 거부함 |
| 의존성 파일 | `requirements.txt`·`pyproject.toml`·`setup.py` **없음**. `oracledb` 설치 요구는 코드·문서에만 적혀 있고 고정된 버전이 없음 |
| 서버 | ① **관리 웹 서버**(`food_trends/api.py`, 기본 포트 8000, 루프백 전용, POST마다 관리 토큰 필요) ② **모바일 앱용 읽기 전용 API**(`food_trends/app_api.py`, `/api/v1`, 기본 포트 8020) ③ **앱 API Mock 서버**(`food_trends/app_mock.py`, 포트 8030, 가상 데이터) |
| 엔드포인트 수 | 관리 API **GET 23 + POST 35 = 58개 경로**(정적 파일 제외, 3.1절) / 앱 API **7개**(전부 GET) |
| 프론트엔드 | `static/`의 **바닐라 JS + 단일 HTML 페이지** 관리 화면. 프레임워크·번들러·차트 라이브러리·CDN 없음. 차트는 DOM 막대(Instagram)와 손으로 그린 SVG(NAVER) |
| 모바일 앱 | **이 저장소에는 모바일 클라이언트 코드가 없음**(`.dart/.kt/.swift/.tsx/.jsx/package.json/pubspec` 0건). 앱은 다른 개발자가 만들며, 이 저장소는 API 계약(OpenAPI)과 서버까지만 제공함 |
| 운영 상태 | 운영 기본 실행(`scripts/ops/start-backend.ps1`)은 `--backend oracle-production --no-schedulers`로, **수집기가 없는 관리·관측 모드**임. 지속 자동 수집은 **CONFIGURED_NOT_ENABLED**(코드는 있지만 켜지 않음) |

---

## 2. 백엔드

### 2.1 실행 진입점

| 진입점 | 역할 | 근거 |
| --- | --- | --- |
| `run.ps1` | `py -3 -m food_trends @args`를 감싼 스크립트(py → anaconda → python 순서로 찾음) | `run.ps1` |
| `python -m food_trends` (`food_trends/__main__.py`, 571줄) | CLI 겸 관리 서버. 플래그 약 60개. 서버 기동 외에 일회성 명령도 함께 처리함(`--collect-once`, `--migrate`, `--maintenance`, `--naver-*`, `--youtube-*`, `--instagram-*`, `--target-*`, `--shadow`, `--recovery`, `--writer-*`, `--execution-*`) | `__main__.py:237-330` |
| `--backend {sqlite, oracle-test, oracle-production}` | 기본값은 `sqlite`(레거시 SQLite + RSS·NAVER·Instagram·YouTube 직접 수집기). `oracle-*`는 `oracle_command()`가 받아 SQLite를 열지 않고 Oracle 서비스를 `make_server`에 넣음. 허용 플래그 밖의 옵션을 섞으면 거부함 | `__main__.py:121-196, 295` |
| `--no-schedulers` | 백그라운드 수집 스레드를 시작하지 않음 | `__main__.py:254, 553-557` |
| `--execution-mode live --execution-registry R [--execution-automatic]` | 승인된 registry로 Live 실행. 자동 실행에는 `B3_AUTOMATIC_ENABLED=true`도 있어야 함 | `__main__.py:142-176, 214-235` |
| `python -m food_trends.app_api --store {sqlite,oracle-test,oracle-production} --port 8020` | 앱 API 서버. DB를 읽기 전용으로 열고 외부 호출을 하지 않음 | `app_api.py:217-250` |
| `python -m food_trends.app_mock [--port 8030] [--host 0.0.0.0]` | 가상 데이터셋(`[MOCK] ...`)을 임시 DB에 만들어 같은 `AppApi`로 응답. LAN 바인딩과 CORS `*`를 허용함(가상 데이터만 다루므로) | `app_mock.py:1-12, 281-292` |
| `scripts/ops/start-backend.ps1` | 운영 환경변수를 불러와 운영 스키마가 준비됐는지 확인하고, 관리 서버(8000, `--backend oracle-production --no-schedulers`)와 앱 API(8020, `--store oracle-production`)를 백그라운드로 띄움. `/api/status`와 `/api/v1/health`가 200을 줄 때까지 최대 90초 기다림. 로그는 `data/ops-prod/logs/{admin,app}{,.err}.log` | `scripts/ops/start-backend.ps1` |
| 기타 ops | `start-oracle.ps1`/`stop-oracle.ps1`(Podman Oracle 컨테이너), `stop-backend.ps1`, `status.ps1`, `verify.ps1`, `verify_production.py`, `live_pilot.py`(상한을 둔 Live 시범) | `scripts/ops/` |

### 2.2 계층 구조 (라우터 → 서비스 → 저장소)

```
HTTP (http.server.ThreadingHTTPServer)
 ├─ api.py  make_server.Handler  ── 경로 접두사로 분기
 │    ├─ food_http.py      → FoodService        | OracleFoodService      (oracle_foods.py)
 │    ├─ tracking_http.py  → TrackingService    | OracleTrackingService  (oracle_tracking.py)
 │    ├─ planner_http.py   → QueryPlanner       | OracleQueryPlanner     (oracle_planner.py)
 │    ├─ execution_http.py → ExecutionController| OracleExecutionController (oracle_rss.py / oracle_execution.py)
 │    ├─ (api.py 안에서 직접) RSS: CollectionService(service.py)+Storage | OracleRssService(oracle_rss.py)
 │    └─ (api.py 안에서 직접) YouTube/NAVER/Instagram 레거시 서비스 (SQLite에서만 연결)
 └─ app_api.py AppApi.handle ── FoodAnalytics(analytics_service.py)
                                   └─ SQLiteAnalyticsStore | TargetAnalyticsStore (analytics_reader.py)
저장소: storage.py(sqlite3, 레거시 앱 DB) / target_db.py(OracleTarget = python-oracledb 풀, SQLiteTarget = 대역) + target_repository.py
모드 선택: runtime_storage.select(mode) — oracle-production은 production_access.verify(스키마·승인 기록)를 통과해야 열림
```

- "라우터"는 별도 프레임워크가 아니라 `if path == ...` 분기임. 관리 서버는 `api.py`의 `do_GET`/`do_POST`가 접두사로 `*_http.py`에 넘김. 앱 API는 `ROUTES` 정규식 표 7행으로 분기함(`app_api.py:29-36`).
- 같은 `make_server`에 SQLite 서비스와 Oracle 서비스를 **의존성 주입**으로 바꿔 끼우는 구조임. `oracle_admin_server()`(`__main__.py:107-118`)가 Oracle 서비스를 넣음.
- Oracle 모드에서는 YouTube·NAVER·Instagram **레거시 직접 수집기를 연결하지 않음**. 그래서 해당 관리 경로는 503을 돌려줌(`__main__.py:110` 독스트링, 스크린샷으로도 확인).

### 2.3 DB 접근

| 모듈 | 대상 | 비고 |
| --- | --- | --- |
| `storage.py`, `instagram_storage.py`, `youtube_storage.py`, `migrations.py` | 레거시 SQLite 앱 DB | `--backend sqlite` 기본 경로 |
| `target_db.py` | `OracleTarget`(python-oracledb Thin, 커넥션 풀), `SQLiteTarget`(대역) | 오류는 코드로만 노출하고 DSN·자격증명은 출력하지 않음 |
| `target_repository.py`, `target_schema.py`, `target_migration.py` | B6 대상 스키마(테이블·마이그레이션 버전 `target_schema_migrations`) | |
| `oracle_*.py`(foods, tracking, planner, execution, rss, ingestion, observations, live) | Oracle 런타임 서비스 | `oracle_execution.py` 1,219줄로 가장 큼 |
| `analytics_reader.py` | 앱 API 읽기 전용 질의. 하나의 SQL로 SQLite와 Oracle을 모두 처리(LIMIT·SQLite 함수 미사용). 읽는 테이블: `food_entities`, `food_aliases`, `food_relations`, `food_mentions`, `source_items`, `run_items`, `ingest_runs`, `metric_observations`, `quality_results`, `food_events`, `entity_change_effects` | `analytics_reader.py:63-122` |

### 2.4 수집 작업과 API 요청의 분리

- 앱 API(`app_api.py`)는 **GET/HEAD/OPTIONS만** 받고 쓰기·외부 호출이 없음(독스트링, `ALLOW`). 관리 토큰 헤더가 붙은 요청은 거부함(`ADMIN_TOKEN_NOT_ACCEPTED`).
- 관리 서버에서 수집을 일으키는 경로는 명시적 POST뿐임: `/api/collect`, `/api/{youtube,naver,instagram}/collect` 등, `/api/execution/{run,tick,schedule,enqueue}`.
- 자동 수집은 별도 **데몬 스레드**에서 돔:

| 스케줄러 | 위치 | 시작 조건 | 운영 상태 |
| --- | --- | --- | --- |
| `rss-scheduler` | `service.py:58-64` | sqlite 모드에서 `--no-schedulers`가 없을 때 | 운영 미사용(운영은 Oracle 모드) |
| `naver-scheduler` | `naver_service.py:198-208` | 위와 같음 | 운영 미사용 |
| `instagram-scheduler` | `instagram_service.py:336-354` | `start(automatic=not --no-schedulers)` | 운영 미사용 |
| `youtube-scheduler` | `youtube_service.py:255-268` | 위와 같음 | 운영 미사용 |
| `oracle-rss-scheduler` | `oracle_rss.py:434-446` | `--execution-mode live`이고 `--no-schedulers`가 없으며 RSS origin이 `live_api`일 때 | **CONFIGURED_NOT_ENABLED** |
| `b3-scheduler-worker` | `execution_runtime.py:78-87` | `--execution-automatic` + `B3_AUTOMATIC_ENABLED=true` | **CONFIGURED_NOT_ENABLED** |

- 근거: `reports/development-oracle-production-cutover-2026-11-07/LIVE_VERIFICATION.md:52`, `PRODUCTION_RUNBOOK.md:26,56`, `FINAL_ACCEPTANCE.md:8`. 실제 외부 수집은 승인된 소량 실행 7회(RSS 1, Instagram 3, YouTube 2, NAVER 1, 전부 HTTP 200)만 기록돼 있음.

### 2.5 오류 처리·로깅·보안

- 관리 서버: GET 전체를 `try/except Exception`으로 감싸 500 `'저장 데이터 조회 실패. 서버 로그를 확인하세요.'`를 돌려줌. 각 처리기는 `FoodError(status)`를 HTTP 코드로 바꾸고, 크기 제한(4 KB~32 KB → 413)과 Content-Type 검사(415)를 함. 수집기 오류는 종류에 따라 409/429/502/503으로 바뀜(`api.py:177-260`).
- 보안: 루프백 바인딩만 허용, Host 검사(DNS rebinding 방어), Origin 동일성 검사, POST마다 `X-MZ-Admin-Token`을 `hmac.compare_digest`로 비교함. 토큰은 서버가 `index.html`의 `<meta>`에 주입하고 `static/admin.js`가 같은 출처의 비-GET 요청에 붙임. Windows에서 거절 응답이 RST로 끊기지 않도록 요청 본문을 먼저 읽음(`api.py:_read_body`, 커밋 `875997e`).
- 앱 API: 오류 형식은 `{"error":{code,message,details},"meta"}`이고 코드는 `app_contract.ERRORS`에 정의됨. 예기치 않은 예외는 `logging.exception`으로 서버 로그에만 남기고 응답에는 세부 정보를 넣지 않음. 알 수 없거나 반복된 파라미터는 400 `INVALID_PARAMETER`.
- 로깅: `logging.basicConfig(level=INFO)`로 stderr에 출력함(`__main__.py:329`). 파일 핸들러는 없고, 실행 스크립트가 표준 출력을 리다이렉트함. 루트의 `server.log`(30 B)와 `server.err.log`(약 100 KB)는 과거 로컬 실행에서 남은 것으로 보임. 운영 로그는 `data/ops-prod/logs/`(admin, app, oracle 각 `.log`/`.err.log`). 로그 내용은 비밀값이 있을 수 있어 열어 보지 않았음.

---

## 3. API 목록

### 3.1 관리 API (`food_trends/api.py` + `*_http.py`, 포트 8000)

정적 파일 GET: `/`(토큰 주입) 외 `app.js, style.css, admin.js, instagram.js, naver.js, youtube.js, foods.js, tracking.js, planner.js, execution.js`. 모든 POST에는 관리 토큰·Host·Origin 검사가 적용됨.

| # | 메서드·경로 | 주요 파라미터 | 처리 함수 → 데이터 | 저장소(SQLite / Oracle) | 프론트 호출 | 테스트 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | GET `/api/status` | – | `service.status()` 수집 요약 | `Storage` / `OracleRssService` | app.js | test_mvp, test_admin_oracle |
| 2 | GET `/api/trends` | `classification`(ALL/FOOD/REVIEW/NON_FOOD), `q` | `storage.trends()` RSS 항목 목록 | RSS 테이블 / `OracleRssStore` | app.js | test_mvp, test_admin_oracle |
| 3 | GET `/api/trends/{id}` | – | 항목 상세 | 같음 | app.js | 같음 |
| 4 | GET `/api/export` | – | 감사용 JSON 다운로드 | 같음(Oracle은 원본 응답 미보관) | index.html 링크 | test_mvp |
| 5 | POST `/api/collect` | – | `service.collect()` RSS 수집 | 실제 HTTP(sqlite) / Oracle: Mock 응답만, 운영은 수집 거부(409) | app.js "지금 수집" | test_mvp, test_admin_oracle |
| 6 | GET `/api/foods` | – | 승인된 음식 목록 | food_entities 등 | foods.js | test_food_entities |
| 7 | GET `/api/foods/resolve` | `name`, `scope` | 별칭 정확 매칭 | food_aliases | foods.js | test_food_entities |
| 8 | GET `/api/foods/{id}` | – | 음식 상세(별칭, 병합 대상) | food_entities/aliases | foods.js | 같음 |
| 9 | GET `/api/food-candidates` | – | 검토 후보 목록 | candidates | foods.js | 같음 |
| 10 | GET `/api/food-candidates/{id}` | – | 후보 상세 | 같음 | foods.js | 같음 |
| 11 | GET `/api/food-events` | `subject_id` | 변경 이력 | food_events | foods.js | 같음 |
| 12-20 | POST `/api/foods/{submit, import, resolve, review, rename, alias, revert-alias, relate, merge}` | 작업별 허용 필드(`food_service.py:300-309`), `expected_revision` | `FoodService.command` | food_* 테이블 | submit, import, review, rename, alias, revert-alias, relate는 foods.js에서 호출. **`resolve`(POST), `merge`는 화면 호출 없음(API만)** | test_food_entities, test_admin_oracle |
| 21 | GET `/api/tracking/accounts` | – | 수집원 계정 공개정보, `execution_enabled:false` | 설정(env) | tracking.js | test_tracking |
| 22 | GET `/api/tracking/targets` | `food_id` | 추적 대상 | tracking_targets | tracking.js | 같음 |
| 23 | GET `/api/tracking/due` | – | 예정 슬롯 | 같음 | **화면 호출 없음** | 같음 |
| 24 | GET `/api/tracking/events` | `target_id` | 대상 이력 | 같음 | **화면 호출 없음**(미확인: 문자열 검색 기준) | 같음 |
| 25-32 | POST `/api/tracking/{register, activate, pause, resume, stop, interval, refresh, schedule}` | 작업별 필드(`tracking_service.py:174-179`) | `TrackingService.command` | tracking_* | `schedule` 외 7개는 tracking.js에서 호출. **`schedule`은 API만** | test_tracking |
| 33 | GET `/api/planner/preview` | `target_id`, 기간·지역 등 14개 옵션 화이트리스트 | `QueryPlanner.preview` 수집 계획 미리보기 | planner_* | planner.js | test_query_planner |
| 34 | GET `/api/planner/history` | `target_id` | 저장된 계획 | 같음 | planner.js | 같음 |
| 35 | GET `/api/planner/plans/{id}` | – | 계획 상세 | 같음 | 화면 호출 없음 | 같음 |
| 36-37 | POST `/api/planner/{preview, commit}` | `target_ids`, `options`, (`request_key`) | 미리보기 / 계획 저장 | 같음 | commit만 planner.js(POST preview는 API만) | 같음 |
| 38 | GET `/api/execution/status` | 없음(파라미터가 있으면 거부) | 실행 상태 + 수집원별 요약(`sources`) | 실행 작업 테이블 | execution.js | test_execution, test_execution_sources |
| 39 | GET `/api/execution/jobs/{id}` | – | 작업 상세 | 같음 | 화면 호출 없음 | 같음 |
| 40-44 | POST `/api/execution/{enqueue, schedule, run, tick, recover-rss}` | `plan_ids`/`mode`, `options`, `job_id`, `max_jobs`, `limit` | `ExecutionController` | 같음 | `schedule`(mode=mock 고정), `run`은 execution.js. **enqueue, tick, recover-rss는 API만** | test_execution, test_execution_handoff |
| 45 | GET `/api/youtube` | – | 상태·쿼터 | youtube_storage | youtube.js | test_youtube |
| 46-48 | POST `/api/youtube/{collect, refresh, delete}` | 빈 JSON `{}`만 허용 | YouTube Data API 호출 / 삭제 | 같음 | youtube.js | test_youtube |
| 49 | GET `/api/naver` | – | 상태 | naver 테이블 | naver.js | test_naver |
| 50-51 | POST `/api/naver/{query, collect}` | `days`(7/30/90), `candidate_ids` 또는 `keywords` | DataLab 호출(결과는 저장하지 않는 transient) | – | query만 naver.js(collect는 API만) | test_naver |
| 52 | GET `/api/instagram` | `days`(7/14/30), `candidate_id`, `hashtag_id` | 해시태그 일별·시간별 집계 | instagram_storage | instagram.js | test_instagram |
| 53 | GET `/api/instagram/media` | `hashtag_id` | 디버그 미디어 목록(캡션 제외) | 같음 | instagram.js | test_instagram |
| 54-58 | POST `/api/instagram/{start, collect, pause, resume, check}` | `hashtag_id`, `candidate_id`, `approved:true` | Graph API 호출 / 상태 변경 | 같음 | instagram.js | test_instagram |

- 집계: GET 23개 + POST 35개 = **58개 경로**(정적 파일 제외). 경로별 대조표 `reports/development-oracle-backend-completion-2026-11-05/ADMIN_API_INVENTORY.md`와 일치함.
- **Oracle·운영 모드에서의 제약**: 45~58번(YouTube, NAVER, Instagram)은 **503 "서비스 미연결"**. 이 수집원들은 운영에서 tracking → planner → execution 경로로만 다룸. 5번 `/api/collect`는 운영 모드에서 수집을 거부함(`production_backend`: "RSS refuses to fetch", `runtime_storage.py:87-108`).
- "테스트" 열은 테스트 파일에 경로 문자열이나 서비스 호출이 들어 있는지를 grep으로 확인한 것임. 개별 assert의 범위는 미확인. 관리 경로 문자열이 나오는 테스트 파일: test_admin_oracle, test_audit_improvements, test_execution_handoff, test_execution_sources, test_food_entities, test_foundations, test_instagram, test_mvp, test_naver, test_oracle_e2e, test_oracle_live_e2e, test_query_planner, test_tracking, test_youtube.

### 3.2 모바일 앱 API v1 (`food_trends/app_api.py`, 포트 8020 / Mock 8030)

| # | GET 경로 | 허용 파라미터 | 처리 → 반환 | 데이터 출처 |
| --- | --- | --- | --- | --- |
| 1 | `/api/v1/health` | – | 서비스·데이터 모드 상태 | `FoodAnalytics.health` |
| 2 | `/api/v1/foods` | `q, kind, sort, limit, cursor` | 음식 목록(커서 페이지네이션) | food_entities, food_aliases |
| 3 | `/api/v1/foods/{food_id}` | – | 음식 상세(병합된 ID면 대상 정보 제공) | 같음 + food_relations |
| 4 | `/api/v1/foods/{food_id}/timeline` | `period, end_date, granularity(day/week), source` | KST 일·주 단위 시계열, `value_state`(OBSERVED / NOT_OBSERVED / EVIDENCE_EXPIRED) | food_mentions, source_items, run_items, ingest_runs, metric_observations |
| 5 | `/api/v1/foods/{food_id}/metrics` | `period, end_date, source` | 기간 값, 직전 기간 대비 증감, coverage | 같음 |
| 6 | `/api/v1/trends/rankings` | `source, metric, period, end_date, kind, include_zero, limit, cursor` | **수집원별** 순위(종합 점수 없음) | 같음 |
| 7 | `/api/v1/sources/status` | `period` | 수집원별 관측 상태. YouTube=POLICY_BLOCKED, NAVER=NOT_STORED | ingest_runs, quality_results |

- 집계 대상 수집원은 `google_trends_rss`와 `instagram_graph` **두 개뿐**(`analytics_service.py: B8_SOURCES`). 순위·증감·시계열은 모두 **서버에서 읽을 때 계산**함(집계 테이블을 미리 만들지 않음). 별도 집계 테이블 제안(`MATERIALIZED_AGGREGATE.md`)과 종합 점수 제안(`COMPOSITE_SCORE_PROPOSAL.md`)은 **제안 문서로만 있음**(`reports/development-b8-app-api-2026-11-04/`).
- 실데이터/Mock 구분은 응답의 `meta.data_mode`(`database`/`mock`)와 `meta.observation_origin`(`live_api`/`mock`)으로 표시함. Mock 서버는 `X-Mock-Error: 500|503` 헤더로 장애 응답을 흉내 낼 수 있음.
- 계약 문서: `docs/app-api/openapi.yaml`(OpenAPI 3.0.3, 3,729줄, 경로 7개로 코드와 일치), `openapi.json`, `README.md`(연동 가이드), `ACCESS.md`, `ERRORS.md`, `DATA_DICTIONARY.md`, `examples/` 32개 JSON(`app_mock --write-examples`로 생성).
- 테스트: `test_app_api.py`(9), `test_app_contract.py`(12, OpenAPI 계약 대조), `test_app_oracle.py`(17), `test_food_analytics.py`(17). `/api/v1`은 `test_oracle_backend`, `test_oracle_e2e`, `test_oracle_live*`, `test_reverse_recovery`에도 나옴.
- 운영 검증 증거: `FINAL_ACCEPTANCE.md`의 "B8 API PASS". 운영 스키마에서 두쫀쿠 2026-11-08 값 28 `OBSERVED/LIMITED`, OpenAPI 위반 0건.
- **소비자**: 이 저장소 안에는 `/api/v1`을 호출하는 클라이언트가 **없음**(관리 화면 `static/*.js`도 호출하지 않음). 앱 개발자 인계 문서는 `reports/development-oracle-production-cutover-2026-11-07/APP_HANDOFF.md`(Base URL `127.0.0.1:8020`, 에뮬레이터 `10.0.2.2`, `adb reverse`). 외부 앱이 실제로 붙었는지는 **미확인**.
- 제약: 루프백 전용(외부 공개·TLS·인증·속도 제한은 별도 과제로 남음, `ACCESS.md`, `APP_HANDOFF.md:19,66`). CORS는 `--cors-origin`으로 명시한 출처만 허용함(Mock 서버는 `*`).

---

## 4. 프론트엔드 (관리 화면)

### 4.1 기술 스택

- `static/index.html`(40줄) 한 페이지, 스크립트 10개(`defer`), `style.css`. **바닐라 JS**만 쓰고 빌드 도구·npm·CDN·차트 라이브러리가 없음.
- 공통: `admin.js`가 `window.fetch`를 감싸 같은 출처 비-GET 요청에 `X-MZ-Admin-Token`을 붙임.
- 차트: `instagram.js igChart`는 div 막대 그래프, `naver.js naverChart`는 `createElementNS`로 직접 그린 SVG 선 그래프. 둘 다 **값은 서버가 집계하고 브라우저는 그리기만 함**. 미관측 구간은 0이 아니라 "미관측"으로 표시하며, 접근성을 위해 수치 표를 함께 제공함.
- 반응형: 모바일 스크린샷이 있음(`*-mobile.png`). 탭 UI는 없고 섹션을 세로로 나열함.

### 4.2 화면(섹션)별 상태

| 섹션 (index.html) | 스크립트 | 호출 API | 데이터 | SQLite 모드 | Oracle·운영 모드 |
| --- | --- | --- | --- | --- | --- |
| 헤더 "지금 수집", 수집 요약 카드 2줄 | app.js | `/api/status`, POST `/api/collect` | 실데이터(RSS 저장값) | 동작(실제 RSS) | 요약은 동작. 수집은 oracle-test에서 Mock, **운영에서는 거부** |
| 음식 사전(등록, 후보 검토, 승인, 별칭, 관계, RSS 가져오기, 이력 다이얼로그) | foods.js | `/api/foods*`, `/api/food-candidates*`, `/api/food-events`, POST foods/* | 실데이터 | 동작 | 동작(운영 PASS 증거 있음) |
| 추적 관리(계정, 대상 등록·활성·일시정지·간격 변경) | tracking.js | `/api/tracking/{accounts,targets}`, POST tracking/* | 실데이터. oracle-test는 Mock 계정 | 동작 | 동작 |
| 수집 계획(미리보기, 저장, 이력 JSON) | planner.js | `/api/planner/{preview,commit,history}` | 실데이터(계획만 만들고 외부 호출 없음) | 동작 | 동작 |
| Google Trends RSS 목록(분류 필터, 검색, 상세, JSON 내보내기) | app.js | `/api/trends`, `/api/trends/{id}`, `/api/export` | 실데이터 | 동작 | 동작 |
| Instagram 해시태그 분석(차트, 공동 출현 순위, 수집, 일시정지) | instagram.js | `/api/instagram*` | 실데이터(Graph API 결과 저장값) | 동작(설정 시) | **503, "서비스가 연결되지 않았습니다"** |
| NAVER 검색 트렌드(키워드 조회, SVG 차트) | naver.js | `/api/naver`, POST `/api/naver/query` | 조회 시점의 실제 API 결과(저장 안 함) | 동작(설정 시) | **503** |
| YouTube 공개 영상(상태, 수집, 갱신, 삭제) | youtube.js | `/api/youtube`, POST youtube/* | 실데이터, 출처 라벨(live_api/mock/legacy_unknown) | 동작(설정 시) | **503** |
| 추적 실행 관리(수집원별 요약, Mock 예정 작업 등록, 한 번 실행) | execution.js | `/api/execution/{status,schedule,run}` | 상태는 실데이터. "Mock 예정 작업 등록" 버튼은 `mode:'mock'` 고정 | 동작 | 상태 표시 동작. 운영에서는 Mock 실행이 거부되므로 **이 화면에서 운영 Live 실행은 불가**(Live는 CLI의 `--execution-mode live`로만) |

- **관리 화면에 없는 것**: 앱 API의 음식별 시계열·지표·순위·수집원 상태(B8 분석)를 보여 주는 화면이 없음. 분석 결과는 `/api/v1` JSON이나 외부 앱에서만 볼 수 있음.
- 화면에서 부르지 않는 관리 API(API·CLI 전용): POST foods `merge`·`resolve`, tracking `schedule`·GET `due`·`events`, planner GET `plans/{id}`·POST `preview`, execution `enqueue`·`tick`·`recover-rss`·GET `jobs/{id}`, naver `collect`.
- Mock 표시: 화면 문구에 "Mock 예정 작업 등록", "Mock 한 번 실행", 출처 라벨 `Mock 테스트`가 있음. 가상 데이터를 화면에 하드코딩하지는 않음. 모든 값은 서버 응답에서 옴.

### 4.3 브라우저 QA 증거

| 증거 | 내용 | 날짜/대상 |
| --- | --- | --- |
| `tests/*browser_check.py` 14개(browser_check, food, tracking, execution, naver, naver_live, youtube, instagram 4종, audit, oracle_admin) | `websocket` 패키지로 헤드리스 Chrome을 CDP로 직접 조종(Playwright·Selenium 아님). 기본 테스트 스위트와 별도로 수동 실행함 | – |
| `reports/development-oracle-backend-completion-2026-11-05/oracle-admin-browser-check-oracle.json` | `successful:true`, `storage:oracle_test`, 화면에서 RSS 수집과 음식 승인 성공, 토큰 없는 요청 401, JS 오류 0, 외부 요청 0, SQLite 미사용. 레거시 3개 패널은 "미연결" | 2026-11-05, **MZ_TEST(테스트 스키마) + Mock RSS** |
| `oracle-admin-ui-oracle.png`, `-standin.png` | 전체 화면 캡처. 이번 감사에서 확인함: 요약 카드, 음식 사전(Mock 후보 3개, 승인 1개), RSS 목록 2건, Instagram/NAVER/YouTube "미연결", 실행 관리의 수집원별 요약 | 같음 |
| `reports/development-b1-b2-2026-10-27/*.png`, `development-b3-2026-10-27/execution-mobile.png`, `development-a4-a5.../manual-registration-mobile.png`, `development-b4-b5-2026-10-28/browser/*.json` | 추적, 계획, 실행, 음식 등록 화면의 모바일 캡처와 결과 JSON | 2026-10-27~28, SQLite |
| `data/instagram-*.png`(live, hashtags, token 경고, unconfigured 등), `data/audit-20261026/*.png` | Instagram 화면과 초기 감사 화면 캡처 | 2026-10 |
| `data/browser-qa/`, `data/chrome-qa/` | QA용 Chrome 프로필 디렉터리(캐시)로, 결과물이 아님 | – |
| **운영(oracle-production) 화면의 브라우저 QA** | 증거 파일을 찾지 못함. 운영 관리 API는 `FINAL_ACCEPTANCE.md`에서 API 수준으로만 PASS | **미확인** |

---

## 5. 완료 / 부분 / 미구현 판정

| 영역 | 판정 | 근거 |
| --- | --- | --- |
| 관리 서버(음식, 추적, 계획, 실행, RSS 조회) on Oracle 운영 | **완료** | FINAL_ACCEPTANCE "관리자 API PASS", 재기동 후에도 데이터 유지 |
| 앱 API v1(7개) on Oracle 운영 | **완료(로컬 루프백)** | FINAL_ACCEPTANCE "B8 API PASS", OpenAPI 위반 0 |
| 앱 API Mock 서버와 계약 문서 | **완료** | app_mock.py, openapi.yaml, examples 32개 |
| 모바일 앱 클라이언트 | **이 저장소 범위 밖 / 미확인** | 클라이언트 코드 0건, APP_HANDOFF 문서만 있음 |
| 관리 화면의 Instagram/NAVER/YouTube 패널 | **SQLite 레거시 모드에서만 동작**. 운영에서는 503 | ADMIN_API_INVENTORY, 스크린샷 |
| 화면에서의 실제 수집 실행 | **부분**. 운영에서는 화면 수집(`/api/collect`)과 Mock 실행이 거부되고, Live 실행은 CLI로만 가능 | runtime_storage.production_backend, execution.js |
| 지속 자동 수집(스케줄러) | **구현됨, CONFIGURED_NOT_ENABLED** | LIVE_VERIFICATION.md:52 |
| 관리 화면의 분석 시각화(B8 시계열·순위) | **미구현** | static/*.js에 `/api/v1` 호출 0건 |
| 종합 점수, 미리 계산한 집계 테이블 | **미구현(제안서만 있음)** | reports/development-b8-app-api-2026-11-04 |
| 외부 공개(인증·TLS·속도 제한), 무인 자동 기동 | **미구현** | FINAL_ACCEPTANCE.md:19, ACCESS.md |
| 의존성 고정(requirements) | **없음** | 파일 미존재 |
| 운영 화면 브라우저 QA | **미확인** | 증거 없음(마지막 QA는 2026-11-05 MZ_TEST) |

## 6. 교수님께 설명할 때 핵심 포인트

1. 프레임워크 없이 Python 표준 라이브러리 `http.server`로 서버 두 개(관리용 쓰기 서버, 앱용 읽기 전용 서버)를 직접 구현했고, Oracle 연결에만 `python-oracledb`를 썼다.
2. 같은 HTTP 계층에 SQLite 서비스와 Oracle 서비스를 **주입으로 바꿔 끼우는** 구조라서, 같은 화면이 레거시 SQLite·Oracle 테스트·Oracle 운영에서 그대로 동작한다. 단, 레거시 직접 수집기 3개 패널은 예외다.
3. 프론트엔드는 바닐라 JS 관리 화면 하나다. 모든 값은 서버 API에서 오고 하드코딩된 Mock 데이터는 없다. 차트는 라이브러리 없이 DOM과 SVG로 그렸다.
4. 앱용 분석(시계열·순위)은 서버가 읽을 때 계산하며 OpenAPI 계약으로 공개했다. 이 데이터를 쓰는 모바일 앱은 이 저장소에 없다.
5. 실제 외부 수집은 승인된 소량 실행만 검증했고, 지속 자동 수집은 일부러 꺼 두었다.
