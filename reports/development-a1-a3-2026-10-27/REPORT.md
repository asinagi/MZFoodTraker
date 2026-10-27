# 1차 개발 결과 — 데이터 무결성 및 공통 계약 기반

2026-10-27 KST. 범위는 통합 감사 로드맵의 **A1~A3**이다. Oracle, 공통 FOOD_ENTITY/FOOD_ALIAS, 탐지·추적 자동 연결, 새 수집원, 유행 점수는 구현하지 않았다. 실제 외부 수집 API는 호출하지 않았다.

## 구현 결과

| 작업 | 구현 내용 | 검증 |
| --- | --- | --- |
| A1 후보 연결 보존 | `Storage._classify`의 REPLACE를 `INSERT ... ON CONFLICT(item_id) DO UPDATE`로 변경했다. 기존 후보 행을 삭제하지 않고 값만 갱신한다. 후보 ID와 자식 FK 관계를 보존한다 | 동일 RSS 반복 수집·재분류·분류 내용 및 버전 변경에서 YouTube 연결 1→1. 새 후보 생성, 변경 전 분류 이력, 동일 결과 재분류의 이력 중복 방지도 검증 |
| A2 공통 계약 | 출처·origin·시각·데이터 유형·단위·필드 제공 상태·요청/관측 조건·계약/어댑터/정책 버전을 `contracts.py`에 정의하고 기존 수집기에 연결했다 | 네 출처의 Mock 계약 저장, 미기록 출처 유지, 비밀키 형태 거부, 필드 누락/NULL/빈값 구분, 비승인 처리 차단 검증 |
| A3 조회 분리 | 상태·목록·상세·내보내기는 `Storage.reader()`의 `mode=ro`, `query_only=ON` 연결을 사용한다. 생성자에서 DDL·정리·재분석·자격증명 상태 쓰기를 제거했다 | 모든 데이터 GET 경로를 쓰기 연결 금지 상태로 호출하고 DB dump가 완전히 같은지 확인 |
| A3 명시적 마이그레이션 | 버전 1은 기존 30개 테이블의 추가형 스키마, 버전 2는 출처·계약 메타데이터 컬럼이다. 전용 명령에서 트랜잭션으로 실행한다 | 기존 실제 DB의 폐기 가능한 복사본에서 원래 모든 행·컬럼 값 보존, 무결성/FK 정상, 재실행 적용 0건, 실패 시 DDL rollback 검증 |

**기존 97개 + 신규 17개 = 114개 테스트 통과, 실패·오류·건너뜀 0, 외부 접속 시도 0회**다. 테스트 결과는 [test-summary.json](test-summary.json), 전체 로그는 [tests.log](tests.log), 실제 자료 복사본 검증은 [copy-migration.json](copy-migration.json)에 있다. 기존 97개 테스트는 임시 DB 생성 지점을 `Storage.create(...)`라는 명시적 스키마 준비로 바꿨으며, 기존 검증 assertion은 약화하지 않았다. 신규 검증은 `tests/test_foundations.py`에 추가했다.

## A1: UPSERT와 이력

후보를 REPLACE하면 SQLite가 기존 후보를 삭제하고 새로 삽입하면서 `youtube_candidate_foods.candidate_id`의 `ON DELETE CASCADE`가 발동했다. 지금은 충돌한 후보를 UPDATE하므로 해당 삭제가 발생하지 않는다.

기존 `_classify`의 결과 비교와 `classification_history` 기록은 유지했다. 분류 시각만 바뀐 경우 이력을 추가하지 않고, 실제 분류 내용이나 버전이 바뀌면 이전 결과를 남긴다. 신규 후보는 INSERT 경로를 사용한다. 후보와 관계없는 YouTube 쿼리 동기화의 의도된 매핑 재구축은 이 변경과 별개다.

## A2: 공통 계약과 출처별 적용

`food_trends/contracts.py`의 공개 진입점은 `envelope`, `encode`, `policy_for`, `require_operation`이다. 계약은 콘텐츠 원문을 복사하는 구조가 아니라 **수집 메타데이터**다. API 키·토큰·인증 헤더·자격증명이 있는 URL을 포함하지 않는다. 요청 메타데이터는 출처별 허용 필드만 받으며, 중첩된 비밀키 형태나 비 JSON 값·비유한 숫자를 거부한다. 임의 문자열 안의 알려지지 않은 비밀값까지 자동 검출한다고 보장하지 않으므로 config 객체나 HTTP 헤더를 통째로 전달하면 안 된다.

| 계약 필드 | 의미 |
| --- | --- |
| source_code | google_trends_rss / instagram_graph / youtube / naver_datalab |
| origin | live_api / mock / user / legacy_unknown. 수집 실행의 출처이며 사용자 지정 검색어 여부와 구분 |
| collected_at / published_at | 시간대가 있는 순간 시각. 수집 시각으로 게시 시각을 대체하지 않음. 게시 시각 미제공은 NULL |
| data_type / metric_units | 출처의 데이터 형식과 측정 단위. 원시 트래픽 문자열·관측 게시물·영상 통계 snapshot·상대 검색 지수를 구분 |
| field_states | provided / not_provided / null / empty. 원문 대신 필드 상태만 담음 |
| field_evidence | adapter_observed 또는 not_recorded. 요청 예약 시 아직 관측하지 않은 필드를 ‘미제공으로 확인’했다고 표시하지 않음 |
| provided_fields / missing_fields | 어댑터가 실제 확인한 제공/미제공 필드 목록 |
| request_conditions / observation_conditions | 검색·기간·그룹·필터·표본·실행 근거. 자격증명 제외. YouTube pageToken은 계약 snapshot에 복사하지 않음 |
| contract_version / adapter_version / policy_version | 후속 변환·검증 기준을 추적할 명시적 버전 |
| policy | 현재 앱의 보존·분석 제한. 공급자가 법적 사용권을 승인했다는 증거가 아님 |

| 출처 | 저장 위치·적용 범위 | 유지한 제한 |
| --- | --- | --- |
| RSS | runs.origin/contract_json, observations.contract_json. 상태와 항목 조회에도 계약을 부가 제공 | 기존 XML·payload·분류 구조 유지. 과거 회차는 legacy_unknown, 과거 계약은 NULL. 원문 재분류로 origin을 바꾸지 않음 |
| Instagram | instagram_runs, instagram_media의 origin/contract_json, 분석 관측의 contract_json | 캡션 기존 보관 설정 사용, 기본 30일. 같은 캡션 반복 관측으로 원문 기한을 연장하지 않음. 기존 태그 분석 외 신규 음식 추출·장기 보존 권한을 부여하지 않음 |
| YouTube | youtube_videos.contract_json은 기존 최신 snapshot과 함께 만료. 실행 결과에는 값 없는 계약 메타데이터 부가 | 기존 29일 TTL과 최신 snapshot 유지. 검색 조건도 같은 만료 범위 안에서만 보관. 음식 추출·콘텐츠 분류·파생 지표 비활성. 기존 legacy_unknown 유지 |
| NAVER | naver_calls에는 요청 전 계약과 origin만 저장. 응답 필드 상태는 현재 요청의 응답 계약에만 전달 | 응답 본문·ratio·시계열·캐시를 저장하지 않음. 기존 API의 origin=rss_food/user_keyword는 호환용으로 유지하며, 표준 실행 origin은 contract.origin으로 확인 |

기본 transport로 확인한 새 호출은 live_api, 주입된 테스트 transport/fetcher는 mock이다. 출처 증거가 없는 기존 행에 기본값으로 live_api를 넣지 않는다. 이미 출처 정보가 있는 YouTube 기존 provenance는 수정하지 않는다. 새 상세 응답으로 실제 갱신한 영상의 최신 provenance가 달라지는 것은 과거 출처를 소급 확정하는 것과 다르다.

## A3: 읽기·쓰기의 경계

| 경로 | 변경 후 동작 |
| --- | --- |
| Storage 생성 | DB 파일·디렉터리를 만들지 않고 스키마 버전만 읽는다. 준비되지 않은 DB는 MigrationRequired로 안내 |
| 출처 서비스/저장소 생성 | DDL, 자동 캡션 재분석, 만료 삭제, 자격증명 상태 갱신 없음 |
| GET /api/status, /api/trends, /api/trends/{id}, /api/export | 읽기 전용 연결 사용. RSS 내보내기의 기존 원본 보존 유지 |
| GET /api/naver | 현재 FOOD 후보와 기존 운영 메타데이터를 읽기 전용으로 조합. 대상 upsert 없음 |
| NAVER query/collect | 명시적 쓰기 작업 안에서 sync_targets 실행 |
| GET /api/youtube | 만료 영상·만료 검색 관계를 필터링해 반환. purge와 sync_queries 없음 |
| YouTube collect | 기존 수집 경로에서 query 동기화·정리 수행. 출처 연결을 새로 자동화하지 않음 |
| GET /api/instagram 및 /media | 조회만 수행. 변경된 자격증명은 로컬 계산으로 unchecked/unverified 표시. 만료 캡션은 반환하지 않음 |
| Instagram 수집/관리 작업 | prepare_credentials에서 변경된 토큰의 로컬 상태를 갱신. 기존 토큰 자체는 변경하지 않음 |
| 유지관리 | 명시적 --maintenance 또는 기존 수집/유지관리 루프에서 만료 삭제. 미분석 legacy 캡션도 최초 관측일을 기준으로 삭제 가능 |
| --no-schedulers | RSS·NAVER 자동 외부 수집 중단, YouTube·Instagram은 외부 수집 없이 로컬 만료 관리만 실행 |
| --instagram-reanalyze / --reanalyze | 명시적으로 요청한 로컬 재처리만 수행. 생성자나 GET에서 호출하지 않음 |

조회와 별도로 실행 중인 수집·유지관리 작업이 있으면 그 작업으로 DB가 변할 수 있다. GET 자체의 읽기 전용 보장은 background worker의 동시 쓰기를 중단한다는 의미가 아니다. 이번 검증은 격리 DB에서 유지관리 thread를 시작하지 않은 상태로 GET 전후를 비교했다.

## 실행·배포 절차

**기존 운영 `data/trends.sqlite3`에는 이번 작업에서 마이그레이션을 적용하지 않았다.** 실제 자료의 격리 복사본으로만 검증했다. 실행 중인 기존 서버를 중단하거나 재시작하지 않았다. 새 코드로 기존 DB를 사용하려면 구버전 writer를 종료한 배포 창에서 백업 후 명시적 마이그레이션이 필요하다. 구버전 YouTube의 위치 기반 INSERT가 새 컬럼과 충돌할 수 있으므로 구버전 프로세스가 쓰는 DB를 동시에 업그레이드하면 안 된다.

아래 명령은 로컬 작업이다. `DATABASE_PATH`가 대상 DB를 결정하므로 실행 전 해당 경로를 확인한다. 별도 검증 DB를 쓰려면 환경변수에 그 경로를 지정한다. `.env` 값은 변경하지 않았다.

```powershell
# 스키마 버전만 읽기. 파일이 없으면 생성하지 않음.
py -3.12 -B -m food_trends --migration-status

# 백업 및 구버전 writer 종료 후 운영자가 명시적으로 실행할 배포 작업
py -3.12 -B -m food_trends --migrate

# 외부 API·재분석 없이 만료 자료만 정리
py -3.12 -B -m food_trends --maintenance

# 생성·마이그레이션·정리 없이 상태 읽기
py -3.12 -B -m food_trends --youtube-status
py -3.12 -B -m food_trends --naver-status
py -3.12 -B -m food_trends --instagram-status

# 외부 소켓과 운영 DB를 차단한 재현 가능한 테스트
py -3.12 -B tests/run_offline.py

# 보존 사본에서 폐기 가능한 복사본을 만들어 마이그레이션 대조
py -3.12 -B tests/verify_preserved_copy.py data/a1-a3-preservation-20261027T054909Z/before.sqlite3
```

`--migrate`는 명시적 추가형 변경만 수행하고 수집·분류·대상 동기화·TTL 갱신을 실행하지 않는다. 성공한 버전은 `schema_migrations(version,name,applied_at)`에 기록한다. 같은 버전 재실행은 변경 0건이다. 변경 중 예외와 FK 검증 실패는 스키마·데이터 트랜잭션을 rollback한다. 더 최신 버전 DB의 downgrade는 거부한다. WAL 모드 설정은 마이그레이션 트랜잭션 밖에서 수행되는 연결 설정이다.

## 변경 파일

정확한 목록과 줄 변경 수는 [change-manifest.json](change-manifest.json)에 있다.

- 새 소스: `food_trends/contracts.py`, `migrations.py`, `migration_definitions.py`.
- 무결성·조회: `storage.py`, `service.py`, `naver_service.py`, `youtube_storage.py`, `youtube_service.py`, `instagram_storage.py`, `instagram_analysis.py`, `instagram_service.py`.
- 출처 표시와 제공 필드: `instagram_graph.py`, `naver_datalab.py`, `youtube.py`.
- 명시적 CLI: `food_trends/__main__.py`.
- 새 검증: `tests/test_foundations.py`, `tests/run_offline.py`, `tests/verify_preserved_copy.py`.
- 기존 단위·브라우저 fixture: `Storage(...)`로 암묵적으로 생성하던 지점을 `Storage.create(...)`로 변경. 실제 브라우저 수집 스크립트는 실행하지 않음.

## Git·보존·복구

작업 시작 시 폴더와 상위 경로에서 Git 저장소를 확인하지 못해 다음 사본부터 확보했다.

- 복구용 원본 파일 59개 및 해시: `data/a1-a3-preservation-20261027T054909Z/files/`, `manifest.json`.
- SQLite 온라인 백업: 같은 폴더의 `before.sqlite3`, 무결성 정상. 실행 중 DB 파일을 단순 복사하지 않고 SQLite backup API를 사용했다.
- `.env`는 수정 대상이 아니므로 새로 복제하지 않고 변경 여부만 해시로 확인했다. 값은 출력하지 않았다.

작업 중 **2026-10-27 14:51:37 KST**에 `.git` 디렉터리가 생긴 것을 확인했다. 현재 저장소는 `D:/mz 제조기`, 브랜치는 main이며 확인 당시 HEAD 커밋이 없었다. 이 작업에서는 Git 초기화·커밋·스테이징·reset을 수행하지 않았다. 같은 시간대에 확인된 README·`.env.example`·`.gitignore` 변경은 이 작업의 수정과 분리해 보존했다.

코드 복구가 필요하면 유지관리 창에서 이 작업의 변경 파일만 `files/` 사본과 대조해 되돌린다. 작업 도중 추가된 위 세 파일의 외부 변경과 `.git`은 되돌리지 않는다. 운영 DB를 변경하지 않았으므로 현재는 DB 복원이 필요하지 않다. 이후 운영 DB를 업그레이드한 뒤 rollback하려면 writer를 멈추고 업그레이드 후 신규 데이터를 별도로 보존한 다음 검증된 DB 사본을 사용해야 한다. 새 데이터를 버리는 단순 덮어쓰기를 복구 절차로 사용하지 않는다.

보존 점검에서는 운영 DB의 Instagram 회차가 8→9건으로 늘고 running 회차와 lease 1건이 생긴 병행 업데이트를 관측했다. 이번 작업의 마이그레이션·테스트가 운영 DB를 쓴 것은 아니며, 운영 DB의 schema_migrations 테이블도 아직 없다. 따라서 ‘작업 기간 내 운영 DB 전체 불변’이라고 주장하지 않는다. [preservation-check.json](preservation-check.json)에 이 차이와 `.env` 보존 여부를 기록했다. 이와 별개로 **초기 보존 사본에서 수행한 마이그레이션 대조는 원래 30개 테이블의 모든 값 보존에 성공**했다.

보존 사본도 YouTube/Instagram API 자료를 포함한다. 원래 레코드의 만료일을 그대로 적용해야 하며, 복원일을 수집일로 바꾸거나 사본을 무기한 보관하면 안 된다. 이번 검증의 추가 마이그레이션 DB는 임시 디렉터리에서만 만들고 종료 시 삭제했다. 원래 복구용 사본에는 운영자가 동일한 출처별 만료·삭제 정책을 적용해야 한다.

## A4~A5에 권장하는 인터페이스

1. **읽기:** 공통 음식·후보 화면은 `Storage.reader()` 경계를 사용한다. GET에서 대상 생성·별칭 승인·재분류를 하지 않는다.
2. **쓰기:** 음식/별칭을 추가할 때 별도 버전 마이그레이션을 정의한다. 저장소 생성자에 CREATE/ALTER를 다시 넣지 않는다.
3. **출처 계약:** `envelope`의 버전·origin·단위·field_states를 입력 계약으로 사용한다. 레거시 NULL 계약은 미확인으로 남긴다.
4. **출처 정책:** 새 음식 추출 같은 작업은 `require_operation(source, operation)`을 통과해야 한다. 현재 YouTube/NAVER/Instagram의 `food_extraction`은 허용되지 않는다. 정책 확인 없는 활성화 설정을 추가하지 않는다.
5. **사용자와 실행 출처:** 사용자가 음식명을 입력했다는 사실은 향후 candidate의 생성 경위다. 실제 NAVER/YT 수집 데이터의 origin을 user로 덮어쓰지 않는다. 사용자가 만든 입력 기록 자체에는 user를 사용할 수 있다.
6. **대상 갱신:** NAVER `sync_targets`와 YouTube `sync_queries`는 수집 준비 쓰기 경로다. A4 이후 공통 target/planner를 붙일 때 이 경계를 대체하며 상태 조회에서 호출하지 않는다.

## 잔여 위험과 제한

- 운영 배포·실제 스키마 적용·실제 외부 API 재검증은 수행하지 않았다. 배포 전 위 마이그레이션 절차가 필요하다.
- 공통 계약은 기존 수집 결과의 메타데이터다. 음식 개체의 동일성, 탐지 정확도, 자동 추적의 완성도를 높였다고 주장하지 않는다.
- IG 원문·태그·관계의 장기 보존 권한, NAVER 응답 보존 권한, YouTube 파생 분석 승인 여부는 여전히 미확인이다. 기존 제한을 유지했다.
- 마이그레이션은 스키마 버전·FK를 검사한다. 수동으로 훼손된 스키마의 모든 제약·인덱스 누락까지 자동 복구하는 도구는 아니다.
- 값이 없는 운영 계약과 기존 원본의 보존은 구별한다. API 응답을 공통 계약의 observation_conditions에 넣으면 안 된다. 허용 필드와 데이터 최소화는 새 adapter에도 적용해야 한다.
- 구버전 실행 파일/스크립트의 위치 기반 INSERT, 복구용 사본의 TTL, 앱이 완전히 종료된 동안의 삭제 지연은 운영 절차로 관리해야 한다. 조회는 만료 값을 숨기지만 DB 삭제 자체는 유지관리 작업이 수행한다.
