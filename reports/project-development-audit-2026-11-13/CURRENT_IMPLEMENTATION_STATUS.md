# 현재 구현 완료 수준 종합 검증 — MZFoodTraker (`main` 363ea0f 기준)

감사일: 2026-11-13 KST · 읽기 전용 감사(코드·운영 DB·수집 설정 무변경, 외부 API 호출 0, 자동 수집 미활성)

이 문서는 함께 작성한 7개 감사 문서의 결론을 기능 단위로 모아 판정한다. 근거의 상세는 각 문서를 본다.
[개발 이력](PROJECT_DEVELOPMENT_HISTORY.md) · [수집 구현](COLLECTION_IMPLEMENTATION_AUDIT.md) · [데이터 파이프라인](DATA_PIPELINE_AUDIT.md) · [DB 변천](DATABASE_EVOLUTION_AUDIT.md) · [백엔드·프론트엔드](BACKEND_FRONTEND_AUDIT.md) · [문제와 해결](ISSUES_AND_SOLUTIONS.md) · [발표 요약](PRESENTATION_TECHNICAL_SUMMARY.md)

## 0. 판정 기준과 증거 구분

| 판정 | 뜻 |
| --- | --- |
| `IMPLEMENTED & VERIFIED` | 코드가 있고 실제 동작이 검증됨(실제 외부 API 또는 운영/개발 Oracle의 실데이터) |
| `IMPLEMENTED / NOT VERIFIED` | 코드와 자동 테스트는 있으나 실제 데이터·실제 환경에서 실행된 기록이 없음 |
| `PARTIAL` | 일부 기능만 구현되었거나 일부 경로만 검증됨 |
| `MOCK ONLY` | Mock 전송·합성 데이터에서만 검증됨 |
| `DESIGNED ONLY` | 설계·제안 문서만 있음 |
| `NOT IMPLEMENTED` | 구현되지 않음 |
| `BLOCKED` | 외부 환경 또는 접근 문제로 검증할 수 없음 |

증거 구분:
- **[과거]** 저장소에 남은 과거 실행 증거(`reports/*/test-summary.json`, `*.json`, 스크린샷, 로그)
- **[재확인]** 이번 감사(2026-11-13)에서 직접 다시 확인한 것. SQLite 읽기 전용 조회, Oracle `MZ_APP` 읽기 전용 SELECT, Git 이력, 테스트 재실행(외부망·운영 DB 차단)

## 1. 이번 감사에서 직접 재확인한 사실

| 항목 | 결과 | 방법 |
| --- | --- | --- |
| Git 상태 | `main` = `origin/main` = `363ea0f`(2026-11-08), 작업 트리 깨끗함, 커밋 71개(2026-10-27~11-08), 병합 커밋 1개(`56c027d`). 미병합 원격 브랜치 `origin/feature/foodsafety`(`ee2b01b`, 2026-11-10, 다른 작성자, 3개 파일 +955줄) | `git status`, `git log`, `git fetch` |
| 운영 SQLite 원본 `data/trends.sqlite3` | 스키마 v0, 30개 테이블. RSS 회차 95(성공 90, 2026-10-13~10-28), 항목 370, 후보 370(NON_FOOD 369 · REVIEW 1 `호박` · **FOOD 0**), Instagram 게시물 185(해시태그 1), YouTube 영상 431(만료 0, 전부 2026-11-26 만료 예정), YouTube 호출 38, NAVER 호출 10(200 응답 7) | `sqlite3 mode=ro` |
| 분류기 오탐 이력 | `classification_history` 251건 중 v1(`context-rules-v1`)의 FOOD 1건 = `광주송정역`, REVIEW 15건. v2(`target-context-rules-v2`)에서 바로잡힘 | 같음 |
| 운영 Oracle `MZ_APP` | 테이블 52, 대상 스키마 v10, 소유 모드 `RUNTIME_PRODUCTION`, 비활성 제약 0. 행 수는 `final-verify.json`(11-08)과 전부 같음. 마지막 기록 2026-11-07 23:26Z → **파일럿 이후 수집 없음** | 컨테이너를 문서화된 `scripts/ops/start-oracle.ps1`로 켜고 SELECT만 실행한 뒤 `stop-oracle.ps1`로 다시 끔 |
| 자동 테스트 | 아래 §3 참조 | `tests/run_offline.py`(외부 소켓·운영 SQLite 차단), 결과는 저장소 밖에 저장 |

## 2. 최종 구현 현황표

| 기능 | 설계 여부 | 코드 구현 | 실제 검증 | 운영 적용 | 판정 | 남은 작업 |
| --- | --- | --- | --- | --- | --- | --- |
| **RSS 수집**(Google Trends KR) | O | O `collector.py`, `service.py` | O SQLite 95회차(10-13~10-28), Oracle 실제 1회(11-08, 새 항목 10) | 수동 1회만 실행. 30분 예약은 꺼짐 | `IMPLEMENTED & VERIFIED` | 지속 수집 승인·활성화. 피드 URL은 코드 상수 1개 |
| **RSS 음식 분류**(규칙 기반) | O | O `classifier.py` v2 | O 380건 분류(오탐 수정 이력 확인) | O | `IMPLEMENTED & VERIFIED`. 단 **실데이터 FOOD 0건** | 발굴 성능 평가. 실제 FOOD가 나오지 않아 "발굴" 효과는 입증되지 않음 |
| **Instagram 해시태그 수집** | O | O `instagram_graph.py` 외 | O 게시물 185(10-26~28) + 40(11-08 파일럿), HTTP 200 | 수동만. 태그 1개(`두쫀쿠`), `recent_media`만 사용 | `IMPLEMENTED & VERIFIED` | 토큰 자동 갱신 없음(만료 **2026-12-25**). 지속 수집 승인 |
| **YouTube 메타데이터 수집** | O | O `youtube.py` 외 | O 영상 431 + 50, search·videos 분리, 29일 TTL | 수동만 | `IMPLEMENTED & VERIFIED` | 추적 검색어 0개. 영상 기반 음식 파생 분석은 **정책상 비활성** |
| **NAVER DataLab 관심도** | O | O `naver_datalab.py` 외 | O 200 응답 7(10-26) + 1(11-08) | 수동 조회만. 값은 설계상 DB에 저장하지 않음 | `IMPLEMENTED & VERIFIED`(조회) | 저장 정책 재검토 여부 결정. 자동 대상 0 |
| **공통 Ingestion·품질 검사(B4)** | O | O `ingestion.py`, `data_quality.py` | O 이관 시 일괄 처리(품질 결과 10,402) + 파일럿 704 | O(Oracle) | `IMPLEMENTED & VERIFIED` | — |
| **음식 후보 발견**(RSS FOOD/REVIEW → 공통 후보) | O | O `import_rss_run` | 실데이터에서 후보가 생성된 적 없음(FOOD 0). Oracle 후보 2건은 관리 작업 결과 | — | `IMPLEMENTED / NOT VERIFIED`(실데이터) · `MOCK ONLY`(전체 경로) | 실제 FOOD 발생 또는 수동 후보로 전체 경로 검증 |
| **음식 개체·FOOD_ID·사람 승인** | O | O `food_service.py` | O 운영에서 `두쫀쿠` 1건 승인(`FOOD_3007c29a…`) | O(1건) | `IMPLEMENTED & VERIFIED`(소량) | 실제 음식 사전 구축(현재 1개) |
| **정확 별칭 정규화**(두쫀쿠 ↔ 두바이 쫀득 쿠키) | O | O NFC+공백 정규화, 범위별 정확 별칭 | 테스트·Mock에서만. 운영 `두쫀쿠`에는 별칭이 없음 | X | `IMPLEMENTED / NOT VERIFIED`(실데이터) | 운영에 별칭 승인. 붙여쓰기 등은 별칭을 하나씩 승인해야 함(유사도 매칭 없음) |
| **파생 음식**(짭쫀쿠 → `VARIANT_OF`) | O | O | 테스트·Mock만 | X | `IMPLEMENTED / NOT VERIFIED` | — |
| **병합 / 분리(split·unmerge)** | O | 제한된 병합만 | 테스트만 | X | `PARTIAL` | 일반 split/unmerge는 미구현(README 명시) |
| **음식 언급·지표(B5)** | O | O `food_observations.py` | O 운영 언급 143건(별칭 재처리로 이관 게시물 포함), 지표 911 | O | `IMPLEMENTED & VERIFIED` | YouTube 언급·NAVER 저장은 정책상 차단 |
| **별칭 변경 재처리** | O | O `apply_entity_changes` | 운영 승인 시 1회(언급 143 생성). 별칭 취소·변경 재처리는 테스트만 | 수동 유지보수 명령 | `PARTIAL` | 실데이터로 취소·변경 시나리오 검증 |
| **추적 대상 관리(B1)** | O | O `tracking_service.py` | O 운영 추적 대상 3 | O | `PARTIAL` — 최종 점검 때 3개 모두 effective `BLOCKED` | 차단 원인(예산·Writer 상태) 해소 후 지속 실행 |
| **요청 계획·예산(B2)** | O | O `query_planner.py`, `quota.py` | O 파일럿 상한 9회 중 7회 사용, 초과 거부 0 | O | `IMPLEMENTED & VERIFIED`(소량) | 제공자 측 사용량 UNKNOWN |
| **실행 전달(B3 Outbox·Lease·Fencing)** | O | O `execution*.py`, `oracle_execution.py` | O 운영 작업 3·호출 6 성공. 장애 시나리오는 MZ_TEST 회귀 | O | `IMPLEMENTED & VERIFIED`(소량) | — |
| **분석 API(B8, `/api/v1` 7개)** | O | O `app_api.py`, `analytics_*.py` | O 운영에서 6개 엔드포인트 200, OpenAPI 위반 0, 두쫀쿠 11-08 값 28 | 루프백 전용 | `IMPLEMENTED & VERIFIED` | 외부 공개(인증·TLS·속도 제한). 집계 수집원은 RSS·Instagram뿐 |
| **앱 API Mock 서버** | O | O `app_mock.py` | 계약 테스트 | 개발용 | `MOCK ONLY`(의도된 용도) | — |
| **모바일 앱 클라이언트** | 인계 문서만 | X(이 저장소에 없음) | — | — | `NOT IMPLEMENTED`(이 저장소 범위) | 외부 앱 개발자 연동 여부 확인 |
| **종합 유행 점수 / 사전 집계 테이블** | 제안 문서 | X | — | — | `DESIGNED ONLY` | 지표 정의·검증 후 구현 |
| **관리 웹 UI** | O | O 바닐라 JS 단일 페이지 | 브라우저 QA는 MZ_TEST·Mock 기준(11-05). 운영 화면 QA 증거 없음 | O(관리·관측 모드) | `PARTIAL` — Instagram·NAVER·YouTube 패널은 Oracle에서 503, B8 분석 화면 없음 | 운영 화면 QA. 레거시 패널 Oracle 연결 |
| **DB: Oracle `MZ_APP` 운영 전환** | O | O `target_*`, `cutover.py`, `production_access.py` | O 15,671행 이관 `consistent`, 재기동 영속성 검증, 이번 감사 실측 일치 | O(SoR) | `IMPLEMENTED & VERIFIED` | DML 전용 계정 분리, 백엔드 자동 기동 |
| **역방향 복구(Oracle → SQLite)** | O | O `reverse_recovery.py` | MZ_TEST·격리 사본에서 검증 | 운영 실행 없음(필요 없음) | `IMPLEMENTED / NOT VERIFIED`(운영) | — |
| **SQLite 원본 보존** | O | — | O v0 원본 해시 보존, 검증된 백업 | O | `IMPLEMENTED & VERIFIED` | v0 원본은 현재 코드로 열 수 없어 TTL 정리 불가(YouTube 11-26, Instagram 캡션 11-25~27 이후 보존 기한 초과 데이터가 남음) |
| **지속 자동 수집(스케줄러·B3 자동)** | O | O(RSS 30분·IG 60분·YT 6시간·NAVER 24시간) | 운영 실행 기록 없음 | `CONFIGURED_NOT_ENABLED` | `IMPLEMENTED / NOT VERIFIED`(운영). 이번 감사에서도 **활성화하지 않음** | 지속 사용 권한·예산 승인 후 활성화 |
| **식품안전나라 공공데이터 연동** | 브랜치 문서 | 미병합 브랜치에만 존재 | 미확인 | X | `NOT IMPLEMENTED`(main 기준) | 리뷰·테스트 후 병합 여부 결정 |
| **의존성 고정·배포 패키징** | — | X(`requirements.txt` 없음) | — | — | `NOT IMPLEMENTED` | `python-oracledb` 버전 고정 |

## 3. 테스트: 과거 기록과 이번 재실행

### 3.1 과거 기록 [과거]

| 시점 | 기본 스위트(Oracle 없이) | 실제 Oracle(MZ_TEST) 전체 |
| --- | --- | --- |
| 통합 감사 2026-10-26 | 97 OK | — |
| A1~A3 2026-10-27 | 114 OK | — |
| B4~B5 2026-10-28 | 280 OK | — |
| G-OPS-0 2026-11-02 | 324 OK(skip 32) | 356 OK |
| B8 통합 2026-11-05 | 456 OK(skip 120) | 576 OK |
| 운영 전환 2026-11-08 | **534 OK(skip 189)** | **723 OK** |

skip은 Oracle 환경 변수가 없을 때 Oracle 전용 클래스를 건너뛴 수다. 실패 기록 2건(ORACLE-RUNTIME-2 수정 전 코드로 돈 505개, LIVE-VERIFICATION-READY 717개 중 오류 1)은 각 보고서에 공개되어 있고, 이후 전체 회귀(723 OK)로 해소됐다.

### 3.2 이번 감사 재실행 [재확인]

실행: `python -B tests/run_offline.py`(외부 소켓 차단, 운영 SQLite 차단, Oracle 환경 변수 없음 → Oracle 전용 클래스 skip). 출력은 저장소 밖에 저장했다.

| 실행 | 결과 | 소요 |
| --- | --- | --- |
| 기본 전체 스위트 | 530개 실행, **실패 0 · 오류 1** · skip 189, 외부 접속 시도 0 | 2,125초 |
| 오류 난 모듈만 재실행(`test_app_oracle.py`) | 실행 25 · skip 26 · **실패 0 · 오류 0**. 오류가 났던 `MockDatasetOnTarget` 4개 테스트 모두 통과 | 56초 |

- **오류 내용:** `test_app_oracle.MockDatasetOnTarget.setUpClass`에서 `OwnershipRefused('ownership_expired')`가 발생했다(SQLite 대역에서 이관 중 120초 런타임 임대가 만료됨). setUpClass가 실패해 그 클래스의 4개 테스트가 실행되지 않았다. 그래서 534개가 아니라 530개가 실행됐다.
- **판단:** 전체 실행이 평소(기록상 약 272초)보다 약 8배 느렸다. 감사 에이전트 여러 개가 동시에 돌던 부하 상태였다. 같은 모듈을 단독으로 실행하면 통과한다. 따라서 **부하에 따른 임대 만료 타이밍 문제(재현성 낮음)**로 판단한다. 기능 결함으로 보지 않는다. 같은 유형은 [ISSUES_AND_SOLUTIONS.md D4](ISSUES_AND_SOLUTIONS.md)에 이미 기록돼 있다. 다만 이관 경로에 임대 연장이 없어, 느린 환경에서는 같은 오류가 다시 날 수 있다. 이 위험은 남은 과제로 둔다.
- **결론:** 현재 `main`의 기본 스위트 534개는 오류 1건을 단독 재실행으로 해소한 기준으로 모두 통과한다(526 + 4).

Oracle 전체 회귀(MZ_TEST)는 다시 실행하지 않았다. 개발 스키마를 초기화하는 파괴적 테스트(`ORACLE_TEST_ALLOW_RESET=true`)라서 읽기 전용 감사 범위를 벗어나기 때문이다.

## 4. 감사에서 드러난 주의 사항(판정에 반영)

1. **"운영 전환 완료"는 "Oracle이 기록 시스템(SoR)"이라는 뜻이다. "운영 중인 수집 서비스"라는 뜻이 아니다.** 실제 외부 호출은 2026-11-08 08:25 KST, 30초 동안의 7회뿐이다. 그 뒤로는 수집이 없다.
2. **RSS에서 음식이 한 번도 발견되지 않았다**(실데이터 380건 중 FOOD 0). "RSS 발굴 → 자동 추적" 경로는 Mock과 수동 등록으로만 검증됐다. 운영의 유일한 음식 `두쫀쿠`는 사람이 직접 등록한 것이다.
3. **Instagram 토큰은 2026-12-25에 만료된다**(자동 갱신 없음).
4. **SQLite v0 원본은 보존 기한 정리를 할 수 없다.** 코드가 v11을 요구하므로 원본을 열지 못한다.
5. **README의 기술 스택 절이 갱신되지 않았다**("추가 패키지 불필요", "Oracle 드라이버는 향후 과제"). 실제로는 `python-oracledb` 26.1.0을 쓴다.
6. **보고서 폴더 날짜와 실제 작업일이 일부 다르다**(B3, B8, LIVE-VERIFICATION-READY, PRODUCTION-CUTOVER). 날짜는 Git 기준으로 본다.
7. **Instagram 16번 회차가 `running`으로 남아 있다**(SQLite v0, 10-28 05:19Z). 회차 시작 직후 구버전 프로세스가 종료됐고, 자동 `interrupted` 마감 코드는 이 DB에서 다시 실행되지 않았다. [ISSUES_AND_SOLUTIONS.md](ISSUES_AND_SOLUTIONS.md)는 이 사고의 기록을 찾지 못해 미확인으로 남겼다. 실제 DB 상태는 [COLLECTION_IMPLEMENTATION_AUDIT.md §7](COLLECTION_IMPLEMENTATION_AUDIT.md)에 있다.
8. **증거 JSON 일부의 한글이 깨져 있다**(`persistence-after.json`, `final-verify.json`의 음식 이름).
9. Git 이전 단계(2026-10-13~10-26)의 순서는 파일 수정 시각과 DB 타임스탬프로 **추정**한 것이다. 10-17~10-25의 활동은 **미확인**이다.

## 5. 감사 중 환경에 준 영향(투명성 기록)

- Oracle 컨테이너 `mz-oracle-free`: 감사 시작 때 정지 상태였다. `start-oracle.ps1`로 켜서 읽기 전용 SELECT만 실행했고, `stop-oracle.ps1`로 정상 종료했다(데이터 변경 없음).
- SQLite를 `mode=ro`로 열었는데도 WAL 모드의 `-shm` 파일 수정 시각이 바뀌었다(본 파일 해시는 그대로).
- 테스트 출력은 저장소 밖(세션 임시 폴더)에 저장했다. 저장소에 추가한 것은 이 감사 문서 8개뿐이다.
