# ORACLE-BACKEND-COMPLETION-1 Google RSS 발굴·관리 웹 서버의 Oracle 연결과 단일 백엔드 E2E 검증

2026-11-05. 기준 `main` `9f3bdb6`에서 작업 브랜치 `feat/oracle-backend-completion`으로 진행했다. 운영 SQLite·운영 서버는 열거나 바꾸지 않았고, 운영 Oracle은 없으며, 실제 외부 API 호출은 0건이다. G-OPS-1~6은 모두 승인 대기 그대로다.

## 결론 요약

| 질문 | 답 |
| --- | --- |
| 완료 기준(“Google RSS 발굴부터 관리자 승인, Oracle 추적·Mock 수집, B8 분석 조회까지 기존 SQLite 없이 하나의 백엔드로 동작”)을 충족했는가 | **개발·테스트 범위에서 충족.** 관리 서버와 B8 앱 API를 같은 개발용 Oracle(MZ_TEST)에 붙여, RSS Mock 수집 → 후보 → 관리 API 승인 → 별칭 확정 → 추적 활성화 → Oracle 계획 → Mock 수집 → B4·B5 → B8 조회를 실제 HTTP로 실행했고 그동안 sqlite3 연결은 0건이었다(§4). |
| RSS 발굴은 Oracle에 직접 기록되는가 | **그렇다.** 기존 수집기·분류기를 그대로 쓰고 결과를 대상 스키마 v8에 바로 쓴다. 같은 Mock 응답을 SQLite 경로로 처리한 결과와 항목 번호·분류·후보 키·B4 키·상태·목록·상세가 같다(§2). |
| 관리 웹 서버가 Oracle로 동작하는가 | **그렇다.** `python -m food_trends --backend oracle-test`가 기존 `make_server`에 Oracle 서비스를 주입한다. 화면이 부르는 모든 경로가 Oracle에서 응답하고 인증·상태 변경 보호는 그대로다. 레거시 직접 수집기(YouTube·NAVER·Instagram 서비스)는 SQLite 전용이라 연결하지 않으며 503으로 답한다(§3). |
| 운영 전환 또는 Live 수집을 했는가 | **아니다.** Oracle 경로는 Mock만 수집하고(RSS도 Mock 응답 없이 수집 요청 거부), 운영 Oracle은 코드에서 거부된다. Live 차단 요소는 §9. |

## 1. 변경 파일과 기능

| 파일 | 변경 |
| --- | --- |
| `food_trends/target_schema.py` | 대상 스키마 **v8**: `rss_runs`(SQLite `runs` 계약, 원본 응답 제외), `rss_observations`(회차×항목 관측 계약·분류). 둘 다 런타임 소유(B7은 SQLite 회차를 옮기지 않음) |
| `food_trends/oracle_rss.py` (신규) | `OracleRssStore`(Storage의 start·finish·status·trends·export 계약을 대상에), `OracleRssService`(CollectionService 계약, Mock fetcher만), `OracleExecutionController`(recover-rss를 대상 RSS 행에서) |
| `food_trends/ingestion.py` | `rss_run`을 SQLite 읽기와 공용 빌더 `rss_observed`로 분리(Oracle은 같은 빌더로 수집 트랜잭션 안에서 B4 적재) |
| `food_trends/food_service.py`, `oracle_foods.py` | RSS 후보 가져오기 판단은 그대로 두고 읽기·갱신만 저장 기본 연산(`_rss_item`·`_rss_classification`·`_rss_prior`·`_rss_candidate`·`_retype`·`_rss_run_items`)으로 분리, Oracle 구현 추가. 후보는 `source_item_key`로 항목을 가리킴(B7 적재와 같은 키) |
| `food_trends/execution_runtime.py` | `recover_rss`의 RSS 읽기 두 개를 분리(Oracle 컨트롤러가 대체) |
| `food_trends/oracle_execution.py` | `status()`에 수집원별 요약 `sources` 추가(추가 필드), `purge()`(SQLite 실행 유지보수와 같은 수신 기록 삭제·체크포인트 묘비), mock 컨트롤러를 `OracleExecutionController`로 |
| `food_trends/runtime_storage.py` | `oracle_backend(..., rss_fetcher)`가 RSS 서비스까지 조립, `oracle_maintenance()`(실행·B4·B5 보존 정리와 별칭·병합 재처리) |
| `food_trends/__main__.py` | `--backend sqlite\|oracle-test\|oracle-production`(관리 서버·`--maintenance` 전용, SQLite 명령과 혼용 거부, 운영 거부), `--rss-mock-file`, `oracle_admin_server()` |
| `food_trends/reverse_recovery.py` | 대상에서 수집한 RSS 행(`rss_runs`·`rss_observations`·분류, 후보의 RSS 항목 참조)을 “SQLite로 되돌리지 않음” 손실로 보고 |
| `food_trends/oracle_ingestion.py`, `oracle_observations.py` | 문서 문자열·거부 메시지 갱신 |
| `static/execution.js` | 실행 패널에 수집원별 상태 한 줄씩(데이터가 있을 때만) |
| `tests/test_oracle_rss.py`, `test_admin_oracle.py`, `test_oracle_e2e.py` (신규), `test_reverse_recovery.py` | Phase A·B·C 검증(대역 + 실제 Oracle 하위 클래스) |
| `tests/oracle_admin_browser_check.py` (신규) | Oracle 런타임이 제공하는 관리 화면의 헤드리스 Chrome 점검(루프백 전용) |
| `README.md`, 이 디렉터리 | 실행 방법·현황, [관리 API 점검](ADMIN_API_INVENTORY.md), 증거 |

커밋: `8d481a7`(Phase A) → `42c696d`(Phase B) → `3289206`(Phase C) → `4e2da90`(Oracle 유지보수 실행 정리·화면 점검) → 보고·README 커밋. 병합은 §8.

## 2. Phase A — RSS 발굴·후보의 Oracle 직접 적재

**구조.** 수집기(`collector.fetch/parse`)와 분류기(`classifier.classify`)는 그대로다. 저장만 대상으로 바꿨다.

| SQLite | 대상(v8) | 키·규칙 |
| --- | --- | --- |
| `items` | `source_contents` `items:N` | N은 B7이 적재한 SQLite 항목 번호 또는 다음 번호(같은 `source_id`면 같은 항목). 첫 수집 시각·origin 유지, payload·마지막 발견 갱신 |
| `candidates`·`classification_history` | `rss_classifications`·`rss_classification_history` | B7과 같은 키(`ITEM_…`, `CLSH_…`). 분류가 바뀌면 이전 결과를 이력으로 |
| `runs`·`observations` | `rss_runs`·`rss_observations` | 회차 번호는 대상의 모든 `runs:N`(B7이 적재한 SQLite 회차의 B4 기록 포함) 다음 → B4 키 충돌 없음. 원본 응답은 대상에 보관하지 않음 |

- RSS 쓰기는 `rss_runs` 배타 잠금(대역은 BEGIN IMMEDIATE)으로 직렬화되고, 런타임 소유권 임대 안에서만 쓴다(Shadow·B7이 소유하면 거부, 쓰기 0 검증).
- 끝난 회차는 같은 트랜잭션에서 B4에 적재된다(SQLite는 커밋 직후 적재). 그래서 적재되지 않은 회차가 남지 않는다. 실패 회차는 `FAILED`·`NONE`(관측 안 됨)으로 적재하며 0으로 기록하지 않는다.
- FOOD/REVIEW 항목은 기존 `FoodService` 가져오기 판단으로 검토 후보가 된다(요청 키 `rss-resolution:{회차}:{항목}`, 후보 키 `[출처, 항목 번호]` — SQLite와 같음). 음식·별칭·추적 대상은 만들지 않는다. 승인된 정확 별칭과 같은 이름만 기존 규칙대로 그 음식에 연결된다. 그다음 B5가 회차를 연결한다. 가져오기 실패는 수집을 되돌리지 않고 `pending_retry`로 남으며 `POST /api/execution/recover-rss`가 대상 행에서 재시도한다.
- Oracle 경로는 실제 피드를 가져오지 않는다. Mock fetcher가 없으면 `accepted:false`·`oracle_live_collection_disabled`(HTTP 409)이고 회차 행도 만들지 않는다.

**검증** (`tests/test_oracle_rss.py`, 대역 7 + 실제 Oracle 7, 모두 통과):

| 항목 | 결과 |
| --- | --- |
| SQLite 의존 없음 | Oracle 흐름 동안 sqlite3 연결 0건(대역은 대상 파일만 허용) |
| SQLite 경로와 동등 | 같은 두 번의 Mock 응답에 대해 수집 요약·`/api/status`·목록·상세·내보내기 항목 수·후보(키·유형·게이트·상태·origin)·B4 `(회차, 항목, Snapshot)` 키·B5 지표 수가 SQLite `CollectionService`와 같음(시각·저장소 표시 제외) |
| 중복·재실행 | 한 응답 안의 중복 항목은 1개, 같은 응답 재수집은 신규 0·후보 증가 0·Snapshot 증가 0, 회차별 관측만 추가. 후보 가져오기 재실행은 기록된 결과 재사용 |
| 브랜드·매장 | `노티드`(FOOD_BRAND)는 BRAND 후보·`BLOCKED_NON_FOOD`로 남고 음식 승인 요청은 409. 거절 후 재가져오기가 되살리지 않음. 수집이 만든 음식·별칭·추적 대상·언급 0 |
| FOOD 0건 | 음식 항목이 없는 회차는 성공·후보 0·`NO_NEW_ITEMS`/관측 `COMPLETE`(실패 아님) |
| 실패 | 피드 장애·형식 오류는 실패 회차(`success=0`), B4 `FAILED`/`NONE`, 지표 0행 |
| 재분류 | 같은 항목의 분류가 바뀌면 `CLSH_` 이력 + 후보 유형 변경 이벤트(`SOURCE_TYPE_CHANGED`) |
| B7 이후 연속성 | SQLite 기간(회차 1·2, 항목 1~3)을 B7로 적재한 뒤 Oracle 수집은 회차 3·항목 4부터, 기존 항목은 번호 유지, 적재된 후보를 다시 찾아 중복 생성 없음 |
| 운영량 성능(실제 Oracle) | 항목 400개 기준 `/api/status` 0.22초, 목록 0.50초, 상세 0.01초, 내보내기 0.50초(운영 SQLite의 RSS 항목은 370개) |

역방향 복구(`reverse_recovery`)는 대상에서 수집한 RSS 발굴 행을 SQLite 발굴 저장소로 되돌리지 않고 손실 목록에 행 수와 사유로 보고한다(`test_rss_discovered_on_the_target_is_listed_as_not_written_back`). Oracle RSS가 Mock 전용인 동안에는 운영 데이터 손실이 생기지 않으며, Live 전환 전 과제로 남긴다(§9).

## 3. Phase B — 관리 웹 서버의 Oracle 연결

**전수 점검.** `api.py`와 처리기 4개, 화면 스크립트의 호출 경로를 모두 대조했다. 경로별 결과와 누락 Endpoint 목록은 [ADMIN_API_INVENTORY.md](ADMIN_API_INVENTORY.md)에 있다. 요약하면, 누락은 (1) Oracle 서비스를 주입해 서버를 띄우는 실행 경로, (2) RSS 경로 5개(`/api/status`·`/api/trends`·`/api/trends/{id}`·`/api/export`·`POST /api/collect`), (3) `POST /api/foods/import`(Oracle이 409로 거부), (4) `POST /api/execution/recover-rss`(Oracle에서 500), (5) 수집원별 상태 요약, (6) Oracle 유지보수였다. 모두 최소 구현으로 채웠고 처리기 모듈·응답 코드 규칙·B8은 바꾸지 않았다.

**실행.** `python -m food_trends --backend oracle-test [--rss-mock-file 로컬.xml]`. 같은 `make_server`에 `OracleRssService`·`OracleFoodService`·`OracleTrackingService`·`OracleQueryPlanner`·`OracleExecutionController`를 넣는다. SQLite DB를 열지 않고 DDL·스케줄러를 시작하지 않는다. `oracle-production`은 연결 전에 거부하고, SQLite 명령과 섞으면 인자 오류로 거부한다. `--backend oracle-test --maintenance`는 Oracle 보존 정리(실행 수신 기록·체크포인트, B4 원문·스테이징, B5 링크)와 별칭·병합 변경 재처리를 한다.

**검증** (`tests/test_admin_oracle.py`, 대역 4 + 실제 Oracle 4, 모두 통과; 실제 루프백 HTTP):

| 항목 | 결과 |
| --- | --- |
| 화면이 부르는 모든 경로 | 첫 화면(토큰 meta 포함)·정적 파일 10개, RSS 5개, 음식 조회 6개와 명령 9개(submit·review·alias 승인/취소·revert-alias·rename·relate·merge·import·resolve), 추적 조회 4개와 명령 6개(register·activate·interval·pause·resume·stop; refresh는 E2E), 계획 조회 3개·명령 2개, 실행 조회 2개·명령 5개 모두 200, 그동안 sqlite3 연결 0건 |
| 인증·보호 | 토큰 없음·틀린 토큰 401, 다른 Origin 403, 루프백 아닌 Host 403(GET·POST), 거부된 요청은 쓰기 0 |
| 오류 응답 | 415(Content-Type), 413(크기), 400(JSON·알 수 없는 필드·지원하지 않는 작업·잘못된 실행 요청·recover-rss 한도), 404(없는 후보·음식·계획·작업·경로), 409(브랜드 승인·변경 버전 충돌, Mock 없는 RSS 수집), 503(레거시 수집기 7개 경로) |
| 수집원별 상태 | `/api/execution/status`의 `sources`: YouTube 성공·예산 `READY_LOCAL`, Instagram 장애 시 최근 실패(`RETRY_WAIT`·`network`)와 예산, NAVER 작업 없음, RSS 성공 1·실패 1과 오류 내용 |
| CLI | `oracle-production` 거부(`oracle_production_runtime_disabled`), SQLite 명령 혼용 거부, 설정 없는 `oracle-test`·`--maintenance` 거부(`oracle_not_configured`), `--rss-mock-file` 단독 거부 |

수동 실행도 확인했다: 실제 MZ_TEST에 `--backend oracle-test --rss-mock-file`로 띄운 서버에서 토큰 없는 수집 401, 수집 200(신규 4·후보 3), 상태 `oracle_test`, 목록 4건, 후보 3건(BRAND는 `BLOCKED_NON_FOOD`), 실행 상태 `sources`, `/api/youtube` 503. 화면은 [헤드리스 Chrome 점검](oracle-admin-browser-check-standin.json)으로 “지금 수집” 버튼 수집, 후보·승인 음식 목록, 화면의 토큰 fetch로 승인, 토큰 없는 요청 401, 실행 패널의 수집원별 4줄, JavaScript 오류 0, 외부 요청 0을 확인했다([화면](oracle-admin-ui-standin.png)). 실제 MZ_TEST에서도 같은 점검이 통과했다([결과](oracle-admin-browser-check-oracle.json), [화면](oracle-admin-ui-oracle.png)).

## 4. Phase C — 단일 Oracle 백엔드 E2E

`tests/test_oracle_e2e.py`(대역 1 + 실제 Oracle 1, 통과, Oracle 약 16초). 관리 서버(`--backend oracle-test` 조립)와 B8 앱 API(`app_api --store oracle-test`와 같은 읽기 경로, **별도 연결 풀**)를 같은 대상에 띄우고 모든 요청을 실제 HTTP로 보냈다. 전 구간에서 sqlite3 연결 0건.

| 단계 | 확인 |
| --- | --- |
| RSS Mock 수집 → 후보 | 후보 3건(FOOD 상품, REVIEW, 브랜드). B8 `/api/v1/foods`는 빈 목록 |
| 관리 API 승인 → 별칭 확정 | 브랜드 승인 409, 없는 후보 404, 상품 후보 승인(음식 `두쫀쿠`), 정확 별칭 `두바이쫀득쿠키` 승인·해석 |
| 추적 활성화 → Oracle Query Planner | Instagram·YouTube 활성화, 미리보기는 계획·예약 쓰기 0, Commit 저장 |
| Mock 수집 → B4·B5 → Oracle 저장 | 작업 전부 `SUCCEEDED`, 적재·연결 대기 0, 유지보수로 승인 별칭이 RSS 항목에 연결 |
| B8 조회 | 승인 음식만 목록에 있음, 두 별칭·두 작업이 같은 게시물을 찾아도 오늘 값 1(`OBSERVED`·`COMPLETE`), RSS 언급 1개 항목, 순위는 기간 미관측일로 `insufficient_coverage` 1, 수집 상태에 Instagram 마지막 성공. B8 응답 어디에도 미승인 후보명 없음 |
| 중복 계산 없음 | 같은 RSS 재수집(신규 0, 회차만 추가), 끝난 작업 재실행(`result:null`), 할 일 없는 tick, 유지보수 재실행 후 원문·스테이징·Snapshot·언급·후보·작업 수 불변, 값 1 유지 |
| 별칭 변경 반영 | 두 별칭 중 하나 취소 → 값 1 유지, 둘 다 취소 → `OBSERVED` 0, 새 별칭 `디저트` 승인 → 1. 재처리 전에는 B8이 `reprocessing_pending`을 표시 |
| 추적 중지·재개 반영 | 별칭 변경으로 대상 `STALE_FOOD_REVISION`, 중지 중에는 일정에 없음, 갱신·재개 후 다음 세대 작업 실행, 이력 `REGISTERED→ACTIVATE→PAUSE→REFRESH→RESUME` |
| 실패·부분·만료 상태 유지 | 다음 날 제공자 장애 → 재시도 후 `FAILED`, 그날 `NOT_OBSERVED`(0 아님). 그다음 날 끝없는 페이지 → `LIMITED`, 그날 `OBSERVED`·`LIMITED`. 31일 뒤 유지보수 → 캡션 정리·언급 만료·실행 수신 기록 삭제·체크포인트 묘비, 첫날 `EVIDENCE_EXPIRED`, 재실행 정리 0 |
| 무결성 | FK 오류·미해결 payload_ref·정책 위반·살아 있는 만료 행 0, 자연키 중복 0 |
| B8 OpenAPI v1 | 흐름의 모든 B8 응답이 OpenAPI v1 응답 스키마 검사 통과. `docs/app-api/`·`app_api.py`·`app_contract.py`·`analytics_*.py` 변경 없음(`git diff 9f3bdb6` 비어 있음) |

## 5. 남은 SQLite 의존성

이번 범위(RSS 발굴 → 승인 → 추적 → Mock 수집 → B4·B5 → B8, 관리 서버)는 Oracle 단독이다. 시스템 전체로는 다음이 SQLite에만 쓴다.

| 파일·기능 | 목적 | Oracle 상태 |
| --- | --- | --- |
| `service.py`·`storage.py` `CollectionService`/`Storage` | SQLite 모드의 RSS 수집(운영 경로, Live) | Oracle은 `oracle_rss`(Mock 전용) |
| `instagram_service`·`youtube_service`·`naver_service`와 저장소 | 레거시 직접 수집기 | Oracle 모드 미연결(503). 추적·계획·B3 Mock 실행이 대체 |
| `execution.py`·`execution_runtime.live_controller` | SQLite B3 Live 실행 | Oracle 런타임은 Mock만 |
| `quota_ledger.import_legacy_usage` | 레거시 호출 사용량 원천 | 레거시 수집기가 있는 동안 SQLite에서 가져와야 함 |
| `__main__` SQLite 명령(`--collect-once`·`--ingest-pending`·`--ingest-legacy`·`--reanalyze`·`--export-json`·출처별 CLI) | SQLite 유지보수·수집 | Oracle은 관리 서버와 `--maintenance`만 |
| `reverse_recovery.py` | Oracle → SQLite 복구 | Oracle에서 수집한 RSS 발굴 행은 되돌리지 않음(손실 보고) |
| `shadow.py`·`target_migration.py`·`cutover.py` | 두 저장소 사이 이관·대조(설계상 SQLite를 읽음) | 정상 |
| `food_demo.py`, `audit.py` | SQLite 데모 서버·감사 | 해당 없음 |

## 6. 테스트

| 실행 | 결과 |
| --- | --- |
| 기본 환경(Oracle 컨테이너 정지, 외부 소켓·운영 DB 차단) | **503개 통과, 실패 0, Oracle 166개 skip**(374초) |
| 실제 Oracle 포함 전체(MZ_TEST) | **669개 통과, 실패 0, skip 0**(1,360초) |

기존 643개 테스트는 삭제·약화하지 않았다(기존 테스트 파일은 `test_reverse_recovery.py`에 사례 1개 추가만). 신규: `test_oracle_rss.py` 7×2, `test_admin_oracle.py` 4×2, `test_oracle_e2e.py` 1×2, 복구 손실 사례 1×2 = 26개(643 + 26 = 669, 기본 환경 490 + 13 = 503·skip 153 + 13 = 166). 단계별 실행(대역·실제 Oracle)도 각 단계 커밋 전에 통과했다. 결과 원본: [기본](default-tests/test-summary.json), [Oracle 포함](oracle-full-tests/test-summary.json).

## 7. 외부 API 호출과 운영 DB

- 외부 API 호출 0건. 테스트는 루프백 외 소켓을 막은 상태에서 실행했고 차단 시도 0건. RSS는 Mock 응답(테스트 픽스처, 수동 실행은 로컬 XML 파일)만 사용했다. 화면 점검 Chrome은 루프백 외 요청을 막았고 외부 요청 0건.
- 운영 DB(`data/trends.sqlite3`) 변경 없음: SHA-256 `38b880f5…48c0`가 이전 작업과 같고 v0·30개 표·7,701행·integrity ok·WAL 0바이트·본 파일 수정 시각 2026-10-28 14:20 그대로([production-final.json](production-final.json), 읽기 전용 확인). 이번 작업은 운영 파일을 읽기 전용 확인 외에는 열지 않았다(테스트 실행기는 운영 경로 연결을 거부).
- 운영 서버·운영 Oracle: 시작·중지·변경 없음(개발 포트 8000/8002/8010/8020/8030/8031/1521 점유 없음). 개발용 Oracle 컨테이너는 검증 동안만 켰고, 마지막 화면 점검 뒤 MZ_TEST를 비우고(표 0개) 컨테이너를 정지했다.

## 8. 최종 Git 커밋

작업 브랜치 `feat/oracle-backend-completion`(원격 push 완료): `8d481a7` Phase A → `42c696d` Phase B → `3289206` Phase C → `4e2da90` Oracle 유지보수 실행 정리·화면 점검 → 보고·README 커밋. `main`에는 fast-forward로 병합해 push한다(강제 push·reset hard 없음). 병합 후 `main` 커밋은 아래 통합 기록에 남긴다.

## 9. 남은 Oracle Live 수집 차단 요소

| # | 차단 요소 | 현재 코드 | 필요한 것 |
| --- | --- | --- | --- |
| 1 | Oracle 런타임의 Live 실행기 없음 | `OracleExecution`은 `live_enabled`를 거부(“mock transports only”), Oracle 어댑터는 Mock 클라이언트만 조립 | Oracle용 Live 컨트롤러(검토된 registry 승인, 출처별 단일 Writer 확인을 대상 소유권으로, 외부 사용량 검증 `require_verified_usage`), 승인 후 소량 검증 |
| 2 | Oracle RSS 실제 수집 비활성 | `OracleRssService`는 Mock fetcher 없이 수집 거부, 자동 주기 실행 없음 | 승인 후 실제 fetch·주기 스케줄러 연결, 원본 응답 보관 정책 결정(현재 대상에는 미보관) |
| 3 | 운영 Oracle 잠금 | `RuntimeOwnership`이 `RUNTIME_PRODUCTION`·운영 환경을 거부, `select('oracle-production')`·앱 API도 거부 | G-OPS-6 승인과 운영 잠금 해제 경로의 별도 구현·검증(직전 보고서 BLOCKED 1) |
| 4 | 레거시 수집기·사용량 원천이 SQLite | 레거시 수집기 스케줄러 플래그·Writer 소유권(`execution_source_ownership`)·`import_legacy_usage`가 SQLite 기준 | 레거시 수집기 정지와 Writer 이전(G-OPS-4/5), 사용량 원장의 마지막 가져오기 후 Oracle 단독 집계 |
| 5 | Oracle RSS의 역방향 복구 | 대상에서 수집한 RSS 발굴 행은 SQLite로 되돌리지 않음(손실 보고) | RSS Live 전 복구 경로에 RSS 행 쓰기 추가 또는 손실 수용 결정 |
| 6 | 운영 승인 게이트 | G-OPS-1~6 모두 대기 | 백업·Writer 정지·SQLite 마이그레이션·Live 승인·운영 Oracle 승인 |

직전 보고서의 BLOCKED 4개 중 3(RSS 발굴·후보 가져오기 SQLite 전용)과 4(관리 웹 서버 SQLite 전용)는 이번 작업으로 해소했다. 1(운영 Oracle 쓰기·실행 잠금)과 2(Oracle Live 수집)는 남아 있으며, 운영 전환과 Oracle 주 데이터베이스의 운영 충족은 완료로 보고하지 않는다.

## 통합 기록

`main`을 `9f3bdb6`에서 `98afd73`(보고 커밋, 위 작업 커밋 5개 포함)으로 fast-forward하고 `origin/main`에 push했다. 이 기록 커밋도 같은 방식으로 `main`에 fast-forward했다. 강제 push·reset hard·기존 작업 삭제는 없었다. 병합 전 `origin/main`은 기준 `9f3bdb6` 그대로였다.
