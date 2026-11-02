# Oracle 대상 스키마 v1 (B6)

2026-11-02. 명세 원본은 `food_trends/target_schema.py`이고, 같은 명세에서 Oracle DDL과 SQLite 대역 DDL을 만든다. 생성된 Oracle DDL 73문은 [oracle-target-ddl-v1.sql](oracle-target-ddl-v1.sql)에 있다. **이 DDL은 실제 Oracle에서 실행되지 않았다(NOT_VERIFIED).** 정적 검사와 SQLite 대역 실행만 통과했다.

## 범위

| 영역 | 대상 테이블 | 비고 |
| --- | --- | --- |
| 음식 개체·후보·결정 | food_commands, food_entities, food_aliases, food_relations, food_candidates, food_events | `food_candidates.source_item_id`(RSS 정수) → `source_item_key`(결정적 키) |
| 추적·계획·예산 | tracking_targets, planner_queries, planner_plans, planner_query_targets, quota_reservations | |
| 실행 이력 | execution_jobs, execution_runs, execution_calls, execution_outbox, execution_results | `mode` → `exec_mode`(Oracle 예약어). Lease·체크포인트·수신 영수증은 실행 중 런타임 상태라 제외 |
| B4 스테이징·품질 | ingest_runs, source_items, item_snapshots, run_items, quality_results | |
| B5 연결 | item_query_matches, food_mentions(+source_code), metric_observations, entity_change_effects, aggregate_invalidations | |
| 원본 참조 | source_contents | `payload_ref` 그대로 키. RSS items·YouTube 영상·Instagram 게시물(캡션은 30일 창 안에서만) |
| RSS 분류 | rss_classifications, rss_classification_history | 정수 ID → 결정적 키 |
| 이관·Shadow 관리 | legacy_key_map, migration_batches, migration_rejections, shadow_applied, target_schema_migrations | |

기존 30개 legacy 테이블 중 이관 대상에서 제외한 것과 사유: 운영 잠금(instagram_leases, naver_guards, youtube_state, instagram_state), 출처별 원시 호출 기록(naver_calls, youtube_calls, youtube_runs, instagram_runs, instagram_searches), 태그 단위 집계(instagram_metrics, instagram_analysis_*), legacy 후보·질의 매핑(naver_targets, youtube_foods 등). RSS 회차 원문(`runs.raw_response`)과 관측(`observations`)은 B4 스테이징(ingest_runs/run_items)과 source_contents로 대체 표현된다. 제외 테이블의 행별 이관은 G-OPS-6 전에 결정할 후속 과제다.

## 자료형

| 명세 종류 | Oracle | SQLite 대역 | 길이·검증 |
| --- | --- | --- | --- |
| id / key / code / text / hash | VARCHAR2(128/512/64/1000/64 CHAR) | TEXT | 바인드 전 문자 수 검사, 1000 CHAR는 AL32UTF8 4000바이트 이내 |
| clob | CLOB | TEXT | |
| json | CLOB + `CHECK (col IS JSON)` | TEXT + `json_valid` | 정규 JSON(sort_keys, 공백 없음)으로 저장 |
| ts | TIMESTAMP(6) WITH TIME ZONE | TEXT(UTC ISO, 마이크로초) | 시간대 없는 값 거부, 저장 전 UTC 변환, Oracle 바인드는 `DB_TYPE_TIMESTAMP_TZ` |
| int | NUMBER(19) | INTEGER | 절댓값 10^19 미만. YouTube 통계 같은 정확한 큰 수는 JSON 안 문자열 유지 |
| real | BINARY_DOUBLE | REAL | 유한값만 |

빈 문자열: Oracle은 VARCHAR2·CLOB의 ''를 NULL로 저장한다. 원본에서 ''가 합법인 NOT NULL 열(`food_candidates.description`)만 `empty_as_null`로 선언해 Oracle에서는 nullable, 읽을 때 ''로 복원한다. 그 밖의 NOT NULL 텍스트에 ''가 오면 두 DBMS 모두 거부한다(`empty_string_not_representable`). 출처 필드의 빈값·결측 구분은 `field_states` JSON에 있으므로 손상되지 않는다.

## 제약

- PK·UNIQUE는 SQLite v10과 같다. 결정적 키(mention_key, observation_key, event_key, invalidation_key, food_event_id, (run,item,query_ref))에 UNIQUE, 업서트는 이 논리 키로 한다.
- 활성 별칭 유일성: Oracle 함수 기반 UNIQUE 인덱스 `(CASE WHEN status='ACTIVE' THEN … END, …)`, SQLite 부분 인덱스.
- FK는 모두 `DEFERRABLE INITIALLY DEFERRED`(커밋 시 검사)이고 모든 자식 FK에 선두 인덱스를 자동 생성한다.
- 정책 CHECK: `metric_observations.source_code`·`food_mentions.source_code IN ('google_trends_rss','instagram_graph')`, origin 집합, 상태·커버리지 값, 값 상태와 값 일치, span 범위.
- 제약·인덱스 이름은 30자 이하 해시 기반으로 고정했다(Oracle 오류 매핑과 12.1 이하 호환).

## SQLite Repository와 Oracle Repository의 차이

| 항목 | SQLite 서비스(현행 기록 시스템) | 대상 Repository(Oracle/대역) |
| --- | --- | --- |
| 쓰기 주체 | 수집기·B3~B5 서비스가 직접 SQL | B7 이관과 Shadow 전달만. 서비스의 런타임 쓰기는 아직 SQLite |
| 트랜잭션 | `BEGIN IMMEDIATE` 직렬화 | Oracle 기본 트랜잭션, 커밋 시 지연 FK 검사, DDL은 암묵 커밋(마이그레이션은 객체 존재 확인 후 재개) |
| 업서트 | `INSERT OR IGNORE`, `ON CONFLICT` | 조회 후 INSERT, ORA-00001 시 재조회 후 UPDATE |
| 시각 | 원본 오프셋 그대로 ISO TEXT | UTC 순간값(TSTZ) |
| 불변성 | 서비스 로직 | Repository가 불변 열 변경·origin 승격을 거부 |
| 조회 | FoodObservationService.food_view | TargetRepository.food_view(이식 SQL), 결과 대조 |

B3 Lease 선점(`claim`)과 B4/B5 처리 로직의 Oracle 직접 실행(`FOR UPDATE SKIP LOCKED` 등)은 아직 구현하지 않았다. 현재 단계의 Oracle 역할은 이관·Shadow 대상과 읽기 모델이다.
