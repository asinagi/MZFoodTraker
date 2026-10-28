# B3 보완·B4·B5 스키마와 데이터 계약

2026-10-28. SQLite 명시적 migration v8~v10. 모든 신규 테이블은 기존 30개 테이블과 A4~B3 테이블을 수정하지 않는 추가형이다. 원문(provider body)을 새 범용 테이블에 복사하지 않고, 기존 출처 저장소의 정책·보존 기한을 그대로 따른다.

## Migration 목록

| 버전 | 이름 | 추가 |
| --- | --- | --- |
| 8 | execution_result_coverage | `execution_runs.result_coverage`(COMPLETE/PARTIAL/LIMITED/NONE), `committed_pages` |
| 9 | common_ingestion_staging | `ingest_runs`, `source_items`, `item_snapshots`, `run_items`, `quality_results` |
| 10 | food_observation_model | `item_query_matches`, `food_mentions`, `metric_observations`, `entity_change_effects`, `aggregate_invalidations` |

## 테이블과 제약

| 논리 모델 | SQLite 테이블 | 키·제약 | 보존·의미 |
| --- | --- | --- | --- |
| INGEST_RUN | `ingest_runs` | PK 결정적 ID, UNIQUE(source_code, run_ref), FK job/query, CHECK origin·coverage(COMPLETE/PARTIAL/LIMITED/NONE/UNKNOWN)·metric_storage | 운영 메타데이터. 요청 문맥은 credential 검사 후 저장, context_hash 별도 |
| SOURCE_ITEM | `source_items` | PK `ITEM_`+sha256(source, external_key), UNIQUE(source_code, external_key), CHECK source≠naver, first_seen≤last_seen | 콘텐츠 식별·가용성(AVAILABLE/EXPIRED/DELETED)·first_origin. expires_at은 출처 저장소 값을 그대로 반영 |
| ITEM_SNAPSHOT | `item_snapshots` | UNIQUE(item_key, payload_hash, adapter_version) | 해시·필드 상태·`payload_ref`만. 본문 없음. 출처 기한 경과 시 purged_at |
| RUN_ITEM | `run_items` | PK(ingest_run_id, item_key), multiplicity≥1, quality PASS/WARN | 회차별 재관측 이력. FAIL 레코드는 연결하지 않음 |
| QUALITY_RESULT | `quality_results` | UNIQUE(event_key)=hash(run, item/subject, rule, version) | 규칙 코드·필드 이름만. 실패 레코드는 subject_hash로만 식별 |
| ITEM_QUERY_MATCH | `item_query_matches` | FK(run, item)→run_items, UNIQUE(run, item, query_ref), verification='SEARCH_ONLY' | 검색 근거. 음식 언급으로 승격하지 않음. YouTube는 원 영상 기한 그대로 |
| FOOD_MENTION | `food_mentions` | UNIQUE(mention_key)=hash(item, food, alias, field, span, resolution_version), span CHECK | 승인된 정확 별칭만. INVALIDATED는 이력 보존. 근거(캡션) 기한과 함께 만료 |
| METRIC_OBSERVATION | `metric_observations` | UNIQUE(observation_key), **CHECK source IN (RSS, Instagram)**, 값 상태와 값 일치 CHECK | NAVER·YouTube 저장은 DB 제약으로도 거부. NOT_OBSERVED/NOT_PROVIDED는 값 NULL |
| ENTITY_CHANGE_EVENT 효과 | `entity_change_effects` | UNIQUE(food_event_id) | A4 `food_events`(불변)마다 한 번만 재처리 |
| B8 무효화 계약 | `aggregate_invalidations` | UNIQUE(invalidation_key), consumed_at | B8이 집계를 다시 만들 음식 목록. 소비 시 consumed_at 기록 |

## 공통 Envelope 매핑

A2 `contracts.envelope()`를 그대로 사용하고 새 필드는 추가하지 않았다. 요청서 항목은 아래 위치에서 표현한다.

| 요청 항목 | 위치 |
| --- | --- |
| source_code, origin, contract/adapter/policy_version, request_context, provided_fields, field_states | A2 envelope → `ingest_runs`, `item_snapshots.field_states_json` |
| run_id / job_id | `ingest_runs.run_ref`, `job_id` |
| external_key, published_at | `source_items` |
| source_schema_version | `ingest_runs.source_schema_version` (DQ가 알려진 버전과 대조) |
| observed_at | `ingest_runs.observed_at`, `run_items.observed_at` |
| coverage, error_kind | `ingest_runs` (B3 `result_coverage`에서 전달) |
| allowed_payload_reference, payload_hash | `item_snapshots.payload_ref`, `payload_hash` |

필드 상태는 A2의 provided/not_provided/null/empty에 생명주기 상태 `expired`(정책 만료 캡션)·`deleted`를 추가했다.

## Oracle 이관 영향 (B6 입력)

| 항목 | SQLite 현재 | Oracle 고려 |
| --- | --- | --- |
| UPSERT | `INSERT … ON CONFLICT DO UPDATE`, `INSERT OR IGNORE` | MERGE 또는 INSERT 후 ORA-00001 재조회. 결정적 키라 재시도 멱등 |
| 시각 | ISO-8601 TEXT(UTC 오프셋 포함) | TIMESTAMP WITH TIME ZONE. DQ가 naive 시각을 FAIL 처리하므로 변환 손실 없음. KST 일자는 B8에서 계산 |
| 빈 문자열 | field_states로 empty/null 구분 | VARCHAR2 ''=NULL이므로 field_states 보존 필수 |
| JSON | TEXT + json_valid CHECK | CLOB IS JSON 또는 native JSON |
| 큰 정수 | YouTube 통계는 원래 문자열 보존(저장 지표 아님) | NUMBER(38) 또는 문자열 유지 |
| 내부 ID와 외부 키 | `ITEM_…`/`SNAP_…`는 해시 기반 텍스트, 외부 키 별도 컬럼 | NUMBER identity PK + 외부 키 UNIQUE로 바꿀 경우 LEGACY_KEY_MAP 필요 |
| 부분 UNIQUE | 별칭 활성 UNIQUE(A4)는 partial index | function-based UNIQUE |
| 삭제 전파 | 상태 전이(EXPIRED/DELETED/INVALIDATED), 물리 삭제 없음 | 동일. 보존 기한은 출처 행 기준으로만 계산 |
| 정책 CHECK | metric source CHECK | 동일 CHECK 유지 |
