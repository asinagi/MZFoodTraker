# ORACLE-LIVE-INTEGRATION-1 Oracle Live 수집 연결·RSS 역방향 복구·개발 환경 정리

2026-11-05. 기준 `main` `2ff5b7a`에서 작업 브랜치 `feat/oracle-live-integration`으로 진행했다. **실제 외부 API 호출 0건, 운영 SQLite·운영 서버 변경 0건, 운영 Oracle 없음.** Live 경로는 구현했지만 기본 비활성이며, 모든 검증은 기존 수집기의 네트워크 계층을 기록용 가짜(LiveWire)로 바꿔 수행했다. G-OPS-1~6은 모두 승인 대기 그대로다.

## 결론 요약

| 질문 | 답 |
| --- | --- |
| 최종 완료 기준(“Oracle이 발굴·승인·추적·수집·정제·분석의 모든 데이터를 관리하고, 기존 수집기를 이용한 Live 경로까지 개발·격리 검증됐으며, Oracle에서 새로 수집한 RSS를 안전하게 복구할 수 있는 상태”)을 충족했는가 | **개발·격리 검증 범위에서 충족.** Live 경로로 RSS → 승인 → 추적 → 수집(Instagram·YouTube·NAVER) → 정제 → B8을 실제 Oracle(MZ_TEST)에서 SQLite 연결 0건으로 실행했고, 이어서 Oracle 데이터 전체(RSS 발굴 포함)를 새 SQLite로 복구해 B8 응답이 같고 재실행 쓰기 0임을 확인했다(§3, §7). |
| 실제 Live 수집을 했는가 | **아니다.** 실제 호출은 별도 승인 대상이다([요청서](LIVE_VERIFICATION_REQUEST.md)). |
| 운영 전환이 가능한가 | **아니다(BLOCKED).** 운영 Oracle은 코드에서 계속 거부되며(§14), G-OPS 승인도 없다. |

## 1. 작업 전후 Git HEAD

| 시점 | 상태 |
| --- | --- |
| 시작 | `main` = `origin/main` = `2ff5b7a`, 미커밋 없음, worktree 1개. 다른 Claude Code 세션 프로세스 1개가 떠 있었으나 이 저장소에 쓰지 않았음. Oracle 컨테이너 정지(MZ_TEST 미사용), 개발 포트(8000/8002/8010/8020/8030/8031/1521) 점유 없음 |
| 종료 | 작업 브랜치의 모든 커밋을 원격에 push한 뒤 `main`으로 fast-forward(§13·통합 기록). 미커밋 없음, MZ_TEST 비움(표 0개), Oracle 컨테이너 정지 |

## 2. 정리한 임시 폴더 (Phase 0)

[CLEANUP.md](CLEANUP.md). `D:\Users\gjgjs\AppData\Local\Temp\claude\…\scratchpad\browser1`까지 빈 디렉터리 9개만 비재귀로 하나씩 삭제했다. 9개 모두 파일 0개, 이전 화면 점검 시각(19:45:52)에 생성됐다. 원래 결과 파일 2개는 이미 세션 scratchpad에 있고 보고용 사본은 이전 보고 디렉터리에 커밋돼 있다. `D:\Users` 자체(같은 시각에 생성된 빈 디렉터리)와 `data\b8-app-api-worktree-logs`는 보존했다.

## 3. Oracle RSS 역방향 복구 결과 (Phase 1)

`reverse_recovery.py`가 Oracle에서 수집한 RSS 발굴 기록을 손실로만 보고하던 것을 **SQLite v11 발굴 저장소로 되돌리도록** 바꿨다. 식별 테이블보다 먼저 쓴다(`food_candidates.source_item_id`가 `items`를 참조하므로).

| Oracle v9 | SQLite v11 | 규칙 |
| --- | --- | --- |
| `source_contents` `items:N` | `items` (id = N) | 번호·`source_id`·첫/마지막 발견 시각 보존, payload는 마지막 관측의 원래 텍스트 |
| `rss_runs` | `runs` (id = run_id) | 시각·성공·오류·건수·origin·계약 보존. **원본 피드 본문은 대상에 없으므로 `raw_response` NULL**(만들어 내지 않음, 손실로 보고) |
| `rss_observations` | `observations` | 회차별 관측 본문을 그대로(대상 스키마 v9가 `payload_text`를 보관) — SHA-256이 B4 snapshot의 payload_hash와 같다 |
| `rss_classifications`·`rss_classification_history` | `candidates`·`classification_history` | B7과 같은 키(대상의 이력 키를 정규 JSON 기준으로 바꿔, 복구된 행도 같은 키가 됨) |
| `food_candidates.source_item_key` | `food_candidates.source_item_id` | 복구된 항목 번호로 연결, 승인된 음식 ID·정확 별칭 해석 보존 |

안전장치: 기존 조건(IDLE·미완료 작업 0·SHADOW 임대·운영 경로 거부·완료된 적재 파일일 때만 mutable 갱신) 유지. **다른 시점의 SQLite가 같은 번호를 쓰면** 그 항목·회차와 거기에 매달린 모든 행(관측, 분류, B4/B5 행, 그 번호로 키가 정해진 후보, 그 후보의 브리지 명령·이벤트)은 충돌로 보고하고 쓰지 않는다 — 다른 SQLite 행에 붙이지 않는다. 중간 실패는 청크 커밋 후 재실행으로 수렴하고, 재실행 쓰기는 0이다.

검증(`tests/test_reverse_recovery.py`, 대역 11 + 실제 Oracle 11, 통과):

| 사례 | 결과 |
| --- | --- |
| Oracle RSS 수집 → 후보 → 음식 승인 → 새 회차(분류 변경·적재 항목 재관측) → 복구 | 항목 3·회차 2·관측 6·분류 3·이력 1 삽입, 적재 항목 갱신 1. SQLite 번호(항목 1~4, 회차 1~3)가 대상과 같음. SQLite RSS 화면(`trends`·`status`)이 대상과 같음. 후보 3개가 항목에 연결, 승인 음식·정확 별칭 해석 유지. B8 응답 동등(RSS 시계열·순위 포함), B7 대조 일치, FK·integrity 정상, 재실행 쓰기 0, 이후 SQLite 수집기가 회차 4·항목 5로 이어감. 손실은 `rss_raw_response` 2건뿐 |
| 관측 청크 중간 실패 | 커밋된 항목·회차는 유지, 재실행으로 수렴, 중복 0 |
| 적재 후 SQLite가 회차 2·항목 2를 먼저 사용 | 회차 2·항목 `items:2`와 매달린 관측·B4/B5·후보·명령·이벤트는 충돌로 보고, SQLite 행 불변, 대상의 회차 3·항목 3~4는 복구, FK 정상, 재실행 쓰기 0 |

기존 테스트 하나(`test_rss_discovered_on_the_target_is_listed_as_not_written_back`, 대역·Oracle 각 1)는 바로 이 제한을 단정하던 테스트라 같은 시나리오의 강화된 검증(`…comes_back_to_sqlite`)으로 바꿨다.

### 남은 정보 손실과 정책상 제한

| 항목 | 내용 | 영향 |
| --- | --- | --- |
| 원본 피드 본문(`runs.raw_response`) | Oracle은 회차의 RSS XML 전체를 보관하지 않으므로 복구된 회차는 NULL. 만들어 내지 않음(손실 `rss_raw_response`로 보고) | SQLite 감사 내보내기의 `raw_response_base64`가 그 회차만 null. 항목별 원래 XML 조각(`raw_xml`)은 항목·관측 본문에 그대로 있음 |
| v9 이전 관측 | 대상 v8에서 쓰인 관측은 본문이 없어 복구하지 않음(손실 `rss_observation_payload`) | 실제로 해당 행은 없음: v8은 개발용 MZ_TEST에서만 쓰였고 운영 Oracle은 없다 |
| 텍스트 표현 | 시각은 UTC 마이크로초 ISO 문자열, 회차 계약·경고·분류 근거·이력 JSON은 정규 JSON(키 순서·공백)으로 돌아감. 값과 시각 자체는 같다. 항목·관측 본문은 원래 텍스트 그대로 | 해시를 원래 텍스트로 다시 계산하는 도구가 없으므로 영향 없음(B4 해시는 원래 텍스트 기준) |
| 번호 충돌 | 적재 뒤 SQLite가 같은 회차·항목 번호를 따로 쓴 경우, 그 번호의 대상 행과 매달린 행(관측·분류·B4/B5·그 번호로 키가 정해진 후보와 브리지 명령·이벤트)은 쓰지 않고 충돌로 남김 | 운영자가 충돌 목록을 보고 수동 판단해야 함(자동 병합 없음) |
| 갱신 범위 | 기존 SQLite 행의 변경(항목 마지막 발견·본문, 분류 결과, 회차 마감 상태)은 완료된 B7 적재 파일일 때만 반영 | 다른 시점 파일에는 삽입만 하고 변경은 미반영으로 보고 |
| 보존 정책 | RSS는 보존 기한이 없어 모두 돌아가고, YouTube 만료 스냅샷·닫힌 Instagram 캡션은 기존대로 돌아가지 않음. NAVER 값은 어디에도 없음 | 정책 그대로 |

## 4. Oracle Live Adapter별 구현 상태 (Phase 2)

기존 수집기를 다시 만들지 않았다. Oracle 실행기(`OracleYouTubeExecutor`·`OracleInstagramExecutor`·공용 `NaverExecutor`)와 Oracle RSS 서비스가 이미 기존 클라이언트를 쓰므로, 남은 연결부(Live 조립, 게이트, Instagram Live 사전 확인, RSS Live·예약)만 구현했다(`food_trends/oracle_live.py`).

| 수집원 | 재사용한 기존 구현 | 이번에 연결한 것 | 상태 |
| --- | --- | --- | --- |
| Google Trends RSS | `collector.fetch/parse`, `classifier` | Live fetcher·origin `live_api`, 수집 직전 게이트 재확인, 30분 예약 실행, 다른 수집기 진행 중이면 대기, 10분 넘은 미완료 회차는 실패로 마감 | 구현·격리 검증 READY / 실제 호출 승인 필요 |
| Instagram | `GraphClient`(오류 분류), 실행기·캡션 창·수신 기록 | Live 모드의 제공자 최근 고유 태그 확인(기존 SQLite 수집기 로직 이식, 최대 3페이지, 창이 차면 1주 대기·조회 안 함), 원문 origin `live_api` | 구현·격리 검증 READY / 계정·태그 권한 실제 확인 승인 필요 |
| YouTube | `YouTubeClient`, 실행기(search·videos, 29일 만료) | Live 조립(기존 실행기가 이미 mode별 origin 처리) | 구현·격리 검증 READY / 실제 호출 승인 필요 |
| NAVER DataLab | `NaverClient`, `NaverExecutor`(값은 메모리에서만) | Live 조립 | 구현·격리 검증 READY / 실제 호출 승인 필요 |

## 5. RSS 자동 주기 실행 결과

- `OracleRssService.start()`: 승인된 Live 서비스(origin `live_api`, 게이트 통과)에서만 시작, 기본 30분(1800초) 간격(SQLite 수집기와 같은 기본값). Mock 서비스·차단된 서비스는 403으로 거부.
- 매 수집 전 게이트 재확인: Writer 이전 철회 등으로 조건이 깨지면 `oracle_live_rss_blocked`와 사유를 돌려주고 요청을 보내지 않음(검증).
- 중복 방지: 다른 수집기의 회차가 10분 안에 시작돼 미완료면 대기(요청 없음). 10분 넘은 미완료 회차는 다음 수집이 실패(`interrupted`)로 마감하고 B4에 `NONE`(관측 안 됨)으로 적재 — 재시작 후 구멍 없이 이어감(검증).
- 관리 서버 Live 모드는 `--no-schedulers`가 없으면 RSS 예약을 시작한다. B3 자동 실행은 `--execution-automatic`과 `B3_AUTOMATIC_ENABLED=true`가 함께 있어야 시작(Live 승인만으로는 거부, 검증).

## 6. 수집원별 정책·예산 보호

| 보호 | 구현·검증 |
| --- | --- |
| Live 실행 승인 | 수집원 정책·registry 승인·자격 증명·`B3_LIVE_ENABLED`·전용 수집원 목록·기존 SQLite 스케줄러 정지·**Writer 이전**(대상 `quota_accounts` EXCLUSIVE, 최근 15분 사용량 가져오기 후에만 기록)·차단 없음·운영 아님·Shadow 미소유. 하나라도 빠지면 조립 거부(403, 사유 코드만). `OracleExecution`은 통과한 준비 점검 없이 Live 설정을 받지 않음 |
| 요청 직전 재확인 | 큐 등록과 매 작업 첫 요청 전 `_gate`가 Live·전용·Writer 이전·사용량 확인·차단을 다시 확인 |
| 사용량 | 원장이 없거나 15분 넘으면 기존대로 실행 차단(`EXTERNAL_USAGE_UNVERIFIED`), `PROVIDER_CONFIRMED`는 만들지 않음, 불확실 사용량은 보수적 합산(기존) |
| 예산 | 요청 전 원자적 예약. 상한 초과 시 **HTTP 0**, 작업은 `RETRY_WAIT`(budget, 다음 창까지)로 기록(YouTube 일일 검색 상한으로 검증). Instagram 7일 고유 태그(로컬 28 + 제공자 확인), NAVER KST 일일 상한, YouTube 일일 상한 기존 유지 |
| 오류 | 인증·권한 → 계정 차단 + 이후 같은 계정 요청 0(Instagram 401로 검증), 429 → 차단 기간 + 재시도(NAVER로 검증), 5xx·네트워크 → 재시도(Instagram 503으로 검증), 부분 페이지 → `LIMITED`/`PARTIAL`(기존) |
| 저장 제한 | NAVER 값·본문 미저장, YouTube 언급·지표 없음, Instagram 캡션 30일, 토큰·키·원본 피드 본문 미기록, 원문 저장 전 정책 검사(기존) |
| Origin | Live 회차·스테이징·원문은 `live_api`, Mock은 `mock`. 같은 대상에서 B8이 origin별로 자기 회차만 셈(검증). Live 작업에 Mock 클라이언트는 기존 정책 검사로 거부 |
| 동시성 | 소유권·Fencing·작업 Lease 유지, 외부 요청 중 DB 트랜잭션 없음(기존), 작업 중 종료된 워커의 작업은 Lease 만료 후 새 프로세스가 이어받음(검증) |
| 운영 모드 | `RUNTIME_PRODUCTION`·`--backend oracle-production`·`--store oracle-production`·운영 대상 Writer 계속 거부 |

## 7. 실제 Oracle E2E 결과 (Phase 4)

`tests/test_oracle_live_e2e.py`(대역 1 + MZ_TEST 1, 통과). Live 조립을 붙인 관리 서버와 별도 연결 풀의 B8을 실제 HTTP로 사용했다. Live 구간 sqlite3 연결 0건.

| 단계 | 확인 |
| --- | --- |
| 1-2 RSS Live 인터페이스 → 후보 | 기존 collector가 가짜 응답을 받아 회차 origin `live_api`, 후보 3건, B8 목록 비어 있음 |
| 3-4 승인·정확 별칭 | 관리 API 승인, 별칭 승인·해석 |
| 5-6 추적·계획 | Instagram(ig 계정)·YouTube·NAVER 활성화, 미리보기 쓰기 0, Commit |
| 7-9 예약·예산·수집·정제 | 모든 작업 Live, YouTube 일일 검색 상한 2 → 세 번째 검색은 **요청 없이** 대기, 나머지 성공, B4·B5 반영 |
| 10 B8 | 승인 음식만, 두 해시태그 작업이 같은 게시물을 찾아도 1회, 수집 상태 정상, 미승인 후보명 없음, 모든 응답 OpenAPI v1 통과 |
| 11 실패·재시도·중복·재시작·만료 | 503 → 재시도 대기 동안 그날 `NOT_OBSERVED`, 회복 후 1. RSS 재수집·완료 작업 재실행 시 원문·스테이징·언급·후보 불변. 작업을 잡은 채 죽은 워커 → 재시작한 런타임이 Lease 만료 후 이어받아 성공(시도 2). 중지한 수집원은 요청 0, 31일 뒤 유지보수로 첫날 `EVIDENCE_EXPIRED` |
| 12-13 Oracle → SQLite 복구·재실행 | 새 SQLite v11로 복구 수렴, RSS 발굴 항목 복구, 원본 피드 미생성, 만료된 YouTube 스냅샷 미복구, FK·integrity 정상, B8 응답(목록·상세·수집 상태·90일 시계열·통계) 동일, 재실행 쓰기 0 |
| 전체 | 모든 회차 `live_api`, 자연키 중복 0, FK 오류·정책 위반·살아 있는 만료 행 0, NAVER 값·YouTube 언급 0 |

## 8. 기존 SQLite 의존성 잔여 목록

| 파일·기능 | 목적 | 상태 |
| --- | --- | --- |
| `service.py`·`storage.py` | SQLite 모드 RSS 수집(현재 운영 경로) | Oracle은 `oracle_rss`(Live 포함). 운영 전환 시 Writer 이전 대상 |
| `instagram_service`·`youtube_service`·`naver_service`와 저장소 | 레거시 직접 수집기 | Oracle 모드 미연결(503). Live는 Oracle 실행기가 대체 |
| `execution.py`·`execution_runtime.live_controller` | SQLite B3 Live | Oracle Live는 `oracle_live` |
| `quota_ledger.import_legacy_usage` | 레거시 호출 사용량 원천(SQLite를 읽음) | Writer 이전 직전 마지막 가져오기에 필요 |
| `__main__` SQLite 명령 | SQLite 유지보수·수집 | Oracle은 관리 서버·`--maintenance`·Live 점검/이전 |
| `shadow.py`·`target_migration.py`·`reverse_recovery.py`·`cutover.py` | 두 저장소 사이 이관·대조·복구 | 설계상 SQLite를 읽거나 씀 |

## 9. 전체 회귀 테스트 결과

| 실행 | 결과 |
| --- | --- |
| 기본 환경(Oracle 컨테이너 정지, 외부 소켓·운영 DB 차단) | **514개 통과, 실패 0, 오류 0, Oracle 177개 skip**(374초, 외부 소켓 차단 시도 0) |
| 실제 Oracle 포함 전체(MZ_TEST) | **689개 통과, 실패 0, 오류 0, skip 0**(1,288초, 외부 소켓 차단 시도 0) |
| 위 실행 시작 뒤 추가한 게이트 테스트 포함 Live 모듈(MZ_TEST) | **16개 통과**(대역 8 + Oracle 8, 52초) |
| 전체 테스트 수 | **691개**(= 기본 514 + Oracle 전용 177 = Oracle 전체 689 + 게이트 테스트 2). 작업 전 669개 |

기존 669개는 약화하지 않았다. 바뀐 기존 테스트는 둘이다.

1. `test_oracle_execution.test_runtime_never_runs_ddl_and_refuses_unsafe_modes` — **변경의 정당성.** 원래 단정 `LIVE_NOT_SUPPORTED_BY_ORACLE_RUNTIME`은 "Oracle 런타임에는 Live 경로가 없다"는 이전 단계의 제한을 확인하던 것이고, 이번 요구(Phase 3: Live 경로 구현, 기본 비활성)가 그 제한을 없앤다. 같은 시나리오(Mock 런타임에 Live 큐 등록)를 이제 실제 차단 사유로 확인한다: 결과 `BLOCKED`, 사유에 `LIVE_DISABLED_OR_LEGACY_WRITER_NOT_TRANSFERRED`와 `LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED`가 모두 있음, 그리고 **작업 생성 0건**(새로 추가한 단정). 같은 테스트의 "준비 점검 없이 Live 설정으로 생성하면 `ValueError`" 단정은 그대로 통과한다(생성자 거부 조건을 "항상"에서 "통과한 준비 점검이 없으면"으로 좁혔기 때문). 이 테스트의 전송은 Mock이라 외부 HTTP가 원래 없다.
2. §3의 RSS 복구 테스트(제한을 단정하던 테스트 → 같은 시나리오의 복구 검증).

**승인 게이트 미충족 시 작업 0건·외부 HTTP 0건**은 다음으로 확인했다(모두 대역·실제 Oracle).

| 지점 | 테스트 | 결과 |
| --- | --- | --- |
| Live 조립 | `test_live_stays_closed_until_every_gate_passes`, `test_unmet_gates_create_no_job_and_send_nothing` | 게이트 미충족이면 조립 거부(403)·`OracleExecution` Live 생성 거부, 작업 0·RSS 회차 0·가짜 전송 호출 0 |
| 큐 등록 | `test_unmet_gates_create_no_job_and_send_nothing`, `test_runtime_never_runs_ddl_and_refuses_unsafe_modes` | Writer 이전이 철회된 수집원·Live가 꺼진 런타임의 등록은 `BLOCKED`, 작업 수 불변 |
| 대기 중 작업의 시작 | `test_unmet_gates_create_no_job_and_send_nothing` | 큐에 들어간 뒤 Writer 이전이 철회되면 첫 요청 전에 `BLOCKED`(`LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED`), 호출 기록 0, 가짜 전송 호출 0 |
| RSS 수집 | `test_rss_schedule_revocation_and_interrupted_runs` | 철회되면 `oracle_live_rss_blocked`, 피드 요청 0, 예약 시작 거부 |
| 차단된 계정 | `test_a_spent_budget_sends_nothing_and_provider_errors_block_or_retry` | 인증 차단 뒤 같은 계정의 요청 0 |

신규: `test_oracle_live.py` 8×2(마지막 검증 단계에서 게이트 테스트 1개 추가), `test_oracle_live_e2e.py` 1×2, RSS 복구 사례 +2×2(1개 교체 + 2개 추가). 결과 원본: [기본](default-tests/test-summary.json), [Oracle 포함](oracle-full-tests/test-summary.json), [게이트 테스트 포함 Live 모듈의 Oracle 재실행](oracle-live-module-tests/test-summary.json).

## 10. 실제 외부 API 호출 여부

**0건.** 기존 클라이언트 모듈의 opener(`instagram_graph`·`youtube`·`naver_datalab`의 `build_opener`, `collector.urlopen`)를 테스트에서 기록용 가짜로 바꿨고, 테스트 실행기는 루프백 외 소켓을 거부한다(차단 시도 0건). 가짜를 거치지 않은 요청이 있었다면 실행기가 실패로 표시했을 것이다.

## 11. 운영 DB 보존 검증

읽기 전용(`mode=ro`, `query_only`)으로 확인했고, 두 이전 기록(ORACLE-CUTOVER-PREP-1·ORACLE-BACKEND-COMPLETION-1)과 모든 값이 같다([production-final.json](production-final.json)).

| 항목 | 값 |
| --- | --- |
| SHA-256 | `38b880f535dded56e84a11570a1f7793bed1668580950172a417687b689548c0`(변화 없음) |
| 스키마 버전 | 0(`schema_migrations` 없음) |
| 표·행 | 30개·7,701행 |
| integrity_check | ok |
| WAL | 0바이트 |
| 본 파일 수정 시각 | 2026-10-28 14:20:01(변화 없음, 점검 전후 동일) |
| 개발 포트 | 8000/8002/8010/8020/8030/8031/1521 모두 미점유(컨테이너 정지 후) |

테스트 실행기는 운영 파일에 대한 연결을 거부했고(`production_database: blocked`), 이번 작업의 어떤 단계도 운영 파일을 쓰기로 열지 않았다.

## 12. 수정·추가 파일

| 파일 | 변경 |
| --- | --- |
| `food_trends/oracle_live.py` (신규) | Live 준비 점검(읽기 전용), Writer 이전, Live 조립 |
| `food_trends/oracle_execution.py` | Live 설정은 통과한 준비 점검으로만, `_gate`의 Live 조건(전용·Writer 이전·사용량), Instagram Live 고유 태그 사전 확인·origin |
| `food_trends/oracle_rss.py` | Live fetcher·게이트·예약 실행·진행 중 대기·중단 회차 마감, 관측 본문 보관, 이력 키 정규화 |
| `food_trends/reverse_recovery.py` | RSS 발굴 복구와 충돌 전파 |
| `food_trends/target_schema.py` | 대상 스키마 v9(`rss_observations.payload_text`) |
| `food_trends/__main__.py` | `--backend oracle-test`의 `--execution-readiness`·`--writer-transfer`·`--execution-mode live [--execution-automatic]` |
| `tests/test_oracle_live.py`·`tests/test_oracle_live_e2e.py` (신규), `tests/test_reverse_recovery.py`, `tests/test_oracle_execution.py` | 검증 |
| `README.md`, 이 디렉터리 | 실행 방법·현황, [정리 기록](CLEANUP.md), [Live 소량 검증 요청서](LIVE_VERIFICATION_REQUEST.md) |

## 13. Git Commit 및 Push

작업 브랜치 `feat/oracle-live-integration`(모두 원격 push):

| 커밋 | 내용 |
| --- | --- |
| `3a9925f` | Phase 0 정리 기록 |
| `3968f57` | Phase 1 RSS 역방향 복구(대상 v9) |
| `f88c94c` | Phase 2·3 Live 경로·안전장치 |
| `35156f7` | Phase 4 Live E2E |
| `d97c316` | 보고: 게이트 테스트, 보고서·요청서·README·테스트 증거 |

`main` 통합은 아래 통합 기록에 남긴다(fast-forward만, Force Push·Reset Hard 없음).

## 14. Phase 5 판정과 G-OPS 승인 필요 항목

| 항목 | 판정 | 근거·필요한 것 |
| --- | --- | --- |
| Oracle RSS 역방향 복구 | **READY** | §3, 실제 Oracle 검증 |
| Oracle Live 실행 안전성(게이트·예산·오류·재시작·origin) | **READY** | §6·§7, 가짜 전송으로 실제 Oracle 검증 |
| RSS 실제 Live 호출 | **REQUIRES_APPROVAL** | 코드 준비 완료(자격 증명 불필요). RSS Writer 이전(SQLite RSS 스케줄러 정지)과 소량 검증 승인 필요 |
| Instagram 계정·해시태그 권한 검증 | **REQUIRES_APPROVAL** | 실제 토큰·앱 권한·태그 사용량 확인은 실제 호출로만 가능 |
| YouTube API Live 검증 | **REQUIRES_APPROVAL** | 실제 키·쿼터로 search 1·videos 1 |
| NAVER DataLab Live 검증 | **REQUIRES_APPROVAL** | 실제 자격 증명으로 1회 |
| 레거시 SQLite Writer 중단·이전 | **REQUIRES_APPROVAL** (G-OPS-4/5) | 명령(`--writer-transfer`)과 조건 검사 구현. 운영 서버의 `*_ENABLED=false`·RSS 스케줄러 정지·마지막 사용량 가져오기 후 실행 |
| **Writer 이전 취소(철회)** | **BLOCKED — 실제 Live 활성화 전 해결 필수** | 이전을 되돌리는 명령이 없다. 지금은 대상 행을 직접 고치는 방법밖에 없어 진행 중 작업·사용량 인계·감사 기록이 보장되지 않는다. 요구사항은 §16 |
| 운영 Oracle 전환 | **BLOCKED** | 운영 대상은 코드에서 거부(`RUNTIME_PRODUCTION`·`oracle-production`·운영 환경 실행). 잠금 해제 경로의 별도 개발·검증과 G-OPS-1~6 승인 필요 |
| Instagram 제공자 창 판정 | 제한(READY 범위 안) | Oracle에는 해시태그 ID 이력이 없어, 제공자 창이 찼을 때 이미 창 안에 있는 태그도 보수적으로 1주 대기시킴(요청 없음, 안전 측) |

G-OPS 승인 필요 항목: G-OPS-1(백업), G-OPS-2(Writer 정지 확인), G-OPS-3(SQLite 마이그레이션·적재), G-OPS-4/5(수집원별 Writer 이전·Live 소량 검증), G-OPS-6(운영 Oracle). 이번 작업은 어느 것도 실행하지 않았다.

## 15. 실제 수집 검증을 위한 후속 요청서

[LIVE_VERIFICATION_REQUEST.md](LIVE_VERIFICATION_REQUEST.md) — 수집원별 요청 상한(RSS 1, Instagram 5, YouTube 2, NAVER 1), 전제 조건, 명령 순서, 성공 기준, 중단·되돌리기, 보고 항목.

## 16. Writer 이전 취소·복구 후속 요구사항 (실제 Live 활성화 전 BLOCKED 해소 조건)

Writer 이전(`--writer-transfer`)은 수집원 계정의 단독 Writer를 SQLite 레거시 수집기에서 Oracle 런타임으로 넘긴다. 실제 Live를 켜기 전에 **같은 수준으로 안전하게 되돌리는 경로**가 있어야 한다. 이번 작업은 이 기능을 만들지 않았고(범위·승인 밖), 아래를 다음 개발의 인수 조건으로 남긴다.

| # | 요구사항 | 이유 |
| --- | --- | --- |
| R1 | 명시적 명령(예: `--backend oracle-test --writer-release SOURCE --execution-registry …`)과 승인 근거(registry의 승인자·근거) 없이는 실행되지 않음. 운영 대상·`oracle-production`·Shadow 소유 중에는 거부 | 이전과 같은 승인 수준 |
| R2 | 실행 직후부터 그 계정의 Live 큐 등록·작업 시작·요청이 모두 막힘(게이트가 `EXCLUSIVE`가 아님을 보고 차단 — 현재 게이트가 이미 그렇게 동작함을 검증했다) | 철회와 동시에 Oracle의 요청 0 |
| R3 | 그 계정의 Lease를 가진 실행 중 작업이 있으면 거부하거나, Lease 만료·종료까지 기다린 뒤 처리. 대기 중 작업은 취소 사유를 남겨 종료. 이미 커밋된 페이지·관측은 그대로 둠 | 요청 도중 철회로 생기는 부분 상태 방지 |
| R4 | 사용량 인계: Oracle 기간에 그 계정이 쓴 제공자 사용량(예약·청구, Instagram 7일 고유 태그, YouTube 일일, NAVER KST 일일)을 레거시 수집기가 볼 수 있게 넘기거나, 해당 창이 끝날 때까지 레거시 수집기 재개를 막음 | 두 Writer의 합산 초과 방지 |
| R5 | 대상의 `quota_accounts`를 `IMPORTED`(기록 시각과 근거 `writer_release:…`)로 되돌리고, 이전·철회 이력을 지워지지 않는 감사 기록으로 남김 | 누가 언제 넘기고 되돌렸는지 추적 |
| R6 | 레거시 SQLite 수집기의 `*_ENABLED=true` 재활성화와 RSS 스케줄러 재시작은 철회가 끝난 뒤에만 하도록 절차·점검 명령으로 확인 | 동시 실행 방지 |
| R7 | 멱등(두 번 실행해도 결과 같음), 중간 실패 시 재실행으로 수렴 | 운영 중 재시도 안전 |
| R8 | Oracle 기간 데이터의 SQLite 반환은 기존 역방향 복구(`--recovery plan|apply`)로 하고, 철회 절차서에 순서를 명시(철회 → 진행 중 작업 종료 확인 → 복구 → 레거시 재개) | 롤백 경로 일원화 |
| R9 | 테스트: 진행 중 작업이 있을 때의 거부·대기, 철회 직후 요청 0, 사용량 인계 후 레거시 예산 계산, 이중 철회, 운영 대상 거부, 실제 Oracle에서의 동일 결과 | 인수 기준 |

이 요구사항이 구현·검증되기 전까지 실제 Live 활성화(소량 검증 포함)는 시작하지 않는다.

## 통합 기록

`main`을 `2ff5b7a`에서 `d97c316`(보고 커밋, 위 작업 커밋 4개 포함)으로 fast-forward하고 `origin/main`에 push했다. 이 기록 커밋도 같은 방식으로 `main`에 fast-forward했다. 강제 push·reset hard·기존 작업 삭제는 없었다. 병합 직전에 다시 가져온 `origin/main`은 기준 `2ff5b7a` 그대로였다.
