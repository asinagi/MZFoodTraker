# B6~B8 인수 계약

B4~B5가 제공하는 것과 다음 단계가 지켜야 할 조건이다. 스키마 상세는 [SCHEMA.md](SCHEMA.md).

## B6 Oracle Repository

- 대상 논리 모델: INGEST_RUN, SOURCE_ITEM, ITEM_SNAPSHOT, RUN_ITEM, QUALITY_RESULT, ITEM_QUERY_MATCH, FOOD_MENTION, METRIC_OBSERVATION, ENTITY_CHANGE_EVENT 효과, 집계 무효화. 기존 FOOD_ENTITY/FOOD_ALIAS(A4)를 그대로 참조한다.
- 결정적 키(`ITEM_`, `SNAP_`, `INGEST_`, `METRIC_`, mention_key, event_key)는 재시도 멱등성의 근거다. Oracle에서 identity PK를 쓰면 이 키들을 UNIQUE로 유지하고 LEGACY_KEY_MAP으로 대응한다.
- METRIC_OBSERVATION의 출처 CHECK(RSS·Instagram만)와 값 상태 CHECK를 그대로 옮긴다. 이 제약이 NAVER·YouTube 저장 금지의 마지막 방어선이다.
- 원문 CLOB 테이블을 새로 만들지 않는다. `payload_ref`는 출처 저장소를 가리키며 출처 TTL이 끝나면 대상도 사라진다.
- MERGE 경쟁(ORA-00001) 재조회, TSTZ 왕복, 빈 문자열=NULL(field_states로 구분)을 실제 Oracle 버전에서 통합 테스트한다.

## B7 단계 이관

- legacy 출처 불명 데이터는 `legacy:<source>:origin-unknown` 논리 회차와 `coverage=UNKNOWN`, `first_origin=legacy_unknown`으로만 적재한다. 실제 API 회차로 승격하지 않는다.
- 이관은 출처 만료 기한을 새로 계산하지 않는다. `source_items.expires_at`은 출처 저장소 값만 반영한다.
- 보존 스냅샷 복제본 결과([copy-b45-processing.json](copy-b45-processing.json))가 예상 건수의 기준이다: RSS 회차 87(성공 82·실패 5), 콘텐츠 711, 검색 근거 400, 품질 FAIL 0.
- Shadow 검증은 B3 Outbox 재전달과 `--ingest-pending`으로 하며, 외부 API 재호출로 차이를 메우지 않는다.

## B8 집계·화면

- 음식별 콘텐츠 수는 `food_mentions(status='ACTIVE')`의 `count(DISTINCT item_key)`다. 검색 근거(`item_query_matches`) 수는 별도 지표이며 언급 수로 쓰지 않는다.
- 지표는 출처·단위·`context_hash`가 같을 때만 합칠 수 있다. 출처가 다른 지표를 합산하지 않는다. NAVER는 저장 지표가 없으며 화면 요청 시 메모리에서만 표시한다.
- `value_state='NOT_OBSERVED'`(수집 실패)는 NULL로 표시하고 0으로 바꾸지 않는다. `OBSERVED`의 0은 정상 관측 0이다. `coverage`가 PARTIAL/LIMITED/UNKNOWN이면 화면에 범위를 표시한다.
- `aggregate_invalidations`에서 `consumed_at IS NULL`인 음식만 재집계하고 처리 후 `consumed_at`을 채운다. 병합된 음식은 `food_view`처럼 `merged_into`로 리다이렉트한다.
- 원문이 만료된 항목은 재처리 불가(`not_reprocessable_items`)로 남는다. 과거 응답을 만들어 내지 않는다.

## 남은 정책 결정 (사용자 결정 필요)

- YouTube 영상 제목·설명에서 음식 언급을 추출하는 것은 정책상 비활성이다(`derived_operations` 비어 있음). 허용하려면 공식 근거와 새 정책 버전이 필요하다.
- NAVER 지수 저장은 금지 상태다. 허용 근거가 확인될 때까지 METRIC_OBSERVATION 행을 만들지 않는다.
- Instagram 관측 게시물 지표는 "이 검색 요청이 반환한 고유 게시물"이다. 음식 단위 합계는 B8에서 별칭 질의 간 item union으로 계산해야 한다.
