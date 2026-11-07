# ORACLE-LIVE-VERIFICATION-READY-1 Oracle Live 실데이터 검증 준비·격리 환경 리허설·최종 운영 적합성 감사

작업 2026-11-07(KST), 브랜치 `feat/oracle-live-verification-ready`, 기준 `main` `98a2e24`. **실제 외부 API 호출 0건, 운영 SQLite 쓰기 0건, 운영 Oracle 접근 0건, 실제 계정 Writer 이전 0건, 기존 수집기 재활성화 0건.** 이 보고서는 실제 API 검증이나 운영 전환의 완료 보고가 아니다.

## 결론 요약

| 질문 | 답 |
| --- | --- |
| 실제 API 소량 검증을 추가 개발 없이 실행할 수 있는가 | **예, 승인 조건부.** 운영 DB 격리 사본과 MZ_TEST로 전 과정을 리허설했고(28개 점검 통과), 그때 드러난 실행 차단 결함 3건을 고쳤다. 명령·요청 상한·중단·복구 절차를 [LIVE_PILOT_RUNBOOK.md](LIVE_PILOT_RUNBOOK.md)에 확정했다 |
| 무엇이 막고 있는가 | 승인뿐이다: G-OPS-4/5, 수집원별 실제 승인 근거가 담긴 registry, 첫 실제 요청에 의한 자격 증명 확인. 4개 수집원 모두 `REQUIRES_APPROVAL`, 기술 차단 0([SOURCE_READINESS.md](SOURCE_READINESS.md)) |
| 운영 DB를 바꿔야 하는가 | 아니다. 경로 A(격리 v11 사본)는 G-OPS-3 없이 가능하다. 운영 SQLite 수집기는 v0 때문에 시작조차 못 하므로 이중 Writer가 생기지 않는다 |
| 찾은 결함 | ① CLI가 격리 사본을 운영 승인 스위치 없이 가리킬 수 없었다 ② 운영 DB처럼 Instagram 기록이 오래되면 Instagram Writer를 영원히 이전할 수 없었다 ③ 사용량 인계 뒤 복구가 Oracle 기간의 갱신을 빠뜨린 채 `converged`를 보고했다. 모두 수정하고 회귀 테스트를 추가했다(8절) |
| 테스트 | 기본 전체 529개 통과·Oracle 188개 skip, 실제 Oracle 전체 717개 중 **716개 통과·오류 1·실패 0·skip 0**. 오류 1건은 Oracle 인스턴스 크래시 복구 직후 첫 스키마 초기화의 환경 일시 오류로, 그 모듈만 다시 실행해 8/8 통과(9절). 기존 711개는 삭제·약화 없음 |
| 운영 전환 | **BLOCKED**(별도). 운영 Oracle은 코드에서 계속 거부되고 G-OPS-1~6 승인도 없다 |

판정표는 [GO_NO_GO.md](GO_NO_GO.md), 리허설 상세는 [ISOLATED_CUTOVER_REHEARSAL.md](ISOLATED_CUTOVER_REHEARSAL.md), 갱신한 요청서는 [LIVE_VERIFICATION_REQUEST.md](../development-oracle-live-integration-2026-11-05/LIVE_VERIFICATION_REQUEST.md).

## 1. 작업 전후 Git HEAD

| 시점 | `main` | `origin/main` | 비고 |
| --- | --- | --- | --- |
| 시작 | `98a2e24` | `98a2e24` | 미커밋 변경 없음, worktree 1개, stash 없음 |
| 종료 | 아래 [통합 기록](#통합-기록) | 같음 | 14절의 커밋을 fast-forward로 통합(강제 push·reset hard 없음) |

## 2. 작업 시작 점검

| 항목 | 결과 |
| --- | --- |
| 다른 세션·프로세스 | 다른 Claude Code 프로세스 1개가 있으나 이 저장소의 대화 기록은 2026-11-05 16:20 이후 쓰기 없음(유휴). 테스트·서버 프로세스 없음 |
| Oracle | 컨테이너 정지 상태에서 시작, MZ_TEST는 직전 작업이 초기화한 상태. 리허설·회귀 동안만 실행 |
| 운영 SQLite | v0, 30개 테이블 7,701행, WAL 0바이트, integrity ok, FK 오류 0, SHA-256 `38b880f535dded56e84a11570a1f7793bed1668580950172a417687b689548c0`(이전 보고서와 같음), 수정 시각 2026-10-28 14:20:01 KST. 읽기 전용 점검이 수정 시각을 바꾸지 않음 |
| 수집기·스케줄러 | 실행 중 없음, 8000·8002·8010·8020·8030·8031 포트 없음. `.env`는 `INSTAGRAM_COLLECTION_ENABLED=true`, YouTube·NAVER 스위치 미설정(기본 켜짐)이지만 **운영 DB가 v0라 SQLite 서버는 `MigrationRequired`로 시작이 거부**된다 |
| `.env`와 우선순위 | `.env` 14개 키(이름만 확인). 프로세스 변수가 우선(`load_env`는 setdefault), 현재 프로세스에 관련 변수 없음. `DATABASE_PATH` 미설정 |
| 자격 증명 | Instagram 토큰·사용자·페이지, YouTube 키, NAVER 클라이언트 ID·Secret 모두 값 있음. 유효성은 확인하지 않음(외부 호출 필요) |
| 기존 Live 게이트 | `--execution-readiness`의 registry·자격 증명·Live 스위치·전용 수집원·기존 스케줄러 정지·Writer 이전·차단·운영·Shadow 점검, 요청 직전 재확인 — 그대로 |
| 운영 DB 보호 | 테스트 러너가 운영 파일 연결을 거부, `_operational`이 운영 파일명·경로·`DATABASE_PATH`·같은 파일을 운영 DB로 보고 인계·복구 쓰기를 승인 스위치 없이는 거부 |

## 3. Phase A — 운영 SQLite 격리 v11 (READY)

- **구현**: `cutover.isolate()`·`--isolate-database PATH`(기존 `backup()`·`prepare()` 재사용) — 읽기 전용 원본을 백업 API로 복사, 사본 헤더에 격리 표시(`application_id`), 사본만 v0→v11·B4/B5 스테이징·출처 불명 이관·보존 정리. 운영 파일명·경로·`DATABASE_PATH`·하드 링크·기존 파일·남은 저널·Git이 추적할 확장자는 복사 전에 거부. `cutover.isolated()`·`--isolated-database PATH` — 격리 표시가 있고 운영 DB가 아닌 파일만 받아 `--target-load`·`--recovery`·`--writer-handover`·`--legacy-resume-check`가 `DATABASE_PATH` 대신 쓴다(운영 승인 스위치와 함께 쓰면 거부).
- **실데이터 결과**(운영 DB 사본, [리허설](ISOLATED_CUTOVER_REHEARSAL.md) 1절): 원본 SHA-256·크기·수정 시각·WAL·버전·무결성·행 수 7개 항목 불변, 원래 30개 테이블 열·행 보존, 사본 v11·integrity ok·FK 0, RSS 회차 95·항목 370·관측 900·Instagram 185 계보 불일치 0, 만료 원문 부당 복원 0, 임의 FOOD_ID 0, 분류되지 않은 테이블 0, 재실행은 `isolated_copy_exists`로 거부. `--writer-handover`·`--legacy-resume-check`가 격리 사본을 대상으로 동작.
- **경로 보호 테스트**: `tests/test_isolated_copy.py` 4개(합성 v0 원본을 `DATABASE_PATH`로 둔 상태에서 모든 쓰기 경로 거부·원본 불변).

## 4. Phase B — 수집원별 선행 조건 (전부 REQUIRES_APPROVAL)

`tests/audit_source_readiness.py`(읽기 전용, 외부 요청 0)로 점검했다. 상세 표는 [SOURCE_READINESS.md](SOURCE_READINESS.md).

| 수집원 | 자격 증명 | 유효성 | registry | 레거시 사용(열린 창) | 기술 차단 | 판정 |
| --- | --- | --- | --- | --- | --- | --- |
| Google Trends RSS | 불필요 | 해당 없음 | 없음 | 예산 없음 | 없음 | REQUIRES_APPROVAL |
| Instagram | 있음, 토큰 만료 기록 2026-12-25(47일) | 미확인(10-26 마지막 확인) | 없음 | 0, 기록 계정 = 설정 계정 | 없음 | REQUIRES_APPROVAL |
| YouTube | 있음 | 미확인(10-26) | 없음 | 0 | 없음 | REQUIRES_APPROVAL |
| NAVER | 있음 | 미확인(10-26) | 없음 | 0 | 없음 | REQUIRES_APPROVAL |

제공자 측 실제 사용량은 확인할 수 없어 `UNKNOWN`으로 남겼고, `PROVIDER_CONFIRMED`는 만들지 않았다.

## 5. Phase C — Writer 이전·철회·사용량 인계 전 과정 리허설 (통과)

`tests/rehearse_live_cycle.py`가 운영자 CLI를 같은 프로세스에서 그대로 실행했다(격리 사본 + MZ_TEST, 합성 registry·자격 증명, 가짜 전송). 15단계 결과는 [ISOLATED_CUTOVER_REHEARSAL.md](ISOLATED_CUTOVER_REHEARSAL.md) 2절.

| 필수 검증(C-3) | 결과 |
| --- | --- |
| 이전 직후 Oracle만 Writer, SQLite·Oracle 동시 요청 0 | 4개 `EXCLUSIVE`, 레거시 스케줄러를 켜면 점검이 `LEGACY_SCHEDULER_ENABLED`로 거부. 모든 요청이 Oracle 작업 안에서 나감(가짜 전송 수 = 대상 기록 수) |
| 철회 직후 신규 요청 0, DRAINING 중 기존 요청 안전 저장 | 전송 중이던 YouTube videos 요청은 저장, 같은 수집원의 나머지 작업 2개는 요청 0으로 `WRITER_RELEASED` |
| 미완료 작업 정리·중복 호출 없음 | 철회 뒤 활성 작업·Lease 0, 호출 기록 중복 0, 자연키 중복 0 |
| 불확실 사용량 보수적 합산 | 기존 설계 유지(결과 불명 호출도 인계), 단위 테스트로 확인 |
| 인계 전 재개 거부, 누락 레거시 사용량 시 거부, RSS 복구 전 거부 | `ORACLE_USAGE_NOT_HANDED_OVER`(창 끝 시각 포함), `ORACLE_RSS_NOT_RECOVERED`, 누락 사용량은 단위 테스트(`DESTINATION_MISSING_LEGACY_USAGE`) |
| 안전한 조건에서만 `ready: true` | 철회·인계·복구 뒤에만 4개 모두 `ready: true` |
| 이전·철회·복구 재실행 멱등, 다른 수집원 영향 없음 | 재이전 `unchanged`, 재철회 `unchanged`, 재인계 0건, 재복구 쓰기 0. YouTube 철회 중 나머지 3개는 `ORACLE` 유지·정상 수집 |
| 기존 SQLite 예산 함수가 인계 사용량을 반영 | 격리 사본에서 YouTube 새 검색 `budget` 거부, NAVER 오늘 호출 1, Instagram 7일 고유 태그 1·새 태그 `budget` 거부 |

## 6. Phase D — Oracle Live HTTP E2E와 데이터 정책

D-1 흐름은 ORACLE-LIVE-INTEGRATION-1의 `tests/test_oracle_live_e2e.py`가 이미 실제 HTTP(관리 서버 + 별도 연결 풀의 B8)로 구현하고 있어 다시 만들지 않았다. 이번 실제 Oracle 전체 회귀에서 다시 통과했고, 리허설은 같은 흐름을 운영 데이터가 실린 대상에서 반복했다.

| D-2 항목 | 검증 위치(실제 Oracle 포함) |
| --- | --- |
| RSS가 Oracle에 직접 적재 | `test_oracle_live_e2e`(회차 origin `live_api`), 리허설(재관측 항목 포함) |
| 미승인 음식 B8 제외 | `test_oracle_live_e2e`(미승인 후보명이 B8 응답에 없음) |
| 음식 승인·별칭 이력 | `test_oracle_live_e2e`, `test_oracle_backend.test_identity_and_tracking_commands_decide_as_the_sqlite_services`(Oracle 음식·별칭 명령과 이력) |
| 여러 별칭의 같은 게시물 1회 | `test_oracle_live_e2e`(두 해시태그 작업, 값 1) |
| 실패한 날 `NOT_OBSERVED` | `test_oracle_live_e2e` 11a |
| 정상 0은 `OBSERVED` 0 | `test_oracle_e2e.test_rss_discovery_to_b8_analytics_on_one_backend`, `test_oracle_backend`, `test_oracle_observations` |
| 제한 수집 `LIMITED`/`PARTIAL` | `test_oracle_e2e`(페이지 상한 → `LIMITED`), `test_oracle_backend` |
| 근거 만료 `EVIDENCE_EXPIRED` | `test_oracle_live_e2e` 11c |
| 출처별 순위만, 통합 점수 없음 | `test_food_analytics.test_rankings_are_single_source_with_competition_ranks`, `test_app_contract.test_contract_has_no_composite_score` |
| YouTube 음식 파생 차단 | `test_oracle_live_e2e`·리허설(YouTube 언급 0) |
| NAVER 응답·지수 미저장 | `test_oracle_live_e2e`·리허설(NAVER 지표 0, 인계 행은 호출 사실만) |
| Instagram 보존 기간 | `test_oracle_live_e2e`(31일 뒤 만료), 리허설 정책 위반 행 0 |
| UTC 기록·KST 분석일 | `test_food_analytics.test_kst_day_boundaries`, `test_oracle_scheduling.test_kst_midnight_and_los_angeles_dst_boundaries` |
| Oracle 관리 서버 | `test_admin_oracle`, 리허설(관리 API로 전 과정) |
| B8 OpenAPI v1 계약 | 모든 E2E 응답을 명세로 검증, `test_app_contract`. `docs/app-api/openapi.yaml`은 `main`과 같음(7개 경로·7개 연산, 3.0.3/1.0.0) |

**D-3 저장소 독립성**: 리허설의 Live 라운드(RSS 수집 → 승인 → 추적 → 계획 → Live 실행 → B4/B5 → 관측) 동안 sqlite3 연결 **0건**. 격리 마이그레이션·사용량 가져오기·인계·복구·재개 판정·레거시 예산 확인은 예외 단계로 따로 셌다(전부 격리 사본, 운영 파일은 격리 복사 때 읽기 전용 5회뿐). E2E 테스트는 Live 구간의 모든 sqlite3 연결을 거부한 상태로 통과한다.

## 7. Phase E — 실제 API 소량 검증 실행 준비

- [LIVE_PILOT_RUNBOOK.md](LIVE_PILOT_RUNBOOK.md): 환경 변수 이름·역할, registry 작성·검토, 사전 점검, 격리 사본·대상 v10·적재, Writer 이전, Live 서버(예약·자동 실행 없음), 수집원별 수동 실행, 요청 수 기록 방법, 즉시 중단, 철회·DRAINING, 인계, 복구, 재개 판정, 정리(MZ_TEST 보존 기한 포함), 민감정보 확인, 경로 B 차이. 모든 명령은 현재 CLI 옵션이며 리허설로 실행됐다.
- 요청 상한(최대 9회: RSS 1·Instagram 5·YouTube 2·NAVER 1)을 예산 설정이 강제한다. 리허설에서 별칭을 더한 계획(작업 6개)으로도 요청은 7회였고 다시 실행해도 늘지 않았다. RSS는 예산이 없어 `/api/collect`를 한 번만 부르는 운영 규칙으로 지킨다.
- 요청서 갱신: 판정 "선행 개발 조건 모두 해결, 승인 대기". 기존 4절의 "사용량 가져오기만 실행"(그런 명령이 없음)과 Oracle 접속 변수 누락을 바로잡았다.
- 경로 A/B: 경로 A 권장(G-OPS-3 불필요). 검증 사용량은 격리 사본으로만 인계되므로, 운영 수집기는 G-OPS-3 이후에도 검증 사용량 창(최대 7일)이 끝나기 전에는 경로 B 인계 없이 켜지 않는다. 그때까지 MZ_TEST를 초기화하지 않는다.

## 8. Phase F — 차단 문제와 수정

| # | 결함(발견 경위) | 영향 | 수정 | 회귀 테스트 |
| --- | --- | --- | --- | --- |
| F-1 | CLI의 인계·복구 대상은 항상 `DATABASE_PATH`이고 그 경로는 운영 DB로 간주된다. 격리 사본을 쓰려면 `--allow-operational-database`를 줘야 했다(코드 검토) | 운영 승인 스위치가 일상 옵션이 되어, 경로를 잘못 주면 운영 원본에 쓸 수 있었다 | `--isolate-database`·`--isolated-database`(격리 표시 있는 사본만, 운영 DB·스위치 병용 거부) | `test_isolated_copy` 3개 |
| F-2 | `--target-load`의 레거시 사용량 가져오기가 열린 창에 행이 있는 Instagram 계정만 기록했다. 운영 DB의 마지막 Instagram 검색은 7일보다 오래돼 설정 계정이 기록되지 않았다(리허설 1차 실행: `--writer-transfer instagram_graph` 거부) | 실제 검증에서 Instagram Writer를 영원히 이전할 수 없었다. 단위 테스트는 계정을 직접 넘겨서 드러나지 않았다 | 적재가 설정된 Instagram 계정도 사용량 확인 계정으로 기록(`legacy_usage_accounts`로 출력) | `test_isolated_copy.test_the_documented_load_lets_each_configured_account_be_transferred` |
| F-3 | 인계가 SQLite 파일을 바꾸면 복구 기준선(적재 당시 파일 지문)이 맞지 않아 `--apply-updates`가 갱신을 승인하지 않았는데도 `converged: true`, 재개 판정 `ready: true`였다(리허설 2차 실행: 재관측 RSS 항목의 갱신 3건 누락) | 절차서 순서(인계 → 복구)대로 하면 Oracle 기간 변경이 SQLite에 돌아오지 않았다 | 인계가 파일 지문을 쓰기 전후로 `USAGE_HANDOVER` 이벤트에 기록하고, 복구 기준선이 "적재한 파일 → 기록된 인계" 사슬을 인정(`matches_recorded_handover`). 다른 주체가 바꾼 파일은 계속 거부 | `test_writer_release.test_recovery_after_the_handover_still_applies_the_oracle_periods_updates`(대역 + MZ_TEST) |

운영 Oracle 잠금은 건드리지 않았다. 제공자 정책·권한 문제는 발견되지 않았다(실제 확인은 승인 뒤).

## 9. Phase G — 전체 테스트

| 실행 | 결과 | 시간 | 근거 |
| --- | --- | --- | --- |
| 변경 모듈(대역 + MZ_TEST) | `test_isolated_copy` 4, `test_writer_release` 22, `test_reverse_recovery` 22, `test_cutover` 11, `test_oracle_quota` 12 — 모두 통과 | — | 단계별 실행 |
| 격리 리허설(운영 사본 + MZ_TEST) | 28개 점검 통과 | 178.6초 | [isolated-rehearsal-oracle.json](isolated-rehearsal-oracle.json) |
| 기본 전체 회귀(Oracle 컨테이너 정지) | 529개 통과, Oracle 188개 skip, 실패·오류 0, 차단된 외부 시도 0 | 463.4초 | [default-tests/test-summary.json](default-tests/test-summary.json) |
| 실제 Oracle 전체 회귀(MZ_TEST) | 717개 실행: **716개 통과, 실패 0, 오류 1, skip 0**, 차단된 외부 시도 0 | 1,546.4초 | [oracle-full-tests/test-summary.json](oracle-full-tests/test-summary.json) |
| 오류 모듈 재검증(MZ_TEST) | `test_admin_oracle` 8개(대역 4 + MZ_TEST 4, 오류 난 테스트 포함) 모두 통과 | 30.3초 | [oracle-recheck-admin/test-summary.json](oracle-recheck-admin/test-summary.json) |

- 새 테스트 6개(`test_isolated_copy` 4, `test_writer_release` 1 × 대역·MZ_TEST)를 더해 전체 711 → 717개. 기존 테스트는 삭제·약화하지 않았다(`test_writer_release`에 import 추가만).
- OpenAPI 계약 검사는 두 회귀에 포함(`test_app_contract`, E2E 응답 검증).
- **실제 Oracle 오류 1건**: `test_admin_oracle.AdminOracleTests.test_every_ui_route_reads_and_writes_the_target_alone`의 `setUp`이 회귀의 첫 Oracle 작업인 `reset_target_schema`에서 `database_error`. 직전 회귀(13:43 시작)가 14:08 `test_youtube` 실행 중 작업 세션 종료로 프로세스와 컨테이너가 강제로 끝났고, 21:46 재시작한 인스턴스는 경고 로그대로 크래시 복구부터 했다(`Beginning crash recovery of 1 threads` → `Completed crash recovery`). 복구 직후 죽은 트랜잭션 정리가 남은 표를 첫 `DROP TABLE`이 만난 것으로 보인다. 같은 클래스의 다음 테스트부터 같은 초기화가 정상이었고 나머지 716개가 통과했으며, 이번 작업은 `test_admin_oracle`·`oracle_support`를 바꾸지 않았다. 전체 재실행 대신 그 모듈만 다시 실행해 8/8 통과했다. 중단된 13:43 회귀는 결과 파일이 없어 근거로 쓰지 않았다.
- 컨테이너 정지: `podman stop`의 기본 10초 안에 Oracle이 끝나지 않아 SIGKILL로 정지된다(다음 시작 때 크래시 복구). 개발 환경에서는 `podman stop -t 120 mz-oracle-free`로 정지하는 편이 안전하다.
- 절전: 리허설 첫 실행이 Modern Standby(05:42:59~13:25:35 KST)로 7시간 넘게 멈췄다가 통과했다. 근거로는 절전 방지 래퍼(SetThreadExecutionState, 실행 중에만 적용·종료 시 해제) 아래 재실행 결과를 쓰고, 두 전체 회귀도 같은 래퍼로 실행했다.

## 10. 실제 API 호출 여부

**0건.** Live 요청은 기존 클라이언트를 그대로 쓰되 네트워크 아래를 기록용 가짜 전송으로 바꿨고, 리허설·준비도 점검·테스트 러너가 루프백 밖 소켓을 모두 거부했다(차단 시도 0). 자격 증명은 리허설에서 합성 값만 썼고, 준비도 점검은 실제 `.env`를 존재 확인용으로만 읽었다.

## 11. 운영 DB 영향

| 항목 | 시작 | 종료 |
| --- | --- | --- |
| SHA-256 | `38b880f5…689548c0` | 같음 |
| 스키마 / 테이블 / 행 | v0 / 30 / 7,701 | 같음 |
| WAL | 0바이트 | 0바이트 |
| 수정 시각 | 2026-10-28 14:20:01 KST | 같음(점검이 바꾸지 않음) |
| integrity / FK 오류 | ok / 0 | ok / 0 |

운영 파일은 격리 복사·준비도 점검에서 읽기 전용으로만 열렸고, 리허설 가드가 쓰기 가능 연결을 거부했다(시도 0). `--migrate`·`--maintenance`·복구 apply·사용량 인계를 운영 원본에 실행하지 않았다. 운영 데이터가 담긴 격리 사본은 리허설 임시 폴더와 함께 삭제됐다. MZ_TEST는 끝에 일회용 계정임을 다시 확인한 뒤 초기화했고(테이블 0), Oracle 컨테이너를 정지했다. 종료 점검은 [production-final.json](production-final.json)(2026-11-07 22:15 KST, 서버 포트 없음).

## 12. 다음 단계 진입 가능 여부

**진입 가능(승인 조건부).** [GO_NO_GO.md](GO_NO_GO.md): 격리 v11·Oracle Live 경로·Writer 이전/철회·사용량 정확성 READY, RSS·Instagram·YouTube·NAVER 실제 검증 REQUIRES_APPROVAL, 운영 Oracle 전환 BLOCKED. 실제 검증 전에 필요한 것: 요청서 승인(G-OPS-4/5), 수집원별 실제 승인 근거가 담긴 registry(검토자 2인), 같은 자격 증명을 쓰는 다른 실행 주체가 없다는 운영자 확인.

남은 한계(안전 측):

- 제공자 측 실제 사용량은 로컬에서 확인할 수 없다(Instagram은 작업이 실행 직전 최근 검색을 확인).
- RSS는 예산이 없어 요청 1회 상한을 운영 규칙으로 지킨다.
- 경로 A의 검증 사용량은 운영 SQLite에 자동으로 반영되지 않는다(창 종료까지 운영 수집기 정지 또는 경로 B 인계, 절차서 13단계).
- 작업 중 테스트 출력 경로를 잘못 줘서 `D:\c\Users\gjgjs\AppData\Local\Temp\claude\…\scratchpad\a-unit\`(테스트 로그 2개, 민감정보 없음)가 생겼다. 자동 삭제가 안전 정책으로 거부돼 남아 있으니 `D:\c` 폴더를 직접 지워야 한다.

## 13. 수정·추가 파일

| 파일 | 내용 |
| --- | --- |
| `food_trends/cutover.py` | `isolate()`·`isolated()`·격리 표시 상수 |
| `food_trends/__main__.py` | `--isolate-database`, `--isolated-database`, 적재가 설정된 Instagram 계정 포함·`legacy_usage_accounts` 출력 |
| `food_trends/usage_handover.py` | 인계 전후 파일 지문 기록(`_file_state`) |
| `food_trends/reverse_recovery.py` | 복구 기준선이 기록된 인계 사슬 인정(`matches_recorded_handover`) |
| `tests/test_isolated_copy.py` | 격리 사본·경로 보호·CLI·Instagram 이전 회귀(4개) |
| `tests/test_writer_release.py` | 인계 뒤 복구 갱신 회귀(1개 × 대역·MZ_TEST) |
| `tests/rehearse_live_cycle.py` | 격리 사본 + MZ_TEST 전 과정 리허설 |
| `tests/audit_source_readiness.py` | 수집원별 준비도 점검(읽기 전용) |
| `reports/development-oracle-live-verification-ready-2026-11-06/` | 이 보고서, SOURCE_READINESS, ISOLATED_CUTOVER_REHEARSAL, LIVE_PILOT_RUNBOOK, GO_NO_GO, 근거 JSON |
| `reports/development-oracle-live-integration-2026-11-05/LIVE_VERIFICATION_REQUEST.md` | 실행 준비 판정·절차 갱신 |
| `README.md` | 현황·명령·보고서 연결·테스트 수 |

## 14. Git Commit

작업 브랜치 `feat/oracle-live-verification-ready`(원격 push 완료), 단계마다 커밋:

| 커밋 | 내용 |
| --- | --- |
| `93d3e24` | feat: 운영 DB의 격리·표시 v11 사본(`--isolate-database`/`--isolated-database`, F-1) |
| `86c8209` | fix: 오래된 레거시 기록 뒤 Instagram Writer 이전(F-2), 인계 뒤 복구 갱신(F-3) |
| `707b484` | test: 격리 사본 + MZ_TEST 전 과정 리허설과 근거 |
| `3cac9d7` | test: 수집원별 준비도 점검과 근거 |
| 이 보고서 커밋 | docs: 보고서·절차서·판정·요청서·README |

## 통합 기록

통합 직전 다시 가져온 `origin/main`은 기준 `98a2e24` 그대로였다. `main`을 `98a2e24`에서 `bb0eaf9`(보고 커밋, 위 작업 커밋 4개 포함)로 fast-forward하고 `origin/main`에 push했다. 이 기록 커밋도 같은 방식으로 `main`에 fast-forward했다. 강제 push·reset hard·강제 worktree 삭제·기존 작업 삭제는 없었다.
