# ORACLE-CUTOVER-PREP-1 Oracle 주 데이터베이스 전환 준비와 실데이터 이관 검증

2026-11-05. 기준 `main` `10e0d54`에서 작업 브랜치 `feat/oracle-cutover-prep`로 진행했다. 운영 SQLite·서버·운영 Oracle은 변경하지 않았고 실제 외부 API 호출은 0건이다. G-OPS-1~6은 모두 승인 대기 그대로다.

## 결론 요약

| 질문 | 답 |
| --- | --- |
| 실제 운영 데이터가 Oracle로 이관됐는가 | **아니다.** 운영 SQLite는 v0 그대로이며 운영 Oracle은 없다. |
| 격리 사본 이관은 성공했는가 | **성공.** 운영 DB(v0, 30개 표, 7,701행)를 읽기 전용으로 열어 backup API로 복사 → 사본만 v0→v11·스테이징 → 개발용 Oracle(MZ_TEST)에 15,671행 적재. 29개 표 해시 일치, FK·정책·만료 위반 0, 앱 응답 34건 동일, 재실행 쓰기 0, 원본 해시·시각·WAL 불변(§3). |
| Oracle은 어느 범위에서 System of Record인가 | **운영에서는 아직 아니다**(운영 기록 시스템은 SQLite v0). 개발·테스트에서는 음식 등록·검토·별칭·관계·병합, 추적 대상 명령, 계획·슬롯·Mock 실행, B4 정제·DQ, B5 관측, B8 조회를 Oracle **단독**으로 수행한다(SQLite 연결 0건으로 검증, §4). RSS 발굴·후보 가져오기, Live 수집, 레거시 수집기, 관리 웹 서버는 여전히 SQLite다(§5). |
| B7 만료 근거 결함은 해결됐는가 | **해결.** 재현(10일 `EVIDENCE_EXPIRED`→Oracle `OBSERVED` 0) 후 수정, SQLite·Oracle 동일 응답 확인(§2). |
| 역방향 복구는 검증됐는가 | **검증.** Oracle 전용 기간의 행·변경을 SQLite로 되돌려 앱 응답·B7 대조가 일치하고, 재실행·중간 실패 재개·덮어쓰기 금지·보존 정책을 확인했다. 표현할 수 없는 상태는 손실 목록으로 보고한다(§6). |
| 운영 전환 준비 완료인가 | **아니다.** 승인만 남은 항목(READY)과 별도로, 운영 전환 전에 해결해야 할 BLOCKED 항목 4개가 있다(§8). Oracle 주 데이터베이스 목표의 운영 충족은 완료로 보고하지 않는다. |

## 1. 시작 상태

| 항목 | 값 |
| --- | --- |
| Git | `main` = `origin/main` = `10e0d54`, 미커밋 없음, worktree 1개(B8 worktree는 이전에 제거) |
| 프로세스 | 다른 Claude 세션 1개, VS Code isort LSP. 테스트·서버 프로세스와 개발 포트(8000/8002/8010/8020/8030/1521) 점유 없음 |
| 운영 SQLite | `data/trends.sqlite3` v0, 30개 표, integrity ok, 본 파일 수정 2026-10-28 14:20(KST), WAL 0바이트, SHA-256 `38b880f5…48c0` |
| 개발 Oracle | MZ_TEST(23.26.3, AL32UTF8), 컨테이너 정지 상태 → 검증 동안만 기동 |
| 스키마 | SQLite v11, 대상 v6 → 이번 작업으로 **대상 v7**(SQLite는 v11 유지) |

## 2. Phase 1 — B7 만료 근거 이관 결함

**재현.** B8 통합 픽스처(Instagram 캡션 10일분이 30일 보존을 넘김)를 픽스처 시계 시점에 B7로 적재하자 B7이 B5의 `EXPIRED` 언급 묘비 10행을 `EXCLUDED_EXPIRED`로 버렸다. 그 10일이 SQLite에서는 `EVIDENCE_EXPIRED`(값 null), Oracle에서는 `OBSERVED`(값 0)였고, 30일 지표의 이전 기간도 `observed 0·evidence_expired 10` 대 `observed 10·evidence_expired 0`으로 갈렸다([재현 기록](phase1-reproduction/test-summary-before-fix.json), 실패 22건). Shadow 경로도 같은 결함이 있었다(만료 언급을 대상에 ACTIVE로 남김: 삭제 조건이 자연키를 못 만들어 무효).

**판단과 수정(B7 계층).** B8은 SQLite와 같은 행을 읽기만 하면 같은 답을 내므로, 행을 잃는 B7을 고치는 것이 변경이 가장 작다(B8·스키마·계약 변경 없음).
- 원문이 없는 B5 링크(`item_query_matches`·`food_mentions`·`metric_observations`)는 만료가 지나도 제외하지 않고, B5 purge가 남기는 상태로 옮긴다(ACTIVE면 `EXPIRED`로 확정, 되살리지 않음). 원문·스테이징 행(`source_contents`·`source_items`·`item_snapshots`)의 만료 제외와 캡션 필드 제거는 그대로다. 묘비에는 원문·캡션·해시태그 원문이 없다(키·FOOD_ID·별칭 ID·필드 코드뿐이며, SQLite와 Oracle 런타임이 이미 유지하는 행과 같다).
- Shadow 파생에도 같은 확정을 적용했다.
- `integrity_checks`: `expired_rows_in_target`는 "보존이 끝났는데 아직 살아 있는 행"(target 정책이 생성을 거부하는 상태)만 센다. 묘비는 `expired_tombstones_in_target`로 따로 센다. 적재 보고에 `tombstones`·`settled_by_load`를 추가했다.
- `value_state`·`coverage`·`null` 계약 변경 없음.

**검증.** 만료 이후 적재에서 B8 전 응답(707개 요청 행렬 포함)이 SQLite와 같음(대역·Oracle), 묘비 적재·정리 우선 적재와 동일 결과·재실행 쓰기 0·묘비가 ACTIVE로 되살아나지 않음·캡션 미보존·런타임 보존 정리 잔여 0, Shadow 만료 전파. 커밋 `a5a8152`.

## 3. Phase 2 — 운영 데이터 격리 사본의 Oracle 이관 리허설

도구: `food_trends/cutover.py`(단계 함수), `tests/rehearse_cutover.py`(실행·증거), `tests/test_cutover.py`(합성 v0 회귀). 리허설은 루프백 외 소켓과, 운영 파일에 대한 읽기 전용이 아닌 모든 연결을 거부한 상태에서 실행했다(시도 0건). 증거: [cutover-rehearsal-oracle.json](cutover-rehearsal-oracle.json).

| 순서 | 결과 |
| --- | --- |
| 1 읽기 전용 검사 | `mode=ro`·`query_only`. v0, 30개 표 7,701행, integrity ok, FK 0, WAL 0 |
| 2 격리 사본 | backup API(읽기 전용 원본 연결) → 임시 폴더, 표·행 수 동일, 리허설 후 삭제 확인 |
| 3 사본 v0→v11 | 1~11 적용, 원래 30개 표의 열·행 해시 보존 |
| 4 원본 대조(lineage) | RSS 회차 95/95, RSS 항목 370/370(시각·출처·payload_ref 불일치 0), 관측 900 → run_items 900·트래픽 지표 900, Instagram 185/185(불일치 0). **YouTube 431건은 provenance가 모두 `live_api`라 B4 스테이징 경로가 없다**(레거시 보충은 출처 불명 행만, 레거시 수집기는 스테이징하지 않음) → 원문만 이관, 검색 근거 473건은 표현되지 않음 |
| 5 분류 | 73개 표 전부 처분 지정(MIGRATED 31·STAGED 4·EMBEDDED 2·LEDGER 3·RUNTIME 9·EXCLUDED 24, 미분류 0). 행 단위 제외·거부 0 |
| 6 Oracle 적재 | 15,671행, 11.7초, 레거시 사용량 원장 0행(열린 창 없음)·계정 2개 IMPORTED |
| 7 대조 | 29개 표 누락·초과·변경 0, 해시 29/29 일치, FK 고아 0·미검증 FK 0, payload_ref 미해결 0, 정책 위반 0, 만료 잔존 0, 키 형식·자연키 중복 0, B8 응답 17건×2(origin) 동일 |
| 8 재실행 | 완료 배치 재실행 쓰기 0, 배치 기록 삭제 후 전체 재실행 쓰기 0(15,671행 unchanged), 행 수 불변, 재대조 일치. 적재 직후 역방향 복구 dry run도 차이 0 |
| 만료 시나리오 | 같은 사본을 2026-12-01 기준 적재: YouTube 원문 431건 보존 만료로 제외, 캡션 184건 제거, 15,240행, 대조 일치 |

대조 항목별 결과:

| 항목 | 결과 |
| --- | --- |
| FOOD_ENTITY / ALIAS / RELATION | 0 / 0 / 0. v0에는 음식 개체 표가 없고 승인된 음식이 없다. **FOOD_ID를 임의 부여하지 않았다** |
| 후보·승인 이력 | 개체 후보(food_candidates)·결정 이력 0. RSS 분류 후보 370건(NON_FOOD 369, REVIEW 1, FOOD 0)과 분류 이력 251건 이관 |
| TRACKING_TARGET / Query Planner | 0. 레거시 검색 정의(youtube_queries 6, instagram_hashtags 1)는 승인 음식의 추적 대상이 아니므로 이관하지 않음(검색 연결은 `instagram_hashtag:N` query_ref로 남음) |
| 출처별 회차·수집 근거 | ingest_runs: RSS COMPLETE 90·NONE 5, Instagram·YouTube 출처 불명 논리 회차 각 1. source_items RSS 370·Instagram 185, source_contents RSS 370·Instagram 185(캡션 184 보유)·YouTube 431 |
| FOOD_MENTION | 0(승인 음식 없음) |
| METRIC_OBSERVATION | `rss.approx_traffic` OBSERVED 900(legacy_unknown) |
| 품질·Coverage | quality_results PASS 9,493·WARN 8·NOT_APPLICABLE 901, run_items PASS 1,084·WARN 1 |
| 만료·삭제 | AVAILABLE만 존재, 묘비 0(실행 시점 만료 없음) |
| 출처 정책·Origin | 출처 불명 555건 `legacy_unknown` 유지(승격 0), YouTube `live_api` 유지, NAVER 저장 0 |
| 제외 행과 사유 | 행 단위 0. 표 단위 제외는 처분표(분석 집계·수집기 상태·레거시 정의·원시 호출 기록) |

커밋 `1aeabe3`(도구), `5af42a3`(대상 v7·복구 dry run 포함 증거 갱신).

## 4. Phase 3 — Oracle 기반 전체 백엔드 검증

**보완한 부분.** ORACLE-RUNTIME-2까지 음식·별칭 결정과 추적 대상 명령은 SQLite에서만 실행됐다. 기존 패턴대로 `FoodService`·`TrackingService`의 판단 로직은 그대로 두고 SQL만 이름 있는 저장 primitive로 옮긴 뒤(SQLite 문장 동일), `OracleFoodService`·`OracleTrackingService`가 그 primitive만 대상 SQL로 재정의한다. 모든 쓰기는 런타임 소유권 가드 트랜잭션 안에서 실행되고, 명령 테이블 배타 잠금으로 명령끼리 직렬화되며, 추적 명령은 스케줄러와 같은 순서로 대상 행을 먼저 잠근다. 대상 스키마 v7: `tracking_commands`·`tracking_events`(런타임 소유)와 이력 삽입 순서 `event_sequence`(SQLite의 rowid 순서 의미; B7은 rowid를 그대로 적재). `runtime_storage.oracle_backend()`가 모든 쓰기 경로를 한 대상에 조립한다. 커밋 `916d699`.

**검증(`tests/test_oracle_backend.py`, 대역·MZ_TEST).** 전체 흐름 동안 SQLite 연결을 가로채 대상 파일(대역) 외 모두 거부, Oracle에서는 0건 허용.

| 요구 | 결과 |
| --- | --- |
| 1 음식 등록·정확 별칭 승인 | Oracle 후보 제출→검토 생성→별칭 승인, 이름 해석 |
| 2 추적 활성화 | Instagram·YouTube·NAVER 등록·활성화 |
| 3 계획 | Preview 쓰기 0(전 표 행 수 불변), 출처 3종 계획 |
| 4 슬롯·작업 | 별칭별 Instagram 3·YouTube 3·NAVER 1 작업, 대상별 슬롯 1, Outbox 7, 한 트랜잭션 |
| 5~7 수집·정제·관측 | 7작업 SUCCEEDED, 스테이징·DQ, Instagram 확정 언급 1, 지표 3(실행마다) |
| 8 저장·상태 | 슬롯 COMPLETED, 상태 저장소 `oracle_test` |
| 9 B8 | 음식 목록, 일별 값 1·OBSERVED·COMPLETE |
| 10 중지·재개·별칭 변경 | 별칭 철회 → 언급 무효화·값 0(관측된 0), 새 별칭 승인 → 값 1, 재처리 대기 표시 해제. 별칭 변경 후 STALE 대상은 계획 제외, 중지 중 계획 제외, 갱신·재개 후 새 세대 슬롯 완료, 이력 순서 보존 |
| 중복 | 같은 요청 키 재전송 동일 결과, 다른 본문 409, 재Claim·재스테이징·재연결·재처리 후 행 수 불변, 자연키 중복 0 |
| 실패·부분·만료 | 장애일 NOT_OBSERVED(실패 1, 값 null), 페이지 상한 LIMITED, 31일 후 런타임 보존 정리 → EVIDENCE_EXPIRED |
| 정책 | RSS 추적 등록 403, NAVER 저장 0, YouTube 언급·지표 0(검색 근거만), B8 YouTube POLICY_BLOCKED·NAVER NOT_STORED·순위 422, RSS 발굴 계획은 런타임이 거부 |
| OpenAPI v1 | 7개 경로 동일, Oracle 응답이 명세와 일치 |
| 쓰기 위치 | 런타임 소유 중 Shadow·B7 거부, Shadow 소유 중 음식·추적 명령 거부·쓰기 0, SQLite 서비스는 대상·Oracle을 열지 않음 |
| 동등성 | 같은 명령 스크립트(제출·검토·거절·별칭·되돌림·관계·순환 거부·이름 변경·병합·추적 9종·재전송)가 SQLite 서비스와 같은 결과·오류(12개 거부 코드 일치) |

## 5. SQLite에 남은 필수 의존성

Phase 3 흐름 자체에는 SQLite 의존이 없다. 시스템 전체로는 다음이 SQLite에만 쓴다.

| 파일·함수 | 목적 | 영향 |
| --- | --- | --- |
| `service.py` `CollectionService.collect/_ingest`, `storage.py` `Storage`, `classifier.py` | Google Trends RSS 발굴 수집·분류·B4/B5 SQLite 처리 | 발굴 데이터는 B7로만 대상에 도착(런타임 소유 중 Shadow 불가) |
| `food_service.py` `FoodService._do_import/import_rss_run` | RSS 분류 → 음식 후보 가져오기 | Oracle 서비스는 거부 |
| `ingestion.py` `IngestionService.rss_run/legacy_backfill` | RSS 회차·출처 불명 레거시 스테이징 | Oracle 런타임은 거부 |
| `instagram_service.py`·`instagram_storage.py`·`instagram_analysis.py`, `youtube_service.py`·`youtube_storage.py`, `naver_service.py` | Live 레거시 수집기와 원본 저장소 | Oracle 런타임은 Mock만(`LIVE_NOT_SUPPORTED_BY_ORACLE_RUNTIME`) |
| `execution.py`·`execution_sources.py`·`execution_runtime.live_controller` | SQLite B3 Live 실행 | 위와 같음 |
| `api.py` `make_server`(+ `static/`) | 관리 웹 서버·UI(RSS 서비스 필수, 음식·추적·계획·실행 기본값이 SQLite 서비스) | Oracle 서비스 주입 서버 모드 없음 |
| `quota_ledger.import_legacy_usage` | 레거시 호출 사용량(예산 안전) 원천 | 레거시 수집기가 있는 동안 주기 가져오기 필요 |
| `__main__.py --maintenance`, `audit.py` | SQLite 보존 정리·감사 | Oracle에는 런타임 purge가 별도 |
| `shadow.py`·`target_migration.py`·`reverse_recovery.py`·`cutover.py` | 두 저장소 사이 이관·대조·복구(설계상 SQLite를 읽거나 씀) | 정상 |

## 6. Phase 4 — Oracle → SQLite 역방향 복구

`food_trends/reverse_recovery.py`, CLI `--recovery plan|apply`. 커밋 `5af42a3`(+ CLI·운영 경로 오류 코드는 Phase 5 커밋).

| 요구 | 구현·검증 |
| --- | --- |
| Oracle 전용 데이터·변경 이력 식별 | 공유 결정적 키로 표마다 target_only·same·mutable_updates·conflicts·sqlite_only 분류(음식·별칭·후보·이벤트·명령, 추적 대상·명령·이벤트·슬롯, 계획·예약·청구, 작업·회차·호출·Outbox·결과·매핑·Lease·체크포인트·수신, 원문, 스테이징·DQ, 링크·효과·무효화) |
| 결정적 키 대조·중복 방지 | INSERT-if-absent, 부모 우선 순서(병합 대상·되돌림 이벤트·삽입 순서 포함) |
| 표현 불가 상태 손실 기록 | 슬롯 생명주기, Instagram 원래 해시태그 표기, 캡션 수집 시각(창 끝에서 역산, 만료 시각 보존), YouTube payload 정규화 텍스트(원 해시는 snapshot에 보존). 미복구: 소유권 임대·참여자, 사용량 원장·계정 확인, 적재·Shadow 기록 |
| 복구 전 IDLE·미완료 0 | 아니면 `RecoveryRefused`(실행 중 런타임, 미완료 작업 각각 검증). 실행 내내 대상 SHADOW 임대 보유 |
| 무단 덮어쓰기 금지 | 기존 행은 mutable 열만, 대상 파일이 완료된 B7 적재의 원본 지문과 같고(WAL 비움) 운영자가 `--apply-updates`를 줄 때만 갱신. 전환 후 SQLite가 바뀐 경우 갱신 0·삽입만(검증) |
| 재실행 멱등 | 재실행 쓰기 0 |
| 보존·삭제 정책 | 만료 YouTube 원문·닫힌 캡션 미복구, 묘비는 묘비로 |
| 중간 실패 재개 | 청크 커밋, 실패 후 재실행 수렴·대조 일치(진행 기록으로 갱신 권한 유지) |
| 운영 DB 보호 | 설정된 운영 경로는 `--allow-operational-database` 없이 `operational_database_requires_approval` |

**복구 가능 범위.** Oracle 단독 기간의 음식·별칭·관계·병합·후보·이벤트·명령, 추적 대상·명령·이벤트, 계획·예약·청구, 실행 전 단계, 보존 기간 안의 YouTube·Instagram 원문, 스테이징·DQ, 언급·지표·효과·무효화. 복구 후 SQLite 서비스가 이어서 명령을 처리하고, B8 응답이 Oracle과 같고, B7 대조가 일치했다. **복구 불가 범위**: 위 손실 목록, 보존 기간이 지난 원문·캡션(정책상), RSS 분류 행이 대상에만 생긴 경우(현재 경로 없음). 운영 대상에서 `apply`는 운영 쓰기 잠금 해제가 있어야 실행된다(`plan`은 지금도 읽기 전용으로 가능).

## 7. 테스트

| 실행 | 대상 코드 | 결과 |
| --- | --- | --- |
| 기본 전체(오프라인 러너, Oracle 컨테이너 정지) | 최종 코드 | 490개 실행, 실패·오류 0, 실제 Oracle 전용 153개 skip, 외부 접속 시도 0, 운영 DB 접근 차단, 413초 ([default-tests](default-tests/test-summary.json)) |
| 실제 Oracle 포함 전체(오프라인 러너, MZ_TEST) | 최종 코드 | 643개 실행, 실패·오류 0, **skip 0**, 외부 접속 시도 0, 운영 DB 접근 차단, 1,069초 ([oracle-full-tests](oracle-full-tests/test-summary.json)) |
| 운영 사본 리허설 | 최종 코드 | passed, 78초(§3). 이후 MZ_TEST 초기화(표 0) |

총 643개 = 기존 576개 + 신규 67개(대역 34·실제 Oracle 33). 두 전체 회귀는 순차로 실행했고 기본 실행 중에는 Oracle 컨테이너를 정지했다. 단계별 실행: Phase 1 대역 183·실제 Oracle 185 통과([phase1](phase1-standin-tests/test-summary.json)), Phase 2 합성 v0 대역 5·Oracle 10, Phase 3 대역 390(1건 실패: 새 파생 열을 모르는 스키마 범위 검사 → 예외 목록 추가) 및 Oracle 203(2건 실패: 스키마 재생성 직후 B8 읽기 전용 트랜잭션의 ORA-01466 → 테스트가 최신 DDL 후 4초 대기) → 수정 후 재실행 통과([phase3-fix-reruns](phase3-fix-reruns/test-summary.json)), Phase 4 대역 8·Oracle 포함 40. 기존 576개는 삭제하지 않았다. 기존 테스트 변경은 두 가지뿐이다: `test_app_oracle`의 적재 시점을 클래스 속성으로 바꿔 만료 이후 적재 클래스를 추가(기존 클래스 동작 동일), `test_target_schema`의 파생 열 예외 목록에 `food_events.rowid` 추가.

## 8. 운영 전환 준비도

**READY** — 개발·격리 검증 완료, 운영 승인만 필요

| 항목 | 근거 |
| --- | --- |
| G-OPS-1 운영 SQLite 백업(backup API, 읽기 전용) | 운영 파일로 리허설, 원본 불변 |
| G-OPS-3 운영 SQLite v0→v11 + 로컬 후속(스테이징·연결·보존·체크포인트) | 운영 파일의 정확한 사본으로 리허설, 원래 열·행 보존, FK 0 |
| Oracle 대상 스키마 v7 생성(`--target-migrate oracle`) | MZ_TEST v1→v7 업그레이드·재실행·체크섬 |
| B7 적재·대조·재실행 절차(`--target-load`), 만료 근거 묘비 | 운영 사본 15,671행·29개 표 해시 일치, 만료 시나리오, CLI 테스트 |
| Oracle 단독 백엔드(음식·추적·계획·실행·B4·B5·B8) | SQLite 연결 0으로 MZ_TEST 검증 |
| 역방향 복구 절차(`--recovery`) | 대역·MZ_TEST 리허설, 운영 경로 승인 스위치 |
| B8 OpenAPI v1 7개 엔드포인트 | 경로·계약 불변, SQLite·Oracle 응답 동등 |

**BLOCKED** — 운영 전환 전에 반드시 해결

| 항목 | 내용 |
| --- | --- |
| 1. 운영 Oracle 쓰기·실행 잠금 | 운영 대상의 SHADOW·RUNTIME 임대, RUNTIME_PRODUCTION, `oracle-production` 런타임·앱 저장소가 코드에서 항상 거부된다(의도된 잠금). 운영 모드 경로는 한 번도 실행·검증되지 않았으므로, G-OPS-6 승인 후 승인 스위치를 갖춘 변경과 그 검증이 필요하다(B7 적재·복구 apply 포함) |
| 2. Oracle Live 수집 | 런타임은 Mock만 실행. Instagram 원격 고유 태그 점검·출처 승인 registry·수집 소유권 이전·레거시 사용량 주기 가져오기(G-OPS-4/5) |
| 3. RSS 발굴·후보 가져오기의 Oracle 저장 | SQLite에만 저장되고 런타임 소유 중에는 Shadow로도 옮길 수 없다. 단일 Writer 전환 뒤 발굴이 멈추거나 두 저장소가 생긴다 |
| 4. 관리 웹 서버의 Oracle 연결 | `api.py`가 SQLite 저장소·서비스로 기동. UI로 등록·추적하면 SQLite에 쓴다 |

**DEFERRED** — 첫 운영에 필수 아님

| 항목 | 메모 |
| --- | --- |
| YouTube `live_api` 레거시 수집분 B4 스테이징 어댑터 | 431건, B8 정책 차단 출처, 원문은 원래 만료(2026-11-26)대로 이관 |
| SQLite 추적 명령·이력의 B7 이관과 Shadow 캡처 | 운영 데이터 0건. SQLite v12 트리거 필요 |
| Shadow 삭제 경로(자연키 표)와 YouTube 시간 만료의 자식 순서 | 현재 도달 경로 없음(만료는 묘비로 처리), 전환 절차는 B7 전체 적재 사용 |
| B5: 별칭 철회를 되돌려도 언급이 복구되지 않음 | 같은 mention_key의 무효화 행을 재사용하지 않음. SQLite·Oracle 동일(기존 동작) |
| 대상 DDL 직후 몇 초간 B8 `503`(ORA-01466) | 스키마 마이그레이션 직후에만, 재시도로 해소 |
| 운영 규모 성능(행 단위 적재: 개발 Oracle 약 1,250행/초) | 운영 서버에서 재측정 |
| 비이관 레거시 표(분석 집계·검색 정의·원시 호출 로그·수집기 상태) | 처분표에 사유 기록 |
| 저장 집계, 종합 유행 점수, B8 외부 공개 배포 | 범위 밖 |

## 9. 운영 전환 절차

[CUTOVER_RUNBOOK.md](CUTOVER_RUNBOOK.md): 백업·복구 검증 → Writer 정지 → SQLite v0→v11 → 운영 Oracle 스키마 → 적재·대조 → 단일 Writer 전환 → 수집기 → B8 → 모니터링 → 복구·전환 취소. 단계마다 현재 실행 가능 여부와 잠금을 표시했다. **이번 작업에서 실행하지 않았다.**

G-OPS 승인 후 순서: (1) G-OPS-1 백업과 백업 사본 리허설(`tests/rehearse_cutover.py --target sqlite`) → (2) G-OPS-2 Writer 정지 확인 → (3) G-OPS-3 `--migrate`·`--ingest-pending`·`--ingest-legacy`·`--maintenance`·체크포인트 → (4) BLOCKED 1~4 해결 변경을 별도 승인·검증 → (5) G-OPS-6 `--target-migrate oracle` → `--target-load oracle`(판정 기준 충족) → 앱 응답 대조 → (6) Oracle 단일 Writer 전환·수집기(G-OPS-4/5)·B8 연결 → (7) 모니터링, 이상 시 `--recovery plan` → `apply`.

## 10. 운영 환경 영향

모든 실행 뒤 읽기 전용으로 재확인했다([production-final.json](production-final.json)). 운영 마이그레이션·Live 호출·수집기 활성화·Writer 전환·운영 Oracle·운영 Shadow·`oracle-production` 잠금 해제는 하지 않았다. 리허설은 운영 파일을 읽기 전용 URI로만 열었고(읽기 연결이 `-shm` 시각만 갱신 — 이전 보고서와 같음), 사본은 임시 폴더에서 삭제됐다. MZ_TEST 초기화는 매번 기존 보호 조건(테스트 계정·DBA 아님·명세 밖 객체 없음)을 확인한 뒤에만 수행했다. 최종 리허설 뒤 같은 조건으로 MZ_TEST를 초기화(표 0)해 보존 기간이 있는 운영 사본 데이터가 개발 스키마에 남지 않게 했다. Oracle 컨테이너는 검증 동안만 기동하고 정지 상태로 되돌렸다.

## 11. 수정·추가 파일

| 구분 | 파일 |
| --- | --- |
| 수정 | `food_trends/target_migration.py`, `target_schema.py`(v7), `food_service.py`, `tracking_service.py`(저장 primitive), `runtime_storage.py`(`oracle_backend`), `__main__.py`(`--target-load`, `--recovery`), `tests/run_offline.py`(모듈·이름 선택), `tests/oracle_support.py`(`settle_after_ddl`), `tests/test_app_oracle.py`, `tests/test_target_migration.py`, `tests/test_shadow.py`, `tests/test_target_schema.py`, `README.md` |
| 추가 | `food_trends/cutover.py`, `oracle_foods.py`, `oracle_tracking.py`, `reverse_recovery.py`, `tests/rehearse_cutover.py`, `tests/test_cutover.py`, `tests/test_oracle_backend.py`, `tests/test_reverse_recovery.py`, 이 폴더의 보고서·절차서·증거 |

## 12. Git

| 커밋 | 내용 |
| --- | --- |
| `a5a8152` | fix: carry expired B5 evidence into Oracle as content-less tombstones |
| `1aeabe3` | feat: rehearse the Oracle cut-over on an isolated copy of the operational DB |
| `916d699` | feat: run food identity and tracking commands on the Oracle target |
| `5af42a3` | feat: recover the Oracle period back into SQLite (rollback path) |
| `5c750bb` | docs: record Oracle cut-over readiness, runbook and final verification(복구·적재 CLI, 보고서·절차서·README, 최종 회귀 증거) |
| 이 문단 커밋 | docs: main 통합 기록 |

`main` 통합: 최종 회귀(기본 490·실제 Oracle 643, 실패 0) 통과와 원격 `main`이 기준 `10e0d54` 그대로이며 브랜치의 조상임을 확인한 뒤, 이 보고서를 포함한 브랜치 HEAD를 `git push origin feat/oracle-cutover-prep:main`으로 Fast-forward했다(force 없음).

커밋마다 관련 파일만 스테이징하고, 스테이징 diff에서 비밀값 형태와 로컬 Oracle·컨테이너 접속 정보 값 대조를 거쳤다(비밀번호·DSN 일치 0; 일치한 것은 README에 이미 공개된 테스트 스키마 이름뿐). `origin/feat/oracle-cutover-prep`에 단계별 Push 후 원격 HEAD를 확인했다. reset hard·force push·기존 작업 삭제 없음. `.env`·DB·백업·자격 증명·로그(`*.log`는 저장소 규칙상 제외)는 커밋하지 않았다.
