# PARALLEL-B8-API 통합 분석 API와 모바일 앱 연동 계약

2026-11-03~04. ORACLE-RUNTIME-2와 병렬로, 별도 Git 워크트리에서 모바일 앱용 읽기 전용 API 계약(OpenAPI), B5 관측 기반 B8 읽기 시 집계, 실제 API와 같은 구현을 쓰는 Mock 서버를 개발했다. **기존 파일 수정 0건**(모두 신규 모듈·문서), 외부 API 호출 0건, 운영 DB·공유 Oracle 테스트 스키마(MZ_TEST) 미사용, Oracle 테스트 미실행. main에는 병합하지 않았다.

| 단계 | 커밋 | 판정 |
| --- | --- | --- |
| P1 앱 API 계약 | `9b2156c docs: define mobile app OpenAPI contract` | 완료 |
| P2 B8 분석 | `6e9d274 feat: implement food analytics read models` | 완료 (읽기 시 집계, SQLite·대상 스키마 동등) |
| P3 API·Mock | `7d3da22 feat: add mobile analytics API and mock responses` | 완료 (로컬 실행, 외부 공개는 별도 과제) |
| P4 계약 테스트·인수 문서 | `test: validate mobile analytics API contracts` | 이 문서 |

## 1. 작업 공간 분리

| 항목 | 값 |
| --- | --- |
| 작업 경로 | `D:\MZFoodTraker-B8-API` (`git worktree add --no-track -b feat/b8-app-api`) |
| 기준 | 시작 시 검증한 `origin/main` 3ac56d9 (main 작업 트리 깨끗, 미커밋 변경·다른 워크트리·실행 중 서버 없음) |
| Push 대상 | `origin/feat/b8-app-api`만. main 직접 Push·`--force` 없음 |
| 격리 | 워크트리에 `.env`·`data/` 없음. DB는 모두 임시 폴더. 기본 DB 경로·`trends.sqlite3` 파일명은 기록기가 DDL 전에 거부 |
| 병렬 세션 | 작업 중 원격 main이 ORACLE-RUNTIME-2 커밋으로 3ac56d9 → f3a77d1 진행. 병합하지 않고 사전 점검만 수행(§6) |

## 2. P1 — 앱 API 계약

산출물: [openapi.yaml](../../docs/app-api/openapi.yaml)·[openapi.json](../../docs/app-api/openapi.json)(OpenAPI 3.0.3, 오퍼레이션 7·스키마 49), [연동 가이드](../../docs/app-api/README.md), [데이터 사전](../../docs/app-api/DATA_DICTIONARY.md), [오류 코드](../../docs/app-api/ERRORS.md), [인증·접근 범위](../../docs/app-api/ACCESS.md), [예시 JSON 32개](../../docs/app-api/examples/).

| 경로 | 용도 |
| --- | --- |
| `GET /api/v1/health` | 연결·데이터 모드 확인 |
| `GET /api/v1/foods` | 음식 목록·검색 (`q`, `kind`, `sort`, 커서) |
| `GET /api/v1/foods/{food_id}` | 상세 (별칭·관계·병합 리다이렉트·출처 제공 여부·재처리 대기) |
| `GET /api/v1/foods/{food_id}/timeline` | 출처별 일·주 단위 추이 |
| `GET /api/v1/foods/{food_id}/metrics` | 출처별 기간 통계·변화·제공자 표시값·품질·출처 불명 데이터 |
| `GET /api/v1/trends/rankings` | 출처별 순위 (출처 필수) |
| `GET /api/v1/sources/status` | 데이터 갱신·수집 범위 |

- 기존 관리 API(`/api/foods`, `/api/trends` 등)와 경로가 겹치지 않는 `/api/v1` 아래에 두고 **별도 서버 프로세스**로 분리했다. 기존 관리 서버는 Host를 루프백 이름으로만 받아 Android 에뮬레이터(`10.0.2.2`)도 막고 관리 토큰을 페이지에 넣으므로 앱용으로 재사용하지 않았다.
- 계약 규칙: 필드는 항상 존재(없으면 `null`), 시각은 UTC 초 단위 `Z`(iOS `ISO8601DateFormatter`의 소수 초 함정 회피), 일자는 KST 달력일, `value: null`(상태는 `value_state`)과 정상 0 구분, 출처 합산·종합 점수 없음, 모르는 쿼리 파라미터 거부, 기간 기본값은 어제까지의 완결 일자, 같은 조건에만 쓸 수 있는 불투명 커서.
- 명세는 `food_trends/app_contract.py` 한 곳에서 만들고 YAML·JSON은 생성물이다. YAML은 모든 문자열을 큰따옴표로 쓰는 결정적 형식이라 YAML 1.1(`on`, `n`, `yes` 등)·1.2 해석이 같다. 저장소 테스트는 표준 라이브러리만으로 구조·`$ref`·예시를 검사하고, 별도로 설치된 js-yaml 3.14.1(VS Code 확장 번들, 저장소 의존성 아님)로 `openapi.yaml`이 `openapi.json`과 같은 구조로 해석됨을 확인했다.

## 3. P2 — B8 읽기 시 집계

[analytics_service.py](../../food_trends/analytics_service.py), [analytics_reader.py](../../food_trends/analytics_reader.py).

| 규칙 | 구현 |
| --- | --- |
| 확인 콘텐츠 | `food_mentions`가 `ACTIVE`이고 근거 만료 전이며 출처 항목이 `AVAILABLE`인 고유 `item_key`. `item_query_matches`(SEARCH_ONLY)는 세지 않음 |
| 중복 제거 | 음식(병합된 ID 포함)·출처·구간마다 `item_key` 집합. 여러 별칭·여러 질의·하루 여러 회차 관측도 1 |
| 관측일 | `run_items.observed_at`(UTC) → KST 일자. 주(월~일)·기간은 집합을 다시 계산 |
| 관측 범위 | RSS: 그날 성공한 피드 회차(공유 피드). Instagram: 그 음식의 질의 회차 — B5가 B3 작업이 처리한 모든 음식에 기록하는 `instagram.observed_posts`(실패 회차는 `NOT_OBSERVED`)로 판별. 실행 엔진 테이블에 의존하지 않음 |
| coverage | 질의별 그날 최선 회차, 음식의 질의 중 최저값. 하루 종일 실패한 질의가 있으면 `PARTIAL` |
| 상태 | 범위 밖 `NOT_OBSERVED`(0 아님), 근거 만료·삭제 `EVIDENCE_EXPIRED`, 기간 일부 `INSUFFICIENT_COVERAGE`. 기간 값은 모든 날 관측·오늘 미포함일 때만 |
| 변화 | 두 기간 모두 `OBSERVED`·`COMPLETE`일 때만. 이전 0은 `PREVIOUS_ZERO`(비율 null) |
| 순위 | 한 출처·`confirmed_items`·한 기간, 기간 전체 관측 음식만, 경쟁 순위(1,2,2,4). 종합 점수 없음 ([제안서](COMPOSITE_SCORE_PROPOSAL.md), 비활성) |
| origin | 서버 설정의 한 origin만 집계(운영 `live_api`). `legacy_unknown`은 별도 값으로만 표시 |
| 무효화 | 읽기 시 집계라 `aggregate_invalidations`를 소비하지 않음. 미처리 별칭·병합 결정은 `reprocessing_pending`으로 표시. 저장 집계 인수 계약은 [MATERIALIZED_AGGREGATE.md](MATERIALIZED_AGGREGATE.md) |
| NAVER·YouTube | `NOT_STORED`·`POLICY_BLOCKED`로 표시만, 수치·순위 없음 |

- **DBMS 독립성**: 이식 SQL(이름 바인드, `LIMIT`·SQLite 함수·테이블 `AS` 미사용, 시각 비교는 Python)로 SQLite 앱 DB와 B6 대상 스키마에 같은 문장을 실행한다. 테스트는 픽스처를 B7 `migrate()`로 대상 스키마(SQLite 대역)에 옮긴 뒤 10가지 조회 결과가 SQLite와 같음을 확인한다(이관 제외 행 0). Oracle 실제 연결은 MZ_TEST 사용 금지로 미검증이며 확인 항목은 [통합 체크리스트](ORACLE_RUNTIME_2_INTEGRATION.md) §5-7.
- 읽기 연결: SQLite는 `Storage.reader()`(`mode=ro`, `query_only`) 한 트랜잭션, 대상 스키마는 Oracle에서 `SET TRANSACTION READ ONLY`.

## 4. P3 — API 서버와 Mock

| 구성 | 내용 |
| --- | --- |
| `food_trends.app_api` | `AppApi.handle()`이 요청 계약 전체(Host 검사, 관리 토큰 거부, 메서드, 경로, 엄격 파라미터, 봉투, 오류)를 맡고 HTTP 어댑터는 얇다. 실제 데이터 서버는 루프백 고정·읽기 전용·스키마 미준비 시 시작 거부. CORS는 지정 Origin만 |
| `food_trends.app_mock` | 같은 구현을 가상 데이터셋에 실행. `[MOCK]` 음식 9개, 고정 시계 2026-11-03 12:00 KST, 실제 B4 수집·B5 연결 서비스로 생성(회차 437, 콘텐츠 2,262, 회차별 관측 3,138, 확인 언급 2,028). 코드 버전별로 한 번 만들고 OS 임시 폴더에 캐시(첫 생성 약 15초, 이후 0.03초). 모든 Origin CORS, 선택적 LAN 바인딩, `X-Mock-Error` |
| 예시 | `--write-examples`가 화면별 32개 시나리오를 실제 Mock 응답으로 기록. 명세의 응답 예시도 이 파일에서 가져옴 |

Mock 데이터셋 기준 로컬 HTTP 응답 시간(단일 측정): health 5 ms, 목록·검색 32~33 ms, 상세 19 ms, 90일 추이·통계 20~22 ms, 90일 순위 14~30 ms, 90일 상태 18 ms.

## 5. 검증

### 요구 테스트 대응

| # | 요구 | 테스트 |
| --- | --- | --- |
| 1 | OpenAPI 문서 유효성 | `test_app_contract.OpenApiDocument` (구조·`$ref`·경로 파라미터·nullable enum·예시 검증, YAML=JSON, 생성물 최신) |
| 2 | 실제 응답과 명세 일치 | `DatabaseServerContract.test_real_responses_follow_the_document` (200·400·403·404·405·422·500·503 전부 엄격 검사) |
| 3 | 음식 검색·상세 | `test_app_api.FoodsEndpoint.test_list_search_and_detail`, `test_food_analytics...test_search_detail_and_merge_redirect` |
| 4 | FOOD_ID·별칭 | 같은 테스트 + `test_unapproved_names_and_variants_are_not_counted_for_the_food` |
| 5 | 일·주 경계 | `test_daily_values_repeat_and_week_and_period_recompute_the_set`, `test_kst_day_boundaries` |
| 6 | 중복 콘텐츠 제거 | 같은 테스트 + `test_partial_and_limited_coverage_are_labelled`(같은 날 재관측) |
| 7 | 다중 별칭 중복 방지 | `test_multi_alias_post_once_and_search_only_hits_excluded` |
| 8 | 미관측과 0 | `test_unobserved_is_null_and_observed_zero_is_zero` |
| 9 | PARTIAL·UNKNOWN | `test_partial_and_limited_coverage_are_labelled`, `test_legacy_unknown_is_reported_separately`, `test_expired_evidence_day_is_not_recounted` |
| 10 | 허용되지 않은 출처 차단 | `test_blocked_sources_have_no_values`, 순위 422 |
| 11 | 통합 점수 생성 방지 | `test_rankings_are_single_source_with_competition_ranks`, `OpenApiDocument.test_contract_has_no_composite_score` |
| 12 | Mock·실제 구분 | `MockServerContract.test_mock_data_is_always_marked_as_mock`, `DatabaseServerContract.test_database_responses_are_not_mock`, `test_origin_filter_keeps_mock_rows_out_of_live_values` |
| 13 | 페이지·정렬·오류 | `test_sorting_and_cursor_pagination`, `test_parameter_errors_are_explicit`, `test_ranking_cursor_keeps_its_end_date_across_midnight`, `HttpBoundary` |
| 14 | 읽기 API DB 변경 0 | `test_reads_do_not_change_the_database`, `DatabaseServerContract.test_requests_change_nothing_in_the_database` (논리 덤프·파일 바이트, 직접 호출·HTTP) |
| 15 | 외부 API 요청 0 | `DatabaseServerContract.test_requests_make_no_outbound_connections` + 전체 실행 러너의 외부 소켓 차단 |

### 전체 테스트 (`tests/run_offline.py`, Oracle 미설정 기본 환경)

| 시점 | 실행 | 실패 | skip(Oracle) | 외부 접속 시도 | 증거 |
| --- | --- | --- | --- | --- | --- |
| 기준선 3ac56d9 | 373 | 0 | 74 | 0 | [baseline-tests](baseline-tests/test-summary.json) |
| P1 | 373 | 0 | 74 | 0 | [p1-tests](p1-tests/test-summary.json) |
| P2 | 390 | 0 | 74 | 0 | [p2-tests](p2-tests/test-summary.json) |
| P3 | 399 | 0 | 74 | 0 | [p3-tests](p3-tests/test-summary.json) |
| P4 최종 | 411 | 0 | 74 | 0 | [final-tests](final-tests/test-summary.json) |

신규 38개(분석 17, API 9, 계약 12). 기존 테스트 실패는 없었고, 로그의 `ERROR:root:Collection failed`는 기존 RSS 실패 경로 테스트의 정상 로그(기준선과 같은 7건)다. Oracle 74개는 `ORACLE_TEST_*` 미설정으로 skip했다(병렬 작업 중 임의 실행 금지).

## 6. 사전 통합 점검 (dry run)

점검 시점 `origin/main` f3a77d1(ORACLE-RUNTIME-2 진행 커밋 3개, 대상 스키마 v5)의 `git archive` 스냅샷에 B8 파일만 복사해 실행했다. 병합·커밋·Push는 하지 않았다.

- 1차: B8 38개 중 2개 실패 — 예시 `error-admin-token.json`이 `.gitignore`의 `*token*.json` 규칙 때문에 P3 커밋에 빠져 있었다(로컬 워크트리에는 파일이 있어 통과). 무시 규칙은 비밀정보 방지 장치라 강제 추가하지 않고 `error-admin-header-rejected.json`으로 이름을 바꿨으며, 예시 이름이 무시 규칙에 걸리지 않는지 검사를 계약 테스트에 넣었다.
- 2차: B8 38개 통과(대상 스키마 v5에서 B7 이관·동등성 테스트 포함). 스냅샷 전체 기본 테스트: **432개 실행, 실패 0, skip 95**(ORACLE-RUNTIME-2가 추가한 Oracle 테스트 포함, `ORACLE_TEST_*` 미설정), 외부 접속 시도 0 ([preintegration-tests](preintegration-tests/test-summary.json)). 이 결과는 진행 중인 main 기준이므로 최종 통합 때 다시 실행한다.
- 파일 경로 겹침 없음. 의미상 의존과 통합 절차는 [ORACLE_RUNTIME_2_INTEGRATION.md](ORACLE_RUNTIME_2_INTEGRATION.md).

## 7. 판단과 제약

- **Instagram 30일·90일 기간 값은 대부분 미제공.** 캡션 근거가 30일 뒤 만료되어 기간의 가장 오래된 날이 `EVIDENCE_EXPIRED`가 되기 쉽다. 하한값을 숫자로 내지 않는다는 요구에 맞춘 보수적 처리이며 데이터 사전·가이드에 명시했다. Mock도 같은 상태를 보인다.
- **RSS `quality.warn_items`는 대부분 전체 콘텐츠 수와 같다.** B4 DQ가 선택 필드 누락(`fields.states`)을 WARN으로 기록하기 때문이며 값 자체에는 영향이 없다.
- **보수적 일 상태.** 그날 관측한 콘텐츠 하나라도 근거가 사라지면 그날 전체를 `EVIDENCE_EXPIRED`로 둔다. 삭제된 게시물도 같은 처리를 받는다.
- **별칭 추가의 과거 반영 한계.** 새 별칭 승인 시 B5는 근거가 남은 항목만 다시 연결한다. RSS는 기한이 없어 과거까지 반영되지만, Instagram은 근거가 만료된 과거 날짜를 당시 별칭 기준으로 둔다.
- **검색은 공백·대소문자 무시.** 유사어·오타 교정은 하지 않는다(정확 별칭 원칙).
- **루트 README는 수정하지 않았다.** ORACLE-RUNTIME-2도 README를 갱신하므로 충돌을 피하고 통합 시 B8 행을 추가한다.
- **Mock 데이터셋 캐시는 OS 임시 폴더**(`mzfoodtraker-mock-cache`)에 있으며 패키지 소스 해시가 키다. 저장소·운영 DB와 무관하다.

## 8. 범위 밖·후속 과제

- ORACLE-RUNTIME-2 완료 후 통합(병합, 충돌 검토, SQLite·Oracle 회귀, main Fast-forward, 정리) — [체크리스트](ORACLE_RUNTIME_2_INTEGRATION.md)
- 앱 API의 Oracle 저장소 연결(`--store oracle-test`)과 MZ_TEST 동등성 확인
- 외부 공개(TLS, 앱 인증, 속도 제한, 재배포 권한, 읽기 복제본) — [ACCESS.md](../../docs/app-api/ACCESS.md) §6
- 저장 집계, 종합 유행 점수(제안만), 사용자 계정, 운영 DB 마이그레이션, 실시간 수집 활성화 — 수행하지 않음

## 9. 안전

- 외부 Live API 호출 0건(러너 차단 + 계약 테스트가 서버 처리 중 비루프백 연결 0 확인). 유료 API·외부 LLM 미사용.
- 운영 DB(`D:\mz 제조기\data\trends.sqlite3`)를 열지 않았다. 앱 API 실제 서버는 최신 스키마 DB만 읽기 전용으로 연다.
- 관리 토큰은 앱 API에서 받지 않으며(헤더가 있으면 400), 응답·예시·로그에 토큰·자격 증명·내부 경로·원문이 없다. 커밋마다 비밀정보 패턴 검사(자격 증명 대입, 제공자 토큰 접두사, 개인 키, 이메일, 로컬 사용자 경로)에서 검출 0건.
- 다른 세션의 파일·프로세스·환경변수·DB·테스트 환경을 변경하지 않았다. Mock 서버 확인용 프로세스는 직접 종료했고 포트가 해제됨을 확인했다.
