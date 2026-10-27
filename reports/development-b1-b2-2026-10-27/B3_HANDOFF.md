# B3 실행 연결 인수 계약

B1/B2의 `execution_enabled=false`는 실행기를 아직 연결하지 않았다는 뜻이다. READY_LOCAL/READY_FOR_B3_RECHECK는 수집 성공도 실제 원격 할당량 보장도 아니다. 정책 미확인 출처의 권한을 사용자 버튼으로 켜지 않는다.

## 실행 전 입력

- 불변 plan_id/query_id/spec_hash/planner_version과 contract/adapter/policy/cost 버전.
- account_scope를 실제 자격 증명에 연결한 서버 registry. 비밀값은 plan/계약/이력에 넣지 않는다.
- target_id, generation, food_revision, 정확 due_at, alias provenance와 FOOD_ID 매핑.
- 원격 상태 확인 결과와 실제 공급자 quota/권한. 현재 public API에는 이를 임의 갱신하는 경로가 없다.

`TrackingService.due()`는 읽기 전용 후보 목록이다. schedule 명령은 한 슬롯을 확정하고 next_due를 전진시키는 로컬 메타데이터다. HTTP job/lease/outbox는 아니다. 계획에 기록된 due_at의 슬롯이 이미 tracking_slots로 확정된 경우에도 B2 검증은 원래 매핑을 확인할 수 있다.

## B3가 추가해야 할 순서

1. 새 공통 executor를 사용할 출처에서 기존 자동 writer와 소유권·예산 원장을 조정한다. 기존 수집기를 동시에 그대로 켜고 같은 query를 실행하지 않는다.
2. 논리 query/due_slot에 대해 원자적 outbox·job·lease/fencing을 확보한다. 현재 quota reservation의 고유성은 HTTP exactly-once나 fencing을 보장하지 않는다.
3. `QueryPlanner.plan(plan_id).current_validation`을 점검하고 실제 실행 시작 직전 같은 DB 트랜잭션에서 generation/food_revision/계정 권한을 재확인한다. future target의 due_at을 넘겨 추정하지 않는다.
4. `planner.quota.reserve(plan_id, attempt_key, attempt_no, ttl_seconds)`로 로컬 예산을 원자 예약한다. 같은 key는 같은 예약을 반환하고 기한을 연장하지 않는다. 기존 유효 예약이 있으면 새 예약을 만들지 않는다. 공유 query+due_slot+attempt 번호의 유일성을 지킨다.
5. runtime에서 IG hashtag ID와 next page token, YT 공개 video IDs·videos batch·next token을 materialize한다. 계획에 없는 임의 검색어나 승인되지 않은 별칭을 추가하지 않는다. 각 HTTP/페이지/재시도를 실제 API_CALL에 기록하고, 추정 워크플로 상한을 실제 HTTP 횟수라고 표시하지 않는다.
6. 실행 소유권을 검증한 후 `mark_attempted(reservation_id)`에 해당하는 원장 상태와 실제 시도 기록을 연결한다. B2 구현은 **시도 의도만 기록하고 HTTP는 보내지 않는다**. replay로 ATTEMPTED가 반환된다고 새 HTTP를 허가하면 안 된다. lease/job의 실행 횟수 소유권이 따로 필요하다.
7. 응답이 늦게 오면 `valid_result_targets(reservation_id)`의 교집합만 음식 집계에 전달한다. PAUSED/STOPPED, 달라진 generation/food_revision, 당시 not-due였던 target은 제외한다. 공유 요청의 다른 유효 음식 매핑은 유지한다. 이 반환값은 FOOD_MENTION 판정이 아니다.
8. 성공·부분·실패·0건·원격 차감 불명 상태를 구분해 기록하고 last_success_at/next_due 및 재시도 상태를 명시적으로 확정한다. B2는 수집 성공을 기록하지 않는다. 성공 후 같은 slot을 다시 실행하지 않도록 outbox 상태를 확정한다.

원격 요청과 DB 트랜잭션은 원자적이지 않다. crash/timeout 후 원격 중복 실행 여부를 알 수 없으면 불명으로 남겨야 한다. 로컬 예약만으로 이 문제를 해결했다고 표시하지 않는다.

## 예산 원장

현재 quota usage는 기존 local calls + 새 워크플로 비용 상한이다. provider_observed_usage/provider_remaining_budget는 null이며 실제 원격 차감 확인은 false다. B3가 legacy calls와 새 ATTEMPTED를 둘 다 기록하면 보수적으로 이중 계산될 수 있으므로 API_CALL↔reservation 연결 또는 writer 전환을 통해 한 시도의 중복 회계를 제거해야 한다. 같은 API project/account를 다른 scope로 등록해 한도를 쪼개지 않는다.

YT search bucket과 videos/기타 unit bucket은 다르다. IG 고유 태그 창은 최초 사용 시각부터 7일이며 반복 사용으로 연장하지 않는다. lookup/media page의 동적 공급자 한도는 별도다. NAVER는 KST 일 예산과 전체 그룹·기간 context를 보존한다. 만료/해제된 미시도 예약만 사용 가능 예산으로 되돌리고 ATTEMPTED는 원격 차감 불명이라도 자동 환불하지 않는다.

출처별 의미:

- IG: 승인된 태그만, page cap이면 PARTIAL_LIMITED. missing/empty/purged 캡션과 실패/정상 0건은 B4에서 구분한다.
- YT: 영상 검색 일치만 보존. 현재 음식 내용 추출·파생 지표 gate는 계속 off. 검색 결과에서 실제 음식 언급을 추정하지 않는다.
- NAVER: 응답 시계열·ratio의 저장/캐시 금지. 요청 파라미터와 group mapping/context hash만 남긴다. 서로 다른 context를 비교 가능한 같은 비율 지표로 합치지 않는다.
- RSS: KR feed 한 번을 공유. 현재 post-collection 로컬 identity bridge의 pending_retry를 idempotent 재처리한다. NEW FOOD 자동 생성이나 추적 자동 승인은 하지 않는다.

## 운영 적용과 복구

운영 DB는 v0이며 이번에 손대지 않았다. B3 전에 writer 목록·활동을 확인하고 별도 승인된 유지보수 창에서 최신 online backup을 만든다. 원본 위에서 시험하지 않고 복제본 0→5 검증을 재수행한다. 코드/DB 버전을 함께 전환하고 한 출처씩 소량 실행·계측한다.

실패 시 실행 소유권을 차단하고 최신 DB를 다시 보존한 뒤 이전 코드와 일관된 별도 복원 DB 경로로 전환한다. 오래된 사본을 실행 중 원본 위에 덮어쓰지 않는다. 신규 승인/수집 데이터는 별도로 대조·복구해야 하며 원래 expires_at/캡션 보존일을 연장하지 않는다. 만료 데이터 정리 후 노출한다. Oracle 운영 이관과 B4~B8는 별도 단계다.
