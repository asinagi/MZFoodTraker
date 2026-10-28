# B3 신뢰성 있는 실행 전달: 인수인계 완료와 검증

2026-10-28. Codex가 Weekly Limit로 중단한 B3 작업을 로컬 작업 트리 그대로 인수해 검증하고 마무리했다. 기존 구현은 재작성하지 않았고, 감사에서 확인된 결함 1건만 최소 수정했다. 실제 외부 API 호출과 운영 DB 변경은 없다. **Live 실행과 자동 실행은 기본 비활성이며, 이번 검증은 격리 DB와 Mock Transport 범위에 한정된다.**

## 인수 상태

| 항목 | 확인 결과 |
| --- | --- |
| 작업 폴더 | `D:\mz 제조기`, 저장소 루트와 동일. Worktree는 이 폴더 하나뿐 |
| 브랜치/HEAD | `main` / `cca7ee7`. `origin/main`과 동일, 원격 반영 완료 |
| 미커밋 변경 | 수정 16개, 신규 11개 파일 + B3 보고서 산출물. 전부 Codex의 출처 연결·소유권·Mock·UI 작업이며 다른 세션의 변경은 없음 |
| 보존 | 작업 전 미커밋 30개 파일과 tracked diff를 별도 사본으로 보존한 뒤 시작. reset/clean/stash/rebase/force push 미사용 |
| Codex 마지막 검증 | `integration-tests/test-summary.json`에 243개 통과가 남아 있었으나 참고용으로만 보고 전부 재실행 |
| 브라우저 검증 | Codex가 하려던 단계였으나 산출물이 없었음. 이번에 새로 작성·실행 |

## 명세 근거

원본 B3 요청서 파일은 저장소에 없다. 다음을 명세로 사용했다.

- [ROADMAP.md](../integrated-audit-2026-10-26/ROADMAP.md)의 B3 완료 기준: crash/retry/lease 만료 주입, 논리 결과 중복 0, 늦은 owner 쓰기 0, 실패 상태 누락 0, 원격 중복 불명을 숨기지 않음.
- [B3_HANDOFF.md](../development-b1-b2-2026-10-27/B3_HANDOFF.md)의 실행 순서 1~8, 예산 원장, 출처별 의미, 운영 적용·복구.
- 원본 요청서를 보관해 두면 다음 인수 때 대조 근거가 더 분명해진다.

## 요구사항 대조

판정은 코드와 테스트·실행 결과로 했다. 테스트는 모두 격리 DB와 Mock/가상 실행 함수만 사용한다.

| 요구 | 판정 | 코드 | 검증 |
| --- | --- | --- | --- |
| 원자적 Outbox·Job·Lease/Fencing, 동시 claim 1건 | PASS | `execution.py` `enqueue`, `claim`, `_fence` | `test_schedule_creates_atomic_job_outbox_and_advances_due`, `test_worker_claim_is_atomic_across_connections` |
| Lease 만료 후 이전 소유자의 renew·finish·결과 쓰기 차단 | PASS | `_fence`, `RequestSession.request` | `test_lease_expiry_rejects_old_owner_renew_and_finish`, `test_late_source_response_cannot_commit_after_lease_takeover` |
| 실행 직전 generation/food_revision/계정 권한 재확인 | PASS | `begin`, `RequestSession._before` | `test_pause_before_claim_cancels_without_budget_or_http`, `test_policy_change_blocks_before_http`, `test_stopped_then_resumed_target_uses_new_generation_and_preserves_old_job` |
| 요청 전 예산 예약과 시도 기록, ATTEMPTED 자동 환불 금지 | PASS | `quota.py` `reserve`/`mark_attempted`/`release`, `_before` | `test_send_intent_crash_retains_unknown_debit_and_retries_new_reservation`, `test_quota_shortage_sends_no_http_and_waits_for_reset` |
| legacy writer와 B3의 이중 회계·동시 실행 방지 | PASS | `execution_ownership.py`, 세 서비스의 게이트, 어댑터의 no-op reserve/finish | `test_ownership_is_read_only_and_default_legacy`, `test_live_execution_default_disabled_and_rss_remains_legacy_owned` |
| 각 HTTP/페이지/재시도를 실제 호출로 기록, 상한을 실제 횟수로 표시하지 않음 | PASS | `execution_calls`, `RequestSession` | 시나리오: 예약 6건 대비 Mock HTTP 11회가 별도로 기록됨 |
| 페이지별 저장·호출 완료·체크포인트의 단일 fenced 트랜잭션 | PASS | `RequestSession.request` | `test_page_commit_then_crash_reuses_checkpoint_without_http_or_quota`, `test_database_commit_failure_rolls_back_page_then_recovers`, `test_partial_page_failure_recovers_from_committed_cursor_without_repeating_page` |
| 논리 결과 중복 0, 완료 작업 재전달 시 추가 HTTP 0 | PASS | `execution_item_receipts`, `ExecutionWorker.run_once` | `test_response_crash_then_retry_has_one_durable_source_item`, `test_duplicate_video_pages_have_one_item_and_one_job_receipt`, 시나리오 `duplicate_http_attempts=0` |
| 늦은 응답에서 유효 대상 교집합만 반영, 공유 요청의 다른 대상 유지 | PASS | `_eligible(started_only=True)` | `test_shared_query_stop_one_target_keeps_other_result_mapping`, `test_shared_actual_query_searches_once_and_filters_stopped_target` |
| 성공·부분·실패·0건·원격 차감 불명 구분 | PASS | `finish`, `fail` | `test_normal_zero_results_succeeds_and_creates_no_video`, `test_retry_exhaustion_and_unknown_network_are_not_success`, `test_partial_result_has_contract_when_retries_exhausted` |
| 원격 중복 불명을 숨기지 않음 | **FAIL → 수정 후 PASS** | `claim` | 아래 "수정한 결함" |
| Retry-After를 줄이지 않음, 인증/권한 오류는 계정 범위 차단 | PASS | `fail`, `retry_after.py` | `test_retry_after_is_not_clamped`, `test_authentication_blocks_entire_account`, `test_existing_clients_honor_retry_after_without_internal_b3_retry` |
| NAVER 응답·ratio 비영속 | PASS (Mock) | `NaverExecutor` | `test_naver_ratio_body_and_series_never_persist` |
| YouTube 29일 TTL·검색 근거 보존, 음식 추출 off | PASS (Mock) | `YouTubeExecutor`, `youtube_storage.save_videos` | `test_youtube_public_snapshot_ttl_and_unverified_query_evidence` |
| Instagram 승인된 태그만, 관련 태그 추적 없음, 페이지 상한은 LIMITED | PASS (Mock) | `InstagramExecutor` | `test_instagram_page_cap_and_no_related_tag_tracking`, `test_limited_page_preserves_results_and_checkpoints` |
| 체크포인트 7일 만료, 만료 후 재생 금지, 오류 문구·자격 증명 비영속 | PASS | `execution_maintenance.purge`, `RequestSession` | `test_expired_checkpoint_does_not_restore_api_data`, `test_explicit_expiry_maintenance_removes_ids_and_keeps_nonreplayable_tombstone`, `test_transport_error_text_and_credentials_are_not_in_execution_state` |
| RSS는 기존 스케줄러 소유, A4 bridge 실패는 로컬 재처리 | PASS | `_gate`, `ExecutionController.recover_rss` | `test_rss_identity_bridge_failure_is_recoverable_without_fetch_or_original_changes` |
| 기본 비활성, HTTP로 Live·정책 권한 부여 불가 | PASS | `ExecutionSettings`, `execution_http.py`, `live_controller` | `test_default_live_runtime_is_closed`, `test_http_cannot_grant_live_or_policy_permissions`, 브라우저에서 Live 모드 미노출 |
| 실제 Live 호출, 공급자 실제 quota·권한 확인 | NOT VERIFIED (BLOCKED) | `live_controller` | 별도 운영 승인 대상. 이번에 수행하지 않음 |

## 수정한 결함

**마지막 시도가 전송 의도 직후 중단되면 원격 중복 불명이 사라졌다.** `claim`의 최대 시도 초과 종결 분기가 `SENT`→`UNKNOWN` 전환보다 먼저 실행되어, 작업이 `FAILED/MAX_ATTEMPTS`로 끝나도 호출은 `SENT`로 남고 `remote_duplicate_possible=0`이었다. 재현은 두 경우였다: `max_attempts=1`에서 첫 시도 중단, 기본 3회에서 앞의 두 시도가 깨끗하게 실패한 뒤 마지막 시도만 중단. 수정 전 둘 다 불명이 숨겨지는 것을 확인했고, 수정 후 호출은 `UNKNOWN`, 작업은 `remote_duplicate_possible=1`이 된다. 전환 블록을 종결 분기 앞으로 옮기는 변경이며 이 시점에는 이전 소유자의 lease가 이미 만료돼 있다.

- 수정: `food_trends/execution.py` `claim`
- 회귀 테스트 2개: `tests/test_execution.py`의 `test_interrupted_send_on_final_attempt_is_not_hidden_when_job_fails`, `test_final_attempt_crash_after_clean_failures_keeps_uncertainty`
- 추가 파일: `tests/execution_browser_check.py` (B3 화면 검증. 결과를 B3 보고서 폴더에만 기록)

## 검증 결과

| 검증 | 결과 |
| --- | --- |
| 인수 직후 전체 회귀 (수정 전) | 243개 통과, 실패/오류/skip 0, 외부 소켓 시도 0 |
| 수정 후 최종 전체 회귀 | 245개 통과, 실패/오류/skip 0, 외부 소켓 시도 0, 운영 DB 접근 차단. [summary](final-tests/test-summary.json) |
| 테스트 구성 | 기존 199 + 인프라 18 + 출처 연결 26 = 243, 이번 회귀 2 = 245 |
| Mock 통합 시나리오 | 음식 0건→후보→승인→3개 출처 대상→계획→Outbox 6개 작업→Mock HTTP 11회→결과 저장. 실제 HTTP 0, 미리보기 DB 변경 0, 완료 작업 재전달 시 추가 HTTP 0, RSS FOOD 0 유지. [acceptance.json](acceptance.json) |
| 복제 DB 0→7 | 보존 스냅샷의 임시 사본에서 수행. 원래 30개 테이블의 열·행 해시 일치, FK 오류 0, integrity ok, legacy 승격 0, 재적용 0건. 스냅샷 SHA-256이 전후 동일. [copy-migration.json](copy-migration.json) |
| 미이관 DB에 새 코드 기동 | v0 사본에서 `MigrationRequired`로 중단되어 명시적 `--migrate` 전에는 동작하지 않음 |
| B3 브라우저 (Chrome headless) | 작업 6개 등록 시 HTTP 0, 버튼 실행 후 6개 SUCCEEDED·Mock HTTP 11회, 완료·새로고침·재등록 후 추가 HTTP 0, 실행 버튼 0, Live 모드 미노출, 자동 실행 꺼짐, JS 오류 0, 외부 요청 0, 차단된 POST 0. [결과](browser-summary.json), [모바일](execution-mobile.png) |
| 기존 화면 회귀 | A4/A5 음식 등록, B1 추적 관리, B2 계획 미리보기 모두 통과 (JS 오류 0, 외부 요청 0). 출력은 기존 보고서 폴더가 아닌 별도 위치에 기록 |
| 정적 검사 | `node --check`(execution.js, tracking.js), `py_compile`, `git diff --check` 통과 |
| 민감정보 | `.env`의 비밀값 8개가 커밋 대상 파일에 0건. 패턴 일치 7건은 QUERY ID의 sha256과 "자격 증명이 저장되지 않음"을 검증하는 합성 문자열이었음 |

테스트 통과는 Mock 환경의 논리·재시작 복구 검증이며 공급자의 실제 응답·권한·quota 동작을 보증하지 않는다.

## 운영 보호

- 운영 DB `data/trends.sqlite3`는 읽기 전용(mode=ro)으로만 열었다. 스키마 v0, 30개 테이블, `schema_migrations` 없음, integrity ok. 마이그레이션·쓰기·삭제 없음.
- 8000/8002 listener(PID 50584/51280)는 그대로 실행 중이며 중지·재시작하지 않았다. 세션 중 운영 DB 행 수가 Codex 기준선보다 늘었는데(예: runs 87→90), 이는 기존 서버의 정기 수집이며 개발 경로의 쓰기가 아니다.
- 테스트와 브라우저는 임시 DB, 임시 Chrome 프로필, 루프백 포트만 사용했다. `.env`를 복사·출력하지 않았다.
- 실행기는 `live_enabled=false`, `automatic=false`가 기본이고 Live에는 출처 승인 registry, `B3_LIVE_ENABLED`, 출처별 소유권 이전이 모두 필요하다.

## 운영 적용 전 필요한 승인 (이번에 수행하지 않음)

1. writer 목록과 활동 확인, 승인된 유지보수 창에서 최신 online backup.
2. 복제본 0→7 재검증 후 명시적 `--migrate`와 코드 전환.
3. 출처별 기존 writer 중지와 `execution_ownership.transfer` 승인, 한 출처씩 소량 Live 실행·계측.
4. 공급자 실제 quota·권한 확인 (`provider_observed_usage`는 계속 null).

## 후속 작업 (B3 범위 밖, 임의 구현하지 않음)

| 항목 | 내용 |
| --- | --- |
| 충돌 종료된 작업의 부분 결과 | 페이지를 커밋한 뒤 중단이 반복돼 `claim`이 최대 시도 초과로 종결하면 `FAILED/MAX_ATTEMPTS`가 되고 결과 계약·대상 매핑이 없다. 정상 오류 경로(`fail`)는 PARTIAL을 확정한다. 저장된 항목은 기존 출처 테이블에 남으며 B4/B5의 부분·실패 구분에서 정책을 정해야 한다 |
| Live Instagram 재개 | 재시도마다 `recently_searched_hashtags` 사전 확인(최대 3회 HTTP)을 체크포인트와 무관하게 반복한다. 호출은 기록·예약되지만 Live 활성화 전에 재개 시 생략 여부를 정해야 한다. Mock으로만 검증됨 |
| Live 운영 절차 | `live_controller`는 Python 진입점이며 CLI/배포 절차에 연결돼 있지 않다 |
| 실행 API 인증 | `/api/execution/*` POST는 로컬 호스트·same-origin·크기 제한만 확인한다. `actor_ref`는 신원 인증이 아니다 (B1과 동일한 한계) |
| 원본 요청서 | 저장소에 없어 ROADMAP과 인수 계약으로 대조했다 |
