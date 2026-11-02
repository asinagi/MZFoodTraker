# 운영 승인 게이트 G-OPS-0 ~ 6 (B6~B7 갱신)

2026-11-02. 기존 [B4~B5 게이트](../development-b4-b5-2026-10-28/DEPLOYMENT_GATES.md)를 유지하고 B6~B7 의존성과 G-OPS-0(개발 Oracle 준비), G-OPS-6(Oracle 운영 전환)을 추가한다. **어떤 게이트도 실행하지 않았다. 모두 사용자 승인 대기다.**

## 현재 상태 (읽기 전용 확인)

| 항목 | 값 |
| --- | --- |
| 운영 DB | `data/trends.sqlite3`, v0(30개 테이블), integrity ok, 마지막 쓰기 2026-10-28 14:20(로컬) |
| 서버·Writer | 8000/8002 리스너 없음, Python 프로세스 없음. 세션 시작 시 이미 정지 상태였고 이번 작업에서 중지·재시작하지 않았다 |
| 코드 스키마 | SQLite v11(v11 = Shadow 변경 기록, 기본 비활성), 대상 스키마 v1 |
| Oracle | 설치·인스턴스·드라이버·컨테이너 런타임 없음 |

서버가 꺼져 있으므로 G-OPS-2(Writer 정지)는 현재 "추가 정지 불필요" 상태다. 다만 재기동 전에 G-OPS-1·3을 먼저 수행해야 한다. 현재 코드로 기동하면 v0 DB에서 `MigrationRequired`로 중단된다.

## G-OPS-0 개발용 Oracle 준비 (신규, B6 완료 조건)

| 요청 항목 | 내용 |
| --- | --- |
| 작업 | (1) Python 드라이버 `python-oracledb` 설치(Thin 모드, 설치 시점의 안정 버전으로 고정). (2) 개발 Oracle 인스턴스: Oracle AI Database Free 로컬 설치, 또는 기존 개발 인스턴스 제공. 격리 스키마(예: `MZ_TEST`) 생성 |
| 변경 대상 | 사용자 Python 환경(패키지 1개 추가), 로컬 시스템(DB 설치 시 관리자 권한·디스크 수 GB·메모리 2GB 이상) |
| 권한 | 패키지 설치 승인, DB 설치 시 Windows 관리자 권한. 클라우드 자원은 과금 가능하므로 사용하지 않음 |
| 실행 범위 | `ORACLE_TEST_DSN/USER/PASSWORD`, `ORACLE_TEST_ALLOW_RESET=true`로 `tests/test_target_compat.py`의 Oracle 15개 계약 테스트와 `--target-migrate oracle`, B7 복제본 이관을 실제 Oracle로 재실행 |
| 복구 | 격리 스키마 삭제, 패키지 제거. 운영 데이터 관여 없음 |
| 승인 사항 | 드라이버 설치, Oracle 설치 방식(로컬 설치·기존 인스턴스·컨테이너 런타임 추가 중 선택) |

## G-OPS-1 운영 SQLite 백업

[B4~B5 문서](../development-b4-b5-2026-10-28/DEPLOYMENT_GATES.md)와 같다. 추가로 백업 사본에도 출처 TTL을 적용하고, Oracle 이관용 임시 사본은 이관 후 삭제한다.

## G-OPS-2 Writer 정지

현재 정지 상태다. 재기동은 G-OPS-3 이후 preview 모드로 하고, 출처 수집 재개 여부는 G-OPS-4와 함께 결정한다.

## G-OPS-3 운영 SQLite 마이그레이션 (v0 → v11)

| 요청 항목 | 내용 |
| --- | --- |
| 작업 | `python -m food_trends --migrate` 한 번, 1~11 단일 트랜잭션 |
| 의존성 | v8(실행 결과 범위), v9(B4), v10(B5), v11(Shadow 변경 기록, 트리거는 `enabled=0`이면 기록하지 않음) |
| 사전 검증 | 직전 백업 복제본에서 `tests/verify_preserved_copy.py`, `tests/verify_b7_copy.py` 재실행. 보존 스냅샷 기준 0→11 통과 |
| 후속 선택 | `--ingest-pending`, `--ingest-legacy …`(API 호출 없음) |
| 분리 | 이 작업은 Oracle 이관과 별개다 |

## G-OPS-4 수집 소유권 이전 · G-OPS-5 Live 검증

[B4~B5 문서](../development-b4-b5-2026-10-28/DEPLOYMENT_GATES.md)와 같다. Instagram `recent_media`가 고유 태그 예산을 소모하지 않는다는 미검증 전제는 G-OPS-5 확인 항목으로 남아 있다.

## G-OPS-6 Oracle 운영 전환 (신규)

| 요청 항목 | 내용 |
| --- | --- |
| 선행 조건 | G-OPS-0 완료와 실제 Oracle 계약 테스트 전부 통과, G-OPS-1·3 완료 |
| 운영 Oracle | 서버·버전·문자셋(AL32UTF8)·전용 스키마·최소 권한 계정(CREATE TABLE/INDEX, 자기 스키마 DML만). 자격 증명은 환경변수, 소스·로그·보고서에 미기록. 네트워크는 앱 호스트에서만 접근, TLS(TCPS) 권장 |
| 접속 보호 | `ORACLE_ENVIRONMENT=production`은 `ORACLE_ALLOW_PRODUCTION=true` 없이는 연결 거부(코드 구현됨) |
| 1단계 스키마 | `--target-migrate oracle`(체크섬 기록, 재실행 무변화, 중단 시 재개) |
| 2단계 초기 이관 | 운영 SQLite의 **새 백업 사본**에서 `target_migration.migrate`; 실행 시점 `now`로 만료분 제외, 제외·거부 사유 기록 |
| 3단계 대조 | `reconcile`: 누락·초과·변경 0, 테이블 해시 일치, FK 0, 원본 참조 미해결 0, 정책 위반 0, 만료 0, FOOD_ID 조회 일치 |
| 4단계 Shadow | `--shadow enable` 후 `--shadow deliver`를 주기 실행, `--shadow compare`로 불일치 감시. SQLite가 기록 시스템이며 수집은 한 번만 실행 |
| 5단계 최종 동기화·전환 | Writer 정지 → 미전달 이벤트 0 → 최종 compare 일치 → 저장소 전환. **서비스 계층의 Oracle 직접 쓰기 구현(현재 미구현)이 이 단계의 전제** |
| 원상 복귀 | 전환 전: Oracle을 버리고 SQLite 계속 사용(데이터 손실 없음). 전환 후: Oracle에만 생긴 행을 SQLite로 되돌리는 역방향 동기화가 필요하며 **아직 구현되지 않았다**. 연결 문자열만 바꾸는 것은 복구로 인정하지 않는다 |
| 성능·용량·비용 | 개발 대역 측정은 참고치뿐이다(REPORT 12장). 실제 Oracle에서 적재·조회 지연과 용량을 다시 측정하고, Free 에디션 제한(12GB 사용자 데이터 등)과 라이선스·호스팅 비용을 확인해야 한다 |
| 승인 사항 | 운영 Oracle 생성, 초기 이관 실행 시각, Shadow 활성화, 전환 일정과 원상 복귀 기준 |
