# 후속 요청서 — Oracle Live 수집 경로의 실제 API 소량 검증

작성 2026-11-05, ORACLE-LIVE-INTEGRATION-1. 갱신 2026-11-05, ORACLE-WRITER-RELEASE-1. 갱신 2026-11-07, ORACLE-LIVE-VERIFICATION-READY-1. **이 문서는 승인 요청이다. 승인 전에는 어떤 실제 외부 요청도 보내지 않는다.** Live 경로는 구현했고 가짜 전송으로만 검증했다(실제 외부 호출 0건).

> **현재 상태: 승인 대기(REQUIRES_APPROVAL), 실행 준비 완료.** 선행 개발 조건 R1~R9(ORACLE-WRITER-RELEASE-1)에 더해, 운영 DB 격리 사본으로 전 과정을 개발용 Oracle(MZ_TEST)에서 리허설했고 그때 드러난 실행 차단 결함 3건을 고쳤다(아래 0-2절, [준비 보고서](../development-oracle-live-verification-ready-2026-11-06/REPORT.md)). 실제 요청은 이 요청서의 승인(G-OPS-4/5)과 수집원별 실제 승인 근거가 담긴 registry 뒤에만, [실행 절차서](../development-oracle-live-verification-ready-2026-11-06/LIVE_PILOT_RUNBOOK.md) 순서대로 보낸다. 운영 Oracle은 계속 범위 밖이다.

## 0. 선행 개발 조건 판정

### 0-1. Writer 철회 (ORACLE-WRITER-RELEASE-1)

| # | 요구사항 | 판정 | 구현·검증 |
| --- | --- | --- | --- |
| R1 | 명시적 명령, registry 승인 근거, 운영·`oracle-production`·Shadow 소유 거부 | 해결 | `--backend oracle-test --writer-release <source> --execution-registry <registry>`. 승인 없으면 403, 운영 대상 403·소유권 거부, Shadow 소유 중 거부 |
| R2 | 실행 직후 그 계정의 큐 등록·작업 시작·요청 차단 | 해결 | 첫 단계가 계정 행 잠금 아래 철회 표시를 커밋. 큐 등록·요청 직전 게이트가 같은 잠금 아래에서 읽어 즉시 거부(경합 테스트 포함). RSS는 RSS 잠금 아래 회차 시작 전 재확인 |
| R3 | 실행 중 Lease는 거부 또는 대기, 대기 작업은 사유를 남겨 종료, 커밋된 데이터 유지 | 해결 | 살아 있는 Lease가 있으면 `DRAINING`(다시 실행 또는 `--release-wait`). 이미 보낸 요청은 결과를 저장하고 다음 요청 전에 멈춤. 남은 작업은 `CANCELLED`/`WRITER_RELEASED`. 만료된 Lease의 `SENT` 호출은 `UNKNOWN`으로 계속 집계 |
| R4 | 사용량 인계, 또는 창이 끝날 때까지 레거시 재개 차단 | 해결 | `--writer-handover`가 Oracle 기간 사용량을 `youtube_calls`·`naver_calls`·`instagram_searches`에 기록해 기존 예산 코드가 그대로 셈. 인계 전·인계 불가 시 `--legacy-resume-check`가 창 종료 시각(`resume_not_before`)까지 거부 |
| R5 | `IMPORTED`로 복귀(근거 `writer_release:…`), 지워지지 않는 이력 | 해결 | `quota_accounts` `IMPORTED`·`writer_release:<승인자>`·`release_requested_at`, 대상 v10 `writer_transfer_events`(추가 전용) |
| R6 | 레거시 재활성화는 철회 후에만, 점검 명령으로 확인 | 해결 | `--legacy-resume-check`(읽기 전용): 철회 완료·사용량 인계·차단 없음·RSS 복구·SQLite 소유권을 모두 확인. 실제 재활성화는 운영자가 판정 후 수행 |
| R7 | 멱등, 중간 실패 후 재실행으로 수렴 | 해결 | 철회·인계 모두 재실행 시 변경 0, 이벤트 0. 중간 실패 후 재실행으로 완료(테스트) |
| R8 | 데이터 반환은 기존 역방향 복구, 순서 명시 | 해결 | 철회 → 진행 중 작업 종료 확인 → 사용량 인계 → `--recovery plan\|apply` → 재개 판정(아래 6절) |
| R9 | 거부·대기, 철회 직후 요청 0, 인계 후 레거시 예산, 이중 철회, 운영 거부, 실제 Oracle 동일 결과 | 해결 | `tests/test_writer_release.py` 11개 사례 × (대역 + MZ_TEST) |

### 0-2. 실행 준비 (ORACLE-LIVE-VERIFICATION-READY-1)

| # | 조건 | 판정 | 구현·검증 |
| --- | --- | --- | --- |
| V1 | 운영 SQLite(v0)를 바꾸지 않는 v11 검증 환경 | 해결 | `--isolate-database <사본>`(읽기 전용 백업 → 격리 표시 → 사본만 v11·스테이징·보존 정리), `--isolated-database <사본>`(적재·인계·복구·재개 판정; 운영 경로·표시 없는 파일 거부, 운영 승인 스위치 불필요) |
| V2 | 실제 계정으로 Writer 이전이 가능해야 함 | **결함 수정** | 운영 DB처럼 마지막 Instagram 검색이 7일보다 오래되면 `--target-load`가 그 계정을 사용량 확인 계정으로 기록하지 않아 `--writer-transfer instagram_graph`가 영원히 거부됐다. 이제 설정된 Instagram 계정도 기록한다 |
| V3 | 인계 뒤 복구가 Oracle 기간 변경을 빠짐없이 돌려줌 | **결함 수정** | 인계가 SQLite 파일을 바꾸면 복구가 mutable 갱신을 승인하지 않은 채 `converged`를 보고했다(재관측한 RSS 항목의 갱신 3건 누락). 이제 인계가 파일 지문을 앞뒤로 기록하고 복구 기준선이 그 사슬을 인정한다 |
| V4 | 요청 상한을 코드가 강제 | 해결 | 아래 2절 설정에서 예산이 상한을 막는다. 리허설: 요청 7회(상한 9), 같은 작업을 다시 실행해도 추가 요청 0 |
| V5 | 수집원별 준비도 확인 | 해결 | `tests/audit_source_readiness.py`(읽기 전용·외부 요청 0): 4개 수집원 모두 기술 차단 없음, `REQUIRES_APPROVAL`([SOURCE_READINESS](../development-oracle-live-verification-ready-2026-11-06/SOURCE_READINESS.md)) |

**판정: 실제 API 소량 검증의 선행 개발 조건은 모두 해결됐다.** 남은 것은 승인(G-OPS-4/5)과 실제 승인 근거뿐이다.

## 1. 목적

기존 수집기(GraphClient·YouTubeClient·NaverClient·RSS collector)를 Oracle 런타임에 연결한 Live 경로가 실제 제공자 응답에서도 같은 계약(origin `live_api`, 예산 선예약, 정책별 저장 제한, 오류 시 차단·재시도)으로 동작하는지 최소 요청으로 확인한다. 검증이 끝나면 Writer를 안전하게 되돌린다.

## 2. 대상과 요청 상한

| 수집원 | 보낼 요청 | 최대 요청 수 | 상한을 강제하는 설정 | 대상에 남는 것 | 제공자 사용량 |
| --- | --- | --- | --- | --- | --- |
| Google Trends RSS | 피드 1회 | 1 | 예산 없음: `/api/collect` 1회(운영자) | 항목·분류·회차별 관측. 피드 전체 본문은 보관하지 않음 | 없음(공개 피드) |
| Instagram Graph | 최근 검색 해시태그 확인(최대 3페이지), 해시태그 조회 1, 최근 게시물 1페이지 | 5 | `INSTAGRAM_MAX_PAGES=1`, `INSTAGRAM_SEARCH_SOFT_LIMIT=1`(두 번째 새 태그는 요청 0으로 대기) | 게시물 메타데이터, 캡션(30일 창), 해시태그 | 7일 고유 태그 1개 |
| YouTube Data API | search 1, videos 1 | 2 | `YOUTUBE_DAILY_SEARCH_LIMIT=1`, `YOUTUBE_DAILY_VIDEO_LIMIT=1`, `YOUTUBE_MAX_PAGES=1` | 공개 영상 스냅샷(29일 만료) | 101 units(search 100·videos 1) |
| NAVER DataLab | 검색어 트렌드 1 | 1 | `NAVER_DATALAB_DAILY_LIMIT=1` | 요청 메타데이터만(값·본문 저장 안 함) | 일일 호출 1회(KST) |
| 합계 | | **9** | 리허설 실측 7 | | |

이 수치는 허용된 호출 수가 아니라 최대 계획치다. 수집원마다 사전 점검·사용량·권한 확인이 통과하지 않으면 요청을 보내지 않는다.

## 3. 승인이 필요한 항목

| 항목 | 내용 |
| --- | --- |
| G-OPS-4/5 수집원별 Writer 이전·Live 검증 | 검증할 수집원의 기존 SQLite 수집기 정지(`INSTAGRAM_COLLECTION_ENABLED`·`YOUTUBE_COLLECTION_ENABLED`·`NAVER_DATALAB_ENABLED`=false; 운영 DB가 v0라 SQLite 서버는 어차피 시작 불가) 상태에서 Oracle 런타임을 단독 Writer로 기록(`--writer-transfer`), 검증 후 철회(`--writer-release`)까지 같은 승인 범위 |
| 실제 자격 증명 사용 | 기존 `.env` 값(프로세스 변수가 우선). 저장소·보고서·로그에 값 기록 금지 |
| 검토된 registry | 수집원별 `permission_verified`·`policy_allowed`·`approved_by`·`evidence`(실제 승인 근거, 두 번째 사람이 검토). 이전과 철회 모두 같은 registry 승인을 요구 |
| 대상 Oracle | 개발용 MZ_TEST(test 환경, 대상 스키마 v10). `ORACLE_TEST_*`와 `ORACLE_*`(`ORACLE_ENVIRONMENT=test`) 두 접두어 모두 MZ_TEST를 가리켜야 한다. 운영 Oracle은 코드에서 계속 거부 |
| 경로 | **경로 A(격리 v11 사본, G-OPS-3 불필요)를 요청한다.** 경로 B(G-OPS-3 운영 마이그레이션 뒤 운영 DB 사용)는 운영 전환 단계용 |
| 선행 개발 | **해결**(0절). 추가 개발 없이 승인만 남음 |

## 4. 절차

명령 전체는 [실행 절차서](../development-oracle-live-verification-ready-2026-11-06/LIVE_PILOT_RUNBOOK.md)에 있다(모두 현재 CLI 옵션이며 MZ_TEST에서 리허설함).

0. 시작 조건: 이 요청서 승인, 승인된 `main` 커밋, 운영 DB v0 그대로, 서버 없음.
1. 환경: Oracle 두 접두어(MZ_TEST), `B3_LIVE_ENABLED=true`, `B3_EXCLUSIVE_SOURCES=<검증 수집원>`, `B3_AUTOMATIC_ENABLED=false`, 기존 수집기 스위치 false, 2절의 상한 설정.
2. registry 작성·검토(Git 밖 `data/ops-pilot/`).
3. 사전 점검(읽기 전용): `tests/audit_source_readiness.py --registry <registry> --oracle` — 차단 사유·설정 불일치 0.
4. 격리 사본 `--isolate-database`, 대상 `--target-migrate oracle`, 적재와 사용량 가져오기 `--target-load oracle --isolated-database <사본>`.
5. 15분 안에 `--execution-readiness`(이전 전) → 수집원마다 `--writer-transfer` → `--writer-status`(ORACLE) → `--execution-readiness`(`ready: true`).
6. Live 관리 서버(예약·자동 실행 없음): `--backend oracle-test --execution-mode live --execution-registry <registry> --no-schedulers`.
7. 관리 API로 1회씩: RSS `POST /api/collect`; 음식 1개 승인·수집원별 추적 1개, `POST /api/planner/commit`, `POST /api/execution/schedule {"mode":"live"}`, 작업마다 `POST /api/execution/run {"job_id": …}` 한 번.
8. 요청 수 확인: 작업별 `GET /api/execution/jobs/<id>`의 `calls`(제공자 요청 1회 = 1행), RSS 회차 수, `--writer-status`, B8 `python -m food_trends.app_api --store oracle-test --observation-origin live_api`.
9. 되돌리기(6절).

## 5. 성공 기준

- 요청 수가 2절 상한 이내, 각 응답 HTTP 200·파싱 성공, 실행 호출 기록이 실제 요청 수와 일치
- 모든 회차·스테이징·원문 행의 origin `live_api`, Mock 데이터와 섞이지 않음
- NAVER 값·본문 미저장, YouTube 언급 미생성, Instagram 캡션 창 30일
- 예산: 예약·청구 기록이 실제 요청 수와 일치, 상한 초과 시 요청 없이 대기
- 토큰·키·원본 피드 본문이 대상·로그·보고서에 없음
- 철회 후: 그 수집원의 요청 0, 인계로 기존 예산 코드가 검증 사용량을 셈, 복구 `converged`·`updates_applied`·남은 갱신 0, 재개 판정 `ready: true`

## 6. 중단과 되돌리기

- 오류·이상 응답: 서버 정지(`Ctrl+C`; 예약·자동 실행 없음). 401/403은 자동으로 계정 차단 기록, 자격 증명·권한 재검토 전 재시도하지 않음. 429는 차단 기간까지 재시도하지 않음. 새 프로세스의 Live는 `B3_LIVE_ENABLED=false`로 닫힌다.
- Writer 되돌리기(수집원마다, 이 순서로):
  1. `python -m food_trends --backend oracle-test --writer-release <source> --execution-registry <registry> [--release-wait 120]` — 새 요청 즉시 중단. 진행 중 요청이 있으면 `DRAINING`(종료 코드 2): 다시 실행하거나 기다린다. 끝나면 `RELEASED`, 남은 작업은 `WRITER_RELEASED`로 종료.
  2. `python -m food_trends --writer-handover <source> --recovery-target oracle --isolated-database <사본>` — Oracle 기간의 열린 창 사용량을 격리 사본의 레거시 호출 기록으로 인계(재실행 0건). 출력의 `open_usage.open_until`을 기록한다.
  3. `python -m food_trends --recovery plan --recovery-target oracle --isolated-database <사본>` → `--recovery apply ... --apply-updates` — `converged`·`updates_applied`·남은 `mutable_updates` 0. 인계가 먼저 파일을 바꿔도 인계가 남긴 지문으로 기준선이 확인된다. 원본 피드 본문 등은 설계상 손실 목록.
  4. `python -m food_trends --legacy-resume-check <source> --recovery-target oracle --isolated-database <사본>` — 경로 A에서는 격리 사본 판정이다. 운영 SQLite 수집기는 켜지 않는다(v0라 시작 불가).
- **검증 사용량의 운영 반영**: 운영 SQLite는 경로 A 검증의 사용량을 모른다. 운영 수집기는 G-OPS-3 이후에도 가장 늦은 `open_until`(Instagram 최대 7일)이 지나기 전에는 경로 B 인계(`--allow-operational-database`) 없이 켜지 않고, 그때까지 MZ_TEST를 초기화하지 않는다.
- 대상 행을 손으로 고치는 방식은 쓰지 않는다. 상태와 이력은 `--backend oracle-test --writer-status`로 확인한다.

## 7. 보고 항목

수집원별 요청 수·응답 코드·오류 종류, 저장된 행 수(원문·스테이징·언급·지표), 예산 사용량, 차단·재시도 기록, 철회 결과(`--writer-status`의 이력), 인계 건수·`open_until`과 재개 판정, 복구 결과, 민감정보 검사 결과, 격리 사본 삭제와 MZ_TEST 초기화 시각.
