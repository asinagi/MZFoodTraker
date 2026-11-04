# ORACLE-RUNTIME-2 Oracle 계획 수립·스케줄·실행 소유권·예산 정합성

2026-11-04. Oracle 런타임이 SQLite 계획기에 의존하지 않고 **계획 수립 → 예정 슬롯 → 실행 → Mock 수집 → 정제 → 관측**을 단독으로 수행하도록 보완했다. Shadow와 런타임의 동시 쓰기는 **데이터베이스 수준에서 차단**되고, 외부 사용량을 확인하지 못한 계정 범위에서는 실행이 막힌다. 모든 신규 검증은 격리 SQLite 대역과 실제 Oracle(MZ_TEST)에서 같은 Mock 입력으로 수행했다. 실제 외부 API 호출 0건, 운영 SQLite·서버 변경 없음, 운영 전환·Live 활성화 없음.

## 1. 구현 결과

| 단계 | 커밋 | 내용 |
| --- | --- | --- |
| R1 계획 수립 | `91ab0dd feat: support Oracle tracking query planning` | `QueryPlanner`의 직렬화·별칭 그룹·공유 Query·정책/권한·버전·예산 판단을 그대로 두고 저장 primitive만 분리. `OracleQueryPlanner`가 대상 테이블로 Preview(읽기 전용)·Commit(삽입 전용 이력, 요청 키 멱등) 수행 |
| R2 슬롯·스케줄 | `203e67b feat: implement Oracle tracking slot scheduling` | SQLite 슬롯 계약(대상·세대·예정 슬롯 유일)에 생명주기를 더한 `tracking_slots`. 작업·Outbox·매핑·슬롯·다음 예정 시각을 한 트랜잭션에서 확정. 실행 재검증이 SQLite와 같은 "next_due 일치 또는 슬롯 존재" 규칙을 사용 |
| R3 실행 소유권 | `f3a77d1 fix: enforce exclusive Oracle runtime ownership` | 대상 DB의 소유 모드(IDLE·SHADOW·RUNTIME_TEST, RUNTIME_PRODUCTION 불허)와 프로세스별 임대. 모든 쓰기 트랜잭션이 공유 잠금 아래 소유권을 검증, 모드 전환은 배타 잠금으로 진행 중 트랜잭션을 기다린 뒤 판단 |
| R4 예산 정합성 | `0f42a3d feat: reconcile Oracle API quota accounting` | 레거시 수집기 호출의 예산 관련 최소 메타데이터를 근거·불확실성과 함께 이관하는 원장, 계정 범위별 사용량 확인 시점, 계정 행 잠금으로 예약 직렬화, 미확인·오래된 사용량이면 실행 차단 |
| 문서 | `docs: record Oracle runtime planning and safety verification` | 이 문서 |

## 2. 변경한 스키마·테이블 (대상 스키마 v2 → v6, 명시적 업그레이드)

| 버전 | 테이블 | 비고 |
| --- | --- | --- |
| v3 | `planner_plan_targets`, `planner_commands` | 런타임 소유(SQLite 계획기는 자기 사본 유지). 계획 이력·명령 멱등 |
| v4 | `tracking_slots` | `slot_id, target_id, plan_id, job_id, generation, food_revision, due_slot, status, created_at, claimed_at, completed_at, cancellation_reason`. 유일키 `(target_id, generation, due_slot)`. 상태 SCHEDULED→CLAIMED→COMPLETED/FAILED/CANCELLED. `due_slot`은 정규화된 UTC 문자열(Oracle은 TSTZ 열을 유일키에 쓸 수 없음, ORA-02329). 요청서의 `revision`은 SQLite 계약의 `food_revision`, 계획 참조는 `plan_id`·`job_id`로 두고 중복 열은 추가하지 않음 |
| v5 | `runtime_ownership`, `runtime_participants` | 모드 행 1개(모드·epoch) + 프로세스 임대 행. Oracle 예약어를 피해 `owner_mode` 사용 |
| v6 | `quota_usage_ledger`, `quota_accounts` | 레거시 사용량 원장(근거·신뢰도·원 출처 라벨·원본 행 참조), 계정 범위별 사용량 확인 상태·시점(예약 직렬화 잠금 행 겸용) |

`TargetRepository.migrate()`는 없는 객체·열만 만든다. 테스트가 v1 스키마를 재구성해 v1→v6 업그레이드와 재실행 무변화를 실제 Oracle에서 확인했다. 서비스 생성자는 버전만 확인하고 DDL을 실행하지 않는다. SQLite 스키마는 변경하지 않았다(v11 유지).

## 3. SQLite와 Oracle 실행 결과 비교

각 테스트는 같은 SQLite 상태를 B7로 대상에 적재한 뒤 같은 시각에서 SQLite 경로와 Oracle 경로를 각각 실행하고, 결과와 테이블을 논리 값으로 비교한다(무작위 ID·회차 식별자만 제외).

| 영역 | 비교 대상 | 결과 |
| --- | --- | --- |
| R1 | Preview·Commit 결과 전체(예산 판단 포함), `planner_queries/plans/query_targets/plan_targets/commands` | 일치 |
| R2 | `tracking_targets`(next_due_at·revision·last_success_at), 슬롯 키, 매핑, 작업 상태 | 일치 |
| R4 | 레거시 사용량을 포함한 예산 판단(관측·예약·잔여), 계정 범위별 판단 | 일치 |
| 전체 경로 | 계획·슬롯·작업·스테이징·연결 13개 테이블, FOOD_ID 조회, 외부 요청 수 | 일치 |

의도적 차이: 예산 판단의 **추가 설명 정보**(레거시 사용량, 근거, 신뢰도, 창, 출처)는 `quota.usage()`에만 붙인다. `assess()`는 SQLite 계약과 같은 값이어야 계획 비교와 저장된 계획이 같아지기 때문이다. 상태 조회(`status()`)와 계획 조회(`plan()`)는 `usage()`를 보여준다.

## 4. 동시성 및 장애 복구

| 시나리오 | 결과 |
| --- | --- |
| 두 스케줄러 동시 실행 | Query별 작업 1개, 대상별 슬롯 1개, 다음 예정 시각 1회만 진행(Query 행 → 대상 행 정렬 순 잠금) |
| 두 Worker 동시 Claim | 1개만 성공, 슬롯 claimed_at 1회. SKIP LOCKED가 대기하지 않음(공유 소유권 잠금과 공존 확인) |
| 계획 확정 트랜잭션 중 실패(슬롯 생성 후·커밋 직전) | 작업·Outbox·매핑·슬롯 0, 예정 시각 불변. 재시도 시 정상 1회 |
| Shadow 활성 중 런타임 쓰기 | `ownership_held_by_SHADOW`로 거부, 쓰기 0. 미리보기·상태 조회는 허용 |
| 런타임 활성 중 Shadow 전달·B7 적재·복구 | `ownership_held_by_RUNTIME_TEST`로 거부, Shadow 이벤트는 실패로 계산되지 않고 그대로 남음. 비교(읽기)는 허용 |
| 런타임 프로세스 강제 종료(미완료 작업 있음) | 임대 만료 후에도 Shadow 전환 거부(`runtime_jobs_in_flight`). 새 런타임이 이어서 완료한 뒤에만 IDLE→SHADOW 가능. 죽은 프로세스의 늦은 쓰기·갱신은 `ownership_expired`·`ownership_mode_changed`로 거부 |
| 작업 없는 런타임 방치 | 임대 만료 후 Shadow가 인수, 이전 프로세스 쓰기 거부 |
| 임대 시간보다 오래 걸리는 Shadow 전달 | 쓰기마다 소유권 확인 트랜잭션 안에서 임대를 연장해 끝까지 전달, 실패 0. 만료된 임대는 연장하지 않음 |
| 유휴 런타임의 임대 만료(다른 모드 인수 없음) | 만료된 임대는 되살리지 않고 새 임대로 다시 참여. Shadow 소유 중이면 참여 자체가 거부됨. 작업 결과는 작업 Lease·Fencing이 계속 판정 |
| 늦은 Worker 결과 | 작업 Lease 이전 후 이전 소유자의 확정 거부(LostLease), 콘텐츠·영수증 중복 0 |
| 모드 전환 중 오류·대상 연결 끊김 | 롤백되어 IDLE 유지, 임대 0. 이후 정상 전환 |
| 열린 런타임 트랜잭션 중 모드 전환 | 전환이 해당 트랜잭션 종료까지 대기한 뒤 거부 |

## 5. 예산 원장과 사용량 불확실성

- **원장 내용(최소 메타데이터)**: 출처·계정 범위·quota 종류·창·리셋 시간대·사용 1단위·(Instagram) 정규화 태그·근거·신뢰도·원 출처 라벨·원본 행 참조(`youtube_calls:12` 등)·관측 시각. 요청 본문·검색어·응답은 이관하지 않는다. 아직 열린 창의 행만 옮긴다.
- **근거 구분**: `PROVIDER_CONFIRMED`(생성하지 않음 — 공급자가 사용량을 알려주지 않으므로) / `LEGACY_LOCAL_COUNT`(완료된 호출) / `LEGACY_SENT_UNCERTAIN`(running 상태로 남은 호출) / `LEGACY_UNKNOWN_ORIGIN`(출처 legacy_unknown). 신규 예약은 기존 `quota_reservations/quota_charges`, 전송 후 불확실 호출은 기존 `execution_calls`(UNKNOWN)에 있다. **모든 근거를 보수적으로 사용량에 포함**한다.
- **계정 범위 분리**: Instagram 사용자별(`ig:<user>`)로 따로 집계하고 합치지 않는다.
- **확인 상태**: 가져오기마다 계정 범위별로 사용량이 확인된 시점을 기록한다. 15분보다 오래되면 STALE, 기록이 없으면 UNVERIFIED다. Live 실행은 항상, Mock은 `require_verified_usage`로 검사하며 확인되지 않은 계정은 `EXTERNAL_USAGE_UNVERIFIED`로 차단된다(외부 호출 0).
- **공급자 한도와 자체 상한 구분**: 줄마다 `provider_limit`·`local_limit`, `provider_window`·`local_window`·`provider_timezone`을 표시한다. YouTube는 로스앤젤레스 일자(DST 25/23시간), NAVER는 KST 일자, Instagram은 7일 고유 태그.
- **동시성**: 예약과 시도 기록은 계정 행을 잠근 뒤 판단해, 마지막 1단위를 두 계획이 동시에 가져가지 못한다(실제 Oracle 경쟁 테스트). 가져오기도 계정 행을 먼저 갱신해 예약과 직렬화된다.
- **가져오기의 소유권**: 런타임의 예산 안전 일부이므로 RUNTIME_TEST 참여자로 쓴다. 런타임 작업 중에도 갱신할 수 있고, Shadow 소유 중에는 거부된다.

## 6. 테스트 결과

| 실행 | 대상 코드 | 결과 |
| --- | --- | --- |
| Oracle 없는 기본 전체 회귀(오프라인 러너) | **최종 코드** | 403개 실행, 실패 0·오류 0, 실제 Oracle 전용 104개 skip, 외부 접속 시도 0, 운영 DB 접근 차단 ([default-tests](default-tests/test-summary.json)) |
| 실제 Oracle 포함 전체 회귀(오프라인 러너) | 마지막 R3 보완(`guard` 임대 연장) **전** 코드 | 505개 실행, 실패 0·오류 0·skip 0, 외부 접속 시도 0 ([oracle-full-tests](oracle-full-tests/test-summary.json)) |
| 보완 후 재실행: 소유권 확인 쓰기를 거치는 10개 모듈(`test_runtime_ownership`, `test_shadow`, `test_target_migration`, `test_oracle_execution`, `test_oracle_scheduling`, `test_oracle_planner`, `test_oracle_quota`, `test_oracle_pipeline`, `test_oracle_observations`, `test_oracle_ingestion`) | 최종 코드와 동작 동일(이후 변경은 모듈 설명문뿐) | Oracle 미설정 실행 79개(앞 9개 모듈의 대역) 통과, 실제 Oracle 환경 실행 178개(대역 90 + 실제 Oracle 88) 통과, 실패·오류 0 ([oracle-post-fix-tests](oracle-post-fix-tests/test-summary.json)) |
| 보완 후 재실행하지 않은 실제 Oracle 테스트 | — | `test_target_compat.OracleContractTests` 16개. DDL·형식·시각 계약만 검사하고 소유권 확인(`guard`)을 호출하지 않아 보완의 영향을 받지 않으므로, 보완 전 전체 회귀의 통과 결과를 적용했다 |

보완 후 재실행은 오프라인 러너가 모듈 선택을 지원하지 않아 `unittest`로 직접 실행했다. 이 테스트들은 Mock Transport만 쓰며, 같은 테스트가 오프라인 러너 아래에서 외부 접속 시도 0으로 통과한 기록이 있다. 실제 Oracle 전용 테스트는 보완 후 104개 중 88개를 다시 실행했고, 나머지 16개는 위 사유로 재실행하지 않았다.

신규 테스트(대역·실제 Oracle 각각): R1 7, R2 6, R3 10, R4 6, 전체 경로 1. 단계별 커밋 직전 결과는 R1 380/461, R2 386/473, R3 394/489, R4 403/505이다(기본/실제 Oracle 전체, 모두 실패 0. R4의 실제 Oracle 전체 505는 마지막 보완 전 코드이며 보완 후 검증은 위 표). 요청서의 기존 447개는 삭제 없이 유지했다. 최종 테스트 총수는 507개(기본 실행 403 + 실제 Oracle 전용 104)이며, 기존 447개에 신규 60개(대역·실제 Oracle 각 30)를 더한 수다. 기존 기본 실행 테스트는 최종 코드에서 통과했다. 기존 실제 Oracle 전용 테스트는 보완 전 전체 회귀에서 통과했고, 그중 소유권 확인 쓰기를 거치는 것은 보완 후에도 다시 통과했다.

최종 기본 전체 회귀는 Oracle 컨테이너를 정지한 상태에서 272초가 걸렸다. 컨테이너를 기동한 채 실행한 직전 기본 회귀는 2,797초였고, 아래 두 임대 만료 오류는 모두 이처럼 실행이 크게 느려진 조건에서 나타났다.

R4 첫 실제 Oracle 포함 회귀(503개)에서 대역 테스트 1건이 오류였다. 부하로 테스트 중간에 120초가 지나 유휴 런타임의 소유권 임대가 끝났고, 엄격한 "만료 임대 갱신 금지" 규칙 때문에 다음 쓰기가 실패했다. 만료된 임대는 계속 되살리지 않되, 같은 프로세스가 **새 임대로 다시 참여**하도록 고쳤다. 참여는 다른 모드가 소유 중이면 거부되므로, 모드 전환을 넘는 늦은 쓰기 차단은 그대로다.

두 번째 회귀(505개)에서는 전체 경로 테스트 1건이 대역·Oracle 모두 실패했다. 같은 관측 시각·지표의 비교 맥락(`context_hash`)이 다른 행의 조회 순서가 정해져 있지 않았기 때문이다. 음식별 지표 조회에 `context_hash` 정렬을 추가해 결정적으로 만들었다(값·집계 변화 없음).

세 번째 회귀에서는 실제 Oracle 포함 505개가 모두 통과했다. 기본 실행에서는 Shadow 테스트 1건이 `ownership_expired`로 오류였다. 부하 중 Shadow 전달 한 번이 120초를 넘겼는데, Shadow 전달·B7 적재에는 임대 갱신이 없었다. 소유권 검증(`guard`)이 **아직 만료되지 않은** 임대의 남은 시간이 절반 미만이면 같은 쓰기 트랜잭션 안에서 연장하도록 고쳤다. 계속 쓰는 참여자는 살아 있는 것이므로 연장하고, 이미 만료된 임대는 여전히 거부한다. 이 연장은 모든 소유권 확인 쓰기(Shadow·B7·런타임·사용량 가져오기)에 적용된다. 회귀 테스트로 `test_long_shadow_delivery_keeps_its_lease`(전달 중 시계를 이벤트마다 40초씩 진행, 임대 60초)를 추가했다. 이 수정 뒤 소유권 확인 쓰기를 거치는 10개 모듈을 대역·실제 Oracle에서 다시 실행하고, 기본 전체 회귀를 최종 코드로 다시 실행했다(위 표). 연장은 쓰기 트랜잭션 안에서 참여자 행을 갱신하므로, 같은 프로세스가 그 트랜잭션을 연 채 다른 연결로 소유권 확인 쓰기를 열면 행 잠금을 기다릴 수 있다. 코드상 중첩 호출은 모두 같은 트랜잭션(`connection=t`)을 넘기므로 해당 경로가 없음을 확인했다.

이 R3 보완 두 건과 지표 정렬 수정은 R4 검증을 직접 막는 결함이어서, 같은 파일의 R4 변경과 함께 R4 커밋에 포함했다.

| 요청서 번호 | 테스트 |
| --- | --- |
| 1~3 | `test_commit_matches_sqlite_for_every_source_and_shared_query`, `test_previews_match_including_budget_and_policy_rejections` |
| 4 | `test_preview_is_read_only` |
| 5 | `test_invalidated_targets_are_not_planned_or_executed`, `test_alias_revocation_needs_refresh_and_keeps_old_plan`, `test_version_change_invalidates_old_plan` |
| 6, 10 | `test_schedule_matches_sqlite_and_is_not_repeated` |
| 7 | `test_concurrent_schedulers_create_one_slot_and_job` |
| 8 | `test_failed_scheduling_transaction_rolls_back_everything` |
| 9 | `test_pause_cancels_open_slot_and_resume_schedules_a_new_generation`, `test_alias_change_and_stale_generation_invalidate_existing_plans` |
| 11 | `test_kst_midnight_and_los_angeles_dst_boundaries` |
| 12 | `test_runtime_write_refused_while_shadow_owns_the_target` |
| 13 | `test_shadow_refused_while_runtime_owns_the_target` |
| 14 | `test_killed_runtime_recovers_without_handing_unfinished_jobs_to_shadow`, `test_abandoned_runtime_without_jobs_can_be_taken_over`, `test_idle_runtime_rejoins_with_a_new_lease_in_the_same_mode`, `test_long_shadow_delivery_keeps_its_lease` |
| 15 | `test_late_worker_result_is_refused` |
| 16 | `test_failed_mode_switch_changes_nothing`, `test_switch_waits_for_an_open_runtime_transaction` |
| 17, 18 | `test_legacy_usage_and_new_reservations_match_sqlite`, `test_unverified_or_stale_usage_blocks_execution_without_http` |
| 19 | `test_full_budget_defers_without_http` |
| 20 | `test_imports_and_reservations_are_not_duplicated` |
| 21 | `test_account_scopes_keep_separate_budgets` |
| 22 | `test_no_debit_is_ever_confirmed` |
| 전체 경로 | `test_oracle_runtime_runs_the_whole_path_alone`: SQLite와 결과 일치, 외부 호출 수 동일, 중복 0, FK 위반 0, 재실행 후 변화 0(명령 기록 제외), 정책 차단 유지, SQLite 파일 변경 0 |

## 7. 실제 외부 API 호출

0건. 모든 실행은 Mock Transport다. 오프라인 러너로 실행한 전체 회귀(기본·실제 Oracle)의 외부 접속 차단 기록은 0이다. 보완 후 모듈 재실행은 `unittest` 직접 실행이며, Mock Transport만 쓰는 테스트로 실제 API 클라이언트를 호출하지 않는다.

## 8. 운영 SQLite·서버 영향

읽기 전용 점검만 했다. 모든 테스트 실행이 끝난 뒤(2026-11-04 05:46 UTC) 다시 확인했다([production-final.json](production-final.json)). 운영 DB는 v0, 테이블 30개, integrity ok이고, 본 파일 수정 시각(2026-10-28 14:20)과 WAL 0바이트도 그대로다. 8000/8002 서버는 이전 단계부터 정지 상태이며 시작·중지하지 않았다. Oracle 컨테이너는 테스트 동안만 기동하고 정지 상태로 되돌렸다. MZ_TEST 초기화는 매번 보호 조건을 확인한 뒤에만 수행했다.

## 9. Git

| 단계 | 커밋 | push |
| --- | --- | --- |
| R1 | `91ab0dd feat: support Oracle tracking query planning` | fast-forward |
| R2 | `203e67b feat: implement Oracle tracking slot scheduling` | fast-forward |
| R3 | `f3a77d1 fix: enforce exclusive Oracle runtime ownership` | fast-forward |
| R4 | `0f42a3d feat: reconcile Oracle API quota accounting` (R3 보완 2건·지표 정렬 수정 포함) | `f3a77d1..0f42a3d` fast-forward |
| 문서 | `docs: record Oracle runtime planning and safety verification` (이 문서·README) | fast-forward |

R1~R3는 기본·실제 Oracle 전체 회귀를 통과한 뒤 커밋했다. R4는 최종 코드의 기본 전체 회귀(403)와 보완 후 실제 Oracle 재실행(178)을 통과한 뒤 커밋했다(실제 Oracle 전체 505는 보완 전). 단계마다 관련 파일만 스테이징하고, 비밀값 패턴과 Oracle 접속 정보 값 대조(0건)를 거쳤다. 원격 HEAD(`git ls-remote`)가 로컬과 같은지 매번 확인했다. `git reset --hard`·force push는 쓰지 않았고, `.env`·DB 파일·백업·토큰은 커밋하지 않았다. `*.log`는 저장소 규칙상 제외되므로 테스트 기록은 요약 JSON만 커밋했다.

## 10. 운영 승인 게이트

G-OPS-1~6은 모두 승인 대기 상태이며 하나도 실행하지 않았다(운영 백업·Writer 정지·운영 마이그레이션·수집 소유권 이전·Live 검증·운영 Oracle 전환). RUNTIME_PRODUCTION 모드는 코드에서 항상 거부된다.

## 11. 미완료 항목과 후속 우선순위

| 과제 | 영향 | 우선순위 |
| --- | --- | --- |
| Oracle → SQLite 역방향 복구 | 전환 후 되돌리기 수단 없음. 아래 12절의 계약이 전제 | 높음(전환 전) |
| Oracle 런타임 Live 모드(Instagram 원격 고유 태그 점검, 출처 승인 registry, 수집 소유권 이전) | Live 실행 불가 | 높음, G-OPS-4/5 승인 후 |
| 추적 대상 명령(등록·중단·재개)의 Oracle 직접 처리와 `tracking_events` | 현재 대상 생명주기 결정은 SQLite에서 하고 Oracle은 반영(슬롯 취소·새 세대 계획)만 함 | 중간 |
| 레거시 사용량 가져오기의 주기 실행 | 지금은 명시적 호출. 15분이 지나면 런타임이 스스로 차단 | 중간(운영 전 스케줄 필요) |
| 남은 legacy 테이블 전체 이관 | 예산 외 이력·검색 연결은 SQLite에만 있음 | 중간 |
| 대량 처리 성능(행 단위 조회 후 삽입, 재검증 반복 조회) | 대량 데이터에서 느릴 수 있음 | 낮음→운영 전 측정 |
| RSS 수집기의 Oracle 직접 저장, B8 집계·화면 | 범위 밖 | 별도 요청 |

## 12. 역방향 복구 및 B8 인수 계약

**역방향 복구(Oracle → SQLite, 미구현)가 지켜야 할 사항**
- Oracle에만 있는 런타임 상태: `execution_mappings/leases/checkpoints/item_receipts/blocks`, `quota_charges`, `tracking_slots`(생명주기 포함), `planner_plan_targets/commands`(런타임 사본), `runtime_ownership/participants`, `quota_usage_ledger/accounts`, 런타임이 쓴 `source_contents`. 이 중 SQLite에 대응 테이블이 있는 것(`tracking_slots`, `planner_plan_targets`, `planner_commands`, `quota_charges`, `execution_*`)은 열 의미가 같고, 생명주기·근거 열은 SQLite에 없다. 따라서 복구는 손실을 기록해야 한다.
- 결정적 키(`ITEM_`, `SNAP_`, `INGEST_`, `METRIC_`, mention_key, plan_id, query_id)는 DBMS와 무관하게 같으므로 대조의 기준이 된다. 런타임 작업·호출 ID는 무작위이거나 Oracle 고유 형식(`JOB…:C00001`)이다.
- 복구는 런타임 쓰기가 멈춘 상태(소유 모드 IDLE, 미완료 런타임 작업 0)에서만 시작해야 하고, R3 소유권을 SHADOW와 같은 배타 쓰기 모드로 확장해 쓰는 것이 자연스럽다.
- 출처 라벨(`legacy_unknown` 등)과 근거 등급은 승격하지 않는다.

**B8(음식별 통합 집계)이 기댈 수 있는 것**
- FOOD_ID 중심: `food_mentions`(확인된 언급), `item_query_matches`(검색 근거, 언급 아님), `metric_observations`(저장 허용 지표), 병합은 `merged_into`로 따라간다.
- 지표는 출처·단위·비교 맥락(`context_hash`)별이며 출처 간 합산하지 않는다. 실패는 `NOT_OBSERVED`·값 없음, 빈 성공은 관측된 0이다.
- 다시 계산할 범위는 `aggregate_invalidations`(`consumed_at`이 비어 있는 행)에서 시작한다.
- 시간: 순간은 UTC. 음식 일별 분석은 KST 일자로 묶는다. 예산 날짜는 공급자 시간대(YouTube 로스앤젤레스, NAVER KST)를 쓴다.
- 커버리지: 실행 결과 계약의 `result_coverage`(COMPLETE/PARTIAL/LIMITED/NONE)와 `ingest_runs.coverage`를 함께 표시해야 한다.
