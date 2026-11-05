# ORACLE-WRITER-RELEASE-1 Oracle Live Writer 이전 철회와 안전한 복귀

2026-11-05. 기준 `main` `1b848f9`에서 작업 브랜치 `feat/oracle-writer-release`로 진행했다. **실제 외부 API 호출 0건, 운영 SQLite·운영 서버 변경 0건, 운영 Oracle 없음, 기존 SQLite 수집기 재활성화 없음.** 모든 검증은 기존 수집기의 네트워크 계층을 기록용 가짜(LiveWire)로 바꿔 SQLite 대역과 개발용 Oracle(MZ_TEST)에서 수행했다. G-OPS-1~6 승인 상태는 바꾸지 않았다.

## 결론 요약

| 질문 | 답 |
| --- | --- |
| 완료 기준("수집원 Writer를 Oracle로 이전했다가 안전하게 철회할 수 있고, 그 과정에서 중복 수집·예산 초과·데이터 손실을 방지하는 기능이 실제 Oracle에서 검증된 상태")을 충족했는가 | **개발·격리 검증 범위에서 충족.** 철회는 새 요청을 즉시 막고, 진행 중 요청이 끝날 때까지 기다린 뒤(DRAINING), 남은 작업을 `WRITER_RELEASED`로 닫고 계정을 `IMPORTED`로 되돌린다. Oracle 기간 사용량은 레거시 호출 기록으로 인계되어 기존 SQLite 수집기의 예산 코드가 그대로 센다. 인계 전·창 종료 전에는 재개 판정이 거부한다. RSS는 중복 수집기 실행이 막히고, 기존 역방향 복구로 데이터가 돌아간다. 모두 실제 Oracle에서 검증했다(§5). |
| 실제 Live 수집을 했는가 | **아니다.** 실제 호출은 별도 승인 대상이다. |
| Live 소량 검증의 선행 개발 조건(R1~R9)은 해결됐는가 | **해결(READY).** [요청서](../development-oracle-live-integration-2026-11-05/LIVE_VERIFICATION_REQUEST.md)를 갱신했다. 요청서 자체는 승인 대기(REQUIRES_APPROVAL) |
| 운영 전환이 가능한가 | **아니다(BLOCKED).** 운영 Oracle은 코드에서 계속 거부되며 G-OPS 승인도 없다(§10). |

## 1. Writer 이전 철회 구현 결과

### 계정 상태와 명령

| 상태 | 대상 표현 | 의미 |
| --- | --- | --- |
| `LEGACY` | 이전 이력 없음 | 기존 SQLite 수집기가 Writer |
| `ORACLE` | `quota_accounts.usage_status='EXCLUSIVE'`, `release_requested_at` 없음 | Oracle 런타임이 단독 Writer |
| `RELEASING` | `EXCLUSIVE` + `release_requested_at`(또는 철회 명령 없이 `IMPORTED`가 된 이전 주기) | 철회 시작: 새 요청 즉시 중단, 진행 중 요청 종료 대기 |
| `RELEASED` | `IMPORTED` + 근거 `writer_release:<승인자>` + `RELEASED` 이벤트 | Oracle은 더 이상 Writer가 아님. 레거시 재개는 재개 판정 뒤 |

| 명령 | 역할 |
| --- | --- |
| `--backend oracle-test --writer-release <source> --execution-registry <registry> [--release-wait 0~600]` | 철회. 결과 `RELEASED` 또는 `DRAINING`(종료 코드 2, 다시 실행) |
| `--backend oracle-test --writer-status` | 수집원별 상태·이전/철회 이력·열린 작업·Lease·미완료 호출·인계할 사용량(읽기 전용) |
| `--writer-handover <source> --recovery-target oracle\|sqlite [--allow-operational-database]` | Oracle 기간 사용량을 설정된 SQLite DB의 레거시 호출 기록으로 인계 |
| `--legacy-resume-check <source> --recovery-target oracle\|sqlite` | 기존 SQLite 수집기를 다시 켜도 되는지 판정(읽기 전용, 아니면 종료 코드 2) |

### 철회 단계 (`oracle_live.release_writer`)

1. **요청**: 계정 행을 잠근 트랜잭션에서 `release_requested_at`을 기록하고 `RELEASE_REQUESTED` 이벤트를 추가한다. 이 커밋부터 그 계정의 큐 등록·작업 시작·요청·RSS 수집이 시작되지 않는다(게이트 사유 `WRITER_RELEASE_IN_PROGRESS`).
2. **대기**: 그 계정의 Live 작업 중 살아 있는 Lease가 있으면(요청이 진행 중일 수 있음) 아무것도 쓰지 않고 `DRAINING`을 돌려준다. RSS는 10분 이내에 시작한 미완료 회차가 있으면 같다. 10분이 지난 회차는 먼저 중단(`interrupted`)으로 마감한다.
3. **철회**: 계정 행 잠금 아래 한 트랜잭션에서 남은 Live 작업을 `CANCELLED`/`WRITER_RELEASED`로 닫는다. 계정은 `IMPORTED`로 돌린다(근거 `writer_release:<승인자>`, `legacy_observed_through`는 마지막 가져오기 그대로라 재이전에는 새 가져오기가 필요). 열린 창 사용량을 요약한 `RELEASED` 이벤트도 함께 쓴다.

안전장치: registry 승인(없으면 403), 운영 대상 거부(403, 소유권 잠금도 거부), Shadow 소유 중 거부, 대상 스키마 v10 필요, HTTP 없음. 철회에는 Live 스위치가 필요 없다(멈추는 동작이므로). 이전(`--writer-transfer`)도 이제 `TRANSFER` 이벤트(새 주기)를 남긴다. 이미 이전된 계정을 다시 이전하면 변경이 없고, 철회 중인 계정의 이전은 409로 거부한다.

### 이력 (D)

대상 v10 `writer_transfer_events`(추가 전용, 수정·삭제 없음): `cycle_no`, `action`(`TRANSFER`·`RELEASE_REQUESTED`·`RELEASED`·`USAGE_HANDOVER`), 시각, 승인자, 전후 상태, 상세(닫은 작업·중단 호출·열린 창 사용량). 이벤트 id는 결정적이라 같은 단계를 다시 실행해도 행이 늘지 않는다. registry의 `evidence` 원문은 기록하지 않는다(승인자만). 역방향 복구에서는 복구하지 않는 표로 보고한다(사용량 자체는 인계로 SQLite에 감).

## 2. 실행 중 작업·Lease 처리 결과

| 상황 | 처리 | 검증 |
| --- | --- | --- |
| 대기 작업(`READY`·`RETRY_WAIT`) | 철회 단계에서 `CANCELLED`/`WRITER_RELEASED`, 슬롯도 같은 사유로 취소, 보내지 않은 예약은 `RELEASED` | `test_release_stops_the_account_at_once_and_keeps_the_other_writers` |
| 철회 표시 뒤에 잡힌 작업 | 시작 직전 게이트(`begin`)에서 `CANCELLED`/`WRITER_RELEASED`, 요청 0 | 이 경로만 따로 검증한 테스트는 없음. 같은 게이트를 큐 등록 거부(§6)와 요청 직전 차단(아래 진행 중 요청 테스트)으로 검증 |
| 요청이 진행 중인 작업 | 철회는 `DRAINING`. 이미 보낸 요청은 응답을 저장(커밋 데이터 유지), 다음 요청 직전에 멈춰 `CANCELLED`/`WRITER_RELEASED`. 다시 실행하면 `RELEASED` | `test_a_request_in_flight_lands_and_holds_the_release_until_its_job_ends` |
| 요청 후 죽은 Worker(Lease 유효) | `DRAINING` | `test_a_crashed_worker_holds_the_release_only_until_its_lease_ends` |
| Lease 만료 후 | `SENT` 호출을 `UNKNOWN`(`INTERRUPTED_AFTER_SEND`)으로, 원격 중복 가능 표시, 작업 `CANCELLED`/`WRITER_RELEASED`. 그 호출은 사용량으로 계속 집계·인계 | 같은 테스트 |
| 철회와 동시에 들어온 Live 큐 등록 | 철회가 잡은 계정 잠금을 기다린 뒤 `BLOCKED`(`WRITER_RELEASED`), 작업 미생성 | `test_an_enqueue_racing_a_release_waits_for_it_and_is_refused` |

경쟁 차단: 철회는 계정 행 → 작업 행 순서로 잠근다(RSS는 계정 행 → `rss_runs`). 런타임은 큐 등록에서 계획 → 대상 → 계정 행, 요청 직전에서 작업 → Lease → 계정 행 순서로 잠근 뒤 게이트를 읽는다. 순환이 없어 교착이 없고, 게이트와 호출 기록 커밋 사이에 철회가 끼어들 수 없다. 철회는 살아 있는 Lease를 먼저 확인한 뒤에만 작업 행을 잠근다. 대역은 쓰기 트랜잭션이 직렬화된다. 네트워크 요청 중에는 여전히 DB 트랜잭션이 없다. 철회의 대기도 트랜잭션 밖에서 한다.

## 3. Instagram·YouTube·NAVER 사용량 인계 결과

기존 SQLite 수집기는 자기 호출 기록만으로 예산을 센다(YouTube `youtube_calls` 태평양 시간 일일, NAVER `naver_calls` KST 일일, Instagram `instagram_searches` 7일 고유 태그. SQLite B3 런타임도 같은 표를 읽음). 그래서 Oracle 기간 사용량을 그 표에 넣는다(`usage_handover.handover`).

| 수집원 | 인계 단위 | SQLite 행 | 표시 |
| --- | --- | --- | --- |
| YouTube | Oracle Live 호출 1건(search·videos) | `youtube_calls`: 시작 시각의 태평양 날짜, 엔드포인트, 상태(성공 `success`, 실패는 오류 종류, 결과 불명은 `unknown`), origin `live_api` | `run_id='oracle:<call id>'` |
| NAVER | Oracle Live 요청 1건 | `naver_calls`: KST 날짜, 요청 본문 `{}`·후보 `[]`(검색어·값 복사 안 함), origin `live_api` | `fingerprint='oracle:<call id>'` |
| Instagram | Oracle이 새로 청구한 해시태그 1개(비용 > 0) | `instagram_searches`: 사용자, 이름(NFC), 검색 시각은 조회 호출의 가장 늦은 시각이라 창이 일찍 끝나지 않음 | 사용자·이름·시각, 같은 태그의 7일 내 기존 행이 있으면 생략 |

- 열린 창의 사용량만 인계한다(끝난 창은 예산에 영향 없음). 재실행 시 0건. SQLite 쓰기는 한 트랜잭션이라 중간 실패 시 0건이다. 인계 기록은 대상에 `USAGE_HANDOVER` 이벤트로 남는다.
- 적게 세지 않음: 결과 불명 호출도 1건으로 센다(`unknown`). `PROVIDER_CONFIRMED`는 만들지 않는다.
- 검증(`test_handed_over_usage_keeps_the_legacy_collectors_within_their_budgets`): 인계 후 **기존 코드 그대로** 다음이 확인됐다. 인계 전 사본에서는 같은 요청이 통과한다(= 인계가 없으면 보냈을 요청).
  - YouTube: `YouTubeStore.reserve`가 일일 검색 상한 1에서 `budget`으로 거부.
  - NAVER: `NaverService.status()`의 당일 요청 수가 1.
  - Instagram: `InstagramService.budget()`의 7일 고유 태그가 1이고, 소프트 상한 1에서 새 태그 `reserve`가 `budget`으로 거부.
- 전달 불가 시 Fail-closed(`test_usage_not_in_sqlite_keeps_the_legacy_collector_off_until_its_window_ends`). 창이 끝나면 열린 사용량이 없어져 `ready`가 된다.
  - 인계 전: 재개 판정이 `ORACLE_USAGE_NOT_HANDED_OVER`와 `resume_not_before`(창 종료 시각)로 거부.
  - v11이 아닌 DB: 409, 0건.
  - 레거시 수집기 자신의 사용량이 빠진 DB(더 오래된 복원본): Oracle 행은 넣지만 `DESTINATION_MISSING_LEGACY_USAGE`로 같은 시각까지 거부. 대상이 가져온 레거시 행을 id와 원래 시각으로 대조한다.
- 이중 Writer 감지: Oracle 기간(이전~철회)에 레거시 수집기가 쓴 행 수를 `legacy_writes_during_oracle_period`로 보고한다(병합하지 않음).

## 4. RSS 수집기 중복 방지 결과

`test_rss_release_waits_for_a_run_in_flight_and_then_nothing_fetches_the_feed`:

- 다른 프로세스의 미완료 회차가 있으면 철회는 `DRAINING`. 그동안 새 수집은 `oracle_live_rss_blocked`(`WRITER_RELEASE_IN_PROGRESS`)로 요청 없이 거부된다.
- 철회 후에는 게이트, 예약 실행 시작, RSS 잠금 아래 회차 시작(`Blocked`)이 모두 거부한다. 피드 요청 수는 철회 이후 그대로였다.
- 기존 SQLite RSS 스케줄러 재개 판정은 Oracle 회차가 SQLite에 복구되기 전까지 `ORACLE_RSS_NOT_RECOVERED`(번호 충돌 방지), 미완료 Oracle 회차가 있으면 `ORACLE_RSS_RUN_UNFINISHED`로 거부한다.

## 5. 실제 Oracle 테스트 결과

| 실행 | 결과 |
| --- | --- |
| 기본 환경(Oracle 컨테이너 정지, 외부 소켓·운영 DB 차단) | **524개 통과, 실패 0, 오류 0, Oracle 187개 skip**(383초, 외부 소켓 차단 시도 0) |
| 실제 Oracle 포함 전체(MZ_TEST) 1차 — 중단됨 | 711개 중 710개 통과, 실패 0, **오류 1**(14,538초). 오류는 `test_oracle_scheduling`의 Oracle 사례 1개(`database_error`, SHADOW 임대 획득 중) |
| 실제 Oracle 포함 전체(MZ_TEST) 2차 — 재실행 | **711개 통과, 실패 0, 오류 0, skip 0**(1,352초, 외부 소켓 차단 시도 0) |
| 전체 테스트 수 | **711개**(= 기본 524 + Oracle 전용 187 = Oracle 전체 711). 작업 전 691개(+20: 신규 10개 사례 × 대역·MZ_TEST) |
| 신규 `tests/test_writer_release.py` | 10개 사례 × (대역 + MZ_TEST) = 20개 |

**1차 실행의 중단 원인은 코드가 아니라 PC 절전이다.** Windows 시스템 로그(Kernel-Power 이벤트 506/507)에 따르면 PC가 2026-11-05 23:33:13부터 2026-11-06 03:10:58까지 Modern Standby에 들어가 테스트 프로세스와 WSL2의 Oracle 컨테이너가 함께 멈췄다(실행 로그의 공백 23:29:46~03:11:23과 일치). 깨어난 직후 진행 중이던 그 사례의 Oracle 연결이 끊겨 오류가 났고, 이후 사례는 모두 통과했다. 같은 사례의 대역 실행은 통과했다. 요구된 "실제 Oracle 전체 회귀"를 온전한 결과로 남기려고, 테스트 실행 동안만 유휴 절전 진입을 막는 래퍼(`SetThreadExecutionState`, 프로세스 종료와 함께 해제)로 전체를 한 번 더 실행했다(2차). 두 실행의 요약을 모두 남긴다.

기존 691개는 수정·삭제하지 않았다(기존 테스트 파일 변경 0). 결과 원본: [기본](default-tests/test-summary.json), [Oracle 포함 2차](oracle-full-tests/test-summary.json), [Oracle 포함 1차(중단)](oracle-full-tests-interrupted/test-summary.json).

## 6. 철회 후 Oracle HTTP 요청 0건 검증

| 지점 | 결과 |
| --- | --- |
| 철회 직후 같은 계획의 Live 큐 등록 | `BLOCKED`(`WRITER_RELEASED`), 작업 미생성 |
| 철회로 닫힌 작업 | 다시 잡히지 않음, 호출 기록 0 |
| 진행 중이던 작업의 다음 요청 | 보내지 않음(가짜 전송 호출 기록에 `videos` 없음) |
| Lease 만료 Worker | 철회 후 그 작업의 새 요청 없음 |
| RSS | 수집·예약·회차 시작 모두 거부, 피드 요청 수 불변 |
| 철회와 경합한 큐 등록 | 대기 후 거부, 가짜 전송 호출 0 |
| 다른 수집원 | 계속 Oracle Writer, 요청 정상(NAVER 성공) |

가짜 전송은 모든 요청을 기록하며 실제 네트워크로 보내지 않는다. 테스트 실행기는 루프백 외 소켓을 거부했고 차단 시도는 0건이다.

## 7. Oracle → SQLite 복구 연계 결과

`test_released_sources_return_to_sqlite_with_their_usage_and_their_data`:

1. Oracle에서 RSS·Instagram·YouTube·NAVER를 Live 경로로 수집하고, 런타임 프로세스를 멈춘다.
2. 네 수집원을 철회한다. 재개 판정은 API 수집원 `ORACLE_USAGE_NOT_HANDED_OVER`, RSS `ORACLE_RSS_NOT_RECOVERED`로 거부한다.
3. 사용량을 인계하고(대상 소유권 `IDLE` 확인) 같은 SQLite 파일로 기존 `recover`를 실행한다.
   - 수렴, integrity·FK 정상, 재실행 쓰기 0, 인계 재실행 0건.
   - 인계 행과 복구된 B3 호출 행이 함께 있고, RSS 회차 수가 대상과 같다.
   - `writer_transfer_events` 15행(4 × 3단계 + 인계 3)은 복구하지 않는 표로 보고된다.
4. 재개 판정이 네 수집원 모두 `ready: true`.

운영 순서(수집원마다): `--writer-release` → (DRAINING이면 다시) → `--writer-handover` → `--recovery plan` → `--recovery apply`(운영 DB는 G-OPS 승인과 `--allow-operational-database`) → `--legacy-resume-check`가 `ready`일 때만 `*_ENABLED=true` 또는 SQLite RSS 스케줄러 재개. 이번 작업은 재개를 수행하지 않았다.

## 8. 운영 DB 보존 검증

읽기 전용(`mode=ro`, `query_only`)으로 확인했고, 직전 기록(ORACLE-LIVE-INTEGRATION-1)과 모든 값이 같다([production-final.json](production-final.json)).

| 항목 | 값 |
| --- | --- |
| SHA-256 | `38b880f535dded56e84a11570a1f7793bed1668580950172a417687b689548c0`(변화 없음) |
| 스키마 버전 | 0(`schema_migrations` 없음) |
| 표·행 | 30개·7,701행 |
| integrity_check | ok |
| WAL | 0바이트 |
| 본 파일 수정 시각 | 2026-10-28 14:20:01(변화 없음, 점검 전후 동일) |
| 개발 포트 | 8000/8002/8010/8020/8030/8031/1521 모두 미점유(컨테이너 정지 후) |

테스트 실행기는 운영 파일에 대한 연결을 거부했고(`production_database: blocked`), 인계·복구 테스트는 모두 임시 파일에만 썼다. 운영 파일과 같은 이름의 거부 확인용 파일도 임시 디렉터리의 사본이었다(쓰기 0). MZ_TEST는 마지막에 초기화했고(표 0개) Oracle 컨테이너는 정지했다.

## 9. Git Commit 및 최종 HEAD

작업 브랜치 `feat/oracle-writer-release`(모두 원격 push):

| 커밋 | 내용 |
| --- | --- |
| `3b8c7cb` | 철회·인계·재개 판정 구현과 테스트(대상 v10) |
| `471f56f` | 보고: 보고서, 요청서 갱신, README, 테스트 증거 |

`main` 통합은 아래 통합 기록에 남긴다(fast-forward만, Force Push·Reset Hard 없음).

## 10. 남은 운영 차단 조건

| 항목 | 판정 | 내용 |
| --- | --- | --- |
| Writer 이전 철회·사용량 인계·재개 판정 | **READY** | §1~§7, 실제 Oracle 검증 |
| 실제 Live 소량 검증(RSS·Instagram·YouTube·NAVER) | **REQUIRES_APPROVAL** | [요청서](../development-oracle-live-integration-2026-11-05/LIVE_VERIFICATION_REQUEST.md) 승인(G-OPS-4/5), 실제 자격 증명, 실제 근거가 담긴 registry |
| 운영 Oracle 전환 | **BLOCKED** | `RUNTIME_PRODUCTION`·`oracle-production`·운영 대상 임대가 코드에서 거부됨. 잠금 해제 경로 개발·검증과 G-OPS-1~6 승인 필요 |
| 운영 SQLite | **REQUIRES_APPROVAL** | 운영 DB는 v0. 인계·복구 대상은 v11이어야 하므로 G-OPS-3 마이그레이션 뒤에만 가능. 운영 DB 쓰기는 `--allow-operational-database`(승인 후)만 |
| 레거시 수집기 재개 | 절차 조건 | 기존 수집기 코드는 재개 판정을 스스로 읽지 않는다. 운영자가 `--legacy-resume-check`가 `ready`일 때만 `*_ENABLED`를 켠다(인계된 사용량은 기존 예산 코드가 자동으로 셈) |

정책상 한계(안전 측):

- 리스가 끝난 뒤에야 깨어나 요청을 보내는 멈춘 Worker는 막을 수 없다(펜싱의 한계). 그 호출은 `UNKNOWN`으로 집계·인계되지만 실제 전송 시각이 늦으면 Instagram 7일 창이 로컬에서 조금 일찍 끝날 수 있다. 로컬 소프트 상한 28과 제공자 상한 30 사이의 여유가 이를 흡수한다.
- Oracle에는 해시태그 ID 이력이 없어 인계 행의 `instagram_id`가 비어 있다. 레거시 예산이 원격 ID와 이름을 따로 셀 수 있다(과대 집계, 안전 측).
- 복구 뒤 SQLite B3 런타임은 같은 Oracle 호출을 인계 행과 복구된 B3 청구로 두 번 셀 수 있다(YouTube·NAVER, 창 종료까지 과대 집계).
- 이전 후 사용량 가져오기(`--target-load`)는 기존 동작대로 계정을 `IMPORTED`로 되돌린다. 이때 상태는 `RELEASING`으로 보이고 Live는 막힌다. 작업 정리와 이력을 위해 철회 명령을 실행해야 하며, 재개 판정도 그때까지 거부한다.
- 철회에는 registry 승인이 필요하다. 승인 파일이 없는 긴급 상황에서는 `B3_LIVE_ENABLED=false`나 프로세스 정지로 Live를 멈출 수 있다(게이트가 막음). 정리는 승인 뒤 철회 명령으로 한다.
- 기존 대상은 v10이 필요하다(`--target-migrate`). 런타임은 버전이 다르면 시작하지 않는다.

## 11. 수정·추가 파일

| 파일 | 변경 |
| --- | --- |
| `food_trends/oracle_live.py` | `release_writer`·`writer_status`·`writer_state`, 이전 이력·재이전 규칙, 준비 점검의 철회 사유 |
| `food_trends/usage_handover.py` (신규) | Oracle 기간 사용량 인계, 레거시 재개 판정 |
| `food_trends/oracle_execution.py` | 철회 사유(`WRITER_RELEASE_IN_PROGRESS`·`WRITER_RELEASED`), 큐 등록·요청 직전 계정 행 잠금, 진행 중 요청의 결과 저장 허용, `writer_released` 종료, `close_released_account` |
| `food_trends/oracle_rss.py` | Live 회차 시작 시 RSS 잠금 아래 Writer 재확인(`Blocked`) |
| `food_trends/target_schema.py` | 대상 v10: `quota_accounts.release_requested_at`, `writer_transfer_events` |
| `food_trends/reverse_recovery.py` | `writer_transfer_events`를 복구하지 않는 표로 보고 |
| `food_trends/__main__.py` | `--writer-release`·`--release-wait`·`--writer-status`·`--writer-handover`·`--legacy-resume-check` |
| `tests/test_writer_release.py` (신규) | 검증 |
| `README.md`, 이 디렉터리, [요청서](../development-oracle-live-integration-2026-11-05/LIVE_VERIFICATION_REQUEST.md) | 실행 방법·현황·판정 |

## 통합 기록

`main`을 `1b848f9`에서 `471f56f`(보고 커밋, 위 구현 커밋 포함)로 fast-forward하고 `origin/main`에 push했다. 이 기록 커밋도 같은 방식으로 `main`에 fast-forward했다. 강제 push·reset hard·기존 작업 삭제는 없었다. 병합 직전에 다시 가져온 `origin/main`은 기준 `1b848f9` 그대로였다.
