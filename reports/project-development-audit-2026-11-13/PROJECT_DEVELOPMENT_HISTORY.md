# MZFoodTraker 개발 이력 (시간순, 개발 단위별)

작성일 2026-11-13 KST. 읽기 전용 감사로 작성했다. 코드·DB·설정·기존 파일은 바꾸지 않았고, 외부 API 호출·서버 기동·수집기 실행·테스트 실행은 하지 않았다. 비밀값 파일(`*.secret`, `.env`, 토큰, `data/oracle-dev/`, `data/oracle-prod/`)은 열지 않았다.

## 0. 근거 등급과 읽는 법

각 사실 옆에 근거 종류를 붙였다.

| 표기 | 의미 | 신뢰도 |
| --- | --- | --- |
| **[GIT]** | `git log`·`git show --stat`·브랜치 그래프에서 직접 확인 | 확정 (커밋 시각과 내용) |
| **[DB]** | `data/trends.sqlite3`를 `mode=ro`로 직접 조회한 값 | 확정 (그 DB에 남은 기록 범위) |
| **[EVID]** | 보고서 폴더의 증거 파일(test-summary.json, *-verification.json 등)을 직접 열어 확인 | 증거 파일과 일치함을 확인 (실행 자체를 재현하지는 않음) |
| **[MTIME]** | 파일 수정 시각으로 순서를 추정 | **추정**. git이 생기기 전 단계는 이것만 있음 |
| **[REPORT]** | REPORT.md 등 문서에만 있고 이번에 따로 확인하지 못함 | 보고서 기재 사항. 교차 확인 못 했으면 "미확인"이라고 적음 |

시각은 KST(+09:00) 기준이다. DB 값은 UTC(`Z`)로 저장돼 있어 필요한 곳에 둘 다 적었다.

**순서 확정 범위:** 2026-10-27 15:13(최초 커밋 `97b3c35`) 이후는 git으로 확정된다. 그 이전(2026-10-13 ~ 10-27 오전)은 루트 문서·`data/` 파일 mtime과 운영 DB 행의 타임스탬프로만 재구성했으므로 **추정**이다.

---

## 1. 요약 타임라인

| 기간 | 개발 단계 | 개발 목표 | 실제 구현 방식 | 구현 결과 | 검증 증거 |
| --- | --- | --- | --- | --- | --- |
| 2026-10-13 ~ 10-16 **[MTIME·DB]** | P0. RSS MVP (git 이전) | 한국 Google Trends 급상승 RSS에서 음식 후보 찾기 | Python 표준 라이브러리(`urllib`, `sqlite3`, `http.server`), `collector.py`·`classifier.py`(규칙 기반)·`storage.py`, 정적 HTML UI | RSS 실제 수집·저장·FOOD/REVIEW/NON_FOOD 분류 동작. 첫 회차 2026-10-13T05:28Z | `VERIFICATION.md`(10-13), `verification.json`, DB `runs` 10-13 41회·10-14 7회·10-16 3회 |
| 2026-10-26 **[MTIME·DB]** | P1. 출처 확장과 분류기 감사 (git 이전) | Instagram·NAVER·YouTube 연동, RSS 오분류 수정 | Instagram Graph API, NAVER DataLab, YouTube Data API v3 클라이언트·서비스·저장소, 분류기 v2 | 세 출처 실제 호출 성공. IG 게시물 67건 저장(당일), NAVER 응답 미저장 정책, YouTube 29일 TTL | 루트 `INSTAGRAM_*`·`NAVER_*`·`YOUTUBE_*`·`AUDIT_REPORT.md`, `data/*-verification.json`, DB 첫 행 IG 05:25Z·NAVER 05:22Z·YT 06:03Z |
| 2026-10-26 **[REPORT·MTIME]** | P2. 통합 감사 | 현황 파악, 결함 찾기, Oracle 목표 모델·로드맵 설계 | 읽기 전용 DB 감사, 임시 DB에서 결함 재현 | 후보 REPLACE가 YouTube 연결을 지우는 결함 발견, A1~B8 로드맵 | `reports/integrated-audit-2026-10-26/`, 테스트 97개 통과 **[EVID]** |
| 2026-10-27 오전 ~ 15:13 **[GIT]** | U1. A1~A3 데이터 무결성·공통 계약 | 감사 결함 수정과 공통 기반 | UPSERT, `contracts.py`, 읽기 전용 reader, 명시적 마이그레이션(v1~v2) | git 저장소 생성, 최초 커밋에 포함 | `97b3c35`, 114개 통과 **[EVID]** |
| 2026-10-27 16:02 **[GIT]** | U2. A4~A5 음식 개체·별칭·수동 등록 | 공통 FOOD_ID와 사람 승인 | `entity_schema.py`, `food_service.py`, SQLite v3 | 두쫀쿠 등 별칭이 같은 FOOD_ID로 해석 | `510bf75`, 141개 **[EVID]** |
| 2026-10-27 16:10 ~ 16:45 **[GIT]** | U3. B1~B2 추적 대상·요청 계획 | 음식별 추적 상태와 출처별 요청 계획·예산 | `tracking_service.py`, `query_planner.py`, `quota.py`, v4~v5 | 미리보기·계획 저장(HTTP 0) | `6cece7c` `9839e80` `c159a47`, 199개 **[EVID]** |
| 2026-10-27 17:26 ~ 10-28 12:20 **[GIT]** | U4. B3 신뢰성 있는 실행 | Outbox·Lease/Fencing 실행기 | `execution.py`, `execution_sources.py`, `execution_ownership.py`, v6~v8 | Mock 6개 작업·HTTP 11회 시나리오, 결함 1건 수정 | `cca7ee7` `59376f2` `ea08fe9`, 245개 **[EVID]** |
| 2026-10-28 12:30 ~ 13:05 **[GIT]** | U5. B4~B5 공통 Ingestion·음식 관측 | 4출처 공통 스테이징·품질검사, 음식 언급·지표 | `ingestion.py`, `data_quality.py`, `food_observations.py`, v9~v10 | FOOD_ID 중심 관측 모델 | `051e310` `594f47b` `9f8f703`, 280개 **[EVID]** |
| 2026-10-28 14:20 ~ 11-02 | (공백) | — | — | 운영 SQLite 마지막 쓰기 10-28 14:20, 이후 수집 중단 **[DB·MTIME]** | 커밋 없음 **[GIT]** |
| 2026-11-02 09:42 ~ 10:10 **[GIT]** | U6. B6~B7 Oracle 대상 스키마·이관·Shadow | SQLite→Oracle 이관 준비 | `target_schema.py`, `target_db.py`(python-oracledb Thin), `target_repository.py`, `target_migration.py`, `shadow.py`, SQLite v11 | Oracle 없이 SQLite 대역으로만 검증 | `5303b61`~`8e64830`, 323개(skip 15) **[EVID]** |
| 2026-11-02 오후 **[GIT]** | U7. G-OPS-0 개발용 Oracle | 실제 Oracle에서 B6~B7 검증 | WSL2 + Podman + Oracle AI Database 26ai Free 23.26.3, `MZ_TEST` 스키마 | 실제 Oracle 356개 통과, FK 대조 결함 수정 | `eafef71` `c6403f7` `5793d39`, 356개(skip 0) **[EVID]** |
| 2026-11-02 15:03 ~ 15:28 **[GIT]** | U8. STORAGE-1 + DB 경로 안전화 | 저장 위치 점검, 경로 결정성 | `paths.py`, `--create-database` | 이동 없음, 경로를 프로젝트 루트 기준으로 | `524fc53` `8f293ca` |
| 2026-11-03 **[GIT]** | U9. ORACLE-RUNTIME-1 | B3~B5 서비스를 Oracle에서 실행 | `oracle_execution.py`(`FOR UPDATE SKIP LOCKED`), `oracle_ingestion.py`, `oracle_observations.py`, `runtime_storage.py`, 대상 v2 | SQLite와 같은 Mock 입력의 결과를 표 단위로 대조해 일치 | `e6867c5` `01da403` `f63ddae` `3ac56d9`, 373(skip 74)/Oracle 447 **[EVID]** |
| 2026-11-04 **[GIT]** | U10. ORACLE-RUNTIME-2 | 계획·슬롯·소유권·예산을 Oracle 단독으로 | `oracle_planner.py`, `tracking_slots`, `runtime_ownership.py`, `quota_ledger.py`, 대상 v3~v6 | Shadow·런타임 동시 쓰기를 DB 수준에서 차단 | `91ab0dd`~`f830489`, 403(skip 104)/Oracle 505(보완 전 코드) **[EVID]** |
| 2026-11-04 **[GIT]** (보고서는 11-03~04) | U11. PARALLEL-B8-API (별도 worktree) | 모바일 앱용 읽기 전용 API 계약 | `feat/b8-app-api` 브랜치, `app_contract.py`, `analytics_service.py`, `app_api.py`, `app_mock.py`, OpenAPI 3.0.3 | `/api/v1` 7개 엔드포인트, 예시 32개, Mock 서버 | `9b2156c`~`1b9459b`, 411(skip 74) **[EVID]** |
| 2026-11-05 13:48 ~ 14:34 **[GIT]** | U12. B8-INTEGRATION-1 | B8 브랜치를 main에 병합, Oracle 동등성 | `--no-ff` 병합 `56c027d` → main fast-forward | SQLite·Oracle 같은 응답, 충돌 0 | `56c027d` `c448c44` `8b1a097` `10e0d54`, 456(skip 120)/Oracle 576 **[EVID]** |
| 2026-11-05 17:06 ~ 18:33 **[GIT]** | U13. ORACLE-CUTOVER-PREP-1 | 전환 리허설, 역방향 복구 | `cutover.py`, `reverse_recovery.py`, `oracle_foods.py`, `oracle_tracking.py`, 대상 v7 | 운영 DB 사본 15,671행 개발 Oracle 적재, 29개 표 해시 일치 | `a5a8152`~`9f3bdb6`, 490(skip 153)/Oracle 643 **[EVID]** |
| 2026-11-05 19:28 ~ 20:22 **[GIT]** | U14. ORACLE-BACKEND-COMPLETION-1 | RSS 발굴·관리 서버까지 Oracle로 | `oracle_rss.py`, `--backend oracle-test`, 대상 v8 | RSS→승인→추적→Mock 수집→B8을 SQLite 연결 0으로 | `8d481a7`~`2ff5b7a`, 503(skip 166)/Oracle 669 **[EVID]** |
| 2026-11-05 21:12 ~ 22:08 **[GIT]** | U15. ORACLE-LIVE-INTEGRATION-1 | 기존 수집기를 Oracle Live 경로에 연결 | `oracle_live.py`, `--writer-transfer`, 대상 v9 | 가짜 전송(LiveWire)으로만 검증 | `3a9925f`~`1b848f9`, 514(skip 177)/Oracle 689 **[EVID]** |
| 2026-11-05 23:18 ~ 11-06 03:55 **[GIT]** | U16. ORACLE-WRITER-RELEASE-1 | Writer 철회·사용량 인계 | `release_writer`, `usage_handover.py`, 대상 v10 | 철회→DRAINING→RELEASED, 재개 판정 | `3b8c7cb` `471f56f` `98a2e24`, 524(skip 187)/Oracle 711 **[EVID]** |
| 2026-11-07 05:15 ~ 22:17 **[GIT]** (폴더명은 11-06) | U17. ORACLE-LIVE-VERIFICATION-READY-1 | 실제 소량 검증 리허설 | `--isolate-database`, `rehearse_live_cycle.py`, `audit_source_readiness.py` | 실행 차단 결함 3건 수정, 절차서 확정 | `93d3e24`~`88b2eaf`, 529(skip 188)/Oracle 717 중 오류 1 **[EVID]** |
| 2026-11-08 00:35 ~ 18:28 **[GIT]** (폴더명은 11-07) | U18. ORACLE-PRODUCTION-CUTOVER-1 | 운영 Oracle `MZ_APP` 전환 | `production_access.py`, `scripts/ops/*`, `live_pilot.py` | 이관 15,671행, 실제 API 7회 저장, 재시작 뒤 데이터 유지 | `dea4b33`~`363ea0f`, 534(skip 189)/Oracle 723 **[EVID]** |
| 2026-11-10 **[GIT]** | U19. 식품안전나라 연동 (**미병합**) | 식약처 공공데이터로 공급 쪽 정보 보강 | `foodsafety.py` 독립 모듈 (다른 작성자) | `origin/feature/foodsafety`에만 있음 | `ee2b01b`, 테스트 13개 정의(실행 결과 미확인) |

---

## 2. 저장소·브랜치 구조 **[GIT]**

- 현재 `main` = `origin/main` = `363ea0f`(2026-11-08 18:28). worktree는 `D:/mz 제조기` 하나다.
- 커밋 71개(main). 작성자는 `asinagi`(최초 커밋 1개, GitHub noreply 주소)와 `gjgjs`(70개)다. 원격은 `https://github.com/asinagi/MZFoodTraker.git`이다(B1~B2 보고서 기재 **[REPORT]**).
- **AI 도구 흔적:** 64개 커밋에 `Co-Authored-By: Claude Opus 5.5`, `59376f2` 1개에 `Claude Sonnet 5.5` 트레일러가 있다. 처음 6개(`97b3c35` ~ `cca7ee7`, A1~B3 인프라)에는 트레일러가 없다. B3 보고서는 "Codex가 Weekly Limit로 중단한 B3 작업을 인수했다"고 적고 있다 **[REPORT]**. 초기 6개 커밋을 어떤 도구로 만들었는지는 **미확인**이다.
- **병합 커밋은 하나뿐이다.** `56c027d` "Merge origin/main (ORACLE-RUNTIME-2) into feat/b8-app-api"(2026-11-05).
  - B8 쪽(`3ac56d9`에서 분기): `9b2156c` → `6e9d274` → `7d3da22` → `d869c75` → `1b9459b`
  - main 쪽(같은 시기 ORACLE-RUNTIME-2): `91ab0dd` → `203e67b` → `f3a77d1` → `0f42a3d` → `f830489`
  - 병합 뒤 `c448c44` → `8b1a097` → `10e0d54`를 main으로 fast-forward했다. B8 worktree(`D:\MZFoodTraker-B8-API`)는 지금은 없다.
- ORACLE-CUTOVER-PREP-1부터는 단위마다 `feat/oracle-*` 브랜치를 만들고 끝나면 main으로 fast-forward했다. 6개 브랜치(`cutover-prep`, `backend-completion`, `live-integration`, `writer-release`, `live-verification-ready`, `production-cutover`) 모두 main에 없는 커밋이 0개다. 다시 말해 전부 main에 들어가 있다.
- **미병합:** `origin/feature/foodsafety`(`ee2b01b`, 2026-11-10, 작성자 `100justin`, `363ea0f` 기반, 파일 3개·+955줄). 로컬 브랜치는 없다.
- 커밋 메시지 형식이 `feat:`/`fix:`/`test:`/`docs:`로 일정하다. 단위마다 "구현 커밋 → 보고서 커밋 → main 통합 기록 커밋" 순서를 따랐다(ORACLE-CUTOVER-PREP-1 이후).

---

## 3. 단위별 상세

### P0. RSS MVP — 2026-10-13 ~ 10-16 (git 이전, **추정**)

- **근거:** `VERIFICATION.md` mtime 2026-10-13 14:33, `verification.json` 14:32, `live-rss.xml` 14:22 **[MTIME]**. DB `runs` 첫 행 `2026-10-13T05:28:55Z`(14:28 KST), 일자별 10-13 41회(성공 39), 10-14 7회, 10-16 3회 **[DB]**.
- **목표:** 한국 Google Trends 급상승 RSS에서 음식 관련 검색어를 찾는 것.
- **구현:** `collector.fetch/parse`(HTTP·XML), `classifier.classify`(문맥 규칙으로 FOOD/REVIEW/NON_FOOD 분류), `storage.py`(원본 응답 `runs.raw_response`, 항목·회차별 관측), `service.py`(수집·잠금·스케줄), `api.py`(로컬 HTTP), 정적 UI. 외부 패키지는 쓰지 않았다.
- **문제와 해결:** Python 3.12로 처음 시도했을 때 타임아웃이 2회 났다. 기본 `python`이 MySQL 번들이어서 Anaconda Python 3.11.7로 실행하고, 이를 피하는 `run.ps1`을 만들었다 **[REPORT: VERIFICATION.md]**.
- **검증:** RSS HTTP 200, 10개 항목 파싱, 3회 반복 수집해도 항목 10·관측 30, FOOD 0 **[REPORT]**.
- **커밋:** 없음. 이 코드는 10-27 최초 커밋에 들어갔다.

### P1. 출처 확장(Instagram·NAVER·YouTube)과 분류기 감사 — 2026-10-26 (git 이전, **추정**)

- **근거:** 루트 문서 mtime: `AUDIT_REPORT.md` 11:30, `NAVER_*` 14:31, `INSTAGRAM_INTEGRATION/LIVE` 14:35, `INSTAGRAM_HASHTAG_ANALYSIS` 15:29, `INSTAGRAM_TOKEN_APPLICATION` 15:30, `YOUTUBE_*` 15:37 **[MTIME]**. `data/` 로그·검증 JSON은 10:51 ~ 15:38 **[MTIME]**.
- **DB 첫 기록 [DB]:** `instagram_hashtags` 생성 05:25Z(14:25 KST), `instagram_media` 첫 행 05:27Z, `naver_calls` 05:22Z ~ 05:54Z(10건), `youtube_calls` 첫 행 06:03Z(15:03 KST).
- **순서(추정):** RSS 분류기 감사(오전, AUDIT_REPORT) → Instagram(오전에 코드 있음, 14시대 실연동) → NAVER(14시대) → YouTube(15시대).
- **분류기 감사(AUDIT_REPORT):** "광주송정역"이 뉴스에 `먹거리`가 있다는 이유로 FOOD가 된 v1 결함을 고쳤다. 기존 240개를 전부 다시 분석했고, "호박"은 REVIEW로 되돌렸다. 정답셋이 없어 Precision/Recall은 산출하지 않았다고 적혀 있다 **[REPORT]**. 변경 전 백업은 `data/audit-20261026/before.sqlite3`, `before-*.py`에 있다 **[MTIME: 파일 존재 확인]**. 이 시점에 "git 저장소가 없었다"고 기록돼 있다.
- **Instagram:** Graph API 해시태그 검색·최근 게시물, 중복 방지, 캡션 해시태그 추출, 공동 출현, 장기 토큰 적용(사용자 승인). `#두쫀쿠` 게시물 67건 저장 **[REPORT]**. DB에는 결국 해시태그 1개, 게시물 185건, 회차 16개가 남았다 **[DB]**.
- **NAVER DataLab:** 7/30/90일 상대 관심도. 응답 본문과 지수는 저장하지 않고 호출 메타데이터만 `naver_calls`에 남긴다(정책) **[REPORT·DB: naver_calls 10행, naver_targets 0]**.
- **YouTube Data API v3:** 설정한 검색어 6개(`youtube_queries` 6 **[DB]**)와 RSS FOOD 후보로 검색, 호출 예산·재시도, 29일 TTL, 음식 추출 파생 분석은 끈다. 영상 431, 호출 38, 실행 3 **[DB]**.
- **보안 처리:** 토큰은 `.secret` 파일과 `.env`에 둔다(이번 감사에서 열지 않음).

### P2. 통합 감사와 Oracle 설계 — 2026-10-26

- **자료:** `reports/integrated-audit-2026-10-26/`(REPORT·ORACLE_MODEL·TRACKING_DESIGN·ROADMAP·evidence.json 등). 기준 시각 2026-10-26 15:47 KST. 10-27 최초 커밋에 들어갔다 **[GIT]**.
- **판단:** 수집원 4개는 있지만 음식 개체 중심의 통합 탐지·추적은 부분 구현이다. 최신 264건 가운데 FOOD 0·REVIEW 1이다. 공통 음식 ID는 없다.
- **찾은 결함:** RSS 후보를 `REPLACE`하면 `ON DELETE CASCADE` 때문에 YouTube 후보 연결이 지워진다(임시 DB에서 1→0으로 재현).
- **산출:** A1~A5, B1~B8, G-OPS 게이트로 된 로드맵과 Oracle 목표 모델(이 시점에는 미구현 제안).
- **검증:** 기존 테스트 97개 통과, 외부 연결 시도 0 **[EVID: test-summary.json `tests_run: 97`]**.

### U1. A1~A3 데이터 무결성·공통 계약 — 2026-10-27, `97b3c35` (15:13)

- **근거 [GIT]:** 최초 커밋에 80개 파일·14,241줄이 들어갔다. P0~P2 코드 전체, `contracts.py`, `migrations.py`, `migration_definitions.py`, `tests/test_foundations.py`, A1~A3 보고서까지 포함한다. 작업 전 보존 폴더 `data/a1-a3-preservation-20261027T054909Z`(14:49 KST)가 있다 **[MTIME]**. 따라서 A1~A3는 git 생성 직전에 작업했고 최초 커밋에 함께 들어갔다.
- **문제:** 감사에서 찾은 REPLACE 결함, 출처마다 다른 메타데이터, 조회 경로에서 쓰기가 일어나는 부작용(생성자 DDL·정리 작업).
- **구현:**
  - A1 `Storage._classify`를 `INSERT ... ON CONFLICT(item_id) DO UPDATE`로 바꿨다. 후보 ID와 자식 FK가 유지된다.
  - A2 `contracts.py`(`envelope`, `encode`, `policy_for`, `require_operation`): source_code·origin(live_api/mock/user/legacy_unknown)·field_states·버전을 담는다. 비밀키 형태의 값은 거부한다.
  - A3 `Storage.reader()`(`mode=ro`, `query_only=ON`), 생성자에서 DDL 제거, 명시적 마이그레이션 v1(기존 30개 테이블)·v2(계약 컬럼).
- **검증:** 97 + 17 = 114개 통과 **[EVID]**. 실제 DB 사본으로 0→2 마이그레이션 보존 검사(`copy-migration.json`).
- **이후 가능해진 것:** 버전 관리되는 스키마 위에서 기능을 추가할 수 있게 됐다.

### U2. A4~A5 음식 개체·정확 별칭·수동 등록 — 2026-10-27, `510bf75` (16:02)

- **목표:** 공통 FOOD_ID, 별칭은 사람이 승인하는 정확 일치만 인정, RSS에 FOOD가 0건이어도 사용자가 음식을 등록할 수 있게 하는 것.
- **구현:** `entity_schema.py`(SQLite v3: 음식·별칭·관계·후보·명령·감사), `entity_types.py`(브랜드·매장 유형 gate), `food_service.py`(식별·승인·이력·멱등·별칭 복구·제한된 병합), `food_http.py`, UI.
- **검증:** 114 + 27 = 141개 **[EVID]**. "두쫀쿠"를 등록하고 대표명을 "두바이 쫀득 쿠키"로 승인한 뒤 세 표기가 같은 FOOD_ID로 해석되는지 API·Chrome에서 확인 **[REPORT]**(`manual-registration-mobile.png` 존재).
- **비고:** A4~A5 보고서는 작업 중 운영 DB 행이 늘어났다고 적었다(RSS runs 82→83). 당시 8000/8002 서버가 계속 수집 중이었기 때문이다. 그래서 "운영 DB 불변"을 주장하지 않는다 **[REPORT]**.

### U3. B1~B2 공통 추적 대상·요청 계획 — 2026-10-27, `6cece7c`(16:10) `9839e80`(16:44) `c159a47`(16:45)

- **B1(v4):** FOOD_ID·출처·계정별로 고유한 추적 대상, 상태(PENDING/ACTIVE/PAUSED/BLOCKED/STOPPED), generation/revision, UTC next_due, 예정 슬롯 고유성. `tracking_service.py`, `tracking_http.py`, `tracking_access.py`.
- **B2(v5):** 버전이 있는 정규 spec/hash, 변경 불가 QUERY/PLAN, query↔target 다대다, 출처별 serializer, 읽기 전용 예산 미리보기, 원자적 예약(reserve/attempt/release) 계약. `query_planner.py`, `quota.py`, `planner_http.py`.
- **검증:** 141 + 18 + 40 = 199개 **[EVID: b1 159, b2 199]**.
- **문제:** main push가 자동 승인 검토에서 거부돼 "사용자 승인 대기"로 기록됐다 **[REPORT]**. B3 시작 시점에는 `origin/main = cca7ee7`로 반영돼 있었다 **[REPORT: B3]**.

### U4. B3 신뢰성 있는 실행 전달 — 2026-10-27 17:26 ~ 10-28 12:20, `cca7ee7` `59376f2` `ea08fe9`

- **목표:** 계획을 실제 요청으로 실행하는 것. 중복 0, 늦은 소유자 쓰기 0, 원격 중복 여부를 모르는 상태를 숨기지 않는 것.
- **구현:** `execution.py`(`enqueue`·`claim`·`_fence`·`RequestSession`), `execution_schema.py`(v6: job·outbox·lease·run·call·checkpoint), `execution_ownership.py`·`execution_sources.py`(v7: 출처별 단일 writer, 만료되는 item receipt), `retry_after.py`, `execution_maintenance.py`, `execution_mock.py`, `live_controller`. SQLite `BEGIN IMMEDIATE`로 claim을 직렬화하고, HTTP 요청 중에는 트랜잭션을 열지 않는다. 페이지마다 저장·호출 완료·체크포인트를 한 트랜잭션에 묶는다.
- **경위:** 인프라(`cca7ee7`) 이후 출처 연결 작업이 커밋되지 않은 상태에서 Codex가 중단했다. 다음 세션이 그 작업 트리를 그대로 이어받아 검증하고 마무리했다 **[REPORT]**.
- **찾은 결함과 수정:** 마지막 시도가 전송 직후 중단되면 `claim`의 최대 시도 초과 분기가 `SENT→UNKNOWN` 전환보다 먼저 실행됐다. 그래서 `remote_duplicate_possible=0`으로 불명 상태가 숨겨졌다. 분기 순서를 고치고 회귀 테스트 2개를 추가했다.
- **검증:** 인수 직후 243 → 수정 후 245개 통과 **[EVID]**. Mock 시나리오: 작업 6개, HTTP 11회, 재전달 시 추가 HTTP 0(`acceptance.json`). 헤드리스 Chrome 화면 점검.
- **한계:** Live·자동 실행은 기본으로 꺼져 있다. 실제 호출은 하지 않았다.

### U5. B3 잔여 + B4 공통 Ingestion/DQ + B5 음식 관측 — 2026-10-28, `ea08fe9`(v8) `051e310`(v9) `594f47b`(v10) `9f8f703`

- **B3 잔여:** `result_coverage`(COMPLETE/PARTIAL/LIMITED/NONE)·`committed_pages`, Instagram 고유 태그 사전 확인 생략 규칙, `live_readiness`(fail-closed), 관리 서버 보호(루프백 바인딩, Host 검사, 프로세스별 관리 토큰).
- **B4:** 출처 어댑터 → A2 Envelope → 정책 게이트 → 스테이징(`ingest_runs`, `source_items`, `item_snapshots`, `run_items`) → `quality_results`. 원문은 복사하지 않고 해시·참조만 남긴다. NAVER는 메모리에서만 처리한다. 레거시 행은 `--ingest-legacy`로 `legacy:<source>:origin-unknown` 논리 회차에 적재한다.
- **B5:** 검색 근거(SEARCH_ONLY), 승인된 정확 별칭으로 찾은 음식 언급(RSS 검색어·뉴스 제목, IG 캡션 해시태그), 허용된 지표만 기록, 별칭·병합 재처리. YouTube 언급과 NAVER 저장은 정책상 막는다.
- **검증:** 280개 **[EVID]**. 보존 스냅샷의 임시 복제본 처리 검증(`copy-b45-processing.json`).
- **이 단위 직후:** 운영 SQLite 마지막 쓰기 10-28 14:20:01 KST **[MTIME·DB: 마지막 RSS run 04:49Z, IG run 05:19Z]**. 이후 운영 수집기는 돌지 않았다. 운영 DB는 끝까지 schema v0이다 **[DB: user_version 0, schema_migrations 테이블 없음]**.

### U6. B6~B7 Oracle 대상 스키마·이관·Shadow — 2026-11-02 오전, `5303b61` `e55f81c` `81fd1cd` `18035ba` `8e64830`

- **목표:** SQLite를 Oracle로 옮길 준비.
- **구현:** `target_schema.py`(29개 데이터 테이블 명세 하나로 Oracle·SQLite DDL을 함께 생성, 대상 v1), `target_db.py`(`python-oracledb` Thin, 풀, `call_timeout`, ORA/DPY 오류 매핑), `target_repository.py`(이식 가능한 업서트: 조회→INSERT, UNIQUE 충돌 시 재조회→UPDATE), `target_migration.py`(읽기 전용 Export → TTL·정책 필터 → `LEGACY_KEY_MAP` → 청크 커밋·재개 → 대조), `shadow.py`·`shadow_schema.py`(SQLite v11 트리거가 변경 키만 기록하고 멱등 전달, 기본 비활성).
- **한계:** 이 PC에 Oracle·드라이버·WSL이 없어서 **Oracle에서는 아무것도 실행하지 않았다.** 계약 테스트 15종은 SQLite 15/15 통과, Oracle 15개는 skip이었다 **[REPORT]**.
- **검증:** 323개, skip 15 **[EVID]**.

### U7. G-OPS-0 개발용 Oracle 확보와 실제 검증 — 2026-11-02 오후, `eafef71` `c6403f7` `5793d39`

- **문제:** Windows 11 Home은 Oracle Free Windows판의 지원 대상이 아니다.
- **해결:** WSL2(사용자 UAC 승인, 재부팅 1회) → Ubuntu 24.04.5(`D:\WSL`) → Podman 4.9.3 → 공식 이미지 Oracle AI Database 26ai Free 23.26.3, PDB `FREEPDB1`, `127.0.0.1:1521`만 게시. 드라이버는 `.venv`에 `python-oracledb==26.1.0`(Python 3.12.1). 테스트 스키마 `MZ_TEST`(최소 권한). `tests/oracle_support.reset_target_schema`는 테스트 계정임을 확인한 뒤에만 초기화한다 **[REPORT]**.
- **찾은 결함:** ① Oracle 대조 함수가 FK 결과를 실제로 측정하지 않고 고정 문자열을 반환했다. 고아 행을 실제로 세도록 바꿨다. ② 빈 `runTest`가 테스트로 잡혀 1개가 과대 집계됐다(`c6403f7`).
- **검증:** 실제 Oracle 포함 356개, skip 0 **[EVID]**. 기본 환경 324개, skip 32 **[EVID]**. Shadow 변경 7건 전달·실패 0 **[REPORT]**.

### U8. STORAGE-1 저장 위치 감사 + DB 경로 안전화 — 2026-11-02, `524fc53` `8f293ca`

- 프로젝트·DB·WSL vhdx(16GB)·Podman 볼륨이 모두 D 드라이브에 있음을 확인했다. 옮긴 것은 없다 **[REPORT]**.
- `8f293ca`: `paths.py`로 기본 DB 경로를 프로젝트 루트 기준으로 정했다. 파일이 없으면 `--migrate`가 거부하고(`--create-database` 필요), 상태 명령은 읽기만 한다. ORACLE-RUNTIME-1 보고서는 이 커밋을 S1 단계로 분류한다.

### U9. ORACLE-RUNTIME-1 기존 서비스의 Oracle 실행 — 2026-11-03, `e6867c5` `01da403` `f63ddae` `3ac56d9`

- **방식:** 서비스의 판단 로직은 그대로 두고 저장 primitive만 바꿨다. `OracleIngestionService`·`OracleObservationService`는 primitive만 재정의한다. B3는 같은 메서드 계약(`enqueue/claim/renew/begin/finish/fail/status`)을 가진 `OracleExecution`으로 구현했다.
- **핵심 기술:** claim에서 `SELECT … FOR UPDATE SKIP LOCKED`, 잠금 아래에서 lease를 다시 읽기, job→lease 순서 잠금으로 교착 방지 **[GIT: oracle_execution.py:623 확인]**.
- **저장소 선택:** `runtime_storage.py`의 `--runtime-status sqlite|oracle-test|oracle-production`. 이 시점에 운영은 항상 거부했다.
- **대상 v2:** `execution_*`, `quota_charges`, `source_contents.source_payload_hash`(DBMS와 무관하게 같은 해시 키). 예약어 `RESOURCE`는 피했다.
- **검증:** 차등 하네스로 SQLite와 Oracle 결과를 표 단위로 대조(시나리오 1~10 일치, 동시 claim은 정확히 1건 성공). 기본 373(skip 74), 실제 Oracle 447(skip 0) **[EVID]**.

### U10. ORACLE-RUNTIME-2 계획·슬롯·소유권·예산 — 2026-11-04, `91ab0dd` `203e67b` `f3a77d1` `0f42a3d` `f830489`

- **R1** `OracleQueryPlanner`(대상 v3): Preview는 읽기 전용, Commit은 요청 키로 멱등.
- **R2** `tracking_slots`(v4): `due_slot`을 정규화한 UTC 문자열로 저장한다. Oracle은 TSTZ 열을 유일키에 쓸 수 없기 때문이다(ORA-02329).
- **R3** `runtime_ownership`·`runtime_participants`(v5): 소유 모드 IDLE/SHADOW/RUNTIME_TEST와 프로세스 임대로 Shadow와 런타임의 동시 쓰기를 DB 수준에서 막는다.
- **R4** `quota_usage_ledger`·`quota_accounts`(v6): 레거시 사용량을 근거·불확실성과 함께 원장으로 옮기고, 사용량을 확인하지 못한 계정은 실행을 막는다.
- **문제와 수정:** Shadow 전달이 임대 시간보다 오래 걸리는 경우가 있었다. 쓰기마다 임대를 연장하도록 고쳤다.
- **검증:** 기본 403(skip 104) **[EVID]**. 실제 Oracle 전체 505개는 **마지막 임대 보완 전 코드**에서 실행했다 **[EVID: oracle-full-tests 505]**. 보완 후에는 영향받는 모듈만 다시 실행했다(`oracle-post-fix-tests/test-summary.json`의 runs 기록, 대역 79 등). 최종 코드의 Oracle 전체 회귀는 이 단위 안에서 돌리지 않았다. 다음 단위(U12)에서 576개 전체가 통과했다.

### U11. PARALLEL-B8-API 앱 데이터 API — 2026-11-04 (별도 worktree, `feat/b8-app-api`)

- **커밋 [GIT]:** `9b2156c`(10:46) `6e9d274`(11:03) `7d3da22`(11:23) `d869c75`(11:35) `1b9459b`(14:42). 보고서는 "2026-11-03~04"라고 적었지만 커밋은 모두 11-04다. 11-03 작업 여부는 **미확인**.
- **방식:** `D:\MZFoodTraker-B8-API`에 worktree를 따로 두고 ORACLE-RUNTIME-2와 동시에 진행했다. 기존 파일은 고치지 않고 새 파일만 추가했다(병합 시 B8 신규 58개, 충돌 0) **[REPORT]**.
- **구현:** `app_contract.py`가 OpenAPI 3.0.3을 생성한다(오퍼레이션 7, 스키마 49, `docs/app-api/openapi.yaml/json`, 예시 32개). `analytics_service.py`·`analytics_reader.py`는 B5 관측을 읽을 때 집계한다(확인된 언급의 고유 콘텐츠 수, 관측이 없으면 0이 아니라 `null`, 종합 점수 없음). `app_api.py`(별도 프로세스, `/api/v1`), `app_mock.py`·`observation_writer.py`.
- **설계 판단:** 기존 관리 서버는 루프백 Host만 받고 관리 토큰을 페이지에 넣는다. 그래서 앱용(Android 에뮬레이터 `10.0.2.2`)으로 재사용하지 않고 서버를 분리했다.
- **검증:** P1 373 → P2 390 → P3 399 → 최종 411(skip 74), 통합 전 점검 432(skip 95) **[EVID]**.

### U12. B8-INTEGRATION-1 — 2026-11-05, `56c027d` `c448c44` `8b1a097` `10e0d54`

- main(`f830489`)을 B8 브랜치에 `--no-ff`로 병합했다. 충돌 0. 이후 main을 fast-forward했다.
- **Oracle 지원:** `--store sqlite|oracle-test`, 요청마다 `SET TRANSACTION READ ONLY`.
- **발견·수정:** ① 연결 획득 단계의 드라이버 오류를 변환하지 않아 503 대신 500이 나갈 수 있었다. ② python-oracledb 26.1.0이 `IS JSON` CLOB을 `dict`로 돌려줘서 `json.loads(dict)`가 실패하는 경로가 있었다.
- **검증:** 기본 456(skip 120), 실제 Oracle 576(skip 0) **[EVID]**. SQLite와 Oracle의 응답이 같고, 계약 예시 32개를 그대로 재현했다 **[REPORT]**.

### U13. ORACLE-CUTOVER-PREP-1 — 2026-11-05, `a5a8152` `1aeabe3` `916d699` `5af42a3` `5c750bb` `9f3bdb6`

- **Phase 1 결함:** B7이 만료된 B5 묘비를 버려서 SQLite는 `EVIDENCE_EXPIRED`(null), Oracle은 `OBSERVED`(0)로 갈렸다. 수정 전에 실패 22건으로 재현했다(`phase1-reproduction`). B7이 내용 없는 `EXPIRED` 묘비를 옮기도록 고쳤다(`a5a8152`).
- **Phase 2 리허설:** `cutover.py`, `tests/rehearse_cutover.py`. 운영 DB(v0, 30개 표, 7,701행)를 읽기 전용 backup API로 복사하고, 사본만 v0→v11로 올려 스테이징한 뒤 MZ_TEST에 15,671행을 적재했다. 29개 표 해시 일치, 재실행 쓰기 0 **[REPORT·EVID: cutover-rehearsal-oracle.json 존재]**.
- **Phase 3:** 음식 명령·추적 명령의 Oracle 서비스(`oracle_foods.py`, `oracle_tracking.py`, 대상 v7 `tracking_commands/events`).
- **Phase 4:** 역방향 복구 `reverse_recovery.py`(`--recovery plan|apply`).
- **중간 실패와 수정 [EVID]:** phase3 대역 390개 중 1건 실패(새 파생 열을 모르는 스키마 범위 검사), Oracle 203개 중 2건 실패(스키마 재생성 직후 읽기 전용 트랜잭션의 ORA-01466 → 테스트에 대기 추가). 다시 실행해 통과(`phase3-fix-reruns`).
- **최종:** 기본 490(skip 153), Oracle 643 **[EVID]**. 운영 전환은 아니라고 명시했다(BLOCKED 4건).

### U14. ORACLE-BACKEND-COMPLETION-1 — 2026-11-05, `8d481a7` `42c696d` `3289206` `4e2da90` `98afd73` `2ff5b7a`

- `oracle_rss.py`(`OracleRssStore`, `OracleRssService`, `OracleExecutionController`), 대상 v8 `rss_runs`·`rss_observations`. 수집기와 분류기는 그대로 쓰고 저장만 Oracle로 보낸다.
- 관리 서버를 `--backend oracle-test`로 띄우면 기존 `make_server`에 Oracle 서비스를 주입한다. 레거시 직접 수집기는 503으로 응답한다. `--maintenance`.
- **E2E:** RSS Mock 수집 → 후보 → 관리 API 승인 → 별칭 → 추적 → 계획 → Mock 수집 → B4·B5 → B8을 실제 HTTP로 돌렸고, 그동안 sqlite3 연결 0 **[REPORT]**. 헤드리스 Chrome 점검(`oracle-admin-browser-check-*.json`, png).
- **검증:** 기본 503(skip 166), Oracle 669 **[EVID]**.

### U15. ORACLE-LIVE-INTEGRATION-1 — 2026-11-05, `3a9925f` `3968f57` `f88c94c` `35156f7` `d97c316` `1b848f9`

- **정리:** 테스트가 잘못 만든 `D:\Users\...` 빈 디렉터리 9개를 삭제했다(CLEANUP.md, `3a9925f`).
- **RSS 역방향 복구(`3968f57`, 대상 v9):** Oracle `rss_observations.payload_text`를 SQLite `observations`로 같은 번호로 되돌린다. 원본 피드 본문은 대상에 없으므로 `raw_response`는 NULL로 두고 손실로 보고한다.
- **Live 경로(`f88c94c`):** `oracle_live.py`가 기존 `GraphClient`·`YouTubeClient`·`NaverClient`·RSS collector를 연결한다. Live 스위치, 전용 수집원, 검토된 registry, 자격 증명, 레거시 스케줄러 정지, `--writer-transfer`가 모두 갖춰져야 하고, 요청 직전마다 다시 확인한다. RSS 30분 예약 실행, 10분 넘은 미완료 회차는 `interrupted`로 마감한다.
- **검증:** 기록용 가짜 전송(LiveWire)으로만 했다. 기본 514(skip 177), Oracle 689, live 모듈 16 **[EVID]**. 실제 호출 요청서 `LIVE_VERIFICATION_REQUEST.md`(상한 RSS 1·IG 5·YT 2·NAVER 1).

### U16. ORACLE-WRITER-RELEASE-1 — 2026-11-05 23:18 ~ 11-06 03:55, `3b8c7cb` `471f56f` `98a2e24`

- `--writer-release`(계정 행 잠금 → 새 요청 차단 → 진행 중이면 `DRAINING` → `WRITER_RELEASED`로 닫고 `IMPORTED`로 복귀), 대상 v10 `writer_transfer_events`(추가 전용), `--writer-status`, `--writer-handover`(`usage_handover.py`: Oracle 기간 사용량을 `youtube_calls`·`naver_calls`·`instagram_searches`로 인계), `--legacy-resume-check`.
- **문제:** 1차 Oracle 전체 회귀가 PC Modern Standby(23:33 ~ 03:10) 때문에 멈췄다. 711개 중 오류 1(`test_oracle_scheduling`, `database_error`)이었다 **[EVID: oracle-full-tests-interrupted errors 1]**. Windows Kernel-Power 이벤트로 원인을 확인한 뒤 다시 실행했다.
- **검증:** 2차 711개 전부 통과 **[EVID]**, 기본 524(skip 187) **[EVID]**.

### U17. ORACLE-LIVE-VERIFICATION-READY-1 — 2026-11-07 05:15 ~ 22:17, `93d3e24` `86c8209` `707b484` `3cac9d7` `bb0eaf9` `88b2eaf`

- **구현:** `--isolate-database`(운영 DB를 읽기 전용 백업으로 복사하고 격리 표시를 남긴 v11 사본, `93d3e24`), `--isolated-database`, `tests/rehearse_live_cycle.py`(리허설 28개 점검), `tests/audit_source_readiness.py`, `LIVE_PILOT_RUNBOOK.md`, `GO_NO_GO.md`.
- **리허설에서 찾은 결함 3건(`86c8209` 등) [REPORT]:**
  - F-1 격리 사본을 쓰려면 운영 승인 스위치가 필요했다.
  - F-2 운영 DB처럼 Instagram 기록이 7일보다 오래되면 Instagram Writer를 이전할 수 없었다.
  - F-3 인계 뒤 복구가 갱신 3건을 빠뜨린 채 `converged: true`를 보고했다.
- **검증:** 기본 529(skip 188) **[EVID]**. 실제 Oracle 717개 중 716 통과, **오류 1**(`successful: false`) **[EVID]**. 오류는 직전 회귀가 강제 종료된 뒤 크래시 복구 직후의 스키마 초기화 문제로 설명했고, 해당 모듈만 다시 실행해 8/8 통과했다 **[EVID: oracle-recheck-admin 8]**. 최종 코드로 717개 전체를 다시 돌리지는 않았다.
- **폴더명과 날짜:** 폴더는 `-2026-11-06`이지만 보고서 본문은 "작업 2026-11-07"이고 커밋도 11-07이다.

### U18. ORACLE-PRODUCTION-CUTOVER-1 운영 전환 — 2026-11-08, `dea4b33`(00:35) `a8eebc5` `875997e` `9307d16` `363ea0f`(18:28)

- **구현:**
  - `production_access.py`: `ORACLE_ALLOW_PRODUCTION`은 접속만 연다. 선언된 운영 스키마(`*_TEST`·시스템 계정 거부)와 승인 기록(G-OPS-1/2/3/6)이 있어야 SHADOW·RUNTIME_PRODUCTION을 쓸 수 있다. 운영에서는 Mock 실행을 거부한다(`MOCK_EXECUTION_REFUSED_ON_PRODUCTION`).
  - `--backend oracle-production`, 앱 API `--store oracle-production`.
  - `scripts/ops/`(`start/stop-oracle.ps1`, `start/stop-backend.ps1`, `status.ps1`, `verify.ps1`, `live_pilot.py`, `verify_production.py`) **[GIT: 파일 존재 확인]**.
- **수정(`875997e`):** 관리 서버가 POST 본문을 읽지 않은 채 거부 응답을 보내면 Windows가 연결을 리셋했다(WinError 10053). 600KB 요청 10회 중 4회 실패 → 수정 후 0회 **[REPORT]**.
- **실행 기록 [EVID]:**
  - 백업: `backup-verification.json` 2026-11-07T15:37Z(11-08 00:37 KST). 원본 SHA-256 `38b880f5…689548c0` 불변, 7,701행, 복원 시험 통과.
  - 이관: `migration-verification.json` 15:39Z. `MZ_APP` 51개 표·16,699행. 이 중 이관 데이터 15,671행 + `legacy_key_map` 991 + `migration_batches` 29 + 소유권·계정 8행 = 16,699로 맞아떨어진다. API 동등성 17×2 요청 불일치 0.
  - 실제 API 소량 검증: `live-pilot.json` 2026-11-07T23:25:50Z ~ 23:26:20Z(**11-08 08:25 KST, 30초**). 요청 7회(RSS 1, IG 3, YT 2, NAVER 1), 모두 HTTP 200, 상한 초과 거부 0. RSS 항목 10개, **후보 0건**. 검증용 음식 1개(FOOD_3007…)를 등록하고 3개 출처 추적을 ACTIVE로 한 뒤 작업 3개(IG LIMITED, YT LIMITED, NAVER SUCCEEDED) 실행, 수집 경로의 SQLite 연결 0.
  - 영속성: `persistence-after.json`·`final-verify.json`(11-08 09:26Z). 재시작 뒤 행 수 동일. B8에서 Instagram 2026-11-08 값 28(OBSERVED).
- **테스트:** 기본 534(skip 189), 실제 Oracle(MZ_TEST) **723개 전부 통과** **[EVID]**. 로그 기준 34,042.9초인데, PC 절전 구간(09:05 ~ 18:06)이 포함된 시간이다 **[EVID: tests.log "Ran 723 tests in 34042.931s / OK"]**.
- **남은 것 [REPORT·EVID]:** 지속 자동 수집 꺼짐(CONFIGURED_NOT_ENABLED). 최종 점검 시 추적 대상 3개의 effective 상태가 모두 `BLOCKED`(`final-verify.json`). 제공자 측 사용량은 UNKNOWN. API는 루프백 전용. 백엔드는 스크립트로 수동 시작. 실행 계정이 스키마 소유자.

### U19. 식품안전나라 공공데이터 연동 — 2026-11-10, `origin/feature/foodsafety` `ee2b01b` (**미병합**)

- **작성자:** `100justin <jonghunbaeg8@gmail.com>`. 프로젝트의 다른 커밋 작성자와 다르다. 기반은 `363ea0f` **[GIT]**.
- **내용:** `food_trends/foodsafety.py`(617줄; `FoodSafetyClient`, `build_report`, `match_kind`, `lookup_ingredient` 등, `urllib`로 `openapi.foodsafetykorea.go.kr` 호출), `tests/test_foodsafety.py`(`def test_` 13개), `docs/FOODSAFETY.md`. 식약처 I1250(품목제조보고)·C002(원재료)를 품목보고번호로 연결해 월별 허가 건수(공급 추이)와 핵심 원재료를 계산한다. 결과는 `data/foodsafety/*.json` 캐시에 둔다.
- **상태:** 기존 DB·Oracle·B8에 연결하지 않은 독립 CLI다. 이 PC에는 `data/foodsafety/` 캐시가 없다. 테스트 실행 결과는 **미확인**(이번 감사에서 실행하지 않음, 증거 파일 없음). main에는 없다.

---

## 4. 수치 추이

### 4.1 테스트 수 (전부 test-summary.json에서 직접 확인 **[EVID]**)

| 단위 | 기본 환경 (통과 / skip) | 실제 Oracle 포함 | 실패·오류 |
| --- | --- | --- | --- |
| 통합 감사 | 97 / 0 | — | 0 |
| A1~A3 | 114 / 0 | — | 0 |
| A4~A5 | 141 / 0 | — | 0 |
| B1~B2 | 199 / 0 | — | 0 |
| B3 | 245 / 0 | — | 0 |
| B4~B5 | 280 / 0 | — | 0 |
| B6~B7 | 323 / 15 | — | 0 |
| G-OPS-0 | 324 / 32 | 356 | 0 |
| ORACLE-RUNTIME-1 | 373 / 74 | 447 | 0 |
| ORACLE-RUNTIME-2 | 403 / 104 | 505 (보완 전 코드) | 0 |
| B8 (worktree) | 411 / 74 | — | 0 |
| B8-INTEGRATION-1 | 456 / 120 | 576 | 0 |
| ORACLE-CUTOVER-PREP-1 | 490 / 153 | 643 | 0 (중간 phase3에서 3건 실패 후 수정) |
| ORACLE-BACKEND-COMPLETION-1 | 503 / 166 | 669 | 0 |
| ORACLE-LIVE-INTEGRATION-1 | 514 / 177 | 689 | 0 |
| ORACLE-WRITER-RELEASE-1 | 524 / 187 | 711 (1차 중단본 오류 1) | 0 |
| ORACLE-LIVE-VERIFICATION-READY-1 | 529 / 188 | 717 (**오류 1**, 모듈 재실행 8/8) | 오류 1 |
| ORACLE-PRODUCTION-CUTOVER-1 | 534 / 189 | 723 | 0 |

"기본 환경"의 숫자는 실행한 테스트 수이고, 그중 skip은 Oracle 전용 테스트다. 모든 실행에서 외부 접속 시도는 0으로 기록돼 있다(`blocked_external_attempts: 0`). 주의할 점: `tests.log`는 `.gitignore`의 `*.log` 규칙 때문에 git에 없다(로컬에만 32개 파일). git에는 `test-summary.json`만 있다.

### 4.2 스키마 버전 **[GIT: 버전 상수 도입 커밋 추적]**

| SQLite 개발 스키마 | 커밋 | | Oracle 대상 스키마 | 커밋 |
| --- | --- | --- | --- | --- |
| v1~v2 (기존 30표·계약 컬럼) | `97b3c35` | | v1 (29개 데이터 테이블) | `5303b61` |
| v3 음식 개체 | `510bf75` | | v2 실행 계층 | `e6867c5` |
| v4 추적 대상 | `6cece7c` | | v3 계획 | `91ab0dd` |
| v5 요청 계획 | `9839e80` | | v4 슬롯 | `203e67b` |
| v6 실행 인프라 | `cca7ee7` | | v5 소유권 | `f3a77d1` |
| v7 출처 연결·소유권 | `59376f2` | | v6 예산 원장 | `0f42a3d` |
| v8 결과 범위 | `ea08fe9` | | v7 추적 명령 | `916d699` |
| v9 Ingestion | `051e310` | | v8 RSS | `8d481a7` |
| v10 음식 관측 | `594f47b` | | v9 RSS 관측 본문 | `3968f57` |
| v11 Shadow 트리거 | `18035ba` | | v10 Writer 이벤트 | `3b8c7cb` |

현재 코드는 `migrations.LATEST_VERSION = 11`, `target_schema.TARGET_SCHEMA_VERSION = 10`이다 **[GIT]**. 운영 SQLite `data/trends.sqlite3`는 끝까지 **v0**이었다 **[DB]**.

### 4.3 실제 데이터 (운영 SQLite 원본, 읽기 전용 조회 **[DB]**)

| 항목 | 값 |
| --- | --- |
| RSS 회차 | 95 (성공 90), 2026-10-13 ~ 10-28 |
| RSS 항목 / 관측 | 370 / 900 |
| 분류 | NON_FOOD 369, REVIEW 1, **FOOD 0** |
| Instagram | 해시태그 1, 게시물 185, 회차 16 |
| YouTube | 영상 431, 호출 38, 실행 3, 검색어 6 |
| NAVER | 호출 10 (응답 본문 미저장) |
| 파일 SHA-256 | `38b880f5…689548c0`. 보고서들이 적은 값과 같고, mtime은 2026-10-28 14:20:01 그대로다 |
| v11 사본 `data/ops-prod/production-source-v11.sqlite3` | `food_entities` 0, `tracking_targets` 0, `schema_migrations` 최대 11 |

---

## 5. 보고서와 다르거나 주의해야 할 점

1. **README "기술 스택" 절이 갱신되지 않았다.** "별도 Python 패키지 설치가 필요하지 않다", "Oracle 저장소·드라이버 연동은 향후 개발 항목"이라고 적혀 있다. 실제로는 G-OPS-0 이후 `.venv`의 `python-oracledb 26.1.0`(+cryptography, cffi)을 쓰고, Oracle이 운영 SoR이다. "디렉터리 구조"에도 보고서가 2개만 나온다. 본문 상단의 현황표는 최신이다.
2. **보고서 폴더명의 날짜와 실제 작업 날짜가 다르다.** B3(폴더 10-27, 보고서 10-28, 커밋 10-27~28), B8(폴더 11-04, 보고서 "11-03~04", 커밋은 모두 11-04), LIVE-VERIFICATION-READY(폴더 11-06, 커밋 11-07), PRODUCTION-CUTOVER(폴더 11-07, 커밋 11-08, 백업·실제 호출은 11-08 KST). 날짜는 git을 기준으로 봐야 한다.
3. **"운영 전환 완료"의 실제 범위는 좁다.** 증거 파일에 따르면:
   - 실제 외부 호출은 2026-11-08 08:25 KST의 30초 동안 7회뿐이다. 그 뒤로 지속 수집은 꺼져 있다.
   - 최종 점검 때 추적 대상 3개의 effective 상태가 모두 `BLOCKED`였다.
   - B8 출처 상태에서 YouTube·NAVER의 최근 데이터는 `null`이다.
   - `MZ_APP`의 음식 1개는 RSS에서 발견한 것이 아니다. 검증하려고 직접 등록한 것이다.
   - 이관된 기존 데이터의 음식 개체는 0건이다.
   "Oracle이 기록 시스템(SoR)이다"는 맞다. 하지만 "운영 중인 수집 서비스"라고 말할 상태는 아니다.
4. **RSS에서 실제 FOOD는 한 번도 나오지 않았다.** 운영 DB 370건에서 FOOD 0, 운영 검증 RSS 10건에서 후보 0이다. "RSS 발굴 → 자동 추적" 연결은 Mock 데이터와 수동 등록으로만 검증됐다. 실제 데이터로는 검증되지 않았다. 보고서들도 이 점을 밝히고 있다(통합 감사 등).
5. **Oracle 전체 회귀가 최종 코드를 대상으로 하지 않은 단위가 2개 있다.** ORACLE-RUNTIME-2는 보완 전 코드로 505개를 돌렸다. LIVE-VERIFICATION-READY는 717개 중 오류 1(`successful: false`)이었고, 해당 모듈만 다시 실행했다. 두 보고서 모두 이 사실을 숨기지 않았다. 이후 단위(576개, 723개)에서 전체가 통과했으므로 현재 코드 기준 공백은 없다.
6. **이관 행 수 15,671과 `MZ_APP` 16,699행의 차이**는 모순이 아니다. 키 맵 991, 배치 기록 29, 소유권·계정 8행을 더하면 정확히 맞는다(4.3과 U18 참고).
7. **git 이전 단계(P0~P2)는 git에 기록이 없다.** P0~P2는 최초 커밋 하나에 몰려 있다. `data/`와 `*.log`는 git 대상이 아니다. 이 단계의 순서와 시각은 mtime과 DB 타임스탬프로 추정한 것이다. 10-17 ~ 10-25에는 RSS 회차가 없다 **[DB]**. 그 기간의 개발 활동은 **미확인**이다.
8. **작업 도구와 작성자:** 초기 6개 커밋에는 AI 공동작성 트레일러가 없다. B3 보고서는 Codex가 B3 일부를 작업했다고 적었다. 이후 커밋은 Claude 공동작성이다. 식품안전나라 브랜치는 다른 작성자(`100justin`)다. 교수님께 설명할 때 이 구분을 정확히 해 두는 것이 좋다.
9. **Python 버전 표기가 섞여 있다.** README는 "Python 3.11 이상"이다. git 이전 실제 수집은 Anaconda 3.11.7이었고, Oracle 작업 `.venv`는 3.12.1이다(G-OPS-0 보고서) **[REPORT]**.

## 6. 미확인 항목

- 2026-10-13 이전과 10-17 ~ 10-25의 개발 활동.
- 초기 6개 커밋(A1 ~ B3 인프라)을 만든 도구나 사람의 구분(Codex 관여는 B3 보고서 기재만 있음).
- B8 작업의 11-03 진행 여부.
- 식품안전나라 모듈의 테스트 통과 여부와 실제 API 동작.
- ~~운영 Oracle `MZ_APP`의 현재 상태~~ → **해소(통합 정리 시 추가):** [DATABASE_EVOLUTION_AUDIT.md §6](DATABASE_EVOLUTION_AUDIT.md)에서 2026-11-13에 읽기 전용 SELECT로 직접 조회했다. 행 수는 `final-verify.json`(11-08)과 전부 같았고, 2026-11-07 23:26Z 이후 새로 쓰인 데이터는 없었다.
- 보고서가 서술한 각 실행(브라우저 점검, 리허설 등)은 증거 파일이 있다는 것만 확인했다. 재실행은 하지 않았다.

## 7. 감사 방법 메모

`git log --stat`·`git show --stat`·`git log -S`(버전 상수 추적)·브랜치 merge-base 비교, 모든 `test-summary.json` 일괄 파싱, 핵심 증거 JSON(live-pilot, migration/backup/persistence/final-verify) 열람, 운영 SQLite와 v11 사본을 `mode=ro`로 조회(파일 SHA-256 재계산 결과 보고서 값과 일치)했다. 참고로 SQLite를 읽기 전용으로 열 때도 보조 파일 `trends.sqlite3-shm`의 수정 시각은 바뀔 수 있다. 본 DB 파일의 내용·해시·mtime은 바뀌지 않았다.
