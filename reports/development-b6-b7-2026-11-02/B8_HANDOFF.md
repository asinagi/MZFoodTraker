# B8 집계·화면 인수 계약 (B6~B7 기준)

B4~B5의 [B6~B8 인수 계약](../development-b4-b5-2026-10-28/B6_B8_HANDOFF.md)을 유지하고 B6~B7에서 달라진 점만 적는다.

## 읽기 경로

- B8 집계는 `TargetRepository.food_view`와 같은 이식 SQL(이름 바인드, `LIMIT`·SQLite 함수 미사용)로 작성해 SQLite와 Oracle 양쪽에서 같은 결과를 내야 한다. B7 대조는 이 조회 결과를 음식마다 비교한다.
- 음식별 고유 콘텐츠는 `food_mentions(status='ACTIVE')`의 `count(DISTINCT item_key)`다. `item_query_matches`는 검색 근거이며 언급 수로 쓰지 않는다.
- 시각 비교·일 단위 집계는 UTC 순간값을 기준으로 하고 KST 일자는 애플리케이션에서 계산한다. Oracle은 TSTZ, 대역은 UTC ISO 문자열이다.

## 쓰기·무효화

- 재집계 대상은 `aggregate_invalidations.consumed_at IS NULL`이다. Shadow 전달 후에는 Oracle에도 같은 행이 있으므로 집계는 한 저장소만 소비해야 한다(전환 전에는 SQLite).
- 집계 결과 테이블을 추가하면 `target_schema.py` 명세, 대상 스키마 버전, Shadow 캡처 목록(`shadow_schema.CAPTURED`)에 함께 넣어야 한다. 빠지면 B7 대조가 그 테이블을 검사하지 않는다.

## 정책

- NAVER 지수와 YouTube 통계는 어느 저장소에도 저장되지 않는다(애플리케이션 게이트와 DB CHECK). B8 화면의 NAVER 값은 요청 시 메모리 값만 쓴다.
- 만료·삭제 전파는 상태 전이(EXPIRED/DELETED/INVALIDATED)와 Shadow DELETE로 이뤄진다. 집계는 비활성 행을 제외하고, 원문이 만료된 항목을 재생성하지 않는다.
- 이관 데이터의 `first_origin='legacy_unknown'`, `coverage='UNKNOWN'`은 집계 화면에서 별도 표시한다. 실제 API 회차로 합산하지 않는다.
