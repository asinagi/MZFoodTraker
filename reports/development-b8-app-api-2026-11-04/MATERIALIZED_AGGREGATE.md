# B8 저장 집계(Materialized Aggregate) 인수 계약 — 설계, 미구현

이번 병렬 작업(PARALLEL-B8-API)은 **읽기 시 집계**만 구현했다. 새 영속 테이블·대상 스키마 버전·마이그레이션은 만들지 않았고, ORACLE-RUNTIME-2가 수정 중인 `target_schema.py`·`migrations.py`·`shadow_schema.py`를 변경하지 않았다. 이 문서는 저장 집계가 필요해질 때의 인수 조건이다.

## 1. 현재 방식 (읽기 시 집계)

- 앱 API 요청마다 `analytics_reader.ReadSession`이 B5 행(FOOD_MENTION × RUN_ITEM × INGEST_RUN, 음식별 관측 범위 METRIC_OBSERVATION)을 한 읽기 트랜잭션에서 읽고, `analytics_service.FoodAnalytics`가 KST 일자·집합·coverage를 계산한다.
- `aggregate_invalidations`는 소비하지 않는다(`consumed_at`을 쓰지 않음). 매 요청이 최신 B5 상태에서 다시 계산하므로 무효화가 결과에 영향을 주지 않는다. 대신 `food_events` 중 B5가 아직 재처리하지 않은 별칭·병합 결정을 `freshness.reprocessing_pending`, `aggregation.pending_entity_changes`로 표시한다.
- MVP 규모(B7 보존 복제본 기준 RSS 회차 87, 콘텐츠 711, 검색 근거 400)와 Mock 데이터셋에서는 응답 시간이 충분히 짧다(측정값은 [REPORT.md](REPORT.md)).

## 2. 저장 집계 도입 조건

다음 중 하나가 충족되면 저장 집계를 설계 승인 후 구현한다.

1. 로컬 측정 p95 응답 시간이 순위 API 기준 500 ms를 넘거나, 음식×콘텐츠×회차 관측 행이 약 20만 행을 넘을 때
2. 외부 공개를 위해 앱 API를 운영 DB와 분리된 읽기 복제본·사전 계산 결과에서 제공해야 할 때 ([ACCESS.md](../../docs/app-api/ACCESS.md) §6)
3. Oracle 전환 후 읽기 시 집계가 운영 쓰기 트랜잭션과 경합할 때

## 3. 인수 계약 (테이블 후보)

주별·기간 고유 콘텐츠 수는 일별 개수로 합산할 수 없으므로(같은 콘텐츠가 여러 날 관측됨) **일별 개수와 일별 콘텐츠 소속을 함께** 저장해야 한다. 근사 스케치(HyperLogLog 등)는 정확한 고유 수 계약을 깨므로 쓰지 않는다.

### B8_FOOD_DAY (음식·출처·일 단위 상태)

| 컬럼 | 의미 |
| --- | --- |
| `aggregation_version` | 집계 규칙 버전 (`b8-read-model-v1`). 규칙이 바뀌면 새 버전으로 다시 만들고 이전 버전은 읽지 않음 |
| `source_id` | `google_trends_rss` / `instagram_graph` (정책상 INCLUDED 출처만, DB CHECK) |
| `food_id` | 집계 시점의 FOOD_ID. 병합 리다이렉트는 읽을 때 `merged_into`로 올림 |
| `metric_code` | `confirmed_items` |
| `metric_day` | KST 일자 (`YYYY-MM-DD`) |
| `comparison_context` | 관측 origin(`live_api`/`mock`)·관측 범위 기준(SHARED_FEED / PER_FOOD_QUERY)·단위의 해시. 다르면 합치지 않음 |
| `value_state` | `OBSERVED` / `NOT_OBSERVED` / `EVIDENCE_EXPIRED` |
| `value` | 고유 콘텐츠 수 (`OBSERVED`일 때만) |
| `coverage` | 그날 검색 조건별 최선 회차 중 최저값 (`COMPLETE`/`LIMITED`/`PARTIAL`/`NONE`) |
| `successful_runs`, `failed_runs` | 관측 범위 회차 수 |
| `legacy_unknown_value` | 출처 불명 데이터 고유 콘텐츠 수 (본 값과 별도) |
| `invalidation` | 마지막으로 반영한 `aggregate_invalidations.invalidation_key` 또는 `ingest_run_id` (재계산 원인 추적) |
| `computed_at` | 계산 시각 |
| `expires_at` | 그날 콘텐츠 근거 중 가장 이른 만료 시각. 지나면 그날을 다시 계산해 `EVIDENCE_EXPIRED`로 바꿈 |

키: (`aggregation_version`, `source_id`, `food_id`, `metric_code`, `metric_day`, `comparison_context`).

### B8_FOOD_DAY_ITEM (일별 콘텐츠 소속)

(`aggregation_version`, `source_id`, `food_id`, `metric_day`, `item_key`, `comparison_context`, `warn`) — 주·기간 값은 `count(DISTINCT item_key)`로 다시 계산한다. 원문·캡션은 복사하지 않고 `item_key`만 둔다. 출처 항목이 EXPIRED/DELETED가 되면 해당 행을 지우고 그날을 다시 계산한다.

## 4. 갱신·무효화 규칙 (B5 계약 준수)

1. **새 관측**: B5 `link_run`이 끝난 회차의 (출처, 음식, KST 일자)만 다시 계산한다. RSS(공유 피드)는 그날의 모든 음식 상태가 바뀔 수 있으므로 그날 전체를 다시 계산한다.
2. **엔티티 변경**: `aggregate_invalidations.consumed_at IS NULL`인 음식의 모든 일자를 다시 계산한 뒤 같은 트랜잭션에서 `consumed_at`을 채운다. Shadow 전달 후 Oracle에도 같은 행이 있으므로 **한 저장소만** 소비한다(전환 전에는 SQLite).
3. **만료·삭제**: `expires_at` 경과, 출처 항목 EXPIRED/DELETED, FOOD_MENTION EXPIRED/INVALIDATED는 상태 전이로 반영하고 과거 응답을 재생성하지 않는다.
4. **병합**: 저장은 원래 FOOD_ID로 두고 읽을 때 루트로 올리거나, 병합 무효화를 받아 대상 음식으로 다시 계산한다(둘 중 하나로 고정).
5. **동등성**: 저장 집계 경로의 API 응답은 읽기 시 집계 경로와 바이트 단위로 같아야 한다. `tests/test_food_analytics.py`의 대상 스키마 동등성 테스트처럼 같은 입력으로 두 경로를 비교하는 테스트를 인수 조건으로 둔다.

## 5. 스키마 변경 절차 (ORACLE-RUNTIME-2 완료 후)

[B8_HANDOFF.md](../development-b6-b7-2026-11-02/B8_HANDOFF.md)의 규칙을 따른다.

- 테이블을 `target_schema.TABLES` 명세에 추가하고 대상 스키마 버전을 올린다(현재 2, ORACLE-RUNTIME-2 결과에 맞춰 번호 확정).
- SQLite에도 명시적 migration을 추가한다(현재 11).
- Shadow 캡처 목록(`shadow_schema.CAPTURED`)에 넣어 B7 대조가 검사하도록 한다. 빠지면 대조가 이 테이블을 확인하지 않는다.
- 이식 SQL(이름 바인드, `LIMIT`·SQLite 함수 미사용)로 쓰고, Oracle에서는 `MERGE` 또는 INSERT 후 ORA-00001 재조회로 멱등성을 유지한다.
