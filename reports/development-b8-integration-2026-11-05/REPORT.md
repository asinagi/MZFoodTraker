# B8-INTEGRATION-1 앱 분석 API 통합과 실제 Oracle 동등성 검증

2026-11-05. ORACLE-RUNTIME-2까지의 `main`(f830489)을 B8 작업 브랜치 `feat/b8-app-api`(1b9459b)에 병합하고, 앱용 분석 API(`/api/v1`)를 실제 Oracle(개발용 MZ_TEST)에서 읽기 전용으로 실행해 SQLite와 같은 응답을 내는지 검증한 뒤 `main`에 Fast-forward로 반영했다. **OpenAPI v1 외부 계약은 바꾸지 않았다**(명세·예시 파일 변경 0). 실제 외부 API 호출 0건, 운영 SQLite·서버 변경 0, 운영 Oracle·Live 수집·운영 Shadow 미실행, G-OPS-1~6 승인 대기 유지.

## 1. 기준과 통합 전후 Git HEAD

| 항목 | 통합 전 | 통합 후 |
| --- | --- | --- |
| `origin/main` | `f830489` (ORACLE-RUNTIME-2 문서) | 이 문서를 포함한 문서 커밋 (브랜치 HEAD로 Fast-forward) |
| `origin/feat/b8-app-api` | `1b9459b` (앱 개발자 안내) | 같은 커밋 (`56c027d` 병합 → `c448c44` → `8b1a097` → 문서 커밋) |
| 분기점 | `3ac56d9` | — |
| SQLite 개발 스키마 / Oracle 대상 스키마 / 운영 SQLite | v11 / v6 / v0 | 변경 없음 |

작업 전 확인: 두 워크트리 모두 미커밋 변경 0, 로컬=원격. B8 세션(`cc55c038`)은 2026-11-04 14:50 이후 유휴, 다른 실행 중 세션(`D:\JDT`)은 다른 프로젝트. Oracle 컨테이너는 정지 상태여서 MZ_TEST를 쓰는 프로세스가 없었다(충돌 없음). 테스트·서버 프로세스와 개발 포트(8000/8002/8010/8020/8030/1521) 사용 없음.

## 2. I1 병합과 충돌

`D:\MZFoodTraker-B8-API`에서 `git merge --no-ff origin/main` → 병합 커밋 `56c027d`. **충돌 0**: 양쪽이 같은 경로를 수정한 파일이 없다(B8은 신규 파일 58개만 추가, main은 25개 변경). 병합 결과는 `origin/main` 대비 B8 파일 58개 추가뿐이고, `1b9459b` 대비 main 변경 25개뿐임을 확인해 어느 쪽 코드도 덮어쓰지 않았다. rebase·force push·기존 커밋 재작성 없음. 병합 직후 B8 테스트 38개가 대상 스키마 v6에서 그대로 통과했다(의미상 충돌 없음).

## 3. I2 실제 Oracle 조회 지원

| 구분 | 내용 |
| --- | --- |
| 저장소 선택 | `python -m food_trends.app_api --store sqlite\|oracle-test\|oracle-production`(기본 `sqlite`, 기존 `--database` 사용법 유지). `open_store()`가 기존 `runtime_storage.select()`를 재사용: `oracle-test`는 `ORACLE_TEST_*`·`environment=test`만, `oracle-production`은 연결 전에 `oracle_production_runtime_disabled`로 시작 거부. 스키마 버전이 맞지 않으면 `target_schema_not_ready`로 거부하고 DDL·마이그레이션을 실행하지 않는다. 접속 정보는 출력하지 않는다 |
| 읽기 전용 | `TargetAnalyticsStore`가 요청마다 트랜잭션 하나를 열고 **첫 문장으로 `SET TRANSACTION READ ONLY`**를 실행한 뒤 스키마 버전 확인까지 그 안에서 수행(이전에는 버전 확인이 별도 일반 트랜잭션). 쓰기 소유권 임대를 받지 않으므로 SHADOW·RUNTIME_TEST 소유 중에도 읽는다 |
| 연결 실패 | `OracleTarget.transaction()`이 풀 생성·연결 획득 단계의 드라이버 오류를 변환하지 않아, Oracle이 내려가 있으면 앱이 계약의 `503 DATA_UNAVAILABLE` 대신 `500 INTERNAL_ERROR`를 낼 수 있었다. 획득 단계도 `TargetError` 코드로 변환하도록 고쳤다(어댑터 문서의 "드라이버 오류는 코드로만" 계약) |
| JSON CLOB | python-oracledb 26.1.0은 `IS JSON` CLOB을 Python `dict`로 반환한다(실측). 기존 `_text()`는 LOB만 처리해 미처리 별칭·음식 결정(`food_events`)이 있으면 `json.loads(dict)`가 실패해 상세·수집 상태 요청이 500이 될 경로였다. dict/list를 정규 JSON 문자열로 되돌리도록 고쳤다 |

### 자료형 호환 (요청서 4.2)

| 항목 | 실측·처리 | 검증 |
| --- | --- | --- |
| TIMESTAMP·UTC | TSTZ는 UTC로 저장, 드라이버는 naive datetime(UTC 값) 반환 → `instant()`가 UTC로 해석 | 세션 시간대 `+09:00`(기본)·`America/Los_Angeles`(DST)에서 응답 동일 |
| 시간대 없는 값 | naive = UTC 규칙 | 같은 테스트 |
| NUMBER→int | `count(DISTINCT)`은 int 반환, 집계 값은 `int()` 통과 | 응답을 **직렬화한 JSON 문자열**로 비교(3과 3.0 구분) |
| JSON CLOB | dict 반환 → 정규 문자열 | 미처리 결정 2건이 있는 픽스처로 상세·상태 비교 |
| 컬럼 대소문자 | 어댑터가 소문자 키로 반환 | 모든 조회 |
| NULL과 0 | `value: null`/`0` 구분 유지 | 미관측·정상 0이 섞인 행렬 비교 |
| DISTINCT 집계 | 출처 불명 회차·콘텐츠 수 | `legacy_unknown` 포함 응답 비교 |
| 병합 관계 | 병합 리다이렉트·`merged_food_ids`·`HAS_VARIANT` | 병합 음식 ID 요청 비교 |
| 정렬·페이지 결정성 | 모든 목록은 Python에서 명시 정렬, 커서는 조건 지문 포함 | 모든 다음 페이지를 같은 커서로 따라가며 비교 |
| 복합 조건·바인드 | 이름 바인드, 병합 음식 IN 목록 | 검색·종류·정렬·기간·출처 조합 행렬 |

## 4. 수정·신규 파일과 함수

| 파일 | 변경 |
| --- | --- |
| `food_trends/analytics_reader.py` | `_text()` JSON 값 처리, `TargetAnalyticsStore._read()`(읽기 전용 트랜잭션·오류 변환), `_current()`, `ready()`·`session()` 재구성 |
| `food_trends/app_api.py` | `STORES`, `StartRefused`, `open_store()`, `main()`의 `--store` 선택·종료 시 풀 닫기 |
| `food_trends/target_repository.py` | `schema_version(tx=None)`: 호출자 트랜잭션 안에서도 확인(기존 호출 호환) |
| `food_trends/target_db.py` | `OracleTarget.transaction()` 연결 획득 오류 변환 |
| `tests/test_app_oracle.py` (신규) | 대역·실제 Oracle 동등성, 계약, 읽기 전용, 소유 모드, 시간대, 저장소 선택 |
| `README.md`, `docs/app-api/README.md` | B8 반영, `main` 기준 안내, 저장소 선택 |

## 5. I3·I4 결과 — SQLite와 실제 Oracle

같은 SQLite 픽스처를 B7 `migrate()`로 대상에 적재하고, 같은 요청을 SQLite 저장소와 대상 저장소의 `AppApi`에 보내 **상태 코드·응답 본문(정렬된 JSON 문자열)·추가 헤더**를 비교했다. 응답에는 DBMS 내부 식별자가 없고 시계를 고정했으므로 **제외 항목은 없다**.

| 픽스처 | 내용 | 비교 요청 수 | 실제 Oracle |
| --- | --- | --- | --- |
| 실데이터형 | `live_api` 회차, RSS 20일(장애 1일), Instagram 14일(실패 1일), 병합, 변형 관계, Mock 회차(제외돼야 함), 출처 불명 이력, **미처리 별칭·음식 결정 2건(JSON)**, **보존 기간이 지난 Instagram 근거 10일** | 707 (음식 350, 목록·순위·상태 332쪽, 계약 대상 25) | 일치 |
| Mock 데이터셋 | 앱 개발용 가상 데이터(회차 437, 콘텐츠 2,262, 확인 언급 2,028, 품질 결과 22,785 등 약 4.3만 행) | 154 (음식 90, 목록·순위·상태 64쪽) | 일치 |
| 계약 예시 | `docs/app-api/examples` 32개 | 32 | **파일과 문자 단위로 동일** |

다룬 범위: 7개 엔드포인트 전부, 음식 7개(병합 ID 포함)×기간 3×출처 5(전체+4)×일·주, `end_date` 과거·오늘, 음식 목록(종류 5×(검색어 없음·6개)×정렬 2~3×페이지 크기 2, 모든 다음 페이지), 출처별 순위(2출처×기간 3×종류 5×0 포함 여부×페이지 크기 2, 모든 다음 페이지), 수집 상태 3기간, 오류 400·403·404·405·422·503. 대상 응답은 OpenAPI 명세로 엄격 검사했다(정의되지 않은 필드 0).

**근거 만료와 B7의 관계(발견 사항).** 첫 비교에서 근거가 만료된 날이 SQLite는 `EVIDENCE_EXPIRED`, 대상은 남은 콘텐츠만으로 `OBSERVED`가 되는 차이가 나왔다. 원인은 B7 이관 규칙이다: 적재 시점에 `expires_at`이 지난 행은 `EXCLUDED_EXPIRED`로 옮기지 않으며(대상 일관성 조건 `expired_rows_in_target=0`), B5가 근거 만료일 판정에 쓰는 `EXPIRED` 언급 묘비 행도 함께 빠진다. 동등성 비교는 만료 전 시점 기준으로 적재해 두 저장소가 같은 논리 행을 갖게 했고, 대상에서 Oracle 런타임 자체의 B4/B5 보존 처리(`purge`)를 실행해 더 만료할 행이 0인 것까지 확인했다. 즉 Oracle 런타임이 직접 유지하는 데이터에서는 B8 결과가 같다. **실제 전환 때 이미 만료된 근거를 B7로 옮기면 해당 날짜가 대상에서 과대 집계되는 문제는 B7 정책 결정이 필요한 후속 과제로 남겼다**(§11).

### 읽기 전용·소유권 (요청서 4.3)

| 항목 | 결과 |
| --- | --- |
| `SET TRANSACTION READ ONLY` | 요청 트랜잭션 안의 UPDATE가 Oracle에서 `ORA-01456`으로 거부됨 |
| DDL·DML 0 | 전 요청 전후 대상 46개 테이블 전 행, 테이블별 `max(ORA_ROWSCN)`, `user_objects`의 DDL 시각이 동일 |
| 소유권·임대·Outbox·예산 예약 | `runtime_ownership/participants`, `execution_outbox/jobs/leases`, `quota_reservations/charges`, `aggregate_invalidations`, `entity_change_effects` 변화 0(위 비교에 포함) |
| SHADOW·RUNTIME_TEST 소유 중 | 두 모드 모두에서 읽기 응답이 SQLite와 같고, 읽기가 소유권 행을 바꾸지 않음 |
| 외부 호출 | HTTP 서버로 전 요청을 처리하는 동안 루프백 외 연결 0(Oracle 접속은 로컬) |
| 저장소 사용 불가 | 닫힌 로컬 포트의 Oracle 설정에서 `503 DATA_UNAVAILABLE`(`storage_unavailable`), health `unavailable` |

## 6. I5 전체 회귀

| 실행 | 대상 코드 | 결과 |
| --- | --- | --- |
| Oracle 없는 기본 전체(오프라인 러너) | 최종 통합 코드 | 456개 실행, 실패 0·오류 0, 실제 Oracle 전용 120개 skip, 외부 접속 시도 0, 운영 DB 접근 차단, 252초 ([default-tests](default-tests/test-summary.json)) |
| 실제 Oracle 포함 전체(오프라인 러너, MZ_TEST) | 최종 통합 코드 | 576개 실행, 실패 0·오류 0·**skip 0**, 외부 접속 시도 0, 운영 DB 접근 차단, 829초 ([oracle-full-tests](oracle-full-tests/test-summary.json)) |

실제 Oracle 실행에는 R1~R4 전체(ORACLE-RUNTIME-2에서 마지막 임대 보완 후 선별 재실행했던 테스트 포함)와 B8 신규 Oracle 테스트가 모두 들어 있다. 두 실행은 순차로 돌렸고, 기본 실행 중에는 Oracle 컨테이너를 정지했다(자원 경합 방지). 신규 테스트: `test_app_oracle` 31개(대역 15 + 실제 Oracle 16). 576 = ORACLE-RUNTIME-2 종료 시점 507 + B8 38 + 신규 31이며, 기존 테스트는 삭제·수정하지 않았다.

OpenAPI: `app_contract --check` 생성물 최신, Mock 예시 32개 재생성 결과가 저장본과 동일, 명세 구조·`$ref`·예시 검증, 실제·Mock·Oracle 응답의 명세 일치, 오류 코드·사유 문서화, 종합 점수 필드 없음.

## 7. 데이터 정책 준수

`null`≠0, `value_state`·`coverage` 유지, 시각 UTC·분석일 KST, 종합 유행 점수 없음, 출처별 순위만, 검색 일치(SEARCH_ONLY)는 언급으로 세지 않음, NAVER 저장 지수 `NOT_STORED`·YouTube 파생 분석 `POLICY_BLOCKED`, 실제·Mock `data_mode` 구분, 관리 토큰 미수용(헤더가 있으면 400), 명세에 없는 파라미터 거부 — 모두 B8 계약 테스트와 Oracle 응답 비교로 확인했다. 응답 구조 변경은 없었다.

## 8. 운영 환경

모든 테스트가 끝난 뒤 읽기 전용으로 다시 확인했다([production-final.json](production-final.json), 2026-11-05 05:32 UTC). 운영 SQLite는 v0, 테이블 30개, integrity ok, 본 파일 수정 시각 2026-10-28 14:20, WAL 0바이트로 그대로다. 8000/8002 서버는 이전부터 정지 상태이며 시작·중지하지 않았다. 테스트가 띄운 앱 서버(임시 포트)는 모두 종료됐고 개발 포트(8000/8002/8010/8020/8030/1521) 대기 0. Oracle 컨테이너는 실제 Oracle 검증 동안만 기동하고 정지 상태로 되돌렸으며, MZ_TEST 초기화는 매번 기존 보호 조건(테스트 계정·DBA 아님·대상 명세 밖 객체 없음)을 확인한 뒤에만 수행했다. 운영 마이그레이션·Live 호출·수집기 활성화·Writer 소유권 이전·Oracle Production·운영 Shadow·외부 배포는 하지 않았다.

## 9. Git

| 커밋 | 내용 |
| --- | --- |
| `56c027d` | `origin/main` 병합(충돌 0) |
| `c448c44 feat: support Oracle-backed mobile analytics API` | 저장소 선택·읽기 전용 Oracle 조회·JSON CLOB·연결 오류 변환 |
| `8b1a097 test: validate B8 analytics parity on Oracle` | `tests/test_app_oracle.py` |
| `docs: integrate B8 app API into main` (이 문서) | README, 앱 개발자 가이드, 이 보고서 |

검증을 모두 통과한 뒤 관련 파일만 단계별로 스테이징했고, 커밋마다 비밀값 패턴과 Oracle 접속 정보 값 대조(0건)를 거쳤다. `origin/feat/b8-app-api`에 먼저 Push한 뒤 원격 `main`이 그대로 `f830489`인지 다시 확인하고, `main`이 브랜치의 조상(Fast-forward 가능)일 때만 `git push origin feat/b8-app-api:main`으로 반영했다. 원격 `main`과 브랜치 HEAD가 같은지 `git ls-remote`로 확인했다. rebase·force push·`git reset --hard`·빈 커밋 없음. `.env`·DB 파일·백업·토큰·로그(`*.log`는 저장소 규칙상 제외)는 커밋하지 않았다.

## 10. 워크트리

- `D:\MZFoodTraker-B8-API` (`feat/b8-app-api`): 보존. 미커밋 변경 0이며 `main`과 같은 커밋을 가리킨다. 삭제 여부(`git worktree remove`)와 원격 브랜치 보존 여부는 사용자 결정 사항으로 남겼다.
- `D:\mz 제조기` (`main`): 미커밋 변경이 없고 다른 세션이 쓰지 않는 것을 확인한 뒤 `git merge --ff-only origin/main`으로 동기화한다(덮어쓰기·reset 없음).

## 11. 미완료 항목과 차기 작업

| 과제 | 영향 | 우선순위 |
| --- | --- | --- |
| B7 적재 시 이미 만료된 근거(B5 `EXPIRED` 묘비 행) 처리 | 전환 시점 이전에 근거가 만료된 날짜가 Oracle에서 `EVIDENCE_EXPIRED` 대신 남은 콘텐츠 수로 표시될 수 있음. 선택지: B7이 내용 없는 묘비 행을 옮기고 `expired_rows_in_target` 규칙을 묘비에 맞게 조정, 또는 적재 범위를 이관 보고서에 표시하고 B8이 해당 기간을 `INSUFFICIENT_COVERAGE`로 처리 | 높음, 운영 Oracle 전환(G-OPS-6) 전 결정 |
| 운영 SQLite v0→v11 마이그레이션(G-OPS-3) | 실제 데이터로 앱 API를 열 수 없음 | 승인 대상 |
| 외부 공개 배포(TLS·앱 인증·속도 제한·재배포 권한) | 앱은 로컬·에뮬레이터·`adb reverse`로만 연결 | 별도 과제 |
| 저장 집계, 종합 유행 점수 | 대용량 성능, 점수 공식 미확정 | 설계·제안만 |
| 병합 음식 IN 목록 1,000개 한도 | 한 음식에 병합 ID가 1,000개를 넘는 경우(현실적이지 않음) | 낮음 |
| Oracle → SQLite 역방향 복구, 남은 legacy 이관, Live 수집 | ORACLE-RUNTIME-2 후속 그대로 | 기존 순위 |
