# 후속 요청서 — Oracle Live 수집 경로의 실제 API 소량 검증

작성 2026-11-05, ORACLE-LIVE-INTEGRATION-1. 갱신 2026-11-05, ORACLE-WRITER-RELEASE-1. **이 문서는 승인 요청 초안이다. 승인 전에는 어떤 실제 외부 요청도 보내지 않는다.** Live 경로는 구현했지만 가짜 전송으로만 검증했다(실제 외부 호출 0건).

> **현재 상태: 승인 대기(REQUIRES_APPROVAL).** 직전 판정의 BLOCKED 사유였던 "Writer 이전을 안전하게 되돌리는 기능(철회)"을 ORACLE-WRITER-RELEASE-1에서 구현하고 실제 Oracle(MZ_TEST)에서 검증했다. 선행 개발 조건 R1~R9는 모두 해결됐다(아래 판정표, [보고서](../development-oracle-writer-release-2026-11-05/REPORT.md)). 실제 요청은 여전히 이 요청서의 승인(G-OPS-4/5)과 실제 자격 증명·검토된 registry 뒤에만 보낸다. 운영 Oracle은 계속 범위 밖이다.

## 0. 선행 개발 조건 판정 (ORACLE-WRITER-RELEASE-1)

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
| R9 | 거부·대기, 철회 직후 요청 0, 인계 후 레거시 예산, 이중 철회, 운영 거부, 실제 Oracle 동일 결과 | 해결 | `tests/test_writer_release.py` 10개 사례 × (대역 + MZ_TEST) |

## 1. 목적

기존 수집기(GraphClient·YouTubeClient·NaverClient·RSS collector)를 Oracle 런타임에 연결한 Live 경로가 실제 제공자 응답에서도 같은 계약(origin `live_api`, 예산 선예약, 정책별 저장 제한, 오류 시 차단·재시도)으로 동작하는지 최소 요청으로 확인한다. 검증이 끝나면 Writer를 기존 SQLite 수집기로 안전하게 되돌린다.

## 2. 대상과 요청 상한

| 수집원 | 보낼 요청 | 최대 요청 수 | 대상에 남는 것 | 제공자 사용량 |
| --- | --- | --- | --- | --- |
| Google Trends RSS | 피드 1회 | 1 | 항목·분류·회차별 관측(각 항목의 원래 XML 조각 포함). 피드 전체 본문은 보관하지 않음 | 없음(공개 피드) |
| Instagram Graph | 최근 검색 해시태그 확인(최대 3페이지), 해시태그 조회 1, 최근 게시물 1페이지 | 5 | 게시물 메타데이터, 캡션(30일 창), 해시태그 | 7일 고유 태그 1개 |
| YouTube Data API | search 1, videos 1 | 2 | 공개 영상 스냅샷(29일 만료) | search 1회·videos 1회(로컬 일일 상한 안) |
| NAVER DataLab | 검색어 트렌드 1 | 1 | 요청 메타데이터만(값·본문 저장 안 함) | 일일 호출 1회(KST) |

## 3. 승인이 필요한 항목

| 항목 | 내용 |
| --- | --- |
| G-OPS-4/5 수집원별 Writer 이전 | 검증할 수집원의 기존 SQLite 수집기를 멈추고(`INSTAGRAM_COLLECTION_ENABLED`·`YOUTUBE_COLLECTION_ENABLED`·`NAVER_DATALAB_ENABLED`=false, SQLite 서버의 RSS 스케줄러 미실행) Oracle 런타임을 단독 Writer로 기록(`--writer-transfer`). 검증 후 철회(`--writer-release`)까지 같은 승인 범위 |
| 실제 자격 증명 사용 | 운영자가 로컬 `.env` 또는 프로세스 환경에만 입력. 저장소·보고서·로그에 값 기록 금지 |
| 검토된 registry | 수집원별 `permission_verified`·`policy_allowed`·`approved_by`·`evidence`(실제 승인 근거). 이전과 철회 모두 같은 registry 승인을 요구 |
| 대상 Oracle | 개발용 MZ_TEST(test 환경, 대상 스키마 v10 — 필요 시 `--target-migrate oracle`). 운영 Oracle은 코드에서 계속 거부되므로 이번 검증 범위가 아님 |
| 선행 개발 | **해결**: ORACLE-WRITER-RELEASE-1(0절). 추가 개발 없이 승인만 남음 |

## 4. 절차

0. 선행 조건 확인: 이 요청서의 승인, 대상 스키마 v10(`--target-status`). 없으면 중단.
1. 백업: 운영 SQLite 사본과 MZ_TEST 상태 기록(검증 후 MZ_TEST 초기화).
2. 기존 SQLite 수집기 정지 확인(위 환경 변수, RSS 스케줄러).
3. 사용량 원장 가져오기(15분 이내): `--target-load oracle` 또는 사용량 가져오기만 실행.
4. 환경: `B3_LIVE_ENABLED=true`, `B3_EXCLUSIVE_SOURCES=<검증 수집원>`, `B3_AUTOMATIC_ENABLED=false`.
5. 점검(읽기 전용): `python -m food_trends --backend oracle-test --execution-readiness <registry>` → `ready: true`와 수집원별 차단 사유 없음 확인.
6. Writer 이전: `python -m food_trends --backend oracle-test --writer-transfer <source> --execution-registry <registry>`(수집원마다). `--writer-status`로 `ORACLE` 확인.
7. Live 관리 서버(자동 실행 없음): `python -m food_trends --backend oracle-test --execution-mode live --execution-registry <registry> --no-schedulers`.
8. 화면 또는 관리 API로 1회씩 실행: RSS `POST /api/collect`; 음식 승인·추적 활성화 후 `POST /api/execution/schedule {"mode":"live"}`, 작업마다 `POST /api/execution/run {"job_id": …}`.
9. 확인: `GET /api/execution/status`(호출 기록·예산 줄·수집원별 상태), B8 `python -m food_trends.app_api --store oracle-test --observation-origin live_api`.
10. 되돌리기(6절): 수집원마다 철회 → 사용량 인계 → 필요 시 복구 → 재개 판정이 `ready: true`일 때만 기존 수집기 재개.

## 5. 성공 기준

- 요청 수가 2절 상한 이내, 각 응답 HTTP 200·파싱 성공
- 모든 회차·스테이징·원문 행의 origin `live_api`, Mock 데이터와 섞이지 않음
- NAVER 값·본문 미저장, YouTube 언급 미생성, Instagram 캡션 창 30일
- 예산: 예약·청구 기록이 실제 요청 수와 일치, 상한 초과 시 요청 없이 대기
- 토큰·키·원본 피드 본문이 대상·로그·보고서에 없음
- 철회 후: 그 수집원의 요청 0, 레거시 호출 기록에 Oracle 기간 사용량이 들어가 기존 예산 코드가 셈, 재개 판정 `ready: true`

## 6. 중단과 되돌리기

- 오류·이상 응답: 서버 정지(인증 오류는 자동으로 계정 차단 기록). 401/403은 자격 증명·권한 재검토 전 재시도하지 않음.
- Writer 되돌리기(수집원마다, 이 순서로):
  1. `python -m food_trends --backend oracle-test --writer-release <source> --execution-registry <registry>` — 새 요청 즉시 중단. 진행 중 요청이 있으면 `DRAINING`(종료 코드 2): 다시 실행하거나 `--release-wait 120`. 끝나면 `RELEASED`, 남은 작업은 `WRITER_RELEASED`로 종료.
  2. `python -m food_trends --writer-handover <source> --recovery-target oracle [--allow-operational-database]` — Oracle 기간의 열린 창 사용량을 설정된 SQLite DB의 레거시 호출 기록으로 인계(재실행 시 0건). 운영 DB는 별도 승인 스위치가 있어야만 쓴다.
  3. 필요 시 `--recovery plan` → `--recovery apply --recovery-target oracle`로 Oracle 기간 데이터(RSS 발굴 포함) 복구. 원본 피드 본문은 손실 목록.
  4. `python -m food_trends --legacy-resume-check <source> --recovery-target oracle` — `ready: true`일 때만 해당 `*_ENABLED=true`(RSS는 SQLite 서버 스케줄러)로 재개. 아니면 `blockers`와 `resume_not_before`를 따른다(종료 코드 2).
- 대상 행을 손으로 고치는 방식은 쓰지 않는다. 상태와 이력은 `--backend oracle-test --writer-status`로 확인한다.

## 7. 보고 항목

수집원별 요청 수·응답 코드·오류 종류, 저장된 행 수(원문·스테이징·언급·지표), 예산 사용량, 차단·재시도 기록, 철회 결과(`--writer-status`의 이력), 인계 건수와 재개 판정, 민감정보 검사 결과, 검증 후 MZ_TEST 초기화 여부.
