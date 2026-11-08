# 최종 인수 점검 — ORACLE-PRODUCTION-CUTOVER-1

| 검증 항목 | 판정 | 증거 |
| --- | --- | --- |
| 영구 Oracle DB | **PASS** | 운영 스키마 `MZ_APP`(MZ_TEST와 별도, CREATE SESSION·CREATE TABLE만), 대상 스키마 v10, 영속 볼륨 `mz-oracle-data`, 정상 종료·재시작 후 데이터 동일([persistence-before.json](persistence-before.json) = [persistence-after.json](persistence-after.json)) |
| 기존 데이터 이관 | **PASS** | 백업 검증 → 원본 보존 v11 사본 → 15,671행 적재, 대조 consistent, 누락·중복·FK·정책 위반·만료 복원 0, 재실행 멱등, B8 34건 동일([MIGRATION_REPORT.md](MIGRATION_REPORT.md)). 원본에 음식 개체가 없어 이관 음식은 0건(사실) |
| 운영 Runtime | **PASS** | `--backend oracle-production` 기동, 소유 모드 RUNTIME_PRODUCTION, 운영 접근 확인(스키마·승인 기록) 없으면 거부, Mock 실행 거부(`tests/test_production_access.py`, [failure-checks.json](failure-checks.json)) |
| 실제 API 수집 | **PASS**(소량) | 사용자 승인 registry로 RSS 1·Instagram 3·YouTube 2·NAVER 1 = 7회, 전부 HTTP 200, 상한 9 이내·초과 거부 0, 요청 수 = 실행 기록 = 저장 결과([LIVE_VERIFICATION.md](LIVE_VERIFICATION.md)). 지속 자동 수집은 CONFIGURED_NOT_ENABLED |
| 관리자 API | **PASS** | 운영 스키마에서 음식 승인·추적 등록·후보 거절·조회, 저장소 표시 `oracle_production`, 재시작 후 유지 |
| B8 API | **PASS** | `--store oracle-production`이 같은 스키마를 읽기 전용으로 조회, 실제 관측(두쫀쿠 2026-11-08 값 28 `OBSERVED`/`LIMITED`), 미관측일 `NOT_OBSERVED`, OpenAPI 위반 0([APP_HANDOFF.md](APP_HANDOFF.md)) |
| SQLite 독립성 | **PASS** | 실제 수집·정제·관측·분석 라운드의 sqlite3 연결 0. 예외(명시적 관리 작업): 이관용 사본 생성·적재·사용량 가져오기 |
| 재기동 영속성 | **PASS** | 백엔드·Oracle 정상 종료(`podman stop -t 120`, 크래시 복구 없음) 후 재시작, 행 수·음식·관측·후보·추적 동일 |
| 장애 복구 | **PASS** | Oracle 정지 중 앱 API 503·관리 API 500(세부 정보 비노출) → 재시작 후 백엔드 재시작 없이 회복. 중복 기동 거부, 잘못된 운영 설정 6종 거부, 레거시 서버 시작 불가. 파괴적 시나리오(Lease 만료·예산 소진·Writer 이전/철회 중단·복구)는 MZ_TEST 회귀 |
| 전체 테스트 | **PASS** | 기본 전체 534 통과·189 skip·실패 0·오류 0([default-tests](default-tests/test-summary.json)), MZ_TEST 전체 723/723 통과([oracle-full-tests](oracle-full-tests/test-summary.json)), 운영 스키마 비파괴 점검 실패 0([final-verify.json](final-verify.json)). 기존 717개 유지 + 신규 6개 |
| Git 통합 | **PASS** | 작업 브랜치 단계별 커밋·push 후 `main` fast-forward 통합·push([REPORT](REPORT.md) 10절) |

## 최종 판정: PASS — 운영 전환 완료

Oracle 운영 스키마가 기존 SQLite 데이터와 실제 수집 데이터를 영구 보관하고, 관리 서버와 앱 API가 같은 스키마를 쓰며, 재시작 후에도 데이터가 그대로이고, 사용자가 승인한 실제 외부 요청의 결과가 Oracle에 저장·조회됨을 실제로 입증했다. 단, 다음은 완료 범위 밖으로 남는다: **지속 자동 수집 CONFIGURED_NOT_ENABLED**(반복 호출의 지속 사용 권한·예산 승인 필요), 제공자 측 사용량 UNKNOWN, 운영 API 루프백 전용(외부 공개·TLS·인증 별도), 백엔드 무인 자동 시작 미구성(수동 스크립트), DML 전용 실행 계정 미분리.
