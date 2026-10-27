# A4~A5 개발·검증 결과

2026-10-27. 음식 개체 식별, 정확 별칭 승인, 공통 후보, 수동 등록 UI/API를 구현했다. **기존 114개 + 신규 27개 = 141개 테스트가 통과했다. 운영 DB 마이그레이션과 기존 서버 재시작은 수행하지 않았다.**

RSS FOOD 0건인 격리 DB에서 사용자가 `두쫀쿠`를 등록하고, 검토자가 대표명을 `두바이 쫀득 쿠키`로 승인한 뒤 `두쫀쿠` / `두바이 쫀득 쿠키` / `두바이쫀득쿠키`가 동일한 FOOD_ID를 반환하는 것을 API와 실제 Chrome UI에서 확인했다. 음식 승인·등록은 외부 API 호출이나 추적 시작을 일으키지 않는다.

## 시작 상태와 운영 적용 여부

| 항목 | 확인 결과 |
| --- | --- |
| 실제 Git 저장소 | `D:\mz 제조기`, 브랜치 `main` |
| 시작 HEAD | `97b3c350fb780ac9c177d653bfc1f5ad88d07ff7` |
| 시작 변경 상태 | clean. 이번 작업에서 commit/reset/stage하지 않음 |
| A1~A3 코드 | HEAD에 반영됨. UPDATE UPSERT, 공통 계약, 읽기 경계, 명시적 migration 존재 |
| 운영 DB | `data/trends.sqlite3`, migration 버전 **0**, 기존 30개 테이블. A1~A3 migration도 미적용 |
| 시작 RSS FOOD | 0건 |
| 시작 진행 중 기록 | RSS/IG/YT run 및 NAVER/YT call의 미완료 기록 0, IG lease 0. 시점 검사이며 writer 부재를 증명하지 않음 |
| 기존 listener | 127.0.0.1:8000 PID 50584, :8002 PID 51280. 종료·재시작·수집 요청하지 않음 |
| baseline | 기존 114개 전부 통과, 외부 소켓 시도 0 |
| 신규 schema | 버전 3. 격리 DB 및 보존 사본의 임시 복제본에만 적용 |

근거: [시작 상태](baseline.json), [기존 테스트](baseline-tests/test-summary.json), [운영 재확인](production-final.json).

운영 DB는 읽기 검사 사이에 변화했다. 보존 시점 대비 RSS runs 82→83, items/candidates 302→307, observations 770→780을 관측했다. 기존 프로세스는 계속 실행 중이고 변경 주체는 특정하지 않았다. 따라서 **작업 시간 전체의 운영 DB 불변을 주장하지 않는다.** 개발 테스트는 운영 DB 접근을 차단했고 별도 검사는 `mode=ro`/`query_only`로 수행했다. 운영 DB에는 음식 테이블이 없고 migration 버전도 0이다.

## 보존과 복구 준비

로컬 보존 경로: `data/a4-a5-preservation-20261027T061551Z/`.

- `before.sqlite3`: SQLite online backup으로 확보한 일관된 DB 사본. integrity `ok`.
- `files/`: 변경 전 코드·정적 파일·테스트·README. manifest의 52개 파일 해시 검증 통과. `.env`는 해시만 확인하고 복사하지 않았다.
- `manifest.json`: HEAD, 파일 해시, 시작 DB 상태, 보존 데이터의 기존 만료일 유지 안내.
- 사본의 임시 복제본에 0→3 migration 후 원래 30개 테이블의 모든 원래 열·행 해시가 일치했다. FK 오류 0, integrity `ok`, legacy origin 승격 0, 재실행 적용 단계 0.

[보존 검사](preservation-check.json), [사본 migration 대조](copy-migration.json).

운영 복구가 필요하면 먼저 writer를 중지하고 **그 시점의 최신 DB를 추가 보존**해야 한다. 과거 사본을 실행 중 DB 위에 덮어쓰지 않는다. 코드 복구와 DB 복구를 함께 검토하고, 보존 시점 이후 신규 수집·음식 승인 데이터를 대조한 뒤 별도 경로의 복원본으로 전환한다. DB/WAL을 임의로 분리 복사하지 않는다. TTL을 수집 시점으로 다시 설정하지 않으며, 복원본의 만료 데이터 정리 후 조회를 허용한다. 이번 작업에서는 이 운영 전환을 실행하지 않았다.

## 구현 파일

| 파일 | 변경 목적 |
| --- | --- |
| [entity_schema.py](../../food_trends/entity_schema.py) | v3 음식·별칭·관계·후보·명령·감사 테이블과 제약 |
| [entity_types.py](../../food_trends/entity_types.py) | 기존 분류 결과를 수정하지 않는 음식 유형 gate |
| [food_service.py](../../food_trends/food_service.py) | 정확 별칭 식별, 사람 승인, 이력·멱등성·별칭 복구·병합 |
| [food_http.py](../../food_trends/food_http.py), [api.py](../../food_trends/api.py) | 공통 후보·음식 읽기/명령 API, localhost 쓰기 경계 |
| [contracts.py](../../food_trends/contracts.py) | 로컬 입력 `user_input` 계약만 추가. 외부 출처 정책 유지 |
| [migrations.py](../../food_trends/migrations.py) | 명시적 버전 3 등록 |
| [instagram_service.py](../../food_trends/instagram_service.py), [naver_service.py](../../food_trends/naver_service.py), [youtube_storage.py](../../food_trends/youtube_storage.py) | 기존 후보 기반 등록/계획에서 알려진 브랜드·매장·비음식 유형 제외 |
| [index.html](../../static/index.html), [foods.js](../../static/foods.js), [style.css](../../static/style.css) | 수동 등록·후보 검토·음식 상세·별칭 승인/복구·관계 UI |
| [food_demo.py](../../food_trends/food_demo.py) | 환경 파일·API client·scheduler를 사용하지 않는 격리 UI 실행 |
| [test_food_entities.py](../../tests/test_food_entities.py) | 신규 회귀 테스트 27개 |
| [test_foundations.py](../../tests/test_foundations.py) | 기존 migration CLI 테스트의 기대 단계만 `[1,2,3]`으로 갱신 |
| [food_browser_check.py](../../tests/food_browser_check.py) | 임시 Chrome 프로필·DB 기반 실제 브라우저 검증 |

기존 classifier, RSS 저장·분류 이력, provider 파서, API 수집 구조를 재작성하지 않았다. `youtube_foods.id` 등 기존 출처 전용 ID는 새 공통 FOOD_ID로 간주하지 않는다.

## 스키마와 식별 규칙

버전 3은 기존 30개 테이블의 데이터나 분류 이력을 변경하지 않고 다음 6개 테이블을 추가한다. 전체 DDL은 `entity_schema.py`가 기준이다. `schema_migrations`는 기존 명시적 migration 관리 테이블이다.

| 테이블 | 주요 열·제약 |
| --- | --- |
| `food_entities` (FOOD_ENTITY) | UUID 기반 `food_id` PK, `canonical_name` 비고유, `entity_kind`, `status`, `revision`, `default_scope`, `merged_into` self-FK, `created_at/updated_at`. kind=PRODUCT/DISH/INGREDIENT/FOOD_CONCEPT. 상태 ACTIVE/MERGED/RETIRED. 병합 상태와 redirect의 일관성 CHECK |
| `food_aliases` (FOOD_ALIAS) | `alias_id` PK, `food_id` FK, `surface_form`, `normalized_form`, `scope`, `normalization_version`, ACTIVE/REVOKED, revision·시각. ACTIVE인 `(normalized_form,scope,normalization_version)`만 고유 |
| `food_relations` (FOOD_RELATION) | `relation_id` PK, 양끝 food FK, VARIANT_OF/RELATED_TO, ACTIVE/REVOKED, revision·시각. 자기 참조 금지, 활성 관계 중복 금지, RELATED_TO는 ID 정렬로 대칭 중복 방지 |
| `food_candidates` | 별도 `candidate_id`, 고유 `candidate_key`, source/item/origin, 입력명·유형·범위·별칭·설명·제안 관계, 상태·revision·계약, resolved food FK·근거 alias FK·방법·resolution_version·type_gate |
| `food_commands` | `request_key` PK, operation, payload hash, 원자적으로 저장한 결과 JSON·시각. 동일 키의 다른 payload는 409 |
| `food_events` | event ID, subject 종류/ID, action, actor, reason, 전후 상태 JSON·시각, command FK, 되돌린 event FK. UPDATE/DELETE 차단 trigger. 조회의 `event_sequence`는 append 순서이며 시간 동률에도 순서가 유지됨 |

음식 대표명 자체에는 UNIQUE를 걸지 않았다. 동형어는 검토자가 다른 `scope`를 부여해야 하며, 범위를 생략한 조회가 다른 범위를 합쳐 선택하지 않는다. 기본 범위는 `food:ko`다.

정규화 버전은 `food-nfc-space-v1`이다. NFC·앞뒤 공백 정리·연속 공백 축약만 한다. 내부 공백 제거, casefold, 문자열 유사도, 임베딩, 공동 출현, 기존 NAVER 별칭 상수의 자동 신뢰·승격은 하지 않는다. 따라서 `두바이 쫀득 쿠키`와 `두바이쫀득쿠키`는 각각 승인해야 한다.

VARIANT_OF는 자식→원본 방향의 DAG다. 서비스의 재귀 검사와 INSERT/UPDATE DB trigger가 순환을 차단한다. RELATED_TO는 방향 없는 연관이며 계보가 아니므로 간접적인 연관 고리는 허용한다. 자기 참조·같은 활성 관계의 중복은 허용하지 않는다.

## 후보와 승인 처리

1. 사용자 등록 또는 명시적 RSS import가 공통 후보를 NEW로 만들고 이력을 기록한다. 원래 RSS `items/candidates/classification_history`는 수정하지 않는다.
2. NEEDS_REVIEW로 전이한다. 유형이 확정된 음식이고 같은 scope의 **ACTIVE 승인 별칭**과 정확히 일치할 때만 RESOLVED로 자동 연결한다. 제안 별칭은 식별 근거가 아니다. UNKNOWN은 사람이 음식 유형을 확인해야 한다.
3. 검토자는 신규 생성, 기존 음식 연결, 별칭 승인, 별도 파생 생성, 관계 추가, 거절을 선택한다. `approved=true`, 검토자, 근거, 기대 revision이 필수다. 검토 상태의 후보만 승인·거절할 수 있다.
4. 신규 생성 승인은 후보명·대표명·제안 별칭·추가 별칭 및 제안 관계를 함께 승인한다. 기존 음식 연결은 `approve_aliases=true`를 명시한 경우에만 제안 별칭을 승인한다.
5. 브랜드/매장/비음식 후보는 사람 승인 요청으로도 같은 후보를 음식으로 승격할 수 없다. 브랜드의 특정 음식 상품은 PRODUCT 후보로 따로 등록한다. 기존 FOOD_BRAND 분류는 삭제·재분류하지 않는다.

수동 후보의 중복 키는 로컬 입력 + 정규화 이름 + scope다. 같은 이름·범위의 다른 request key도 기존 후보를 반환한다. 유형·설명·제안 별칭을 바꿔 재등록해도 기존 입력을 조용히 덮어쓰지 않는다. 추가 별칭은 음식 상세의 승인 작업으로 처리한다. 서로 다른 별칭으로 들어온 후보는 출발 근거가 다른 후보로 남되 같은 food_id에 연결될 수 있다.

모든 쓰기는 `BEGIN IMMEDIATE` 안에서 command·개체·별칭·관계·후보·event를 함께 commit한다. 실패하면 모두 rollback한다. 동일 request key/payload 재실행은 **원래 응답 스냅샷**을 반환하므로 최신 상태는 GET으로 읽는다. 프런트엔드도 등록 응답 후 최신 후보를 다시 조회한다.

## 변경·복구와 제한된 병합

- 대표명 변경: food_id 유지, 새 대표명의 정확 별칭도 사람 승인으로 추가. 이전 승인 별칭과 전후 이력 보존.
- 별칭 취소·이동: 같은 alias_id의 revision 증가와 전후 이력 기록. 그 별칭을 근거로 자동 연결된 후보는 NEEDS_REVIEW로 되돌린다. 별도로 사람이 확정한 연결은 보존한다.
- 복구: `revert-alias`가 해당 event의 이전 food/status를 복원한다. 최초 승인 되돌리기는 REVOKED다. 이후 변경이 있으면 거부하며, 복구도 새 승인 event로 기록하고 `reverses_event_id`를 남긴다. 후보 재식별은 명시적 resolve에서 수행한다.
- 병합: 명시적 사람 승인·양쪽 revision을 요구한다. 원본 ID를 삭제하지 않고 MERGED/merged_into를 남기며 승인 별칭을 활성 대상에 옮긴다. 자동 연결은 다시 검토시키고 기존 사람 승인 후보의 과거 ID는 보존한다. 자기 병합·비활성 대상 병합을 거부한다.
- 활성 관계가 있는 원본의 병합은 거부한다. 관계를 임의 재배치하지 않는다. 일반적인 split/unmerge, 관계 삭제·재배치, RETIRED 관리 API는 이번 MVP에서 제공하지 않는다. RETIRED는 스키마에 정의된 비활성 상태다.

## 사용자 UI와 API

대시보드의 **음식 사전 · 검토와 승인**에서 이름·유형·별칭·설명과 선택적 범위·관계를 입력한다. 등록 후 검토 창에서 대표명, 작업, 검토자, 근거를 확인하고 승인 체크 후 저장한다. 승인 음식 상세에서 ID, 별칭, 관계, 전달용 JSON, 변경 이력을 확인한다. 별칭 추가·취소·이동·복구, 대표명 변경, 관계 추가를 지원한다. 병합은 검토 API로 제공한다.

운영 DB를 건드리지 않고 실행하는 방법:

```powershell
py -3.12 -B -m food_trends.food_demo --port 8010
```

`data/food-identity-demo.sqlite3`만 명시적으로 생성/업그레이드한다. `.env`와 DATABASE_PATH를 읽지 않으며 API client·scheduler를 시작하지 않는다. 음식 명령 외 POST는 거부한다. 8010이 사용 중이면 `--port 0`으로 빈 포트를 선택한다. 기존 운영 서버에서는 재시작/DB 적용 전 새 화면·API가 제공되지 않는다.

읽기 API는 모두 `Storage.reader()`를 사용하며 없는 후보나 음식을 생성하지 않는다.

| GET | 반환 |
| --- | --- |
| `/api/foods` | 모든 음식 descriptor 목록 |
| `/api/foods/{food_id}` | 단일 descriptor, 승인/취소 별칭·관계 |
| `/api/foods/resolve?name=...&scope=food:ko` | 정확 별칭 조회; RESOLVED 또는 NEEDS_REVIEW. 쓰기 없음 |
| `/api/food-candidates` / `/{candidate_id}` | 공통 후보 목록 / 상세·최신 resolution |
| `/api/food-events?subject_id=...` | 음식·후보·별칭·관계 ID의 append 순서 이력 |

명령은 `POST /api/foods/{operation}`, JSON이다. localhost Host와 동일 Origin을 검사하며 최대 16KiB, 미정의 필드를 거부한다. 모든 명령에 `request_key`가 필요하다. submit/import/resolve를 제외한 승인 명령에는 `approved:true`, `actor_ref`, `reason`, `expected_revision`이 필요하다.

| operation | 추가 입력 |
| --- | --- |
| `submit` | name, 선택 aliases/entity_kind/description/relations/scope. 기본 kind PRODUCT. 관계는 `{food_id,relation_type}` 목록 |
| `import` | source_code=`google_trends_rss`, source_item_id. 기존 RSS 분류 결과만 명시적으로 가져옴 |
| `resolve` | candidate_id, expected_revision. 승인 별칭으로 다시 확인 |
| `review` | candidate_id, action=`create/link/create_variant/reject`. 필요 canonical_name/entity_kind/aliases/food_id/approve_aliases/parent_food_id |
| `rename` | food_id, canonical_name. entity revision 사용 |
| `alias` | action=approve: food_id/surface_form/선택 scope, entity revision. revoke/restore/reassign: alias_id, alias revision. reassign에는 대상 food_id |
| `revert-alias` | event_id, 현재 alias revision. 그 event 이후 변경이 없어야 함 |
| `relate` | from_food_id/to_food_id/relation_type, from entity revision |
| `merge` | food_id/target_food_id, source expected_revision, expected_target_revision |

오류는 형식/승인 누락 400, 금지 추출 403, 없는 ID 404, stale revision·별칭 충돌·관계 충돌·키 재사용 충돌 409다. [API 사용 예](API_EXAMPLE.md)를 제공한다.

## A1~A3 계약·보존 정책과 호환성

`envelope`, origin, field_states, `require_operation`, `Storage.reader`, 명시적 migration을 재사용한다. 외부 4개 출처의 정책은 바꾸지 않았다. `user_input`은 로컬 입력의 출처 구분이며 새 외부 수집기가 아니다.

- 수동 후보는 origin=user. 미래 API 관측의 origin을 user로 바꾸지 않는다.
- RSS import는 기존 observation 계약의 origin을 유지하고, 없는 과거 계약은 legacy_unknown으로 둔다. 새 live_api 근거를 만들지 않는다.
- RSS 기존 규칙 사용은 `existing_food_rules`, 로컬 입력은 `candidate_resolution` 정책 검사를 거친다. IG/YT/NAVER의 음식 추출 요청은 `food_extraction` gate에서 차단되고 command도 rollback된다.
- NAVER 응답·ratio 비저장, YouTube 파생 음식 분석 비활성/29일 TTL, Instagram 기존 보존 설정은 유지한다. 제한된 출처의 본문을 공통 후보에 복사하는 경로는 없다. 계약에 인증정보를 추가하지 않는다.
- 서비스 생성자·GET에서 migration·후보 생성·정리·재처리를 실행하지 않는다. 운영 버전 0 또는 이전 버전 2 DB에 새 앱을 바로 재시작하면 기존의 명시적 migration 요구 오류로 중단된다.
- 기존 출처 전용 추적기는 계속 별도 체계다. 알려진 FOOD_BRAND/STORE/LOCATION 등은 기존 후보 기반 등록/계획에서 제외한다. 과거에 이미 승인·저장된 출처 전용 추적 행을 자동 삭제하거나 공통 음식으로 이관하지 않는다.

## 검증 결과

| 검증 | 결과·근거 |
| --- | --- |
| 변경 전 baseline | 114/114 통과 |
| 최종 단위·API 회귀 | **141/141 통과**, 실패/오류/skip 0, 외부 소켓 시도 0, 운영 DB 접근 차단. [summary](validation/test-summary.json) |
| 신규 27개 | 별칭 3개→동일 ID, 미승인/fuzzy/짭쫀쿠 차단, 동형어 범위, 브랜드/매장 gate, 반복·동시 요청 멱등성, 승인/거절·동일 시각 이력 순서, rename ID 유지, 별칭 이동/취소/복구·stale 복구 차단, 사람 연결의 별칭 비승인, DAG·대칭 중복, 사람 병합, RSS import·origin 유지, 정책 차단, 원래 30개 테이블 보존, v2→3/retry, GET 읽기 경계, HTTP 등록→승인 및 입력 경계 |
| 실제 브라우저 | 임시 headless Chrome·격리 DB. FOOD 0→등록→승인→재등록→동일 ID, 모바일 패널 폭, JS 오류 0, 외부 페이지 요청 0, 출처 POST 0. [summary](browser-summary.json), [모바일 화면](manual-registration-mobile.png) |
| 보존 사본 migration | 30개 테이블의 원래 열·행 동일, integrity ok, FK 오류 0, legacy 승격 0, 조회 변경 0, 재실행 적용 0 |
| 정적 확인 | `node --check static/foods.js`, `git diff --check` 통과 |

재실행:

```powershell
py -3.12 -B tests/run_offline.py --output reports/development-a4-a5-2026-10-27/validation
py -3.12 -B tests/food_browser_check.py
py -3.12 -B tests/verify_preserved_copy.py data/a4-a5-preservation-20261027T061551Z/before.sqlite3 --output reports/development-a4-a5-2026-10-27/copy-migration.json
```

Chrome 검증은 이 환경에서 샌드박스의 로컬 IPC 제한 때문에 실행 범위 확장이 필요했다. 임시 프로필·격리 DB만 사용했으며 기존 Chrome 세션을 재사용하지 않았다. 별도 의존성 설치나 외부 서비스 연결은 하지 않았다.

## B1~B2 전달 계약과 남은 과제

`FoodService.food(food_id)` / GET 단일 음식의 descriptor를 전달 계약으로 사용한다. 필수 출력은 `food_id`, `canonical_name`, `approved_aliases`, `entity_kind`, `entity_status`, `resolution_version`, `approval_status`다. `revision`, `tracking_eligible`, `merged_into`, 각 별칭의 scope/normalization_version도 제공한다.

B1~B2는 ACTIVE·APPROVED·tracking_eligible인 음식과 승인 ACTIVE 별칭만 소비해야 한다. MERGED/RETIRED, NEEDS_REVIEW, REJECTED, 제안 별칭은 검색 계획에 넣지 않는다. alias scope를 섞지 않고 descriptor revision을 기록한다. 병합 전 ID를 만난 경우 merged_into를 명시적으로 따라 최신 ACTIVE 상태를 재확인해야 한다. 과거 사람 승인 후보의 source ID를 현재 ID인 것처럼 사용하지 않는다.

다음 선행 작업이 남는다.

1. 운영 writer·유지보수 프로세스 정리, 최신 온라인 백업, 격리 대조 후 별도 승인된 0→3 적용·재시작 계획. 이번에는 미실행.
2. B1의 공통 TRACKING_TARGET 상태·세대·pause/resume/stop, B2의 출처별 serializer·동일 요청 공유·API 예산 예약. 현재 descriptor만 제공하며 검색 요청은 생성하지 않는다.
3. 기존 출처 전용 음식/후보/태그와 공통 FOOD_ID의 명시적 검토·전환. 새 음식 등록을 기존 네 출처에 자동 연결하지 않는다.
4. 검토자 actor_ref는 로컬에서 입력하는 식별값이며 사용자 인증·권한 증명이 아니다. 다중 사용자 배포 전 역할/인증을 마련해야 한다.
5. 일반 split/unmerge, 관계 해제·재배치, 거절 후보 재개, 개체 폐기 관리, 대용량 목록 페이지 처리와 승인 UX 개선은 후속이다. 별칭 복구는 현재 revision과 충돌하지 않는 변경만 허용한다.
6. 음식/브랜드 판정은 독립 유형 gate와 사람 근거에 의존한다. 미지의 브랜드를 사용자가 음식이라고 입력하는 오류까지 자동 판별하지 않는다. 출처 사용 범위 미확인 기능은 계속 차단한다.

추적 자동 연결·Oracle 이관·신규 외부 수집원·유료 LLM·임베딩 자동 병합·통합 유행 점수는 구현하지 않았다. 운영 G1 준비 완료나 전 출처 실제 수집 성공을 주장하지 않는다.
