# MZFoodTraker 기술 문제와 해결 기록 (Issues & Solutions)

작성일: 2026-11-13 KST. 범위: git `main` HEAD `363ea0f`(커밋 71개, 2026-10-27~11-08)와 Git 도입 이전 작업(2026-10-13~10-26, 루트 `*.md`·`data/` 로그·`reports/integrated-audit-2026-10-26`).

**작성 원칙**

- 읽기 전용 감사다. 코드·DB·설정·기존 파일은 고치지 않았고 외부 API·서버·수집기·테스트는 실행하지 않았다.
- 실제 기록(커밋 diff·메시지, 보고서 본문, 증거 JSON·로그, 테스트 이름)으로 확인한 사례만 실었다. 확인하지 못한 세부는 **미확인**으로 적었다.
- Git 도입 전 작업(사례 A1~A7, B1)은 첫 커밋 `97b3c35`(2026-10-27, "initialize MZFoodTraker repository")에 당시 코드 그대로 들어 있다. 그래서 수정 커밋을 따로 지목할 수 없고, 근거는 `97b3c35`와 해당 시점 보고서로 적었다.
- 테스트 결과 수치는 각 보고서의 `test-summary.json`과 커밋 메시지에 기록된 값이다. 이번 감사에서 다시 실행하지 않았다.

---

## 0. 요약표

| # | 사례 | 영역 | 근본 원인 | 해결 | 근거 커밋 |
| --- | --- | --- | --- | --- | --- |
| A1 | RSS 음식 분류 오탐(광주송정역=FOOD, REVIEW 15건) | 데이터 품질 | 배경 뉴스의 음식 단어를 검색어 자체의 음식성으로 옮김. 부분 문자열 매칭(‘어떡하나’→떡) | 검색 대상의 개체 유형과 직접 관계를 요구하는 `target-context-rules-v2`, 분류 이력 보존 | `97b3c35`(사전 작업) |
| A2 | 재시작 뒤 구버전 서버가 응답하고 run 60이 스키마 오류로 실패 | 운영 환경 | Windows에서 같은 포트에 두 서버가 바인딩됨. 구버전의 위치 기반 INSERT가 새 컬럼과 충돌 | 같은 포트 중복 바인딩 차단, 시작 오류 명시. 실패 이력은 보존 | `97b3c35`(사전 작업) |
| A3 | YouTube 화면 수치(9/40)와 보고서(0개)가 다름. 영상 출처 불명 | 데이터 계보 | 실패 시점에 고정된 보고서와 이후 성공 수집의 혼동. 행별 출처(origin) 미기록 | 실행·호출별 계측, `live_api`/`mock`/`legacy_unknown` 구분, `no_request` 상태 | `97b3c35`(사전 작업) |
| A4 | NAVER 진단 요청의 한글 깨짐 | 인코딩 | PowerShell 표준입력으로 한글 검색어를 넘길 때 인코딩이 깨짐 | UTF-8 소스 파일(`naver_verify.py`)로 실행. 깨진 요청은 성공 집계에서 제외 | `97b3c35`(사전 작업) |
| A5 | NAVER 별칭 3개·동일 요청의 중복 전송 위험 | 외부 API 예산 | 같은 주제의 별칭을 따로 보내거나 같은 요청을 반복하면 호출이 중복됨 | 명시적 별칭 1그룹 전송, 정렬된 요청 본문 SHA-256 `naver_guards` 잠금 | `97b3c35`(사전 작업) |
| A6 | Instagram 단기 토큰 만료(10-26 16:00), 교환·적용, 완료 뒤 버튼 잠김 | 인증·UI | 단기 토큰. 수명 분류를 확정하지 못해 교환이 보류됨. 화면이 ‘수집 중’ 상태를 갱신하지 않음 | 명시적 교환 명령과 별도 `.secret` 저장, 승인 뒤 `.env` 토큰만 교체, 7일 전 경고, 상태 갱신과 버튼 연결 | `97b3c35`(사전 작업) |
| A7 | 수집 데이터 중복 저장·중복 집계 | 데이터 무결성 | 반복 수집·여러 검색어·여러 별칭이 같은 콘텐츠를 반환함 | 출처별 자연키, 작업별 수신 영수증, 음식·출처·일자 단위 고유 집계 | `97b3c35`, `59376f2`, `594f47b`, `051e310` |
| B1 | RSS 재수집이 YouTube 후보 연결을 삭제(1→0) | 데이터 무결성 | `INSERT OR REPLACE`가 행을 지웠다가 다시 넣으면서 자식 FK `ON DELETE CASCADE`가 발동 | `INSERT … ON CONFLICT DO UPDATE`(UPSERT) | `97b3c35`(A1~A3 작업) |
| B2 | 상태 조회가 DB에 씀. 버전 없는 마이그레이션 | 아키텍처 | 생성자·GET에 DDL·purge·upsert·재분석이 섞여 있었음 | `Storage.reader()`(mode=ro) 분리, `schema_migrations` 버전 관리 | `97b3c35`(A1~A3 작업) |
| B3 | 식품 브랜드(FOOD_BRAND)가 음식으로 자동 추적될 수 있음 | 도메인 모델 | 분류기가 브랜드도 FOOD로 반환했고 수집기는 classification만 확인 | 독립 유형 gate(`entity_types.can_track_legacy`)와 사람 승인 | `510bf75` |
| C1 | 마지막 시도가 전송 직후 끊기면 ‘원격 중복 가능’이 사라짐 | 실행 신뢰성 | `claim`의 최대 시도 종결 분기가 `SENT→UNKNOWN` 전환보다 먼저 실행됨 | 전환 블록을 종결 분기 앞으로 이동 | `59376f2` |
| C2 | 기존 수집기와 B3 실행기의 이중 쓰기·이중 예산 차감 | 동시성 | 같은 출처를 두 writer가 동시에 쓸 수 있었음 | 출처별 단일 Writer 소유권(`execution_ownership`, migration v7) | `59376f2` |
| C3 | B3 인수인계 뒤 남은 안전 공백 4건 | 실행 신뢰성·보안 | 부분 결과 소실, Instagram 사전 확인 반복, Live 진입점 부재, POST 인증 부재 | coverage 독립 기록(v8), 15분 사전 확인 재사용, fail-closed readiness, 관리자 토큰 | `ea08fe9` |
| D1 | 작업 폴더에 따라 다른 DB를 열거나 새로 만들 수 있음 | 운영 환경 | 기본 DB 경로가 상대경로였고 `--migrate`가 없는 DB를 만들었음 | 프로젝트 루트 기준 경로(`paths.py`), 명시적 생성만 허용 | `8f293ca` |
| D2 | `D:\Users\…` 가짜 사용자 프로필 트리 생성 | 운영 환경 | 브라우저 점검에 상대 출력 경로를 줌 | 빈 디렉터리 9개를 하나씩 비재귀 삭제하고 기록 | `3a9925f` |
| D3 | 거부 응답(401/403/503)이 클라이언트에 도착하지 않음(간헐) | 네트워크(Windows) | 요청 본문을 읽지 않은 채 응답하면 Windows가 연결을 리셋함(WinError 10053) | 응답 전에 POST 본문을 읽음(1MB 상한) | `875997e` |
| D4 | Oracle 컨테이너가 켜져 있으면 기본 테스트가 약 10배 느려지고 임대 만료 오류가 남 | 테스트·동시성 | 부하로 120초 임대가 지나는데 갱신·재참여 경로가 없었음 | 재참여, 쓰기마다 남은 시간 절반 미만이면 연장, 정렬 tie-break | `0f42a3d` |
| D5 | 장시간 Oracle 회귀가 PC 절전·WSL 종료·크래시 복구로 중단 | 운영 환경 | Modern Standby, WSL 유휴 종료, 강제 종료 뒤 인스턴스 복구 | 실행 동안 `SetThreadExecutionState` 래퍼, 컨테이너 세션 유지, 모듈 재검증 | (보고서 기록, 코드 변경 아님) |
| E1 | Oracle 대조가 FK 오류를 측정하지 않고 ‘보장’으로 표시 | 이관 검증 | Oracle 분기에서 고정 문자열을 반환 | 선언된 모든 FK의 고아 행 실측, ENABLED/VALIDATED 확인 | `c6403f7` |
| E2 | Shadow와 Oracle 런타임이 같은 대상에 동시에 쓸 수 있음 | 동시성 | 운영 절차로만 금지했음 | DB 수준 소유 모드·epoch·프로세스 임대(`runtime_ownership`) | `f3a77d1` |
| E3 | Oracle 런타임이 레거시 수집기의 API 사용량을 모름 | 외부 API 예산 | 예산 원장이 SQLite 호출 테이블에만 있었음 | `quota_usage_ledger`, 미확인·15분 넘은 사용량이면 실행 차단 | `0f42a3d` |
| E4 | 만료 근거가 Oracle에서 `OBSERVED 0`으로 바뀜 | 이관 정합성 | B7이 만료 행을 모두 제외하면서 B5의 `EXPIRED` 묘비도 버림 | 원문 없는 링크 행은 묘비 상태로 옮김 | `a5a8152` |
| E5 | Instagram Writer를 영원히 이전할 수 없음. 인계 뒤 복구가 갱신을 빠뜨리고 `converged` 보고 | 이관·복구 | 열린 창에 행이 있는 계정만 원장에 기록. 인계가 파일 지문을 바꿈 | 설정 계정 포함 기록, 인계 전후 지문 사슬 인정 | `86c8209` |
| E6 | SQLite 대역과 실제 Oracle의 동작 차이 | 이식성 | 시간대·TSTZ·UNIQUE 경쟁·DDL 직후 읽기·TSTZ 유일키 제약 | 계약 테스트, UTC 문자열 키, DDL 뒤 대기, 운영 Mock 거부 | `e55f81c`, `eafef71`, `203e67b`, `916d699`, `dea4b33` |
| E7 | SQLite→Oracle 운영 이관 무결성 | 이관 검증 | (결함이 아니라 검증 사례) | 백업→v11 사본→B7 적재→대조 `consistent` | `9307d16`(보고) |

---

## A. 수집·데이터 품질 (Git 도입 이전, 2026-10-13~10-26)

### A1. RSS 음식 분류 오탐과 식재료 누락

**문제 발생.** Google Trends RSS 240개 항목에서 v1 분류기가 장소 `광주송정역`을 FOOD로, 음식과 관계없는 15개를 REVIEW로 분류했다. 실제 식재료인 `호박`은 NON_FOOD였다(`AUDIT_REPORT.md` §1·§4, `data/audit-20261026/final/summary.json`의 `comparison.changes` 17건).

**원인 분석.** `AUDIT_REPORT.md` §4와 `summary.json`의 `before_reasons`에 원인이 기록돼 있다.
- 배경 뉴스와 검색 대상을 구분하지 않았다. 광주송정역은 관련 뉴스 3개 중 2개에 `먹거리`가 있다는 이유로 FOOD가 됐다(`'관련 뉴스 «…남도 먹거리·공연 한자리에»: 먹거리'`).
- 부분 문자열 매칭이 동형어·비유에 반응했다. `레임덕`은 ‘어떡하나’의 ‘떡’, `최지우`는 ‘붕어빵 딸’의 ‘빵’, `아리엘 후라도`는 ‘떡잎’ 때문에 REVIEW가 됐다.
- ‘출시/품절’만 있으면 음식 근거 없이 REVIEW로 올렸다(애플·렉서스·종신보험·여행 상품).

**해결 방법.** `target-context-rules-v2`(`food_trends/classifier.py`, `VERSION = 'target-context-rules-v2'`).
1. 검색어의 개체 유형(장소·조직·인물 직책·비식품 상품·행사)을 먼저 추정한다. 특정 이름 차단 목록은 쓰지 않는다.
2. 검색어와 같은 표현이 뉴스에 실제로 있는지 확인한다(공백 차이만 허용).
3. 검색어에 직접 붙는 음식 정의·속성·식품 출시 연결만 근거로 쓴다. 뉴스에 음식 단어가 있다는 사실만으로는 FOOD를 주지 않는다.
4. 판정 규칙: 직접 근거가 있으면 FOOD, 약한 속성이나 농산물 단서만 있으면 REVIEW, 근거가 없으면 NON_FOOD. UNKNOWN을 자동으로 REVIEW에 올리지 않는다.
5. 저장 구조는 추가형 마이그레이션으로 바꿨다. `classification_history`에 이전 결과를 보존하고, 관측 시점의 `classification_json`과 회차별 분류기 버전을 남긴다. `--reanalyze`는 결과가 바뀔 때만 이력을 추가한다.

**변경된 코드.** `food_trends/classifier.py`, `storage.py`, `service.py`, `api.py`, `__main__.py`, 신규 `food_trends/audit.py`, `tests/test_audit_improvements.py`(`AUDIT_REPORT.md` §10). 변경 전 코드는 `data/audit-20261026/before-classifier.py` 등으로 보존돼 있다. 커밋: `97b3c35`에 포함(사전 작업).

**테스트.** `tests/test_audit_improvements.py::test_regressions_and_novel_names`(확인된 오탐과 미등록 신규 음식명 하위 사례 30개), `test_legacy_migration_preserves_raw_and_old_classification`, `test_agent_labels_cannot_be_used_as_ground_truth`. `data/audit-20261026/tests-final.log`: "Ran 30 tests … OK".

**해결 결과.**

| 같은 스냅샷 | FOOD | REVIEW | NON_FOOD |
| --- | ---: | ---: | ---: |
| 240개 / v1 | 1 | 15 | 224 |
| 240개 / v2 | 0 | 1(호박) | 239 |

과거 관측 payload 420개를 다시 실행했을 때도 FOOD 0, REVIEW 1이었다(`historical-context-review.json`). 정답셋이 없어서 Precision/Recall/F1은 계산하지 않았고 `null`로 표시했다. 사람 검토용 `human-review-template.csv`를 만들었다. 호박은 REVIEW에 머물렀으며 FOOD로 확정되지 않았다.

**기술적 의미.** 설계 결함은 **증거와 대상의 관계(entity linking)를 무시한 키워드 매칭**이었다. 개선 후 분류는 "검색 대상이 무엇인가 → 뉴스가 그 대상을 직접 설명하는가"의 2단계로 판단하고, 규칙 점수는 확률이 아닌 증거 강도 표시로만 쓴다. 분류기가 자기 결과를 정답으로 삼지 않도록 사람 라벨 gate(`--labels`)를 두었다. 대가로 재현율이 낮아질 수 있으며 보고서도 이 한계를 적고 있다(§7).

---

### A2. 같은 포트의 구버전 서버와 run 60 스키마 오류

**문제 발생.** 재시작 검증 중 이전 서버 프로세스가 같은 포트에 남아 구버전 응답을 돌려줬다. 마이그레이션 뒤 구버전 코드의 위치 기반 INSERT가 스키마 오류를 1회 기록했다(run 60, `AUDIT_REPORT.md` §8). 통합 감사도 RSS 실패 5회 가운데 `OperationalError` 1건이 run 60이라고 확인했다(`integrated-audit-2026-10-26/REPORT.md` §3.2 "실패 run60은 HTTP200 뒤 저장 오류").

**원인 분석.** Windows에서는 같은 포트에 서버 두 개가 바인딩될 수 있었다. 컬럼 순서에 의존하는 INSERT(명시 컬럼 없음)는 새 컬럼이 추가되면 깨진다(통합 감사 F11).

**해결 방법.** 같은 포트의 중복 바인딩을 막고 시작 오류를 명시적으로 보여주도록 수정했다. 실패 이력은 지우지 않았고, 트랜잭션 롤백으로 부분 저장이 없었음을 확인한 뒤 새 서버로 다시 수집했다. A1~A3 단계에서는 "구버전 writer를 끈 배포 창에서만 마이그레이션"을 운영 절차로 정했다(`development-a1-a3-2026-10-27/REPORT.md` 실행·배포 절차).

**변경된 코드·테스트.** 서버 기동부(`food_trends/api.py`/`__main__.py`, 사전 작업이라 diff 미확인). 테스트 `tests/test_audit_improvements.py::test_second_server_cannot_bind_same_port`. 커밋 `97b3c35`.

**해결 결과·기술적 의미.** 문제의 핵심은 **프로세스 단일성 보장이 없었던 것**과 **스키마 진화에 취약한 SQL**이었다. 이후 명시적 버전 마이그레이션(B2), 미이관 DB에서는 기동을 거부하는 `MigrationRequired`, 단일 writer 소유권(C2·E2)으로 이어지는 출발점이 됐다.

---

### A3. YouTube 실제 API 연결과 화면 표시 불일치(origin `legacy_unknown` vs `live_api`)

**문제 발생.** UI는 검색 9/40·상세 6/1,000과 영상 253개를 보여줬지만 이전 검증 보고서는 0개였다(`YOUTUBE_VERIFICATION.md`).

**원인 분석.** (`YOUTUBE_VERIFICATION.md` "불일치" 1~6)
1. 15:03경 검증은 샌드박스 연결 거부로 끝났고 검색 3회가 `status=network`, `http_status=NULL`로 남았다.
2. 15:22에 같은 DB로 검색 6회·상세 6회가 HTTP 200으로 성공해 253개가 저장됐다. `data/instagram-hashtags-final-server.err.log`의 `POST /api/youtube/collect HTTP 200`으로 이를 확인했다.
3. 보고서는 실패 시점에 고정돼 있었고, UI는 같은 DB의 캐시를 읽었다. 8000 서버와 8002 조회 서버가 같은 DB를 공유한다는 점도 혼동을 키웠다.
4. 과거 행에는 transport 출처가 기록되지 않아 실제 수집인지 Mock인지 판별할 수 없었다.

**해결 방법.** (`YOUTUBE_VERIFICATION.md` "수정한 결함")
- `youtube_runs`/`youtube_calls`에 실행별 HTTP 코드, 응답 수, 고유 ID, 중복 제거, 신규·갱신 수를 기록한다.
- 기본 공식 transport는 `live_api`, 주입 테스트는 `mock`, 과거 행은 `legacy_unknown`으로 구분한다. 영상별 최신 갱신 출처는 `youtube_video_provenance`에 둔다.
- 외부 요청이 0회인 작업은 `no_request`로 기록해 연결 성공으로 잘못 표시하지 않는다.
- 부분 실패가 나도 앞서 저장한 수를 0으로 보고하지 않는다.
- 최소 verifier(검색 1회와 상세 1회, 최대 5개)는 두 API가 모두 성공하지 않으면 `verification_passed=false`로 끝난다.

**테스트.** `tests/test_youtube.py::test_legacy_http_history_is_not_labeled_live_verified`, `test_run_metrics_match_calls_and_existing_upsert`, `test_partial_failure_keeps_correct_saved_count`, `test_failed_response_counts_include_retries_and_no_http`, `test_verification_minimal_calls_preserve_checkpoint_and_sources`. `data/youtube-audit-tests.log`: "Ran 97 tests … OK".

**해결 결과.** `data/youtube-live-verification.json`: `origin=live_api`, `requests=2`, `saved_videos=5`, `new_videos=0`, `updated_existing_videos=5`, 저장 253→253, `protected_tables_unchanged=true`, `verification_passed=true`. 통합 감사 기준 출처 분포는 live_api 5/253(1.98%), legacy_unknown 248/253(98.02%)이다(F04). 248개를 검증 없이 live로 바꿔 적지 않았다.

**기술적 의미.** 설계 결함은 **데이터 계보(provenance)의 부재**였다. "HTTP 200이 있었다", "DB에 행이 있다", "실제 API에서 왔다"를 구분할 수 없었다. 개선 후 origin은 A2 공통 계약(`contracts.py`의 `live_api/mock/user/legacy_unknown`)으로 일반화됐고, Oracle 운영 단계에서 Mock 결과의 운영 DB 유입을 거부하는 규칙(E6)으로 이어졌다.

---

### A4. 한글 인코딩 문제(NAVER 진단 요청)

**문제 발생.** NAVER DataLab 초기 진단에서 PowerShell 표준입력으로 넘긴 한글 검색어가 깨진 채 전송됐다. 응답은 HTTP 200이었다(`NAVER_LIVE_VERIFICATION.md` 호출 횟수의 구분).

**원인 분석.** 콘솔 표준입력을 거치면서 한글이 다른 코드페이지로 변환됐다. 정확한 코드페이지(cp949 여부)는 **미확인**이다. 보고서는 "PowerShell 표준입력의 한글 인코딩이 깨진" 것으로만 적고 있다.

**해결 방법.** UTF-8 소스 파일인 `food_trends/naver_verify.py`에서 검색어를 읽어 실행하도록 바꿨다. 수정 후 반환 제목과 검색어가 정확한 한글인지 확인했다. 깨진 요청의 메타데이터는 감사 이력으로 남기되 음식 검증 성공 건수에서는 뺐다(HTTP 응답 5건 중 음식 검증 성공 4건). 통합 감사도 성공 7건을 모두 정상 음식 조회로 세면 안 된다고 다시 적었다(`integrated-audit-2026-10-26/REPORT.md` §3.2).

**이후 설계에 반영된 부분.**
- Oracle 계약 테스트 `tests/test_target_compat.py::test_korean_emoji_and_combining_characters_round_trip`(`e55f81c`에서 추가, `eafef71`에서 실제 Oracle로 실행)(한글·이모지·조합형 왕복)와 `test_empty_string_and_null_keep_their_meaning`(G-OPS-0 REPORT §5 "한글·이모지·조합형"). 실제 Oracle 356개 통과(`development-g-ops-0-2026-11-02/oracle-full-tests/test-summary.json`).
- B4 데이터 품질 규칙 `dq-rules-v1`의 "문자열 인코딩" 검사(`051e310`, `development-b4-b5-2026-10-28/REPORT.md` §67).
- 토큰 교체 때 `.env`의 줄바꿈·주석·인코딩을 바이트 단위로 보존(`INSTAGRAM_TOKEN_APPLICATION.md`).

**커밋.** `97b3c35`(naver_verify.py 포함), `e55f81c`·`eafef71`, `051e310`.

**기술적 의미.** 한글이 실행 환경의 입출력 경계(셸·콘솔)를 지날 때 손상될 수 있다는 점을 실제로 겪었다. 이후 프로젝트는 **입력 경로를 UTF-8 파일로 고정**하고, **DB 왕복을 계약 테스트로 고정**하고, **깨진 데이터는 품질 규칙으로 격리**하는 3단계로 대응했다. 오염된 결과도 숨기지 않고 감사 이력으로 남긴 점이 특징이다. 이번 감사 중 콘솔 출력에서 JSON 한글이 깨져 보였는데, 원본 파일 바이트는 정상 UTF-8이었다. 이는 감사 도구 쪽 표시 문제이며 프로젝트 결함이 아니다.

---

### A5. NAVER DataLab 별칭·동일 요청 중복 전송 방지(`naver_guards`, fingerprint)

**문제 발생.** 실제 사고가 아니라 설계 단계에서 다룬 위험이다. 두쫀쿠의 별칭 3개(`두쫀쿠`, `두바이 쫀득 쿠키`, `두바이쫀득쿠키`)를 따로 보내면 호출이 3배가 되고, 별칭 선택만 바꿔 같은 요청을 다시 보내거나 서버·CLI가 동시에 보내면 일일 한도(자체 100회)를 낭비한다. 실제 중복 전송이 일어났다는 기록은 **없다**.

**해결 방법.** (`food_trends/naver_service.py`)
- `ALIASES`의 명시적 정확 별칭만 사용하고, `keyword_group()`이 이를 한 `groupName`으로 묶어 한 그룹으로 전송한다. 퍼지 매칭은 하지 않는다.
- `_query()`가 `hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False))`로 요청 지문을 만들고, `BEGIN IMMEDIATE` 트랜잭션에서 `naver_guards`를 조회해 유효한 잠금이 있으면 `NaverError('duplicate')`를 낸다. 실행 중 잠금은 600초, 완료 뒤 쿨다운은 기본 60초다.
- KST 일일 예약이 재시도를 포함해 DB에 기록되므로 서버·CLI·재시작 사이에도 한도가 합산된다.

**테스트.** `tests/test_naver.py::test_aliases_exact_dedup_and_food_only`, `test_explicit_keywords_without_rss_food_and_alias_dedup`, `test_duplicate_persists_across_service_instances`, `test_concurrent_query_is_not_duplicated`, `test_daily_budget_includes_retries_and_survives_restart`.

**해결 결과.** 실환경에서 재조회하면 409 안내가 나오고 외부 요청은 늘지 않았다. 별칭 3개는 한 그룹으로 전송·반환됐다(`NAVER_LIVE_VERIFICATION.md` 표). 커밋 `97b3c35`.

**기술적 의미.** 외부 API 호출을 **멱등 키(정규화된 요청 본문 해시)로 직렬화**한 사례다. 응답 저장 권한이 확인되지 않아 캐시를 쓸 수 없는 조건에서 응답 대신 *요청* 단위로 중복을 막았다. 이 방식은 B2 query planner의 "공유 Query 1회 실행"과 B3 예산 예약으로 일반화됐다(`test_shared_actual_query_searches_once_and_filters_stopped_target`, `59376f2`).

---

### A6. Instagram 액세스 토큰 만료·교환·적용과 ‘running’ 상태 처리

**문제 발생.**
1. 실연동 시점의 토큰은 **2026-10-26 16:00 KST에 만료되는 단기 USER 토큰**이었다(`INSTAGRAM_LIVE_VERIFICATION.md`).
2. 발급 시각이 오지 않아 수명 구간(단기/장기)을 확정할 수 없었고, 첫 구현의 교환 명령은 이때 교환을 보류했다("실제 장기 토큰 적용은 남은 운영 과제").
3. 실제 화면에서 수집 중 상태를 읽은 상세 화면의 버튼이 완료 뒤에도 잠겨 있었다.

**원인 분석.** 토큰 수명은 Meta `debug_token` 응답을 기준으로 해야 하는데, 문자열 길이로 추정하지 않도록 한 보수적 규칙 때문에 교환 경로가 막혔다. 버튼 잠김은 `static/instagram.js`가 상태를 갱신할 때 버튼 활성화와 연결하지 않았기 때문이다.

**해결 방법.**
- 토큰 검사·교환의 비밀값은 URL이 아닌 POST 본문으로 보내고(`instagram_graph.py`), 원본 오류와 토큰은 출력하지 않는다.
- 규칙을 보완했다. 수명을 확정할 수 없어도 **유효한 만료 예정 USER 토큰**이면 명시적 교환을 요청할 수 있다. 교환 결과는 다시 검사하고, 실패하면 새 비밀 파일을 지운다(`INSTAGRAM_HASHTAG_ANALYSIS.md` 토큰 관리 검증).
- 새 토큰은 `.secret` 파일에만 저장하고 `.env`에 자동 반영하지 않는다. 사용자 승인 뒤 `.env`의 `INSTAGRAM_ACCESS_TOKEN` 값만 교체했고, 나머지 바이트는 그대로임을 비교로 확인했다. 원본은 `.secret`으로 백업했다.
- 만료일을 `instagram_state`에 저장하고, 서버가 외부 호출 없이 남은 일수를 계산해 **7일 전부터** 경고(`role=alert`)를 띄운다. 만료되면 자동 수집을 막는다.
- 상태 갱신과 버튼 활성화를 연결했다.

**적용 흐름(`INSTAGRAM_TOKEN_APPLICATION.md`).** 실제 HTTP 5회(토큰 검사 1, 계정 연결·예산 2, 기존 해시태그 1, 1페이지 수집 1) → 서버 재시작 뒤 자격증명 지문 일치·`unchecked` 확인 → 토큰 검사 → 만료일 **2026-12-25 09:55:03 KST** → 50건 수집(신규 2·중복 48) → 72건·185태그, `integrity_check=ok`.

**‘running’ 상태 회차.** `instagram_runs` 회차가 `running`으로 영구히 남은 사고는 기록에서 확인하지 못했다(**미확인**). 확인된 내용은 다음과 같다.
- A1~A3 보존 점검에서 운영 서버의 병행 수집으로 `running` 회차와 lease 1건이 생긴 것이 관측됐다(`development-a1-a3-2026-10-27/preservation-check.json`의 `original_database_changed_tables: instagram_runs, instagram_leases`). 다음 단계 기준선에서는 running 0이었다(`development-a4-a5-2026-10-27/baseline.json` `running.instagram_runs: 0`).
- 통합 감사 시점에는 "모든 출처의 미완료 회차/시도는 현재 0"이었다.
- 설계상 대응은 갖춰져 있다. 요청마다 갱신하는 5분 DB lease가 있고, 비정상 종료 뒤 lease가 만료되면 다음 수집이 남은 회차를 `interrupted`로 마감한다(`food_trends/instagram_storage.py`: `UPDATE instagram_runs SET status='interrupted'…`). 테스트는 `tests/test_instagram.py::test_leases_cross_process_and_crash_recovery`다.
- Oracle 단계에서는 RSS도 같은 원칙을 쓴다. 10분 넘은 미완료 회차는 `interrupted`로 마감하고 B4에 `NONE`으로 적재한다(`f88c94c`, `test_rss_schedule_revocation_and_interrupted_runs`). running으로 남은 레거시 호출은 사용량 계산에서 `LEGACY_SENT_UNCERTAIN`으로 보수적으로 포함한다(`0f42a3d`).

**테스트.** `tests/test_instagram.py::test_token_expiry_warning_is_local_and_tracks_time`, `test_token_mismatch_missing_permissions_and_expiry_block_exchange`, `test_connection_token_check_and_exchange_never_replace_env`, `test_connection_error_and_token_change_clear_stale_expiry`. 토큰 적용 시점 전체 91개 통과, Chrome 경고 표시 검증 통과.

**기술적 의미.** 자격증명 수명 관리를 **명시적 운영 명령 + 사람 승인 + 비밀값 격리 + 로컬 계산 경고**로 설계했다. 자동 갱신은 공식 문서를 확인하지 못해 구현하지 않았다(`INSTAGRAM_INTEGRATION.md`). 중단된 작업은 "lease 만료 → interrupted"로 수렴시켜 상태 기계가 막다른 상태에 머물지 않게 했다.

---

### A7. 수집 데이터 중복 저장·중복 집계

**문제 발생.** 반복 수집이 같은 콘텐츠를 다시 반환했다(Instagram 재조회 50건 중 50건 중복, 토큰 적용 때 50건 중 48건 중복). YouTube는 고유 영상 253개에 검색 관계가 277개여서 단순 합산하면 24건이 과대 집계된다(통합 감사 F05).

**원인 분석.** 같은 영상이 여러 검색어·별칭에 일치하고, 같은 게시물이 여러 해시태그에 속할 수 있다. 콘텐츠와 검색 관계를 같은 단위로 세면 중복이 생긴다.

**해결 방법(단계별).**
1. 사전 작업(`97b3c35`): RSS는 source URL·원검색어·UTC 게시 시각·링크로 SHA-256 ID를 만들고 `(run_id,item_id)` PK를 둔다. Instagram은 게시물 전역 ID PK와 `(태그,게시물)` PK, YouTube는 영상 ID PK와 `(query,video)` PK를 쓴다.
2. B3(`59376f2`): `execution_item_receipts`로 작업별 수신 영수증을 남겨 재전달·재시도에서도 논리 결과가 1건이다.
3. B4(`051e310`): 결정적 키로 재적재해도 변화가 없다(no-op).
4. B5(`594f47b`): 음식·출처·일자 단위로 고유 콘텐츠를 집계하고, 주·월 값은 일별 합이 아닌 기간 집합에서 다시 계산한다.

**테스트.** `tests/test_instagram.py::test_pagination_dedup_cache_metrics_and_privacy`, `test_invalid_media_is_not_miscounted_as_duplicate`; `tests/test_execution_sources.py`(59376f2) `test_duplicate_video_pages_have_one_item_and_one_job_receipt`, `test_response_crash_then_retry_has_one_durable_source_item`; `tests/test_ingestion.py::test_same_video_from_three_alias_queries_is_one_item_with_three_run_links`; `tests/test_food_observations.py::test_two_aliases_one_food_and_one_item_counted_once`.

**해결 결과.** 통합 감사 기준 ID 중복 저장은 RSS 0/264, Instagram 0/72, YouTube 0/253이었다. B4~B5 Mock 전체 경로에서 YouTube 검색 근거 3개는 고유 영상 1개, Instagram 근거 2개는 고유 게시물 1개, RSS 언급 3개는 고유 콘텐츠 1개로 집계됐고, 재전달 뒤 HTTP 0·행 변화 0이었다(`development-b4-b5-2026-10-28/REPORT.md` §9). B4~B5 최종 280개 통과.

**기술적 의미.** **콘텐츠(item), 검색 근거(match), 음식 언급(mention), 관측(metric)을 별도 키로 분리**한 것이 핵심이다. "검색 결과에 나왔다"를 "음식이 언급됐다"와 같게 취급하지 않는 데이터 모델로 바뀌었다.

---

## B. 저장 무결성과 아키텍처 경계 (A1~A5 단계, 2026-10-27)

### B1. RSS 재수집이 YouTube 후보 연결을 삭제(F01)

**문제 발생.** 통합 감사가 임시 DB에서 재현했다. FOOD 후보 → YouTube `sync_queries` → 같은 RSS 재수집을 거치자 `youtube_candidate_foods` 연결이 **1→0**이 됐다(`integrated-audit-2026-10-26/additional-verification.json`의 `candidate_replace_cascade`: before 1, after 0, `production_loss_observed: false`).

**원인 분석.** `storage.py`의 `INSERT OR REPLACE candidates`가 충돌 시 기존 행을 **삭제한 뒤 다시 삽입**하므로, 자식 테이블의 `ON DELETE CASCADE`가 발동했다(cause: "INSERT OR REPLACE candidates + ON DELETE CASCADE youtube_candidate_foods").

**해결 방법.** `Storage._classify`를 `INSERT … ON CONFLICT(item_id) DO UPDATE SET …`로 바꿨다. 후보 ID와 자식 FK가 유지되고, 분류 내용·버전이 바뀔 때만 이력을 추가한다(`development-a1-a3-2026-10-27/REPORT.md` A1).

**변경된 코드.** `food_trends/storage.py` `_classify`(`97b3c35` 시점 `storage.py` 39행에 `ON CONFLICT(item_id) DO UPDATE` 존재). Git 저장소가 A1~A3 작업 도중 생겼기 때문에 첫 커밋 `97b3c35`에 포함됐다.

**테스트.** `tests/test_foundations.py::test_a1_repeated_collection_and_reanalysis_preserve_links`, `test_a1_version_and_classification_changes_keep_id_and_history`, `test_a1_new_candidate_still_created`. A1~A3 전체 114개 통과, 외부 접속 0(`development-a1-a3-2026-10-27/test-summary.json`).

**해결 결과.** 반복 수집·재분류·버전 변경 뒤에도 연결은 1→1이었다.

**기술적 의미.** SQLite의 `REPLACE`가 *갱신*이 아니라 *삭제+삽입*이라는 의미 차이 때문에 생긴 **참조 무결성 함정**이었다. 운영 데이터 손실이 생기기 전에 재현 테스트로 잡았고, Oracle 이식 전에 고쳐야 할 P0로 분류했다.

### B2. 조회 경로의 쓰기 부작용과 버전 없는 마이그레이션(F09·F11)

**문제 발생.** `NaverService.status()`가 대상을 upsert했고, `YouTubeStore.__init__/status`가 purge를 실행했으며, `HashtagAnalysis.__init__()`가 캡션을 재분석했다. 마이그레이션은 생성자의 `CREATE TABLE IF NOT EXISTS` + `ALTER`로만 이루어졌다(통합 감사 §2.2, F09/F11).

**해결 방법.** 상태·목록·내보내기는 `Storage.reader()`(`mode=ro`, `query_only=ON`)를 쓰고, 생성자에서 DDL·정리·재분석을 제거했다. 마이그레이션은 `schema_migrations(version,name,applied_at)`에 기록하고 `--migrate`로만 실행하며, 실패하면 롤백하고 다운그레이드는 거부한다(`food_trends/migrations.py`, `migration_definitions.py`).

**테스트.** `tests/test_foundations.py::test_all_get_routes_are_readonly_even_with_pending_targets_and_expired_data`, `test_all_constructors_and_direct_status_are_pure_reads`, `test_explicit_migration_is_idempotent_and_rolls_back_failed_step`. 커밋 `97b3c35`.

**기술적 의미.** **명령-조회 분리(CQS)**를 DB 연결 수준에서 강제했다. 덕분에 이후 모든 감사·점검이 운영 DB를 `mode=ro`로 안전하게 열 수 있었다.

### B3. 식품 브랜드가 음식으로 자동 추적될 위험(F03)

**문제·원인.** `classifier.py`가 FOOD_BRAND도 `FOOD`로 반환했고, NAVER·YouTube 대상 선정은 `classification`만 확인했다(통합 감사 F03). 운영 양성이 0이라 실제 오추적은 관측되지 않았다.

**해결.** `food_trends/entity_types.py`(`can_track_legacy`)로 브랜드·매장·비음식 유형을 추적 대상에서 뺐고, 사람 승인으로도 브랜드를 음식으로 승격할 수 없게 했다(`development-a4-a5-2026-10-27/REPORT.md` 49·83행). `naver_service.py`의 `targets()`/`sync_targets()`에 `can_track_legacy(entity_type)` 조건이 있다. 커밋 `510bf75`, A4~A5 신규 27개 테스트 중 "브랜드/매장 gate" 포함.

**기술적 의미.** 분류 결과(label)와 개체 유형(type)을 **서로 독립된 gate**로 분리했다.

---

## C. 추적 실행 신뢰성 (B3, 2026-10-27~10-28)

### C1. 마지막 시도가 전송 직후 끊기면 ‘원격 중복 가능’이 사라짐

**문제 발생.** B3 인수인계 감사의 요구사항 대조에서 "원격 중복 불명을 숨기지 않음"이 **FAIL**이었다(`development-b3-2026-10-27/REPORT.md` 수정한 결함). 재현은 두 경우였다. ① `max_attempts=1`에서 첫 시도가 중단된 경우, ② 기본 3회 중 앞의 두 번은 깨끗하게 실패하고 마지막 시도만 중단된 경우. 두 경우 모두 작업은 `FAILED/MAX_ATTEMPTS`인데 호출은 `SENT`로 남고 `remote_duplicate_possible=0`이었다.

**원인 분석.** `food_trends/execution.py`의 `claim`에서 최대 시도 초과 종결 분기가 `SENT→UNKNOWN` 전환 블록보다 **먼저** 실행됐다.

**해결 방법.** 전환 블록을 종결 분기 앞으로 옮겼다. 이 시점에는 이전 소유자의 lease가 이미 만료돼 있으므로 안전하다.

**변경된 코드.** `food_trends/execution.py` `claim`, 커밋 `59376f2`(커밋 메시지 "fix: a final attempt interrupted after send intent was finalised as a certain failure…").

**테스트.** `tests/test_execution.py::test_interrupted_send_on_final_attempt_is_not_hidden_when_job_fails`, `test_final_attempt_crash_after_clean_failures_keeps_uncertainty`. 수정 전 243개, 수정 후 245개 통과(`development-b3-2026-10-27/final-tests/test-summary.json`).

**해결 결과.** 수정 후 호출은 `UNKNOWN`, 작업은 `remote_duplicate_possible=1`이 된다.

**기술적 의미.** 분산 시스템의 **"요청은 보냈지만 결과를 모름"(at-least-once 불확실성)**을 상태 기계에서 잃어버리는 순서 버그였다. 불확실성은 사용량에도 보수적으로 집계된다(ATTEMPTED 자동 환불 금지).

### C2. 기존 수집기와 B3 실행기의 이중 쓰기·이중 예산 차감 방지(단일 Writer 소유권)

**문제.** B3 실행기가 기존 NAVER·YouTube·Instagram 클라이언트에 연결되면, 같은 출처를 레거시 스케줄러와 B3가 동시에 호출해 한 시도가 두 번 차감되거나 결과가 이중 저장될 수 있었다.

**해결.** migration v7의 출처별 단일 Writer 소유권(`food_trends/execution_ownership.py`). 소유권이 이전된 출처에서는 레거시 writer를 거부하고(`naver_service._query`의 `legacy_allowed(...)` → `NaverError('duplicate')`), 어댑터에서는 레거시 예약·완료를 no-op으로 둔다. RSS는 기존 스케줄러가 계속 소유한다. Live·자동 실행은 기본적으로 꺼져 있다. 커밋 `59376f2`.

**테스트.** `test_ownership_is_read_only_and_default_legacy`, `test_default_live_runtime_is_closed`, `test_late_source_response_cannot_commit_after_lease_takeover`, `test_live_execution_default_disabled_and_rss_remains_legacy_owned`(`cca7ee7`에서 추가된 기반 테스트). 245개 통과, 복제 DB 0→7 마이그레이션에서 원래 30개 테이블 보존.

**기술적 의미.** "누가 쓰는가"를 코드 관례가 아니라 **DB에 저장된 소유권 상태**로 만들었다. 이 개념은 Oracle 런타임 소유 모드(E2)와 Writer 이전·철회(E5)로 확장됐다.

### C3. B3 인수인계 뒤 남은 안전 공백 4건

**문제 발생.** B3 보고서 "후속 작업" 표가 네 가지 공백을 기록했다(`development-b3-2026-10-27/REPORT.md`).
1. 페이지를 커밋한 뒤 중단이 반복돼 `claim`이 종결하면 `FAILED/MAX_ATTEMPTS`만 남고 결과 계약·대상 매핑이 없었다.
2. Live Instagram은 재시도마다 `recently_searched_hashtags` 사전 확인(최대 HTTP 3회)을 반복했다.
3. `live_controller`가 CLI나 배포 절차에 연결돼 있지 않았다.
4. `/api/execution/*` POST는 로컬 호스트·same-origin·크기만 확인했고 `actor_ref`는 신원 인증이 아니었다.

**해결 방법.** 커밋 `ea08fe9` 메시지.
1. migration v8: 모든 종결 경로에서 실행 상태와 별개로 결과 범위(COMPLETE/PARTIAL/LIMITED/NONE), 커밋한 단계, 재개 상태, 원격 중복 플래그를 기록한다.
2. 이 작업이 이미 태그를 해석했다면 사전 확인을 생략한다(`recent_media`는 7일 고유 태그 예산을 소비하지 않음). 같은 계정·API·정책·태그 지문 아래에서 15분만 재사용하고, 실패 결과는 캐시하지 않는다.
3. 읽기 전용 fail-closed readiness(스키마, `B3_LIVE_ENABLED`, registry 승인, 자격증명, 레거시 스케줄러 off, 소유권 이전, 계정 차단 없음), CLI `--execution-readiness`, `--execution-mode preview|live`.
4. 루프백 바인드만 허용, 모든 요청에서 Host 검사, 상태를 바꾸는 POST에는 프로세스별 관리자 토큰.

**변경된 코드.** `food_trends/execution.py`, `execution_runtime.py`, `execution_sources.py`, `execution_result_schema.py`, `api.py`, `__main__.py`, `static/admin.js`.

**테스트.** `tests/test_execution_handoff.py`의 11개: `test_partial_pages_then_repeated_worker_crash_keeps_committed_data_and_lineage`, `test_failure_before_any_page_reports_no_committed_data`, `test_limited_and_complete_coverage_are_distinct_from_status`, `test_resume_after_lookup_does_not_repeat_usage_check`, `test_failed_usage_check_is_not_cached`, `test_live_readiness_is_fail_closed_and_read_only`, `test_cli_mode_selection_refuses_unsafe_live_configuration`, `test_state_changing_requests_need_the_process_admin_token`, `test_server_refuses_non_loopback_bind_and_weak_tokens` 등. 결과: 256개 통과(기준 245 + 신규 11).

**기술적 의미.** **실행 상태(status)와 데이터 범위(coverage)를 분리**해 "실패했지만 일부 데이터는 유효"를 표현할 수 있게 됐다. Live 진입은 **fail-closed**(조건 하나라도 불명이면 거부)로 바뀌었다.

---

## D. 실행 환경·운영 문제 (Windows·WSL·테스트)

### D1. 작업 폴더에 따라 다른 DB를 열거나 새 DB를 만드는 문제(결정적 DB 경로)

**문제 발생.** STORAGE-1 감사: "기본 DB 경로가 상대경로다. `run.ps1` 밖, 예를 들어 다른 폴더에서 `python -m food_trends --migrate`를 실행하면 그 위치에 새 DB가 생길 수 있다"(`reports/storage-1-2026-11-02/STORAGE_AUDIT.md` 52행). `run.ps1`은 작업 디렉터리를 바꾸므로 평소에는 드러나지 않았다.

**해결 방법.** 커밋 `8f293ca`.
- `food_trends/paths.py`가 기본 DB(`data/trends.sqlite3`)와 `.env`를 프로젝트 루트에 고정한다. 명시적 `DATABASE_PATH`는 절대경로면 그대로, 상대경로면 루트 기준으로 해석한다.
- `migrate()`는 없는 DB를 암묵적으로 만들지 않는다. `create=True` 또는 `--migrate --create-database`일 때만 만들고, `--create-database` 단독은 거부한다.
- `--migration-status`/`--migrate`는 절대경로, 존재 여부, 이관 전 스키마 버전을 보여준다.

**테스트.** `tests/test_paths.py`의 `test_default_database_is_the_same_from_any_working_directory`, `test_explicit_absolute_and_relative_paths`, `test_cli_refuses_to_create_a_missing_database_implicitly`, `test_cli_creates_a_new_database_only_when_asked`, `test_library_creation_is_explicit_too`, `test_env_file_is_read_from_the_project_not_the_working_directory`. 결과: 기본 330개 통과(Oracle 32개 skip), 운영 SQLite 변경 없음(v0).

**기술적 의미.** 실행 위치라는 **숨은 입력**을 제거했다. "없으면 만든다"는 편의 동작이 잘못된 경로에서 빈 DB를 운영 DB로 착각하게 만드는 위험을 없앴다.

### D2. 상대 출력 경로로 생긴 `D:\Users\…` 트리 정리

**문제 발생.** ORACLE-BACKEND-COMPLETION-1의 관리 화면 점검을 상대 출력 경로로 실행하는 바람에 `D:\Users\gjgjs\AppData\Local\Temp\claude\…\scratchpad\browser1`까지 사용자 프로필처럼 보이는 디렉터리 9개가 생겼다(파일 0개, 생성 2026-11-05 19:45:52).

**해결 방법.** 각 디렉터리가 비어 있고 생성 시각이 점검 시각과 같은지 확인한 뒤, 가장 깊은 곳부터 `[System.IO.Directory]::Delete(path, $false)`로 하나씩 비재귀 삭제했다. `Remove-Item -Recurse -Force`는 쓰지 않았다. `D:\Users` 자체와 `data\b8-app-api-worktree-logs`는 남겼다. 처음 시도한 경로 계산 스크립트는 PowerShell 5.1의 `Split-Path -LiteralPath … -Parent` 매개변수 집합 오류로 삭제 전에 멈췄고, 삭제 전후 개수를 비교해 아무것도 지워지지 않았음을 확인했다(`reports/development-oracle-live-integration-2026-11-05/CLEANUP.md`). 커밋 `3a9925f`.

**재발 기록.** 다음 단계에서도 같은 유형으로 `D:\c\Users\…\scratchpad\a-unit\`(테스트 로그 2개)가 생겼다. 자동 삭제는 안전 정책으로 거부돼 사용자가 직접 지우도록 남겼다(`development-oracle-live-verification-ready-2026-11-06/REPORT.md` 154행). 현재 존재 여부는 이번 감사에서 확인하지 않았다(**미확인**).

**기술적 의미.** 코드 결함이 아니라 **도구 사용 결함**이다. 정리 과정에서 파괴적 재귀 삭제를 피하고, 검증된 고정 목록과 전후 개수로 증명했다. 같은 유형이 재발했으므로 출력 경로를 절대경로로 쓰는 운영 규칙이 필요하다는 교훈이 남는다.

### D3. Windows에서 POST 거부 응답이 연결 리셋으로 사라짐

**문제 발생.** 기본 회귀에서 `test_state_changes_stay_guarded_and_errors_are_reported`가 간헐적으로 오류를 냈다(`development-oracle-production-cutover-2026-11-07/REPORT.md` §2, §6 "직전 1차 실행의 간헐 오류 1건(관리 서버 연결 리셋)").

**원인 분석.** 서버가 요청 본문을 읽지 않은 채 401·403·503 같은 조기 거부를 보내면, Windows가 읽지 않은 바이트가 남은 소켓을 리셋한다. 그러면 클라이언트는 `WinError 10053`을 받고 거부 응답 자체를 잃는다. 600KB 거부 요청 10회 중 4회 실패로 재현했다.

**해결 방법.** `food_trends/api.py`에 `_read_body()`를 추가하고 `do_POST()`가 맨 처음 호출하게 했다. `Content-Length`가 0 초과 1MB 이하이면 본문을 다 읽어 `io.BytesIO`로 바꾼다(HTTP/1.0: 연결당 요청 1개). 크기 검사는 각 핸들러가 계속 한다. 커밋 `875997e`.

**테스트.** `tests/test_admin_oracle.py::test_a_refusal_reaches_the_client_even_over_a_large_body`: 600,000바이트 본문의 POST를 토큰 없이 10회 보내 `[401] * 10`을 확인한다. 결과: 10회 중 실패 4 → 0. 최종 기본 534개 통과(Oracle 189 skip), 실제 Oracle 723개 전부 통과.

**기술적 의미.** 표준 라이브러리 HTTP 서버의 조기 응답이 **OS별 TCP 동작**(읽지 않은 수신 버퍼가 있는 상태에서 close하면 RST)과 만나 생긴 간헐 결함이다. 보안 거부(인증 실패)가 클라이언트에 확실히 전달되는 것도 보안 경계의 일부라는 점을 보여준다. 간헐 오류를 무시하지 않고 재현율(4/10)을 측정한 뒤 고쳤다.

### D4. Oracle 컨테이너가 켜진 상태의 테스트 지연과 임대 만료 오류

**문제 발생.** (`development-oracle-runtime-2-2026-11-04/REPORT.md` 79~87행)
- 기본 전체 회귀가 Oracle 컨테이너를 정지하면 **272초**, 켠 채 실행하면 **2,797초**였다.
- 이렇게 느려진 조건에서 오류가 세 번 났다.
  1. R4 첫 실제 Oracle 회귀(503개): 유휴 런타임의 소유권 임대(120초)가 테스트 도중 끝났고, "만료 임대 갱신 금지" 규칙 때문에 다음 쓰기가 실패했다.
  2. 두 번째 회귀(505개): 같은 시각·지표에 `context_hash`만 다른 행의 조회 순서가 정해져 있지 않아 전체 경로 테스트가 대역·Oracle 양쪽에서 실패했다.
  3. 세 번째 회귀의 기본 실행: Shadow 전달 한 번이 120초를 넘겨 `ownership_expired`가 났다. Shadow 전달과 B7 적재에는 임대 갱신이 없었다.

**해결 방법.** 커밋 `0f42a3d`에 포함했다("Fixes found by the R4 regressions").
1. 만료된 임대는 되살리지 않고, 같은 프로세스가 **새 임대로 다시 참여**한다. 다른 모드가 소유 중이면 참여 자체를 거부한다.
2. 음식별 지표 조회에 `context_hash` 정렬을 추가했다. 값·집계는 바뀌지 않는다.
3. `guard()`는 **아직 만료되지 않은** 임대의 남은 시간이 절반 미만이면 같은 쓰기 트랜잭션 안에서 연장한다. 이미 만료된 임대는 계속 거부한다.

운영 규칙도 바꿨다. 이후 보고서들은 "기본 실행 중에는 Oracle 컨테이너를 정지"했다(`development-b8-integration-2026-11-05/REPORT.md` 87행 등).

**테스트.** `tests/test_runtime_ownership.py::test_idle_runtime_rejoins_with_a_new_lease_in_the_same_mode`, `test_long_shadow_delivery_keeps_its_lease`(전달 중 이벤트마다 시계를 40초씩 진행, 임대 60초). 결과: 보완 뒤 소유권 확인 쓰기를 거치는 10개 모듈을 다시 실행해 대역 79개, 실제 Oracle 환경 156+22개 통과(`oracle-post-fix-tests/test-summary.json`). 기본 전체 403개 통과.

**기술적 의미.** 테스트 환경의 부하가 **시간 기반 안전장치(lease TTL)의 경계 조건**을 드러냈다. 해결 방식은 TTL을 늘리는 대신 "살아서 계속 쓰는 참여자는 연장하고, 죽은 참여자의 만료는 되돌리지 않는다"는 의미론을 명확히 한 것이다. 펜싱 원칙은 그대로 지켰다. 비결정적 정렬도 함께 드러나 고쳤다.

### D5. 장시간 Oracle 회귀를 끊은 PC 절전·WSL 유휴 종료·크래시 복구

**문제 발생과 원인.**
- **Modern Standby**: ORACLE-WRITER-RELEASE-1의 실제 Oracle 전체 회귀 1차가 711개 중 710개 통과, 오류 1(14,538초)이었다. Windows Kernel-Power 이벤트 506/507에 따르면 PC가 2026-11-05 23:33:13~11-06 03:10:58 동안 Modern Standby에 들어가 테스트 프로세스와 WSL2 Oracle 컨테이너가 함께 멈췄다. 실행 로그의 공백(23:29:46~03:11:23)과 일치한다. 깨어난 직후 진행 중이던 `test_oracle_scheduling` Oracle 사례의 연결이 끊겼다(`development-oracle-writer-release-2026-11-05/REPORT.md` 92~97행, `oracle-full-tests-interrupted/test-summary.json`: errors 1).
- **WSL 유휴 종료**: G-OPS-0 검증 중 연결된 세션이 없자 WSL이 배포판을 종료했고 Oracle 접속이 거부됐다(`development-g-ops-0-2026-11-02/REPORT.md` 105행).
- **인스턴스 크래시 복구**: 직전 회귀가 작업 세션 종료로 강제 종료된 뒤, 재시작한 인스턴스가 크래시 복구를 하는 동안 첫 `reset_target_schema`가 `database_error`를 냈다(`development-oracle-live-verification-ready-2026-11-06/REPORT.md` 125행). 그 회귀는 717개 중 716개 통과였다.

**해결 방법.**
- 테스트 실행 동안만 유휴 절전 진입을 막는 `SetThreadExecutionState` 래퍼(프로세스가 끝나면 해제)로 다시 실행했다. 2차는 **711개 전부 통과**(1,352초). 1차와 2차 요약을 모두 남겼다.
- `podman start --attach mz-oracle-free`를 백그라운드로 유지했다. 운영 단계에서는 `start-oracle.ps1`이 연결된 세션을 유지한다(production-cutover REPORT §7).
- 크래시 복구 직후 오류는 해당 모듈만 다시 실행해 `test_admin_oracle` 8/8 통과로 확인했다(`oracle-recheck-admin/test-summary.json`).

**코드 변경.** 없다. 래퍼는 저장소 코드가 아닌 실행 절차로 보고서에 기록됐다. 커밋 `471f56f`(writer-release 보고), `bb0eaf9`(live-ready 보고).

**기술적 의미.** 오류를 코드 결함으로 착각하지 않도록 **OS 이벤트 로그와 테스트 로그의 시간 공백을 교차 검증**해 환경 원인을 증명했다. 중단된 실행 결과를 지우지 않고 재실행 결과와 함께 남겼다. 운영 전환 회귀(723개)는 실행 중 PC가 09:05~18:06 절전에 들어갔는데도 오류 없이 끝났다(production-cutover REPORT §6). 다만 절전이 실행 시간을 34,043초로 늘렸다.

---

## E. SQLite → Oracle 이관 (B6~ORACLE-PRODUCTION-CUTOVER, 2026-11-02~11-08)

### E1. Oracle 대조가 FK 오류를 측정하지 않음

**문제 발생.** G-OPS-0에서 실제 Oracle로 B7 이관·대조를 처음 실행했을 때, 대조 함수가 Oracle에서는 FK 오류를 세지 않고 `'enforced_by_deferred_constraints'`라는 고정 문자열을 반환하고 있었다. 일관성 판정도 이 문자열을 통과로 인정했다(`c6403f7` diff, G-OPS-0 REPORT §6·§7).

**원인 분석.** SQLite는 `PRAGMA foreign_key_check`로 실측했지만, Oracle 분기는 "지연 제약이 보장한다"고 **가정**했다.

**해결 방법.** `food_trends/target_migration.py` `reconcile()`.
- 두 방언 모두 선언된 모든 FK에 대해 이식 가능한 SQL(`SELECT count(*) FROM child c WHERE <fk not null> AND NOT EXISTS (SELECT 1 FROM parent p WHERE …)`)로 고아 행을 센다.
- Oracle에서는 `user_constraints`의 `constraint_type='R'` 제약이 모두 `ENABLED`·`VALIDATED`인지도 확인한다.
- `consistent` 조건은 `foreign_key_errors == 0`이면서 `foreign_keys_not_enabled_validated`가 0일 때만 참이다.
- 함께 고친 결함: 픽스처 생성기의 빈 `runTest`가 테스트로 발견돼 테스트 수가 1개 부풀려졌다.

**테스트.** `tests/test_target_migration.py::test_orphan_rows_are_counted_and_break_consistency`(SQLite에서 실제 고아를 만들어 측정이 공허하지 않음을 증명). B7 이관 테스트에 실제 Oracle 클래스 8개 추가. 결과: 실제 Oracle 포함 356개 실패 0, 기본 324개(Oracle 32 skip).

**해결 결과.** 운영 이관에서 FK 오류 0을 **측정값으로** 보고할 수 있게 됐다(MIGRATION_REPORT §5).

**기술적 의미.** 검증 도구가 **"검증하지 않은 것을 검증한 것처럼" 보고하는** 위험을 잡았다. 제약이 선언돼 있어도 비활성(DISABLED)이거나 미검증(NOVALIDATE)일 수 있으므로, 데이터 실측과 제약 상태 확인을 함께 요구하도록 바꿨다.

### E2. Shadow와 Oracle 런타임의 동시 쓰기(배타적 런타임 소유권)

**문제 발생.** ORACLE-RUNTIME-1의 남은 위험 표에 "Shadow와 Oracle 런타임 동시 실행 방지 장치: 운영 절차로만 금지"라고 적혀 있었다. 동시에 실행되면 Shadow가 `next_due_at`을 앞당겨 계획이 실행되지 않을 수 있었다(`development-oracle-runtime-1-2026-11-03/REPORT.md` 112·129행).

**해결 방법.** 커밋 `f3a77d1`(대상 스키마 v5).
- `runtime_ownership`은 모드 행 1개(IDLE/SHADOW/RUNTIME_TEST/RUNTIME_PRODUCTION, mode epoch)이고, `runtime_participants`는 쓰기 프로세스마다 임대 행 1개를 둔다.
- 모든 런타임·Shadow 쓰기 트랜잭션은 `LOCK TABLE runtime_ownership IN SHARE MODE`를 잡고 모드·epoch·유효 임대를 검증한다(`guard`). 공유 잠금끼리는 공존하므로 같은 모드의 Worker는 병렬로 돈다.
- 모드 전환은 테이블을 EXCLUSIVE로 잠가 진행 중인 쓰기를 기다리고, 다른 모드의 살아 있는 임대나 미완료 런타임 작업이 있으면 거부한다.
- 당시에는 RUNTIME_PRODUCTION을 절대 부여하지 않았다. 운영용 접근은 이후 `dea4b33`에서 승인 기록 기반으로 열었다.

**변경된 코드.** `food_trends/runtime_ownership.py`(신규), `oracle_execution.py`, `shadow.py`, `target_migration.py`, `target_schema.py`, `runtime_storage.py`.

**테스트.** `tests/test_runtime_ownership.py`의 `test_runtime_write_refused_while_shadow_owns_the_target`, `test_shadow_refused_while_runtime_owns_the_target`, `test_killed_runtime_recovers_without_handing_unfinished_jobs_to_shadow`, `test_abandoned_runtime_without_jobs_can_be_taken_over`, `test_late_worker_result_is_refused`, `test_failed_mode_switch_changes_nothing`, `test_refusals`, `test_switch_waits_for_an_open_runtime_transaction`. 대역과 실제 Oracle에서 통과했다. R3 단계 커밋 직전 결과는 기본 394개, 실제 Oracle 489개, 실패 0(runtime-2 REPORT).

**해결 결과.** Shadow 활성 중의 런타임 쓰기는 `ownership_held_by_SHADOW`, 그 반대는 `ownership_held_by_RUNTIME_TEST`로 거부되고 쓰기는 0건이다. 강제 종료된 런타임의 미완료 작업이 있으면 Shadow 전환이 `runtime_jobs_in_flight`로 거부된다(runtime-2 REPORT §4).

**기술적 의미.** 운영 규칙에 기대던 상호 배제를 **DB 잠금 프로토콜(공유/배타 잠금 + epoch + lease 펜싱)**로 바꿨다. 읽기·쓰기 잠금 구분으로 같은 모드 안의 병렬성을 유지하면서 모드 사이의 배타성을 보장한다.

### E3. Oracle 런타임의 API 예산 정합성(Quota reconciliation)

**문제 발생.** ORACLE-RUNTIME-1 보고서의 차이 표: "예산 사용량 — SQLite는 legacy 호출 테이블 + 예약 청구, Oracle은 예약 청구만. legacy 수집기가 같은 계정을 쓰면 대상은 그 사용량을 모름"(`development-oracle-runtime-1-2026-11-03/REPORT.md` 113행).

**해결 방법.** 커밋 `0f42a3d`(대상 스키마 v6).
- `quota_usage_ledger`: 열린 창의 레거시 호출이나 고유 해시태그 검색 1건당 1행. 출처, 계정 범위, 창, 공급자 시간대, 근거, 신뢰도, 원 출처 라벨, 원본 행 참조를 담는다. 요청 본문·검색어·응답은 복사하지 않는다.
- `quota_accounts`: 계정 범위별 상태 UNVERIFIED/IMPORTED/EXCLUSIVE.
- 근거 등급: `PROVIDER_CONFIRMED`는 만들지 않는다. `LEGACY_LOCAL_COUNT`, `LEGACY_SENT_UNCERTAIN`(running으로 남은 호출), `LEGACY_UNKNOWN_ORIGIN`이 있으며 모두 사용량에 보수적으로 포함한다.
- `assess()`는 SQLite와 같은 값을 내고, `usage()`에만 설명 정보를 붙인다.
- 예약 전에 계정 행을 먼저 잠가 마지막 1단위가 한 계획에만 가게 한다.
- Live 모드에서는 계정 사용량이 미확인이거나 15분 넘게 지났으면 `EXTERNAL_USAGE_UNVERIFIED`로 막고 HTTP를 보내지 않는다.

**테스트.** `tests/test_oracle_quota.py`(6개): `test_legacy_usage_and_new_reservations_match_sqlite`, `test_unverified_or_stale_usage_blocks_execution_without_http`, `test_full_budget_defers_without_http`, `test_imports_and_reservations_are_not_duplicated`, `test_account_scopes_keep_separate_budgets`, `test_no_debit_is_ever_confirmed`. 전체 경로 테스트는 `test_oracle_pipeline::test_oracle_runtime_runs_the_whole_path_alone`. 결과: 기본 403개, 실제 Oracle 505개(guard 보완 전), 보완 뒤 10개 모듈 재실행 통과(D4).

**기술적 의미.** 두 저장소가 공존하는 전환기에 **외부 자원(API quota)의 단일 진실**을 지키려고 근거·신뢰도가 붙은 원장을 도입했다. 확인할 수 없는 사용량은 0이 아니라 "모름"으로 두고 실행을 막는다(fail-closed).

### E4. 만료된 근거가 Oracle에서 `OBSERVED 0`으로 바뀌는 문제(묘비 이관)

**문제 발생.** B8 통합(2026-11-05) 중 첫 비교에서 발견됐다. 근거가 만료된 날이 SQLite에서는 `EVIDENCE_EXPIRED`(값 null)인데 대상에서는 `OBSERVED`가 됐다(`development-b8-integration-2026-11-05/REPORT.md` 67행, 후속 과제로 남김). CUTOVER-PREP Phase 1에서 재현했다. Instagram 캡션 10일분이 30일 보존을 넘긴 픽스처에서 B7이 B5의 `EXPIRED` 언급 묘비 10행을 `EXCLUDED_EXPIRED`로 버렸다. 10일이 `('EVIDENCE_EXPIRED', None)` 대 `('OBSERVED', 0)`으로 달랐고, 30일 지표의 이전 기간도 `evidence_expired 10` 대 `observed 10`이었다. 실패 하위 테스트는 22건이었다(`phase1-reproduction/test-summary-before-fix.json`). Shadow 경로도 만료 언급을 대상에 ACTIVE로 남겼다.

**원인 분석.** B7 이관 규칙이 적재 시점에 `expires_at`이 지난 행을 모두 제외했는데, B8이 "근거 만료일"을 판정하는 데 쓰는 *원문 없는* 묘비 행까지 같이 버렸다.

**해결 방법.** 커밋 `a5a8152`, `food_trends/target_migration.py`.
- 원문이 없는 B5 링크(`item_query_matches`, `food_mentions`, `metric_observations`)는 만료가 지나도 제외하지 않고, B5 purge가 남기는 상태로 옮긴다(ACTIVE면 EXPIRED로 확정하고 되살리지 않는다).
- 원문·스테이징 행의 만료 제외와 캡션 필드 제거는 그대로 둔다.
- Shadow 파생에도 같은 확정을 적용한다.
- `integrity_checks`는 "만료됐는데 살아 있는 행"(`expired_rows_in_target`)과 묘비(`expired_tombstones_in_target`)를 따로 센다.
- B8·스키마·계약은 바꾸지 않았다. 가장 작은 변경 지점인 B7만 고쳤다.

**테스트.** `tests/test_app_oracle.py::test_expired_evidence_arrives_as_contentless_tombstones`, `tests/test_target_migration.py::test_expired_evidence_is_carried_as_a_contentless_tombstone`, `test_tombstones_neither_revive_nor_keep_expired_content`, `tests/test_shadow.py::test_evidence_expiry_reaches_the_target_as_a_tombstone`. 결과: 수정 전 실패 22건 → 수정 후 Phase 1 대역 183개, 실제 Oracle 185개 통과. 만료 이후 적재에서 B8 전체 응답(요청 행렬 707개 포함)이 SQLite와 같다(cutover-prep REPORT §2).

**기술적 의미.** **"데이터 없음(0)"과 "데이터를 보존 정책상 지웠음(만료)"의 의미 차이**를 이관이 잃은 사례다. 보존 정책(원문 삭제)과 분석 의미(그날은 관측 불가)를 동시에 지키려면 원문 없는 묘비(tombstone)가 필요하다는 설계 원칙이 확립됐다.

### E5. Instagram Writer 이전 불가와 인계 뒤 복구 갱신 누락

**문제 발생.** ORACLE-LIVE-VERIFICATION-READY-1에서 운영 DB 격리 사본으로 리허설하다가 실행을 막는 결함 3건을 발견했다(`development-oracle-live-verification-ready-2026-11-06/REPORT.md` §8).
- **F-2**: `--target-load`의 레거시 사용량 가져오기가 열린 창에 행이 있는 Instagram 계정만 기록했다. 운영 DB의 마지막 Instagram 검색은 7일보다 오래돼 설정 계정이 기록되지 않았고, 리허설 1차의 `--writer-transfer instagram_graph`가 거부됐다. 단위 테스트는 계정을 직접 넘겨줬기 때문에 이 문제가 드러나지 않았다.
- **F-3**: 인계가 SQLite 파일을 바꾸면 복구 기준선(적재 당시 파일 지문)이 맞지 않는다. 그 결과 `--apply-updates`가 갱신을 승인하지 않았는데도 `converged: true`, `ready: true`가 나왔다(리허설 2차: 재관측 RSS 항목의 갱신 3건 누락).
- F-1(CLI가 격리 사본을 운영 승인 스위치 없이 가리킬 수 없음)은 `--isolate-database`/`--isolated-database`로 해결했다(같은 단계의 `93d3e24`).

**해결 방법.** 커밋 `86c8209`.
- F-2: `__main__.py`의 적재가 `account_scope('instagram_graph')`를 `import_legacy_usage(..., scopes=…)`에 넘겨, 설정 계정도 사용량 확인 계정으로 기록하게 했다(`legacy_usage_accounts` 출력).
- F-3: `usage_handover.handover()`가 WAL을 체크포인트한 뒤 쓰기 전후 파일 지문을 `USAGE_HANDOVER` 이벤트의 `destination_fingerprint={'before','after'}`에 기록한다. `reverse_recovery.Recovery`는 "적재한 파일 → 기록된 인계" 사슬을 따라 허용 지문 집합을 넓힌다(`matches_recorded_handover`). 다른 주체가 바꾼 파일은 계속 거부한다.

**테스트.** `tests/test_isolated_copy.py::test_the_documented_load_lets_each_configured_account_be_transferred`, `tests/test_writer_release.py::test_recovery_after_the_handover_still_applies_the_oracle_periods_updates`(대역 + MZ_TEST). 결과: 기본 529개 통과(Oracle 188 skip), 실제 Oracle 717개 중 716개 통과, 오류 1은 환경 원인(D5)이며 모듈 재검증 8/8.

**기술적 의미.** 두 결함 모두 **합성 테스트 데이터가 운영 데이터의 모양(오래된 이력, 절차 순서)을 반영하지 못해** 숨어 있었다. 운영 사본 리허설이 그 차이를 드러냈다. 특히 F-3은 복구 도구가 실패를 성공(`converged`)으로 보고한 경우였다. 파일 지문 사슬은 "누가 왜 파일을 바꿨는가"를 검증 가능한 이벤트로 만든다.

### E6. Mock·SQLite 대역과 실제 Oracle·운영 환경의 차이

**문제 발생.** B6~B7은 Oracle 인스턴스 없이 같은 명세의 SQLite 대역으로만 검증됐다("B6 완료 조건 미충족", `development-b6-b7-2026-11-02/REPORT.md` 3행). 실제 Oracle에서 다음 차이가 확인됐다.

| 차이 | 근거 | 조치 |
| --- | --- | --- |
| 세션 시간대가 클라이언트 PC 기준 `+09:00` | G-OPS-0 §6 | 드라이버가 오프셋과 함께 바인드해 저장값은 `+00:00`. DB 저장값을 직접 확인하는 계약 테스트 추가(`eafef71`) |
| TSTZ 조회 결과가 naive datetime | G-OPS-0 §6 | 모든 쓰기를 UTC로 정규화하는 계약 유지 |
| UNIQUE 경쟁: SQLite는 `BEGIN IMMEDIATE`로 직렬화, Oracle은 두 번째 세션이 대기한 뒤 ORA-00001 | G-OPS-0 §6 | 재조회·갱신 경로로 수렴(동시 업서트 테스트) |
| 지연 FK 위반이 COMMIT 시 ORA-02091/ORA-02291 | G-OPS-0 §6 | `foreign_key_violation` 분류 확인 |
| TSTZ 열을 유일키에 쓸 수 없음(ORA-02329) | runtime-2 REPORT §2 v4 | `tracking_slots.due_slot`을 정규화된 UTC 문자열로 저장(`203e67b`) |
| 스키마 재생성 직후 읽기 전용 트랜잭션의 ORA-01466 | cutover-prep REPORT 136행 | 테스트 도구 `oracle_support.settle_after_ddl`이 최신 DDL 뒤 4초 대기(`916d699`) |
| Oracle 빈 문자열 = NULL | 통합 감사 §7, `test_empty_string_and_null_keep_their_meaning` | missing/empty를 상태 값으로 보존 |
| 처리 속도: 대역 약 1,800행/초, Oracle 약 590행/초 | G-OPS-0 §6 | 참고치로 기록 |

Mock과 실제 운영 데이터의 경계도 코드로 강제했다. 운영 대상에서는 모든 작업이 Live여야 하고 Mock 응답은 `MOCK_EXECUTION_REFUSED_ON_PRODUCTION`으로 거부한다. `--rss-mock-file`도 운영에서 거부한다(`dea4b33`, production-cutover REPORT §2). B8 Mock 서버는 실제 API와 같은 구현을 쓰도록 설계했다(`development-b8-app-api-2026-11-04/REPORT.md` 3행).

**테스트·결과.** G-OPS-0: "Repository·DDL·변환 코드는 실제 Oracle에서 수정 없이 통과"했고 실제 Oracle 356개 통과. 유일하게 고친 결함은 E1이다. 이후 모든 Oracle 기능은 대역 클래스와 실제 Oracle(MZ_TEST) 클래스로 같은 사례를 두 번 실행한다(예: `OwnershipStandInTests`/`OwnershipOracleTests`). 운영 전환 회귀는 723개 전부 통과했다.

**기술적 의미.** 대역 검증의 한계를 처음부터 "NOT_VERIFIED"로 명시했고, **같은 테스트를 두 DBMS에 매개변수화해 실행**하는 구조로 이식성 차이를 체계적으로 드러냈다. 차이는 Repository 계층에 가두고, 계약 쪽은 바꾸지 않았다.

### E7. SQLite → Oracle 운영 이관 무결성(검증 사례)

**배경.** 결함 사례가 아니라 이관 전 과정의 무결성 검증 기록이다(`development-oracle-production-cutover-2026-11-07/MIGRATION_REPORT.md`).

**절차와 결과.**
1. 원본 `data/trends.sqlite3`: v0, 30개 테이블, 7,701행, SHA-256 `38b880f5…689548c0`, integrity ok, FK 0. 작업 전후 수정 시각이 같다. 원본은 읽기 전용으로만 열었다.
2. 백업: SQLite 백업 API로 만들었다. 모든 테이블의 열·행 다이제스트가 원본과 같았고, 복원 시험에서도 integrity ok·FK 0이었다. 파일 해시가 원본과 다른 이유는 페이지 배치 차이라고 기록했다.
3. 격리 사본만 v0→v11로 마이그레이션했다. 원래 30개 테이블은 변경 0, 72개 테이블 21,777행이 됐다.
4. B7 적재: 1차 15,671행. 2차 재실행은 완료 배치를 재사용해 새 쓰기가 없었다.
5. 대조: `consistent`. missing·extra·changed 0, 해시·NULL 불일치 표 0, FK 오류·미해결 원문 참조·정책 위반·살아 있는 만료 행 모두 0.
6. B8 동등성: 같은 요청 17개 × origin 2종에서 SQLite 사본과 Oracle 응답의 상태·본문·헤더가 모두 같았다.
7. 이후 Live 소량 검증: 실제 API 요청 7회(상한 9)가 모두 HTTP 200이었고, 결과는 `MZ_APP`에 저장됐으며 수집 경로의 SQLite 연결은 0이었다(production-cutover REPORT 결론 3).

**커밋.** `a8eebc5`, `dea4b33`, `875997e`, 보고 `9307d16`, 통합 기록 `363ea0f`.

**기술적 의미.** 앞의 E1(FK 실측)·E4(묘비)·E5(인계 지문)가 모두 이 최종 이관의 판정 기준에 반영됐다. "원본은 건드리지 않고 → 검증된 백업을 만들고 → 격리 사본을 변환하고 → 멱등 적재 후 → 행·해시·의미(B8 응답) 3단계로 대조"하는 전환 패턴이 완성됐다. 남은 한계로 지속 자동 수집은 `CONFIGURED_NOT_ENABLED`이고 제공자 측 사용량은 UNKNOWN이다(production-cutover REPORT §7).

---

## F. 확인하지 못한 항목

| 후보 | 상태 |
| --- | --- |
| `instagram_runs`가 `running`으로 영구히 남은 실제 사고 | **미확인.** 기록상 병행 수집 중의 일시적 running 1건(A1~A3 보존 점검)과, 이후 running 0만 확인된다. lease 만료 → `interrupted` 설계와 테스트는 확인했다(A6) |
| NAVER 별칭 중복 요청이 실제로 전송된 사고 | 기록 없음. 예방 설계와 실환경 409 검증만 있다(A5) |
| NAVER 인코딩 깨짐의 정확한 코드페이지(cp949 등) | **미확인.** 보고서는 "PowerShell 표준입력의 한글 인코딩"으로만 기록한다 |
| Git 도입 이전 수정(A1~A6, B1~B2)의 정확한 diff | 별도 커밋이 없어 **미확인**. 첫 커밋 `97b3c35`에 수정 후 상태가 들어 있고, 변경 전 코드는 `data/audit-20261026/before-*.py`와 `data/*-preservation-*/files/`에 보존돼 있다 |
| 포트 중복 바인딩 수정의 구체 함수 | **미확인**(사전 작업). 테스트 이름만 확인 |
| `D:\c\…` 잔여 폴더의 현재 존재 여부 | 이번 감사에서 확인하지 않음 |
| Modern Standby 방지 래퍼의 코드 | 저장소 코드에는 없음. 보고서와 메모리 기록의 실행 절차로만 존재 |
