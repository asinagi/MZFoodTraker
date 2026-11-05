# 후속 요청서 — Oracle Live 수집 경로의 실제 API 소량 검증

작성 2026-11-05, ORACLE-LIVE-INTEGRATION-1. **이 문서는 승인 요청 초안이다. 승인 전에는 어떤 실제 외부 요청도 보내지 않는다.** 이번 작업은 Live 경로를 구현하고 가짜 전송으로만 검증했다(실제 외부 호출 0건).

> **현재 상태: 시작 불가(BLOCKED).** Writer 이전을 안전하게 되돌리는 기능(철회)이 아직 없다. 실제 Live는 소량 검증이라도 수집원의 단독 Writer를 Oracle로 넘겨야 하므로, 철회·사용량 인계·감사 기록이 구현·검증되기 전에는 이 요청을 승인·실행하지 않는다(요구사항: [REPORT.md §16](REPORT.md)).

## 1. 목적

기존 수집기(GraphClient·YouTubeClient·NaverClient·RSS collector)를 Oracle 런타임에 연결한 Live 경로가 실제 제공자 응답에서도 같은 계약(origin `live_api`, 예산 선예약, 정책별 저장 제한, 오류 시 차단·재시도)으로 동작하는지 최소 요청으로 확인한다.

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
| G-OPS-4/5 수집원별 Writer 이전 | 검증할 수집원의 기존 SQLite 수집기를 멈추고(`INSTAGRAM_COLLECTION_ENABLED`·`YOUTUBE_COLLECTION_ENABLED`·`NAVER_DATALAB_ENABLED`=false, SQLite 서버의 RSS 스케줄러 미실행) Oracle 런타임을 단독 Writer로 기록(`--writer-transfer`) |
| 실제 자격 증명 사용 | 운영자가 로컬 `.env` 또는 프로세스 환경에만 입력. 저장소·보고서·로그에 값 기록 금지 |
| 검토된 registry | 수집원별 `permission_verified`·`policy_allowed`·`approved_by`·`evidence`(실제 승인 근거) |
| 대상 Oracle | 개발용 MZ_TEST(test 환경). 운영 Oracle은 코드에서 계속 거부되므로 이번 검증 범위가 아님 |
| 선행 개발(승인 전 필수) | Writer 이전 철회 명령과 사용량 인계(REPORT §16 R1~R9)의 구현·실제 Oracle 검증 |

## 4. 절차

0. 선행 조건 확인: Writer 이전 철회 기능이 구현·검증됐는지(REPORT §16), 이 요청서의 승인이 있는지. 둘 중 하나라도 없으면 중단.
1. 백업: 운영 SQLite 사본과 MZ_TEST 상태 기록(검증 후 MZ_TEST 초기화).
2. 기존 SQLite 수집기 정지 확인(위 환경 변수, RSS 스케줄러).
3. 사용량 원장 가져오기(15분 이내): `--target-load oracle` 또는 사용량 가져오기만 실행.
4. 환경: `B3_LIVE_ENABLED=true`, `B3_EXCLUSIVE_SOURCES=<검증 수집원>`, `B3_AUTOMATIC_ENABLED=false`.
5. 점검(읽기 전용): `python -m food_trends --backend oracle-test --execution-readiness <registry>` → `ready: true`와 수집원별 차단 사유 없음 확인.
6. Writer 이전: `python -m food_trends --backend oracle-test --writer-transfer <source> --execution-registry <registry>`(수집원마다).
7. Live 관리 서버(자동 실행 없음): `python -m food_trends --backend oracle-test --execution-mode live --execution-registry <registry> --no-schedulers`.
8. 화면 또는 관리 API로 1회씩 실행: RSS `POST /api/collect`; 음식 승인·추적 활성화 후 `POST /api/execution/schedule {"mode":"live"}`, 작업마다 `POST /api/execution/run {"job_id": …}`.
9. 확인: `GET /api/execution/status`(호출 기록·예산 줄·수집원별 상태), B8 `python -m food_trends.app_api --store oracle-test --observation-origin live_api`.

## 5. 성공 기준

- 요청 수가 2절 상한 이내, 각 응답 HTTP 200·파싱 성공
- 모든 회차·스테이징·원문 행의 origin `live_api`, Mock 데이터와 섞이지 않음
- NAVER 값·본문 미저장, YouTube 언급 미생성, Instagram 캡션 창 30일
- 예산: 예약·청구 기록이 실제 요청 수와 일치, 상한 초과 시 요청 없이 대기
- 토큰·키·원본 피드 본문이 대상·로그·보고서에 없음

## 6. 중단과 되돌리기

- 오류·이상 응답: 서버 정지(인증 오류는 자동으로 계정 차단 기록). 401/403은 자격 증명·권한 재검토 전 재시도하지 않음.
- Writer 되돌리기: **전용 철회 명령이 아직 없어 이 요청은 BLOCKED다.** 철회 기능이 생기면 순서는 철회(그 계정의 Oracle 요청 즉시 차단) → 진행 중 작업 종료 확인 → 사용량 인계 → 필요 시 역방향 복구 → 기존 SQLite 수집기 재개다. 대상 행을 손으로 고치는 방식은 쓰지 않는다.
- 데이터 되돌리기: 필요 시 `--recovery plan|apply`로 Oracle 기간의 기록을 SQLite로 복구(RSS 발굴 포함, 원본 피드 본문은 손실 목록).

## 7. 보고 항목

수집원별 요청 수·응답 코드·오류 종류, 저장된 행 수(원문·스테이징·언급·지표), 예산 사용량, 차단·재시도 기록, 민감정보 검사 결과, 검증 후 MZ_TEST 초기화 여부.
