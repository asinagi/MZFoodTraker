# 수동 음식 등록과 승인 예

격리 데모 `py -3.12 -B -m food_trends.food_demo --port 8010`에서 사용한다. 이름·scope가 같은 재등록은 같은 후보를 반환한다. 아래 UUID 형태의 키는 클라이언트가 작업마다 생성하고 재시도 시 재사용한다. 실제 검토자와 근거를 넣어야 한다.

## 1. 등록

`POST /api/foods/submit`

```json
{
  "request_key": "manual-cookie-registration-001",
  "name": "두쫀쿠",
  "aliases": ["두바이 쫀득 쿠키", "두바이쫀득쿠키"],
  "entity_kind": "PRODUCT",
  "scope": "food:ko",
  "description": "검토할 음식 후보"
}
```

아직 별칭 승인이 없으면 NEEDS_REVIEW다. 응답의 candidate_id와 revision을 사용한다. RSS 데이터는 생성·변경하지 않는다.

## 2. 사람 승인

`POST /api/foods/review`

```json
{
  "request_key": "manual-cookie-approval-001",
  "candidate_id": "CAND_<등록 응답의 ID>",
  "expected_revision": 2,
  "action": "create",
  "canonical_name": "두바이 쫀득 쿠키",
  "entity_kind": "PRODUCT",
  "approved": true,
  "actor_ref": "검토자 식별값",
  "reason": "음식 상품과 세 명칭의 동일성을 확인한 근거"
}
```

expected_revision은 예시의 2를 고정하지 않고 직전 GET 결과를 사용한다. 신규 생성 승인은 후보명·대표명·제안 별칭과 제안 관계를 함께 승인한다. 승인 응답의 resolved_food_id가 영속 공통 FOOD_ID다. 같은 승인 request key/payload 재시도는 같은 결과를 반환한다.

## 3. 정확 별칭 조회와 전달

```text
GET /api/foods/resolve?name=<URL 인코딩한 두쫀쿠>&scope=food:ko
GET /api/foods/resolve?name=<URL 인코딩한 두바이 쫀득 쿠키>&scope=food:ko
GET /api/foods/{food_id}
```

두 별칭 모두 같은 food_id를 반환한다. 대표 descriptor 형식은 다음과 같다. 실제 응답에는 revision·시각·취소 별칭·관계 등도 포함한다.

```json
{
  "food_id": "FOOD_<UUID>",
  "canonical_name": "두바이 쫀득 쿠키",
  "entity_kind": "PRODUCT",
  "entity_status": "ACTIVE",
  "resolution_version": "approved-exact-alias-v1",
  "approval_status": "APPROVED",
  "tracking_eligible": true,
  "revision": 4,
  "approved_aliases": [
    {"surface_form": "두쫀쿠", "normalized_form": "두쫀쿠", "scope": "food:ko", "normalization_version": "food-nfc-space-v1", "status": "ACTIVE"},
    {"surface_form": "두바이 쫀득 쿠키", "normalized_form": "두바이 쫀득 쿠키", "scope": "food:ko", "normalization_version": "food-nfc-space-v1", "status": "ACTIVE"},
    {"surface_form": "두바이쫀득쿠키", "normalized_form": "두바이쫀득쿠키", "scope": "food:ko", "normalization_version": "food-nfc-space-v1", "status": "ACTIVE"}
  ]
}
```

이는 검색어·태그 생성이나 수집 실행 결과가 아니다. B1~B2가 후속 단계에서 승인된 별칭과 정책을 소비한다.

## 4. 별칭 오류 수정과 복구

`POST /api/foods/alias`에서 `action=revoke`, alias_id, alias expected_revision 및 새 request_key/승인/검토자/근거를 보낸다. 자동 식별 근거가 취소된 후보는 검토 상태로 돌아간다. 별칭 상태는 삭제되지 않고 REVOKED로 남는다.

`GET /api/food-events?subject_id={alias_id}`에서 취소 event_id를 확인한다. `POST /api/foods/revert-alias`에 그 event_id와 현재 alias expected_revision 및 새 승인 정보를 보내면 이전 상태를 복원한다. 그 이후 변경이 있거나 별칭이 다른 활성 음식과 충돌하면 409다.

복구 후 후보를 다시 연결하려면 최신 candidate revision으로 `POST /api/foods/resolve`를 호출한다. 복구 명령이 후보를 자동 재처리하지 않는다. GET은 어떤 단계에서도 후보를 생성하거나 변경하지 않는다.
