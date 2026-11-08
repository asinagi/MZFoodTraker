# 운영 절차서 — Oracle 운영 백엔드 (`MZ_APP`)

ORACLE-PRODUCTION-CUTOVER-1. 저장소 루트(`D:\mz 제조기`)의 PowerShell에서 실행한다. 스크립트는 자격 증명을 출력하지 않는다. 운영 파일(접속 정보·승인 기록·registry·백업·로그·PID·관리 토큰)은 전부 Git이 무시하는 `data/` 아래에 있다.

| 파일 | 내용 |
| --- | --- |
| `data/oracle-prod/oracle-prod.env` | `ORACLE_DSN/USER/PASSWORD`(MZ_APP), `ORACLE_ENVIRONMENT=production`, `ORACLE_ALLOW_PRODUCTION=true`, `ORACLE_PRODUCTION_SCHEMA=MZ_APP`, `ORACLE_PRODUCTION_APPROVAL` |
| `data/oracle-prod/production-approval.json` | 운영 승인 기록(스키마·승인자·시각·G-OPS-1/2/3/6 근거). 없거나 형식이 틀리면 운영 접근 전부 거부 |
| `data/ops-prod/registry.json` | 수집원별 Live 승인(사용자 승인) |
| `data/ops-prod/production-source-v11.sqlite3` | 이관 원본 v11 사본(SQLite 보관·롤백 기준) |
| `data/ops-backup-20261107T153742Z/` | 운영 SQLite v0 백업 |
| `data/ops-prod/run/`, `data/ops-prod/logs/` | PID·관리 토큰, 서버 로그 |

## 1. 시작과 정지

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\start-oracle.ps1     # 컨테이너 시작(영속 볼륨 mz-oracle-data), 운영 스키마 응답까지 대기
powershell -ExecutionPolicy Bypass -File scripts\ops\start-backend.ps1    # 관리 서버 127.0.0.1:8000 + 앱 API 127.0.0.1:8020
powershell -ExecutionPolicy Bypass -File scripts\ops\status.ps1           # 컨테이너·스키마 버전·소유 모드·프로세스·HTTP·Writer
powershell -ExecutionPolicy Bypass -File scripts\ops\verify.ps1           # 비파괴 점검(앱 API OpenAPI 검증, 관리 API, 행 수)
powershell -ExecutionPolicy Bypass -File scripts\ops\stop-backend.ps1
powershell -ExecutionPolicy Bypass -File scripts\ops\stop-oracle.ps1      # podman stop -t 120 (백엔드가 돌면 거부, -Force로 강제)
```

- `start-backend.ps1`은 이미 실행 중이거나 포트가 쓰이면 거부한다(중복 실행 방지). 운영 스키마가 준비되지 않았으면 시작하지 않는다. 시작한 프로세스는 레거시 수집기 스위치·자동 실행을 끈 상태로 뜬다.
- 기본 실행은 **수집기 없는 관리·관측 모드**다(`--backend oracle-production --no-schedulers`): 음식 검토·별칭·추적·계획·관측·B8은 동작하고, 수집 작업은 Live 승인 실행에서만 나간다. Mock 수집은 운영에서 항상 거부된다.
- Oracle 정지는 반드시 `stop-oracle.ps1`(120초 유예)로 한다. `podman stop` 기본 10초는 SIGKILL로 끝나 다음 시작 때 크래시 복구가 돈다.
- Oracle만 재시작해도 관리 서버·앱 API는 재시작 없이 다시 연결된다(정지 중 앱 API는 503).

## 2. 관리 화면과 API

- 관리 화면: `http://127.0.0.1:8000`(화면이 관리 토큰을 붙인다). 스크립트로 관리 POST를 보낼 때는 `X-MZ-Admin-Token: (data/ops-prod/run/admin-token 내용)`.
- 앱 API: `http://127.0.0.1:8020/api/v1`(읽기 전용, 루프백 전용). 앱 개발자 인계는 [APP_HANDOFF.md](APP_HANDOFF.md).

## 3. 수집 상태와 사용량

```powershell
. .\scripts\ops\common.ps1; Import-ProductionEnv
& $Python -B -m food_trends --backend oracle-production --writer-status          # 수집원별 Writer·이전/철회 이력·열린 사용량
& $Python -B -m food_trends --backend oracle-production --execution-readiness data\ops-prod\registry.json   # Live 게이트(읽기 전용)
```

관리 API: `GET /api/execution/status`(작업 상태별 건수·예산), `GET /api/execution/jobs/{id}`(작업별 실제 요청 = `calls`), `GET /api/status`(RSS 마지막 회차). 앱 API: `GET /api/v1/sources/status`.

## 4. 승인된 수동 수집 (Live)

지속 수집 권한·예산 승인 전까지는 수동 소량 실행만 한다.

```powershell
. .\scripts\ops\common.ps1; Import-ProductionEnv
& $Python -B scripts\ops\live_pilot.py --registry data\ops-prod\registry.json --source-copy data\ops-prod\production-source-v11.sqlite3 --approved-by "<승인자>" --food "<승인된 음식>" --output data\ops-prod\pilot-<날짜>.json
```

- 하네스는 수집원별 상한(RSS 1·Instagram 5·YouTube 2·NAVER 1)을 넘는 요청을 보내기 전에 거부하고, 예산 설정을 상한 1로 둔다. 같은 날 두 번째 실행은 예산이 요청 없이 막는다.
- 401·403은 계정 차단으로 기록되며 자격 증명 재검토 전 재시도하지 않는다. 429는 차단 기간까지 재시도하지 않는다.
- 지속 자동 수집을 켜려면(별도 승인 후): `--backend oracle-production --execution-mode live --execution-registry data\ops-prod\registry.json`(RSS 30분 예약 포함, `--no-schedulers` 제외) + `--execution-automatic` + `B3_AUTOMATIC_ENABLED=true`. 현재 상태는 CONFIGURED_NOT_ENABLED.

## 5. 유지보수

```powershell
& $Python -B -m food_trends --backend oracle-production --maintenance    # 보존 만료·스테이징 만료·언급 만료·별칭 재처리(API 호출 없음)
```

하루 1회 권장. Instagram 캡션 30일, YouTube 29일, NAVER 값 미저장 정책을 유지한다.

## 6. 백업

- Oracle 데이터는 볼륨 `mz-oracle-data`에 있다. 일관된 백업은 백엔드 정지 → `stop-oracle.ps1` → WSL에서 `podman volume export mz-oracle-data -o <경로>.tar`(또는 Data Pump `expdp`) → `start-oracle.ps1`. 백업 파일은 `C:` 여유 공간(현재 약 28GB)을 확인하고 Git 밖에 둔다.
- 운영 SQLite v0 백업과 v11 사본은 그대로 보관한다(삭제 금지).

## 7. 장애 대응

| 증상 | 조치 |
| --- | --- |
| 앱 API 503, 관리 API 500 | `status.ps1` → Oracle 정지 상태면 `start-oracle.ps1`(백엔드 재시작 불필요) |
| `start-backend.ps1`이 `production schema not ready` | Oracle 미기동 또는 접속 정보·승인 기록 문제. `--runtime-status oracle-production`의 오류 코드 확인(`production_approval_missing`, `production_schema_mismatch` 등) |
| 프로세스가 죽음 | `data/ops-prod/logs/*.err.log` 확인 → `stop-backend.ps1` → `start-backend.ps1` |
| 수집 작업이 `RETRY_WAIT`/`BLOCKED` | `GET /api/execution/jobs/{id}`의 사유. 예산·차단·Writer 철회 사유는 다음 창이나 재승인까지 기다린다 |
| 작업 중 프로세스 종료 | 작업 Lease가 만료되면 다음 런타임이 이어받는다(중복 요청 없음, 보낸 요청은 `UNKNOWN`으로 집계) |

## 8. 롤백 (Oracle → SQLite)

전환을 되돌릴 때만, 이 순서로(이중 수집 금지):

1. 수집원마다 Writer 철회: `& $Python -B -m food_trends --backend oracle-production --writer-release <source> --execution-registry data\ops-prod\registry.json --release-wait 120` → 모두 `RELEASED`.
2. 백엔드 정지(`stop-backend.ps1`), 진행 중 작업·Lease 0 확인(`--writer-status`).
3. 사용량 인계: `& $Python -B -m food_trends --writer-handover <source> --recovery-target oracle --isolated-database data\ops-prod\production-source-v11.sqlite3`.
4. 역방향 복구: `--recovery plan` → `--recovery apply --recovery-target oracle --isolated-database data\ops-prod\production-source-v11.sqlite3 --apply-updates` — `converged`, `updates_applied`, 남은 갱신·충돌 0.
5. 재개 판정: `--legacy-resume-check <source> --recovery-target oracle --isolated-database data\ops-prod\production-source-v11.sqlite3` → `ready: true`.
6. SQLite 운영으로 돌아가려면 그 v11 사본을 새 운영 경로로 쓰는 별도 승인(G-OPS)이 필요하다. 원본 v0 파일을 덮어쓰지 않는다. `ready`가 아닌 수집원의 레거시 수집기는 켜지 않는다.

## 9. 금지 사항

- 운영 스키마에 테스트 초기화(`oracle_support.reset_target_schema`)를 실행하지 않는다(코드가 `*_TEST`가 아닌 계정을 거부한다).
- `ORACLE_TEST_*`에 운영 계정을 넣지 않는다(`oracle_test_schema_required`로 거부).
- 승인 기록·registry의 근거를 지어내지 않는다.
- 관리 서버(8000)를 외부에 공개하지 않는다.
