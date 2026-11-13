# 데이터 파이프라인 감사 — 수집 이후 실제 코드 흐름

- 감사일: 2026-11-13 (읽기 전용 감사, 코드·DB·설정 변경 없음, 외부 API·서버·수집기·테스트 실행 없음)
- 범위: `food_trends/` 수집 이후 단계, SQLite 저장소(`data/trends.sqlite3`, `data/ops-prod/production-source-v11.sqlite3`), 관련 테스트 코드·저장된 테스트 로그, `reports/` 설계·인수 문서
- 범위 밖: 운영 Oracle `MZ_APP`의 행 수(다른 감사자가 집계한다). 이 문서에서 Oracle 수치는 **보고서에 적힌 값의 인용**이며 직접 확인하지 않았다(`미확인` 표시).
- 확인 방법: SQLite는 `sqlite3.connect('file:...?mode=ro', uri=True)`로만 열어 `count(*)`·`GROUP BY`를 실행했다.

이 문서는 세 가지를 구분해 쓴다.

| 표시 | 뜻 |
| --- | --- |
| **설계** | `reports/`의 설계 문서에만 있음 |
| **구현** | `food_trends/` 코드와 테스트가 있음 (테스트는 격리 DB·Mock 전송으로만 돎) |
| **실데이터 실행** | 실제 수집된 데이터가 담긴 DB에 그 단계의 결과 행이 있음 |

---

## 0. 요약

1. **코드의 실제 순서는 참고 순서와 다르다.** 음식 판정(규칙 분류)과 1차 중복 처리는 별도 전처리 단계가 아니라 **원천 저장 트랜잭션 안에서** 일어난다. 공통 전처리(B4)는 그 다음에 돈다. 실제 순서는 다음과 같다.
   `수집 → 파싱 → 원천 저장 + 항목 단위 중복 제거 + 규칙 분류(1차 음식 판정) → 후보 생성(FOOD/REVIEW만) + 정확 별칭 자동 연결 → B4 공통 전처리(Envelope·staging·DQ·2차 중복) → B5 연결(검색 근거·별칭 언급·지표) → [사람 승인: FOOD_ID 생성·별칭 확정] → 추적 등록·활성화(승인) → 요청 계획·예산 → 실행(outbox·lease) → 다시 B4/B5 → 별칭 변경 재처리(별도 명시 단계) → B8 조회 시점 집계`
2. **SQLite 실데이터에서 실행된 단계는 수집, 원천 저장, 규칙 분류, B4 전처리, B5 연결 일부(검색 근거·RSS 트래픽 지표)뿐이다.** 음식 개체·별칭·후보·추적·계획·실행·언급 테이블은 이관 사본에서 **모두 0행**이다. 원본 운영 DB(`data/trends.sqlite3`)는 스키마 v0라 이 테이블들이 **아예 없다**.
3. 실제 RSS 370개 항목의 분류 결과는 **NON_FOOD 369 / REVIEW 1(`호박`) / FOOD 0**이다. 이 REVIEW 1건조차 음식 후보(`food_candidates`)로 들어가지 않았다(0행). 후보 생성 다리(`import_rss_run`)가 이 회차들에는 한 번도 돌지 않았기 때문이다(§3.4).
4. `두쫀쿠 ↔ 두바이 쫀득 쿠키` 연결은 **사람이 승인한 정확 별칭**으로만 이루어진다. 문자열 유사도, 띄어쓰기 제거, 공동 출현 같은 자동 연결은 없다. 이 연결은 **테스트·Mock에서만** 확인됐다. 보고서에 따르면 운영 Oracle에는 `두쫀쿠` 음식 1개가 **추가 별칭 없이** 승인됐다(미확인). 그러므로 운영에서 `두바이 쫀득 쿠키`는 현재 같은 FOOD_ID로 묶이지 않는다.
5. `짭쫀쿠`는 `두쫀쿠`의 별칭으로 자동 흡수되지 않는다. 검토자가 `create_variant`를 고르면 **FOOD_ID가 다른 별도 개체**가 되고 `VARIANT_OF` 관계로 연결된다. 집계는 서로 합산하지 않는다. 이것도 테스트에서만 확인됐다.

---

## 1. 단계별 실행 현황 한눈에 보기

수치는 이관 사본 `data/ops-prod/production-source-v11.sqlite3`(v0 운영 원본을 v11로 올린 사본, 2026-11-07 생성)에서 직접 센 값이다.

| # | 단계 (실제 순서) | 주요 모듈 | 대상 테이블 | 설계 | 구현 | 실데이터 실행 | 실데이터 행 수 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 수집 (Google Trends RSS) | `collector.py`, `service.py` | `runs` | O | O | **O** | 회차 95 (성공 90·실패 5) |
| 2 | 원천 저장 + 항목 단위 중복 제거 | `storage.py:finish` | `items`, `observations` | O | O | **O** | 항목 370 · 관측 900 |
| 3 | 규칙 분류 (1차 음식 판정) | `classifier.py:classify`, `storage.py:_classify` | `candidates`, `classification_history` | O | O | **O** | 370 (NON_FOOD 369·REVIEW 1·FOOD 0) · 이력 251 |
| 4 | 음식 후보 생성 + 정확 별칭 자동 연결 | `food_service.py:import_rss_run` | `food_candidates`, `food_events` | O | O | **X** | 0 · 0 |
| 5 | B4 공통 전처리 (Envelope·staging·DQ) | `ingestion.py`, `data_quality.py` | `ingest_runs`, `source_items`, `item_snapshots`, `run_items`, `quality_results` | O | O | **O** (이관 시 일괄) | 97 · 555 · 840 · 1,085 · 10,402 |
| 6 | B5 연결: 검색 근거 | `food_observations.py:link_run` | `item_query_matches` | O | O | **O** (레거시 Instagram 해시태그) | 185 (전부 `SEARCH_ONLY`) |
| 6 | B5 연결: 음식 언급 | 같음 | `food_mentions` | O | O | **X** (승인 별칭 0) | 0 |
| 6 | B5 연결: 지표 | 같음 | `metric_observations` | O | O | **O** (RSS 트래픽만) | 900 (`food_id` 전부 NULL) |
| 7 | 사람 승인 (FOOD_ID·별칭·관계·병합) | `food_service.py:command` | `food_entities`, `food_aliases`, `food_relations`, `food_commands` | O | O | **X** | 0 · 0 · 0 · 0 |
| 8 | 추적 등록·활성화 | `tracking_service.py` | `tracking_targets`, `tracking_events`, `tracking_slots` | O | O | **X** | 0 |
| 9 | 요청 계획·예산 | `query_planner.py`, `quota.py` | `planner_*`, `quota_*` | O | O | **X** | 0 |
| 10 | 실행 (outbox·lease·fencing) | `execution.py`, `execution_sources.py` | `execution_*` | O | O | **X** | 0 |
| 11 | 별칭 변경 재처리 | `food_observations.py:apply_entity_changes` | `entity_change_effects`, `aggregate_invalidations` | O | O | 호출은 됐으나 대상 0 | 0 · 0 |
| 12 | B8 조회 시점 집계 | `analytics_reader.py`, `analytics_service.py` | (쓰기 없음) | O | O | 읽을 음식이 0이라 SQLite에서는 빈 결과 | — |
| — | 종합 유행 점수 | `COMPOSITE_SCORE_PROPOSAL.md` | — | 제안만 | **X** (의도적으로 미구현) | X | — |

운영 원본 `data/trends.sqlite3`: `PRAGMA user_version=0`이고 `schema_migrations` 테이블이 없다. 단계 1~3의 테이블(`runs` 95, `items` 370, `observations` 900, `candidates` 370, `classification_history` 251)과 기존 Instagram/YouTube/NAVER 테이블만 있다. 단계 4~11의 테이블은 **존재하지 않는다**.

---

## 2. 실제 코드 순서 (RSS 한 회차 기준)

진입점은 `food_trends/service.py:15 CollectionService.collect()`이다.

```
collect()
 ├─ storage.start(origin)                         → runs 1행 (contract_json 포함)
 ├─ fetcher()  = collector.fetch                  → HTTP GET trends.google.com/trending/rss?geo=KR (5MB 상한)
 ├─ collector.parse(raw)                          → 항목 목록 (DTD/ENTITY 거부, source_id = sha256(SOURCE,title,pubDate,link))
 ├─ storage.finish(run_id, items, …)              ← 한 트랜잭션
 │    ├─ 같은 응답 안 source_id 중복 skip
 │    ├─ INSERT OR IGNORE items / UPDATE last_seen,payload   (회차 간 중복 제거)
 │    ├─ _classify → classifier.classify(item)   → candidates UPSERT (+ 결과가 바뀌면 classification_history)
 │    └─ INSERT OR IGNORE observations(run_id,item_id)
 ├─ FoodService.import_rss_run(run_id)            → FOOD/REVIEW 항목만 food_candidates (실패해도 수집은 보존: pending_retry)
 └─ _ingest(run_id)
      ├─ IngestionService.rss_run(run_id)         → B4: ingest_runs / source_items / item_snapshots / run_items / quality_results
      └─ FoodObservationService.link_run(ingest_run_id) → B5: item_query_matches / food_mentions / metric_observations
```

수집 이후의 흐름은 사람이 명령할 때만 진행된다.

```
사람 검토  FoodService.command('review', approved=True, actor_ref, reason, expected_revision)
             → food_entities(FOOD_ID 생성) + food_aliases + food_relations + food_events(불변 이력)
추적       TrackingService.command('register') → PENDING, 'activate'(approved=True) → ACTIVE
계획       QueryPlanner: 승인 별칭 → Instagram 해시태그 / YouTube q / NAVER keywordGroups(5개 이하)
예산       QuotaManager: preview(쓰기 없음) → reservation
실행       ExecutionService.enqueue → claim(lease, fencing_generation) → request → finish
             → IngestionService.execution_run → FoodObservationService.link_run
재처리     --maintenance / oracle_maintenance → apply_entity_changes()
조회       B8 analytics_service (read-only, 조회 시점 집계)
```

참고 순서와 다른 점:

- **"전처리 → 중복 처리 → 음식 인식"이 아니다.** 1차 중복 제거와 규칙 분류(음식 인식 1차)는 원천 저장과 **같은 트랜잭션**(`storage.py:76-119`)에서 일어난다. B4 "전처리"는 이미 저장·분류된 행을 읽어 계보·품질만 기록하며 **원문을 바꾸지 않는다**(`ingestion.py` 모듈 docstring: "No provider call … no provider body is copied").
- **"후보 생성"이 B4보다 먼저다.** `service.py:29`(후보 생성)가 `service.py:34`(B4)보다 앞에 있다.
- **"승인"은 수집 흐름 밖에 있다.** 비동기 사람 작업이며 자동으로 일어나지 않는다. 별칭 연결 가운데 자동인 것은 이미 승인된 정확 별칭과 **완전히 일치할 때**뿐이다.
- **RSS는 추적 대상이 아니다.** `tracking_targets.source_id`의 CHECK는 `instagram_graph`, `youtube`, `naver_datalab`만 허용한다(`tracking_schema.py`). RSS는 모든 음식이 함께 쓰는 발굴 피드로 남는다.

---

## 3. 단계별 상세

### 3.1 수집 — `collector.py`, `service.py`

| 항목 | 내용 |
| --- | --- |
| 입력 | `https://trends.google.com/trending/rss?geo=KR` 응답 바이트 |
| 출력 | 항목 dict(`source_id`, `keyword`, `published_at`, `traffic`, `news[]`, `related_queries`, `raw_xml`, `provided_fields`) |
| 규칙 | 응답 5MB 초과는 거부. `<!DOCTYPE>`/`<!ENTITY>`가 있으면 거부(XXE 방지). `title`이 없는 항목은 회차 전체를 실패로 처리. `pubDate`를 해석하지 못하면 경고만 남기고 날짜를 지어내지 않음 |
| 식별자 | `source_id = sha256(SOURCE \x1f title \x1f published \x1f link)` |
| origin | `fetcher`를 주입하면 `mock`, 아니면 `live_api` (`service.py:12`) |
| 실데이터 | `runs` 95: 성공 90, 실패 5. 기간 2026-10-13 ~ 2026-10-28. 성공 회차마다 항목 10개. **전부 `origin=legacy_unknown`**(계약 메타데이터가 생기기 전에 수집된 행이라 출처 라벨이 기록되지 않음) |
| 테스트 | `tests/test_foundations.py`, `tests/test_audit_improvements.py` 등. 모두 `fetcher=lambda _: (fixture, 200)` 주입이라 네트워크를 쓰지 않음 |

### 3.2 원천 저장 + 1차 중복 처리 — `storage.py:76 finish`

| 항목 | 내용 |
| --- | --- |
| 중복 규칙 | ① 한 응답 안의 같은 `source_id`는 건너뜀(`unique_ids`). ② `INSERT OR IGNORE items`로 회차 간 같은 항목은 한 행만 둠. `last_seen`과 `payload`는 최신 값으로 덮어씀. ③ `observations`는 `(run_id,item_id)`당 1행 |
| 주의 | 키워드가 같아도 `pubDate`가 다르면 **다른 항목**이다. 실데이터에서 키워드 9개가 항목 2~3개로 나뉘어 있다(예: `카드뉴스` 3, `국민연금` 2). 이 단계는 "같은 검색어"가 아니라 "같은 피드 항목"을 중복으로 본다 |
| 실데이터 | `items` 370 (고유 키워드 360), `observations` 900 (90회차 × 10). 한 항목이 관측된 회차 수 분포: 1회 135개, 2회 116개, 3회 63개, …, 10회 5개 |

### 3.3 규칙 분류 = 1차 "음식인가" 판정 — `classifier.py:26 classify`

버전은 `target-context-rules-v2`다. 점수는 "규칙 점수 0–100; 확률 또는 보정된 신뢰도가 아님"(`SCORE_BASIS`)으로 명시돼 있다.

판정 순서:

1. **검색어 형태로 제외**: 장소(`…역`, `…시/군/구`, `식당·카페·맛집`으로 끝남), 인물 직책, 기관, 비식품 상품(전기차·스마트폰 등), 행사 → `NON_FOOD`
2. **검색어 자체가 음식 범주 단어로 끝나면**(`…쿠키`, `…케이크`, `…라떼` 등) → `FOOD` 70점, `FOOD_PRODUCT`
3. **뉴스 제목이 검색어를 직접 음식 범주로 정의하면**(`target_pattern` + 범주어, 조사 허용) → `FOOD` 85점
4. **검색어와 식품·메뉴 출시가 연결되면** → `FOOD` 75점, `entity_type=FOOD_BRAND`
5. **맛·식감·레시피 속성만 붙거나 농산물 선발·무게 문맥이면** → `REVIEW` 40점 / 25점
6. 그 밖은 → `NON_FOOD` 0점. 근거는 "UNKNOWN을 자동 REVIEW로 처리하지 않음". 배경 뉴스에 음식 단어가 있어도 그것을 검색어의 음식성으로 옮겨 붙이지 않는다

동음이의·유사 표현에 대한 장치:

- `target_pattern()`은 검색어 앞뒤 경계를 검사하므로 `쿠키`가 `쿠키런`에 매치되지 않는다
- 인물 역할(`셰프`, `대표`)이 붙으면 음식 문맥이 있어도 `PERSON`으로 제외한다

실데이터 결과(`candidates` 370행, 전부 v2):

| classification | entity_type | 건수 |
| --- | --- | ---: |
| NON_FOOD | UNKNOWN | 268 |
| NON_FOOD | EVENT | 31 |
| NON_FOOD | ORGANIZATION | 31 |
| NON_FOOD | NON_FOOD_PRODUCT | 17 |
| NON_FOOD | LOCATION | 14 |
| NON_FOOD | PERSON | 8 |
| REVIEW | UNKNOWN | 1 (`호박`, item_id 89, 25점, "농산물 선발/무게 맥락") |
| FOOD | — | **0** |

`classification_history`는 251행(항목 247개)이다. 규칙을 다시 적용하면서 바뀌기 전 결과를 남긴 것이다. 이전 버전 `context-rules-v1`의 **FOOD 1건(`광주송정역`)과 REVIEW 15건**(`렉서스`, `종신보험`, `청바지`, `애플`, `식품의약품안전처장` 등)이 v2에서 모두 NON_FOOD로 바로잡혔다. 분류기를 강화한 결과가 실제 데이터에 남아 있다는 근거다.

테스트: `tests/test_audit_improvements.py`(6개). 여기서 `두쫀쿠`/`두바이 쫀득 쿠키` + "신상 디저트 판매" → FOOD, `두쫀쿠의 식감 인기` → REVIEW를 고정 입력으로 검증한다. **실제 피드에는 두 이름이 없다**(실데이터 `items.payload`에서 `두쫀쿠`·`두바이…쫀득`·`짭쫀쿠` 검색 결과 0건).

### 3.4 음식 후보 생성 + 정확 별칭 자동 연결 — `food_service.py:420 import_rss_run`

| 항목 | 내용 |
| --- | --- |
| 입력 | 성공한 회차의 관측 중 `candidates.classification IN ('FOOD','REVIEW')`인 항목(`_rss_run_items`, 207행) |
| 변환 | `entity_types.candidate_kind(entity_type, classification)`: `FOOD_PRODUCT`→`PRODUCT`. `FOOD_BRAND`/`BRAND`/`ORGANIZATION`→`BRAND`. `STORE`→`STORE`. 차단 유형이거나 NON_FOOD이면→`NON_FOOD`. 나머지→`UNKNOWN` |
| type_gate | BRAND/STORE/NON_FOOD는 `BLOCKED_NON_FOOD`(승격 불가, DB CHECK로도 막힘). UNKNOWN은 `REQUIRES_TYPE_APPROVAL`(사람이 유형을 지정해야 함). 음식 유형은 `FOOD` |
| 상태 | `NEW → NEEDS_REVIEW → RESOLVED / REJECTED` |
| 자동 연결 | `_resolve()`: 음식 유형이고 `normalized_form`이 **이미 승인된 ACTIVE 별칭과 정확히 같을 때만** `RESOLVED`(`APPROVED_EXACT_ALIAS`). **새 FOOD_ID나 추적 대상은 만들지 않는다**(docstring "never creates foods or tracking targets") |
| 멱등성 | `candidate_key = sha256([source, item_id])`, `request_key = rss-resolution:{run}:{item}`이므로 같은 요청을 다시 보내면 같은 결과가 돌아온다 |
| 정책 | `google_trends_rss` 이외 출처의 음식 추출 요청은 403으로 거부(`_do_import`) |
| 주의 | 분류기가 `FOOD`로 판정한 `FOOD_BRAND`는 후보 단계에서 `BRAND`가 되어 **차단**된다. 분류기의 FOOD와 "음식 개체"는 같은 것이 아니다 |

실데이터: **`food_candidates` 0행.** 원인은 다음과 같다.

- 95개 회차는 모두 스키마 v0 시절(엔티티 테이블이 없을 때)에 수집됐다. 그래서 `collect()` 안의 `import_rss_run`은 실패해 `pending_retry`로 끝났거나 아직 존재하지 않던 코드였다(어느 쪽인지 회차별로는 미확인).
- 이관 준비 `cutover.prepare()`(`cutover.py:198`)는 B4 staging, B5 링크, 재처리는 실행하지만 **`import_rss_run`이나 `recover_rss`는 호출하지 않는다.** docstring에도 "No food is approved"라고 쓰여 있다.
- 따라서 유일한 REVIEW 항목 `호박`도 후보 큐에 없다. 실행했다면 `candidate_kind('UNKNOWN','REVIEW')='UNKNOWN'`이므로 `REQUIRES_TYPE_APPROVAL` 후보 1건이 생겼을 것이다(코드로 추론, 실행되지 않음).
- 재시도 경로 `ExecutionController.recover_rss()`(`execution_runtime.py:40`)는 구현돼 있지만 SQLite 실데이터에서 실행된 흔적은 없다.

### 3.5 B4 공통 전처리 — `ingestion.py`, `data_quality.py`, `ingestion_schema.py`

| 항목 | 내용 |
| --- | --- |
| 역할 | 기존 저장소의 행 → Source Envelope → 정책 검사 → staging → DQ. **HTTP 0, 원문 복사 0.** `payload_ref`(예: `items:89`, `instagram_media:…`)와 `payload_hash`만 저장 |
| 식별자 | `ingest_run_id = 'INGEST_'+sha256([source, run_ref])[:32]` (`run_ref='runs:N'`). `item_key = 'ITEM_'+sha256([source, external_key])[:32]`. `snapshot_id = 'SNAP_'+sha256([item_key, payload_hash, adapter])[:32]` |
| 2차 중복 | 한 배치 안의 같은 `external_key`는 `multiplicity`로 합치고 `duplicate.batch` WARN을 남김. 같은 내용 해시의 스냅숏은 `INSERT OR IGNORE`. 같은 회차를 다시 ingest해도 아무것도 바뀌지 않음 |
| 제외·보류 | DQ 결과가 FAIL이면 staging하지 않고 `rejected_items`로 셈. `source_absent`이거나 만료되면 staging하지 않고 `retention.active NOT_APPLICABLE`. NAVER 본문은 `PolicyBlocked`로 처리(지표는 메모리에만). 자격 증명처럼 보이는 메타데이터는 거부 |
| DQ 규칙 (`dq-rules-v1`) | `request.metadata`, `version.source_schema`, `version.api`, `coverage.scope`, `required/format.external_key`, `payload.json`, `encoding.text`, `time.published_at`, `type.counts`, `fields.states`, 지표용 `unit.defined`, `type.metric_value`, `time.metric_period`, `policy.metric_storage` |
| 실패 회차 | `coverage=NONE`, `execution_status=FAILED`로 기록. **0으로 측정된 것이 아니라 "관측 안 됨"이다** |

실데이터. 이관 시 `cutover.prepare`가 `ingest_pending` + `legacy_backfill(youtube, instagram_graph)`로 **한꺼번에 생성**했다. 모든 레거시 논리 회차의 `observed_at`이 2026-11-07T15:38:17Z다.

| 테이블 | 행 | 구성 |
| --- | ---: | --- |
| `ingest_runs` | 97 | RSS 95 (COMPLETE/SUCCEEDED 90, NONE/FAILED 5) + Instagram 레거시 1 (UNKNOWN) + YouTube 레거시 1 (UNKNOWN, **staged 0**) |
| `source_items` | 555 | RSS 370 + Instagram 185. YouTube 0 |
| `item_snapshots` | 840 | RSS 655 (payload가 회차마다 바뀌어 한 항목에 스냅숏이 최대 10개) + Instagram 185 |
| `run_items` | 1,085 | RSS 900 (PASS) + Instagram 185 (PASS 184, WARN 1) |
| `quality_results` | 10,402 | PASS 9,493 / WARN 8 / NOT_APPLICABLE 901 / **FAIL 0**. WARN 8의 내역: 실패 RSS 회차 coverage NONE 5, 레거시 coverage UNKNOWN 2, 캡션 없는 게시물 1 |
| `rejected_items` 합계 | 0 | |

YouTube가 0인 이유: `legacy_backfill('youtube')`는 출처 라벨이 없거나 `legacy_unknown`인 영상만 대상으로 한다. 실제 영상 431개는 모두 `youtube_video_provenance.origin='live_api'`이고, 이를 staging할 B3 `execution_runs`도 없어서 어느 경로로도 staging되지 않았다(이관 보고서 §6과 일치).

테스트: `tests/test_ingestion.py`(14개). 저장된 B4-B5 최종 테스트 로그(`reports/development-b4-b5-2026-10-28/final-tests/tests.log`)는 "Ran 280 tests … OK"다.

### 3.6 B5 연결 — `food_observations.py:243 link_run`

서로 다른 사실 세 가지를 따로 저장한다.

| 사실 | 테이블 | 규칙 |
| --- | --- | --- |
| 검색이 이 항목을 돌려줌 | `item_query_matches` | 항상 `verification_status='SEARCH_ONLY'`. **언급으로 세지 않음** |
| 승인 별칭이 근거에 등장 | `food_mentions` | `_extract`: ACTIVE 별칭 × (RSS: 키워드·뉴스 제목 / Instagram: 원래 캡션의 해시태그). `alias_pattern()`은 단어 경계 + 조사만 허용하는 **정확 일치**. 띄어쓰기 변형·유사도 매칭 없음. 상태 `EXACT_ALIAS_FIELD`/`EXACT_ALIAS_TEXT`/`EXACT_ALIAS_HASHTAG` |
| 허용된 숫자 관측 | `metric_observations` | RSS `rss.approx_traffic`(원문 텍스트), Instagram `instagram.observed_posts`(B3 작업의 음식별). YouTube 지표 저장은 정책상 차단, NAVER는 저장 안 함 |

출처별 정책(`mention_policy`): RSS·Instagram은 ALLOWED, YouTube는 POLICY_BLOCKED, NAVER는 NOT_APPLICABLE.

실데이터:

- `item_query_matches` 185: 전부 Instagram, `query_ref='instagram_hashtag:1'`(레거시 `#두쫀쿠` 해시태그 검색), `query_id` NULL, `SEARCH_ONLY`
- `food_mentions` **0**: 승인 별칭이 0개라 찾을 이름이 없음
- `metric_observations` 900: 전부 `rss.approx_traffic`, OBSERVED, **`food_id` NULL**(음식에 연결되지 않은 피드 항목의 트래픽). 값 분포: `200+` 277, `500+` 159, `1000+` 155, `100+` 147, `2000+` 86, `10000+` 31 …

테스트: `tests/test_food_observations.py`(10개), `tests/test_oracle_observations.py`(10개).

### 3.7 사람 승인 = FOOD_ID 생성 — `food_service.py:295 command`, `:467 _do_review`

- **승인이 필요한 명령**: `review`, `rename`, `alias`, `revert-alias`, `relate`, `merge`. `approved is True`, `actor_ref`, `reason`, `expected_revision`(낙관적 잠금)이 없으면 거부한다(`command`, 317~320행)
- **승인 없이 가능한 명령**: `submit`(사용자 후보 등록), `import`(RSS 후보), `resolve`(정확 별칭 재확인). 셋 다 FOOD_ID를 만들지 않는다
- **FOOD_ID가 생기는 시점**: `review` 명령의 `action='create'` 또는 `'create_variant'`가 실행될 때뿐이다. 이때 `identity('FOOD')` = `'FOOD_' + uuid4().hex`가 만들어진다(482행). 이름에서 결정적으로 만들어지는 ID가 아니며, 이름을 바꿔도(`rename`) ID는 그대로다
- **생성과 동시에 승인되는 별칭**: `canonical_name`, 후보의 `surface_form`, 후보의 `proposed_aliases`, 검토자가 덧붙인 `aliases`(487~489행). 사용자가 후보를 올릴 때 별칭을 제안하는 것만으로는 승인이 아니며, 검토자가 `create`를 실행해야 승인된다
- **`link`**: 기존 음식에 연결하는 동작이다. 별칭은 `approve_aliases=True`를 줄 때만 승인한다(테스트 `test_human_link_does_not_implicitly_approve_proposed_aliases`)
- **브랜드·매장 gate**: `BLOCKED_NON_FOOD` 후보는 `review`에서 409 "브랜드·매장·비음식은 음식으로 승격할 수 없습니다. 특정 상품을 별도 등록하세요"
- **이력**: `food_events`는 UPDATE/DELETE 트리거로 막혀 있다(불변). 요청별 결과는 `food_commands`에 저장되어 같은 요청을 다시 보내면 같은 결과가 돌아온다
- **병합**: `merge`는 별칭을 대상 음식으로 옮기고 원래 음식을 `status='MERGED'`, `merged_into=…`로 남긴다(ID를 지우지 않고 리다이렉트). 관계가 있는 음식은 병합하지 않는다

실데이터(SQLite): 0. 운영 Oracle에서는 보고서 기준으로 2026-11-08 `두쫀쿠` 1건(`FOOD_3007c29a43654e0fb9571c2a2c8c1c86`, "별칭 없음")을 사용자가 승인했다(`LIVE_VERIFICATION.md`, `APP_HANDOFF.md`). 직접 확인은 **미확인**(다른 감사 범위).

### 3.8 추적 → 계획 → 예산 → 실행 (B1/B2/B3)

| 단계 | 핵심 규칙 | 근거 |
| --- | --- | --- |
| 추적 등록 | 승인된 ACTIVE 음식이어야 함. 해당 범위에 **승인 정확 별칭이 있어야 함**. 후보 ID를 주면 그 후보가 RESOLVED이고 같은 FOOD_ID여야 함. 등록 결과는 PENDING | `tracking_service.py:202 _register` |
| 활성화·재개 | `approved is True`가 필요 | `tracking_service.py:252` |
| 계획 | 승인 별칭 → Instagram: casefold 해시태그(공백·숫자만인 태그는 BLOCKED `HASHTAG_REQUIRES_EXACT_APPROVAL_WITHOUT_SPACES`). YouTube: `q=별칭`(`\|`·`-` 연산자는 차단). NAVER: 음식당 별칭 5개 이하로 `keywordGroups`(`groupName=food_id`) | `query_planner.py:158-183` |
| 예산 | preview는 쓰지 않음. 명시적으로 예약할 때만 기록 | `quota.py` docstring |
| 실행 | 슬롯별 job + outbox. `claim`은 lease를 잡고 `fencing_generation`을 1 올림. `_fence`가 owner·token·만료를 검사하므로 늦게 도착한 commit은 거부됨 | `execution.py:155, 164-197` |
| 실행 후 | `IngestionService.execution_run` → `link_run`(Instagram은 음식별 `observed_posts` 지표) | `ingestion.py:256` |

실데이터(SQLite): `tracking_*`, `planner_*`, `quota_*`, `execution_*`가 전부 0. 테스트는 `test_tracking.py` 18, `test_query_planner.py` 40, `test_execution.py` 20, `test_execution_sources.py` 26개다. 모두 Mock 전송을 쓴다(`execution_mock.py`: "Never opens a socket").

참고로 B 단계 이전의 레거시 수집기(A 단계)는 FOOD_ID 없이 실제로 돌았다. 지금의 엔티티 파이프라인과는 별개다. Instagram `#두쫀쿠` 해시태그 1개·게시물 185·회차 16, YouTube 발굴 검색어 6개(`신상 디저트` 등)·영상 431(`live_api`), NAVER 호출 10. NAVER 쪽 `naver_service.py:15 ALIASES = {'두쫀쿠': ['두쫀쿠','두바이 쫀득 쿠키','두바이쫀득쿠키']}`는 하드코딩된 상수이고 엔티티 별칭 테이블과 무관하다. A4-A5 보고서는 이 상수를 "자동 신뢰·승격하지 않는다"고 적었다.

### 3.9 별칭 변경 시 기존 데이터 재처리 — `food_observations.py:300 apply_entity_changes`

두 층에서 처리한다.

1. **후보 층(즉시, 같은 트랜잭션)**: 별칭을 `revoke`·`reassign`·`revert`하면 `_change_alias → _invalidate`가 그 별칭으로 자동 연결됐던 `RESOLVED` 후보를 **`NEEDS_REVIEW`로 되돌린다**(`food_service.py:515-528`). 다시 확정하려면 `resolve` 명령이 필요하다(테스트 `test_alias_correction_invalidates_automatic_links_and_can_be_reverted`).
2. **관측 층(지연, 별도 명시 단계)**: `food_events` 가운데 `alias/food`의 `APPROVED/CHANGED/REVERTED/MERGED` 중 `entity_change_effects`에 아직 반영되지 않은 이벤트를 하나씩 처리한다.
   - 새로 승인된 별칭이면 **언급 가능한 모든 AVAILABLE 항목(RSS·Instagram)을 다시 스캔**해 새 언급을 만든다
   - 취소·이동·병합이면 기존 언급을 `INVALIDATED`로 바꾸고(`invalidated_by=event_id`) 새 매핑에 맞는 언급을 만든다
   - 영향받은 음식마다 `aggregate_invalidations`를 남기고, 이벤트마다 `entity_change_effects` 1행을 남긴다(이벤트당 한 번만 처리)
   - 캡션 보존 기간(30일)이 지난 근거는 **다시 만들지 않는다**. 매핑이 더는 유효하지 않은 언급만 철회한다(`not_reprocessable_items`로 셈)
   - 원문 값(provider value)은 수정하지 않는다
   - **자동으로 실행되지 않는다.** 호출 지점은 `__main__ --maintenance`(438행), `runtime_storage.oracle_maintenance`(118행), `cutover.prepare`(220행), `scripts/ops/live_pilot.py`(205행)다. 처리되지 않은 변경이 있으면 B8 응답의 `freshness.reprocessing_pending=true`로 드러난다(`analytics_service.py:352`)

실데이터(SQLite): 이관 시 한 번 호출됐지만 대상 이벤트가 0이라 `entity_change_effects` 0, `aggregate_invalidations` 0이다. 보고서 기준 운영 Oracle에서는 `두쫀쿠` 승인 뒤 `live_pilot.py`가 `oracle_maintenance`를 불러, 이관된 Instagram 게시물을 포함해 활성 언급 143건이 생겼다(**미확인**). 테스트: `test_alias_approval_and_revocation_reprocess_mentions`(승인 → 언급 2 생성, 취소 → 2 무효화, 재호출 → 처리할 것 없음), `test_merge_and_split_move_mentions_and_list_affected_foods`, `test_expired_caption_evidence_is_not_regenerated`.

### 3.10 B8 분석 — `analytics_reader.py`, `analytics_service.py`

- 조회 시점에 집계하며 쓰기는 없다. SQLite는 `query_only`, Oracle은 읽기 전용 트랜잭션이다. SQL 문장 하나를 두 DBMS가 함께 쓴다
- 세는 단위: ACTIVE이고 만료되지 않은 `food_mentions`가 있는 **고유 항목**. `item_query_matches`(검색 근거)는 세지 않는다. 별칭·검색어·회차가 여러 개여도 음식·출처·구간마다 1로 센다
- 관측 범위 밖은 `NOT_OBSERVED`(null)이고, 성공했지만 비어 있는 회차는 0이다. 근거가 만료되면 `EVIDENCE_EXPIRED`
- 파생 음식은 상세 화면의 `relations`에 `HAS_VARIANT`로 보여 주기만 하고 합산하지 않는다
- 출처 간 합산과 종합 점수는 **없다**. `COMPOSITE_SCORE_PROPOSAL.md`가 "상태: 미구현"이라고 명시하고, `tests/test_app_contract.py`가 그런 필드가 없는지 검사한다
- 테스트: `tests/test_food_analytics.py` 17개. 앱 개발용 데이터는 `app_mock.py`의 `[MOCK]` 합성 데이터셋(`DUJJONKU`, `JJAPJJONKU` 등)이다

---

## 4. 정규화와 음식 판정 — 질문별 답

### 4.1 음식 후보는 어떻게 발견되는가

경로는 두 가지뿐이다.

1. **RSS 자동 발굴**: 규칙 분류가 FOOD 또는 REVIEW인 항목만 `import_rss_run`으로 후보가 된다. 실데이터에서는 해당 항목이 1개(`호박`)였고 후보로 들어간 것은 0개다.
2. **사용자 수동 등록**: `submit`(`source='user_input'`, `origin='user'`). 설계 문서(`TRACKING_DESIGN.md` §시나리오 1)는 처음부터 "RSS에 FOOD가 없다 → 사용자가 두쫀쿠를 등록"을 기본 경로로 잡았다.

Instagram·YouTube 내용에서 새 음식명을 뽑아내는 기능은 **정책상 꺼져 있다**(`_do_import`는 RSS 외 출처를 403으로 거부, YouTube mention은 POLICY_BLOCKED).

### 4.2 "음식인가"는 어디서 판정하는가 (3중 판정)

| 층 | 판정 주체 | 결과 |
| --- | --- | --- |
| 1. 규칙 분류 | `classifier.classify` (자동, 확률 아님) | FOOD / REVIEW / NON_FOOD |
| 2. 유형 gate | `entity_types.candidate_kind` (자동) | FOOD 유형 / `REQUIRES_TYPE_APPROVAL` / `BLOCKED_NON_FOOD` (DB CHECK로 RESOLVED 불가) |
| 3. 사람 승인 | 검토자 `review(approved=True)` | `entity_kind ∈ {DISH, PRODUCT, INGREDIENT, FOOD_CONCEPT}`를 지정해야 FOOD_ID가 생김 |

### 4.3 정규화 규칙 (`food-nfc-space-v1`)

- `classifier.normalize`: Unicode NFC 변환, 앞뒤 공백 제거, 연속 공백을 하나로 줄이는 것까지만 한다
- **하지 않는 것**: 내부 공백 제거, 대소문자 통일(casefold), 유사도 계산, 임베딩, 공동 출현 기반 연결. 테스트 `test_nfc_whitespace_normalization_does_not_remove_spaces_or_case`는 `'  Cafe   Cookie  '`는 연결되고 `'CafeCookie'`와 `'cafe cookie'`는 연결되지 않음을 확인한다
- 예외: Instagram 해시태그 비교에만 `normalize_tag = normalize(...).casefold()`를 쓴다(`instagram_analysis.py:15`)

### 4.4 동음이의어

- 별칭 유일성은 `(normalized_form, scope, normalization_version)` 단위다(ACTIVE 별칭에 부분 UNIQUE 인덱스). 대표명(`canonical_name`)은 중복돼도 된다
- 같은 표기가 이미 다른 음식에 승인돼 있으면 409 "모호한 명칭은 별도 scope로 검토하세요"
- 테스트 `test_homonyms_need_explicit_scopes_canonical_names_can_duplicate`: `밤`을 `food:chestnut`과 `food:dessert`에 각각 등록하면 FOOD_ID가 2개가 된다. 기본 scope(`food:ko`)로 `밤`을 찾으면 **연결되지 않는다**
- 언급 추출에서도 `alias_pattern('쿠키')`는 `쿠키런`에 매치되지 않는다. `두쫀쿠런`도 `두쫀쿠`가 아니다(`test_multiple_foods_and_variant_food_are_separate`, `test_unapproved_names_and_variants_are_not_counted_for_the_food`)

### 4.5 `두쫀쿠 ↔ 두바이 쫀득 쿠키`는 어떻게 연결되는가

**메커니즘**: 같은 FOOD_ID 아래 **사람이 승인한 정확 별칭 여러 개**를 둔다. 구체적인 흐름은 다음과 같다.

1. 사용자가 `submit(name='두쫀쿠', aliases=['두바이 쫀득 쿠키','두바이쫀득쿠키'])`를 보내면 후보가 `NEEDS_REVIEW`가 된다(이 시점에는 아무 이름도 연결되지 않음)
2. 검토자가 `review(action='create', canonical_name='두바이 쫀득 쿠키', approved=True, actor_ref, reason)`을 실행하면 `FOOD_<uuid>` 1개가 생기고 별칭 3개가 승인된다
3. 이후 `resolve_name('두쫀쿠' | '두바이 쫀득 쿠키' | '두바이쫀득쿠키')`는 모두 같은 FOOD_ID를 돌려준다
4. B5에서는 RSS 키워드·뉴스 제목이나 Instagram 해시태그에 세 표기 중 하나가 있으면 같은 FOOD_ID의 언급이 된다. 두 별칭이 한 항목에 같이 나와도 확인 항목은 1로 센다

`두바이 쫀득 쿠키`(띄어쓴 형태)와 `두바이쫀득쿠키`(붙인 형태)는 **정규화로 같아지지 않으므로 둘 다 따로 승인해야 한다.** `두쫀쿠`만 승인된 상태에서는 `두바이쫀득쿠키`, `두 쫀 쿠`, `두쫀쿠 신상`이 모두 `NEEDS_REVIEW`로 남는다(`test_proposed_alias_fuzzy_spacing_and_variant_are_not_automatic`).

| 근거 | 위치 | 실행 범위 |
| --- | --- | --- |
| 설계 | `reports/integrated-audit-2026-10-26/TRACKING_DESIGN.md` 27행 "`두쫀쿠/두바이 쫀득 쿠키`는 승인된 정확 별칭의 예시" | 문서 |
| 구현 | `food_service.py:467-504`, `food_observations.py:31 alias_pattern`, `:196 _extract` | 코드 |
| 테스트 | `test_food_entities.py:80` (3개 이름 → 같은 ID), `test_food_observations.py:60` (두 별칭 → 1개 음식, 항목 2·언급 4), `tests/planner_scenario.py:24`, `tests/b45_scenario.py`, `tests/food_browser_check.py:167-177` | 격리 DB·Mock |
| Mock 데이터 | `app_mock.py:37` `('DUJJONKU','두쫀쿠',('두쫀쿠','두바이쫀득쿠키','두바이 쫀득 쿠키'),…)` | 합성 데이터 |
| 실데이터 SQLite | 별칭 0, 피드에 해당 이름 0건 | **실행 안 됨** |
| 운영 Oracle | 보고서상 `두쫀쿠` 1개를 "별칭 없음"으로 승인 | **미확인**. 보고서대로라면 현재 운영에서 `두바이 쫀득 쿠키`는 연결돼 있지 않음 |

### 4.6 `짭쫀쿠`처럼 비슷한 음식은 어떻게 처리되는가

- **자동 연결하지 않는다.** `짭쫀쿠`는 `두쫀쿠`와 글자가 비슷해도 `NEEDS_REVIEW`로 남는다(`test_food_entities.py:101`)
- 검토자는 세 가지 중 하나를 고른다
  - (a) `create`: 무관한 별도 음식
  - (b) `create_variant` + `parent_food_id`: **별도 FOOD_ID** + `VARIANT_OF` 관계(`food_relations`)
  - (c) `link` + `approve_aliases`: 같은 음식의 별칭으로 확정
- 설계 문서가 권하는 방식은 "별도 후보로 검토"(`TRACKING_DESIGN.md` 27·89행)다
- 관계에 대한 안전장치: 자기 참조 금지, 순환 금지(SQL 트리거 2개와 재귀 CTE `_variant_cycle`), 같은 관계 중복 금지, `RELATED_TO`는 `from<to` 순서로 정규화
- 집계: 파생 음식은 **자기 FOOD_ID로만** 센다. 테스트 `test_unapproved_names_and_variants_are_not_counted_for_the_food`에서 RSS에 `짭쫀쿠`가 7일간 나오면 FOOD_V(짭쫀쿠)는 매일 1, FOOD_A(두쫀쿠)는 매일 0이다. 상세 화면에는 `HAS_VARIANT` 관계로만 보인다
- 테스트: `test_food_entities.py:233` (variant의 ID가 다르고 순환이 차단됨), `test_food_observations.py:93`, `test_food_analytics.py:107`, `tests/app_api_support.py:33`, `app_mock.py:44` (`JJAPJJONKU`, 합성)
- 실데이터: **없음**(SQLite 0건, 피드에 `짭쫀쿠` 0건, Oracle도 보고서상 승인 음식은 `두쫀쿠` 1개뿐)

### 4.7 사람 승인이 반드시 필요한 지점

| 지점 | 강제 위치 |
| --- | --- |
| FOOD_ID 생성 (`create`/`create_variant`) | `food_service.py:317` `approved is True` + `actor_ref` + `reason` + `expected_revision` |
| 별칭 승인·취소·복구·이동·되돌리기 | 같음 (`alias`, `revert-alias`) |
| 기존 음식 연결 (`link`), 이름 변경, 관계, 병합 | 같음 |
| UNKNOWN 유형 후보 | 검토자가 `entity_kind`를 명시해야 함 (`:479`) |
| 추적 활성화·재개 | `tracking_service.py:252` |
| 운영 수집원 사용 | `data/ops-prod/registry.json` 승인 (보고서 기준, Git 제외 파일. 내용 미열람) |

자동으로 일어나는 것은 **이미 승인된 별칭과 정확히 같은 후보의 RESOLVED 처리**와 **승인 별칭 기반 언급 추출**뿐이다.

---

## 5. "구현 ≠ 실데이터 실행"의 정량화

SQLite 이관 사본에서 단계별로 실제 행이 있는지 센 결과다(엔티티 이후 테이블 기준).

| 구분 | 테이블 수 | 행 있음 | 행 0 |
| --- | ---: | ---: | ---: |
| 음식 정체성 (A4-A5: `food_commands/entities/aliases/relations/candidates/events`) | 6 | 0 | 6 |
| 추적 (B1: `tracking_commands/targets/events/slots`) | 4 | 0 | 4 |
| 계획·예산 (B2: `planner_*` 5, `quota_*` 3) | 8 | 0 | 8 |
| 실행 (B3: `execution_*` 11) | 11 | 0 | 11 |
| 공통 전처리 (B4: `ingest_runs`, `source_items`, `item_snapshots`, `run_items`, `quality_results`) | 5 | 5 | 0 |
| 관측 (B5: `item_query_matches`, `food_mentions`, `metric_observations`, `entity_change_effects`, `aggregate_invalidations`) | 5 | 2 | 3 |

- 실데이터가 들어간 단계는 **수집·저장·분류·B4·B5 일부**다. 이 가운데 B4·B5는 실시간 수집 흐름이 아니라 **이관 스크립트가 과거 행을 한 번에 처리**해서 생긴 것이다(`cutover.prepare`, HTTP 0).
- "음식"이 등장하는 모든 단계(후보, FOOD_ID, 별칭, 언급, 추적, 계획, 실행)는 SQLite 실데이터에서 **한 번도 실행되지 않았다.**
- 테스트 규모: 이 감사에서 테스트를 다시 실행하지 않았다. 저장된 로그는 B4-B5 최종 "Ran 280 tests … OK", 운영 전환 기본 "Ran 534 tests … OK (skipped=189)", Oracle 전체 "Ran 723 tests … OK"다(`reports/development-oracle-production-cutover-2026-11-07/{default,oracle-full}-tests/tests.log`).
- 운영 Oracle에서 음식 단계가 처음 실행된 것은 보고서상 2026-11-08 소량 검증 1회다(실제 요청 7회: RSS 새 항목 10·**음식 후보 0**, Instagram 새 게시물 40, YouTube 50, NAVER 값 저장 0, `두쫀쿠` 승인, 언급 143). 지속 수집은 `CONFIGURED_NOT_ENABLED`다. 모두 **미확인**(Oracle 감사자 확인 필요).
- **교차 확인(2026-11-13, 통합 정리 시 추가):** [DATABASE_EVOLUTION_AUDIT.md §6](DATABASE_EVOLUTION_AUDIT.md)에서 운영 `MZ_APP`을 읽기 전용 SELECT로 직접 조회했고, 위 수치가 실제와 같았다. `food_entities` 1(두쫀쿠), `food_aliases` 1, `food_candidates` 2(RESOLVED 1·REJECTED 1), `food_mentions` 143, `tracking_targets` 3, `execution_jobs` 3, `execution_calls` 6이다. 최신 기록 시각은 2026-11-07 23:26Z이고, 그 뒤로 새로 쓰인 데이터는 없다. 그러므로 음식 단계(4·6 언급·7·8·10)는 **운영 Oracle에서 소량 검증 1회로 실행됐다**. SQLite 실데이터에서는 한 번도 실행되지 않았다.

---

## 6. 교수 설명용 정리 (사실만)

- 설계한 파이프라인은 코드로 끝까지 구현됐고 테스트(격리 DB·Mock)로 검증됐다. 범위는 수집부터 분석까지이며, 승인 게이트, 정확 별칭, 파생 관계, 재처리, 정책 차단이 포함된다.
- 실제 수집 데이터(2026-10-13~10-28, RSS 370항목)에서는 규칙 분류기가 음식을 하나도 찾지 못했다(FOOD 0, REVIEW 1). 그 결과 음식 개체가 하나도 생기지 않았고, 음식 중심의 하류 단계는 실데이터로 돌지 않았다.
- 이것은 의도된 동작이다. 코드는 근거 없이 후보나 FOOD_ID를 만들지 않고, `0`과 `관측 안 됨`을 구분한다. 설계도 처음부터 "RSS에 FOOD가 없으면 사용자가 등록하고 사람이 승인한다"를 기본 경로로 잡았다.
- 남은 공백은 두 가지다.
  - 유일한 REVIEW 항목(`호박`)이 후보 큐에 올라가지 않았다. `import_rss_run`/`recover_rss`가 실행되지 않았기 때문이다.
  - 운영 `두쫀쿠`에는 보고서상 `두바이 쫀득 쿠키` 별칭이 승인돼 있지 않다.
