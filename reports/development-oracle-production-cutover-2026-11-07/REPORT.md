# ORACLE-PRODUCTION-CUTOVER-1 Oracle 영구 운영 환경 구축·실데이터 이관·Live 수집·관리 API·모바일 API 통합

작업 2026-11-07~08(KST), 브랜치 `feat/oracle-production-cutover`, 기준 `main` `88b2eaf`. **최종 판정: PASS — 운영 전환 완료** (근거는 [FINAL_ACCEPTANCE.md](FINAL_ACCEPTANCE.md)).

## 결론 요약

| 질문 | 답 |
| --- | --- |
| 1. Oracle이 실제 운영 데이터를 영구 보관하는가 | **예.** 개발용 `MZ_TEST`와 분리된 운영 스키마 `MZ_APP`(최소 권한)가 Podman 영속 볼륨 `mz-oracle-data`에 있다. 백엔드·Oracle을 정상 종료·재시작한 뒤 모든 행·음식·관측·후보·추적이 그대로였다 |
| 2. 기존 SQLite 데이터가 Oracle로 이전됐는가 | **예.** 운영 SQLite(v0, 7,701행)를 검증된 백업 뒤 원본 보존 상태로 v11 사본을 만들어 15,671행 적재, 대조 `consistent`, 누락·중복·FK·정책 위반 0, B8 응답 34건 동일([MIGRATION_REPORT.md](MIGRATION_REPORT.md)). v0 원본에는 음식 개체가 없어 이관된 음식은 0건이다 |
| 3. 새 수집 데이터가 Oracle에 저장되는가 | **예.** 사용자가 승인한 실제 API 요청 7회(상한 9)가 모두 HTTP 200, 결과가 `MZ_APP`에 저장됐고 수집 경로의 SQLite 연결 0([LIVE_VERIFICATION.md](LIVE_VERIFICATION.md)) |
| 4. 관리 화면이 실제 Oracle을 조회·변경하는가 | **예.** `--backend oracle-production`(저장소 `oracle_production`)에서 음식 승인·추적 등록·후보 거절이 Oracle에 저장되고 재시작 후 유지 |
| 5. 앱 API가 같은 Oracle을 조회하는가 | **예.** `--store oracle-production`이 같은 `MZ_APP`를 읽기 전용 트랜잭션으로 읽고, 실제 수집 관측(두쫀쿠 2026-11-08 값 28)을 OpenAPI v1 그대로 제공 |
| 6. 껐다 켜도 데이터가 남는가 | **예.** 정상 종료(`podman stop -t 120`, SIGTERM으로 인스턴스 정상 종료) → 재시작 전후 스냅숏 동일. Oracle만 재시작해도 백엔드가 재시작 없이 다시 연결 |
| 7. 기존 SQLite Writer와 충돌하지 않는가 | **예.** 4개 수집원 Writer가 Oracle 런타임에 이전됐고(EXCLUSIVE), 운영 SQLite는 v0라 레거시 서버가 시작조차 못 한다 |
| 8. 장애 시 안전하게 복구할 수 있는가 | **예.** Writer 철회 → 사용량 인계 → 역방향 복구 → 재개 판정 절차가 운영 경로에서도 열린다(운영 접근 확인 포함). 파괴적 시나리오는 MZ_TEST 회귀가 검증 |
| 9. 실제 API 호출과 Oracle 적재가 검증됐는가 | **예.** RSS·Instagram·YouTube·NAVER 각 수집원 1회씩, 요청 수 = 실행 기록 = 저장 결과 일치 |
| 10. 코드와 문서가 GitHub에 반영됐는가 | **예.** 작업 브랜치 커밋을 `main`에 fast-forward로 통합하고 `origin/main`에 push(아래 10절·통합 기록) |

## 1. 기존 상태 대비 변경점

| 항목 | 작업 전 | 작업 후 |
| --- | --- | --- |
| 운영 Oracle | 미구축, 코드에서 전면 차단 | `MZ_APP` 구축(v10), 명시적 운영 접근으로 사용 |
| 운영 실행 모드 | `oracle-production`·`RUNTIME_PRODUCTION` 항상 거부 | 선언된 운영 스키마 + 승인 기록 + 소유 모드(SHADOW/RUNTIME_PRODUCTION)를 확인한 뒤에만 허용. `ORACLE_ALLOW_PRODUCTION`만으로는 접속만 열림 |
| 데이터 | SQLite v0만 | Oracle `MZ_APP`이 SoR(이관 15,671행 + 실제 수집분). SQLite v0 원본·백업·v11 사본은 보관 |
| 수집 | Oracle Live 미검증 | 실제 소량 검증 완료, Writer는 Oracle 런타임 |
| 관리·앱 API | Oracle 테스트 스키마까지 | 운영 스키마에서 실행, 운영 스크립트 제공 |

## 2. 구현 (Phase D)

- `food_trends/production_access.py`: 운영 접근 확인 — 접속 계정 = `ORACLE_PRODUCTION_SCHEMA`(`*_TEST`·테스트 계정·시스템 계정·DBA 보유 계정 거부), 승인 기록 `ORACLE_PRODUCTION_APPROVAL`(스키마·승인자·시각·G-OPS-1/2/3/6 근거, 형식 엄격).
- `runtime_ownership`: 운영 대상은 승인 확인 뒤 SHADOW·RUNTIME_PRODUCTION만, RUNTIME_TEST 거부. 다른 대상의 RUNTIME_PRODUCTION 거부. 테스트 라벨 Oracle 대상은 `*_TEST` 스키마만. `runtime_mode()`로 실행 주체(실행기·Writer 이전·사용량 가져오기·인계)가 대상에 맞는 모드를 쓴다.
- `oracle_execution`: 운영 대상은 승인된 경우에만 실행, 운영에서는 모든 작업이 Live여야 함(`MOCK_EXECUTION_REFUSED_ON_PRODUCTION`) — Mock 응답이 운영 데이터에 들어가지 않는다.
- `runtime_storage`: `select('oracle-production')`(ORACLE_* + 운영 확인), `oracle_test_repository`는 `*_TEST`만, `production_backend`(수집기 없는 운영 관리 조립: 실제 계정 기준 추적·계획·관측, RSS 요청 거부).
- CLI: `--backend oracle-production`(관리 서버·유지보수·Live 점검·Writer 이전/철회/상태), `--rss-mock-file`은 운영에서 거부, `--target-*`·`--recovery-target oracle`은 운영 대상이면 DDL·적재 전에 운영 확인. 앱 API `--store oracle-production`.
- `oracle_live`·`quota_ledger`·`usage_handover`: 운영 대상에서 승인 확인 + RUNTIME_PRODUCTION.
- 테스트 도구 `oracle_support.reset_target_schema`: `*_TEST`이면서 선언된 운영 스키마가 아닌 계정만 초기화.
- 관리 서버(`food_trends/api.py`): POST 본문을 응답 전에 읽는다(1MB 한도). 본문을 읽지 않은 채 거부(401·403·503)를 보내면 Windows가 연결을 리셋해 클라이언트가 응답을 잃었다(WinError 10053). 기본 회귀에서 같은 테스트가 두 번 간헐 오류로 드러났고, 600KB 거부 요청 10회 중 4회 실패 → 수정 후 0회. 회귀 테스트 `test_a_refusal_reaches_the_client_even_over_a_large_body` 추가.
- 기존 거부 코드(`oracle_production_runtime_disabled`, `production_target_requires_approval`, `runtime_production_disabled`, `ORACLE_PRODUCTION_LOCKED`)는 승인 없는 경우에 그대로 유지(기존 테스트 계약 보존).

## 3. 운영 환경 (Phase A·B)

| 항목 | 상태 |
| --- | --- |
| Git | 시작 `main`=`origin/main`=`88b2eaf`, 미커밋 변경 없음 |
| 다른 세션·프로세스 | 테스트·서버 프로세스 없음 |
| 운영 SQLite | `data/trends.sqlite3`, v0, 30개 테이블 7,701행, SHA-256 `38b880f5…689548c0`(기준과 같음), integrity ok, FK 0, WAL 0 — 작업 전후 불변 |
| 자격 증명 | Instagram·YouTube·NAVER 값 존재(값은 출력하지 않음) |
| 최근 사용량 | 레거시 기록상 열린 창 사용량 0(마지막 호출 10-26~10-28) |
| 기존 Writer | 서버·스케줄러 없음, v0라 시작 불가 |
| 자원 | WSL 메모리 7.8GB, Oracle Free(USERS 72MB 사용). **`C:` 여유 약 28GB**(WSL 가상 디스크 위치) — 데이터 증가와 백업 시 확인 필요 |
| 운영 계정·권한 | 컨테이너 안 OS 인증 SYSDBA로 `MZ_APP` 생성(CREATE SESSION·CREATE TABLE, USERS 4GB). 백엔드는 `MZ_APP`만 사용 |
| 영속 저장 | Podman 이름 있는 볼륨 `mz-oracle-data` |

## 4. G-OPS 점검 (A-3)

| 게이트 | 판정 | 근거 |
| --- | --- | --- |
| G-OPS-1 백업 | 충족 | 요청서 §2.1 승인. 백업 검증(다이제스트 동일·복원 시험) |
| G-OPS-2 Writer 정지 | 충족 | 요청서 §2.1. 운영 SQLite 서버 시작 불가(v0), 포트 없음. Writer는 Oracle로 이전 |
| G-OPS-3 SQLite 마이그레이션 | 충족(원본 무변경 경로) | 요청서 §2.1·C-3 우선 경로: 검증된 v11 사본만 마이그레이션 |
| G-OPS-4/5 수집 소유권·Live 검증 | 충족(소량) | 사용자가 이 세션에서 4개 수집원 승인, 상한 9회. 지속 수집 승인은 없음 |
| G-OPS-6 운영 Oracle | 충족 | 요청서 §2.1. 승인 기록 `data/oracle-prod/production-approval.json` |

## 5. 단계별 결과

| Phase | 결과 | 근거 |
| --- | --- | --- |
| B 영구 DB | `MZ_APP` v10, 107개 객체, 영속성 검증 | [persistence-before.json](persistence-before.json)·[persistence-after.json](persistence-after.json) |
| C 이관 | 15,671행, 재실행 멱등, B8 동등 | [MIGRATION_REPORT.md](MIGRATION_REPORT.md) |
| D 운영 모드 | 위 2절, 테스트 `tests/test_production_access.py` | |
| E 실제 수집 | 7/9 요청, 전부 200, Oracle 저장·B8 확인 | [LIVE_VERIFICATION.md](LIVE_VERIFICATION.md) |
| F 관리 서버 | 운영 스키마에서 조회·승인·추적·거절, 재시작 후 유지 | persistence 스냅숏 |
| G 앱 API | 같은 스키마, OpenAPI 위반 0, 읽기 전용 트랜잭션 | [APP_HANDOFF.md](APP_HANDOFF.md) |
| H 운영 스크립트 | `scripts/ops/*.ps1`(시작·정지·상태·점검), 중복 실행 거부 | [PRODUCTION_RUNBOOK.md](PRODUCTION_RUNBOOK.md) |
| I 장애·안전 | Oracle 정지 중 503→재시작 후 자동 회복, 잘못된 설정 6종 거부 | [failure-checks.json](failure-checks.json) |
| J 회귀 | 아래 6절 | |

## 6. 테스트

| 실행 | 결과 | 근거 |
| --- | --- | --- |
| 새 테스트 | `test_production_access` 4개(대역) 통과 | |
| 운영 하네스 건식 실행(MZ_TEST + 가짜 전송) | 요청 7, 작업 성공, B8 6개 200·위반 0, SQLite 0 | |
| 기본 전체 회귀(Oracle 정지) | 534개 통과, Oracle 전용 189개 skip, 실패·오류 0, 차단된 외부 시도 0(651초). 직전 1차 실행의 간헐 오류 1건(관리 서버 연결 리셋)은 원인을 고친 뒤 다시 실행한 결과 | [default-tests/test-summary.json](default-tests/test-summary.json) |
| 실제 Oracle 전체 회귀(MZ_TEST) | **723개 전부 통과, 실패·오류·skip 0**(차단된 외부 시도 0). 실행 중 PC가 09:05~18:06 절전·수면에 들어가 실측 34,043초였으나 오류 없이 끝났다. 회귀 전후 운영 스키마 행 수 동일(운영은 초기화 대상 아님) | [oracle-full-tests/test-summary.json](oracle-full-tests/test-summary.json) |
| 운영 스키마 비파괴 점검 | `verify.ps1` 실패 0(재시작 전후), 운영 스키마는 자동 초기화 대상이 아님 | persistence 스냅숏 |

## 7. 남은 문제

- **지속 자동 수집: CONFIGURED_NOT_ENABLED.** 반복 호출에는 최초 소량 검증과 별개의 지속 사용 권한·예산 승인이 필요하다.
- 제공자 측 사용량은 확인할 수 없다(UNKNOWN). 로컬 원장만 근거.
- 운영 API는 루프백 전용이다. Mac·다른 기기의 실데이터 접근, 외부 공개(TLS·인증)는 별도 과제.
- 운영 백엔드 프로세스는 스크립트로 수동 시작한다(무인 자동 시작은 승인되지 않음). WSL 세션이 끝나면 컨테이너가 멈출 수 있어 `start-oracle.ps1`이 연결된 세션을 유지한다.
- `C:` 여유 공간 약 28GB.
- 백엔드 실행 계정이 스키마 소유자(`MZ_APP`)다. DML 전용 계정 분리는 코드가 스키마 이름 없는 SQL을 쓰므로 추가 개발이 필요하다(현재 계정은 CREATE SESSION·CREATE TABLE만).
- 앱 API 기본 기간은 어제(KST)까지라 당일 수집분은 `end_date` 지정 시 보인다(계약대로).

## 8. 실제 외부 호출과 데이터 보호

- 실제 외부 요청: **7회**(RSS 1·Instagram 3·YouTube 2·NAVER 1), 모두 사용자 승인 범위 안, 상한 초과 거부 0.
- 운영 SQLite 원본 쓰기 0(해시·시각·WAL 불변). 백업·v11 사본·접속 정보·승인 기록·registry는 Git 밖(`data/`).
- 자격 증명·비밀번호·응답 원문·계정 ID는 저장소·보고서에 없다(민감정보 검사).

## 9. 수정·추가 파일

| 파일 | 내용 |
| --- | --- |
| `food_trends/production_access.py` | 운영 접근 확인 |
| `food_trends/runtime_ownership.py`, `oracle_execution.py`, `oracle_live.py`, `quota_ledger.py`, `usage_handover.py`, `runtime_storage.py`, `app_api.py`, `__main__.py` | 운영 모드 경로 |
| `food_trends/api.py`, `tests/test_admin_oracle.py` | 관리 서버가 POST 본문을 응답 전에 읽음(Windows 연결 리셋 방지), 회귀 테스트 |
| `tests/test_production_access.py`, `tests/oracle_support.py` | 운영 접근 테스트, 테스트 초기화 보호 |
| `scripts/ops/*` | 운영 스크립트, 실제 소량 검증 하네스, 비파괴 점검 |
| `docs/app-api/README.md` | 실제 운영 API 실행·접속 안내 |
| `README.md` | 현황 |
| `reports/development-oracle-production-cutover-2026-11-07/` | 이 보고서와 근거 |

## 10. Git

작업 브랜치 `feat/oracle-production-cutover`(원격 push), 단계별 커밋:

| 커밋 | 내용 |
| --- | --- |
| `dea4b33` | feat: 운영 접근 확인(`production_access`), 운영 모드 경로 |
| `a8eebc5` | feat: 운영 스크립트, 실제 소량 검증 하네스, CLI 대상 명령의 운영 확인 |
| `875997e` | fix: 관리 서버가 POST 본문을 응답 전에 읽음(Windows 연결 리셋) |
| 이 보고서 커밋 | docs: 보고서·절차서·인계서·인수 점검·README·앱 API 가이드 |

`main` 통합 결과는 아래 [통합 기록](#통합-기록).
