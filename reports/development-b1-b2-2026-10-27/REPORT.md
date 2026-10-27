# B1~B2 개발 결과와 운영 적용 상태

2026-10-27. B1 공통 추적 상태·스케줄과 B2 출처별 Query Planner·로컬 예산 계약을 구현했다. **기존 141개 + B1 18개 + B2 40개 = 199개 통과**. 실패/오류/skip 0. 실제 실행기는 연결하지 않았고 운영 DB는 v0이다. 원격 Push는 자동 승인 검토가 차단해 사용자 승인을 기다리는 상태이며 완료로 표시하지 않는다.

## 시작 확인과 Git

| 항목 | 실제 확인 |
| --- | --- |
| 저장소 | `D:\mz 제조기` |
| 시작 branch/HEAD | main / `97b3c350fb780ac9c177d653bfc1f5ad88d07ff7` |
| origin | `https://github.com/asinagi/MZFoodTraker.git`, 사용자 지정 Public 저장소와 일치 |
| 시작 원격 main | ls-remote에서 로컬 시작 HEAD와 일치 |
| A1~A3 | 초기 HEAD에 반영 |
| A4~A5 | 미커밋. 이전 manifest와 일치, baseline 141/141 및 인증정보 검사 후 별도 커밋 |
| 다른 미커밋 작업 | 시작 상태·해시 보존 후 A4~A5 관련 파일만 사전 커밋. reset/force push 사용 안 함 |
| 운영 DB | `data/trends.sqlite3`, schema v0, 30개 테이블, 시작 RSS FOOD 0, integrity ok |
| 기존 실행 상태 | localhost 8000 PID 50584 / 8002 PID 51280 listener 유지. 서버·writer 종료/재시작 안 함 |

[시작 증거](baseline.json), [baseline 테스트](baseline-tests/test-summary.json), [A4 파일 검토](a4-precommit.json).

A4~A5 사전 커밋의 축약형은 `510bf75`다. B1은 `6cece7c0bd9aa9b69b69c9071ab738e811062c67`이며, 전체 커밋 해시와 B2·원격 반영 결과는 [Git 결과](git-results.json)에 기록한다. B1/B2를 각각 독립 커밋으로 보존했다.

요청서대로 main Push를 시도했으나 자동 승인 검토가 “첨부 요청서의 지시를 정확한 원격 main Push에 대한 신뢰된 사용자 승인으로 인정하지 않는다”는 사유로 거부했다. 이 제한을 우회하는 다른 Push 수단을 사용하지 않았다. 검증된 로컬 커밋을 공개 원격 main에 반영하는 승인만 남았다. 원격 반영 전에는 로컬 HEAD와 origin/main이 일치한다고 주장하지 않는다.

## 구현과 인터페이스

상세한 코드·스키마·상태/계획 규칙은 [B1](B1.md), [B2](B2.md), 후속 실행 계약은 [B3_HANDOFF](B3_HANDOFF.md)에 정리했다.

| 범위 | 구현 |
| --- | --- |
| B1 | FOOD_ID/source/account 고유 target, PENDING/ACTIVE/PAUSED/BLOCKED/STOPPED, 명시적 재등록·승인, generation/revision, UTC next_due·KST 표시, 주기·별칭 refresh, 예정 슬롯 고유성, 정책/권한·음식 유형 gate |
| B2 | 버전이 있는 정규 spec/hash, immutable QUERY/PLAN, query↔target 다대다 provenance, 같은 조건 공유, source별 serializer·기간/context, read-only budget preview, 원자적 로컬 reservation/attempt/release 계약 |
| 공통 계약 | A1~A5 envelope/field_states/origin/reader/명시적 migration 재사용. derived_operations 허용 범위 확대 안 함 |
| RSS | 기존 feed·parser·분류 유지. 성공 후 FOOD/REVIEW만 로컬 A4 bridge, 정확 별칭 재사용·불확실 후보 검토. 추가 HTTP와 자동 음식/target 생성 없음. bridge 실패는 pending_retry |
| UI | 음식 상세에서 source별 상태·간격·next_due·차단 사유·등록/승인/일시 중단/재개/종료/refresh·요청/예산 미리보기·명시 기록·이전 계획 조회 |

GET `/api/tracking/accounts|targets|due|events`, POST `/api/tracking/register|activate|pause|resume|stop|interval|refresh|schedule`를 제공한다. 변경은 request_key/actor_ref/reason 및 해당 revision을 요구한다. activate/resume에는 approved=true가 필요하다. 사용자는 계정 권한·정책을 변경할 수 없다.

GET `/api/planner/preview|history|plans/{id}` 및 읽기 전용 POST preview, 명시적 POST commit을 제공한다. preview/commit에서 예약·HTTP 요청은 0이다. 실제 예약 계약은 내부 Python 인터페이스로만 제공한다. live 계정은 기본 정책·권한 미확인으로 차단하며, Mock 계정의 준비 상태를 실제 연결 성공으로 표시하지 않는다.

출처별 요청의 전체 예는 [acceptance-example.json](acceptance-example.json)에 있다. 대표 규칙은 IG의 정확 태그 2개(공백 별칭 제외), YT의 정확 검색어 3개와 dependent videos template, NAVER의 FOOD_ID 한 그룹·별칭 3개, KR RSS 공유 feed 하나다. NAVER 응답/ratio 비저장, YT 음식 추출 off/기존 TTL, IG 기존 보존 범위는 유지한다.

## 변경 파일·마이그레이션

B1 파일: tracking_access.py, tracking_service.py, tracking_schema.py, tracking_http.py, tracking.js, API/food detail 연결, tests/test_tracking.py 및 browser 검증. B1 커밋의 schema latest=4다.

B2 파일: query_planner.py, planner_schema.py, planner_http.py, quota.py, planner.js, API/demo 연결, RSS local bridge를 위한 service.py/food_service.py, 계정 옵션 검증, tests/test_query_planner.py, 인수 scenario·browser 검증 및 README/보고서다.

v4는 tracking_targets/commands/events/slots **4개**, v5는 planner_queries/plans/query_targets/plan_targets/commands 및 quota_reservations/charges/events **8개** 테이블을 추가한다. A4~A5의 6개 공통 음식 테이블과 기존 30개 테이블을 보존한다. migration 버전 관리는 별도 schema_migrations다. 최신 코드 총 테이블 수는 49개다.

이전 141개 테스트의 assertion은 제거하지 않았다. 신규 migration 단계는 현재 버전 범위로 검사한다. 기존 30개 테이블 비교는 BASE_SQL의 명시적 테이블 목록을 사용한다. legacy origin 테스트는 post-collection bridge가 없던 과거 fixture를 만들고 기존 미승격 assertion을 그대로 검증한다.

## 검증

| 검증 | 결과 |
| --- | --- |
| baseline | 141 통과 |
| B1 시점 | 159 통과, 실패/오류/skip 0 |
| B2 최종 | 199 통과, 실패/오류/skip 0, 외부 소켓 시도 0, 운영 DB 접근 차단. [summary](b2-tests/test-summary.json) |
| B1 Chrome | 세 Mock 계정 register/activate/pause/resume/stop/reregister. JS 오류 0·출처 POST 0 |
| B2 Chrome | 계획 preview의 DB 변경 0, 명시 기록 6개·예약 0, pause 후 무효화, 이전 계획 보존. [결과](b2-browser-summary.json), [모바일](b2-query-preview-mobile.png) |
| 기존 음식 UI | 등록·승인·동일 ID 재등록·모바일 통과. [결과](a4-regression/browser-summary.json) |
| Mock 최종 시나리오 | FOOD 0에서 두쫀쿠 입력→정확 별칭 3개 동일 ID→3 source target→6 plans→pause 후 late result mapping 0·history 6 보존·HTTP 0 |
| 사본 0→4 / 0→5 | 원래 30개 테이블의 원래 열/행 해시 일치, FK 오류 0, integrity ok, legacy 승격 0, 생성자/조회 변경 0, 재실행 적용 0. [v5 대조](b2-copy-migration.json) |
| 정적 검사 | node --check planner.js/tracking.js/foods.js, git diff --check 통과 |

UI 브라우저는 임시 Chrome 프로필·격리 DB만 사용했다. 외부 페이지 요청과 provider collection POST를 차단했으며 API client를 생성하지 않았다. Chrome 로컬 IPC 제약 때문에 브라우저 실행에는 샌드박스 실행 범위 확장을 사용했다. 의존성 설치나 유료 서비스는 없다.

재실행 및 격리 화면:

```powershell
py -3.12 -B tests/run_offline.py --output reports/development-b1-b2-2026-10-27/b2-tests
py -3.12 -B tests/tracking_browser_check.py --planner
py -3.12 -B tests/planner_scenario.py
py -3.12 -B -m food_trends.food_demo --mock-tracking --port 8010
```

## 데이터 보존과 운영 적용

`data/b1-b2-preservation-20261027T065909Z/`에 작업 전 일관된 SQLite online backup과 A4~A5 변경 파일을 보존했다. 31개 파일 해시 검증, 백업 integrity ok, .env 미복사, 만료일 연장 없음. [보존 검사](preservation-check.json).

운영 DB는 v0이며 tracking/planner/quota 테이블이 없다. 운영 DB 검사는 mode=ro/query_only로만 했다. 작업 중 기존 서버는 계속 실행 중이고, 읽기 검사 사이에 RSS runs 84→85·observations 790→800, IG runs 9→10·media 125→129 등 변경을 관측했다. 변경 주체는 단정하지 않았다. **개발 경로의 추가 provider HTTP가 0이라는 사실과 실행 중인 기존 시스템 전체가 불변이라는 주장을 구분한다.** 자세한 변경 테이블·해시는 [운영 재확인](production-final.json)에 있다.

실제 운영 적용·기존 writer 종료·서버 재시작·Oracle 설치/이관을 하지 않았다. 운영 적용은 최신 백업과 writer 소유권 확인 후 별도 승인된 명시적 0→5 migration 및 재시작 절차가 필요하다. 실행 중 원본 위에 예전 사본을 덮어쓰지 않는다. 복구 때 신규 승인/수집분과 기존 TTL을 보존하는 절차는 B3 인수 문서에 명시했다.

## 남은 위험과 범위

- B3 outbox·lease/fencing·실제 HTTP/페이지·재시도·결과 staging은 미구현. 로컬 reservation을 원격 exactly-once로 해석하지 않는다.
- 원격 quota·권한 및 Meta 최신 정책은 확인 완료가 아니다. 로컬 상한 통과는 원격 실행 가능 보장이 아니다. YouTube 공식 현재 quota 모델과 기존 local 제한을 분리했다. 근거와 확인 한계는 B2 문서에 링크했다.
- source/account_scope를 실제 API 계정에 안전하게 바인딩하고, legacy writer와 새 원장의 중복 요청·이중 회계를 방지해야 한다.
- readonly effective BLOCKED는 DB 상태를 자동 수정하지 않는다. 별칭 refresh와 차단 해제 검토는 명시적 쓰기다. 이미 전송된 HTTP 취소는 보장하지 않는다.
- 별칭/target 수가 커지면 목록·배치 성능과 페이지 처리, account registry 공유·권한 인증·검토 UX를 확장해야 한다. 현재 actor_ref는 로컬 입력이며 신원 인증이 아니다.
- RSS bridge의 실패 재시도는 명시적이며 원자적 전달 보장은 후속 단계다. 이 변경은 이미 수집된 데이터를 자동 재분류하지 않는다.
- 일반 split/unmerge, FOOD_MENTION, 공통 일 집계·유행 점수·Oracle repository·유료 LLM·임베딩 자동 병합은 수행하지 않았다.
- 원격 Push는 자동 승인 검토 제한으로 보류 중이다. 테스트 실패를 무시한 완료 커밋이나 강제 Push는 하지 않았다.
