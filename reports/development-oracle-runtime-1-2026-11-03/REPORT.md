# ORACLE-RUNTIME-1 기존 서비스의 Oracle 실행 연결

2026-11-03. B3 실행·B4 처리·B5 관측 서비스가 Oracle Repository(대상 스키마)를 통해 실제로 읽고 쓰도록 연결했다. 모든 신규 검증은 **격리 SQLite와 실제 Oracle(MZ_TEST)** 에서 같은 Mock 입력으로 수행했고, SQLite 기존 경로와 논리 결과가 같음을 표 단위로 대조했다. 실제 외부 API 호출 0건, 운영 SQLite·서버 변경 없음. 운영 Oracle 전환(G-OPS-6)은 수행하지 않았다.

| 단계 | 커밋 | 판정 |
| --- | --- | --- |
| S1 DB 경로 안전성 | `8f293ca fix: make database paths deterministic and safe` | 완료 |
| S2 B3 Oracle 실행 계층 | `e6867c5 feat: support Oracle tracking execution runtime` | 완료(Mock 한정) |
| S3 B4 Oracle 데이터 처리 | `01da403 feat: support Oracle ingestion and data quality writes` | 완료 |
| S4 B5 Oracle 음식 관측 | `f63ddae feat: support Oracle food observation writes` | 완료 |
| 문서 | `docs: record Oracle runtime parity validation` | 이 문서 |

## 1. 공통 구조

- **서비스 로직은 그대로 두고 저장 primitive만 교체했다.** `IngestionService`·`FoodObservationService`의 판단 코드(정책 게이트, 배치 중복 제거, DQ 규칙, 지표 허용, 정확 별칭 추출, 재처리)는 수정하지 않고, SQL 문장만 이름 있는 primitive 메서드로 옮겼다(SQLite SQL은 동일). Oracle 쪽 `OracleIngestionService`·`OracleObservationService`는 그 primitive만 재정의한다. B3는 SQL 의존이 커서 같은 메서드 계약(`enqueue/claim/renew/begin/finish/fail/status`)을 가진 `OracleExecution`을 별도로 두고, 기존 `ExecutionWorker`·`ExecutionController`·`NaverExecutor`와 출처 파서·체크포인트 함수(`execution_sources`에서 변경 없이 추출)를 그대로 재사용한다.
- **저장소 선택은 명시적이다**([runtime_storage.py](../../food_trends/runtime_storage.py)). `sqlite`(기존), `oracle-test`(ORACLE_TEST_*·`ENVIRONMENT=test` 필수), `oracle-production`(항상 거부, `oracle_production_runtime_disabled`). `--runtime-status <mode>`는 읽기 전용이며 파일·DDL을 만들지 않는다. Oracle 서비스는 SQLite를 열지 않고 SQLite 서비스는 Oracle을 열지 않으므로 Shadow 외 자동 이중 쓰기는 없다.
- **DDL은 명시적 버전 마이그레이션으로만.** 대상 스키마 v2(아래)를 `--target-migrate`로 적용한다. 서비스 생성자는 버전만 확인하고 불일치면 `target_schema_version_mismatch`로 거부한다(테스트로 확인).
- **역할 분담.** 계획 수립·예정 슬롯 진행·음식/별칭 결정은 SQLite 기록 시스템에 남고 B7 이관 또는 Shadow로 대상에 도착한다. Oracle 런타임은 그 계획을 실행·처리·연결한다.

## 2. 대상 스키마 v2 (명시적 업그레이드)

| 변경 | 이유 |
| --- | --- |
| `execution_mappings`, `execution_leases`, `execution_checkpoints`, `execution_item_receipts`, `execution_blocks`, `quota_charges` | B3 계약의 매핑·Lease/Fencing·체크포인트·작업별 수신 영수증·계정 차단·예산 청구를 Oracle에 보관(런타임 소유, SQLite에서 이관하지 않음) |
| `source_contents.source_payload_hash` | Oracle은 정규화 JSON(키 순서 없음)으로 저장하므로, 원본 저장소가 직렬화한 그대로의 SHA-256을 따로 두어 B4 Snapshot 키가 DBMS와 무관하게 같아지게 함. B7이 채운다 |

`TargetRepository.migrate()`는 v1 스키마에 없는 객체·열만 추가한다(재실행 가능). 실제 Oracle에서 v1→v2 업그레이드와 재실행 무변화를 테스트했다. Oracle 예약어 `RESOURCE`는 `resource_code`로 피했다.

## 3. S1 — DB 경로 (완료, 이전 커밋)

기본 경로는 작업 디렉터리가 아니라 프로젝트 루트 기준(`paths.py`). `--migrate`는 파일이 없으면 만들지 않고 종료 코드 2로 거부하며 `--create-database`를 함께 줄 때만 생성한다. `--migration-status`와 `--runtime-status sqlite`는 읽기만 한다. `run.ps1`은 변경하지 않았다.

## 4. S2 — B3 Oracle 실행 계층

**구현.** [oracle_execution.py](../../food_trends/oracle_execution.py)
- 상태(READY/CLAIMED/RUNNING/RETRY_WAIT/종결 6종)·Outbox·실행 회차·매핑·호출 원장·결과 계약을 SQLite와 같은 규칙으로 기록한다.
- **잠금**: claim은 후보 job 행을 `SELECT … FOR UPDATE SKIP LOCKED`로 잠근 뒤 lease를 **잠금 아래에서 다시 읽는다**(스캔 후 다른 Worker가 커밋한 경우 대비). 모든 fenced 쓰기는 job 행 → lease 행 순서로 잠가 오래된 Worker의 커밋 후 쓰기를 막고 claim과 교착하지 않는다. 대역 SQLite는 `BEGIN IMMEDIATE`로 직렬화한다.
- **HTTP 중 트랜잭션 없음**: 예산 예약·시도 기록·전송 의도는 요청 전에 커밋하고, 응답 저장·체크포인트·호출 완료는 다음 fenced 트랜잭션에서 함께 커밋한다.
- 예산: `QuotaManager`의 요구량·판단 로직을 재사용(`OracleQuota`)하고 사용량은 대상의 `quota_reservations`/`quota_charges`로 계산한다.
- Oracle YouTube/Instagram 실행기는 기존 파서·체크포인트 함수를 공유하고 허용 콘텐츠를 `source_contents`(YouTube 29일 만료, Instagram 캡션 창)와 작업 영수증에 저장한다. NAVER는 기존 `NaverExecutor` 그대로이며 아무것도 저장하지 않는다.
- Live 모드·운영 표시 Oracle 대상은 거부한다(Mock Transport 한정).

**검증 시나리오 1~10** ([test_oracle_execution.py](../../tests/test_oracle_execution.py), 대역·실제 Oracle 각 22개). 차등 하네스가 같은 B7 적재 상태와 같은 시각에서 SQLite `ExecutionService`와 `OracleExecution`을 각각 실행하고, 작업 상태·호출 원장·회차·결과 계약·적격 대상·차단 사유·예산 줄·외부 요청 수를 비교한다.

| # | 시나리오 | 결과 |
| --- | --- | --- |
| 1 | 작업 생성(원자적 job·outbox·run·mapping, 같은 계획 재요청 멱등) | 일치 |
| 2 | Outbox 전달(전달 횟수, 종결 시 delivered_at) | 일치 |
| 3·4 | Lease 획득·Claim, **두 Worker 동시 Claim**: 정확히 1개 성공, 실제 Oracle에서 두 번째 Worker가 잠금 보유 중 대기 없이 반환(SKIP LOCKED 확인), 3 Worker 병렬 처리 시 job 중복 claim 없음 | 통과 |
| 5 | Mock 응답 처리(YouTube·Instagram·NAVER), 콘텐츠 값·만료·해시가 SQLite 원본 저장소와 동일 | 일치 |
| 6 | 작업 완료(결과 계약, execution_results, last_success_at) | 일치 |
| 7 | 중복 전달(완료 후 재전달·늦은 Worker는 HTTP 0), 중복 페이지는 영수증 1개 | 일치 |
| 8 | Worker 종료·재시작: 응답 후 중단 → UNKNOWN·원격 중복 가능 표시 후 재개, 페이지 커밋 후 중단 → 체크포인트 재사용, 마지막 시도 전송 중단 → FAILED+불확실성 유지 | 일치 |
| 9 | Fencing 실패: 오래된 소유자의 renew/finish/fail 거부, 응답 처리 중 소유권 이전 시 쓰기 0 | 일치 |
| 10 | 예산 부족(HTTP 0, RETRY_WAIT), 네트워크 재시도 소진(FAILED), 커밋된 페이지 후 소진(PARTIAL), 429 Retry-After·계정 차단, 401 차단, 페이지 상한(LIMITED), 일시정지 대상(CANCELLED, 예약 0) | 일치 |

추가: Oracle 실행 중 SQLite 파일 변경 0(덤프 비교), 생성자의 DDL 미실행·버전 불일치 거부, v1→v2 업그레이드.

## 5. S3 — B4 Oracle 데이터 처리

**구현.** [oracle_ingestion.py](../../food_trends/oracle_ingestion.py). `ingest()`는 그대로이고 저장 primitive(회차·Source Item 병합·Snapshot·회차 관계·DQ 결과)를 대상 SQL로 재정의했다. Oracle 런타임 회차는 작업 영수증과 `source_contents`에서 기존 레코드 빌더(`_youtube_record`/`_instagram_record`)로 만든다. `purge()`는 만료·삭제를 반영하고 런타임이 소유한 보존(YouTube 29일 콘텐츠 삭제, Instagram 캡션 창 종료 시 캡션 제거)을 적용한다. RSS 수집·legacy 보충은 SQLite 전용 저장소를 읽으므로 대상에서는 거부하고, 그 결과는 B7/Shadow로 도착한다.

**검증 시나리오 11~14·18·20** ([test_oracle_ingestion.py](../../tests/test_oracle_ingestion.py), 대역·실제 Oracle 각 11개). 다섯 스테이징 테이블(`ingest_runs`, `source_items`, `item_snapshots`, `run_items`, `quality_results`)을 회차 식별자만 제외하고 논리 값으로 전부 비교했다.

| # | 시나리오 | 결과 |
| --- | --- | --- |
| 11 | 네 출처 공통 입력: YouTube·Instagram·NAVER는 실행 경로로, RSS·NAVER는 같은 Source Envelope로 투입 | 행 일치, NAVER 값 미저장 |
| 12 | Source Item 중복 제거(중복 페이지·재처리) | 1행 |
| 13 | Snapshot 저장(`source_payload_hash`로 키 동일) | 일치 |
| 14 | DQ 검사·결과 저장, 동일 입력 재처리 무변화 | 일치 |
| — | 수집 실패(FAILED/NONE)·정상 0건(SUCCEEDED/COMPLETE)·부분 수집(PARTIAL) 구분 | 일치 |
| 18 | NAVER 콘텐츠·자격증명 형태 요청 거부, 대상 정책(NAVER Source Item 거부), 만료 레코드 미적재 | 거부 |
| 20 | 만료(EXPIRED·Snapshot purge)·원천 삭제(DELETED) 전파 | 일치 |

## 6. S4 — B5 Oracle 음식 관측

**구현.** [oracle_observations.py](../../food_trends/oracle_observations.py). 근거는 `source_contents`에서 읽는다(RSS 본문은 B7/Shadow로 도착, YouTube·Instagram은 런타임 저장). 캡션 창은 `field_expires_at`. 대상의 정책 검사(언급 허용 출처, 저장 가능 지표, 출처 표시)가 모든 쓰기에 다시 적용된다. FOOD_ID 조회(`food_view`)는 SQLite와 같은 형태로 이식했다.

**검증 시나리오 15~19·21~28** ([test_oracle_observations.py](../../tests/test_oracle_observations.py), 대역·실제 Oracle 각 9개 + 저장소 선택 1개). 음식·별칭 결정은 SQLite에서 하고 B7 행 파생으로 대상에 반영했다(Shadow 전달과 같은 경로).

| # | 시나리오 | 결과 |
| --- | --- | --- |
| 15 | 검색 근거 ITEM_QUERY_MATCH(SEARCH_ONLY), 음식 언급과 분리 | 일치 |
| 16 | 승인 별칭 언급: Instagram 캡션 해시태그, RSS 검색어·뉴스 제목(대상에서 재연결해도 동일) | 일치 |
| 17 | 허용 지표: Instagram 관측 게시물 수, RSS 표시값. 실패는 NOT_OBSERVED·값 없음, 빈 성공은 관측된 0 | 일치 |
| 18 | YouTube 언급·조회수 지표, NAVER 지표 저장을 대상이 거부, `legacy_unknown` 표시 승격 거부 | 거부 |
| 19 | 별칭 승인·철회, 병합·분리 재처리, Aggregate Invalidation·변경 효과, 결정 1회 처리, 만료 근거 미재생성 | 일치 |
| 21 | 논리 행 수 일치(스테이징·연결 8개 테이블) | 일치 |
| 22 | 결정적 키(item_key, snapshot_id, mention_key)와 값 | 일치 |
| 23 | NULL·0·미관측 상태 | 일치 |
| 24 | UTC 시각, KST 일 경계(UTC 15:30=KST 00:30에 NAVER 창 15:00Z~익일 15:00Z), 예산 창 | 일치 |
| 25 | FK 고아 0, Oracle FK 전부 ENABLED·VALIDATED (B7 `integrity_checks` 재사용) | 0 |
| 26 | 자연 키 중복 0(10개 테이블) | 0 |
| 27 | 실행·처리·연결·재처리 재실행 후 대상 전 테이블 무변화 | 0 변화 |
| 28 | payload_ref 해석 실패 0, 정책 위반·만료 잔존 행 0 | 0 |

B7 `reconcile`의 대상 무결성 검사는 `integrity_checks()`로 분리해 그대로 재사용했다(`reconcile` 결과는 동일).

## 7. 전체 테스트

| 실행 | 결과 |
| --- | --- |
| 기본(Oracle 없음) | **373 실행, 실패·오류 0, Oracle 74 skip** ([default-tests](default-tests/test-summary.json)) |
| 실제 Oracle 포함 | **447 실행, 실패·오류·skip 0** ([oracle-full-tests](oracle-full-tests/test-summary.json)) |

단계별 커밋 직전에도 같은 두 실행을 통과시켰다(S2: 352/406, S3: 363/428). 테스트 통과는 운영 성능·이용약관 준수의 보증이 아니다.

## 8. SQLite와 의도적으로 다른 점

| 항목 | SQLite | Oracle 런타임 | 영향 |
| --- | --- | --- | --- |
| 실행 시점 재검증의 예정 슬롯 | `tracking_slots` 또는 `next_due_at` | `next_due_at`만(슬롯 테이블 없음) | 대상의 `next_due_at`이 Shadow로 앞당겨지면 그 계획은 실행되지 않음. 런타임과 Shadow를 같은 대상에 동시에 돌리지 않는다 |
| 예산 사용량 | legacy 호출 테이블 + 예약 청구 | 예약 청구만 | legacy 수집기가 같은 계정을 쓰면 대상은 그 사용량을 모름(기존 writer 소유권 이전 전 Live 금지와 같은 조건) |
| 예약 이벤트 이력 | `quota_events` | 없음(`quota_reservations` 상태만) | 감사 이력 축소 |
| 호출 ID | 무작위 | `JOB…:C00001` 결정적 순번(fence 아래 생성) | 삽입 순서 보장 |
| 결과 계약 참조 | `existing_source_tables` | `target_source_contents` | 저장 위치만 다름 |
| Instagram 캡션 만료 | 분석 저장소 purge | B4 `purge()`에서 캡션·태그 제거 | 결과 동일 |

## 9. 미완료·범위 밖 후속 과제

| 과제 | 영향 | 필요성 | 권장 우선순위 |
| --- | --- | --- | --- |
| Oracle 기반 계획 수립·예정 슬롯 진행(planner commit, tracking_slots) | 지금은 SQLite가 계획해야 Oracle이 실행 가능 | Oracle 단독 운영의 전제 | 높음(G-OPS-6 전) |
| Oracle 런타임 Live 모드(Instagram 원격 고유 태그 점검, writer 소유권, 출처 승인 registry) | Live 실행 불가 | 실제 수집 전환 | 높음, G-OPS-4/5 승인 후 |
| Oracle → SQLite 역방향 복구 | 전환 후 되돌리기 수단 없음 | 전환 안전성 | 높음(전환 전) |
| 남은 legacy 테이블 이관(youtube_queries/matches, instagram 실행·분석, naver_calls, quota_events, tracking_events/slots, 소유권) | 대상 단독으로 일부 이력·검색 연결 부재 | 완전 이관 | 중간 |
| RSS 수집의 Oracle 저장(runs/observations/items) | RSS는 SQLite 수집 후 B7/Shadow로만 도착 | 단일 저장소화 | 중간 |
| 웹 서버·UI의 Oracle 모드 연결 | 현재 CLI 상태 점검과 서비스 API만 | 화면 사용 | 중간 |
| Shadow와 Oracle 런타임 동시 실행 방지 장치 | 운영 절차로만 금지 | 실수 방지 | 중간 |
| 대량 처리 성능(행 단위 조회 후 삽입, 재검증 반복 조회) | 대량 데이터에서 느릴 수 있음 | 운영 규모 검증 | 낮음→운영 전 측정 |
| B8 음식별 통합 집계, 유행 지수 | 범위 밖 | 제품 기능 | 별도 요청 |
| WSL swap 위치, Oracle 데이터 볼륨 이동 | C: 여유 공간(STORAGE-1 기록) | 장기 운영 | 낮음 |

## 10. 운영 안전

- 실제 외부 API 호출 0(오프라인 러너의 차단 시도 기록 0).
- 운영 SQLite: 읽기 전용 점검만. v0·30개 테이블·integrity ok, 본 파일 수정 시각 2026-10-28 14:20 그대로, WAL 0바이트([production-final.json](production-final.json)). 점검 연결이 `-shm` 파일 시각만 갱신했다.
- 8000/8002 서버: B6~B7 작업 시작 전부터 정지 상태(2026-11-02 기록). 이번 작업에서 시작·중지하지 않았다.
- MZ_TEST 초기화는 매 테스트에서 보호 조건(사용자 일치, 내장·DBA 아님, `ENVIRONMENT=test`, 명세 밖 객체 없음)을 확인한 뒤에만 수행했다.
- Oracle 컨테이너는 테스트 동안만 기동했고 작업 후 정지 상태로 되돌렸다.
- `.env`·Oracle 비밀번호·DB·백업은 커밋하지 않았다(커밋마다 스테이징 diff 비밀값 대조 0건).
- G-OPS-1~6은 실행하지 않았다.
