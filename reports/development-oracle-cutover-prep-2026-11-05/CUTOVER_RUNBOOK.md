# Oracle 운영 전환 절차서 (G-OPS-1~6 승인 후 실행)

2026-11-05 작성. **이 절차는 이번 작업에서 운영 환경에 실행하지 않았다.** 각 단계는 해당 게이트의 별도 승인 후에만 실행한다. 단계마다 "현재 실행 가능 여부"를 적었다. 현재 코드는 운영 Oracle 쓰기·실행을 의도적으로 거부하므로(아래 [잠금](#현재-코드-잠금)), 5단계 이후는 BLOCKED 항목이 해결된 뒤에만 수행할 수 있다.

- 명령은 저장소 루트에서 `py -3.12 -B`로 실행한다. 운영 SQLite 경로는 `DATABASE_PATH`(기본 `data/trends.sqlite3`)다.
- Oracle 접속 정보는 환경변수로만 준다(`ORACLE_DSN`·`ORACLE_USER`·`ORACLE_PASSWORD`·`ORACLE_ENVIRONMENT=production`·`ORACLE_ALLOW_PRODUCTION=true`). 출력·로그·보고서에 남기지 않는다.
- 모든 명령은 외부 API를 호출하지 않는다. 출력은 건수·코드·해시뿐이다.
- 리허설 근거: [cutover-rehearsal-oracle.json](cutover-rehearsal-oracle.json)(운영 DB 격리 사본 → 개발용 Oracle MZ_TEST), [REPORT.md](REPORT.md).

## 0. 승인과 판단 기준

| 항목 | 내용 |
| --- | --- |
| 필요한 승인 | G-OPS-1(백업), G-OPS-2(Writer 정지), G-OPS-3(SQLite 마이그레이션), G-OPS-4/5(수집 소유권·Live 검증), G-OPS-6(운영 Oracle 생성·적재·전환) |
| 전환 전 해결 | REPORT §8의 BLOCKED 항목 전부(운영 쓰기 잠금 해제 변경과 검증, Oracle Live 수집, RSS 발굴 Oracle 저장, 관리 서버 Oracle 연결) |
| 진행 중단 기준 | 어느 단계든 대조 불일치(`consistent=false`), FK 고아·정책 위반·만료 잔존 행이 0이 아님, B8 응답 불일치, 원본 해시 변화, 예상 밖 거부 사유 |
| 롤백 기준 | 전환 후 24시간 안에 수집 실패율 급증, B8 `503` 지속, 데이터 정합성 점검 실패, 운영자 판단 → 11단계 |

## 1. 사전 점검 (읽기 전용) — 현재 실행 가능

```powershell
git rev-parse HEAD                                   # 승인된 main 커밋과 같은지
py -3.12 -B -m food_trends --migration-status        # 운영 SQLite 버전(전환 전 0)
py -3.12 -B -m food_trends --runtime-status sqlite   # 저장소 선택 점검(쓰기 0)
Get-NetTCPConnection -State Listen | ? LocalPort -in 8000,8002,8010,8020,8030   # Writer·서버 없음 확인
```

운영 Oracle: 버전(개발 검증 23.26.3), 문자셋 AL32UTF8, 전용 스키마, 최소 권한(CREATE SESSION·CREATE TABLE·자기 스키마 DML), 여유 공간(G-OPS-0 실측: 16,576행에 세그먼트 29.2MB, 운영 서버에서 재측정). 접속은 앱 호스트에서만, TCPS 권장.

## 2. Writer 정지와 중복 쓰기 방지 (G-OPS-2) — 현재 이미 정지 상태

1. 8000/8002 서버와 스케줄러가 꺼져 있는지 확인한다(현재 2026-10-26 이후 정지).
2. 전환이 끝날 때까지 SQLite 서버를 재기동하지 않는다. 운영 DB 파일을 읽기 전용 속성으로 바꿔 실수 쓰기를 막는다(`attrib +R`, 6단계 적재 후).
3. Oracle 대상 소유 모드가 `IDLE`인지 확인한다(`--runtime-status oracle-production`은 현재 잠금으로 거부됨 → 잠금 해제 변경에 상태 점검 포함 필요).

## 3. 원본 백업과 복구 검증 (G-OPS-1) — 현재 실행 가능(승인 후)

```powershell
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'); New-Item -ItemType Directory "data/ops-backup-$stamp" | Out-Null
py -3.12 -B -c "import json,sys; from food_trends import cutover; print(json.dumps(cutover.backup('data/trends.sqlite3', sys.argv[1]), default=str))" "data/ops-backup-$stamp/trends.sqlite3"
py -3.12 -B tests/rehearse_cutover.py --source "data/ops-backup-$stamp/trends.sqlite3" --output "data/ops-backup-$stamp/rehearsal.json" --target sqlite
```

- 백업은 읽기 전용 연결 + SQLite backup API다. 출력의 `sha256`·행 수·`integrity=ok`를 기록하고, 백업 사본을 별도 매체에 1벌 더 둔다(출처 보존 기간이 지난 원문은 사본에서도 정리).
- 복구 검증: 위 리허설이 백업 사본으로 v0→v11·스테이징·대상 적재·대조·재실행·복구 dry run까지 통과(`passed=true`)해야 한다. 운영 원본은 열기만 하며 바뀌지 않는다(리허설이 해시·시각·WAL로 확인).

## 4. SQLite v0 → v11 (G-OPS-3) — 현재 실행 가능(승인 후)

```powershell
py -3.12 -B -m food_trends --migrate                       # 1~11 단일 트랜잭션, 원본 열·행 보존
py -3.12 -B -m food_trends --ingest-pending                # RSS 회차 스테이징·연결(API 호출 없음)
py -3.12 -B -m food_trends --ingest-legacy instagram_graph # 출처 불명 Instagram 논리 회차
py -3.12 -B -m food_trends --ingest-legacy youtube         # 출처 불명 YouTube(현재 운영 데이터는 live_api라 0건)
py -3.12 -B -m food_trends --maintenance                   # 출처 보존 정리·재처리
py -3.12 -B -c "import sqlite3; c=sqlite3.connect('data/trends.sqlite3'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
py -3.12 -B -m food_trends --migration-status              # 11
```

리허설 기대값(운영 사본): RSS 회차 95개(성공 90·실패 5) 스테이징, Instagram 185건, YouTube live_api 431건은 스테이징 경로 없음(원문만 이관), 원래 30개 테이블 열·행 보존, FK 0.

## 5. 운영 Oracle 스키마 (G-OPS-6) — 실행 가능(운영 접속은 `ORACLE_ALLOW_PRODUCTION=true` 필요)

```powershell
py -3.12 -B -m food_trends --target-migrate oracle   # 대상 v7, 체크섬 기록, 중단 시 재실행으로 재개
py -3.12 -B -m food_trends --target-status oracle    # {"version": 7}
```

## 6. 데이터 적재와 대조 — **BLOCKED**(운영 대상 SHADOW 쓰기 잠금)

```powershell
py -3.12 -B -m food_trends --target-load oracle      # B7 적재 + 레거시 사용량 원장 + 대조(JSON)
```

- 판정: `consistent=true`, `summary`의 missing·extra·changed 0, 해시 불일치 표 0, `checks`의 `foreign_key_errors`·`unresolved_payload_refs`·`blocked_policy_rows`·`expired_rows_in_target` 0. 제외·거부는 `dispositions`(사유 코드)로 확인한다. 만료 근거는 `tombstones`로 들어온다(원문 없음).
- 리허설 실측(개발용 Oracle): 15,671행 적재 11.7초, 29개 표 해시 일치, 제외 0(2026-12-01 기준이면 YouTube 원문 431건 만료 제외·캡션 184건 제거).
- 재실행 확인: 같은 명령을 다시 실행해도 쓰기 0이어야 한다(배치 재개·멱등).
- 앱 응답 대조: SQLite와 Oracle에서 `py -3.12 -m food_trends.app_api --store ...`로 같은 요청의 응답이 같아야 한다(리허설 34건 일치). 운영 Oracle 저장소 선택(`oracle-production`)은 현재 잠금.
- 대상 DDL 직후 몇 초간 앱 API가 `503`(ORA-01466, 읽기 전용 스냅샷)일 수 있다. 재시도하면 사라진다.

## 7. Oracle 단일 Writer 전환 — **BLOCKED**(RUNTIME_PRODUCTION 잠금)

1. SQLite Writer 정지 유지(2단계), 운영 SQLite 파일 읽기 전용 속성.
2. Oracle 런타임을 운영 모드로 기동(음식·별칭·추적 명령은 `runtime_storage.oracle_backend`의 Oracle 서비스, 스케줄러·Worker는 Oracle 실행기). 대상 소유 모드가 RUNTIME으로 바뀌면 Shadow·B7 쓰기는 자동으로 거부된다.
3. 관리 화면은 현재 SQLite 서버 전용이다(BLOCKED). Oracle 서비스 주입 서버 모드가 필요하다.

## 8. 수집기 연결 — **BLOCKED**

- YouTube·Instagram·NAVER Live: Oracle 런타임은 Mock만 실행한다(`LIVE_NOT_SUPPORTED_BY_ORACLE_RUNTIME`). Instagram 원격 고유 태그 점검, 출처 승인 registry, 수집 소유권 이전(G-OPS-4)과 Live 검증(G-OPS-5)이 선행 조건이다.
- RSS 발굴·후보 가져오기: SQLite 저장소에만 쓴다(Oracle은 `rss_run`·`import`를 거부). 단일 Writer 전환 뒤에는 Oracle 저장 경로가 필요하다.
- 레거시 사용량 원장: 레거시 수집기가 남아 있는 동안 15분 주기 가져오기가 필요하다(지나면 런타임이 `EXTERNAL_USAGE_UNVERIFIED`로 실행을 막는다).

## 9. B8 앱 API 연결 — **BLOCKED**(`--store oracle-production` 잠금)

`py -3.12 -m food_trends.app_api --store oracle-production`(잠금 해제 후). OpenAPI v1 7개 엔드포인트 계약은 변경 없음. 웹 프론트엔드는 Base URL만 바꾼다(외부 공개 배포는 별도 과제: TLS·인증·속도 제한).

## 10. 운영 상태 모니터링

| 대상 | 방법 | 경보 기준 |
| --- | --- | --- |
| 소유권 | `RuntimeOwnership.status()`(mode, live_participants, runtime_jobs_in_flight) | 예상 밖 모드, 미완료 작업 누적 |
| 실행 | `OracleExecution.status()` counts, 마지막 성공 | FAILED·BLOCKED 증가, 성공 시각 정체 |
| 수집 범위 | B8 `/api/v1/sources/status` | coverage NONE·PARTIAL 증가, 품질 FAIL |
| 예산 | `quota.usage()`의 `usage_coverage` | STALE·UNVERIFIED |
| 정합성 | `target_migration.integrity_checks` 일 1회 | FK 고아·정책 위반·`expired_rows_in_target`이 0이 아님 |
| 용량 | Oracle 세그먼트·테이블스페이스 | 사용률 임계 |

## 11. 오류 시 복구와 전환 취소

- **7단계 이전**(Oracle이 아직 Writer가 아님): Oracle을 버리고 SQLite로 계속한다. 데이터 손실 없음. 운영 SQLite 읽기 전용 속성을 해제하고 서버를 재기동한다.
- **7단계 이후**(Oracle에만 새 데이터가 있음): 역방향 복구.

```powershell
# 1) Oracle Writer 정지 → 소유 모드 IDLE·미완료 작업 0 확인(복구 명령이 직접 확인하고 아니면 거부)
py -3.12 -B -m food_trends --recovery plan --recovery-target oracle            # dry run: 대상 전용 행·mutable 갱신·충돌·손실 목록(쓰기 0)
# 2) 계획 검토 후(G-OPS 승인), 적재 당시 그대로인 운영 SQLite로 복구
attrib -R data\trends.sqlite3
py -3.12 -B -m food_trends --recovery apply --recovery-target oracle --apply-updates --allow-operational-database
# 3) 검증: converged=true, remaining의 target_only 0, 충돌 0
py -3.12 -B -m food_trends --shadow compare --shadow-target oracle             # 읽기 전용 대조(consistent=true), 이어서 앱 응답 대조
```

- `plan`은 읽기만 하므로 지금도 운영 대상에서 실행된다. `apply`는 대상의 SHADOW 임대를 잡으므로 운영 대상에서는 7단계와 같은 잠금 해제 변경이 있어야 한다(전환 후라면 이미 해제된 상태).

- 복구는 대상의 배타 SHADOW 임대를 쥔 채 진행하며, 기존 SQLite 행은 "변경 열이 mutable + 대상 파일이 적재 당시와 같음 + `--apply-updates`"일 때만 갱신한다. 그 밖의 차이는 충돌로 보고만 한다. 중간 실패 시 같은 명령을 다시 실행하면 이어서 수렴한다.
- 복구되지 않는 것(손실 목록): 슬롯 생명주기 열, Instagram 원래 해시태그 표기, 소유권 임대·사용량 원장·적재 기록. 보존 기간이 지난 원문·캡션은 정책상 복구하지 않는다.
- 최후 수단: 3단계 백업을 **새 경로**에 복원해 기동(원본 덮어쓰기 금지).

## 현재 코드 잠금

운영 Oracle에 대한 다음 동작은 코드에서 항상 거부된다. 해제는 G-OPS-6 승인 후 별도 변경(승인 스위치와 운영 모드 경로의 검증 포함)으로만 한다.

| 위치 | 거부 |
| --- | --- |
| `runtime_ownership.RuntimeOwnership._allowed` | 운영 대상의 SHADOW·RUNTIME 임대(`production_target_requires_approval`: B7 적재·Shadow·역방향 복구 apply·런타임 모두 해당), RUNTIME_PRODUCTION(`runtime_production_disabled`) |
| `oracle_execution.OracleExecution` | 운영 대상(`oracle_production_runtime_disabled`), Live 모드 |
| `runtime_storage.select('oracle-production')`, `app_api --store oracle-production` | `oracle_production_runtime_disabled` |
| `target_db.OracleSettings.from_env` | `ORACLE_ENVIRONMENT=production`은 `ORACLE_ALLOW_PRODUCTION=true` 없이 연결 거부 |
