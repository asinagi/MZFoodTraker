# Oracle Live 실제 API 소량 검증 실행 절차서 (경로 A: 격리 v11 + MZ_TEST)

작성 2026-11-07, ORACLE-LIVE-VERIFICATION-READY-1. **이 절차서는 승인이 아니다.** [요청서](../development-oracle-live-integration-2026-11-05/LIVE_VERIFICATION_REQUEST.md)의 G-OPS-4/5 승인과 수집원별 실제 승인 근거(registry)가 갖춰진 뒤에만 실행한다. 승인 전에는 6단계(Live 서버) 이후를 실행하지 않는다.

- 모든 명령은 저장소 루트의 PowerShell 한 세션에서 순서대로 실행한다. 명령과 옵션은 현재 코드(`python -m food_trends --help`)에 있는 것만 썼고, 같은 순서를 개발용 Oracle에서 리허설했다([ISOLATED_CUTOVER_REHEARSAL.md](ISOLATED_CUTOVER_REHEARSAL.md), 통과).
- 출력은 건수·상태·사유 코드뿐이다. 자격 증명·DSN·토큰을 화면·로그·보고서에 남기지 않는다. 격리 사본·registry·로그는 Git이 무시하는 `data/ops-pilot/` 아래에만 둔다.
- 운영 SQLite(`data/trends.sqlite3`, v0)는 읽기만 한다. 운영 Oracle은 코드에서 거부되며 이 절차의 범위가 아니다.

## 0. 시작 조건

| 확인 | 기준 |
| --- | --- |
| 승인 | 요청서 승인(G-OPS-4/5, 검증할 수집원과 최대 요청 수), 수집원별 실제 승인 근거 |
| 코드 | `git rev-parse HEAD`가 승인된 `main` 커밋 |
| 운영 DB | `--migration-status`가 `version: 0`, 수정 시각 2026-10-28 14:20:01 그대로 |
| 기존 수집기 | 8000·8002 서버 없음(운영 DB가 v0라 SQLite 서버는 시작 자체가 거부됨) |
| Oracle | 개발용 MZ_TEST 컨테이너 실행 중, 다른 작업이 MZ_TEST를 쓰지 않음 |

## 1. 환경 변수

```powershell
# Oracle: 두 접두어가 모두 같은 MZ_TEST를 가리켜야 한다(값은 출력하지 않는다)
#   ORACLE_TEST_*  : --backend oracle-test 명령(점검·이전·철회·상태·Live 서버)
#   ORACLE_*       : --target-* · --recovery-target oracle 명령(적재·인계·복구·재개 판정)
$env:ORACLE_TEST_ENVIRONMENT = 'test'          # ORACLE_TEST_DSN/USER/PASSWORD는 개발용 접속 정보로 따로 설정
$env:ORACLE_DSN = $env:ORACLE_TEST_DSN; $env:ORACLE_USER = $env:ORACLE_TEST_USER; $env:ORACLE_PASSWORD = $env:ORACLE_TEST_PASSWORD
$env:ORACLE_ENVIRONMENT = 'test'               # test가 아니면 인계·재개 판정이 거부된다(runtime_test_requires_test_environment)
Remove-Item Env:ORACLE_ALLOW_PRODUCTION -ErrorAction SilentlyContinue
Remove-Item Env:DATABASE_PATH -ErrorAction SilentlyContinue   # 원본은 기본 data/trends.sqlite3(읽기 전용으로만 사용)

# Live 게이트
$env:B3_LIVE_ENABLED = 'true'
$env:B3_EXCLUSIVE_SOURCES = 'google_trends_rss,instagram_graph,youtube,naver_datalab'   # 승인된 수집원만
$env:B3_AUTOMATIC_ENABLED = 'false'

# 기존 SQLite 수집기 정지(아니면 점검이 LEGACY_SCHEDULER_ENABLED로 거부)
$env:INSTAGRAM_COLLECTION_ENABLED = 'false'; $env:YOUTUBE_COLLECTION_ENABLED = 'false'; $env:NAVER_DATALAB_ENABLED = 'false'

# 요청 상한을 예산이 강제하게 하는 설정(리허설로 확인: 같은 작업을 다시 실행해도 추가 요청 0)
$env:INSTAGRAM_MAX_PAGES = '1'; $env:INSTAGRAM_SEARCH_SOFT_LIMIT = '1'
$env:YOUTUBE_DAILY_SEARCH_LIMIT = '1'; $env:YOUTUBE_DAILY_VIDEO_LIMIT = '1'; $env:YOUTUBE_MAX_PAGES = '1'
$env:NAVER_DATALAB_DAILY_LIMIT = '1'

# 관리 서버
$env:PORT = '8000'; $env:MZ_ADMIN_TOKEN = [guid]::NewGuid().ToString('N')
New-Item -ItemType Directory -Force data\ops-pilot | Out-Null
$REG = 'data\ops-pilot\registry.json'; $COPY = "data\ops-pilot\isolated-$((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')).sqlite3"
```

자격 증명은 기존 `.env`의 `INSTAGRAM_ACCESS_TOKEN`·`INSTAGRAM_USER_ID`·`FACEBOOK_PAGE_ID`, `YOUTUBE_DATA_API_V3_KEY`(또는 `YOUTUBE_API_KEY`), `NAVER_DATALAB_CLIENT_ID`·`NAVER_DATALAB_CLIENT_SECRET`을 쓴다. 프로세스 환경 변수가 `.env`보다 우선한다.

`INSTAGRAM_SEARCH_SOFT_LIMIT=1`은 7일 동안 새 해시태그 1개만 허용한다. 계정이 최근 7일 안에 다른 태그를 이미 조회했다면 Instagram 작업은 최근 검색 확인 요청 1회 뒤 1주 대기로 끝난다(안전 측, 요청 추가 없음).

## 2. registry 작성과 검토

`data\ops-pilot\registry.json`(Git 제외)에 수집원마다 한 줄씩 쓴다. 키는 정확히 아래 6개다.

```json
[{"source_id": "google_trends_rss", "account_scope": "public-feed", "permission_verified": true, "policy_allowed": true,
  "approved_by": "<승인자>", "evidence": "<공개 피드 이용 조건 검토 근거>"},
 {"source_id": "instagram_graph", "account_scope": "ig:<INSTAGRAM_USER_ID 값>", "permission_verified": true, "policy_allowed": true,
  "approved_by": "<승인자>", "evidence": "<Meta 앱 권한·해시태그 검색 기능 승인 근거>"},
 {"source_id": "youtube", "account_scope": "local-default", "permission_verified": true, "policy_allowed": true,
  "approved_by": "<승인자>", "evidence": "<Google Cloud 프로젝트 YouTube Data API 사용·할당량 근거>"},
 {"source_id": "naver_datalab", "account_scope": "local-default", "permission_verified": true, "policy_allowed": true,
  "approved_by": "<승인자>", "evidence": "<NAVER 애플리케이션 데이터랩 API 사용 근거>"}]
```

- `permission_verified`·`policy_allowed`는 사람이 실제 근거를 확인한 뒤에만 `true`로 쓴다. 리허설의 합성 registry는 승인 근거가 아니다.
- 검토: 두 번째 사람이 근거 문서와 대조한다. 형식은 3단계 점검이 검증하고, 근거 문장은 어떤 출력에도 나오지 않는다.

## 3. 사전 점검 (읽기 전용, 외부 요청 0)

```powershell
git rev-parse HEAD
py -3.12 -B -m food_trends --migration-status                 # 운영 SQLite version 0
Get-NetTCPConnection -State Listen | ? LocalPort -in 8000,8002,8010,8020,8030   # 없어야 함
py -3.12 -B tests/audit_source_readiness.py --registry $REG --oracle --output data\ops-pilot\readiness-before.json
```

`readiness-before.json`에서 수집원마다 `blockers: []`, `pilot_settings_mismatch: []`, `registry_status: GRANTED_IN_FILE`, Instagram `token_days_left_by_record` 7일 이상, `history_account_is_configured_account: true`를 확인한다. 판정은 최대 `REQUIRES_APPROVAL`이다(자격 증명 유효성은 실제 요청 없이 확인할 수 없다).

## 4. 격리 사본, 대상 스키마, 적재 (외부 요청 0)

```powershell
py -3.12 -B -m food_trends --isolate-database $COPY           # 운영 DB를 읽기 전용 백업 → 사본만 v11·스테이징·보존 정리
py -3.12 -B -m food_trends --target-migrate oracle            # MZ_TEST 대상 v10
py -3.12 -B -m food_trends --target-status oracle             # {"version": 10}
py -3.12 -B -m food_trends --target-load oracle --isolated-database $COPY
```

- `--isolate-database`: `source_unchanged`가 전부 `true`, `copy.schema_version` 11, `integrity` ok, `foreign_key_errors` 0, `prepare.original_rows_and_columns_preserved` true. 같은 경로로 다시 실행하면 `isolated_copy_exists`로 거부된다.
- `--target-load`: `consistent: true`, `rejected_rows` 0, `legacy_usage_accounts`에 `instagram_graph/ig:<계정>`·`youtube/local-default`·`naver_datalab/local-default`가 있어야 한다.
- 여기서부터 **15분 안에** 5단계를 끝낸다(사용량 가져오기 유효 시간). 넘기면 `--target-load`를 다시 실행한다(재실행 쓰기 0).

## 5. Writer 이전 (승인 범위의 수집원만)

```powershell
py -3.12 -B -m food_trends --backend oracle-test --execution-readiness $REG     # 이전 전: LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED만
foreach ($s in 'google_trends_rss','instagram_graph','youtube','naver_datalab') {
  py -3.12 -B -m food_trends --backend oracle-test --writer-transfer $s --execution-registry $REG }
py -3.12 -B -m food_trends --backend oracle-test --writer-status                 # 모두 ORACLE
py -3.12 -B -m food_trends --backend oracle-test --execution-readiness $REG     # ready: true (아니면 중단)
```

## 6. Live 서버와 수동 실행 (여기부터 실제 외부 요청)

```powershell
py -3.12 -B -m food_trends --backend oracle-test --execution-mode live --execution-registry $REG --no-schedulers
```

`--no-schedulers`로 RSS 예약을 시작하지 않고, `--execution-automatic`을 주지 않아 B3 자동 실행도 없다. 다른 PowerShell 창에서(같은 `$env:MZ_ADMIN_TOKEN`):

```powershell
$api = 'http://127.0.0.1:8000'; $h = @{ 'X-MZ-Admin-Token' = $env:MZ_ADMIN_TOKEN }
function Post($path, $body) { Invoke-RestMethod -Method Post -Uri ($api + $path) -Headers $h -ContentType 'application/json' -Body ($body | ConvertTo-Json -Compress -Depth 5) }
Post '/api/collect' @{}                                                            # RSS 피드 1회
Invoke-RestMethod "$api/api/food-candidates" -Headers $h                            # 승인할 후보 확인
```

음식 1개를 화면(`http://127.0.0.1:8000`) 또는 `/api/foods/review`로 승인하고, 추적을 수집원별로 1개씩 등록·활성화한다(`/api/tracking/register` → `/api/tracking/activate`, `account_scope`는 registry와 같은 값). 별칭은 추가하지 않는다(별칭마다 작업이 생긴다. 추가 작업은 예산이 막지만 계획이 단순해진다). 이어서:

```powershell
Post '/api/planner/commit' @{ request_key = [guid]::NewGuid().ToString() }
$jobs = (Post '/api/execution/schedule' @{ mode = 'live' }).items
foreach ($j in $jobs) { Post '/api/execution/run' @{ job_id = $j.job_id } }     # 작업마다 한 번만
```

| 수집원 | 계획 상한 | 이 설정에서 코드가 막는 것 | 리허설 실측 |
| --- | ---: | --- | ---: |
| Google Trends RSS | 1 | 예산 없음: `/api/collect`를 한 번만 호출한다 | 1 |
| Instagram | 5 | 최근 검색 확인 최대 3쪽 + 해시태그 조회 1 + 최근 게시물 1쪽(`INSTAGRAM_MAX_PAGES=1`). 두 번째 새 태그는 요청 없이 대기(`INSTAGRAM_SEARCH_SOFT_LIMIT=1`) | 3 |
| YouTube | 2 | search 1·videos 1(태평양 시간 일일 상한 1). 두 번째 검색은 요청 없이 대기 | 2 |
| NAVER DataLab | 1 | KST 일일 상한 1 | 1 |
| 합계 | 9 | | 7 |

실패한 작업은 다시 실행하지 않는다(8단계).

## 7. 요청 수 기록과 확인

```powershell
Invoke-RestMethod "$api/api/execution/status" -Headers $h                              # 작업 상태별 건수(counts)
foreach ($j in $jobs) { (Invoke-RestMethod "$api/api/execution/jobs/$($j.job_id)" -Headers $h).items[0].calls.Count }   # 작업별 실제 요청 수
Invoke-RestMethod "$api/api/status" -Headers $h                                        # RSS 마지막 회차(피드 요청 1회 = 회차 1개)
py -3.12 -B -m food_trends --backend oracle-test --writer-status                     # 수집원별 열린 사용량(open_usage)
py -3.12 -B -m food_trends.app_api --store oracle-test --observation-origin live_api --port 8020   # B8 조회(별도 창)
```

- 제공자 요청 1회는 실행 호출 기록 1행(`calls`, 상태 SUCCEEDED·FAILED·UNKNOWN)이고, RSS 피드 요청 1회는 RSS 회차 1개다. 리허설에서 이 기록이 가짜 전송이 받은 요청 수와 수집원마다 정확히 같았다.
- 합계가 2절 상한을 넘거나 예상 밖 요청이 있으면 즉시 8단계.

## 8. 즉시 중단

1. Live 서버 창에서 `Ctrl+C`(프로세스 종료. 예약·자동 실행이 없으므로 더 나가는 요청 없음).
2. 401/403은 계정이 자동으로 차단 기록된다. 자격 증명·권한을 다시 검토하기 전에는 재시도하지 않는다. 429는 차단 기간이 끝날 때까지 재시도하지 않는다.
3. 새 프로세스가 Live를 열지 못하게 하려면 `$env:B3_LIVE_ENABLED = 'false'`(게이트가 닫힘). 이어서 9단계로 철회한다.

## 9. Writer 철회

```powershell
foreach ($s in 'instagram_graph','youtube','naver_datalab','google_trends_rss') {
  py -3.12 -B -m food_trends --backend oracle-test --writer-release $s --execution-registry $REG --release-wait 120 }
py -3.12 -B -m food_trends --backend oracle-test --writer-status                 # 모두 RELEASED, active_live_jobs·live_leases 0
```

- 철회 표시가 커밋되는 순간 그 수집원의 새 큐 등록·작업 시작·요청이 막힌다. 진행 중 요청이 있으면 `DRAINING`(종료 코드 2): 그 요청의 결과는 저장되고 다음 요청 전에 멈춘다. 같은 명령을 다시 실행하거나 `--release-wait`로 기다린다. 남은 작업은 `CANCELLED`/`WRITER_RELEASED`.
- 철회 중(`RELEASING`)에는 Writer 이전·사용량 인계가 409로 거부되고 재개 판정은 `ORACLE_WRITER_NOT_RELEASED`(리허설 확인).

## 10. 사용량 인계

```powershell
foreach ($s in 'instagram_graph','youtube','naver_datalab','google_trends_rss') {
  py -3.12 -B -m food_trends --writer-handover $s --recovery-target oracle --isolated-database $COPY }
```

Oracle 기간의 열린 창 사용량이 격리 사본의 `instagram_searches`·`youtube_calls`·`naver_calls`에 들어간다(재실행 0건, 결과 불명 호출도 집계). 출력의 `open_usage.open_until`(Instagram 7일, YouTube 태평양 시간 자정, NAVER KST 자정)을 기록한다. 13단계의 보존 기한이 된다.

## 11. Oracle → SQLite 복구

```powershell
py -3.12 -B -m food_trends --recovery plan --recovery-target oracle --isolated-database $COPY
py -3.12 -B -m food_trends --recovery apply --recovery-target oracle --isolated-database $COPY --apply-updates
```

판정: `converged: true`, `updates_applied: true`, `remaining`의 `target_only`·`mutable_updates`·`conflicts`가 모두 0. 인계가 파일을 바꾼 뒤에도 기준선은 인계가 남긴 파일 지문으로 확인된다(`baseline.matches_recorded_handover: true`). `updates_applied: false`이면서 `mutable_updates`가 0이 아니면 중단하고 원인을 확인한다. 손실 목록(슬롯 생명주기, 원래 해시태그 표기, 원본 피드 본문 등)은 설계상 복구 대상이 아니다.

## 12. 재개 판정

```powershell
foreach ($s in 'instagram_graph','youtube','naver_datalab','google_trends_rss') {
  py -3.12 -B -m food_trends --legacy-resume-check $s --recovery-target oracle --isolated-database $COPY }
```

경로 A에서 이 판정은 격리 사본에 대한 것이다. **운영 SQLite 수집기는 켜지 않는다**(운영 DB는 v0이라 시작도 되지 않는다). 운영 수집기 재개는 G-OPS-3 이후의 별도 절차이며, 그때 13단계의 기한 규칙을 따른다.

## 13. 정리

```powershell
py -3.12 -B tests/audit_source_readiness.py --registry $REG --oracle --output data\ops-pilot\readiness-after.json
py -3.12 -B -m food_trends --migration-status                                   # 운영 DB version 0 그대로
Remove-Item $COPY, "$COPY-wal", "$COPY-shm" -ErrorAction SilentlyContinue      # 운영 데이터 사본: 보고서 작성 뒤 삭제
```

- **MZ_TEST는 가장 늦은 `open_until`(Instagram 태그 기준 최대 7일)까지 초기화하지 않는다.** 운영 SQLite는 이 검증의 사용량을 모르므로, G-OPS-3 이후 운영 수집기를 그 기한 전에 켜려면 경로 B로 운영 DB에 인계해야 하고 그 근거가 MZ_TEST에 있다.
- 기한이 지나면 MZ_TEST를 초기화한다: `py -3.12 -B -c "import sys; sys.path.insert(0, 'tests'); from oracle_support import oracle_target, reset_target_schema; db = oracle_target(); reset_target_schema(db); db.close()"`(일회용 테스트 계정인지 다시 확인한 뒤 삭제).
- Live용 환경 변수는 세션을 닫아 없앤다.

## 14. 민감정보 확인

```powershell
git status --short                                                    # data\ops-pilot\ 아래 파일이 보이지 않아야 함(Git 제외)
git grep -n -E "EAA[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}" -- reports README.md   # 결과 없음
```

보고서에는 요청 수·응답 코드·오류 종류·건수·해시만 옮긴다. 응답 본문·캡션·계정 식별자·토큰은 옮기지 않는다.

## 경로 B (G-OPS-3 운영 마이그레이션 이후)

G-OPS-1(백업)·G-OPS-2(Writer 정지)·G-OPS-3(운영 SQLite v0→v11, [CUTOVER_RUNBOOK](../development-oracle-cutover-prep-2026-11-05/CUTOVER_RUNBOOK.md) 3~4단계) 승인과 실행 뒤에는 격리 사본 대신 운영 DB를 쓴다. 차이는 다음뿐이다.

| 단계 | 경로 A | 경로 B |
| --- | --- | --- |
| 4 | `--isolate-database` + `--target-load ... --isolated-database $COPY` | `--target-load oracle`(운영 DB 읽기 전용) |
| 10 | `--writer-handover ... --isolated-database $COPY` | `--writer-handover ... --allow-operational-database` |
| 11 | `--recovery apply ... --isolated-database $COPY --apply-updates` | `--recovery apply ... --apply-updates --allow-operational-database` |
| 12 | 격리 사본 판정, 운영 수집기는 계속 정지 | `ready: true`인 수집원만 `*_ENABLED=true`(RSS는 SQLite 서버 스케줄러)로 재개 |
